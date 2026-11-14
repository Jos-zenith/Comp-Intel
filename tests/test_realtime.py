"""V3 real-time layer: CTC rule engine + API, HRIS/ATS integrations, streaming
percentiles, clearing price."""
import json
import random
import time
from datetime import date

import pytest

import ctc_engine
import main
from helpers import as_role, benchmark, demo, new_offer, package_form, post, q, run_sql
from workflow import clearing, pilot, streaming
from workflow import db as wdb
from workflow.integrations import providers as prov
from workflow.integrations.schema import (NormalizationError, map_level, map_role, normalize,
                                          parse_money_inr)
from workflow.realtime_routes import demo_network_rows
from workflow.routes import DEFAULT_SETTINGS

SE_KEY = "Software Engineer|Senior|Tier 1 SaaS Hub|Series A"


# ======================================================================= C. CTC rule engine

def test_rulesets_load_select_by_version_and_date():
    versions = [r.version for r in ctc_engine.load()]
    assert versions == ["2026.04-r1", "2026.04-r2"]
    assert ctc_engine.active().version == "2026.04-r2"
    assert ctc_engine.ruleset_for(version="2026.04-r1").status == "superseded"
    assert ctc_engine.ruleset_for(as_of=date(2026, 6, 1)).version == "2026.04-r2"
    with pytest.raises(ctc_engine.RulesetError):
        ctc_engine.ruleset_for(as_of=date(2025, 1, 1))
    with pytest.raises(ctc_engine.RulesetError):
        ctc_engine.ruleset_for(version="1999.01-r1")


def test_malformed_ruleset_fails_loudly(tmp_path):
    folder = tmp_path / "in_ctc"
    folder.mkdir()
    good = json.loads((ctc_engine.RULESET_DIR / "in_ctc" / "2026.04-r2.json").read_text())
    good["rules"][0]["type"] = "made_up_rule"
    (folder / "bad.json").write_text(json.dumps(good))
    with pytest.raises(ctc_engine.RulesetError, match="unknown rule type"):
        ctc_engine.load(directory=tmp_path, refresh=True)


def test_engine_structure_numbers_are_unchanged():
    s = ctc_engine.structure(30, 3, "Bengaluru")
    by = {r["component"]: r["inr"] for r in s["rows"]}
    assert by["Basic + DA"] == 1_500_000 and by["Employer EPF"] == 180_000 and by["Gratuity (accrued)"] == 72_150
    assert by["HRA"] == 750_000 and s["sum_inr"] == s["total_inr"] == 3_000_000 and s["fits"]


def test_r1_and_r2_run_different_wage_rules():
    payload = {"cash_ctc_lpa": 30, "variable_lpa": 3, "city": "Bengaluru"}
    r2 = {c["id"] for c in ctc_engine.validate(payload)["checks"]}
    r1 = {c["id"] for c in ctc_engine.validate({**payload, "ruleset_version": "2026.04-r1"})["checks"]}
    assert "ctc_wage" in r2 and "ctc_basic" not in r2
    assert "ctc_basic" in r1 and "ctc_wage" not in r1 and "epf_ceiling" not in r1


def test_ctc_validate_api(client):
    r = client.post("/api/v1/ctc/validate", json={"cash_ctc_lpa": 24, "variable_lpa": 2.4, "city": "Bangalore"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["compliant"] and data["city"] == "Bengaluru" and data["structure"]["is_metro"]
    assert data["ruleset"]["version"] == "2026.04-r2" and "Decision support only" in data["disclaimer"]
    assert data["structure"]["sum_inr"] == data["structure"]["total_inr"]

    too_much_variable = client.post("/api/v1/ctc/validate", json={"cash_ctc_lpa": 20, "variable_lpa": 12,
                                                                  "city": "Pune"}).get_json()
    assert not too_much_variable["compliant"] and too_much_variable["status"] == "block"
    assert too_much_variable["checks"][0]["id"] == "ctc_fit"

    # An already-decided breakup with EPF at 8% of Basic is blocked; HRA too low is only a warning.
    decided = client.post("/api/v1/ctc/validate", json={
        "cash_ctc_lpa": 20, "city": "Chennai",
        "components": {"basic_lpa": 10, "hra_lpa": 3, "employer_epf_lpa": 0.8, "gratuity_lpa": 0.481}}).get_json()
    statuses = {c["id"]: c["status"] for c in decided["checks"]}
    assert statuses["ctc_epf"] == "block" and statuses["ctc_hra"] == "warn" and not decided["compliant"]

    form = client.post("/api/v1/ctc/validate", data={"cash_ctc_lpa": "18", "city": "Kochi"})
    assert form.status_code == 200 and not form.get_json()["structure"]["is_metro"]
    assert client.post("/api/v1/ctc/validate", json={"city": "Pune"}).status_code == 400
    assert client.post("/api/v1/ctc/validate", json={"cash_ctc_lpa": "abc", "city": "Pune"}).status_code == 400
    assert client.post("/api/v1/ctc/validate", json={"cash_ctc_lpa": 20, "city": "Pune",
                                                     "as_of": "2024-01-01"}).status_code == 422
    rulesets = client.get("/api/v1/ctc/rulesets").get_json()
    assert rulesets["active"]["version"] == "2026.04-r2" and len(rulesets["rulesets"]) == 2


def test_workflow_checks_cite_the_engine_ruleset(client):
    demo(client)
    rid = new_offer(client)
    html = post(client, f"/app/offers/{rid}/evaluate", package_form(benchmark(rid))).get_data(as_text=True)
    assert "IN-CTC v2026.04-r2" in html and "Employer EPF = 12% of Basic" in html and "EPF wage ceiling" in html


# ======================================================================= A. Integrations

def test_normalisation_vocabulary():
    assert map_role("SDE-2, Payments") == "Software Engineer"
    assert map_role("Senior QA Engineer") == "QA Engineer"
    assert map_role("Associate Product Manager") == "Product Manager"
    assert map_role("Site Reliability Engineer") == "DevOps / Cloud Engineer"
    assert map_role("Engineering Manager") is None  # not modelled -> rejected, never guessed
    assert map_level(None, "SDE II") == "Mid" and map_level("L5") == "Senior"
    assert map_level(None, "Software Engineer", years=7) == "Senior"
    assert parse_money_inr("₹18,00,000") == 1_800_000 and parse_money_inr("18 LPA") == 1_800_000
    assert parse_money_inr("1.2 Cr") == 12_000_000 and parse_money_inr("150k") == 150_000


def _raw(**kw):
    base = {"id": "x", "status": "accepted", "title": "Senior Backend Engineer", "location": "Bangalore, Karnataka",
            "currency": "INR", "fixed_amount": 3_600_000, "timestamp": "2026-11-01"}
    return {**base, **kw}


def test_normalise_rejects_rather_than_guesses():
    kw = dict(provider="greenhouse", kind="offer", default_currency=None, source_ref="r", company_stage="Series A")
    ev = normalize(_raw(variable_pct=10), **kw)
    assert (ev.role, ev.level, ev.city, ev.cash_ctc_lpa) == ("Software Engineer", "Senior", "Bengaluru", 39.6)
    assert ev.role_id == "software-engineer" and not hasattr(ev, "candidate_name")
    assert normalize(_raw(fixed_amount=300_000, pay_period="monthly"), **kw).fixed_ctc_lpa == 36.0
    for bad, field in ((dict(currency="USD"), "currency"), (dict(location="Mumbai"), "location"),
                       (dict(title="Chief of Staff"), "title"), (dict(fixed_amount=36), "fixed_amount"),
                       (dict(status=None), "status"), (dict(pay_period="hourly"), "pay_period")):
        with pytest.raises(NormalizationError) as exc:
            normalize(_raw(**bad), **kw)
        assert exc.value.field == field, bad
    with pytest.raises(NormalizationError, match="currency"):  # an ATS doesn't imply INR
        normalize(_raw(currency=None), **kw)


@pytest.mark.parametrize("key", sorted(prov.PROVIDERS))
def test_every_provider_sample_maps_to_the_same_offer_event(key):
    p = prov.provider(key)
    raws = prov.extract(p, prov.sample_payload(key, amount_lpa=42))
    assert len(raws) == 1
    ev = normalize(raws[0], provider=key, kind=p.kind, default_currency=p.default_currency, source_ref="r",
                   company_stage="Series A")
    assert (ev.role, ev.level, ev.city, ev.cash_ctc_lpa) == ("Software Engineer", "Senior", "Bengaluru", 42.0)
    assert ev.status == "accepted"  # ATS: accepted offer; HRIS: new joiner's first structure


@pytest.mark.parametrize("key", sorted(prov.PROVIDERS))
def test_signatures_verify_and_reject_tampering(key):
    p = prov.provider(key)
    secret = "whsec_dGVzdC1zZWNyZXQtZm9yLWNvbXBpbnRlbA"
    body, headers = prov.sign(p, secret, prov.sample_payload(key))
    prov.verify(p, secret, body, headers)
    tampered = body.replace(b"Senior", b"Junior")
    with pytest.raises(prov.SignatureError):
        prov.verify(p, secret, tampered, headers)
    with pytest.raises(prov.SignatureError):
        prov.verify(p, "whsec_b3RoZXItc2VjcmV0", body, headers)


def test_replayed_timestamped_delivery_is_rejected():
    p = prov.provider("finch")
    body, headers = prov.sign(p, "whsec_c2VjcmV0", prov.sample_payload("finch"), now=time.time() - 3600)
    with pytest.raises(prov.SignatureError, match="too old"):
        prov.verify(p, "whsec_c2VjcmV0", body, headers)


def test_every_signature_scheme_covers_the_body():
    """A scheme that signs only metadata (e.g. Lever's native token + triggeredAt) can't
    vouch for the offer amounts, so no provider may use one."""
    assert {p.signature for p in prov.PROVIDERS.values()} <= {"generic", "greenhouse", "merge", "finch"}


def _connect(client, provider="greenhouse"):
    as_role(client, "admin")
    r = post(client, "/app/integrations/connections", {"provider": provider, "name": f"{provider} test"})
    assert r.status_code == 200 and "shown once" in r.get_data(as_text=True)
    return q("SELECT * FROM integration_connections ORDER BY id DESC LIMIT 1")[0]


def _deliver(client, conn, payload):
    p = prov.provider(conn["provider"])
    body, headers = prov.sign(p, conn["secret"], payload)
    return client.post(f"/api/v1/integrations/hooks/{conn['hook_key']}", data=body,
                       headers={**headers, "Content-Type": "application/json"})


def test_webhook_end_to_end_is_idempotent_updates_status_and_stores_no_pii(client):
    demo(client)
    conn = _connect(client)
    sent = _deliver(client, conn, prov.sample_payload("greenhouse", status="sent", amount_lpa=40, record_id="gh-9"))
    assert sent.status_code == 200 and sent.get_json()["recorded"] == 1
    accepted_payload = prov.sample_payload("greenhouse", status="accepted", amount_lpa=43, record_id="gh-9")
    r = _deliver(client, conn, accepted_payload)
    assert r.get_json()["updated"] == 1
    outcomes = q("SELECT * FROM outcomes WHERE source_ref IS NOT NULL")
    assert len(outcomes) == 1  # same provider record -> same outcome
    o = outcomes[0]
    assert o["status"] == "accepted" and o["final_cash_ctc"] == 43.0 and o["initial_cash_ctc"] == 40.0
    assert o["created_by"] is None and o["external"] == 1
    # Redelivery of the exact same body is acknowledged and skipped.
    assert _deliver(client, conn, accepted_payload).get_json()["duplicate"] is True
    # A late "sent" can't downgrade the accepted outcome.
    late = _deliver(client, conn, prov.sample_payload("greenhouse", status="sent", amount_lpa=39, record_id="gh-9"))
    assert late.get_json()["stale"] == 1 and q("SELECT status FROM outcomes WHERE id = ?", (o["id"],))[0]["status"] == "accepted"
    # Shared to the network (consent on) and streamed: a cohort snapshot was stored.
    assert q("SELECT * FROM benchmark_observations WHERE outcome_id = ?", (o["id"],))
    assert q("SELECT * FROM cohort_snapshots WHERE cohort_key = ?", (SE_KEY,))
    # The sample payload carried a candidate name and email: none of it is stored anywhere.
    with main.app.app_context():
        dump = "\n".join(wdb.get_db().iterdump())
    assert "NotBeStored" not in dump and "candidate@example.com" not in dump
    audit = q("SELECT * FROM audit_log WHERE action = 'integration_delivery'")
    assert audit and audit[-1]["user_role"] == "integration" and audit[-1]["user_id"] is None


def test_webhook_auth_and_rejections(client):
    demo(client)
    conn = _connect(client)
    body = json.dumps(prov.sample_payload("greenhouse")).encode()
    r = client.post(f"/api/v1/integrations/hooks/{conn['hook_key']}", data=body, headers={"Signature": "sha256 nope"})
    assert r.status_code == 401
    assert "Signature rejected" in q("SELECT last_error FROM integration_connections WHERE id = ?", (conn["id"],))[0]["last_error"]
    assert not q("SELECT * FROM offer_events")  # unauthenticated deliveries never touch the log
    assert client.post("/api/v1/integrations/hooks/not-a-hook", data=body).status_code == 404
    usd = prov.sample_payload("greenhouse", record_id="gh-usd")
    usd["payload"]["offer"]["custom_fields"]["salary"]["value"]["unit"] = "USD"
    res = _deliver(client, conn, usd).get_json()
    assert res["rejected"] == 1 and "isn't INR" in res["errors"][0]["error"]
    assert q("SELECT disposition FROM offer_events ORDER BY id DESC LIMIT 1")[0]["disposition"] == "rejected"
    main.app.config["COMPINTEL_DEMO_MODE"] = False
    assert _deliver(client, conn, prov.sample_payload("greenhouse", record_id="gh-off")).status_code == 503


def test_incumbent_payroll_never_becomes_a_market_offer(client):
    demo(client)
    conn = _connect(client, "keka")
    old = _deliver(client, conn, prov.sample_payload("keka", record_id="emp-1", joined_days_ago=900)).get_json()
    assert old["incumbent_stored"] == 1 and not q("SELECT * FROM outcomes WHERE source_ref IS NOT NULL")
    new = _deliver(client, conn, prov.sample_payload("keka", record_id="emp-2", joined_days_ago=5)).get_json()
    assert new["recorded"] == 1


def test_integrations_admin_pages(client):
    demo(client)
    conn = _connect(client, "lever")
    page = client.get("/app/integrations").get_data(as_text=True)
    assert "have not been tested against a live account" in page and conn["secret"] not in page
    r = post(client, f"/app/integrations/connections/{conn['id']}/test", {"status": "declined", "amount_lpa": 38})
    assert r.status_code == 302 and q("SELECT * FROM outcomes WHERE status = 'declined' AND source_ref IS NOT NULL")
    preview = post(client, "/app/integrations/preview", {"provider": "darwinbox",
                                                         "payload": json.dumps(prov.sample_payload("darwinbox"))})
    assert "maps" in preview.get_data(as_text=True)
    bad_map = post(client, f"/app/integrations/connections/{conn['id']}/field-map", {"field_map": "[1]"})
    assert bad_map.status_code == 302
    good_map = post(client, f"/app/integrations/connections/{conn['id']}/field-map",
                    {"field_map": '{"fields": {"title": ["f.role_name"]}}'})
    assert good_map.status_code == 302
    assert json.loads(q("SELECT field_map FROM integration_connections WHERE id = ?", (conn["id"],))[0]["field_map"]) \
        == {"fields": {"title": ["f.role_name"]}}
    as_role(client, "recruiter")
    assert client.get("/app/integrations").status_code == 403


# ======================================================================= B. Streaming percentiles

def test_weighted_quantile_equals_percentile_cont_with_equal_weights():
    rng = random.Random(4)
    for _ in range(50):
        values = sorted(round(rng.uniform(5, 80), 2) for _ in range(rng.randint(1, 40)))
        for qq in (0.25, 0.5, 0.75):
            assert streaming.weighted_quantile([(v, 1.0) for v in values], qq) == pytest.approx(pilot.quantile(values, qq))
    # Weighting the top values pulls the median up, never outside the data.
    vals = [10, 20, 30, 40]
    weighted = streaming.weighted_quantile([(10, 1), (20, 1), (30, 3), (40, 3)], 0.5)
    assert streaming.weighted_quantile([(v, 1) for v in vals], 0.5) < weighted <= 40


def _obs(role="Software Engineer", level="Senior", tier="Tier 1 SaaS Hub", stage="Series A", status="accepted",
         final=40.0, initial=None, month=None, contributor="c1"):
    month = month or date.today().isoformat()[:7]
    run_sql("INSERT INTO benchmark_observations (contributor_key, role, level, city_tier, funding_stage, status, "
            "final_cash_ctc, initial_cash_ctc, observed_month, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'partner_import')",
            (contributor, role, level, tier, stage, status, final if status == "accepted" else None,
             initial if initial is not None else final, month))


def _months_ago(n):
    t = date.today()
    total = t.year * 12 + t.month - 1 - n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def test_snapshot_window_thresholds_and_recency(client):
    demo(client)
    run_sql("DELETE FROM benchmark_observations")
    with main.app.app_context():
        assert streaming.compute_snapshot(SE_KEY, DEFAULT_SETTINGS)["suppressed"]
    for i in range(12):
        _obs(final=40 + i, contributor=f"c{i % 2}")  # only 2 companies
    with main.app.app_context():
        snap = streaming.compute_snapshot(SE_KEY, DEFAULT_SETTINGS)
    assert snap["suppressed"] and "privacy threshold" in snap["reason"]
    run_sql("DELETE FROM benchmark_observations")
    for i in range(6):
        _obs(final=50 + i, contributor=f"c{i % 4}")                       # this month: weight 2
        _obs(final=30 + i, contributor=f"c{i % 4}", month=_months_ago(2))  # ~60 days: weight 1
    _obs(final=99, contributor="c9", month=_months_ago(7))                 # outside the window
    with main.app.app_context():
        snap = streaming.compute_snapshot(SE_KEY, DEFAULT_SETTINGS)
    assert not snap["suppressed"] and snap["n"] == 12 and snap["contributors"] == 4
    plain = pilot.quantile(sorted([50 + i for i in range(6)] + [30 + i for i in range(6)]), 0.5)
    assert snap["p50"] > plain  # recent offers weighted up
    assert snap["p75"] < 99 and snap["effective_n"] < 12


def test_outcome_pushes_a_live_snapshot_to_subscribers(client):
    demo(client)
    for i in range(12):
        _obs(final=40 + i, contributor=f"c{i % 4}")
    with main.app.app_context():
        before = streaming.compute_snapshot(SE_KEY, DEFAULT_SETTINGS)["n"]  # includes the demo seed's outcomes
    hub_q = streaming.pipeline.hub.subscribe(SE_KEY)
    try:
        as_role(client, "recruiter")
        r = post(client, "/app/outcomes/new", {"role": "Software Engineer", "level": "Senior", "city": "Bengaluru",
                                               "status": "accepted", "final_cash_ctc": "47", "decision_date":
                                               date.today().isoformat()})
        assert r.status_code == 302
        msg = hub_q.get(timeout=5)
    finally:
        streaming.pipeline.hub.unsubscribe(SE_KEY, hub_q)
    assert msg["cohort_key"] == SE_KEY and msg["n"] == before + 1 and not msg["suppressed"]
    assert q("SELECT trigger FROM cohort_snapshots WHERE cohort_key = ? ORDER BY id DESC", (SE_KEY,))[0]["trigger"] == "event"


def test_sse_stream_sends_the_current_snapshot(client):
    demo(client)
    r = client.get("/app/market/stream?role=Software+Engineer&level=Senior&tier=Tier+1+SaaS+Hub&stage=Series+A"
                   "&max_seconds=0")
    assert r.status_code == 200 and r.mimetype == "text/event-stream"
    text = r.get_data(as_text=True)
    assert text.startswith("retry: 3000") and "event: snapshot" in text
    payload = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
    assert payload["cohort_key"] == SE_KEY
    assert client.get("/app/market/stream?role=Nope").status_code == 400


def test_inprocess_bus_coalesces_bursts():
    calls = []
    hub = streaming.Hub()
    bus = streaming.InProcessBus(lambda key: (calls.append(key), time.sleep(0.05)), hub)
    for _ in range(200):
        bus.publish("a|b|c|d")
    bus.publish("e|f|g|h")
    assert bus.drain(5)
    assert set(calls) == {"a|b|c|d", "e|f|g|h"} and len(calls) <= 4


class FakeRedis:
    """Just enough of redis-py's Streams + pub/sub API to exercise RedisStreamsBus."""

    def __init__(self):
        self.stream, self.acked, self.published, self.groups = [], [], [], set()
        self._next = 0
        self._inbox = []

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if group in self.groups:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.add(group)

    def xadd(self, stream, fields, maxlen=None, approximate=True):
        self._next += 1
        self.stream.append((f"{self._next}-0".encode(), {k.encode(): v.encode() for k, v in fields.items()}))

    def xreadgroup(self, group, consumer, streams, count=None, block=None):
        entries, self.stream = self.stream, []
        return [(b"compintel:cohort-events", entries)] if entries else []

    def xack(self, stream, group, *ids):
        self.acked.extend(ids)

    def publish(self, channel, data):
        self.published.append(data)
        self._inbox.append({"type": "message", "data": data.encode()})

    def get_message(self, ignore_subscribe_messages=True, timeout=0):
        return self._inbox.pop(0) if self._inbox else None


def test_redis_streams_bus_with_a_fake_client():
    fake, hub, handled = FakeRedis(), streaming.Hub(), []
    bus = streaming.RedisStreamsBus(fake, handled.append, hub, consumer="t1")
    bus.ensure_group()
    bus.ensure_group()  # BUSYGROUP is swallowed
    for _ in range(3):
        fake.xadd(bus.STREAM, {"key": "k|1|t|s"})
    fake.xadd(bus.STREAM, {"key": "k|2|t|s"})
    assert bus.process_once(block_ms=0) == 2 and handled == ["k|1|t|s", "k|2|t|s"]  # coalesced
    assert len(fake.acked) == 4
    sub = hub.subscribe("k|1|t|s")
    bus.broadcast("k|1|t|s", {"n": 3})
    assert bus.relay_once(fake) and sub.get_nowait() == {"n": 3}
    failing = streaming.RedisStreamsBus(fake, lambda key: 1 / 0, hub, consumer="t2")
    fake.xadd(bus.STREAM, {"key": "k|3|t|s"})
    acked_before = len(fake.acked)
    assert failing.process_once(block_ms=0) == 0 and len(fake.acked) == acked_before  # left pending for redelivery


# ======================================================================= D. Clearing price

def test_isotonic_fit_is_monotone_and_prices_interpolate():
    pts = [(30, 0, 1), (35, 1, 1), (36, 0, 1), (40, 0, 1), (42, 1, 1), (45, 1, 1), (50, 1, 1)]
    curve = clearing.isotonic_fit(pts)
    ps = [p for _x, p in curve]
    assert ps == sorted(ps) and ps[0] == 0 and ps[-1] == 1
    price = clearing.price_for(curve, 0.5)
    assert price["bound"] == "estimate" and 36 <= price["lpa"] <= 45
    assert clearing.price_for(curve, 1.01)["bound"] == "above"


def test_evidence_uses_declines_negotiation_and_counters():
    rows = [{"status": "accepted", "initial_cash_ctc": 40, "final_cash_ctc": 44, "counter_cash_ctc": 46},
            {"status": "declined", "initial_cash_ctc": 38, "final_cash_ctc": None, "counter_cash_ctc": 45},
            {"status": "accepted", "initial_cash_ctc": None, "final_cash_ctc": 41, "counter_cash_ctc": None},
            {"status": "withdrawn", "initial_cash_ctc": 50, "final_cash_ctc": None, "counter_cash_ctc": None}]
    pts = clearing.evidence(rows, [1, 1, 2, 1])
    assert (40.0, 0, 1) in pts and (44.0, 1, 1) in pts            # opening offer didn't clear, final did
    assert (38.0, 0, 1) in pts and (45.0, 1, 0.5) in pts          # decline + counter at half weight
    assert (41.0, 1, 2) in pts and not any(x == 50.0 for x, _y, _w in pts)


def test_clearing_needs_data_and_both_outcomes():
    settings = dict(DEFAULT_SETTINGS)
    few = [{"status": "accepted", "final_cash_ctc": 40, "contributor_key": "a"}] * 3
    assert clearing.compute(few, [1] * 3, 40, settings)["status"] == "insufficient"
    all_yes = [{"status": "accepted", "final_cash_ctc": 40 + i, "contributor_key": f"c{i % 3}"} for i in range(12)]
    assert clearing.compute(all_yes, [1] * 12, 45, settings)["status"] == "all_accepted"


def _cohort_rows(rng, n, reservation, offer_level, contributors=5):
    out = []
    for _ in range(n):
        offer = round(offer_level * rng.uniform(0.9, 1.1), 2)
        ok = offer >= reservation
        out.append({"status": "accepted" if ok else "declined", "initial_cash_ctc": offer,
                    "final_cash_ctc": offer if ok else None, "contributor_key": f"c{rng.randrange(contributors)}"})
    return out


def test_clearing_price_alerts_when_the_benchmark_p50_stops_clearing():
    rng = random.Random(1)
    settings = dict(DEFAULT_SETTINGS)
    calm = _cohort_rows(rng, 40, reservation=38, offer_level=42)    # asks below the P50: offers clear
    moved = _cohort_rows(rng, 40, reservation=46, offer_level=42)   # asks now above the P50
    ok = clearing.compute(calm, [1] * 40, 42, settings)
    assert ok["status"] == "ok" and not ok["alert"] and ok["clearing_price_lpa"] < 42
    alert = clearing.compute(moved, [1] * 40, 42, settings, previous=(calm, [1] * 40), anchor_label="24-month P50")
    assert alert["alert"] and alert["clearing_price_lpa"] > 42 and alert["decline_at_p50_pct"] >= 50
    assert alert["trend_pp"] > 0 and "24-month P50" in alert["message"]
    assert [p["ratio"] for p in alert["curve_by_p50"]] == list(clearing.CURVE_RATIOS)
    assert "curve" not in alert  # individual offer amounts are never exposed


def test_demo_sample_triggers_one_alert_and_feeds_offer_checks(client):
    demo(client)
    as_role(client, "admin")
    assert post(client, "/app/market/demo-sample").status_code == 302
    batch = q("SELECT * FROM network_import_batches WHERE consent_basis LIKE 'Synthetic demo sample%'")
    assert batch and batch[0]["rows"] == len(demo_network_rows("Series A"))
    latest = {}
    for r in q("SELECT cohort_key, alert FROM cohort_snapshots ORDER BY id"):
        latest[r["cohort_key"]] = r["alert"]
    assert latest[SE_KEY] == 1
    assert sum(latest.values()) == 1
    notes = q("SELECT * FROM notifications WHERE message LIKE 'Market alert%'")
    assert {n["role"] for n in notes} == {"recruiter", "finance", "exec"}
    page = client.get("/app/market?role=Software+Engineer&level=Senior&tier=Tier+1+SaaS+Hub&stage=Series+A")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "Market alert." in html and "Recompute history" in html
    assert post(client, "/app/market/demo-sample").status_code == 302  # second load refused, not duplicated
    assert len(q("SELECT * FROM network_import_batches WHERE consent_basis LIKE 'Synthetic demo sample%'")) == 1

    # The offer builder's checks now carry the clearing price (requisition 1 = SE / Senior / Bengaluru).
    as_role(client, "recruiter")
    rid = new_offer(client)
    low = post(client, f"/app/offers/{rid}/evaluate", package_form(benchmark(rid), cash=38)).get_data(as_text=True)
    assert "Below the market clearing price" in low
    high = post(client, f"/app/offers/{rid}/evaluate", package_form(benchmark(rid), cash=60)).get_data(as_text=True)
    assert "Market is moving above P50" in high
