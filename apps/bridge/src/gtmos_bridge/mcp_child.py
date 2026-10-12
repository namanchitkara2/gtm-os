"""An executor that is an MCP client session against a locally spawned server.

Both backends the bridge drives — the upstream LinkedIn server and the
Stagehand web agent — are MCP servers run as child processes. What they share
is the part that is easy to get subtly wrong: who owns the anyio scope, how a
startup failure is surfaced, and how a tool result is turned into a payload or a
classified error. That lives here once; each backend adds only what is specific
to it (the LinkedIn identity probe, for one).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gtmos_bridge.executor import ExecutorError

logger = logging.getLogger(__name__)


class McpChildExecutor:
    """Spawns an MCP server over stdio and calls its tools, one at a time."""

    #: Human name for logs and startup errors.
    label = "mcp server"

    def __init__(
        self,
        *,
        command: str,
        args: tuple[str, ...],
        env: Mapping[str, str] | None = None,
        classify: Callable[[str], str] = lambda _message: "unknown",
    ) -> None:
        self._params = StdioServerParameters(
            command=command, args=list(args), env=dict(env) if env is not None else None
        )
        self._classify = classify
        # The session's context managers are entered and exited by ONE task that
        # owns them for their whole life. anyio task groups — which stdio_client
        # is built on — refuse to be exited from a different task than entered
        # them, so an AsyncExitStack closed by whoever happens to call aclose()
        # raises "Attempted to exit cancel scope in a different task". Holding
        # the scope in a dedicated task and signalling it to unwind is the shape
        # that actually survives shutdown.
        self._serve_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._stop: asyncio.Event = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._session: ClientSession | None = None
        self._tools: set[str] = set()
        self._browser_ready = False
        # One call at a time. Both backends drive a single browser profile; holding
        # the lock here means we queue in our own process rather than blocking
        # inside someone else's lock with no visibility.
        self._gate = asyncio.Lock()

    async def start(self) -> None:
        self._serve_task = asyncio.create_task(self._serve())
        ready = asyncio.ensure_future(self._ready.wait())
        done, _ = await asyncio.wait(
            {ready, self._serve_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if ready not in done or self._session is None:
            # The server task ended before signalling readiness: surface its
            # cause rather than a bare timeout, because "uvx not installed" and
            # "LinkedIn checkpoint" need very different fixes from the user.
            ready.cancel()
            await self._serve_task
            raise ExecutorError(
                f"{self.label} exited during startup: {self._startup_error}", kind="browser"
            )

    async def _serve(self) -> None:
        """Own the MCP session for its entire lifetime, in this one task."""
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._tools = {t.name for t in listed.tools}
                    self._session = session
                    self._browser_ready = True
                    logger.info("%s up with %d tools", self.label, len(self._tools))
                    self._ready.set()
                    await self._stop.wait()
        except Exception as exc:  # noqa: BLE001 - recorded, then reported by start()
            self._startup_error = exc
            logger.error("%s session ended: %s", self.label, exc)
        finally:
            self._session = None
            self._browser_ready = False
            self._ready.set()

    def known_tools(self) -> set[str]:
        return set(self._tools)

    async def health(self) -> tuple[bool, bool]:
        return True, self._session is not None and self._browser_ready

    def _on_error(self, kind: str) -> None:
        """Hook for a backend to update its health from a classified failure."""
        if kind == "browser":
            self._browser_ready = False

    async def call(self, tool: str, args: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        if self._session is None:
            raise ExecutorError(f"{self.label} not started", kind="browser")
        async with self._gate:
            try:
                async with asyncio.timeout(timeout_s):
                    result = await self._session.call_tool(tool, args)
            except TimeoutError as exc:
                raise ExecutorError(f"{tool} exceeded {timeout_s}s", kind="timeout") from exc

        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )
        if result.is_error:
            kind = self._classify(text)
            self._on_error(kind)
            raise ExecutorError(text or f"{tool} failed", kind=kind)

        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    async def aclose(self) -> None:
        """Ask the owning task to unwind, and wait for it.

        Signalling rather than closing directly is the whole point: the scope
        must be exited by the task that entered it.
        """
        self._stop.set()
        if self._serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._serve_task, timeout=15.0)
            self._serve_task = None
        self._session = None
