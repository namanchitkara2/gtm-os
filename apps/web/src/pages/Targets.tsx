import { TopBar } from "../App";
import { api } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { ago } from "../lib/format";
import { Empty, ErrorState, Loading, Panel } from "../components/ui";

/** Facets worth surfacing from an ICP; anything else stays in the raw spec. */
const FACETS = ["titles", "locations", "industries", "keywords"] as const;

export default function Targets() {
  const { data, error, loading } = usePoll(api.targets, []);
  const targets = data ?? [];

  return (
    <>
      <TopBar title="Targets" />
      <div className="page">
        {error && <ErrorState error={error} />}
        {loading ? (
          <Panel><Loading what="targets" /></Panel>
        ) : targets.length === 0 ? (
          <Panel>
            <Empty title="No targets defined">
              <p>
                A target is a brief plus the structured reading of it. Both are stored, so an
                interpretation that was wrong can be seen and corrected rather than quietly
                shaping every run that follows.
              </p>
            </Empty>
          </Panel>
        ) : (
          targets.map((t) => (
            <Panel key={t.id} title={t.name} sub={`${t.runs} ${t.runs === 1 ? "run" : "runs"} · ${ago(t.created_at)}`}>
              <div className="callout info" style={{ marginBottom: 12 }}>
                <b>As briefed:</b> {t.brief}
              </div>
              <div className="stack" style={{ gap: 8 }}>
                {FACETS.map((facet) => {
                  const values = (t.spec?.[facet] as string[] | undefined) ?? [];
                  if (values.length === 0) return null;
                  return (
                    <div key={facet} className="row" style={{ alignItems: "flex-start" }}>
                      <span className="muted" style={{ width: 92, flex: "none", fontSize: 11 }}>
                        {facet}
                      </span>
                      <div className="wrap">
                        {values.map((v) => (
                          <span key={String(v)} className="pill idle">{String(v)}</span>
                        ))}
                      </div>
                    </div>
                  );
                })}
                {Boolean(t.spec?.headcount_min ?? t.spec?.headcount_max) && (
                  <div className="row">
                    <span className="muted" style={{ width: 92, flex: "none", fontSize: 11 }}>headcount</span>
                    <span className="mono">
                      {String(t.spec.headcount_min ?? "any")} – {String(t.spec.headcount_max ?? "any")}
                    </span>
                  </div>
                )}
              </div>
            </Panel>
          ))
        )}
      </div>
    </>
  );
}
