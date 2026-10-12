"""Reading a free-text brief into a structured, executable ICP.

This module exists because of a specific production failure. ``define_target``
used to build its ``ICP`` purely from optional keyword arguments supplied by the
*calling* agent, so a client that passed only ``brief=`` produced
``ICP()`` — every list empty, every bound ``None`` — which serialised to
``interpreted_as: {}``. Nothing rejected it. ``start_run`` loaded that empty spec,
the source node fell back to a stale cached keyword, and the run spent six
LinkedIn actions out of a ~100/day budget returning people with no relationship
to what was asked for.

Two lessons are encoded here as code rather than as advice:

1. **Interpretation is the server's job.** Delegating it to the client's model
   means the product's most important transformation has no schema, no version,
   no tests, and no consistency between two callers wording the same ask
   differently. The client may still *offer* structure — it often reads a brief
   better than a regex does — but the server parses independently and merges,
   so a silent client produces a parse rather than an empty spec.

2. **An empty interpretation must never execute.** Spending a scarce, rate-
   limited, per-identity budget on a guess is the most expensive possible
   failure mode, because the cost is paid in the one resource that cannot be
   bought back (see ``docs/bottlenecks.md``). So :meth:`BriefInterpretation.
   executable` is a gate, not a hint, and the refusal names what is missing.

The parser is deterministic on purpose. An LLM pass belongs *above* this (it can
propose structure, which arrives as the caller's hints), but the floor has to be
reproducible: the same brief must yield the same spec on every run, or a cached
target silently changes meaning between executions and nobody can tell why a
weekly run's results moved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gtmos_schemas import ICP

#: Bumped whenever a change here could alter the spec produced for an existing
#: brief. Stored alongside a parsed target so a result set can be attributed to
#: the parser that produced it, and so a reparse can be detected as a change in
#: interpretation rather than a change in the market.
PARSER_VERSION = "2026.09.1"


# --------------------------------------------------------------- vocabulary --

#: Seniority tokens, mapped to the words LinkedIn headlines actually use. The
#: value is a tuple because a search for "VP" must also match "Vice President":
#: matching literally on the user's abbreviation finds a fraction of the people
#: who hold the role.
_SENIORITY: dict[str, tuple[str, ...]] = {
    "founder": ("founder", "co-founder", "cofounder"),
    "c-level": ("chief", "ceo", "cto", "cfo", "cmo", "coo", "cpo", "cro"),
    "ceo": ("ceo", "chief executive"),
    "cto": ("cto", "chief technology"),
    "cmo": ("cmo", "chief marketing"),
    "cfo": ("cfo", "chief financial"),
    "cro": ("cro", "chief revenue"),
    "vp": ("vp", "vice president"),
    "svp": ("svp", "senior vice president"),
    "avp": ("avp", "associate vice president"),
    "director": ("director",),
    "head": ("head of", "head"),
    "lead": ("lead",),
    "principal": ("principal",),
    "senior": ("senior", "sr"),
    "manager": ("manager",),
}

#: Functions a GTM user actually targets. Kept explicit rather than inferred:
#: a lexicon that guesses produces titles nobody asked for, and a title is the
#: highest-weighted dimension in ``methodology.ICP_FIT_WEIGHTS``.
#:
#: "people" is deliberately absent even though "Head of People" is a real title.
#: As a bare function it matches the English word in "find me some good people",
#: which turned an unreadable brief into ``titles=['People']`` — executable,
#: meaningless, and exactly the class of false confidence this module exists to
#: prevent. It is recovered below only in the qualified forms.
_FUNCTIONS: tuple[str, ...] = (
    "marketing", "growth", "sales", "revenue", "demand generation", "demand gen",
    "product", "engineering", "data", "analytics", "operations", "revops",
    "rev ops", "partnerships", "business development", "bizdev", "customer success",
    "talent", "recruiting", "hr", "finance", "design", "security",
    "martech", "lifecycle", "crm", "brand", "content", "seo", "performance marketing",
    "ecommerce", "e-commerce", "supply chain", "category", "merchandising",
    "people operations", "people ops",
)

#: Adjectival and demonymic forms of places. "Indian fintech startups" names a
#: geography as clearly as "in India" does, and a parser that only matches the
#: noun drops the single most valuable filter in the product — LinkedIn's own
#: geography facet does not work through search (see ``workflows.location_matches``),
#: so if we do not read the place, nothing downstream enforces it either.
_PLACE_ADJECTIVES: dict[str, str] = {
    "indian": "India", "american": "United States", "british": "United Kingdom",
    "emirati": "UAE", "german": "Germany", "french": "France", "dutch": "Netherlands",
    "singaporean": "Singapore", "australian": "Australia", "canadian": "Canada",
    "european": "Europe", "japanese": "Japan", "chinese": "China",
    "brazilian": "Brazil", "mexican": "Mexico", "israeli": "Israel",
    "spanish": "Spain", "italian": "Italy", "swedish": "Sweden",
    "nordic": "Nordics", "asian": "APAC", "gulf": "MENA",
}

#: Funding stages, normalised to the spelling the ICP field expects.
_FUNDING: dict[str, str] = {
    "pre-seed": "Pre-Seed", "preseed": "Pre-Seed", "pre seed": "Pre-Seed",
    "seed": "Seed",
    "series a": "Series A", "series-a": "Series A",
    "series b": "Series B", "series-b": "Series B",
    "series c": "Series C", "series-c": "Series C",
    "series d": "Series D", "series-d": "Series D",
    "series e": "Series E",
    "growth stage": "Growth", "late stage": "Late Stage",
    "bootstrapped": "Bootstrapped", "public": "Public", "pre-ipo": "Pre-IPO",
}

#: Industry words worth matching. Short, because an industry the parser invents
#: filters out good candidates silently.
_INDUSTRIES: dict[str, str] = {
    "saas": "SaaS", "software": "Software", "fintech": "Fintech",
    "healthtech": "HealthTech", "health tech": "HealthTech",
    "edtech": "EdTech", "insurtech": "InsurTech", "proptech": "PropTech",
    "b2b": "B2B", "b2c": "B2C", "d2c": "D2C", "dtc": "D2C",
    "ecommerce": "E-commerce", "e-commerce": "E-commerce",
    "marketplace": "Marketplace", "retail": "Retail", "banking": "Banking",
    "insurance": "Insurance", "logistics": "Logistics", "gaming": "Gaming",
    "cybersecurity": "Cybersecurity", "ai": "AI", "martech": "MarTech",
    "manufacturing": "Manufacturing", "telecom": "Telecom",
    "consulting": "Consulting", "agency": "Agency", "staffing": "Staffing",
}

#: Place spellings that differ between how a user asks and how LinkedIn renders.
#: This is the same table ``workflows.location_matches`` applies at match time;
#: it lives here too because the *parser* must normalise the ICP itself — a spec
#: holding "Bangalore" and a matcher expanding to "Bengaluru" only agree by
#: coincidence, and the spec is the thing that gets cached and re-run.
_PLACE_CANON: dict[str, str] = {
    "bangalore": "Bangalore", "bengaluru": "Bangalore",
    "bombay": "Mumbai", "mumbai": "Mumbai",
    "delhi": "Delhi", "new delhi": "Delhi", "ncr": "Delhi",
    "gurgaon": "Gurugram", "gurugram": "Gurugram", "noida": "Noida",
    "calcutta": "Kolkata", "kolkata": "Kolkata",
    "madras": "Chennai", "chennai": "Chennai",
    "hyderabad": "Hyderabad", "pune": "Pune", "ahmedabad": "Ahmedabad",
    "india": "India",
    "uae": "UAE", "united arab emirates": "UAE", "dubai": "Dubai",
    "abu dhabi": "Abu Dhabi",
    "usa": "United States", "us": "United States", "u.s.": "United States",
    "united states": "United States", "america": "United States",
    "uk": "United Kingdom", "united kingdom": "United Kingdom",
    "london": "London", "singapore": "Singapore", "canada": "Canada",
    "australia": "Australia", "germany": "Germany", "berlin": "Berlin",
    "france": "France", "netherlands": "Netherlands", "amsterdam": "Amsterdam",
    "europe": "Europe", "emea": "EMEA", "apac": "APAC", "mena": "MENA",
    "sf": "San Francisco", "san francisco": "San Francisco",
    "bay area": "San Francisco", "new york": "New York", "nyc": "New York",
    "remote": "Remote",
}

#: Words that look like a company name but are not. Without this, "Find VPs at
#: Series B companies" reads "Series B companies" as a named account and
#: restricts the search to a company that does not exist.
_NOT_A_COMPANY = {
    "series", "companies", "company", "startups", "startup", "firms", "orgs",
    "organisations", "organizations", "businesses", "scale-ups", "scaleups",
    "brands", "teams", "least", "most", "employees", "people",
}


# ------------------------------------------------------------------ results --

@dataclass
class BriefInterpretation:
    """What the parser made of a brief, and whether it is safe to execute.

    ``confidence`` is not decoration. It gates execution: a brief the parser
    barely understood must produce a clarifying question, not a run, because the
    budget spent on a bad interpretation is unrecoverable.
    """

    icp: ICP
    confidence: float
    notes: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    parser_version: str = PARSER_VERSION
    #: Which fields the parser filled, and from where. Kept so the dashboard can
    #: show the user *why* a run searched what it searched.
    provenance: dict[str, str] = field(default_factory=dict)

    #: Criteria that actually narrow a search. Headcount and funding stage are
    #: deliberately excluded: LinkedIn people-search cannot filter on either, so
    #: a spec carrying only "100-500 employees" would pass a naive emptiness
    #: check and then search for everyone on the platform.
    DISCRIMINATING = ("titles", "keywords", "named_accounts", "industries")

    #: Below this, execution is refused and the user is asked to confirm. Set at
    #: the level where the parser found *something* but not enough to be worth a
    #: LinkedIn budget — measured against the regression corpus in
    #: ``tests/test_brief_parser.py``, not chosen for feel.
    MIN_CONFIDENCE = 0.35

    @property
    def discriminating_fields(self) -> list[str]:
        return [f for f in self.DISCRIMINATING if getattr(self.icp, f)]

    @property
    def executable(self) -> bool:
        return bool(self.discriminating_fields) and self.confidence >= self.MIN_CONFIDENCE

    def refusal(self) -> dict[str, Any] | None:
        """Why this must not start a run, phrased for the calling agent.

        Returns ``None`` when the interpretation is executable. The message is
        written to be actionable by a model: it says which dimension is missing
        and which argument supplies it, because "could not parse brief" sends a
        client into a retry loop with the same words.
        """
        if self.executable:
            return None
        if not self.discriminating_fields:
            return {
                "error": "brief_not_understood",
                "why": (
                    "No searchable criteria could be read from this brief. Running it "
                    "would spend LinkedIn actions on an unfiltered search."
                ),
                "brief_was": None,  # filled by the caller, which has the text
                "needed": "At least one of: job titles, target companies, industries, or keywords.",
                "how_to_fix": [
                    "Re-call define_target with your own structured reading, e.g. "
                    "titles=['VP Marketing'], locations=['United States'].",
                    "Or restate the brief naming the roles and/or companies to find.",
                ],
                "parsed_so_far": self.icp.model_dump(exclude_defaults=True),
                "ambiguities": self.ambiguities,
            }
        return {
            "error": "brief_confidence_too_low",
            "why": (
                f"Interpretation confidence {self.confidence:.2f} is below the "
                f"{self.MIN_CONFIDENCE} floor required to spend LinkedIn actions."
            ),
            "parsed_so_far": self.icp.model_dump(exclude_defaults=True),
            "ambiguities": self.ambiguities,
            "how_to_fix": [
                "Confirm or correct the reading above by passing it explicitly to "
                "define_target.",
            ],
        }


# ------------------------------------------------------------------ parsing --

_HEADCOUNT_RANGE = re.compile(
    r"(\d[\d,]*)\s*(?:-|–|—|to|and)\s*(\d[\d,]*)\+?\s*(?:employees|people|staff|headcount|fte)",
    re.IGNORECASE,
)
_HEADCOUNT_MIN = re.compile(
    r"(?:over|more than|at least|>=?|above|\bmin(?:imum)?\b)\s*(\d[\d,]*)\+?\s*"
    r"(?:employees|people|staff|headcount|fte)",
    re.IGNORECASE,
)
_HEADCOUNT_MAX = re.compile(
    r"(?:under|less than|fewer than|below|<=?|\bmax(?:imum)?\b|up to)\s*(\d[\d,]*)\s*"
    r"(?:employees|people|staff|headcount|fte)",
    re.IGNORECASE,
)
_HEADCOUNT_PLUS = re.compile(
    r"(\d[\d,]*)\+\s*(?:employees?|people|staff|headcount|fte)", re.IGNORECASE
)

#: "Series B/C" and "Series A-B" name two stages in one token. Without this the
#: brief in the product spec parses only one of them and silently narrows the ask.
_FUNDING_COMPOUND = re.compile(
    r"\bseries\s+([a-e])\s*(?:/|-|–|or|and|,)\s*([a-e])\b", re.IGNORECASE
)

#: "at Stripe", "from Google", "working at Acme Corp". Capitalisation is required
#: because lowercase words after "at" are almost never company names, and the
#: false positives ("at least 100 employees") are what ruined the first version.
_AT_COMPANY = re.compile(
    r"\b(?:at|from|within|inside|for)\s+"
    r"((?:[A-Z][\w&.\-]*)(?:\s+(?:[A-Z][\w&.\-]*|of|and|&)){0,3})"
)

#: Explicit exclusions: "not recruiters", "excluding agencies", "no consultants".
_EXCLUDE = re.compile(
    r"\b(?:not|no|excluding|exclude|except|without|avoid)\s+([a-z][\w\s/&-]{2,40})",
    re.IGNORECASE,
)


def _term_re(term: str) -> str:
    r"""A word-boundary pattern for ``term`` that tolerates plurals.

    Users write "CTOs", "founders", "Heads of Growth" and "Directors of RevOps".
    Matching the singular literally finds none of them, which is how five
    perfectly ordinary briefs came back unexecutable on the first run of this
    parser's own test suite. Optional ``s`` is allowed on the first and last
    word — enough for "heads of" and "vice presidents", and narrow enough not to
    invent matches.
    """
    words = term.split()
    if len(words) == 1:
        return rf"\b{re.escape(words[0])}s?\b"
    first, *middle, last = words
    parts = [rf"\b{re.escape(first)}s?"] + [re.escape(w) for w in middle] + [rf"{re.escape(last)}s?\b"]
    return r"\s+".join(parts)


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving dedupe, case-insensitive. Order is the user's emphasis."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        key = v.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(v.strip())
    return out


def _parse_int(raw: str) -> int:
    return int(raw.replace(",", ""))


def _extract_titles(text: str, low: str) -> tuple[list[str], list[str]]:
    """Titles, built from seniority × function actually present in the brief.

    Composing rather than pattern-matching whole titles is what lets one lexicon
    cover "VP Marketing", "Head of Growth" and "Director of RevOps" without
    enumerating them. The seniority word alone is kept when no function is named
    nearby, because "find me founders" is a complete, searchable ask.
    """
    notes: list[str] = []
    titles: list[str] = []

    seniorities = [
        canon for canon, variants in _SENIORITY.items()
        if any(re.search(_term_re(v), low) for v in variants)
    ]
    functions = [f for f in _FUNCTIONS if re.search(_term_re(f), low)]

    # A phrase like "VP Marketing" or "Head of Growth" verbatim in the brief is
    # a better title than anything composition produces, so look for it first.
    for sen in seniorities:
        for variant in _SENIORITY[sen]:
            pattern = _term_re(variant) + r"(?:\s+of)?\s+([A-Za-z][\w&/ -]{2,30}?)\b"
            for match in re.finditer(pattern, text, re.IGNORECASE):
                tail = match.group(1).strip()
                if tail.lower() in _FUNCTIONS or any(
                    tail.lower().startswith(f) for f in _FUNCTIONS
                ):
                    titles.append(f"{match.group(0).strip()}")

    if not titles:
        for sen in seniorities:
            if functions:
                titles.extend(f"{sen.upper() if len(sen) <= 3 else sen.title()} {f.title()}"
                              for f in functions)
            else:
                titles.append(sen.title())
        if seniorities and not functions:
            notes.append(
                "Seniority was named but not a function — searching on seniority alone, "
                "which is broad."
            )

    if not titles and functions:
        titles.extend(f.title() for f in functions)
        notes.append("A function was named but not a seniority level.")

    return _dedupe(titles), notes


def _extract_locations(low: str) -> list[str]:
    """Canonical place names, longest match first so "new york" beats "new"."""
    found: list[str] = []
    for table in (_PLACE_CANON, _PLACE_ADJECTIVES):
        for raw in sorted(table, key=len, reverse=True):
            if re.search(rf"\b{re.escape(raw)}\b", low):
                canon = table[raw]
                if canon not in found:
                    found.append(canon)
    return found


def _extract_headcount(text: str) -> tuple[int | None, int | None, list[str]]:
    notes: list[str] = []
    lo = hi = None
    if (m := _HEADCOUNT_RANGE.search(text)) is not None:
        lo, hi = _parse_int(m.group(1)), _parse_int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
            notes.append("Headcount range was given high-to-low; read as low-to-high.")
    else:
        if (m := _HEADCOUNT_PLUS.search(text)) is not None:
            lo = _parse_int(m.group(1))
        if (m := _HEADCOUNT_MIN.search(text)) is not None:
            lo = _parse_int(m.group(1))
        if (m := _HEADCOUNT_MAX.search(text)) is not None:
            hi = _parse_int(m.group(1))
    return lo, hi, notes


def _extract_named_accounts(text: str) -> tuple[list[str], list[str]]:
    """Company names, with the "at least 100 employees" trap guarded explicitly."""
    ambiguities: list[str] = []
    out: list[str] = []
    for match in _AT_COMPANY.finditer(text):
        candidate = match.group(1).strip().rstrip(".,;")
        head = candidate.split()[0].lower() if candidate.split() else ""
        if head in _NOT_A_COMPANY or candidate.lower() in _NOT_A_COMPANY:
            continue
        # Trailing generic noun: "Acme Corp companies" -> drop the noun.
        words = [w for w in candidate.split() if w.lower() not in _NOT_A_COMPANY]
        if not words:
            continue
        cleaned = " ".join(words)
        # A single very common word after "at" is more likely prose than a company.
        if len(cleaned) < 2:
            continue
        out.append(cleaned)
    deduped = _dedupe(out)
    if len(deduped) > 5:
        ambiguities.append(
            f"{len(deduped)} possible company names were read from the brief "
            f"({', '.join(deduped[:5])}...). Confirm which are real targets."
        )
    return deduped, ambiguities


def parse_brief(brief: str, *, hints: dict[str, Any] | None = None) -> BriefInterpretation:
    """Read a brief into an ICP. Never raises; returns a non-executable result instead.

    ``hints`` is the calling agent's own structured reading. It is *merged over*
    the deterministic parse rather than replacing it, in both directions: the
    client usually reads intent better, but a client that passes nothing must
    still get a real spec, and a client that passes only ``titles`` should not
    lose the location the brief clearly stated.
    """
    text = (brief or "").strip()
    low = text.lower()
    notes: list[str] = []
    ambiguities: list[str] = []
    provenance: dict[str, str] = {}

    if not text:
        return BriefInterpretation(
            icp=ICP(), confidence=0.0,
            notes=["Brief was empty."],
            ambiguities=["No brief text was supplied."],
        )

    titles, title_notes = _extract_titles(text, low)
    notes.extend(title_notes)
    locations = _extract_locations(low)
    lo, hi, hc_notes = _extract_headcount(text)
    notes.extend(hc_notes)
    named, acct_ambig = _extract_named_accounts(text)
    ambiguities.extend(acct_ambig)

    funding = [
        canon for raw, canon in _FUNDING.items()
        if re.search(rf"\b{re.escape(raw)}\b", low)
    ]
    for match in _FUNDING_COMPOUND.finditer(low):
        funding.extend(f"Series {g.upper()}" for g in match.groups())
    funding = _dedupe(funding)
    industries = _dedupe([
        canon for raw, canon in _INDUSTRIES.items()
        if re.search(rf"\b{re.escape(raw)}\b", low)
    ])

    excludes: list[str] = []
    for match in _EXCLUDE.finditer(text):
        phrase = match.group(1).strip()
        # Only keep an exclusion that names something we would otherwise match.
        if any(f in phrase.lower() for f in _FUNCTIONS) or phrase.lower() in _INDUSTRIES:
            excludes.append(phrase)

    for name, value in (
        ("titles", titles), ("locations", locations), ("industries", industries),
        ("funding_stages", funding), ("named_accounts", named),
    ):
        if value:
            provenance[name] = "parsed_from_brief"
    if lo is not None or hi is not None:
        provenance["headcount"] = "parsed_from_brief"

    icp = ICP(
        titles=titles,
        exclude_titles=_dedupe(excludes),
        industries=industries,
        locations=locations,
        headcount_min=lo,
        headcount_max=hi,
        funding_stages=funding,
        named_accounts=named,
    )

    # The caller's structured reading wins per-field where it supplied one.
    if hints:
        for key, value in hints.items():
            if value in (None, [], {}) or not hasattr(icp, key):
                continue
            setattr(icp, key, value)
            provenance[key] = "supplied_by_caller"

    confidence = _score(icp, low)

    if icp.named_accounts and not icp.titles:
        ambiguities.append(
            "Companies were named but no role. Sourcing every employee of a company "
            "is rarely the intent and is expensive — name the roles to look for."
        )
    if not icp.locations:
        notes.append("No location was given, so results are not geographically filtered.")

    return BriefInterpretation(
        icp=icp, confidence=confidence, notes=notes,
        ambiguities=ambiguities, provenance=provenance,
    )


#: What each filled dimension contributes to confidence. Titles dominate because
#: a title is what makes a people-search a search rather than a crawl, and
#: because it is the highest-weighted dimension at scoring time too.
_WEIGHTS = {
    "titles": 0.45,
    "named_accounts": 0.20,
    "industries": 0.12,
    "locations": 0.13,
    "funding_stages": 0.05,
    "keywords": 0.05,
}


def _score(icp: ICP, low: str) -> float:
    """How much of the brief the parser believes it actually understood.

    Deliberately not a model's self-report. It is the fraction of weighted
    dimensions filled, with one penalty that encodes the Ourshopee failure: a
    long brief that yielded almost nothing is *less* trustworthy than a short
    one that yielded the same, because length means the user said things the
    parser could not read.
    """
    score = sum(w for field_, w in _WEIGHTS.items() if getattr(icp, field_, None))
    if icp.headcount_min is not None or icp.headcount_max is not None:
        score += 0.03

    words = len(low.split())
    if words > 25 and score < 0.5:
        # A detailed ask the parser mostly missed. Report low, refuse, and let the
        # caller supply structure rather than guessing at scale.
        score *= 0.6
    return round(min(score, 1.0), 3)
