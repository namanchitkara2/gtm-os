"""The dashboard's read API.

Everything the web UI shows already exists — as the return value of an MCP tool.
That is the wrong shape for a browser: an MCP tool returns prose-adjacent JSON
shaped for a model to read aloud, and it is reachable only over a session
protocol the browser does not speak. So the same tables are exposed here as
plain resources, and the two surfaces stay independent on purpose. An MCP tool's
output is a *product decision* about what a model should see; changing it to suit
a table layout is how tool output slowly turns into an API nobody designed.

Three rules hold for every handler below:

1. **``db.scoped``, never ``db.system``.** Row-level security fails closed, so a
   scoping mistake here does not raise — it renders an empty dashboard, which is
   also exactly what a brand-new tenant correctly looks like. There is no way to
   tell those two apart by looking at the screen, so the rule is enforced by
   reading, not by testing.
2. **Identity comes from the same resolver the MCP tools use.** A second identity
   path is a second place for the dev-mode gate to be wrong.
3. **Read-only, with one exception.** ``/api/runs/{id}/cancel`` stops a run and
   ``/api/bridge/pair/approve`` completes pairing; both are things the UI exists
   to do. Everything else is a projection of the database.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gtmos_control import pairing

Identity = Callable[[], Awaitable[tuple[UUID, UUID]]]


# ------------------------------------------------------------- serialisation --

def jsonable(value: Any) -> Any:
    """Postgres types that ``JSONResponse`` cannot encode.

    ``NUMERIC`` arrives as :class:`~decimal.Decimal` (scores, costs) and
    timestamps as :class:`~datetime.datetime`. Both raise at response time, not
    query time, and cost columns are zero all through local development — so the
    failure surfaces first on the day a run finally costs money.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def as_json(value: Any) -> Any:
    """JSONB, which asyncpg hands back as ``str`` or as a decoded object
    depending on codec registration. The existing tool code guards the same way
    in three places; a UI that gets a string where it expects an object renders
    a JSON blob into a table cell and looks broken rather than failing."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def ms_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int((end - start).total_seconds() * 1000)


# ------------------------------------------------------------------ routing --

def build_routes(*, db, registry, executor, identity: Identity) -> list[Route]:
    """The API routes, closed over the same collaborators the MCP tools use."""

    def guard(handler):
        """Resolve the caller, or refuse.

        ``resolve_identity`` raises outside dev mode by design: no identity
        provider is wired yet, and the env-var gate exists so a misconfigured
        deployment serves nobody rather than serving every anonymous caller the
        dev workspace. That must stay true through the HTTP layer — a 401 here,
        never a 200 carrying the dev tenant's rows.
        """
        async def wrapped(request: Request) -> JSONResponse:
            try:
                tenant_id, user_id = await identity()
            except PermissionError as exc:
                return JSONResponse({"error": "unauthenticated", "detail": str(exc)}, status_code=401)
            return await handler(request, tenant_id, user_id)
        wrapped.__name__ = handler.__name__
        return wrapped

    # ---------------------------------------------------------------- me --

    @guard
    async def me(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        """Who the browser is, and the one number that governs everything.

        The daily bridge budget is on this payload rather than a settings page
        because it is the ceiling on the entire product's throughput; the UI
        shows it next to today's usage on every screen.
        """
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            tenant = await conn.fetchrow(
                "SELECT name, slug, plan, daily_bridge_budget FROM tenants WHERE id=$1",
                tenant_id,
            )
            user = await conn.fetchrow(
                "SELECT email, display_name FROM users WHERE id=$1", user_id
            )
            today = await conn.fetchrow(
                "SELECT bridge_calls, write_actions, cost_usd FROM usage_daily "
                "WHERE tenant_id=$1 AND day = current_date", tenant_id,
            )
        return JSONResponse(jsonable({
            "tenant_id": tenant_id,
            "user_id": user_id,
            "tenant": dict(tenant) if tenant else None,
            "user": dict(user) if user else None,
            "dev_mode": os.environ.get("GTMOS_DEV_MODE") == "1",
            "today": dict(today) if today else {"bridge_calls": 0, "write_actions": 0, "cost_usd": 0},
        }))

    # ----------------------------------------------------------- bridges --

    @guard
    async def bridges(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        """Paired bridges, with the live socket's view layered on the row.

        The ``bridges`` table records what was true when the socket last wrote to
        it; the registry knows whether that socket is attached *right now*. A
        control plane that restarted with a bridge attached has stale rows, so
        where the two disagree the registry wins.
        """
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            rows = await conn.fetch(
                "SELECT id, linkedin_username, bridge_version, platform, status, "
                " last_seen_at, last_error, created_at FROM bridges "
                "WHERE revoked_at IS NULL ORDER BY created_at DESC",
            )
        live = registry.get(tenant_id)
        out = []
        for row in rows:
            item = jsonable(dict(row))
            attached = live is not None and str(live.bridge_id) == str(row["id"])
            item["attached"] = attached
            if attached:
                status = live.status
                item["status"] = "busy" if (status and status.busy) else "online"
                item["ready"] = live.is_ready
                item["actions_today"] = status.actions_today if status else 0
                item["linkedin_username"] = (
                    live.hello.linkedin_username if live.hello else item["linkedin_username"]
                )
            else:
                item["status"] = "offline"
                item["ready"] = False
                item["actions_today"] = None
            out.append(item)
        return JSONResponse({"bridges": out, "any_online": any(b["attached"] for b in out)})

    # ------------------------------------------------------------- runs --

    @guard
    async def runs(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        limit = min(int(request.query_params.get("limit", 50)), 200)
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            rows = await conn.fetch(
                "SELECT r.id, r.workflow, r.status, r.progress, r.blocked_reason, r.error, "
                "  r.total_bridge_calls, r.total_cost_usd, r.limits, "
                "  r.created_at, r.started_at, r.ended_at, "
                "  t.name AS target_name, t.brief AS target_brief, "
                "  (SELECT count(*) FROM run_people rp WHERE rp.run_id = r.id) AS people, "
                "  (SELECT count(*) FROM run_people rp WHERE rp.run_id = r.id "
                "     AND rp.from_cache) AS from_cache "
                "FROM runs r LEFT JOIN targets t ON t.id = r.target_id "
                "ORDER BY r.created_at DESC LIMIT $1", limit,
            )
        items = []
        for row in rows:
            item = jsonable(dict(row))
            item["progress"] = as_json(row["progress"])
            item["limits"] = as_json(row["limits"])
            item["duration_ms"] = ms_between(row["started_at"], row["ended_at"])
            items.append(item)
        return JSONResponse({"runs": items})

    @guard
    async def run_detail(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        """A run and its trace.

        ``run_steps`` is the product's trace surface, not an ops log — which is
        why it lives in this database rather than behind a vendor dashboard, and
        why the step's LinkedIn-action count is carried out to the UI as a
        first-class field. A step that spent none is the good outcome.
        """
        run_id = UUID(request.path_params["run_id"])
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            run = await conn.fetchrow(
                "SELECT r.*, t.name AS target_name, t.brief AS target_brief, t.spec AS target_spec, "
                "  (SELECT count(*) FROM run_people rp WHERE rp.run_id = r.id) AS people, "
                "  (SELECT count(*) FROM run_people rp WHERE rp.run_id = r.id "
                "     AND rp.from_cache) AS from_cache "
                "FROM runs r LEFT JOIN targets t ON t.id = r.target_id WHERE r.id=$1", run_id,
            )
            if run is None:
                return JSONResponse({"error": "run not found"}, status_code=404)
            steps = await conn.fetch(
                "SELECT sequence, node, status, input_summary, output_summary, "
                "  bridge_calls, cost_usd, input_tokens, output_tokens, retries, error, "
                "  started_at, ended_at FROM run_steps WHERE run_id=$1 ORDER BY sequence",
                run_id,
            )
        payload = jsonable(dict(run))
        payload["progress"] = as_json(run["progress"])
        payload["limits"] = as_json(run["limits"])
        payload["target_spec"] = as_json(run["target_spec"]) if run["target_spec"] else None
        payload["duration_ms"] = ms_between(run["started_at"], run["ended_at"])
        payload["steps"] = [
            {
                **jsonable(dict(s)),
                "input_summary": as_json(s["input_summary"]),
                "output_summary": as_json(s["output_summary"]),
                "duration_ms": ms_between(s["started_at"], s["ended_at"]),
            }
            for s in steps
        ]
        return JSONResponse(payload)

    @guard
    async def run_people(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        run_id = UUID(request.path_params["run_id"])
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            rows = await conn.fetch(
                "SELECT p.id, p.full_name, p.headline, p.location, p.linkedin_username, "
                "  p.email_status, p.phone_status, p.doc, p.refreshed_at, "
                "  rp.score, rp.score_rationale, rp.from_cache "
                "FROM run_people rp JOIN people p ON p.id = rp.person_id "
                "WHERE rp.run_id=$1 ORDER BY rp.score DESC NULLS LAST", run_id,
            )
        return JSONResponse({"people": [_person(r) for r in rows]})

    @guard
    async def cancel_run(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        run_id = UUID(request.path_params["run_id"])
        stopped = await executor.cancel(run_id, tenant_id)
        return JSONResponse({"cancelled": stopped})

    # ----------------------------------------------------------- people --

    @guard
    async def people(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        """The cache, browsable.

        Worth its own screen because of what it costs to fill: at one LinkedIn
        action at a time, every row here is a browser action already spent. Rows
        are the asset, not a byproduct of the run that created them.
        """
        limit = min(int(request.query_params.get("limit", 100)), 500)
        q = (request.query_params.get("q") or "").strip()
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            if q:
                rows = await conn.fetch(
                    "SELECT id, full_name, headline, location, linkedin_username, "
                    "  email_status, phone_status, doc, refreshed_at, NULL::numeric AS score, "
                    "  NULL::text AS score_rationale, FALSE AS from_cache FROM people "
                    "WHERE full_name ILIKE $1 OR headline ILIKE $1 OR location ILIKE $1 "
                    "ORDER BY refreshed_at DESC LIMIT $2", f"%{q}%", limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, full_name, headline, location, linkedin_username, "
                    "  email_status, phone_status, doc, refreshed_at, NULL::numeric AS score, "
                    "  NULL::text AS score_rationale, FALSE AS from_cache FROM people "
                    "ORDER BY refreshed_at DESC LIMIT $1", limit,
                )
        return JSONResponse({"people": [_person(r) for r in rows]})

    # ---------------------------------------------------------- targets --

    @guard
    async def targets(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            rows = await conn.fetch(
                "SELECT t.id, t.kind, t.name, t.brief, t.spec, t.interpretation_notes, "
                "  t.created_at, (SELECT count(*) FROM runs r WHERE r.target_id = t.id) AS runs "
                "FROM targets t ORDER BY t.created_at DESC LIMIT 100",
            )
        return JSONResponse([
            {
                **jsonable(dict(r)),
                "spec": as_json(r["spec"]),
                "interpretation_notes": as_json(r["interpretation_notes"]) or [],
            }
            for r in rows
        ])

    @guard
    async def workflows(request: Request, tenant_id: UUID, user_id: UUID) -> JSONResponse:
        from gtmos_control.workflows import LIBRARY
        return JSONResponse([w.model_dump() for w in LIBRARY])

    # ---------------------------------------------------------- pairing --

    async def approve(request: Request) -> JSONResponse:
        """Approve a device code as the signed-in user.

        The existing ``/bridge/pair/approve`` takes ``tenant_id`` and ``user_id``
        from the request body, which would mean the browser posting the UUIDs it
        just read from ``/api/me`` back to the server and the server believing
        them. This variant takes only the code and resolves the principal the
        same way every other handler does. (Phase 1b gap: the older route still
        trusts its body, and stays until Cognito lands and it can be deleted.)
        """
        try:
            tenant_id, user_id = await identity()
        except PermissionError as exc:
            return JSONResponse({"error": "unauthenticated", "detail": str(exc)}, status_code=401)
        body = await request.json()
        code = (body.get("code") or "").strip().upper()
        if not code:
            return JSONResponse({"error": "no code supplied"}, status_code=400)
        # System connection: an unclaimed pairing row has no tenant_id, so
        # row-level security matches nothing and the scoped role sees no code.
        async with db.system() as conn:
            payload, status = await pairing.approve_code(
                conn, code=code, tenant_id=tenant_id, user_id=user_id
            )
        return JSONResponse(payload, status_code=status)

    return [
        Route("/api/me", me),
        Route("/api/bridges", bridges),
        Route("/api/runs", runs),
        Route("/api/runs/{run_id}", run_detail),
        Route("/api/runs/{run_id}/people", run_people),
        Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route("/api/people", people),
        Route("/api/targets", targets),
        Route("/api/workflows", workflows),
        Route("/api/bridge/pair/approve", approve, methods=["POST"]),
    ]


def _person(row) -> dict[str, Any]:
    """One person, with the evidence trail flattened enough to render.

    ``doc`` holds the full Person model including per-field provenance. The list
    view needs contact status and where a value came from, not the whole tree, so
    emails and phones are projected with their source providers attached — the
    claim and its evidence travel together or the UI is asserting things it
    cannot support.
    """
    doc = as_json(row["doc"])
    def contacts(key: str) -> list[dict[str, Any]]:
        return [
            {
                "value": item.get("value"),
                "sources": [ev.get("provider") for ev in (item.get("evidence") or [])],
            }
            for item in (doc.get(key) or [])
            if item.get("value")
        ]
    identity_doc = doc.get("identity") or {}
    return {
        "id": str(row["id"]),
        "name": row["full_name"],
        "headline": row["headline"],
        "location": row["location"],
        "linkedin_username": row["linkedin_username"],
        "linkedin_url": (
            f"https://www.linkedin.com/in/{row['linkedin_username']}"
            if row["linkedin_username"] else None
        ),
        "score": float(row["score"]) if row["score"] is not None else None,
        "score_rationale": row["score_rationale"],
        "from_cache": bool(row["from_cache"]),
        "email_status": row["email_status"],
        "phone_status": row["phone_status"],
        "emails": contacts("emails"),
        "phones": contacts("phones"),
        "current_title": identity_doc.get("headline") or row["headline"],
        "refreshed_at": jsonable(row["refreshed_at"]),
    }
