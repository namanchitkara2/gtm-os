"""FULL END-TO-END: pair a bridge, attach it, dispatch a real LinkedIn read."""
import asyncio, logging, os, sys
from uuid import UUID, uuid4
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
for noisy in ("mcp","asyncio","httpx","httpcore","websockets","uvicorn.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

import httpx, uvicorn
from gtmos_control.app import create_app, registry, db
from gtmos_control.bridges import BridgeUnavailable
from gtmos_bridge.linkedin import LinkedInExecutor
from gtmos_bridge.ledger import ActionLedger
from gtmos_bridge.budget import DailyBudget
from gtmos_bridge.tunnel import Tunnel

TENANT = UUID("11111111-1111-1111-1111-111111111111")
USER   = UUID("aaaaaaaa-0000-0000-0000-000000000001")
BASE   = "http://127.0.0.1:8077"
import tempfile, pathlib
STATE = pathlib.Path(tempfile.mkdtemp())

def ok(m): print(f"  \033[92m✓\033[0m {m}", flush=True)
def step(m): print(f"\n\033[1m→ {m}\033[0m", flush=True)

async def main():
    step("1. start control plane")
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=8077, log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        await asyncio.sleep(0.1)
        if server.started: break
    ok(f"control plane listening on {BASE}")

    step("2. device-code pairing (what the user actually does)")
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        r = await c.post("/bridge/pair/start"); r.raise_for_status()
        code, verify = r.json()["code"], r.json()["verification_url"]
        ok(f"bridge requested code: {code}")

        poll = await c.post("/bridge/pair/poll", json={"code": code})
        assert poll.status_code == 202, poll.text
        ok("before approval, poll correctly returns 202 pending")

        r = await c.post("/bridge/pair/approve", json={"code": code, "tenant_id": str(TENANT), "user_id": str(USER)})
        r.raise_for_status(); ok(f"user approved in browser -> bridge {r.json()['bridge_id'][:8]}…")

        r = await c.post("/bridge/pair/poll", json={"code": code}); r.raise_for_status()
        token, limit = r.json()["token"], r.json()["daily_limit"]
        ok(f"bridge collected token (daily limit {limit})")

        again = await c.post("/bridge/pair/poll", json={"code": code})
        assert again.status_code == 409, again.text
        ok("token is single-use: second collection refused (409)")

    step("3. start the local bridge (LinkedIn stays on this machine)")
    ex = LinkedInExecutor(); await ex.start()
    ex.REPROBE_AFTER_S = 5.0
    for _ in range(24):
        auth, ready = await ex.health()
        if auth and ready: break
        await asyncio.sleep(6)
    ok(f"LinkedIn session live as {ex.linkedin_username!r}, {len(ex.known_tools())} tools")

    tunnel = Tunnel(f"ws://127.0.0.1:8077/bridge/ws", token, bridge_id=uuid4(), version="e2e",
                    executor=ex, ledger=ActionLedger(STATE/"l.db"),
                    budget=DailyBudget(STATE/"b.db", limit=limit),
                    linkedin_username=ex.linkedin_username)
    ttask = asyncio.create_task(tunnel.run_forever(max_attempts=1))

    for _ in range(60):
        await asyncio.sleep(0.25)
        conn = registry.get(TENANT)
        if conn and conn.hello: break
    assert conn and conn.hello, "bridge never attached"
    ok(f"bridge attached over WSS, announced {len(conn.hello.capabilities)} capabilities")

    async with db.system() as c:
        row = await c.fetchrow("SELECT status, revoked_at FROM bridges WHERE tenant_id=$1 "
                               "ORDER BY created_at DESC LIMIT 1", TENANT)
        revoked = await c.fetchval("SELECT count(*) FROM bridges WHERE tenant_id=$1 AND revoked_at IS NOT NULL", TENANT)
    ok(f"DB records live bridge: status={row['status']}, prior bridges revoked: {revoked}")

    step("4. cloud dispatches a REAL LinkedIn read down the tunnel")
    for _ in range(40):
        if conn.is_ready: break
        await asyncio.sleep(0.5)
    out = await conn.dispatch(tool="get_person_profile",
                              args={"linkedin_username": "williamhgates"},
                              run_id=uuid4(), idempotency_key=registry.fresh_key(), deadline_s=150)
    assert out.ok, f"dispatch failed: {out.error}"
    secs = (out.payload or {}).get("sections", {})
    ok(f"read returned via tunnel: keys={list(out.payload)[:4]} sections={list(secs)}")
    ok(f"raw text length {len(next(iter(secs.values()),''))} chars")

    step("5. refuse an unknown tool (provably not started)")
    try:
        await conn.dispatch(tool="rm_rf", args={}, run_id=uuid4(), idempotency_key=registry.fresh_key())
        print("  ✗ should have raised"); return 1
    except BridgeUnavailable as e:
        ok(f"rejected: reason={e.reason}")

    step("6. WRITE dedup short-circuit (the ack-without-result path is covered\n     properly in apps/control-plane/tests/test_ambiguity.py)")
    key = registry.new_idempotency_key(tenant_id=TENANT, person_ref="urn:test:9", action="send_message")
    ok(f"stable idempotency key: {key}")
    ledger = tunnel._ledger
    assert ledger.begin(key, tool="send_message", person_ref="urn:test:9") is True
    ledger.complete(key, ok=True)
    o = await conn.dispatch(tool="send_message", args={"linkedin_username":"x","message":"hi"},
                            run_id=uuid4(), idempotency_key=key, deadline_s=30)
    assert o.payload and o.payload.get("deduped") is True, o.payload
    ok("redispatch of an already-performed write was DEDUPED, never re-sent")

    step("7. shutdown")
    ttask.cancel(); await ex.aclose(); server.should_exit = True; await task
    ok("clean")
    print("\n\033[92m\033[1mEND-TO-END PASSED\033[0m")
    return 0

sys.exit(asyncio.run(main()))
