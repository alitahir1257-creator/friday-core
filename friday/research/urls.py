"""URL normalization shared by search, fetch, and evidence deduplication."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit, urlunsplit
import ipaddress
import re


_BAD_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data",
    "metadata",
})
_CREDENTIAL_PARAMETERS = re.compile(
    r"(?:access[_-]?token|api[_-]?key|auth(?:orization)?|credential|password|passwd|secret|"
    r"session(?:id)?|signature|sig|token)",
    re.I,
)


class UnsafeResearchURL(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_url(value: str, *, fixture_hosts: frozenset[str] | set[str] | tuple[str, ...] = ()) -> str:
    """Return a stable public URL form without fragments or default ports."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise UnsafeResearchURL("INVALID_URL", "The source URL is invalid.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise UnsafeResearchURL("INVALID_URL", "The source URL contains unsafe characters.")
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"}:
            raise UnsafeResearchURL("BLOCKED_SCHEME", "Only public HTTP and HTTPS pages can be fetched.")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise UnsafeResearchURL("INVALID_URL", "The source URL is invalid or contains credentials.")
        host = parsed.hostname.casefold().rstrip(".").encode("idna").decode("ascii")
        port = parsed.port
    except UnsafeResearchURL:
        raise
    except (UnicodeError, ValueError):
        raise UnsafeResearchURL("INVALID_URL", "The source URL is invalid.") from None
    if not re.fullmatch(r"[a-z0-9.-]+", host) and not _is_ip(host):
        raise UnsafeResearchURL("INVALID_URL", "The source host is invalid.")
    if any(_CREDENTIAL_PARAMETERS.search(name) for name, _value in parse_qsl(parsed.query, keep_blank_values=True)):
        raise UnsafeResearchURL("BLOCKED_CREDENTIAL_URL", "Credential-bearing source URLs are not fetched.")
    allowed_fixtures = {item.casefold() for item in fixture_hosts}
    fixture_host = host in allowed_fixtures
    if (host == "localhost" or host.endswith((".localhost", ".local")) or host in _BAD_HOSTS) and not fixture_host:
        raise UnsafeResearchURL("SSRF_BLOCKED", "Local and metadata hosts are blocked.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not _public_address(address) and not (fixture_host and address.is_loopback):
        raise UnsafeResearchURL("SSRF_BLOCKED", "Local and private network addresses are blocked.")
    if port is not None and port not in {80, 443} and not fixture_host:
        raise UnsafeResearchURL("BLOCKED_PORT", "Research pages are limited to standard HTTP/HTTPS ports.")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme.casefold() == "https" else 80):
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))


def domain_of(value: str) -> str:
    try:
        return urlsplit(value).hostname.casefold().rstrip(".")  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        return ""


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(address.is_global and not address.is_private and not address.is_loopback
                and not address.is_link_local and not address.is_multicast
                and not address.is_reserved and not address.is_unspecified)
