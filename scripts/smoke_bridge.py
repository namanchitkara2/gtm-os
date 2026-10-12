"""Prove the bridge can drive the REAL upstream LinkedIn MCP server."""
import asyncio, logging, sys
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
from gtmos_bridge.linkedin import LinkedInExecutor
from gtmos_bridge.executor import ExecutorError

async def main() -> int:
    ex = LinkedInExecutor()
    print("→ spawning `uvx mcp-server-linkedin@latest` as a child process...")
    try:
        await ex.start()
    except Exception as e:
        print(f"FAILED to start: {type(e).__name__}: {e}")
        return 1

    tools = ex.known_tools()
    print(f"\n✓ MCP handshake OK — {len(tools)} tools exposed by upstream")
    print("  sample:", ", ".join(sorted(tools)[:6]), "...")

    # Behave like the real bridge: health() re-probes every 30s, so wait the
    # way the tunnel does instead of judging the browser on one cold probe.
    ex.REPROBE_AFTER_S = 5.0
    for _ in range(24):
        auth, ready = await ex.health()
        if auth and ready:
            break
        await asyncio.sleep(6)
    print(f"\n  authenticated = {auth}")
    print(f"  browser_ready = {ready}")
    print(f"  linkedin_username = {ex.linkedin_username!r}")

    # The write actions the ledger protects must actually exist upstream.
    from gtmos_schemas.bridge import WRITE_ACTIONS
    missing = WRITE_ACTIONS - tools
    print(f"\n  WRITE_ACTIONS present upstream: {not missing}"
          + (f" (MISSING: {missing})" if missing else ""))

    if auth:
        print("\n→ real read through the executor: get_person_profile('williamhgates')")
        try:
            r = await ex.call("get_person_profile", {"linkedin_username": "williamhgates"}, timeout_s=120)
            secs = r.get("sections", {})
            print(f"✓ returned keys={list(r)[:5]} sections={list(secs)[:5]}")
            body = next(iter(secs.values()), "")
            print(f"  first section is raw text, {len(body)} chars — confirms structuring is OUR job")
        except ExecutorError as e:
            print(f"  read failed [{e.kind}]: {str(e)[:200]}")
    await ex.aclose()
    print("\n✓ clean shutdown")
    return 0

sys.exit(asyncio.run(main()))
