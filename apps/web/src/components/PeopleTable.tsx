import type { Person } from "../lib/api";
import { ago } from "../lib/format";
import { Pill } from "./ui";

/**
 * People, with the provenance attached to the claim.
 *
 * Emails carry the providers that produced them rather than a bare string. A
 * sourcing tool that shows a contact without saying where it came from is
 * asking the user to trust an unattributed guess, and the whole point of
 * keeping per-field evidence in the Person document is to not do that.
 */
export default function PeopleTable({
  people, showScore = false,
}: { people: Person[]; showScore?: boolean }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Headline</th>
          <th>Location</th>
          {showScore && <th className="num">Score</th>}
          <th>Contact</th>
          <th className="num">Sourced</th>
        </tr>
      </thead>
      <tbody>
        {people.map((p) => (
          <tr key={p.id}>
            <td>
              <div className="row" style={{ gap: 7 }}>
                {p.linkedin_url ? (
                  <a href={p.linkedin_url} target="_blank" rel="noreferrer noopener"
                     style={{ color: "var(--live)" }}>{p.name}</a>
                ) : p.name}
                {/* A cache hit is the good outcome: this row cost no LinkedIn action. */}
                {p.from_cache && <Pill tone="free">cached</Pill>}
              </div>
              {p.score_rationale && (
                <div className="muted" style={{ fontSize: 11, marginTop: 3, maxWidth: 380 }}>
                  {p.score_rationale}
                </div>
              )}
            </td>
            <td className="truncate dim">{p.headline ?? "—"}</td>
            <td className="dim">{p.location ?? "—"}</td>
            {showScore && (
              <td className="num">
                {p.score == null ? <span className="muted">—</span> : (
                  <>
                    <span className="score">{p.score.toFixed(0)}</span>
                    <div className="bar-mini" style={{ marginLeft: "auto" }}>
                      <i style={{ width: `${Math.min(100, p.score)}%` }} />
                    </div>
                  </>
                )}
              </td>
            )}
            <td>
              {p.emails.length === 0 && p.phones.length === 0 ? (
                <span className="muted" title="Email and phone enrichment is Phase 2">
                  not enriched
                </span>
              ) : (
                <div className="stack">
                  {p.emails.map((e) => (
                    <div key={e.value} className="mono" style={{ fontSize: 11 }}>
                      {e.value}
                      {e.sources.filter(Boolean).length > 0 && (
                        <span className="muted"> · {e.sources.filter(Boolean).join(", ")}</span>
                      )}
                    </div>
                  ))}
                  {p.phones.map((t) => (
                    <div key={t.value} className="mono" style={{ fontSize: 11 }}>{t.value}</div>
                  ))}
                </div>
              )}
            </td>
            <td className="num muted">{ago(p.refreshed_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
