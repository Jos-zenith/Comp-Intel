"""
Tamper evidence for the audit trail.

Every audit entry stores the hash of the entry before it, so editing or
deleting any row (even directly in the database, bypassing the triggers)
breaks the chain from that point on. Submissions carry a SHA-256 of their
frozen snapshot, and every approval action carries a hash of its own fields
plus the snapshot hash it acted on; both hashes are also written into the
chained audit entry for that event. verify_all() recomputes all three.
"""
from __future__ import annotations

import hashlib
import json

GENESIS = "0" * 64
AUDIT_FIELDS = ("ts", "company_id", "user_id", "user_role", "action", "entity", "entity_id", "detail", "context")
ACTION_FIELDS = ("submission_id", "step", "approver_role", "user_id", "action", "comment", "ts")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def audit_entry_hash(prev_hash: str, row) -> str:
    return sha256(prev_hash + canonical({f: row[f] for f in AUDIT_FIELDS}))


def action_hash(row, snapshot_hash: str) -> str:
    return sha256(canonical({**{f: row[f] for f in ACTION_FIELDS}, "snapshot_hash": snapshot_hash}))


def verify_all(conn) -> dict:
    """Recompute the audit chain, every snapshot hash and every action hash."""
    problems = []
    prev = GENESIS
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    for row in rows:
        if row["prev_hash"] != prev:
            problems.append(f"Audit entry #{row['id']}: link to the previous entry is broken.")
        expected = audit_entry_hash(row["prev_hash"] or "", row)
        if row["entry_hash"] != expected:
            problems.append(f"Audit entry #{row['id']}: contents don't match its hash.")
        prev = row["entry_hash"]

    subs = conn.execute("SELECT id, snapshot, snapshot_hash FROM submissions").fetchall()
    snap_hashes = {}
    for s in subs:
        snap_hashes[s["id"]] = s["snapshot_hash"]
        if sha256(s["snapshot"]) != s["snapshot_hash"]:
            problems.append(f"Submission #{s['id']}: snapshot doesn't match its hash.")

    actions = conn.execute("SELECT * FROM approval_actions ORDER BY id").fetchall()
    for a in actions:
        if a["content_hash"] != action_hash(a, snap_hashes.get(a["submission_id"], "")):
            problems.append(f"Approval action #{a['id']}: contents don't match its hash.")

    return {
        "ok": not problems,
        "problems": problems,
        "audit_entries": len(rows),
        "submissions": len(subs),
        "actions": len(actions),
        "head": prev,
    }
