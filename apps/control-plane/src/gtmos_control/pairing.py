"""Device-code pairing: how a laptop proves it belongs to a tenant.

The bridge cannot hold a browser-based OAuth session, and we refuse to ask users
to paste a long-lived secret into a terminal. So the bridge asks for a short
code, the user approves it in a browser where they are already signed in, and
the bridge exchanges the code for a token bound to (tenant, user).

Two properties matter:

* codes expire quickly — an unclaimed code is a standing invitation to attach a
  bridge to someone else's tenant;
* only the token's hash is stored, so a database dump does not yield a working
  bridge.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

CODE_TTL = timedelta(minutes=10)
POLL_INTERVAL_S = 3

#: No vowels, no 0/O/1/I/L: the code is read aloud and typed by hand.
_ALPHABET = "BCDFGHJKMNPQRSTVWXYZ23456789"


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def expiry() -> datetime:
    return datetime.now(timezone.utc) + CODE_TTL


def is_expired(expires_at: datetime) -> bool:
    return datetime.now(timezone.utc) >= expires_at


def token_matches(token: str, stored_hash: str) -> bool:
    """Constant-time compare, so a stored hash cannot be probed byte by byte."""
    return secrets.compare_digest(hash_token(token), stored_hash)


def bridge_id_from(value: str) -> UUID:
    return UUID(value)


async def approve_code(conn, *, code: str, tenant_id: UUID, user_id: UUID) -> tuple[dict, int]:
    """Bind a pending code to a tenant, minting the bridge and its token.

    Lives here rather than in a route handler because two callers approve codes:
    the legacy ``/bridge/pair/approve``, which takes the tenant and user from its
    request body, and ``/api/bridge/pair/approve``, which resolves them from the
    signed-in principal. Approval decides which tenant a stranger's laptop may
    attach to, so having one implementation of it — rather than two that drift —
    is the whole point of pulling it out.

    Returns ``(body, status_code)`` for the caller to serialise; ``conn`` must be
    a system connection, since an unclaimed pairing row has no tenant for
    row-level security to match.
    """
    row = await conn.fetchrow(
        "SELECT expires_at, approved_at FROM bridge_pairings WHERE code=$1", code
    )
    if row is None:
        return {"error": "unknown code"}, 404
    if is_expired(row["expires_at"]):
        return {"error": "code expired"}, 410
    if row["approved_at"] is not None:
        return {"error": "code already used"}, 409

    # Re-pairing revokes whatever came before. The registry only holds one bridge
    # per tenant anyway, so a previous row is not a second usable bridge — it is
    # a bearer token that still works, on a machine the user may have moved on
    # from, sold, or lost. Revoking here is what makes "pair again" a real remedy
    # for a compromised laptop.
    await conn.execute(
        "UPDATE bridges SET revoked_at = now() "
        "WHERE tenant_id = $1 AND user_id = $2 AND revoked_at IS NULL",
        tenant_id, user_id,
    )

    token = new_token()
    bridge_id = uuid4()
    await conn.execute(
        "INSERT INTO bridges (id, tenant_id, user_id, token_hash, status) "
        "VALUES ($1,$2,$3,$4,'offline')",
        bridge_id, tenant_id, user_id, hash_token(token),
    )
    await conn.execute(
        "UPDATE bridge_pairings SET tenant_id=$1, user_id=$2, bridge_id=$3, "
        "approved_at=now() WHERE code=$4",
        tenant_id, user_id, bridge_id, code,
    )
    # Stored, not held in memory: approve and poll can land on different tasks
    # behind the load balancer.
    await conn.execute("UPDATE bridge_pairings SET token_once=$1 WHERE code=$2", token, code)
    return {"ok": True, "bridge_id": str(bridge_id)}, 200
