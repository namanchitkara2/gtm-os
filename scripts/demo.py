"""Drive GTM-OS the way a user's Claude Code does: over MCP, nothing else."""
import asyncio, json, sys, time
from fastmcp import Client

URL = "http://127.0.0.1:8080/mcp"
BRIEF = "VP of Sales at B2B SaaS companies in Bangalore"

def hdr(t): print(f"\n\033[1m{'─'*70}\n{t}\n{'─'*70}\033[0m", flush=True)

async def main():
    async with Client(URL) as c:
        hdr("connect_linkedin")
        s = (await c.call_tool("connect_linkedin", {})).data
        print(f"  bridge connected : {s['connected']}")
        print(f"  linkedin account : {s['linkedin_account']}")
        print(f"  ready / busy     : {s['ready']} / {s['busy']}")
        print(f"  actions today    : {s['actions_today']}")
        if not s["connected"]:
            print("  no bridge — stopping"); return 1

        hdr("list_workflows")
        for w in (await c.call_tool("list_workflows", {})).data:
            print(f"  {w['slug']}  [{w['pack']}]  bridge_required={w['requires_bridge']}")
            print(f"     {w['description'][:100]}")

        hdr(f"define_target — {BRIEF!r}")
        t = (await c.call_tool("define_target", {
            "brief": BRIEF,
            "name": "Bangalore SaaS sales leaders",
            "titles": ["vp of sales", "vice president sales"],
            "locations": ["Bangalore"],
            "keywords": ["saas", "b2b", "sales"],
        })).data
        # define_target now refuses a brief it cannot read, returning an error
        # shape with no target_id rather than persisting an empty spec.
        if "target_id" not in t:
            print(f"  REFUSED        : {t.get('error')}")
            print(f"  why            : {t.get('why')}")
            return
        print(f"  target_id      : {t['target_id']}")
        print(f"  interpreted as : {json.dumps(t['interpreted_as'])}")
        print(f"  confidence     : {t.get('confidence')}")

        hdr("start_run")
        r = (await c.call_tool("start_run", {
            "workflow": "sales_sourcing", "target_id": t["target_id"], "max_people": 6,
        })).data
        run_id = r["run_id"]
        print(f"  run_id   : {run_id}")
        print(f"  status   : {r['status']}  (returned immediately)")
        print(f"  estimate : ~{r['estimated_bridge_calls']} sequential LinkedIn actions")

        hdr("get_run — live trace")
        start = time.time()
        while time.time() - start < 900:
            st = (await c.call_tool("get_run", {"run_id": run_id})).data
            line = "  ".join(
                f"{x['node']}:{x['status'][:4]}({x['linkedin_actions']})" for x in st["trace"]
            )
            print(f"  [{int(time.time()-start):>3}s] {st['status']:<16} {line}", flush=True)
            if st["status"] in ("succeeded", "failed", "cancelled", "awaiting_bridge"):
                break
            await asyncio.sleep(5)

        hdr("final trace")
        for x in st["trace"]:
            ms = f"{x['ms']}ms" if x["ms"] is not None else "-"
            print(f"  {x['step']}. {x['node']:<9} {x['status']:<10} linkedin={x['linkedin_actions']:<2} {ms:>8}")
            if x["output"]:
                print(f"       {json.dumps(x['output'])[:180]}")
            if x["error"]:
                print(f"       \033[91mERROR\033[0m {x['error'][:160]}")
        print(f"\n  total LinkedIn actions used: {st['linkedin_actions_used']}")
        if st.get("blocked_reason"): print("  blocked:", st["blocked_reason"])
        if st.get("error"): print("  error:", st["error"])

        hdr("get_results")
        res = (await c.call_tool("get_results", {"run_id": run_id})).data
        print(f"  {res['count']} people\n")
        for p in res["people"]:
            print(f"  \033[1m{p['name']}\033[0m   score {p['score']}   cached={p['served_from_cache']}")
            print(f"    {(p['headline'] or '—')[:95]}")
            print(f"    {p['location'] or '—'}")
            print(f"    {p['linkedin']}")
            print(f"    why: {p['why']}\n")
    return 0

sys.exit(asyncio.run(main()))
