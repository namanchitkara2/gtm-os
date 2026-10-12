"""The ledger's only job is to survive a crash. So the tests crash it."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from gtmos_bridge.ledger import ActionLedger


def test_begin_claims_a_key_exactly_once(tmp_path: Path) -> None:
    led = ActionLedger(tmp_path / "l.db")
    assert led.begin("k1", tool="send_message", person_ref="urn:1") is True
    assert led.begin("k1", tool="send_message", person_ref="urn:1") is False, (
        "a second claim on the same key must be refused — this is the double-send guard"
    )


def test_completed_action_reports_performed(tmp_path: Path) -> None:
    led = ActionLedger(tmp_path / "l.db")
    led.begin("k1", tool="send_message", person_ref="urn:1")
    led.complete("k1", ok=True)
    assert led.outcome("k1") == "performed"


def test_failed_action_is_safe_to_retry(tmp_path: Path) -> None:
    """A recorded failure provably did not take effect."""
    led = ActionLedger(tmp_path / "l.db")
    led.begin("k1", tool="connect_with_person", person_ref="urn:1")
    led.complete("k1", ok=False, error="rate limited")
    assert led.outcome("k1") == "not_performed"


def test_unseen_key_is_not_performed(tmp_path: Path) -> None:
    led = ActionLedger(tmp_path / "l.db")
    assert led.outcome("never-seen") == "not_performed"


def test_crash_between_begin_and_complete_reports_unknown(tmp_path: Path) -> None:
    """The case the whole design exists for.

    A child process claims a key and is killed before completing it — exactly
    what a laptop lid closing or an OOM does mid-send. Reopening the ledger must
    report ``unknown``, not a confident answer in either direction.
    """
    db = tmp_path / "l.db"
    script = textwrap.dedent(f"""
        import os
        from pathlib import Path
        from gtmos_bridge.ledger import ActionLedger
        led = ActionLedger(Path({str(db)!r}))
        led.begin("crash-key", tool="send_message", person_ref="urn:9")
        os._exit(9)  # hard kill: no cleanup, no atexit, no flush
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode == 9, proc.stderr.decode()

    reopened = ActionLedger(db)
    assert reopened.outcome("crash-key") == "unknown", (
        "a write interrupted mid-flight must be reported as unknown, so the cloud "
        "can apply its treat-as-performed policy rather than blind-retrying a send"
    )
    assert reopened.begin("crash-key", tool="send_message", person_ref="urn:9") is False, (
        "an interrupted write must still block a repeat attempt"
    )


def test_outcomes_batch(tmp_path: Path) -> None:
    led = ActionLedger(tmp_path / "l.db")
    led.begin("a", tool="send_message", person_ref=None)
    led.complete("a", ok=True)
    led.begin("b", tool="send_message", person_ref=None)
    assert led.outcomes(["a", "b", "c"]) == {
        "a": "performed",
        "b": "unknown",
        "c": "not_performed",
    }
