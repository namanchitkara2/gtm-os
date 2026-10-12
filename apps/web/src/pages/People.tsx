import { useState } from "react";
import { TopBar } from "../App";
import { api } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { Empty, ErrorState, Loading, Panel } from "../components/ui";
import PeopleTable from "../components/PeopleTable";

export default function People() {
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState("");
  const { data, error, loading } = usePoll(() => api.people(applied), [applied]);
  const people = data?.people ?? [];

  return (
    <>
      <TopBar title="People" />
      <div className="page">
        {error && <ErrorState error={error} />}
        <Panel
          title="Cached people"
          sub={`${people.length} rows`}
          flush
          actions={
            <form
              onSubmit={(e) => { e.preventDefault(); setApplied(query.trim()); }}
              style={{ width: 240 }}
            >
              <input
                className="input"
                placeholder="Search name, headline, location"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </form>
          }
        >
          {loading ? (
            <Loading what="people" />
          ) : people.length === 0 ? (
            <Empty title={applied ? "Nothing matches that" : "No people cached yet"}>
              <p>
                Every row here is a LinkedIn action already spent. Once a person is cached, a
                later run that finds them again costs nothing — which is why this table is the
                asset, not a byproduct of the run that filled it.
              </p>
            </Empty>
          ) : (
            <PeopleTable people={people} />
          )}
        </Panel>
      </div>
    </>
  );
}
