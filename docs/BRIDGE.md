# The bridge, and how a write happens exactly once

## Why a bridge exists

The upstream LinkedIn MCP server works because it is local: it drives a real
Chromium against a persistent profile using the user's own live session, and it
explicitly refuses to read a host browser cookie on a non-loopback bind. Hosting
it on AWS deletes that premise. The two alternatives were a cloud browser farm
(you become custodian of customers' LinkedIn credentials, logins appear from
datacenter ASNs, accounts get restricted) or a hosted vendor API (per-seat cost,
vendor dependency).

The bridge is the third option: the cloud runs the graphs, and a small local
agent holds the LinkedIn session. No cookie, password, or browser profile ever
reaches AWS.

## Why it is not a fork

The plan called for forking the upstream server and adding a `BRIDGE` role beside
its `DIRECT`/`OWNER`/`PROXY` roles. Reading the code showed a smaller design:
everything the bridge needs *from* that server is already exposed as MCP tools,
and everything the bridge *adds* — the tunnel, the ledger, the budget — needs to
live outside it anyway.

So the bridge runs the published package as a child process and speaks MCP to it
as an ordinary client. No fork to maintain, upstream fixes arrive by version
bump, no derivative work (plain dependency attribution instead of modification
notices), and their daemon election, profile lease, and sequential middleware
keep working exactly as designed. The cost is one process hop per call, which is
noise beside a page scrape.

## The send-once problem

`daemon_proxy.py` upstream states the rule its local version relies on: *only a
request that provably never left this process may be repeated*, because
repeating a connection request sends it twice. In-process that is decidable.
Across a socket it is not — a drop after dispatch and before result is genuinely
ambiguous, and both confident answers are wrong.

Four frames make it decidable:

| Frame | Meaning | Cloud policy |
|---|---|---|
| `reject` | provably not started | redispatch freely, writes included |
| no `ack` within 15s | never taken (ack precedes execution) | redispatch freely |
| `ack`, no `result` | **ambiguous** | never blind-retry a write — ask the ledger |
| `result` | terminal | done |

### The ledger

Bridge-local SQLite, `synchronous=FULL`, written **before** the action:

- `begin(key)` claims the key; a second claim is refused. This is the guard that
  survives a socket drop, a redispatch, and a control-plane restart.
- A recorded *failure* means the action provably did not take effect → safe to retry.
- A row still at `started` means we died mid-flight → **`unknown`**.

`unknown` is a real answer, not a bookkeeping gap, and the protocol carries it
rather than collapsing it. The cloud's policy for writes is to treat `unknown` as
performed: a missed outreach is recoverable, a duplicate connection request is
not, and it is the customer's LinkedIn account that pays for the mistake.

### And a second ledger, server-side

The bridge-local ledger is not sufficient alone. A user who re-pairs on a new
machine, or whose state directory is wiped, comes back with an empty ledger and
would send again. So `action_ledger` mirrors it in Postgres keyed by
`(tenant, person_urn, action)`, with a partial unique index that makes a repeat
physically impossible regardless of which code path asks:

```sql
CREATE UNIQUE INDEX action_ledger_once_idx ON action_ledger (tenant_id, person_urn, action)
    WHERE status IN ('dispatched','acked','performed');
```

A `failed` row is excluded, so a genuine retry is still allowed.

### One more trap

LangGraph's Postgres checkpointer **replays the node that was in flight** when a
worker restarts. Free for reads, a double-send for writes. Write nodes therefore
check the ledger *inside the node body*; checkpoint boundaries are not a safety
mechanism.

## The web agent (`gtm-bridge run --web`)

A second backend for non-LinkedIn sites (Workday, Greenhouse, Ashby, career
pages): the Stagehand server in `~/stagehand-agent`, spawned the same way as the
LinkedIn server and put behind `RoutingExecutor`. Four rules keep it from
weakening anything above:

- **Reads only.** It runs with `STAGEHAND_READ_ONLY=1`, and `WebExecutor.start()`
  refuses to run if the server offers any tool outside `WEB_TOOLS`. `web_act` can
  submit a form, so it may join only after it is in `WRITE_ACTIONS`.
- **Its own profile.** A second Chromium on `~/.linkedin-mcp` would break the
  upstream profile lease this whole design rests on.
- **No LinkedIn.** The server refuses linkedin.com and lnkd.in, so it cannot become a side
  door around the ledger or the budget.
- **Not budgeted.** `WEB_TOOLS` skip the bridge's daily counter, which is the
  one place the tenant's `daily_bridge_budget` (handed over at pairing) is
  enforced per action; the control plane's `dispatch()` does not count per tool.
  Health is checked per tool, so a LinkedIn checkpoint does not stop a portal
  read and a crashed web agent does not stop LinkedIn.

Status: the bridge advertises and serves these tools, but no graph in
`packages/graphs` or workflow in the control plane dispatches them yet.
