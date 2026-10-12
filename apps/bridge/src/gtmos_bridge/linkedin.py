"""Drives LinkedIn by speaking MCP to the upstream server, rather than forking it.

The plan called for forking ``stickerdaniel/linkedin-mcp-server`` and adding a
BRIDGE role beside its DIRECT/OWNER/PROXY ones. Reading the code made a smaller
design obvious: none of what the bridge adds — the outbound tunnel, the ledger,
the budget — needs to live *inside* that server. Everything the bridge needs
*from* it is already exposed as MCP tools.

So the bridge runs the published package as a child process and talks to it as
an ordinary MCP client. What that buys:

* no fork to maintain, and upstream fixes arrive by version bump;
* no derivative work, so the Apache-2.0 §4(d) obligation is plain attribution
  of a dependency rather than modification notices in a vendored tree;
* their daemon election, profile lease, and sequential middleware keep working
  exactly as designed, against the one browser profile they were built for;
* all 19 tools are available on day one instead of the handful worth porting.

The cost is one extra process hop per call, which is noise beside a page scrape.
"""

from __future__ import annotations

import asyncio
import logging

from gtmos_bridge.executor import ExecutorError
from gtmos_bridge.mcp_child import McpChildExecutor

logger = logging.getLogger(__name__)

#: Upstream error text → the classification the tunnel needs. Classification is
#: not cosmetic: ``kind`` decides whether an interrupted *write* is recorded as
#: provably-not-performed (safe to retry) or left unknown. Anything not matched
#: here stays unknown on purpose — guessing in this direction sends twice.
_ERROR_KINDS: tuple[tuple[str, str], ...] = (
    ("captcha", "auth"),
    ("checkpoint", "auth"),
    ("login", "auth"),
    ("authentication", "auth"),
    ("not authenticated", "auth"),
    ("session", "auth"),
    ("rate limit", "rate_limited"),
    ("too many requests", "rate_limited"),
    ("429", "rate_limited"),
    ("not found", "not_found"),
    ("404", "not_found"),
    ("no such profile", "not_found"),
    ("timeout", "timeout"),
    ("timed out", "timeout"),
    ("browser", "browser"),
    ("chromium", "browser"),
)


def classify(message: str) -> str:
    lowered = message.lower()
    for needle, kind in _ERROR_KINDS:
        if needle in lowered:
            return kind
    return "unknown"


class LinkedInExecutor(McpChildExecutor):
    """An MCP client session against a locally spawned linkedin-mcp-server."""

    label = "linkedin server"

    def __init__(
        self,
        *,
        command: str = "uvx",
        args: tuple[str, ...] = ("mcp-server-linkedin@latest",),
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(command=command, args=args, env=env, classify=classify)
        self._authenticated = False
        #: Monotonic time of the last identity probe, so a bridge that came up
        #: before its browser did can recover on its own instead of needing a
        #: restart. Upstream reports browser-not-ready as a tool error, not on a
        #: status channel, so re-probing is the only way to learn it is ready.
        self._last_probe: float = 0.0
        self.linkedin_username: str | None = None

    async def start(self) -> None:
        await super().start()
        await self._probe_identity()

    async def _probe_identity(self) -> None:
        """Establish who we are, once, at startup.

        Spends one LinkedIn action deliberately. It is the only way to know
        whether the stored session is still valid before a run depends on it,
        and it yields the account name the control plane pins the run to — so a
        user who swaps LinkedIn accounts mid-campaign is caught rather than
        silently mixing two outreach histories.
        """
        self._last_probe = asyncio.get_running_loop().time()
        try:
            result = await self.call("get_my_profile", {}, timeout_s=90.0)
        except ExecutorError as exc:
            # Only an auth failure says the session is bad. A browser failure
            # says nothing about the session, and must not be reported as an
            # authenticated bridge that is ready to work.
            if exc.kind == "auth":
                self._authenticated = False
            logger.warning("identity probe failed (%s): %s", exc.kind, str(exc)[:160])
            return
        self._authenticated = True
        self._browser_ready = True
        url = result.get("url") if isinstance(result, dict) else None
        if isinstance(url, str) and "/in/" in url:
            self.linkedin_username = url.rstrip("/").rsplit("/in/", 1)[-1]
        logger.info("authenticated as %s", self.linkedin_username or "<unknown>")

    REPROBE_AFTER_S = 30.0

    async def health(self) -> tuple[bool, bool]:
        """Report liveness, re-probing when we last saw the browser unavailable.

        Reporting ``browser_ready=True`` because a session exists was a real bug:
        on a cold machine the upstream server answers every tool call with
        "downloading Chromium in the background", so the session is up and the
        browser is not. The tunnel would ack dispatches it could not run.
        """
        if self._session is None:
            return self._authenticated, False
        if not self._browser_ready and (
            asyncio.get_running_loop().time() - self._last_probe > self.REPROBE_AFTER_S
        ):
            await self._probe_identity()
        return self._authenticated, self._browser_ready

    def _on_error(self, kind: str) -> None:
        if kind == "auth":
            self._authenticated = False
        super()._on_error(kind)
