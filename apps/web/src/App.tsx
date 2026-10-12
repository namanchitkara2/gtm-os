import { NavLink, Route, Routes } from "react-router-dom";
import { api, isLive } from "./lib/api";
import { usePoll } from "./lib/hooks";
import { Budget, Pill } from "./components/ui";
import Overview from "./pages/Overview";
import Bridges from "./pages/Bridges";
import ApprovePairing from "./pages/ApprovePairing";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import People from "./pages/People";
import Targets from "./pages/Targets";

export default function App() {
  return (
    <Routes>
      {/* Pairing stands outside the shell. It is the first screen a new user
          reaches — the bridge printed its link before there is anything to
          navigate to — so it must not depend on a dashboard that is empty. */}
      <Route path="/bridges/approve" element={<ApprovePairing />} />
      <Route path="/*" element={<Shell />} />
    </Routes>
  );
}

function Shell() {
  const me = usePoll(api.me, []);
  // Bridge presence is a live fact about someone's laptop; runs tell us whether
  // anything is in flight worth a fast refresh.
  const bridges = usePoll(api.bridges, [], 5_000);
  const runs = usePoll(api.runs, [], 4_000);

  const live = (runs.data?.runs ?? []).filter((r) => isLive(r.status));
  const online = bridges.data?.any_online ?? false;
  const tenant = me.data?.tenant;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <b>GTM-OS</b>
          <span>{tenant?.plan ?? ""}</span>
        </div>
        <nav className="nav">
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/runs">
            Runs
            <span className="count">{live.length ? `${live.length} live` : runs.data?.runs.length ?? ""}</span>
          </NavLink>
          <NavLink to="/people">People</NavLink>
          <NavLink to="/targets">Targets</NavLink>
          <NavLink to="/bridges">
            Bridge
            <Pill tone={online ? "ok" : "idle"}>{online ? "online" : "offline"}</Pill>
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          {tenant?.name ?? "—"}
          {me.data?.dev_mode && (
            <div style={{ marginTop: 6 }}>
              <Pill tone="warn">dev mode — no auth</Pill>
            </div>
          )}
        </div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Overview me={me.data} bridges={bridges.data} runs={runs.data} error={me.error ?? runs.error} />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/people" element={<People />} />
          <Route path="/targets" element={<Targets />} />
          <Route path="/bridges" element={<Bridges />} />
        </Routes>
      </main>
    </div>
  );
}

/** Shared topbar. Pages render it themselves so each can own its title and
 *  actions while the budget readout stays in one place. */
export function TopBar({ title, actions }: { title: string; actions?: React.ReactNode }) {
  const me = usePoll(api.me, [], 15_000);
  return (
    <header className="topbar">
      <h1>{title}</h1>
      <span className="spacer" />
      {actions}
      {me.data?.tenant && (
        <Budget used={me.data.today.bridge_calls} limit={me.data.tenant.daily_bridge_budget} />
      )}
    </header>
  );
}
