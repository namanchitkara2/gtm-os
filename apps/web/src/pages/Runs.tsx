import { useNavigate } from "react-router-dom";
import { TopBar } from "../App";
import { api, isLive } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { ago, duration } from "../lib/format";
import { Empty, ErrorState, Loading, Panel, RunPill } from "../components/ui";

export default function Runs() {
  const nav = useNavigate();
  const { data, error, loading } = usePoll(api.runs, [], 4_000);
  const runs = data?.runs ?? [];
  const live = runs.filter((r) => isLive(r.status)).length;

  return (
    <>
      <TopBar title="Runs" />
      <div className="page">
        {error && <ErrorState error={error} />}
        <Panel
          title="All runs"
          sub={live ? `${live} in flight` : `${runs.length} total`}
          flush
        >
          {loading ? (
            <Loading what="runs" />
          ) : runs.length === 0 ? (
            <Empty title="No runs yet">
              <p>
                Runs are started from your agent, not from here — this is where you watch them.
                Call <span className="mono">start_run</span> with a target id.
              </p>
            </Empty>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Target</th>
                  <th>Status</th>
                  <th className="num">People</th>
                  <th className="num">Cached</th>
                  <th className="num">Actions</th>
                  <th className="num">Took</th>
                  <th className="num">Started</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} className="clickable" onClick={() => nav(`/runs/${r.id}`)}>
                    <td className="mono">{r.workflow}</td>
                    <td className="truncate dim">{r.target_name ?? r.target_brief ?? "—"}</td>
                    <td>
                      <RunPill status={r.status} />
                      {r.blocked_reason && (
                        <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                          {r.blocked_reason}
                        </div>
                      )}
                    </td>
                    <td className="num">{r.people}</td>
                    <td className="num" style={{ color: r.from_cache ? "var(--free)" : undefined }}>
                      {r.from_cache || "—"}
                    </td>
                    <td className="num">{r.total_bridge_calls}</td>
                    <td className="num muted">{duration(r.duration_ms)}</td>
                    <td className="num muted">{ago(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </>
  );
}
