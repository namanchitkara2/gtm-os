"""Where the bridge keeps its state, and what it needs to start.

The state directory holds the ledger, the budget counters, and the pairing
token — never a LinkedIn cookie. That lives in the upstream server's own profile
at ``~/.linkedin-mcp``, owned by the user, and it is the entire point of the
hybrid design that it never travels.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

STATE_DIR = Path(os.environ.get("GTMOS_BRIDGE_HOME", Path.home() / ".gtm-bridge"))


@dataclass(frozen=True)
class BridgeConfig:
    control_plane_url: str
    token: str
    bridge_id: UUID
    daily_limit: int = 100

    @property
    def ws_url(self) -> str:
        base = self.control_plane_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/bridge/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + "/bridge/ws"
        return base + "/bridge/ws"


def _credentials_path() -> Path:
    return STATE_DIR / "credentials.json"


def save_credentials(url: str, token: str, bridge_id: UUID, daily_limit: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _credentials_path()
    # Written 0600 before content lands in it: a bridge token is a bearer
    # credential for someone's tenant, and a world-readable home directory on a
    # shared machine is not hypothetical.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        json.dump(
            {
                "control_plane_url": url,
                "token": token,
                "bridge_id": str(bridge_id),
                "daily_limit": daily_limit,
            },
            handle,
        )


def load_credentials() -> BridgeConfig | None:
    path = _credentials_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return BridgeConfig(
        control_plane_url=data["control_plane_url"],
        token=data["token"],
        bridge_id=UUID(data["bridge_id"]),
        daily_limit=int(data.get("daily_limit", 100)),
    )


def new_bridge_id() -> UUID:
    return uuid4()
