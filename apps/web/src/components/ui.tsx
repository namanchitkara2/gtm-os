import type { ReactNode } from "react";
import type { RunStatus, StepStatus } from "../lib/api";

type Tone = "ok" | "live" | "free" | "warn" | "bad" | "idle";

export function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`pill ${tone}`}>
      <i className="dot" />
      {children}
    </span>
  );
}

/**
 * A run's status, coloured by what it means to the person reading it.
 *
 * `awaiting_bridge` is the case worth distinguishing: nothing has gone wrong —
 * the run is fine and the user's laptop is not reachable. Colouring it as a
 * failure would send people debugging a run that is simply parked.
 */
const RUN_TONE: Record<RunStatus, Tone> = {
  queued: "idle",
  running: "live",
  awaiting_bridge: "warn",
  awaiting_approval: "warn",
  succeeded: "ok",
  failed: "bad",
  cancelled: "idle",
};

export const RunPill = ({ status }: { status: RunStatus }) => (
  <Pill tone={RUN_TONE[status] ?? "idle"}>{status.replace(/_/g, " ")}</Pill>
);

/** `cached` is a success that cost nothing, so it reads as its own outcome. */
const STEP_TONE: Record<StepStatus, Tone> = {
  running: "live",
  succeeded: "ok",
  cached: "free",
  failed: "bad",
  skipped: "idle",
};

export const StepPill = ({ status }: { status: StepStatus }) => (
  <Pill tone={STEP_TONE[status] ?? "idle"}>{status}</Pill>
);

export function Panel({
  title, sub, actions, children, flush,
}: {
  title?: string; sub?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean;
}) {
  return (
    <section className="panel">
      {title && (
        <header className="panel-head">
          <h2>{title}</h2>
          {sub && <span className="sub">{sub}</span>}
          <span className="spacer" />
          {actions}
        </header>
      )}
      {flush ? children : <div className="panel-body">{children}</div>}
    </section>
  );
}

export function Stat({
  label, value, unit, note, accent,
}: {
  label: string; value: ReactNode; unit?: string; note?: ReactNode;
  accent?: "free" | "live" | "warn";
}) {
  return (
    <div className={`stat${accent ? ` accent-${accent}` : ""}`}>
      <div className="k">{label}</div>
      <div className="v">
        {value}
        {unit && <small> {unit}</small>}
      </div>
      {note && <div className="note">{note}</div>}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <div className="empty muted">Loading {what}…</div>;
}

export function ErrorState({ error }: { error: Error }) {
  const unauth = "status" in error && (error as { status?: number }).status === 401;
  return (
    <div className="callout bad">
      <b>{unauth ? "Not signed in." : "Could not load."}</b>{" "}
      {unauth
        ? "The control plane is refusing anonymous callers. Locally, start it with GTMOS_DEV_MODE=1."
        : error.message}
    </div>
  );
}

/** The daily LinkedIn ceiling. Shown in the topbar on every screen because it
 *  is the number that decides whether a customer's account survives. */
export function Budget({ used, limit }: { used: number; limit: number }) {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  const cls = pct >= 100 ? "full" : pct >= 75 ? "hot" : "";
  return (
    <div className="budget" title="LinkedIn actions used today against this workspace's daily budget">
      <span className="bar"><i className={cls} style={{ width: `${pct}%` }} /></span>
      <span>{used}/{limit} actions today</span>
    </div>
  );
}
