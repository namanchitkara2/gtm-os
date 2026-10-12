"""The gate every outbound fetch passes through, or does not happen.

A scraper is a service that makes HTTP requests to URLs chosen by someone else.
That is the definition of Server-Side Request Forgery, so the protection cannot
be a helper that call sites remember to use — it is the only door.

What this module refuses, and why each one is a real attack rather than a
checklist item:

* **Private and loopback ranges.** ``http://127.0.0.1:5432`` reaches the Postgres
  this process is already authenticated to. ``http://10.0.0.5/admin`` reaches
  whatever else runs in the VPC. Neither needs a vulnerability in the target —
  the scraper's own network position is the vulnerability.

* **Cloud metadata endpoints.** ``169.254.169.254`` returns IAM role credentials
  on EC2 with no authentication at all. A single unguarded fetch of a
  user-supplied URL is a full credential compromise, and it is the single most
  exploited SSRF target in the industry.

* **Non-HTTP schemes.** ``file:///etc/passwd`` and ``gopher://`` are not fetches,
  they are file reads and protocol smuggling.

* **DNS rebinding.** This is the subtle one. Validating the hostname and then
  letting the HTTP client resolve it again invites a DNS entry with a 0-second
  TTL that answers public on the first lookup and ``127.0.0.1`` on the second.
  The defence is to resolve *once*, validate the resolved addresses, and connect
  to the address that was validated — which is why :func:`resolve_and_validate`
  returns the addresses rather than a boolean, and why the fetcher must pin them.

* **Redirects.** A URL that passes validation can return ``302`` to one that
  would not. Every hop is re-validated; a redirect chain is a sequence of new
  requests, not a detail of the first one.

The default posture is deny. An allowlist-only mode exists for tenants that want
it, and the internal-target escape hatch requires an explicit, non-default
configuration because a scraper that can reach internal services on request is a
different product with a different threat model.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

#: Only these reach the network. Everything else — file, ftp, gopher, data,
#: javascript, blob — is a different operation wearing a URL's clothes.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Link-local, where every major cloud parks its instance-credentials endpoint.
#: Listed separately from the private ranges because the consequence is
#: qualitatively different: not "reached something internal" but "leaked the
#: production IAM role".
_METADATA_ADDRESSES = frozenset({
    "169.254.169.254",   # AWS / Azure / GCP / DigitalOcean / Oracle IMDS
    "fd00:ec2::254",     # AWS IMDSv2 over IPv6
    "100.100.100.200",   # Alibaba Cloud
    "192.0.0.192",       # Oracle Cloud legacy
})

#: Hostnames that resolve somewhere useful to an attacker regardless of DNS.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "instance-data.ec2.internal",
})

#: Ports that are never a website. Blocking them turns "scrape this URL" into
#: something that cannot be used as a generic internal port scanner, and cannot
#: be pointed at the platform's own datastores.
_BLOCKED_PORTS = frozenset({
    22, 23, 25, 110, 143, 445, 993, 995,            # shell, mail, SMB
    1433, 1521, 3306, 5432, 6379, 9200, 11211, 27017,  # databases and caches
    2375, 2376,                                      # Docker daemon
    5672, 15672,                                     # RabbitMQ
    8500, 8600,                                      # Consul
    2379, 2380,                                      # etcd
    9000,                                            # common internal admin
})

#: The maximum bytes any response may occupy in memory. A scraper without this
#: is a memory-exhaustion primitive: a 40GB file behind a 200 OK takes the whole
#: worker down, and so does a 10KB gzip that expands to 10GB.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

#: Ratio of decompressed to compressed size that indicates a decompression bomb
#: rather than a well-compressed page. Real HTML compresses around 4-8x; 100x is
#: not a page.
MAX_DECOMPRESSION_RATIO = 100


class UrlRejected(Exception):
    """A URL that must not be fetched.

    Carries a machine-readable ``reason`` because the caller has to distinguish
    "this tenant's policy forbids it" (report to the user) from "this is an SSRF
    attempt" (report, and do not retry with a variation).
    """

    def __init__(self, reason: str, detail: str, url: str = "") -> None:
        self.reason = reason
        self.detail = detail
        self.url = url
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class NetworkPolicy:
    """A tenant's network boundary. Every default is the safe one.

    ``allow_internal_targets`` is the only way to reach a private address, and it
    exists for the legitimate case of a customer scraping their own intranet. It
    is a separate flag rather than a domain in ``allow_domains`` so that granting
    it is a visible, auditable decision and cannot happen as a side effect of
    someone adding a hostname to a list.
    """

    #: When non-empty, *only* these registrable domains may be fetched. Subdomains
    #: are included; a bare hostname match is not enough (``evil-example.com``
    #: must not satisfy an allowlist entry of ``example.com``).
    allow_domains: frozenset[str] = frozenset()
    deny_domains: frozenset[str] = frozenset()
    allow_internal_targets: bool = False
    allowed_ports: frozenset[int] = frozenset({80, 443, 8080, 8443})
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = 5


@dataclass
class ValidatedTarget:
    """A URL that passed every check, with the addresses it resolved to.

    The addresses are the point. Handing back only "ok" would leave the caller to
    resolve the hostname again at connect time, which is precisely the window a
    DNS-rebinding attack needs. The fetcher is expected to connect to one of
    these and to send the original ``Host`` header.
    """

    url: str
    hostname: str
    port: int
    addresses: list[str] = field(default_factory=list)


def _is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address is off-limits, or ``None`` if it is a public address.

    Returns a reason string rather than a bool so the rejection can say which
    property disqualified the address — the difference between a misconfigured
    DNS record and a deliberate metadata-endpoint probe.
    """
    if str(ip) in _METADATA_ADDRESSES:
        return "cloud metadata endpoint"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_private:
        return "private address range"
    if ip.is_reserved:
        return "reserved address range"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address (0.0.0.0)"
    # IPv4-mapped and 6to4 addresses smuggle a v4 address inside a v6 one, which
    # bypasses a check that only looks at the v6 properties above.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_internal(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _is_internal(ip.sixtofour)
        if ip.teredo is not None:
            return _is_internal(ip.teredo[1])
    return None


def _registrable_suffixes(hostname: str) -> list[str]:
    """Every dotted suffix of a hostname, for allow/deny matching.

    ``a.b.example.com`` yields ``a.b.example.com``, ``b.example.com``,
    ``example.com``, ``com``. Matching on suffixes rather than substrings is what
    stops ``notexample.com`` from satisfying a rule about ``example.com``.
    """
    parts = hostname.split(".")
    return [".".join(parts[i:]) for i in range(len(parts))]


def validate_url(url: str, policy: NetworkPolicy | None = None) -> tuple[str, int]:
    """Check everything knowable without a DNS lookup. Returns (hostname, port).

    Split from resolution so the cheap, synchronous, total-order checks happen
    before any network I/O — a denied domain should never cause a DNS query, both
    because it is faster and because the query itself is a signal to an attacker
    that their URL reached a real system.
    """
    policy = policy or NetworkPolicy()

    if not url or not isinstance(url, str):
        raise UrlRejected("invalid_url", "empty or non-string URL", str(url))
    if len(url) > 2048:
        raise UrlRejected("invalid_url", f"URL exceeds 2048 characters ({len(url)})", url[:120])

    # A URL containing a newline can smuggle headers into some clients.
    if any(c in url for c in "\r\n\t") or any(ord(c) < 0x20 for c in url):
        raise UrlRejected("invalid_url", "URL contains control characters", repr(url[:120]))

    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlRejected(
            "scheme_not_allowed",
            f"scheme {parts.scheme!r} is not http or https",
            url,
        )

    # Credentials in a URL are both a leak risk in logs and a way to confuse the
    # host parsing of downstream clients (``http://expected.com@evil.com/``).
    if parts.username or parts.password:
        raise UrlRejected("credentials_in_url", "URL carries embedded credentials", url)

    hostname = (parts.hostname or "").lower().rstrip(".")
    if not hostname:
        raise UrlRejected("invalid_url", "URL has no hostname", url)

    if hostname in _BLOCKED_HOSTNAMES:
        raise UrlRejected("internal_target", f"hostname {hostname!r} is internal", url)

    # `.internal`, `.local` and friends are never public websites.
    if any(hostname.endswith(suffix) for suffix in (".internal", ".local", ".localdomain", ".onion")):
        raise UrlRejected("internal_target", f"hostname {hostname!r} is a private suffix", url)

    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:  # non-numeric port
        raise UrlRejected("invalid_url", f"invalid port: {exc}", url) from None

    if port in _BLOCKED_PORTS:
        raise UrlRejected("port_not_allowed", f"port {port} is never a website", url)
    if policy.allowed_ports and port not in policy.allowed_ports:
        raise UrlRejected("port_not_allowed", f"port {port} is not in the tenant's allowed ports", url)

    suffixes = _registrable_suffixes(hostname)
    if policy.deny_domains and any(s in policy.deny_domains for s in suffixes):
        raise UrlRejected("domain_denied", f"{hostname} matches a deny rule", url)
    if policy.allow_domains and not any(s in policy.allow_domains for s in suffixes):
        raise UrlRejected(
            "domain_not_allowed",
            f"{hostname} is not covered by this workspace's allowlist",
            url,
        )

    # A literal IP in the URL is checked here; a hostname is checked after DNS.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not policy.allow_internal_targets:
        if (reason := _is_internal(literal)) is not None:
            raise UrlRejected("internal_target", f"{hostname} is a {reason}", url)

    return hostname, port


def resolve_and_validate(
    url: str,
    policy: NetworkPolicy | None = None,
    *,
    resolver=None,
) -> ValidatedTarget:
    """Validate, resolve once, validate every resolved address.

    ``resolver`` is injectable so the DNS-rebinding and split-horizon cases can be
    tested without a network or a hosts file. It takes ``(hostname, port)`` and
    returns ``getaddrinfo``-shaped tuples.

    **Every** address is checked, not just the first. A hostname that resolves to
    one public and one private address is an attack, not a multi-homed server: if
    any answer is internal, the client may pick it on a retry.
    """
    policy = policy or NetworkPolicy()
    hostname, port = validate_url(url, policy)

    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlRejected("dns_failure", f"cannot resolve {hostname}: {exc}", url) from None

    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addresses:
            addresses.append(addr)

    if not addresses:
        raise UrlRejected("dns_failure", f"{hostname} resolved to no addresses", url)

    if not policy.allow_internal_targets:
        for addr in addresses:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                raise UrlRejected("dns_failure", f"unparseable address {addr!r}", url) from None
            if (reason := _is_internal(ip)) is not None:
                raise UrlRejected(
                    "internal_target",
                    f"{hostname} resolves to {addr}, a {reason}",
                    url,
                )

    return ValidatedTarget(url=url, hostname=hostname, port=port, addresses=addresses)


def validate_redirect(
    location: str,
    *,
    from_url: str,
    policy: NetworkPolicy | None = None,
    hops_so_far: int = 0,
    resolver=None,
) -> ValidatedTarget:
    """Re-validate a redirect target. A hop is a new request, not a continuation.

    Relative locations are resolved against the origin URL first, because
    ``Location: /admin`` is common and legitimate, while ``Location:
    http://169.254.169.254/`` is the attack this function exists to stop.
    """
    from urllib.parse import urljoin

    policy = policy or NetworkPolicy()
    if hops_so_far >= policy.max_redirects:
        raise UrlRejected(
            "too_many_redirects",
            f"redirect limit {policy.max_redirects} reached",
            from_url,
        )
    absolute = urljoin(from_url, (location or "").strip())
    return resolve_and_validate(absolute, policy, resolver=resolver)


def check_response_size(
    declared_length: int | None,
    received: int,
    *,
    policy: NetworkPolicy | None = None,
    compressed: int | None = None,
) -> None:
    """Enforce the byte ceiling and refuse decompression bombs.

    Both halves matter. ``Content-Length`` is a hint an attacker controls, so the
    received count is checked as the body streams in; and a small compressed body
    that expands enormously passes every length check while still exhausting
    memory.
    """
    policy = policy or NetworkPolicy()
    if declared_length is not None and declared_length > policy.max_bytes:
        raise UrlRejected(
            "response_too_large",
            f"Content-Length {declared_length} exceeds limit {policy.max_bytes}",
        )
    if received > policy.max_bytes:
        raise UrlRejected(
            "response_too_large",
            f"received {received} bytes, exceeds limit {policy.max_bytes}",
        )
    if compressed and compressed > 0 and received / compressed > MAX_DECOMPRESSION_RATIO:
        raise UrlRejected(
            "decompression_bomb",
            f"body expanded {received / compressed:.0f}x from {compressed} bytes, "
            f"over the {MAX_DECOMPRESSION_RATIO}x ceiling",
        )

