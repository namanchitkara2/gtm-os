"""Per-day cap on LinkedIn actions, enforced on the bridge as well as the cloud.

Enforced in both places on purpose. The cloud cap is the product's promise; this
one is the backstop that still holds when a run is replayed, a second control
plane is pointed at the same bridge, or a bug double-counts. The account being
protected belongs to the user running this process, so the last line of defence
belongs here too.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path


class DailyBudget:
    def __init__(self, path: Path, *, limit: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, actions INT NOT NULL DEFAULT 0)"
        )
        self._db.commit()
        self._lock = threading.Lock()
        self.limit = limit

    def used_today(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT actions FROM usage WHERE day=?", (date.today().isoformat(),)
            ).fetchone()
        return row[0] if row else 0

    def remaining(self) -> int:
        return max(0, self.limit - self.used_today())

    def try_spend(self, n: int = 1) -> bool:
        """Reserve budget *before* acting. Refuses rather than going negative."""
        with self._lock:
            today = date.today().isoformat()
            row = self._db.execute("SELECT actions FROM usage WHERE day=?", (today,)).fetchone()
            used = row[0] if row else 0
            if used + n > self.limit:
                return False
            self._db.execute(
                "INSERT INTO usage (day, actions) VALUES (?,?) "
                "ON CONFLICT(day) DO UPDATE SET actions = actions + ?",
                (today, n, n),
            )
            self._db.commit()
            return True

    def refund(self, n: int = 1) -> None:
        """Give budget back when a reserved action provably never happened."""
        with self._lock:
            self._db.execute(
                "UPDATE usage SET actions = MAX(0, actions - ?) WHERE day=?",
                (n, date.today().isoformat()),
            )
            self._db.commit()
