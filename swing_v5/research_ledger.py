"""Local research ledger. Normal reads do not rewrite the file."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


class LedgerConflict(Exception):
    pass


class LedgerCorrupt(Exception):
    def __init__(self, lines: list[int]):
        super().__init__(f"corrupt lines: {lines}")
        self.lines = lines


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def logical_id(parts: dict) -> str:
    return hashlib.sha256(_canonical(parts).encode()).hexdigest()[:24]


def load_events(path: Path) -> list[dict]:
    """Read-only. Corrupt lines raise instead of being deleted."""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if raw and not raw.endswith("\n"):
        raise LedgerCorrupt([-1])
    rows = []
    bad = []
    for number, line in enumerate(raw.splitlines(), start=1):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad.append(number)
    if bad:
        raise LedgerCorrupt(bad)
    return rows


def recover_ledger(path: Path, quarantine: Path) -> dict:
    """Quarantine an incomplete final append. Interior corrupt lines are copied out, not dropped silently."""
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    incomplete = ""
    body = raw
    if raw and not raw.endswith("\n"):
        head, _, tail = raw.rpartition("\n")
        body = head + ("\n" if head else "")
        incomplete = tail
    kept = []
    interior = []
    for number, line in enumerate(body.splitlines(), start=1):
        try:
            json.loads(line)
            kept.append(line)
        except json.JSONDecodeError:
            interior.append({"line": number, "text": line})
    log = {"incomplete_tail": incomplete, "interior": interior}
    quarantine.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
    path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    return {"kept": len(kept), "interior_quarantined": len(interior), "incomplete_tail": bool(incomplete)}


def append_event(path: Path, event: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    existing = {row["event_id"]: row for row in load_events(path)}
    current = existing.get(event["event_id"])
    if current is not None:
        if _canonical(current) != _canonical(event):
            raise LedgerConflict(event["event_id"])
        return {**event, "duplicate_skipped": True}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, default=str) + "\n")
        handle.flush()
    return event


class ResearchLedger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events (logical_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, logical_id TEXT, at TEXT, note TEXT)"
        )
        self.conn.commit()

    def append(self, logical: str, payload: dict) -> dict:
        body = _canonical(payload)
        digest = hashlib.sha256(body.encode()).hexdigest()
        try:
            with self.conn:
                row = self.conn.execute("SELECT payload_hash FROM events WHERE logical_id=?", (logical,)).fetchone()
                if row and row[0] != digest:
                    raise LedgerConflict(logical)
                if row:
                    return {"duplicate_skipped": True, "logical_id": logical}
                self.conn.execute("INSERT INTO events VALUES (?,?,?)", (logical, digest, body))
        except sqlite3.IntegrityError as exc:
            raise LedgerConflict(logical) from exc
        return {"duplicate_skipped": False, "logical_id": logical}

    def attempt(self, logical: str, at: str, note: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO attempts (logical_id, at, note) VALUES (?,?,?)", (logical, at, note))

    def events(self) -> list[dict]:
        rows = self.conn.execute("SELECT payload FROM events ORDER BY logical_id").fetchall()
        return [json.loads(row[0]) for row in rows]
