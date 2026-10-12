"""Pair a local bridge without the web UI (local development only).

Stands in for the browser approval step: starts a pairing, approves it as the
seeded dev workspace, collects the token, and writes the bridge credentials.
"""
import asyncio, os, sys
import httpx
from gtmos_bridge.config import save_credentials, new_bridge_id

BASE = os.environ.get("GTMOS_URL", "http://127.0.0.1:8080")
TENANT = os.environ.get("GTMOS_DEV_TENANT", "11111111-1111-1111-1111-111111111111")
USER = os.environ.get("GTMOS_DEV_USER", "aaaaaaaa-0000-0000-0000-000000000001")

async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        r = await c.post("/bridge/pair/start"); r.raise_for_status()
        code = r.json()["code"]
        print(f"  code {code}")
        r = await c.post("/bridge/pair/approve", json={"code": code, "tenant_id": TENANT, "user_id": USER})
        r.raise_for_status()
        print(f"  approved as dev workspace -> bridge {r.json()['bridge_id']}")
        r = await c.post("/bridge/pair/poll", json={"code": code}); r.raise_for_status()
        d = r.json()
        save_credentials(BASE, d["token"], new_bridge_id(), d["daily_limit"])
        print(f"  credentials written to ~/.gtm-bridge (daily limit {d['daily_limit']})")
asyncio.run(main())
