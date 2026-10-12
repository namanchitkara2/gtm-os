"""Turning fetched bytes into claims that carry their source.

Fetching and extraction are separate stages on purpose. They fail for unrelated
reasons, they scale differently (one is network-bound, the other CPU-bound), and
keeping them apart is what lets a cached page be re-extracted by a newer extractor
without being re-fetched — which matters because the extractor changes far more
often than the web does.

Every extractor returns :class:`Extracted`, which pairs values with the evidence
for them. There is no path that returns a bare value: a field without a source
cannot be audited, cannot be deduped by trust when two sources disagree, and cannot
be defended when a customer says it was wrong. The platform's existing
``gtmos_schemas.Evidence`` is reused rather than a second provenance system being
invented alongside it.

Deterministic extraction runs first and always. The LLM tier — which this module
defines an interface for but does not call — is allowed to fill only what the
deterministic pass could not, and its output goes through
``untrusted.validate_extraction`` before it is believed. An LLM that reads a hostile
page must not be able to write a field at full confidence.

No HTML parser is vendored. ``html.parser`` from the standard library does the
work, because the alternative is a dependency that changes the security surface
(lxml parses entities and can be pointed at external DTDs) for a gain this product
does not need yet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from gtmos_schemas import Evidence

from .canonical import canonical_url, content_hash
from .untrusted import InjectionScan, scan_for_injection

#: Bumped when a change here could produce a different value from the same bytes.
#: Stored with each extraction so a cached page can be re-extracted when the
#: extractor improves, and so a changed value can be attributed to us rather than
#: to the company.
EXTRACTOR_VERSION = "2026.09.1"

#: Tags whose contents are never page content. ``script`` and ``style`` are the
#: obvious ones; ``template`` and ``noscript`` are the ones that get missed and
#: then show up as garbage text in an extracted company description.
_INVISIBLE_TAGS = frozenset({"script", "style", "template", "noscript", "svg", "head"})

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b"
)
#: Deliberately conservative. A loose phone pattern matches dates, prices, order
#: numbers and street addresses, and a wrong phone number in a GTM tool gets
#: dialled.
_PHONE_RE = re.compile(
    r"(?:(?<=\s)|^|(?<=[:>(]))"
    r"(\+\d{1,3}[\s.-]?)?(\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}"
    r"(?=\s|$|[<,.)])"
)
_HEADCOUNT_RE = re.compile(
    r"(\d[\d,]{1,8})\+?\s*(?:employees|people|team members|staff)\b", re.IGNORECASE
)

#: Addresses that are never a real contact. Scraping these into a CRM is worse than
#: finding nothing, because a bounced send costs sender reputation.
_JUNK_EMAIL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "mailer-daemon", "example", "your.email", "youremail", "email@",
    "user@", "name@", "test@", "sentry", "wordpress@",
)
_JUNK_EMAIL_DOMAINS = (
    "example.com", "example.org", "domain.com", "yourdomain.com", "email.com",
    "sentry.io", "wixpress.com", "squarespace.com",
)


@dataclass
class Extracted:
    """Values and the evidence for them. Never one without the other."""

    values: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    extractor: str = "unknown"
    extractor_version: str = EXTRACTOR_VERSION
    notes: list[str] = field(default_factory=list)
    scan: InjectionScan | None = None

    def add(
        self,
        name: str,
        value: Any,
        *,
        source_url: str,
        confidence: float,
        note: str | None = None,
    ) -> None:
        """Record a value with its provenance, capped by the page's trust level.

        The cap is the mechanism that makes a hostile page harmless: a value read
        off a page that tried to issue instructions is still recorded — suppressing
        it entirely would let an attacker hide a company's real data — but it cannot
        claim the confidence needed for auto-approval.
        """
        if value is None or value == "" or value == []:
            return
        ceiling = self.scan.confidence_ceiling if self.scan else 1.0
        effective = min(confidence, ceiling)
        if self.scan and self.scan.hostile:
            note = (note or "") + " [source page contains instruction-like text]"
        self.values[name] = value
        self.evidence[name] = Evidence(
            provider="web",
            method="scrape",
            source_url=source_url,
            confidence=effective,
            note=(note or None),
        )

    def to_payload(self, *, url: str, body: bytes | str, source_type: str) -> dict[str, Any]:
        """The evidence envelope the request asks for, answering "where did this come from".

        ``content_hash`` is what makes the answer verifiable rather than asserted:
        the same URL re-fetched later can be compared byte-for-byte against what was
        read when the claim was made.
        """
        return {
            **self.values,
            "evidence": {
                "url": canonical_url(url),
                "source_type": source_type,
                "retrieved_at": (
                    next(iter(self.evidence.values())).retrieved_at.isoformat()
                    if self.evidence else None
                ),
                "content_hash": content_hash(body),
                "extractor": self.extractor,
                "extractor_version": self.extractor_version,
                "per_field": {
                    name: {
                        "confidence": ev.confidence,
                        "source_url": ev.source_url,
                        "note": ev.note,
                    }
                    for name, ev in self.evidence.items()
                },
                "hostile_source": bool(self.scan and self.scan.hostile),
                "injection_signals": list(self.scan.signals) if self.scan else [],
            },
        }


class _Reader(HTMLParser):
    """One pass over the document, collecting everything the extractors need.

    A single pass rather than one per extractor: parsing is the expensive part of
    extraction, and running it five times to answer five questions is how a
    CPU-bound stage becomes the bottleneck.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.title: str | None = None
        self.meta: dict[str, str] = {}
        self.jsonld: list[Any] = []
        self.links: list[tuple[str, str]] = []
        self.canonical_href: str | None = None
        self.tables: list[list[list[str]]] = []
        self._stack: list[str] = []
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self._link_text: list[str] = []
        self._in_link = False
        self._current_href: str | None = None
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._stack.append(tag)
        a = {k.lower(): (v or "") for k, v in attrs}

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("property") or a.get("name") or a.get("itemprop")
            if key and a.get("content"):
                self.meta[key.lower()] = a["content"]
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self.canonical_href = a.get("href")
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_buf = []
        elif tag == "a" and a.get("href"):
            self._in_link = True
            self._current_href = a["href"]
            self._link_text = []
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw = "".join(self._jsonld_buf).strip()
            if raw:
                try:
                    self.jsonld.append(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    # A malformed block is the site's bug, not ours. Recorded as
                    # absent rather than raised: one bad script tag must not fail
                    # the extraction of an otherwise good page.
                    pass
        elif tag == "a" and self._in_link:
            self._in_link = False
            text = " ".join("".join(self._link_text).split())
            if self._current_href:
                self.links.append((self._current_href, text))
            self._current_href = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(c for c in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

        while self._stack and self._stack[-1] != tag:
            self._stack.pop()
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._in_title and self.title is None:
            self.title = " ".join(data.split()) or None
        if any(t in _INVISIBLE_TAGS for t in self._stack):
            return
        if self._cell is not None:
            self._cell.append(data)
        if self._in_link:
            self._link_text.append(data)
        if data.strip():
            self.text_parts.append(data)

    @property
    def text(self) -> str:
        """Visible text, whitespace-collapsed."""
        return re.sub(r"[ \t]{2,}", " ", "\n".join(
            line for line in (" ".join(p.split()) for p in self.text_parts) if line
        ))


def _read(html: str) -> _Reader:
    reader = _Reader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:  # noqa: BLE001
        # html.parser can raise on sufficiently broken markup. A partial parse is
        # more useful than none, and the alternative is that one malformed page
        # fails a job that had already paid for the fetch.
        pass
    return reader


class HTMLExtractor:
    """Title, description, visible text, links, tables. The generic tier."""

    name = "html_extractor"

    def extract(self, html: str, *, url: str) -> Extracted:
        reader = _read(html)
        scan = scan_for_injection(html)
        out = Extracted(extractor=self.name, scan=scan)

        out.add("title", reader.title, source_url=url, confidence=0.9)
        for key in ("og:description", "description", "twitter:description"):
            if (value := reader.meta.get(key)):
                out.add("description", value, source_url=url, confidence=0.8,
                        note=f"meta[{key}]")
                break
        for key in ("og:site_name", "application-name"):
            if (value := reader.meta.get(key)):
                out.add("site_name", value, source_url=url, confidence=0.85,
                        note=f"meta[{key}]")
                break

        if reader.canonical_href:
            out.add("canonical_url", urljoin(url, reader.canonical_href),
                    source_url=url, confidence=1.0, note="rel=canonical")

        text = reader.text
        if text:
            out.add("text", text[:100_000], source_url=url, confidence=1.0)
            out.add("text_length", len(text), source_url=url, confidence=1.0)

        links = [urljoin(url, href) for href, _ in reader.links]
        if links:
            out.add("links", links[:500], source_url=url, confidence=1.0)
        if reader.tables:
            out.add("tables", reader.tables[:20], source_url=url, confidence=0.9)

        if scan.hostile:
            out.notes.append(
                f"Page contains instruction-like text ({', '.join(scan.signals)}); "
                f"all fields capped at confidence {scan.confidence_ceiling}."
            )
        return out


class JSONLDExtractor:
    """schema.org structured data. The most reliable source on any page.

    Preferred over text parsing wherever it exists because it is the site
    *asserting* a fact in a machine format rather than us inferring one from prose —
    which is the difference between a confidence of 0.95 and 0.6.
    """

    name = "jsonld_extractor"

    #: schema.org types that describe an organisation.
    _ORG_TYPES = frozenset({
        "organization", "corporation", "localbusiness", "onlinebusiness",
        "ngo", "educationalorganization", "governmentorganization",
    })

    def extract(self, html: str, *, url: str) -> Extracted:
        reader = _read(html)
        out = Extracted(extractor=self.name, scan=scan_for_injection(html))

        for block in self._flatten(reader.jsonld):
            if not isinstance(block, dict):
                continue
            types = block.get("@type") or block.get("type") or ""
            types = [types] if isinstance(types, str) else list(types)
            lowered = {str(t).lower() for t in types}

            if lowered & self._ORG_TYPES:
                out.add("company_name", block.get("name"), source_url=url,
                        confidence=0.95, note="schema.org Organization")
                out.add("company_url", block.get("url"), source_url=url, confidence=0.9)
                out.add("company_description", block.get("description"),
                        source_url=url, confidence=0.9)
                out.add("logo", self._as_url(block.get("logo")), source_url=url,
                        confidence=0.85)
                if (count := block.get("numberOfEmployees")) is not None:
                    parsed = self._employee_count(count)
                    out.add("employee_count", parsed, source_url=url, confidence=0.9,
                            note="schema.org numberOfEmployees")
                if isinstance(block.get("address"), dict):
                    addr = block["address"]
                    out.add("address_country", addr.get("addressCountry"),
                            source_url=url, confidence=0.9)
                    out.add("address_locality", addr.get("addressLocality"),
                            source_url=url, confidence=0.9)
                for key, name in (("email", "company_email"), ("telephone", "company_phone")):
                    if (value := block.get(key)):
                        out.add(name, value, source_url=url, confidence=0.9)

            if "jobposting" in lowered:
                out.add("job_title", block.get("title"), source_url=url, confidence=0.95)
                out.add("job_posted_at", block.get("datePosted"), source_url=url,
                        confidence=0.95)
                org = block.get("hiringOrganization")
                if isinstance(org, dict):
                    out.add("hiring_company", org.get("name"), source_url=url,
                            confidence=0.95)

            if "person" in lowered:
                out.add("person_name", block.get("name"), source_url=url, confidence=0.9)
                out.add("person_title", block.get("jobTitle"), source_url=url,
                        confidence=0.9)
        return out

    def _flatten(self, blocks: Any) -> list[Any]:
        """schema.org nests via @graph and bare lists. Flatten both, bounded.

        The depth bound is a safety limit: a document can declare a deeply nested
        or self-referential graph, and an unbounded walk over attacker-supplied
        JSON is a denial-of-service with no exploit required.
        """
        out: list[Any] = []
        stack = list(blocks) if isinstance(blocks, list) else [blocks]
        seen = 0
        while stack and seen < 500:
            item = stack.pop()
            seen += 1
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                out.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
        return out

    @staticmethod
    def _as_url(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("url") or value.get("contentUrl")
        return None

    @staticmethod
    def _employee_count(value: Any) -> int | None:
        """numberOfEmployees is a string, an int, or a QuantitativeValue."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            match = re.search(r"\d[\d,]*", value)
            return int(match.group(0).replace(",", "")) if match else None
        if isinstance(value, dict):
            for key in ("value", "minValue", "maxValue"):
                if (inner := value.get(key)) is not None:
                    return JSONLDExtractor._employee_count(inner)
        return None


class CompanyExtractor:
    """Company facts, deterministic tier: JSON-LD first, then text patterns.

    The ordering is the whole design. A headcount asserted in schema.org is the
    company's own claim; a headcount matched out of prose is our inference, and the
    two must not arrive with the same confidence.
    """

    name = "company_extractor"

    def extract(self, html: str, *, url: str) -> Extracted:
        structured = JSONLDExtractor().extract(html, url=url)
        generic = HTMLExtractor().extract(html, url=url)

        out = Extracted(extractor=self.name, scan=generic.scan)
        out.values.update(structured.values)
        out.evidence.update(structured.evidence)
        out.notes.extend(structured.notes)

        if "company_name" not in out.values:
            name = generic.values.get("site_name") or generic.values.get("title")
            if isinstance(name, str):
                # A title is "About | Acme Inc" far more often than it is a name.
                cleaned = re.split(r"\s+[|–—-]\s+", name)[-1].strip()
                out.add("company_name", cleaned or name, source_url=url,
                        confidence=0.55, note="inferred from page title")
        if "company_description" not in out.values and generic.values.get("description"):
            out.add("company_description", generic.values["description"],
                    source_url=url, confidence=0.7, note="meta description")

        text = generic.values.get("text") or ""
        if "employee_count" not in out.values and isinstance(text, str):
            if (match := _HEADCOUNT_RE.search(text)) is not None:
                out.add("employee_count", int(match.group(1).replace(",", "")),
                        source_url=url, confidence=0.6,
                        note=f"inferred from text: {match.group(0)!r}")

        emails = self._emails(text)
        if emails:
            out.add("emails", emails, source_url=url, confidence=0.65,
                    note="published on the page")
        out.notes.extend(generic.notes)
        return out

    @staticmethod
    def _emails(text: str) -> list[str]:
        """Published addresses, minus the ones that are never a contact."""
        found: list[str] = []
        for raw in _EMAIL_RE.findall(text or ""):
            addr = raw.lower()
            if any(addr.startswith(p) for p in _JUNK_EMAIL_PREFIXES):
                continue
            if any(addr.endswith("@" + d) or addr.endswith("." + d) for d in _JUNK_EMAIL_DOMAINS):
                continue
            if addr not in found:
                found.append(addr)
        return found[:50]


class PersonExtractor:
    """People named on a team or leadership page.

    Pairing on the page's own structure rather than a language model is enough for
    the common shape (a name element followed by a role element) and costs nothing.
    Where it finds nothing, the LLM tier is the right escalation — it is not a
    substitute for trying this first.
    """

    name = "person_extractor"

    #: Role words that make an adjacent capitalised phrase a person's title rather
    #: than a heading.
    _ROLE_HINT = re.compile(
        r"\b(?:ceo|cto|cfo|cmo|coo|cpo|cro|chief|founder|co-founder|president|"
        r"vp|vice president|director|head of|lead|manager|partner|principal)\b",
        re.IGNORECASE,
    )
    _NAME_RE = re.compile(r"^[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’.-]+){1,3}$")

    def extract(self, html: str, *, url: str) -> Extracted:
        reader = _read(html)
        scan = scan_for_injection(html)
        out = Extracted(extractor=self.name, scan=scan)

        structured = JSONLDExtractor().extract(html, url=url)
        people: list[dict[str, str]] = []
        if structured.values.get("person_name"):
            people.append({
                "name": structured.values["person_name"],
                "title": structured.values.get("person_title", ""),
                "source": "schema.org Person",
            })

        lines = [line for line in reader.text.split("\n") if line.strip()]
        for index, line in enumerate(lines):
            if not self._NAME_RE.match(line.strip()):
                continue
            for candidate in lines[index + 1: index + 3]:
                if self._ROLE_HINT.search(candidate) and len(candidate) < 120:
                    entry = {"name": line.strip(), "title": candidate.strip(),
                             "source": "adjacent text on page"}
                    if entry not in people:
                        people.append(entry)
                    break

        if people:
            out.add("people", people[:100], source_url=url,
                    confidence=0.6 if len(people) > 1 else 0.55,
                    note="name/role pairs read from page structure")
        else:
            out.notes.append(
                "No name/role pairs found deterministically. This is the case where "
                "the LLM tier is worth its cost."
            )
        return out


#: The registry. A new extractor is added here and nowhere else — the orchestrator
#: selects by name, so no call site holds a vendor- or site-specific branch.
EXTRACTORS: dict[str, Any] = {
    HTMLExtractor.name: HTMLExtractor(),
    JSONLDExtractor.name: JSONLDExtractor(),
    CompanyExtractor.name: CompanyExtractor(),
    PersonExtractor.name: PersonExtractor(),
}


def needs_browser(html: str, *, min_text: int = 250) -> tuple[bool, str]:
    """Whether an HTTP fetch produced enough content, or a browser is required.

    This is the decision that keeps the browser pool small. Calling it on every
    HTTP response and escalating only on a real shortfall is the difference between
    a worker handling hundreds of pages a minute and a handful.
    """
    if not html or not html.strip():
        return True, "empty response body"
    reader = _read(html)
    text_len = len(reader.text.strip())
    if text_len >= min_text:
        return False, f"{text_len} characters of text extracted over HTTP"

    lowered = html.lower()
    for marker, why in (
        ('id="root"', "empty SPA mount point"),
        ('id="app"', "empty SPA mount point"),
        ("__next_data__", "Next.js client-rendered page"),
        ("window.__nuxt__", "Nuxt client-rendered page"),
        ("ng-app", "Angular client-rendered page"),
        ("data-reactroot", "React client-rendered page"),
    ):
        if marker in lowered:
            return True, f"{why}, only {text_len} characters of text"
    if text_len < min_text:
        return True, f"only {text_len} characters of text (below {min_text})"
    return False, "sufficient content"
