"""What the tunnel needs from a thing that can actually drive LinkedIn.

Kept as a protocol so the tunnel's dispatch semantics — ack, reject, ledger,
budget — can be tested exhaustively against a fake, without a browser, a
LinkedIn account, or a network. Those semantics are the part that can silently
send a message twice; they deserve tests that run in milliseconds.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ExecutorError(Exception):
    """Raised by an executor with a classified kind the protocol understands."""

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


@runtime_checkable
class Executor(Protocol):
    def known_tools(self) -> set[str]: ...

    async def health(self) -> tuple[bool, bool]:
        """(authenticated, browser_ready)."""
        ...

    async def call(self, tool: str, args: dict[str, Any], *, timeout_s: float) -> dict[str, Any]: ...
