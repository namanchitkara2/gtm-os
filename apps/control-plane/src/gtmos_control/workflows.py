"""The workflow library, and the nodes the runs are made of.

Node functions are plain async callables over a mutable ``RunContext`` rather
than LangGraph nodes yet. The shape is chosen to be the shape LangGraph wants —
one step, one recorded trace row, resumable from persisted state — so swapping
the executor for a compiled graph changes ``runs.py`` and leaves these alone.

Every node records what it cost. ``bridge_calls`` is the number that matters:
the profile lease upstream permits one LinkedIn action at a time per user, so a
run's wall-clock is essentially its bridge-call count, and a node that answered
from cache is the single most useful thing a trace can show.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import UUID

from gtmos_schemas import (
    ICP,
    Employment,
    Person,
    PersonIdentity,
    Sourced,
    WorkflowRef,
)

logger = logging.getLogger(__name__)

#: A profile link, absolute or relative. Upstream returns relative hrefs
#: (``/in/yaminimaggo/``) in its ``references``, so matching only on
#: ``linkedin.com/in/`` finds nothing at all — which is exactly how the first
#: version of this parser silently sourced zero people from a good search.
_PROFILE_RE = re.compile(r"(?:linkedin\.com)?/in/([A-Za-z0-9\-_%]+)")

#: A search result headline: a name followed by its connection degree. Used to
#: separate genuine results from the other people LinkedIn names on the page.
_RESULT_RE = re.compile(r"([^\n•·]{2,80}?)\s*\n?\s*[•·]\s*(?:1st|2nd|3rd)\b")


def username_from_url(url: str) -> str | None:
    match = _PROFILE_RE.search(url or "")
    return match.group(1).lower() if match else None


@dataclass(frozen=True)
class SearchResult:
    username: str
    name: str
    headline: str | None = None
    location: str | None = None


#: Places LinkedIn and users spell differently. Not a gazetteer — just the
#: handful where a literal match fails on results a user would call correct.
_PLACE_ALIASES: dict[str, tuple[str, ...]] = {
    "bangalore": ("bengaluru", "bangalore"),
    "bengaluru": ("bengaluru", "bangalore"),
    "bombay": ("mumbai", "bombay"),
    "mumbai": ("mumbai", "bombay"),
    "delhi": ("delhi", "new delhi", "gurugram", "gurgaon", "noida"),
    "ncr": ("delhi", "new delhi", "gurugram", "gurgaon", "noida"),
    "calcutta": ("kolkata", "calcutta"),
    "madras": ("chennai", "madras"),
    "uae": ("united arab emirates", "dubai", "abu dhabi"),
    "usa": ("united states", "usa"),
    "us": ("united states", "usa"),
    "uk": ("united kingdom", "england", "scotland", "wales"),
    "sf": ("san francisco", "bay area"),
}


def location_matches(wanted: list[str], actual: str | None) -> bool | None:
    """Whether a profile's location satisfies the ICP. None when unknowable.

    This exists because **LinkedIn's location filter does not work through the
    search URL.** Upstream builds ``&location=Bangalore``, but LinkedIn filters
    geography by a numeric ``geoUrn`` facet and silently ignores free text — the
    same trap upstream *does* guard against for ``current_company``, where it
    raises rather than accept a plain name. Asking for Bangalore returned people
    in Dallas, Dubai and Texas, and nothing anywhere said so.

    So geography is enforced here instead, against the location LinkedIn renders
    next to each result. Doing it at the search step rather than after profiling
    matters: a filtered-out candidate costs zero LinkedIn calls, and calls are
    the scarce resource.
    """
    if not wanted:
        return True
    if not actual:
        return None
    haystack = actual.lower()
    for place in wanted:
        needle = place.strip().lower()
        if not needle:
            continue
        for variant in _PLACE_ALIASES.get(needle, (needle,)):
            if variant in haystack:
                return True
    return False


def parse_search_results(payload: Any) -> tuple[list[SearchResult], list[str]]:
    """Parse a people-search page into results, and the results we cannot reach.

    Reads the rendered text rather than the reference links, because not every
    person named on the page is a result: LinkedIn also names mutual
    connections, which appear in ``references`` as ordinary person links and are
    indistinguishable by shape. One real search carried 15 person links for 10
    results.
    """
    sections = payload.get("sections") if isinstance(payload, dict) else None
    references = payload.get("references") if isinstance(payload, dict) else None
    text = ""
    if isinstance(sections, dict):
        text = sections.get("search_results") or next(iter(sections.values()), "")

    by_name: dict[str, str] = {}
    if isinstance(references, dict):
        for entries in references.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("kind") == "person":
                    username = username_from_url(entry.get("url", ""))
                    name = (entry.get("text") or "").strip()
                    if username and name and name not in by_name:
                        by_name[name] = username

    matches = list(_RESULT_RE.finditer(text))
    results: list[SearchResult] = []
    unlinked: list[str] = []
    seen: set[str] = set()

    for index, match in enumerate(matches):
        name = match.group(1).strip()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = meaningful_lines(text[match.end():block_end])

        headline = location = None
        for line in block[:3]:
            if line.startswith(("Current:", "Past:")):
                continue
            if looks_like_location(line):
                location = location or line
            elif headline is None:
                headline = line

        username = by_name.get(name)
        if username is None:
            unlinked.append(name)
            continue
        if username in seen:
            continue
        seen.add(username)
        results.append(SearchResult(username, name, headline, location))

    if not results and by_name:
        for name, username in list(by_name.items())[:5]:
            results.append(SearchResult(username, name))
    return results, unlinked


def extract_result_people(
    payload: Any, *, limit: int | None = None
) -> tuple[list[tuple[str, str]], list[str]]:
    results, unlinked = parse_search_results(payload)
    if limit is not None:
        results = results[:limit]
    return [(r.username, r.name) for r in results], unlinked


def extract_usernames(payload: Any, *, limit: int | None = None) -> list[str]:
    people, _ = extract_result_people(payload, limit=limit)
    return [u for u, _ in people]


#: Lines LinkedIn renders around a profile that carry no information about the
#: person. Positional parsing without this filter reads the connection degree as
#: a job title — which it did: every sourced person scored 0.0 because "· 2nd"
#: matches no ICP term.
_NOISE_RE = re.compile(
    r"""^(?:
        [·•]\s*(?:1st|2nd|3rd)           # connection degree
      | [·•]                             # bare separator
      | (?:He|She|They)/(?:Him|Her|Them) # pronouns
      | Contact\s+info
      | \d[\d,\.]*\+?\s*(?:followers|connections)?
      | (?:Connect|Message|More|Follow|Following|Activity)
      | .*\bis\s+a\s+mutual\s+connection\b.*
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

#: Words that appear in job headlines and effectively never in place names.
#: Needed because "Head of Growth, Head of Marketing, Ex Uber, IIM" is a
#: comma-separated line starting with a capital — indistinguishable from
#: "Delhi, India" by shape alone, and duly misread as a location.
_ROLE_HINT = re.compile(
    r"\b(head|vp|vice\s+president|director|manager|engineer|founder|co-?founder"
    r"|ceo|cto|coo|cmo|cfo|lead|consultant|specialist|analyst|officer|president"
    r"|partner|advisor|sales|marketing|growth|product|design|data|operations"
    r"|recruiter|talent|ex[-.\s]|@)\b",
    re.IGNORECASE,
)

#: "San Francisco Bay Area", "Greater Bengaluru Area", "Delhi NCR Region".
_REGION_SUFFIX = re.compile(r"\b(Area|Region|Metropolitan\s+Area)$", re.IGNORECASE)


def looks_like_location(line: str) -> bool:
    """Whether a line is a place rather than a job title.

    A previous version used one regex with an alternation and ``re.match``,
    which had two faults at once: it accepted any comma-separated capitalised
    line, so headlines became locations; and its ``\bArea$`` branch could only
    ever match at position 0 under ``match()``, so "San Francisco Bay Area" was
    never recognised at all.
    """
    line = line.strip()
    if not line or len(line) > 60:
        return False
    if " at " in line or "|" in line or _ROLE_HINT.search(line):
        return False
    if _REGION_SUFFIX.search(line):
        return True
    parts = [part.strip() for part in line.split(",")]
    return len(parts) >= 2 and all(1 <= len(part) <= 32 for part in parts)


def meaningful_lines(text: str) -> list[str]:
    """Profile lines with LinkedIn's chrome removed."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not _NOISE_RE.match(line.strip())
    ]


def parse_profile_text(text: str) -> dict[str, str | None]:
    """Pull name, headline and location out of a scraped profile page.

    Heuristic, and honest about it. Upstream returns the rendered page as text
    and tells the caller's model to read it; until the model-backed structuring
    node is enabled (it needs an Anthropic key), this reads the layout instead:
    name first, headline next, and a location only where a line actually looks
    like one.
    """
    lines = meaningful_lines(text)
    if not lines:
        return {"full_name": None, "headline": None, "location": None}

    name = lines[0]
    headline = location = None
    for line in lines[1:5]:
        if looks_like_location(line):
            location = location or line
        elif headline is None:
            headline = line
    return {"full_name": name, "headline": headline, "location": location}


def person_from_raw(username: str, payload: dict[str, Any]) -> Person:
    """Best-effort structuring of an upstream profile payload.

    Deliberately heuristic and deliberately marked as such: upstream returns
    ``{url, sections: {name -> raw text}}`` and tells the caller's LLM to parse
    it. The model-backed structuring node replaces this and will carry
    ``method="inference"`` evidence; until then every field here is honest about
    being scraped text read positionally, at a confidence that says so.
    """
    sections = payload.get("sections") or {}
    main = sections.get("main_profile") or next(iter(sections.values()), "")
    url = payload.get("url") or f"https://www.linkedin.com/in/{username}"

    parsed = parse_profile_text(main)
    name = parsed["full_name"] or username
    headline = parsed["headline"]
    location = parsed["location"]

    person = Person(
        identity=PersonIdentity(
            linkedin_urn=payload.get("profile_urn"),
            linkedin_username=username,
            full_name=name,
            headline=headline,
            location=location,
        )
    )
    if headline and " at " in headline:
        title, _, company = headline.partition(" at ")
        person.employments.append(
            Employment(company_name=company.strip(), title=title.strip(), is_current=True)
        )
    if main:
        person.summary = Sourced.of(
            main[:2000], provider="linkedin", method="scrape", confidence=0.9, source_url=url
        )
    return person


@dataclass
class RunContext:
    """Everything a node reads and writes. Persisted between steps."""

    run_id: UUID
    tenant_id: UUID
    icp: ICP
    limits: dict[str, int]
    dispatch: Callable[..., Awaitable[Any]]
    cache_lookup: Callable[[list[str]], Awaitable[dict[str, Person]]]
    cache_store: Callable[[Person, bool], Awaitable[None]]

    usernames: list[str] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    from_cache: set[str] = field(default_factory=set)
    #: username -> display name as the search page rendered it, which is more
    #: reliable than the first line of a scraped profile.
    names: dict[str, str] = field(default_factory=dict)
    #: What the search page already told us, which is often better than the
    #: profile page and costs nothing extra.
    search_hints: dict[str, "SearchResult"] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def max_people(self) -> int:
        return int(self.limits.get("max_people", 10))


@dataclass
class StepCost:
    bridge_calls: int = 0
    cached: bool = False
    summary: dict[str, Any] = field(default_factory=dict)


NodeFn = Callable[[RunContext], Awaitable[StepCost]]


async def node_source(ctx: RunContext) -> StepCost:
    """Find candidates from the ICP and drop the ones in the wrong place.

    Geography is enforced here, not by LinkedIn: its search URL ignores a
    free-text location (see :func:`location_matches`). Filtering at this step
    rather than after profiling is what makes it cheap — a rejected candidate
    costs no LinkedIn call, and calls are the whole budget.
    """
    keywords = " ".join(ctx.icp.titles or ctx.icp.keywords or ["head of growth"])
    args: dict[str, Any] = {"keywords": keywords}
    if ctx.icp.locations:
        args["location"] = ctx.icp.locations[0]

    payload = await ctx.dispatch("search_people", args, deadline_s=180)
    results, unlinked = parse_search_results(payload)

    kept: list[SearchResult] = []
    wrong_place: list[str] = []
    unknown_place: list[str] = []
    for result in results:
        verdict = location_matches(ctx.icp.locations, result.location)
        if verdict is False:
            wrong_place.append(f"{result.name} ({result.location})")
        else:
            if verdict is None and ctx.icp.locations:
                unknown_place.append(result.name)
            kept.append(result)
        if len(kept) >= ctx.max_people:
            break

    ctx.usernames = [r.username for r in kept]
    ctx.names = {r.username: r.name for r in kept}
    ctx.search_hints = {r.username: r for r in kept}
    if unlinked:
        ctx.notes.append(f"{len(unlinked)} result(s) had no profile link: {', '.join(unlinked)}")

    return StepCost(
        bridge_calls=1,
        summary={
            "keywords": keywords,
            "seen": len(results),
            "kept": len(kept),
            "results": [r.name for r in kept],
            # Surfaced, not hidden: LinkedIn's own location filter does nothing,
            # so the user should see how much of the page was off-target.
            "dropped_wrong_location": wrong_place,
            "location_unknown": unknown_place,
            "seen_but_unreachable": unlinked,
        },
    )


async def node_dedupe(ctx: RunContext) -> StepCost:
    """Answer from cache wherever possible. Costs no LinkedIn calls at all."""
    cached = await ctx.cache_lookup(ctx.usernames)
    for username, person in cached.items():
        ctx.people.append(person)
        ctx.from_cache.add(username)
    remaining = [u for u in ctx.usernames if u not in cached]
    ctx.usernames = remaining
    return StepCost(
        cached=bool(cached),
        summary={"served_from_cache": len(cached), "still_to_fetch": len(remaining)},
    )


async def node_profile(ctx: RunContext) -> StepCost:
    """Fetch each uncached profile. One bridge call each — the expensive node."""
    calls = 0
    budget = ctx.max_people - len(ctx.people)
    for username in ctx.usernames[: max(0, budget)]:
        try:
            payload = await ctx.dispatch(
                "get_person_profile", {"linkedin_username": username}, deadline_s=180
            )
        except Exception as exc:  # noqa: BLE001 - one bad profile must not fail the run
            ctx.notes.append(f"{username}: {exc}")
            logger.warning("profile %s failed: %s", username, exc)
            continue
        calls += 1
        person = person_from_raw(username, payload)
        hint = ctx.search_hints.get(username)
        if hint:
            person.identity.full_name = hint.name
            person.identity.headline = person.identity.headline or hint.headline
            person.identity.location = person.identity.location or hint.location
        ctx.people.append(person)
        await ctx.cache_store(person, False)
    return StepCost(bridge_calls=calls, summary={"fetched": calls, "failed": len(ctx.notes)})


async def node_score(ctx: RunContext) -> StepCost:
    """Rank against the ICP. Free — no LinkedIn, no provider.

    Keyword overlap for now, and it says so in the rationale. The model-backed
    scorer replaces this; the interface it replaces is already the right one.
    """
    terms = {t.lower() for t in (ctx.icp.titles + ctx.icp.keywords)}
    for person in ctx.people:
        haystack = " ".join(
            filter(None, [person.identity.headline, person.identity.full_name])
        ).lower()
        hits = [t for t in terms if t and t in haystack]
        person.score = round(100 * len(hits) / len(terms), 1) if terms else None
        person.score_rationale = (
            f"matched {len(hits)}/{len(terms)} ICP terms: {', '.join(hits) or 'none'}"
            " (keyword overlap; model scoring not yet enabled)"
        )
    ctx.people.sort(key=lambda p: p.score or 0, reverse=True)
    return StepCost(summary={"scored": len(ctx.people)})


WORKFLOW_NODES: dict[str, list[tuple[str, NodeFn]]] = {
    "sales_sourcing": [
        ("source", node_source),
        ("dedupe", node_dedupe),
        ("profile", node_profile),
        ("score", node_score),
    ],
}

LIBRARY: list[WorkflowRef] = [
    WorkflowRef(
        slug="sales_sourcing",
        name="Sales sourcing",
        pack="sales",
        description=(
            "Find people matching an ICP on LinkedIn, fetch their profiles, cache them, "
            "and rank against the target. Email/phone enrichment is added in Phase 2."
        ),
        target_kind="icp",
        requires_bridge=True,
        requires_providers=[],
    ),
]
