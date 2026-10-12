"""Drives non-LinkedIn sites through the Stagehand web agent (~/stagehand-agent).

Same shape as the LinkedIn backend — a child MCP server spoken to as a client —
with three constraints that come from the bridge, not from Stagehand:

* **Read-only.** The server is started with ``STAGEHAND_READ_ONLY=1``, so
  ``web_act`` is never registered. An act can submit a form; the bridge has no
  ledger entry for that. Adding it means adding it to ``WRITE_ACTIONS`` first.
* **Its own browser profile.** Never ``~/.linkedin-mcp``: a second Chromium on
  that user-data-dir breaks the upstream profile lease.
* **LinkedIn refused** by the server itself, so it cannot become a side door
  around the ledger and the daily budget.
"""

from __future__ import annotations

import os
from pathlib import Path

from gtmos_schemas.bridge import WEB_TOOLS

from gtmos_bridge.executor import ExecutorError
from gtmos_bridge.mcp_child import McpChildExecutor

DEFAULT_DIR = Path(os.environ.get("GTMOS_WEB_AGENT_DIR", Path.home() / "stagehand-agent"))

_ERROR_KINDS: tuple[tuple[str, str], ...] = (
    ("refused:", "not_found"),
    ("no model key", "auth"),
    ("invalid x-api-key", "auth"),
    ("timeout", "timeout"),
    ("timed out", "timeout"),
    ("chrome exited", "browser"),
    ("no open page", "browser"),
)


def classify(message: str) -> str:
    lowered = message.lower()
    for needle, kind in _ERROR_KINDS:
        if needle in lowered:
            return kind
    return "unknown"


class WebExecutor(McpChildExecutor):
    label = "web agent"

    def __init__(self, agent_dir: Path = DEFAULT_DIR) -> None:
        server = agent_dir / "src" / "server.ts"
        if not server.exists():
            raise ExecutorError(f"web agent not found at {server}", kind="browser")
        super().__init__(
            command="npx",
            args=("--prefix", str(agent_dir), "tsx", str(server)),
            env={**os.environ, "STAGEHAND_READ_ONLY": "1", "STAGEHAND_HEADLESS": "1"},
            classify=classify,
        )

    async def start(self) -> None:
        await super().start()
        # Belt and braces for the read-only promise: refuse to run if the
        # server exposes anything beyond the tools the tunnel treats as reads.
        extra = self.known_tools() - WEB_TOOLS
        if extra:
            await self.aclose()
            raise ExecutorError(
                f"web agent exposes tools the bridge has no ledger policy for: {sorted(extra)}",
                kind="browser",
            )
