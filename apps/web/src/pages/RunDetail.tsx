import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { TopBar } from "../App";
import { api, isLive, type RunStep } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { ago, duration, shortId } from "../lib/format";
import { Empty, ErrorState, Loading, Panel, RunPill, Stat, StepPill } from "../components/ui";
import PeopleTable from "../components/PeopleTable";

export default function RunDetail() {
  const { runId = "" } = useParams();
  // A finished run's trace never changes again, so polling stops the moment it
  // reaches a terminal state rather than hammering the API for a static row.
  const [watching, setWatching] = useState(true);
  const run = usePoll(() => api.run(runId), [runId], watching ? 1_500 : 0);
  const people = usePoll(() => api.runPeople(runId), [runId], watching ? 4_000 : 0);
  const [cancelling, setCancelling] = useState(false);

  const status = run.data?.status;
  useEffect(() => {
    if (status) setWatching(isLive(status));
  }, [status]);

  const data = run.data;
  if (run.error) return <><TopBar title="Run" /><div className="page"><ErrorState error={run.error} /></div></>;
  if (!data) return <><TopBar title="Run" /><div className="page"><Loading what="run" /></div></>;

  const cached = data.steps.filter((s) => s.status === "cached").length;
  const found = data.progress.people ?? data.people;

  const cancel = async () => {
    setCancelling(true);
    try { await api.cancelRun(runId); run.reload(); } finally { setCancelling(false); }
  };

  return (
    <>
      <TopBar
        title={`Run ${shortId(runId)}`}
        actions={
          isLive(data.status) ? (
            <button className="btn danger" onClick={cancel} disabled={cancelling}>
              {cancelling ? "Cancelling…" : "Cancel run"}
            </button>
          ) : undefined
        }
      />
      <div className="page">
        <div className="row" style={{ marginBottom: 14, flexWrap: "wrap" }}>
          <RunPill status={data.status} />
          <span className="mono muted">{data.workflow}</span>
          {data.target_name && (
            <>
              <span className="muted">·</span>
              <Link to="/targets" className="dim">{data.target_name}</Link>
            </>
          )}
          <span className="muted">· started {ago(data.created_at)}</span>
        </div>

        {data.target_brief && (
          <div className="callout info" style={{ marginBottom: 16 }}>
            <b>Brief:</b> {data.target_brief}
          </div>
        )}

        {data.status === "awaiting_bridge" && (
          <div className="callout" style={{ marginBottom: 16 }}>
            <b>Parked, not failed.</b> {data.blocked_reason ?? "The bridge is not reachable."}{" "}
            Start the bridge on your machine and the run picks up where it stopped.{" "}
            <Link to="/bridges" style={{ color: "var(--live)" }}>Bridge status →</Link>
          </div>
        )}
        {data.error && (
          <div className="callout bad" style={{ marginBottom: 16 }}>
            <b>Failed.</b> <span className="mono">{data.error}</span>
          </div>
        )}

        <div className="grid c4">
          <Stat label="LinkedIn actions" value={data.total_bridge_calls}
                note={`limit was ${data.limits.max_people ?? "—"} people`} />
          <Stat label="Steps free" value={cached} accent={cached ? "free" : undefined}
                note="answered from cache" />
          <Stat label="People" value={found} note={`${data.from_cache} already known`} />
          <Stat label="Duration" value={duration(data.duration_ms)} />
        </div>

        <Panel title="Trace" sub="every node, in order, with what it cost">
          <div className="trace">
            {data.steps.length === 0 && <Empty title="No steps recorded yet" />}
            {data.steps.map((s) => <Step key={s.sequence} step={s} />)}
          </div>
        </Panel>

        <Panel title="Results" sub={`${people.data?.people.length ?? 0} people, best match first`} flush>
          {people.data?.people.length
            ? <PeopleTable people={people.data.people} showScore />
            : <Empty title="No people yet">
                <p>Results are written as the run finishes each stage.</p>
              </Empty>}
        </Panel>
      </div>
    </>
  );
}

function Step({ step }: { step: RunStep }) {
  const [open, setOpen] = useState(false);
  const tone =
    step.status === "cached" ? "free"
    : step.status === "succeeded" ? "ok"
    : step.status === "running" ? "live"
    : step.status === "failed" ? "bad" : "idle";

  const outputs = Object.entries(step.output_summary ?? {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0),
  );
  // Lists are the interesting part of a node's output — who was dropped and
  // why, which profiles the search rendered but would not hand over. Collapsing
  // them to a count hides exactly the thing that explains a disappointing run,
  // so the count is the summary and the names are one click away.
  const lists = outputs.filter(([, v]) => Array.isArray(v)) as [string, unknown[]][];
  const scalars = outputs.filter(([, v]) => !Array.isArray(v));

  return (
    <div className="trace-step">
      <div className="trace-rail"><div className={`trace-node ${tone}`} /></div>
      <div className="trace-body">
        <div className="trace-title">
          <b>{step.node}</b>
          <StepPill status={step.status} />
          <span className="muted mono" style={{ fontSize: 11 }}>{duration(step.duration_ms)}</span>
          {step.retries > 0 && <span className="muted" style={{ fontSize: 11 }}>{step.retries} retries</span>}
        </div>

        {(scalars.length > 0 || lists.length > 0) && (
          <div className="trace-out">
            {scalars.map(([k, v]) => (
              <span key={k}>{k.replace(/_/g, " ")} <b>{String(v)}</b></span>
            ))}
            {lists.map(([k, v]) => (
              <span key={k}>{k.replace(/_/g, " ")} <b>{v.length}</b></span>
            ))}
            {lists.length > 0 && (
              <button
                className="link-toggle"
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
              >
                {open ? "hide detail" : "show detail"}
              </button>
            )}
          </div>
        )}

        {open && lists.map(([k, v]) => (
          <div key={k} className="trace-detail">
            <div className="muted">{k.replace(/_/g, " ")}</div>
            <div className="wrap">
              {v.map((item, i) => (
                <span key={i} className="pill idle">{String(item)}</span>
              ))}
            </div>
          </div>
        ))}

        {step.error && (
          <div className="trace-out" style={{ color: "var(--bad)" }}>{step.error}</div>
        )}
      </div>
      <div className="trace-cost">
        <div className={`n${step.bridge_calls === 0 ? " zero" : ""}`}>{step.bridge_calls}</div>
        <div className="l">{step.bridge_calls === 1 ? "action" : "actions"}</div>
      </div>
    </div>
  );
}
