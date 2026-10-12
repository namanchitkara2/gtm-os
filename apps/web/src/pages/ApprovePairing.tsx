import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { Panel, Pill } from "../components/ui";

/**
 * The device-code approval screen.
 *
 * The bridge prints a link to this page and then polls; approving here is what
 * binds a laptop to a workspace. It renders outside the dashboard shell because
 * it is usually the very first page a customer opens, when there is no run, no
 * person and no bridge for a shell to show.
 */
export default function ApprovePairing() {
  const params = new URLSearchParams(window.location.search);
  const [code, setCode] = useState(params.get("code") ?? "");
  const [state, setState] = useState<"idle" | "sending" | "done" | "error">("idle");
  const [message, setMessage] = useState("");
  const me = usePoll(api.me, []);

  useEffect(() => { document.title = "Approve a machine · GTM-OS"; }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setState("sending");
    try {
      await api.approvePairing(code);
      setState("done");
    } catch (err) {
      setState("error");
      setMessage(
        err instanceof ApiError && err.status === 410
          ? "That code has expired. Codes last ten minutes — run `gtm-bridge pair` again."
          : err instanceof Error ? err.message : "Approval failed.",
      );
    }
  };

  return (
    <div className="center-page">
      <div className="card-narrow">
        <div className="brand" style={{ borderBottom: 0, padding: "0 0 18px", justifyContent: "center" }}>
          <b>GTM-OS</b>
          <span>pair a machine</span>
        </div>

        {state === "done" ? (
          <Panel title="Approved">
            <div className="row" style={{ marginBottom: 10 }}>
              <Pill tone="ok">paired</Pill>
              <span className="dim">This machine can now act as your LinkedIn identity.</span>
            </div>
            <p className="dim" style={{ fontSize: 12 }}>
              Back in the terminal, the bridge has collected its token. Start it and leave it
              running:
            </p>
            <pre className="cmd"><b>uvx gtm-bridge run</b></pre>
            <p className="muted" style={{ fontSize: 11, marginBottom: 0 }}>
              Any machine you paired before this one has been revoked.
            </p>
          </Panel>
        ) : (
          <Panel title="Approve this code">
            <p className="dim" style={{ marginTop: 0, fontSize: 12 }}>
              Only approve a code you are looking at in your own terminal. Approving binds that
              machine to
              {" "}<b className="mono">{me.data?.tenant?.name ?? "your workspace"}</b> and lets it
              act with your LinkedIn session.
            </p>
            <form onSubmit={submit}>
              <input
                className="input code"
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                placeholder="XXXX-XXXX"
                autoFocus
                spellCheck={false}
              />
              {state === "error" && (
                <div className="callout bad" style={{ marginTop: 12 }}>{message}</div>
              )}
              <button
                className="btn primary"
                style={{ width: "100%", marginTop: 12, padding: "9px" }}
                disabled={state === "sending" || code.trim().length < 8}
              >
                {state === "sending" ? "Approving…" : "Approve this machine"}
              </button>
            </form>
            <p className="muted" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
              Codes expire ten minutes after they are printed.
            </p>
          </Panel>
        )}
      </div>
    </div>
  );
}
