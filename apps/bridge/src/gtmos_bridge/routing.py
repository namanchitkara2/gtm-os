"""One Executor in front of several, routed by tool name.

The tunnel stays unaware that there is more than one backend. Two things it
does need per tool rather than globally, and which this answers:

* **health** — a LinkedIn checkpoint must not refuse a web read, and a crashed
  web agent must not refuse a LinkedIn read. ``health_for(tool)`` asks only the
  backend that would run it; plain ``health()`` reports the primary, which is
  what the heartbeat's ``authenticated`` has always meant.
* **names** — a tool offered by two backends is refused at construction. A
  silent first-wins would route a call to whichever happened to be listed first.
"""

from __future__ import annotations

from typing import Any

from gtmos_bridge.executor import Executor, ExecutorError


class RoutingExecutor:
    def __init__(self, primary: Executor, *others: Executor) -> None:
        self._primary = primary
        self._route: dict[str, Executor] = {}
        for backend in (primary, *others):
            for tool in backend.known_tools():
                if tool in self._route:
                    raise ValueError(f"tool {tool!r} is offered by two backends")
                self._route[tool] = backend

    def known_tools(self) -> set[str]:
        return set(self._route)

    async def health(self) -> tuple[bool, bool]:
        return await self._primary.health()

    async def health_for(self, tool: str) -> tuple[bool, bool]:
        backend = self._route.get(tool, self._primary)
        return await backend.health()

    async def call(self, tool: str, args: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        backend = self._route.get(tool)
        if backend is None:
            raise ExecutorError(f"no backend offers {tool}", kind="not_found")
        return await backend.call(tool, args, timeout_s=timeout_s)
