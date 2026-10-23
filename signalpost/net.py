"""Outbound HTTP with a safe URL policy, robots.txt checks and content hashing.

Every fetch returns a Snapshot. Nothing here decides identity; it only records what was
retrieved, from where, when, and with which content hash.
"""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = "SignalpostResearchAgent/1.0 (+https://github.com/mstege/signalpost-agent)"
ROBOTS_AGENT = "SignalpostResearchAgent"
MAX_BYTES = 2_500_000
MAX_REDIRECTS = 5
MAX_RESPONSE_SECONDS = 20.0   # wall-clock limit for reading one response body
MAX_RETRY_WAIT = 30.0         # a longer Retry-After means: do not ask this host again during the run
ALLOWED_PORTS = {None, 80, 443}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Snapshot:
    requested_url: str
    final_url: str | None
    status: int
    retrieved_at: str
    content_sha256: str | None = None
    content_type: str = ""
    text: str = ""
    redirects: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0
    bytes: int = 0
    retry_after: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error is None

    def json(self):
        return json.loads(self.text)


class UrlPolicyError(Exception):
    pass


_dns_cache: dict[str, list[str]] = {}
_dns_lock = threading.Lock()


def resolve(host: str) -> list[str]:
    with _dns_lock:
        if host in _dns_cache:
            return _dns_cache[host]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        addrs = sorted({info[4][0] for info in infos})
    except (socket.gaierror, UnicodeError, OSError):
        addrs = []
    with _dns_lock:
        _dns_cache[host] = addrs
    return addrs


def check_url(url: str) -> str:
    """Return the normalised URL or raise UrlPolicyError.

    Only public http(s) hosts on default ports are allowed. Hosts that resolve to private,
    loopback, link-local, multicast or reserved addresses are refused (SSRF guard).
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UrlPolicyError(f"scheme not allowed: {parts.scheme}")
    if parts.username or parts.password:
        raise UrlPolicyError("credentials in URL")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or "." not in host:
        raise UrlPolicyError("host missing")
    if parts.port not in ALLOWED_PORTS:
        raise UrlPolicyError(f"port not allowed: {parts.port}")
    try:
        host_idna = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlPolicyError("invalid host") from exc
    addrs = resolve(host_idna)
    if not addrs:
        raise UrlPolicyError("dns: no address")
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            raise UrlPolicyError(f"non-public address for {host}")
    netloc = host_idna if parts.port is None else f"{host_idna}:{parts.port}"
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_ssl_context = ssl.create_default_context()
_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context))


class HostGate:
    """Limits concurrent requests per host and spaces request starts."""

    def __init__(self, per_host: int = 2, min_interval: float = 0.25):
        self.per_host = per_host
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}
        self._last: dict[str, float] = {}

    def acquire(self, host: str) -> threading.Semaphore:
        with self._lock:
            sem = self._sems.setdefault(host, threading.Semaphore(self.per_host))
        sem.acquire()
        with self._lock:
            wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
            self._last[host] = max(time.monotonic(), self._last.get(host, 0) + self.min_interval)
        if wait > 0:
            time.sleep(wait)
        return sem

    def cool(self, host: str, seconds: float) -> None:
        """Push the next request start for a host into the future (after 429/503), never by more than 60 s."""
        with self._lock:
            self._last[host] = max(self._last.get(host, 0), time.monotonic() + min(max(seconds, 0.0), 60.0))


class Fetcher:
    def __init__(self, *, timeout: float = 10.0, respect_robots: bool = True, gate: HostGate | None = None):
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.gate = gate or HostGate()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self.requests = 0
        self.bytes = 0
        self.by_source: dict[str, int] = {}
        self.limit: int | None = None   # hard cap on HTTP attempts for the whole run (None = unlimited)
        self.reserve = 0                # attempts kept free for registry calls of companies not yet started
        self.refused: dict[str, int] = {}
        self.rate_limited = 0
        self._blocked: dict[str, float] = {}

    # robots.txt ---------------------------------------------------------
    def robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            known = origin in self._robots
            parser = self._robots.get(origin)
        if not known:
            parser = None
            snap = self._raw_get(origin + "/robots.txt", accept="text/plain", max_bytes=300_000)
            if snap.status in (401, 403):
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
            elif snap.ok and "html" not in snap.content_type:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(snap.text.splitlines())
            with self._robots_lock:
                self._robots[origin] = parser
        if parser is None:
            return True
        return parser.can_fetch(ROBOTS_AGENT, url)

    # fetching -----------------------------------------------------------
    def get(self, url: str, *, accept: str = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
            headers: dict | None = None, max_bytes: int = MAX_BYTES, check_robots: bool = True) -> Snapshot:
        redirects: list[str] = []
        current = url
        started = time.monotonic()
        for _ in range(MAX_REDIRECTS + 1):
            try:
                current = check_url(current)
            except UrlPolicyError as exc:
                return Snapshot(url, None, 0, utc_now(), error=f"url_policy: {exc}", redirects=redirects)
            if check_robots and not self.robots_allowed(current):
                return Snapshot(url, current, 0, utc_now(), error="blocked_by_robots", redirects=redirects)
            snap = self._raw_get(current, accept=accept, headers=headers, max_bytes=max_bytes)
            if snap.status in (301, 302, 303, 307, 308) and snap.final_url:
                redirects.append(current)
                current = snap.final_url
                continue
            snap.requested_url = url
            snap.redirects = redirects
            snap.elapsed_ms = int((time.monotonic() - started) * 1000)
            return snap
        return Snapshot(url, current, 0, utc_now(), error="too_many_redirects", redirects=redirects)

    def _admit(self, source: str) -> bool:
        """Count one HTTP attempt against the run budget. Registry calls may use the reserve; nothing may pass the cap."""
        with self._count_lock:
            if self.limit is not None:
                ceiling = self.limit if source == "brreg" else self.limit - self.reserve
                if self.requests >= ceiling:
                    self.refused[source] = self.refused.get(source, 0) + 1
                    return False
            if source == "brreg" and self.reserve > 0:
                self.reserve -= 1  # a registry call uses up one reserved slot
            self.requests += 1
            self.by_source[source] = self.by_source.get(source, 0) + 1
            return True

    def _raw_get(self, url: str, *, accept: str, headers: dict | None = None, max_bytes: int = MAX_BYTES) -> Snapshot:
        """One HTTP attempt, plus at most one retry after a short 429/503 back-off."""
        host = urllib.parse.urlsplit(url).hostname or ""
        with self._count_lock:
            blocked = self._blocked.get(host, 0) > time.monotonic()
        if blocked:
            return Snapshot(url, url, 0, utc_now(), error="rate_limited: host asked to wait longer than this run allows")
        snap = self._attempt(url, accept=accept, headers=headers, max_bytes=max_bytes)
        if snap.status not in (429, 503):
            return snap
        with self._count_lock:
            self.rate_limited += 1
        wait = snap.retry_after
        if wait > MAX_RETRY_WAIT:
            with self._count_lock:
                self._blocked[host] = time.monotonic() + min(wait, 3600.0)
            return snap
        if hasattr(self.gate, "cool"):
            self.gate.cool(host, max(wait, 1.0))   # the retry waits in the host gate, without holding a slot
        return self._attempt(url, accept=accept, headers=headers, max_bytes=max_bytes)

    def _attempt(self, url: str, *, accept: str, headers: dict | None = None, max_bytes: int = MAX_BYTES) -> Snapshot:
        host = urllib.parse.urlsplit(url).hostname or ""
        if not self._admit(source_of(host)):
            return Snapshot(url, url, 0, utc_now(), error="request_budget_exhausted")
        req_headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6",
                       "Accept-Encoding": "gzip, deflate"}
        req_headers.update(headers or {})
        request = urllib.request.Request(url, headers=req_headers)
        sem = self.gate.acquire(host)
        retrieved_at = utc_now()
        try:
            with _opener.open(request, timeout=self.timeout) as response:
                raw = _read_limited(response, max_bytes + 1, time.monotonic() + MAX_RESPONSE_SECONDS)
                status = response.status
                ctype = response.headers.get("Content-Type", "")
                encoding = response.headers.get("Content-Encoding", "")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                return Snapshot(url, urllib.parse.urljoin(url, location), exc.code, retrieved_at)
            try:
                raw = exc.read(200_000)
            except Exception:  # noqa: BLE001
                raw = b""
            self._count(len(raw))
            snap = Snapshot(url, url, exc.code, retrieved_at, content_sha256=sha256(raw), error=f"HTTP {exc.code}")
            if exc.code in (429, 503):
                snap.retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            return snap
        except Exception as exc:  # noqa: BLE001 - any transport problem becomes a recorded fetch error
            reason = getattr(exc, "reason", exc)
            return Snapshot(url, url, 0, retrieved_at, error=f"{type(exc).__name__}: {str(reason)[:120]}")
        finally:
            sem.release()
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        try:
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            elif "deflate" in encoding:
                raw = zlib.decompress(raw)
        except (OSError, zlib.error, EOFError):
            pass
        self._count(len(raw))
        text = decode(raw, ctype)
        return Snapshot(url, url, status, retrieved_at, content_sha256=sha256(raw), content_type=ctype.lower(),
                        text=text, bytes=len(raw))

    def _count(self, nbytes: int) -> None:
        with self._count_lock:
            self.bytes += nbytes


def _read_limited(response, limit: int, deadline: float) -> bytes:
    """Read at most `limit` bytes and stop at `deadline`, so a server that trickles bytes cannot hold a worker."""
    chunks, size = [], 0
    while size < limit:
        if time.monotonic() > deadline:
            raise TimeoutError(f"response took longer than {MAX_RESPONSE_SECONDS:.0f} s")
        chunk = response.read(min(65536, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def source_of(host: str) -> str:
    host = (host or "").lower()
    if host.endswith("brreg.no"):
        return "brreg"
    if host.endswith("nav.no"):
        return "nav"
    if host.endswith("wikidata.org"):
        return "wikidata"
    return "web"


def _retry_after(value: str | None) -> float:
    """Seconds from a Retry-After header (delta seconds or HTTP date); 5 s when absent or unreadable."""
    if not value:
        return 5.0
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return 5.0


def decode(raw: bytes, content_type: str) -> str:
    charset = None
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=")[-1].split(";")[0].strip().strip('"')
    if not charset:
        head = raw[:2048].decode("ascii", "ignore").lower()
        marker = head.find("charset=")
        if marker >= 0:
            charset = head[marker + 8:marker + 30].strip("\"' ").split('"')[0].split("'")[0].split(";")[0].split(">")[0].strip()
    for candidate in (charset, "utf-8", "cp1252"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")
