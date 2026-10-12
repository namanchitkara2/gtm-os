import { Link, useNavigate } from "react-router-dom";
import { TopBar } from "../App";
import type { Bridge, Me, RunSummary } from "../lib/api";
import { isLive } from "../lib/api";
import { ago, duration } from "../lib/format";
import { Empty, ErrorState, Panel, RunPill, Stat } from "../components/ui";

interface Props {
  me: Me | null;
  bridges: { bridges: Bridge[]; any_online: boolean } | null;
  runs: { runs: RunSummary[] } | null;
  error: Error | null;
}

export default function Overview({ me, bridges, runs, error }: Props) {
  const list = runs?.runs ?? [];
  const online = bridges?.any_online ?? false;

  // A brand-new workspace has no bridge and no runs, and that is not an error
  // state — it is the first thing every customer sees. It gets the whole page.
  if (!error && list.length === 0 && !online) return <FirstRun />;

  const people = list.reduce((n, r) => n + r.people, 0);
  const cached = list.reduce((n, r) => n + r.from_cache, 0);
  const actions = list.reduce((n, r) => n + r.total_bridge_calls, 0);
  const live = list.filter((r) => isLive(r.status));
  const budget = me?.tenant?.daily_bridge_budget ?? 100;

  return (
    <>
      <TopBar title="Overview" />
      <div className="page">
        {error && <ErrorState error={error} />}

        <div className="grid c4">
          <Stat
            label="LinkedIn actions"
            value={actions}
            note={`${me?.today.bridge_calls ?? 0} today of ${budget} allowed`}
          />
          {/* The product's actual claim, stated as a number: people obtained
              without spending a browser action. */}
          <Stat
            label="Served from cache"
            value={cached}
            accent="free"
            note={people ? `${Math.round((cached / people) * 100)}% of people cost 0 actions` : "no people yet"}
          />
          <Stat label="People sourced" value={people} note={`across ${list.length} runs`} />
          <Stat
            label="Running now"
            value={live.length}
            accent={live.length ? "live" : undefined}
            note={online ? "bridge attached" : "bridge offline"}
          />
        </div>

        {!online && list.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <div className="callout">
              <b>No bridge attached.</b> Anything that is not LinkedIn still works, but new
              sourcing runs will park at <span className="mono">awaiting_bridge</span> until the
              bridge on your machine is running. <Link to="/bridges" style={{ color: "var(--live)" }}>Connect it →</Link>
            </div>
          </div>
        )}

        <Panel title="Recent runs" sub={`${list.length} total`} flush actions={<Link className="mono muted" to="/runs">all runs →</Link>}>
          {list.length === 0 ? (
            <Empty title="No runs yet">
              <p>Start one from your agent with <span className="mono">define_target</span> then <span className="mono">start_run</span>.</p>
            </Empty>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Target</th>
                  <th>Status</th>
                  <th className="num">People</th>
                  <th className="num">Actions</th>
                  <th className="num">Took</th>
                  <th className="num">Started</th>
                </tr>
              </thead>
              <tbody>
                {list.slice(0, 8).map((r) => (
                  <RunRow key={r.id} run={r} />
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </>
  );
}

export function RunRow({ run }: { run: RunSummary }) {
  const nav = useNavigate();
  return (
    <tr className="clickable" onClick={() => nav(`/runs/${run.id}`)}>
      <td className="mono muted">{run.workflow}</td>
      <td className="truncate dim">{run.target_name ?? run.target_brief ?? "—"}</td>
      <td><RunPill status={run.status} /></td>
      <td className="num">
        {run.people}
        {run.from_cache > 0 && <span style={{ color: "var(--free)" }}> ({run.from_cache} cached)</span>}
      </td>
      <td className="num">{run.total_bridge_calls}</td>
      <td className="num muted">{duration(run.duration_ms)}</td>
      <td className="num muted">{ago(run.created_at)}</td>
    </tr>
  );
}

/** What a customer sees on day one: one instruction, not a dashboard of zeroes. */
function FirstRun() {
  return (
    <>
      <TopBar title="Overview" />
      <div className="page">
        <Panel title="Connect your machine" sub="one-time setup">
          <p className="dim" style={{ marginTop: 0, maxWidth: 620 }}>
            LinkedIn work runs on the computer where you are already signed in — your session
            never reaches this server. Run the bridge there, approve the code it prints, and
            leave it running.
          </p>
          <pre className="cmd">
            <b>uvx gtm-bridge pair</b> --url {window.location.origin}{"\n"}
            <b>uvx gtm-bridge run</b>
          </pre>
          <p className="dim" style={{ fontSize: 12, marginBottom: 0 }}>
            Then point your agent at the control plane:{" "}
            <span className="mono">claude mcp add --transport http gtm-os {window.location.origin}/mcp</span>
          </p>
        </Panel>

        <Panel title="Then, from your agent">
          <div className="stack" style={{ gap: 10 }}>
            <div>
              <div className="mono" style={{ color: "var(--live)" }}>define_target</div>
              <div className="dim">Describe who you want in plain English. Stored verbatim, so the
                interpretation can be corrected instead of quietly shaping every future run.</div>
            </div>
            <div>
              <div className="mono" style={{ color: "var(--live)" }}>start_run</div>
              <div className="dim">Returns a run id immediately. LinkedIn permits one action at a
                time, so sourcing ten people is eleven sequential steps — nothing useful can block
                on that.</div>
            </div>
            <div>
              <div className="mono" style={{ color: "var(--live)" }}>get_run · get_results</div>
              <div className="dim">Poll the trace and read structured rows. The same trace renders
                here, step by step.</div>
            </div>
          </div>
        </Panel>
      </div>
    </>
  );
}
