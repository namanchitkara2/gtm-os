"""The control plane's HTTP surface: MCP for agents, WSS for bridges.

One process serves both because they share the bridge registry: an MCP tool call
that needs LinkedIn has to reach the socket a bridge is holding open, and putting
those in separate services would mean brokering every dispatch through Redis for
no benefit at this size.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from gtmos_control.bridges import BridgeConnection, BridgeRegistry
from gtmos_control.db import Database
from gtmos_control import pairing
from gtmos_control.runs import RunExecutor
from gtmos_control.api import build_routes
from gtmos_control.memory_tools import register_memory_tools
from gtmos_control.tools import register_tools

logger = logging.getLogger(__name__)

registry = BridgeRegistry()
mcp = FastMCP("GTM-OS")
db = Database(
    os.environ.get("DATABASE_URL", "postgresql://gtmos_app:app@localhost:55432/gtmos"),
    os.environ.get("DATABASE_SYSTEM_URL", "postgresql://gtmos_system:system@localhost:55432/gtmos"),
)
executor = RunExecutor(db, registry)


async def resolve_identity() -> tuple[UUID, UUID]:
    """Who is calling. Local development short-circuits this.

    Deployed, this reads the verified OAuth token that FastMCP's OAuthProxy put
    on the request and maps its Cognito ``sub`` to a user and their tenant. That
    proxy exists because Cognito does not implement RFC 7591 dynamic client
    registration, without which Claude Code never gets past metadata discovery.

    Locally there is no identity provider, so a single seeded workspace is used.
    It is gated on an explicit env var rather than on "no token present", so
    that a misconfigured deployment fails closed instead of quietly serving
    every anonymous caller the dev tenant's data.
    """
    if os.environ.get("GTMOS_DEV_MODE") == "1":
        return (
            UUID(os.environ.get("GTMOS_DEV_TENANT", "11111111-1111-1111-1111-111111111111")),
            UUID(os.environ.get("GTMOS_DEV_USER", "aaaaaaaa-0000-0000-0000-000000000001")),
        )
    raise PermissionError(
        "No authenticated principal. Set GTMOS_DEV_MODE=1 for local use, or deploy "
        "with Cognito behind FastMCP's OAuthProxy."
    )


register_tools(mcp, db=db, registry=registry, executor=executor, identity=resolve_identity)
# The memory layer is a separate registration rather than more tools in the same
# module: these read and write what the workspace knows, and none of them touch
# the bridge, the registry or the executor. Different dependencies, different file.
register_memory_tools(mcp, db=db, identity=resolve_identity)


async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


# ------------------------------------------------------------------ pairing --

async def pair_start(request: Request) -> JSONResponse:
    """Bridge asks for a code. Anonymous by design — the code is worthless
    until a signed-in user approves it."""
    code = pairing.new_code()
    async with db.system() as conn:
        # Which tenant this belongs to is decided at *approval*, by whoever is
        # signed in, not by whoever asked. That is what stops a stranger's
        # bridge from attaching itself to a tenant it names.
        await conn.execute(
            "INSERT INTO bridge_pairings (code, tenant_id, user_id, expires_at) "
            "VALUES ($1, NULL, NULL, $2)",
            code, pairing.expiry(),
        )
    base = os.environ.get("GTMOS_PUBLIC_URL", str(request.base_url).rstrip("/"))
    return JSONResponse({
        "code": code,
        "verification_url": f"{base}/bridges/approve?code={code}",
        "expires_in": int(pairing.CODE_TTL.total_seconds()),
        "interval": pairing.POLL_INTERVAL_S,
    })


async def pair_approve(request: Request) -> JSONResponse:
    """Approve a code on behalf of a user named in the request body.

    Kept for the pairing flow that predates the dashboard. The web UI uses
    ``/api/bridge/pair/approve`` instead, which takes only the code and resolves
    the principal server-side; this one believes the tenant and user it is told
    about.

    **Dev-mode only.** Deployed, that trust is an account-takeover primitive:
    anyone who can reach this route can attach their own bridge to any workspace
    whose UUID they can guess or read out of a log, and a bridge is a live
    LinkedIn session. It is gated rather than deleted because
    ``scripts/dev_pair.py`` and ``scripts/e2e.py`` both use it for local setup —
    so it survives exactly where it is harmless and 404s everywhere else.
    """
    if os.environ.get("GTMOS_DEV_MODE") != "1":
        logger.warning(
            "legacy /bridge/pair/approve refused outside dev mode (from %s)",
            request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            {
                "error": "route_disabled",
                "why": (
                    "This endpoint trusts tenant_id and user_id from its request "
                    "body and is available only in local development."
                ),
                "use_instead": (
                    "/api/bridge/pair/approve, which resolves the principal from "
                    "the authenticated session."
                ),
            },
            status_code=404,
        )

    body = await request.json()
    async with db.system() as conn:
        payload, status = await pairing.approve_code(
            conn,
            code=body["code"],
            tenant_id=UUID(body["tenant_id"]),
            user_id=UUID(body["user_id"]),
        )
    return JSONResponse(payload, status_code=status)


async def pair_poll(request: Request) -> JSONResponse:
    body = await request.json()
    code = body["code"]
    async with db.system() as conn:
        row = await conn.fetchrow(
            "SELECT b.id, p.approved_at, p.expires_at, t.daily_bridge_budget "
            "FROM bridge_pairings p "
            "LEFT JOIN bridges b ON b.id = p.bridge_id "
            "LEFT JOIN tenants t ON t.id = p.tenant_id "
            "WHERE p.code=$1", code,
        )
    if row is None:
        return JSONResponse({"error": "unknown code"}, status_code=404)
    if row["approved_at"] is None:
        if pairing.is_expired(row["expires_at"]):
            return JSONResponse({"error": "expired"}, status_code=410)
        return JSONResponse({"status": "pending"}, status_code=202)

    # Single-use, atomically, across every task: the row is locked, its current
    # token read, and the column cleared in one statement, so a concurrent poll
    # on another task finds nothing.
    #
    # The self-join is not decoration. `UPDATE ... SET token_once = NULL
    # RETURNING token_once` returns the *new* value in Postgres 16 — that is,
    # NULL — so the obvious version clears the token and then reports it was
    # already collected, which is exactly how this was first written and how it
    # failed. (`RETURNING OLD.*` would do it, but only from Postgres 18.)
    async with db.system() as conn:
        collected = await conn.fetchval(
            "UPDATE bridge_pairings p SET token_once = NULL "
            "FROM (SELECT code, token_once AS tok FROM bridge_pairings "
            "      WHERE code = $1 FOR UPDATE) prior "
            "WHERE p.code = prior.code AND prior.tok IS NOT NULL "
            "RETURNING prior.tok",
            code,
        )
    if collected is None:
        return JSONResponse({"error": "token already collected"}, status_code=409)
    token = collected
    return JSONResponse({
        "token": token,
        "bridge_id": str(row["id"]),
        "daily_limit": row["daily_bridge_budget"] or 100,
    })


# ------------------------------------------------------------------ tunnel --

async def bridge_ws(websocket: WebSocket) -> None:
    """The bridge's outbound socket. Authenticated by bearer token."""
    header = websocket.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not token:
        await websocket.close(code=4401, reason="missing bearer token")
        return

    async with db.system() as conn:
        row = await conn.fetchrow(
            "SELECT id, tenant_id FROM bridges WHERE token_hash=$1 AND revoked_at IS NULL",
            pairing.hash_token(token),
        )
    if row is None:
        await websocket.close(code=4403, reason="unknown or revoked bridge token")
        return

    await websocket.accept()
    conn_obj = BridgeConnection(websocket, tenant_id=row["tenant_id"], bridge_id=row["id"])
    await registry.attach(conn_obj)
    async with db.system() as c:
        await c.execute(
            "UPDATE bridges SET status='online', last_seen_at=now() WHERE id=$1", row["id"]
        )
    try:
        while True:
            raw = await websocket.receive_text()
            await conn_obj.handle_frame(raw)
            if conn_obj.status is not None:
                async with db.system() as c:
                    await c.execute(
                        "UPDATE bridges SET last_seen_at=now(), status=$2, "
                        "linkedin_username=COALESCE($3, linkedin_username) WHERE id=$1",
                        row["id"],
                        "busy" if conn_obj.status.busy else "online",
                        conn_obj.hello.linkedin_username if conn_obj.hello else None,
                    )
    except WebSocketDisconnect:
        pass
    finally:
        await registry.detach(conn_obj)
        async with db.system() as c:
            await c.execute("UPDATE bridges SET status='offline' WHERE id=$1", row["id"])



# --------------------------------------------------------------------- web --

WEB_DIST = Path(
    os.environ.get(
        "GTMOS_WEB_DIST",
        Path(__file__).resolve().parents[3] / "web" / "dist",
    )
)


async def web_index(request: Request):
    """The dashboard's HTML shell, for any path the router owns.

    The control plane serves the built SPA rather than a separate origin because
    of one line in ``pair_start``: the verification URL it hands the bridge is
    built from ``GTMOS_PUBLIC_URL``, this service's own origin. Splitting the web
    app onto its own host would make that link 404 on the exact screen a new
    user reaches first. In production the same ``dist`` sits behind CloudFront;
    this path is what keeps local development a single command.
    """
    index = WEB_DIST / "index.html"
    if not index.exists():
        return PlainTextResponse(
            "Dashboard is not built.\n\n"
            "  cd apps/web && npm install && npm run build\n\n"
            "Or run the Vite dev server on :5173 with `npm run dev`.",
            status_code=503,
        )
    return FileResponse(index)


#: Every path the SPA's router owns. Kept as one list so a screen added in
#: React and not added here fails loudly on reload rather than subtly.
WEB_PATHS = [
    "/", "/bridges", "/bridges/approve", "/runs", "/runs/{run_id}",
    "/people", "/targets",
]


def web_routes() -> list:
    """Serve the shell for each SPA path — enumerated, deliberately not a catch-all.

    A blanket ``/{path:path}`` before the MCP mount would be simpler and would
    also be a trap: FastMCP's ``OAuthProxy`` publishes its discovery documents at
    ``/.well-known/...`` on that mounted app, and a catch-all placed ahead of it
    would swallow them. Claude Code never gets past metadata discovery when that
    happens, which is the exact failure the proxy exists to avoid — and it would
    not show up until Cognito is wired in Phase 1b, long after anyone would think
    to look here.

    So unknown paths 404 from the MCP app, which is the honest answer for a path
    the dashboard has no screen for.
    """
    routes = []
    if (WEB_DIST / "assets").is_dir():
        routes.append(Mount("/assets", StaticFiles(directory=WEB_DIST / "assets")))
    routes.extend(Route(path, web_index) for path in WEB_PATHS)
    return routes


def create_app() -> Starlette:
    # FastMCP's ASGI app carries its own lifespan (session manager, transport).
    # It has to be chained rather than replaced, or the MCP endpoint accepts
    # connections and then fails on the first request with no session manager.
    mcp_app = mcp.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(app):
        await db.connect()
        async with mcp_app.lifespan(app):
            logger.info("control plane up")
            try:
                yield
            finally:
                await executor.shutdown()
                await db.close()

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", healthz),
            Route("/bridge/pair/start", pair_start, methods=["POST"]),
            Route("/bridge/pair/approve", pair_approve, methods=["POST"]),
            Route("/bridge/pair/poll", pair_poll, methods=["POST"]),
            WebSocketRoute("/bridge/ws", bridge_ws),
            *build_routes(db=db, registry=registry, executor=executor, identity=resolve_identity),
            *web_routes(),
            Mount("/", mcp_app),
        ],
    )


app = create_app()
