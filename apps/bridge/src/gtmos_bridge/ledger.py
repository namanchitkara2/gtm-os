"""The local record of every write this bridge has performed.

Why a durable file and not memory: the whole point is to survive the crash. A
bridge that loses its ledger on restart will happily send a connection request
it already sent, and LinkedIn's own UI is not a reliable check — a withdrawn or
ignored invite may not be visible.

Why three outcomes and not two: the interesting state is the one where the
bridge wrote "starting" and then died. That is genuinely *unknown*, and both
confident answers are wrong. Recording the start before the action, rather than
only the completion after it, is what turns a silent double-send into a
knowable ambiguity — and the cloud's policy for writes is to treat unknown as
performed, because a missed outreach is recoverable and a duplicate is not.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from gtmos_schemas.bridge import LedgerOutcome

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    idempotency_key TEXT PRIMARY KEY,
    tool            TEXT NOT NULL,
    person_ref      TEXT,
    status          TEXT NOT NULL CHECK (status IN ('started','performed','failed')),
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS actions_person_idx ON actions (person_ref, tool);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActionLedger:
    """Append-mostly log of write attempts, keyed by idempotency key."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus an explicit lock: the tunnel's reader and
        # its dispatch workers are different tasks, and sqlite's own threading
        # guard would reject the handoff without telling us why.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # The ledger's only job is to survive a crash. FULL sync is the whole
        # reason it exists; NORMAL would let the OS lose the last write, which
        # is exactly the record that matters.
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()

    def begin(self, key: str, *, tool: str, person_ref: str | None) -> bool:
        """Claim a key before performing the action.

        Returns True if this caller now owns the action, False if the key was
        already claimed — which means a previous attempt got at least as far as
        starting, and this dispatch must not execute.
        """
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO actions (idempotency_key, tool, person_ref, status, started_at)"
                    " VALUES (?,?,?,'started',?)",
                    (key, tool, person_ref, _utcnow()),
                )
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def complete(self, key: str, *, ok: bool, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE actions SET status=?, ended_at=?, error=? WHERE idempotency_key=?",
                ("performed" if ok else "failed", _utcnow(), error, key),
            )
            self._db.commit()

    def outcome(self, key: str) -> LedgerOutcome:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM actions WHERE idempotency_key=?", (key,)
            ).fetchone()
        if row is None:
            return "not_performed"
        if row[0] == "performed":
            return "performed"
        if row[0] == "failed":
            # A recorded failure is a real answer: the action provably did not
            # take effect, so the cloud may safely try again.
            return "not_performed"
        return "unknown"  # 'started' — we died mid-flight and cannot claim either way

    def outcomes(self, keys: list[str]) -> dict[str, LedgerOutcome]:
        return {k: self.outcome(k) for k in keys}

    def close(self) -> None:
        with self._lock:
            self._db.close()
