import { TopBar } from "../App";
import { api } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { ago } from "../lib/format";
import { Empty, ErrorState, Loading, Panel, Pill } from "../components/ui";

export default function Bridges() {
  const { data, error, loading } = usePoll(api.bridges, [], 5_000);
  const bridges = data?.bridges ?? [];
  const origin = window.location.origin;

  return (
    <>
      <TopBar title="Bridge" />
      <div className="page">
        {error && <ErrorState error={error} />}

        <div className="callout info" style={{ marginBottom: 16 }}>
          <b>Your LinkedIn session never leaves your machine.</b> The bridge runs there, holds the
          session, and opens an outbound connection to this server. Only the actions a run needs
          travel down it — one at a time, which is the ceiling on everything this product can do.
        </div>

        <Panel title="Paired machines" sub={`${bridges.length}`} flush>
          {loading ? (
            <Loading what="bridges" />
          ) : bridges.length === 0 ? (
            <Empty title="No bridge paired">
              <p>Run the two commands below on the machine where you are logged into LinkedIn.</p>
            </Empty>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>LinkedIn account</th>
                  <th>Status</th>
                  <th>Version</th>
                  <th className="num">Actions today</th>
                  <th className="num">Last seen</th>
                </tr>
              </thead>
              <tbody>
                {bridges.map((b) => (
                  <tr key={b.id}>
                    <td className="mono">{b.linkedin_username ?? <span className="muted">unknown</span>}</td>
                    <td>
                      {b.status === "busy" ? <Pill tone="live">busy</Pill>
                        : b.attached ? <Pill tone="ok">online</Pill>
                        : <Pill tone="idle">offline</Pill>}
                      {b.attached && !b.ready && (
                        <span className="muted" style={{ marginLeft: 8, fontSize: 11 }}>
                          attached, not ready
                        </span>
                      )}
                      {b.last_error && (
                        <div style={{ color: "var(--bad)", fontSize: 11, marginTop: 4 }}>
                          {b.last_error}
                        </div>
                      )}
                    </td>
                    <td className="muted mono">
                      {b.bridge_version ?? "—"}{b.platform ? ` · ${b.platform}` : ""}
                    </td>
                    <td className="num">{b.actions_today ?? <span className="muted">—</span>}</td>
                    <td className="num muted">{ago(b.last_seen_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Pair a machine" sub="pairing again revokes the previous token">
          <pre className="cmd">
            <b>uvx gtm-bridge pair</b> --url {origin}{"\n"}
            <b>uvx gtm-bridge run</b>
          </pre>
          <p className="dim" style={{ fontSize: 12, marginBottom: 0 }}>
            The first command prints a short code and a link back here to approve it. Approving is
            what decides which workspace the machine joins — the machine does not get to name one,
            which is what stops a stranger's bridge from attaching to your account.
          </p>
        </Panel>
      </div>
    </>
  );
}
