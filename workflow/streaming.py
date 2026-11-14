"""
Streaming percentile pipeline: offer outcome -> cohort event -> recompute -> live push.

  1. Something changes the benchmark network: an outcome is recorded (form, CSV, or
     an HRIS/ATS webhook), a partner batch is imported or removed.
  2. The affected cohort key (role | level | city tier | funding stage) is queued for
     the request and published to the event bus after the request commits.
  3. A worker recomputes that cohort's rolling-window percentiles (recent offers
     weighted up) and its clearing price, stores an append-only cohort snapshot,
     raises a notification when the cohort tips into a market alert, and broadcasts
     the new snapshot.
  4. Every open /app/market page subscribed to that cohort gets it over
     Server-Sent Events and redraws its percentile bar without a reload.

Bus backends (COMPINTEL_STREAM_BACKEND):
  inprocess (default)  a coalescing worker thread. Bulk imports that touch the same
                       cohort 500 times cause one recompute, not 500. Single process.
  redis                Redis Streams (XADD / consumer group / XACK) for the work queue
                       and Redis pub/sub for the fan-out, so it works across gunicorn
                       workers and hosts. Needs `pip install redis` and
                       COMPINTEL_REDIS_URL.
  sync                 recompute inline after the request (tests, scripts).

Observation dates are kept at month granularity on purpose (privacy coarsening in
benchmark_observations). Each observation is dated to the 15th of its month, so a
"90-day window" is the months whose mid-point is at most 90 days old, and "recent"
(weighted up) is mid-point at most 30 days old.

These live numbers are a *view*. A submitted offer keeps the benchmark frozen into
its snapshot; Re-benchmark is how an in-flight offer adopts newer data.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import date

from flask import g, has_request_context

from saas_domain import CITIES

from . import clearing
from . import db

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = "cohort-snapshot/1"


# --------------------------------------------------------------------------
# Cohort keys and weighted percentiles
# --------------------------------------------------------------------------

def cohort_key(role: str, level: str, city_tier: str, funding_stage: str) -> str:
    return "|".join((role, level, city_tier, funding_stage))


def cohort_key_for_city(role, level, city, funding_stage) -> str:
    return cohort_key(role, level, CITIES[city][0], funding_stage)


def parse_key(key: str) -> dict:
    role, level, tier, stage = key.split("|")
    return {"role": role, "level": level, "city_tier": tier, "funding_stage": stage}


def label_for(key: str) -> str:
    return " · ".join(key.split("|"))


def weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float:
    """Weighted generalisation of PERCENTILE_CONT / pandas' linear quantile: with
    equal weights it returns exactly the same number. Each sorted value sits at
    position (cum_weight_before + w/2 - w_first/2) / (total - w_first/2 - w_last/2)."""
    pts = sorted(pairs)
    if not pts:
        raise ValueError("no values")
    if len(pts) == 1:
        return pts[0][0]
    total = sum(w for _v, w in pts)
    w_first, w_last = pts[0][1], pts[-1][1]
    denom = total - w_first / 2 - w_last / 2
    positions, cum = [], 0.0
    for _v, w in pts:
        positions.append((cum + w / 2 - w_first / 2) / denom)
        cum += w
    if q <= positions[0]:
        return pts[0][0]
    for i in range(len(pts) - 1):
        if positions[i] <= q <= positions[i + 1]:
            span = positions[i + 1] - positions[i]
            t = (q - positions[i]) / span if span else 0.0
            return pts[i][0] + t * (pts[i + 1][0] - pts[i][0])
    return pts[-1][0]


def _age_days(month: str, today: date) -> int:
    y, m = (int(p) for p in month.split("-")[:2])
    return max(0, (today - date(y, m, 15)).days)


def _month_floor(today: date, days: int) -> str:
    """Earliest YYYY-MM whose mid-month could fall inside `days`."""
    total = today.year * 12 + today.month - 1 - (days // 28 + 1)
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def compute_snapshot(key: str, settings: dict, today: date | None = None) -> dict:
    """Rolling, recency-weighted P25/P50/P75 for one cohort, plus its clearing price.
    Reads benchmark_observations only (consented, anonymised)."""
    today = today or date.today()
    c = parse_key(key)
    window = int(settings.get("live_window_days", 90))
    recent_days = int(settings.get("live_recent_days", 30))
    recent_w = float(settings.get("live_recent_weight", 2.0))
    min_n = int(settings.get("min_cohort_size", 10))
    min_contrib = int(settings.get("min_contributors", 3))
    lookback_months = int(settings.get("pilot_lookback_months", 24))
    floor = min(_month_floor(today, 2 * window), _month_floor(today, lookback_months * 30))
    rows = [dict(r) for r in db.query(
        "SELECT * FROM benchmark_observations WHERE role = ? AND level = ? AND city_tier = ? AND funding_stage = ? "
        "AND observed_month >= ?", (c["role"], c["level"], c["city_tier"], c["funding_stage"], floor))]
    current, previous, reference = [], [], []
    for r in rows:
        age = _age_days(r["observed_month"], today)
        r["_w"] = recent_w if age <= recent_days else 1.0
        if age <= lookback_months * 30 and r["status"] == "accepted" and r["final_cash_ctc"] is not None:
            reference.append(r)
        if age <= window:
            current.append(r)
        elif age <= 2 * window:
            previous.append(dict(r, _w=1.0))
    accepted = [r for r in current if r["status"] == "accepted" and r["final_cash_ctc"] is not None]
    contributors = len({r["contributor_key"] for r in accepted})
    weights = [r["_w"] for r in accepted]
    snap = {
        "schema": SNAPSHOT_SCHEMA, "cohort_key": key, "label": label_for(key), **c,
        "computed_at": db.now_iso(), "as_of": today.isoformat(),
        "window_days": window, "recent_days": recent_days, "recent_weight": recent_w,
        "n": len(accepted), "contributors": contributors,
        "recent_n": sum(1 for w in weights if w > 1.0),
        "effective_n": round(sum(weights) ** 2 / sum(w * w for w in weights), 1) if weights else 0,
        "latest_month": max((r["observed_month"] for r in current), default=None),
        "min_n": min_n, "min_contributors": min_contrib,
        "p25": None, "p50": None, "p75": None, "suppressed": True, "reason": None,
    }
    if len(accepted) < min_n or contributors < min_contrib:
        snap["reason"] = (f"Below the privacy threshold: {len(accepted)} accepted offers from {contributors} "
                          f"companies in the last {window} days (needs {min_n} from {min_contrib}).")
    else:
        pairs = [(r["final_cash_ctc"], r["_w"]) for r in accepted]
        snap.update(suppressed=False, **{k: round(weighted_quantile(pairs, q), 2)
                                          for k, q in (("p25", 0.25), ("p50", 0.50), ("p75", 0.75))})
    # The alert's anchor is the benchmark P50 recruiters actually anchor on: accepted offers
    # over the full lookback, unweighted (what the observed-offer benchmark shows). The live
    # P50 can't be the anchor: when asks rise, only high offers get accepted, so accepted-offer
    # percentiles drift up with the market and hide exactly the shift we're looking for.
    ref_contrib = len({r["contributor_key"] for r in reference})
    if len(reference) >= min_n and ref_contrib >= min_contrib:
        ref_values = sorted(r["final_cash_ctc"] for r in reference)
        snap["reference_p50"] = round(weighted_quantile([(v, 1.0) for v in ref_values], 0.5), 2)
        snap["reference_label"] = f"{lookback_months}-month accepted-offer P50"
    else:
        snap["reference_p50"] = snap["p50"]
        snap["reference_label"] = f"live {window}-day P50"
    snap["reference_n"] = len(reference)
    decided = [r for r in current if r["status"] in ("accepted", "declined")]
    prev_decided = [r for r in previous if r["status"] in ("accepted", "declined")]
    snap["clearing"] = clearing.compute(decided, [r["_w"] for r in decided], snap["reference_p50"], settings,
                                        previous=(prev_decided, [r["_w"] for r in prev_decided]),
                                        anchor_label=snap["reference_label"])
    snap["alert"] = bool(snap["clearing"]["alert"])
    return snap


def latest_snapshot(key: str) -> dict | None:
    row = db.query("SELECT data FROM cohort_snapshots WHERE cohort_key = ? ORDER BY id DESC LIMIT 1", (key,), one=True)
    return db.loads(row["data"]) if row else None


def persist(snap: dict, trigger: str) -> int:
    """Append-only history of what the live view showed and when, plus a one-time
    notification when a cohort crosses into a market alert."""
    prev = latest_snapshot(snap["cohort_key"])
    sid = db.execute(
        "INSERT INTO cohort_snapshots (cohort_key, computed_at, trigger, n, contributors, p25, p50, p75, suppressed, "
        "alert, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (snap["cohort_key"], snap["computed_at"], trigger, snap["n"], snap["contributors"], snap["p25"], snap["p50"],
         snap["p75"], int(snap["suppressed"]), int(snap["alert"]), db.dumps(snap)))
    if snap["alert"] and not (prev and prev.get("alert")):
        company = db.query("SELECT id FROM companies ORDER BY id LIMIT 1", one=True)
        if company is not None:
            from urllib.parse import urlencode
            link = "/app/market?" + urlencode({"role": snap["role"], "level": snap["level"],
                                               "tier": snap["city_tier"], "stage": snap["funding_stage"]})
            for role in ("recruiter", "finance", "exec"):
                db.execute("INSERT INTO notifications (company_id, role, message, link, created_at) "
                           "VALUES (?, ?, ?, ?, ?)",
                           (company["id"], role, f"Market alert, {snap['label']}: {snap['clearing']['message']}",
                            link, db.now_iso()))
    return sid


def _settings() -> dict:
    from .routes import settings_for  # lazy: routes imports this module
    company = db.query("SELECT * FROM companies ORDER BY id LIMIT 1", one=True)
    return settings_for(company)


# --------------------------------------------------------------------------
# Local fan-out to SSE subscribers
# --------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs: dict[str, set] = {}

    def subscribe(self, key: str) -> queue.Queue:
        q = queue.Queue(maxsize=20)
        with self._lock:
            self._subs.setdefault(key, set()).add(q)
        return q

    def unsubscribe(self, key: str, q) -> None:
        with self._lock:
            subs = self._subs.get(key)
            if subs:
                subs.discard(q)
                if not subs:
                    del self._subs[key]

    def deliver(self, key: str, message: dict) -> int:
        with self._lock:
            subs = list(self._subs.get(key, ()))
        for q in subs:
            try:
                q.put_nowait(message)
            except queue.Full:  # a stalled browser tab drops updates rather than blocking the worker
                pass
        return len(subs)

    def count(self, key: str | None = None) -> int:
        with self._lock:
            return len(self._subs.get(key, ())) if key else sum(len(s) for s in self._subs.values())


# --------------------------------------------------------------------------
# Bus backends
# --------------------------------------------------------------------------

class SyncBus:
    name = "sync"

    def __init__(self, handler, hub):
        self.handler, self.hub = handler, hub

    def publish(self, key):
        self.handler(key)

    def broadcast(self, key, message):
        self.hub.deliver(key, message)


class InProcessBus:
    """Coalescing work queue + background thread, for a single process."""
    name = "inprocess"

    def __init__(self, handler, hub):
        self.handler, self.hub = handler, hub
        self._pending: set[str] = set()
        self._cond = threading.Condition()
        self._thread = None

    def publish(self, key):
        with self._cond:
            self._pending.add(key)
            self._cond.notify()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="compintel-cohort-worker", daemon=True)
                self._thread.start()

    def broadcast(self, key, message):
        self.hub.deliver(key, message)

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until the queue is empty (used by tests and graceful shutdown)."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._cond:
                if not self._pending and not getattr(self, "_busy", False):
                    return True
            time.sleep(0.02)
        return False

    def _run(self):
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                keys, self._pending = self._pending, set()
                self._busy = True
            for key in sorted(keys):
                try:
                    self.handler(key)
                except Exception:
                    logger.exception("Cohort recompute failed for %s", key)
            with self._cond:
                self._busy = False


class RedisStreamsBus:
    """Work queue on a Redis Stream with a consumer group (each event processed once
    across all processes; un-acked events stay pending for redelivery), fan-out on
    pub/sub (every process relays to its own SSE subscribers)."""
    name = "redis"
    STREAM = "compintel:cohort-events"
    GROUP = "compintel-recompute"
    CHANNEL = "compintel:cohort-updated"

    def __init__(self, client, handler, hub, consumer: str | None = None):
        self.client, self.handler, self.hub = client, handler, hub
        self.consumer = consumer or f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}-{os.getpid()}"
        self._started = False
        self._lock = threading.Lock()

    @staticmethod
    def _s(value):
        return value.decode() if isinstance(value, (bytes, bytearray)) else value

    def ensure_group(self):
        try:
            self.client.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP: already exists
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, key):
        self.client.xadd(self.STREAM, {"key": key}, maxlen=10000, approximate=True)
        self.start()

    def broadcast(self, key, message):
        self.client.publish(self.CHANNEL, json.dumps({"key": key, "message": message}, default=str))

    def process_once(self, block_ms: int = 5000) -> int:
        resp = self.client.xreadgroup(self.GROUP, self.consumer, {self.STREAM: ">"}, count=200, block=block_ms)
        ids_by_key: dict[str, list] = {}
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                fields = {self._s(k): self._s(v) for k, v in fields.items()}
                ids_by_key.setdefault(fields["key"], []).append(msg_id)
        done = 0
        for key, ids in ids_by_key.items():  # coalesced: one recompute per cohort per batch
            try:
                self.handler(key)
            except Exception:
                logger.exception("Cohort recompute failed for %s; left pending for redelivery", key)
                continue
            self.client.xack(self.STREAM, self.GROUP, *ids)
            done += 1
        return done

    def relay_once(self, pubsub, timeout: float = 1.0) -> bool:
        msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if not msg or msg.get("type") != "message":
            return False
        data = json.loads(self._s(msg["data"]))
        self.hub.deliver(data["key"], data["message"])
        return True

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        self.ensure_group()
        pubsub = self.client.pubsub()
        pubsub.subscribe(self.CHANNEL)

        def work():
            while True:
                try:
                    self.process_once()
                except Exception:
                    logger.exception("Redis stream worker error")

        def relay():
            while True:
                try:
                    self.relay_once(pubsub)
                except Exception:
                    logger.exception("Redis pub/sub relay error")

        threading.Thread(target=work, name="compintel-redis-worker", daemon=True).start()
        threading.Thread(target=relay, name="compintel-redis-relay", daemon=True).start()


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

class Pipeline:
    def __init__(self):
        self.app = None
        self.hub = Hub()
        self.bus = None
        self.recomputes = 0

    def init_app(self, app):
        self.app = app
        backend = (app.config.get("COMPINTEL_STREAM_BACKEND") or os.environ.get("COMPINTEL_STREAM_BACKEND")
                   or "inprocess").lower()
        if backend == "redis":
            url = app.config.get("COMPINTEL_REDIS_URL") or os.environ.get("COMPINTEL_REDIS_URL")
            try:
                import redis  # optional dependency
                self.bus = RedisStreamsBus(redis.Redis.from_url(url), self._handle, self.hub)
                self.bus.start()
            except Exception:
                logger.exception("Redis stream backend unavailable (%s); falling back to in-process.", url)
                self.bus = InProcessBus(self._handle, self.hub)
        elif backend == "sync":
            self.bus = SyncBus(self._handle, self.hub)
        else:
            self.bus = InProcessBus(self._handle, self.hub)
        app.after_request(self._flush)

    @property
    def backend(self) -> str:
        return self.bus.name if self.bus else "none"

    def _sync(self) -> bool:
        return bool(self.app.config.get("TESTING")) or self.backend == "sync"

    def recompute(self, key: str, trigger: str = "event") -> dict:
        """Recompute, store and return a cohort snapshot (own app context and DB connection)."""
        with self.app.app_context():
            snap = compute_snapshot(key, _settings())
            snap["snapshot_id"] = persist(snap, trigger)
            db.commit()
        self.recomputes += 1
        return snap

    def _handle(self, key: str):
        snap = self.recompute(key)
        self.bus.broadcast(key, snap)

    def publish(self, key: str):
        if self._sync():
            snap = self.recompute(key)
            self.hub.deliver(key, snap)
        else:
            self.bus.publish(key)

    def queue(self, key: str):
        """Queue a cohort for recompute once the current request has committed (so a
        worker on another connection sees the new rows). Outside a request: now."""
        if has_request_context():
            g.setdefault("_compintel_cohorts", set()).add(key)
        else:
            self.publish(key)

    def _flush(self, response):
        keys = g.pop("_compintel_cohorts", None)
        if keys and response.status_code < 500:
            for key in sorted(keys):
                try:
                    self.publish(key)
                except Exception:
                    logger.exception("Failed to publish cohort event %s", key)
        return response


pipeline = Pipeline()


def queue_observation(row) -> None:
    """Queue the cohort an observation row belongs to."""
    pipeline.queue(cohort_key(row["role"], row["level"], row["city_tier"], row["funding_stage"]))
