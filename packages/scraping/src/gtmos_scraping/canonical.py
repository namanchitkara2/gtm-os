"""URL canonicalisation, so the same page is not fetched five times.

The cache is the throughput of this product (``docs/bottlenecks.md``), and a cache
keyed on raw URLs has no hit rate: the same team page arrives as
``example.com/team``, ``example.com/team/``, ``www.example.com/team?utm_source=x``
and ``EXAMPLE.com/team#leadership``. Four fetches, one page.

The hard part is knowing when *not* to normalise. Stripping query parameters is a
tempting way to collapse duplicates and it silently breaks half the web:
``?page=2`` is a different page, ``?id=88`` is a different record, and a
canonicaliser that drops them makes the crawler read page 1 forever while
reporting a healthy cache hit rate. So the rule here is a **deny-list of known
tracking parameters**, never an allow-list of known-good ones.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Parameters that identify a *referrer*, not a resource. Removing them is safe
#: because no server varies its content on them. Anything not on this list is
#: preserved, on the principle that an unknown parameter is meaningful until
#: proven otherwise.
TRACKING_PARAMS = frozenset({
    # Google / generic campaign tagging
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    # Click identifiers
    "gclid", "gclsrc", "dclid", "wbraid", "gbraid",   # Google Ads
    "fbclid",                                         # Meta
    "msclkid",                                        # Microsoft
    "twclid",                                         # X
    "ttclid",                                         # TikTok
    "li_fat_id",                                      # LinkedIn
    "igshid",                                         # Instagram
    "yclid",                                          # Yandex
    "irclickid",                                      # Impact
    # Email and marketing platforms
    "mc_cid", "mc_eid",                               # Mailchimp
    "_hsenc", "_hsmi", "hsCtaTracking",               # HubSpot
    "vero_conv", "vero_id",
    "mkt_tok",                                        # Marketo
    "elqTrackId", "elqTrack",                         # Eloqua
    "sc_cid",
    # Misc analytics
    "ref", "referrer", "source", "_ga", "_gl", "s_kwcid", "trk", "trkCampaign",
    "spm", "scm",
})

#: Default ports, removed because ``https://x.com:443/`` and ``https://x.com/``
#: are the same origin and must share a cache key.
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Index filenames that are equivalent to the directory itself.
_INDEX_FILES = re.compile(r"/(?:index|default)\.(?:html?|php|aspx?|jsp)$", re.IGNORECASE)


def canonical_url(url: str, *, keep_fragment: bool = False) -> str:
    """The cache key for a URL.

    Deterministic and idempotent: ``canonical_url(canonical_url(u)) ==
    canonical_url(u)`` for every input, which is what lets it be applied at
    several layers without corrupting the key.
    """
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower().rstrip(".")

    # `www.` is a hosting convention, not a distinct site. Stripped only as the
    # leading label, so `www.co.uk` style hosts and `wwwsomething.com` are safe.
    if hostname.startswith("www."):
        hostname = hostname[4:]

    netloc = hostname
    if parts.port and _DEFAULT_PORTS.get(scheme) != parts.port:
        netloc = f"{hostname}:{parts.port}"

    path = parts.path or "/"
    path = _INDEX_FILES.sub("/", path)
    # Collapse duplicate slashes, which routers treat as equivalent.
    path = re.sub(r"/{2,}", "/", path)
    # A trailing slash on a non-root path is the most common duplicate of all.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # Query parameters: drop trackers, keep everything else, sort for stability.
    # Sorting is safe for every server that parses a query string as a mapping,
    # which is all of them in practice, and it is what makes two orderings of the
    # same parameters share a cache entry.
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(kept), doseq=True)

    # Fragments are never sent to a server, so they cannot change the response.
    fragment = parts.fragment if keep_fragment else ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def url_hash(url: str) -> str:
    """A short, stable digest of a canonical URL, for indexing and log lines."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:32]


def content_hash(body: bytes | str) -> str:
    """Digest of fetched content, for change detection.

    The point of this is *not* deduplication of storage — it is avoiding
    re-extraction. Extraction is the expensive half (it can involve an LLM call),
    so a page that returns 200 with a byte-identical body should short-circuit
    before the extractor, not after it.
    """
    data = body.encode("utf-8") if isinstance(body, str) else body
    return hashlib.sha256(data).hexdigest()


def same_document(a: str, b: str) -> bool:
    """Whether two URLs are the same document ignoring fragment and trackers."""
    return canonical_url(a) == canonical_url(b)


def registrable_domain(url: str) -> str:
    """The host used for domain-level throttling.

    This is the *effective* host, not a public-suffix-list registrable domain: no
    PSL is bundled, so ``a.example.co.uk`` throttles separately from
    ``b.example.co.uk``. That is the conservative direction — it produces more
    buckets and therefore lower request rates per bucket, never higher — and it is
    called out here so nobody later assumes PSL semantics it does not have.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host
