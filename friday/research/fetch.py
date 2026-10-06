"""Public-page fetcher with DNS pinning, redirect, timeout, and size bounds."""

from __future__ import annotations

from http.client import HTTPConnection, HTTPSConnection, HTTPResponse
from socket import SOCK_STREAM, create_connection, getaddrinfo
from ssl import create_default_context
from urllib.parse import urljoin, urlsplit
import ipaddress
import math
import re

from friday.research.models import FetchResult
from friday.research.urls import UnsafeResearchURL, _public_address, normalize_url


_REDIRECTS = {301, 302, 303, 307, 308}
_ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_USER_AGENT = "FRIDAY-Research/1.0 (+read-only public research; contact: local user agent)"
class ResearchFetchError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class ResearchFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_bytes: int = 2_000_000,
        max_redirects: int = 4,
        fixture_hosts: tuple[str, ...] = (),
        accepted_types: tuple[str, ...] = _ALLOWED_TYPES,
        user_agent: str = _USER_AGENT,
        resolver=getaddrinfo,
        connection_factory=None,
    ) -> None:
        if not 0.1 <= float(timeout_seconds) <= 30:
            raise ValueError("timeout_seconds must be from 0.1 to 30.")
        if not 1024 <= max_bytes <= 10_000_000:
            raise ValueError("max_bytes must be from 1024 to 10000000.")
        if not 0 <= max_redirects <= 5:
            raise ValueError("max_redirects must be from 0 to 5.")
        if not accepted_types or any(not isinstance(value, str) or "/" not in value for value in accepted_types):
            raise ValueError("accepted_types must contain media types.")
        if not isinstance(user_agent, str) or not user_agent.strip() or len(user_agent) > 200:
            raise ValueError("user_agent must contain 1 to 200 characters.")
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.fixture_hosts = frozenset(host.casefold() for host in fixture_hosts)
        self.accepted_types = tuple(value.casefold() for value in accepted_types)
        self.user_agent = user_agent.strip()
        self._resolver = resolver
        self._connection_factory = connection_factory

    def fetch(self, url: str, *, timeout_seconds: float | None = None) -> FetchResult:
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0.1 <= timeout_seconds <= 30
        ):
            raise ValueError("timeout_seconds must be from 0.1 to 30.")
        timeout = min(self.timeout_seconds, float(timeout_seconds)) if timeout_seconds is not None else self.timeout_seconds
        current = normalize_url(url, fixture_hosts=self.fixture_hosts)
        requested = current
        visited: set[str] = set()
        for redirect_index in range(self.max_redirects + 1):
            if current in visited:
                raise ResearchFetchError("REDIRECT_LOOP", "The page redirected to a previously visited URL.")
            visited.add(current)
            parsed = urlsplit(current)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = self._resolve_public(host, port)
            if not addresses:
                raise ResearchFetchError("DNS_FAILED", "The public source host could not be resolved.")
            connection = self._connect(parsed.scheme, host, addresses[0], port, timeout)
            response: HTTPResponse | None = None
            try:
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
                host_header = f"[{host}]" if ":" in host else host
                if port != (443 if parsed.scheme == "https" else 80):
                    host_header += f":{port}"
                connection.request(
                    "GET", target,
                    headers={
                        "Host": host_header,
                        "User-Agent": self.user_agent,
                        "Accept": ",".join(self.accepted_types),
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                status = response.status
                if status in _REDIRECTS:
                    location = response.getheader("Location")
                    if not location or redirect_index >= self.max_redirects:
                        raise ResearchFetchError("REDIRECT_LIMIT", "The page exceeded the safe redirect limit.")
                    current = normalize_url(urljoin(current, location), fixture_hosts=self.fixture_hosts)
                    continue
                if not 200 <= status < 300:
                    raise ResearchFetchError("HTTP_ERROR", f"The page returned HTTP {status}.", status_code=status)
                content_type = response.getheader("Content-Type", "").strip()
                media_type = content_type.split(";", 1)[0].casefold()
                if media_type == "application/pdf" or parsed.path.casefold().endswith(".pdf"):
                    return FetchResult(requested, current, status, content_type or "application/pdf", b"", {}, "DOCUMENT_PARSING_DEFERRED")
                if media_type not in self.accepted_types:
                    raise ResearchFetchError("UNSUPPORTED_CONTENT_TYPE", "The source is not an HTML, XHTML, or text page.")
                content_encoding = response.getheader("Content-Encoding", "identity").strip().casefold()
                if content_encoding not in {"", "identity"}:
                    raise ResearchFetchError("UNSUPPORTED_CONTENT_ENCODING", "Compressed page responses are not accepted.")
                raw_length = response.getheader("Content-Length")
                if raw_length:
                    try:
                        if int(raw_length) > self.max_bytes:
                            raise ResearchFetchError("RESPONSE_TOO_LARGE", "The page exceeded the response size limit.")
                    except ValueError:
                        pass
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise ResearchFetchError("RESPONSE_TOO_LARGE", "The page exceeded the response size limit.")
                headers = {key.casefold(): value for key, value in response.getheaders() if key.casefold() in {"content-type", "last-modified", "etag"}}
                return FetchResult(requested, current, status, content_type, body, headers)
            except ResearchFetchError:
                raise
            except (OSError, TimeoutError, ValueError) as error:
                code = "FETCH_TIMEOUT" if isinstance(error, TimeoutError) else "FETCH_FAILED"
                raise ResearchFetchError(code, "The source could not be fetched safely.") from None
            finally:
                if response is not None:
                    response.close()
                connection.close()
        raise ResearchFetchError("REDIRECT_LIMIT", "The page exceeded the safe redirect limit.")

    def _resolve_public(self, host: str, port: int) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        try:
            rows = self._resolver(host, port, type=SOCK_STREAM)
        except OSError:
            if literal is not None and host in self.fixture_hosts and literal.is_loopback:
                return (host,)
            raise ResearchFetchError("DNS_FAILED", "The public source host could not be resolved.") from None
        addresses = tuple(dict.fromkeys(str(row[4][0]).split("%", 1)[0] for row in rows))
        if not addresses:
            raise ResearchFetchError("DNS_FAILED", "The public source host could not be resolved.")
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                raise ResearchFetchError("DNS_FAILED", "The source host returned an invalid address.") from None
            if host in self.fixture_hosts and address.is_loopback:
                continue
            if not _public_address(address):
                raise ResearchFetchError("SSRF_BLOCKED", "Local and private network addresses are blocked.")
        if host in self.fixture_hosts:
            if any(not ipaddress.ip_address(value).is_loopback for value in addresses):
                raise ResearchFetchError("SSRF_BLOCKED", "Test fixture hosts must resolve only to loopback addresses.")
            return addresses
        return addresses

    def _connect(self, scheme: str, host: str, address: str, port: int, timeout: float):
        if self._connection_factory is not None:
            return self._connection_factory(scheme, host, address, port, timeout)
        if scheme == "https":
            return _PinnedHTTPSConnection(host, address, port, timeout)
        return HTTPConnection(address, port=port, timeout=timeout)
