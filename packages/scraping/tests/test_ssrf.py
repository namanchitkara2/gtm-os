"""SSRF, the single most consequential bug class in a scraper.

Each test names the thing an attacker gets if it fails, because a test that only
asserts "raises" invites someone to weaken it to make a fetch work.
"""

from __future__ import annotations

import socket

import pytest
from gtmos_scraping.ssrf import (
    DEFAULT_MAX_BYTES,
    NetworkPolicy,
    UrlRejected,
    check_response_size,
    resolve_and_validate,
    validate_redirect,
    validate_url,
)


def _resolver(*addresses: str):
    """A fake DNS resolver returning the addresses given."""
    def resolve(host, port, **_):
        out = []
        for addr in addresses:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port)))
        return out
    return resolve


# ------------------------------------------------------- the credential theft --

class TestCloudMetadata:
    """169.254.169.254 returns IAM credentials with no auth. This is the big one."""

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://192.0.0.192/opc/v1/instance/",
    ])
    def test_metadata_endpoints_are_refused(self, url):
        with pytest.raises(UrlRejected) as exc:
            validate_url(url)
        assert exc.value.reason == "internal_target"

    def test_metadata_via_hostname_is_refused(self):
        with pytest.raises(UrlRejected):
            validate_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_metadata_reached_by_dns_is_refused(self):
        """A public hostname whose A record points at the metadata IP."""
        with pytest.raises(UrlRejected) as exc:
            resolve_and_validate(
                "http://totally-normal-site.com/", resolver=_resolver("169.254.169.254")
            )
        assert "metadata" in exc.value.detail


class TestPrivateNetworks:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:5432/",
        "http://localhost/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://169.254.1.1/",
    ])
    def test_private_and_loopback_are_refused(self, url):
        with pytest.raises(UrlRejected) as exc:
            validate_url(url)
        assert exc.value.reason in ("internal_target", "port_not_allowed")

    def test_ipv4_mapped_ipv6_cannot_smuggle_loopback(self):
        """::ffff:127.0.0.1 is loopback wearing a v6 address.

        A check that only reads IPv6Address.is_loopback returns False here, which
        is how this bypass survives most implementations.
        """
        with pytest.raises(UrlRejected):
            validate_url("http://[::ffff:127.0.0.1]/")

    def test_sixtofour_cannot_smuggle_private_range(self):
        with pytest.raises(UrlRejected):
            validate_url("http://[2002:0a00:0001::1]/")  # 6to4 wrapping 10.0.0.1

    def test_decimal_ip_notation_still_resolves_to_loopback(self):
        """http://2130706433/ is 127.0.0.1 in decimal — blocked after resolution."""
        with pytest.raises(UrlRejected):
            resolve_and_validate("http://2130706433/", resolver=_resolver("127.0.0.1"))

    def test_internal_targets_allowed_only_by_explicit_policy(self):
        policy = NetworkPolicy(allow_internal_targets=True)
        target = resolve_and_validate(
            "http://10.0.0.5/", policy, resolver=_resolver("10.0.0.5")
        )
        assert target.addresses == ["10.0.0.5"]


class TestDnsRebinding:
    def test_all_resolved_addresses_are_checked_not_just_the_first(self):
        """One public answer and one private answer is an attack, not multi-homing.

        If only the first address is validated, the HTTP client may pick the second
        on a retry and reach the private one.
        """
        with pytest.raises(UrlRejected) as exc:
            resolve_and_validate(
                "http://split-horizon.com/", resolver=_resolver("93.184.216.34", "127.0.0.1")
            )
        assert "127.0.0.1" in exc.value.detail

    def test_validated_addresses_are_returned_for_pinning(self):
        """The fetcher must connect to what was validated, not re-resolve."""
        target = resolve_and_validate(
            "https://example.com/", resolver=_resolver("93.184.216.34")
        )
        assert target.addresses == ["93.184.216.34"]
        assert target.hostname == "example.com"
        assert target.port == 443


class TestSchemesAndShapes:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "file://localhost/etc/shadow",
        "gopher://evil.com:70/_data",
        "dict://localhost:11211/stat",
        "ftp://internal/secrets",
        "data:text/html,<script>x</script>",
        "javascript:alert(1)",
        "jar:http://evil.com!/",
    ])
    def test_non_http_schemes_are_refused(self, url):
        with pytest.raises(UrlRejected) as exc:
            validate_url(url)
        assert exc.value.reason in ("scheme_not_allowed", "invalid_url")

    def test_embedded_credentials_are_refused(self):
        """http://expected.com@evil.com/ is a host-confusion attack."""
        with pytest.raises(UrlRejected) as exc:
            validate_url("http://expected.com:pw@evil.com/")
        assert exc.value.reason == "credentials_in_url"

    def test_crlf_injection_is_refused(self):
        with pytest.raises(UrlRejected) as exc:
            validate_url("http://example.com/\r\nX-Injected: yes")
        assert exc.value.reason == "invalid_url"

    def test_absurdly_long_url_is_refused(self):
        with pytest.raises(UrlRejected):
            validate_url("https://example.com/" + "a" * 3000)

    @pytest.mark.parametrize("url", ["", None, "not a url", "https://"])
    def test_malformed_input_is_refused_not_crashed(self, url):
        with pytest.raises(UrlRejected):
            validate_url(url)  # type: ignore[arg-type]


class TestPorts:
    @pytest.mark.parametrize("port", [22, 3306, 5432, 6379, 27017, 2375, 9200, 11211])
    def test_service_ports_are_never_a_website(self, port):
        with pytest.raises(UrlRejected) as exc:
            validate_url(f"http://example.com:{port}/")
        assert exc.value.reason == "port_not_allowed"

    def test_the_platforms_own_postgres_port_is_blocked(self):
        """55432 is the local dev Postgres. Not in the global block list, so the
        tenant's allowed-ports policy is what stops it."""
        with pytest.raises(UrlRejected):
            validate_url("http://example.com:55432/")

    def test_standard_web_ports_pass(self):
        for port in (80, 443, 8080, 8443):
            assert validate_url(f"http://example.com:{port}/")[1] == port


class TestRedirects:
    def test_redirect_to_metadata_is_refused(self):
        """A URL that passes validation can still 302 somewhere fatal."""
        with pytest.raises(UrlRejected) as exc:
            validate_redirect(
                "http://169.254.169.254/latest/meta-data/",
                from_url="https://example.com/",
                resolver=_resolver("169.254.169.254"),
            )
        assert exc.value.reason == "internal_target"

    def test_relative_redirect_is_resolved_against_origin(self):
        target = validate_redirect(
            "/team", from_url="https://example.com/about",
            resolver=_resolver("93.184.216.34"),
        )
        assert target.url == "https://example.com/team"

    def test_redirect_limit_is_enforced(self):
        with pytest.raises(UrlRejected) as exc:
            validate_redirect(
                "https://example.com/next", from_url="https://example.com/",
                hops_so_far=5, resolver=_resolver("93.184.216.34"),
            )
        assert exc.value.reason == "too_many_redirects"

    def test_redirect_to_file_scheme_is_refused(self):
        with pytest.raises(UrlRejected):
            validate_redirect(
                "file:///etc/passwd", from_url="https://example.com/",
                resolver=_resolver("93.184.216.34"),
            )


class TestAllowAndDenyLists:
    def test_allowlist_excludes_everything_else(self):
        policy = NetworkPolicy(allow_domains=frozenset({"example.com"}))
        assert validate_url("https://www.example.com/x", policy)
        with pytest.raises(UrlRejected) as exc:
            validate_url("https://other.com/", policy)
        assert exc.value.reason == "domain_not_allowed"

    def test_allowlist_covers_subdomains(self):
        policy = NetworkPolicy(allow_domains=frozenset({"example.com"}))
        assert validate_url("https://careers.eu.example.com/jobs", policy)

    def test_allowlist_is_not_a_substring_match(self):
        """notexample.com must not satisfy a rule about example.com."""
        policy = NetworkPolicy(allow_domains=frozenset({"example.com"}))
        for host in ("notexample.com", "example.com.evil.net", "myexample.com"):
            with pytest.raises(UrlRejected):
                validate_url(f"https://{host}/", policy)

    def test_denylist_beats_allowlist(self):
        policy = NetworkPolicy(
            allow_domains=frozenset({"example.com"}),
            deny_domains=frozenset({"secret.example.com"}),
        )
        with pytest.raises(UrlRejected) as exc:
            validate_url("https://secret.example.com/", policy)
        assert exc.value.reason == "domain_denied"


class TestResponseSize:
    def test_oversized_content_length_is_refused_before_download(self):
        with pytest.raises(UrlRejected) as exc:
            check_response_size(DEFAULT_MAX_BYTES + 1, 0)
        assert exc.value.reason == "response_too_large"

    def test_lying_content_length_is_caught_while_streaming(self):
        """Content-Length is attacker-controlled; the received count is not."""
        with pytest.raises(UrlRejected):
            check_response_size(100, DEFAULT_MAX_BYTES + 1)

    def test_decompression_bomb_is_refused(self):
        """10KB of gzip expanding to 5MB passes every length check on the wire."""
        with pytest.raises(UrlRejected) as exc:
            check_response_size(None, 4_000_000, compressed=1_000)
        assert exc.value.reason == "decompression_bomb"

    def test_normally_compressed_page_is_fine(self):
        check_response_size(None, 240_000, compressed=40_000)  # 6x, ordinary HTML


def test_public_url_passes_the_whole_gate():
    """The happy path must not be broken by any of the above."""
    target = resolve_and_validate(
        "https://www.example.com/about?utm_source=x", resolver=_resolver("93.184.216.34")
    )
    assert target.hostname == "www.example.com"
    assert target.port == 443
