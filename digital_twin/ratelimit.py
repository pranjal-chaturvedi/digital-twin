"""Per-client rate limiting: client identification, a limiter interface, and its in-memory backend.

Limits are per process and reset on restart. The handler depends only on `Limiter`, so a
different backend can replace the in-memory one without touching it.
"""
import ipaddress
import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Protocol

from digital_twin.config import (
    RATE_LIMIT_MAX_CLIENTS,
    RATE_LIMIT_REQUESTS_PER_WINDOW,
    RATE_LIMIT_WINDOW_SECONDS,
    TRUSTED_PROXY_COUNT,
)

logger = logging.getLogger(__name__)

UNKNOWN_CLIENT = "unknown"


class Limiter(Protocol):
    def allow(self, client_key: str) -> bool:
        """Record one request for `client_key`; return False if it's over the limit."""
        ...


def _normalise_ip(value: str | None) -> str | None:
    """Parse an IP; IPv6 is bucketed by /64, since one host usually owns a whole /64."""
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _header(headers, name: str) -> str | None:
    if not headers or not hasattr(headers, "get"):
        return None
    # Starlette headers are case-insensitive; plain dicts (tests, other callers) aren't
    return headers.get(name) or headers.get(name.title())


def client_key(request, trusted_proxy_count: int = TRUSTED_PROXY_COUNT) -> str:
    """Identify the client behind `request` (a gr.Request) for rate limiting.

    X-Forwarded-For is read from the right: each trusted proxy appends the address it
    received the request from, so the entry `trusted_proxy_count` from the end is the
    one the outermost trusted proxy saw. Entries further left come from the client and
    are ignored, so a forged header can't buy a fresh bucket. If the header is missing,
    too short or invalid, the socket peer address is used instead.
    """
    if request is None:
        return UNKNOWN_CLIENT

    if trusted_proxy_count > 0:
        forwarded = _header(getattr(request, "headers", None), "x-forwarded-for")
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            if len(hops) >= trusted_proxy_count:
                ip = _normalise_ip(hops[-trusted_proxy_count])
                if ip:
                    return ip

    client = getattr(request, "client", None)
    return _normalise_ip(getattr(client, "host", None)) or UNKNOWN_CLIENT


class SlidingWindowRateLimiter:
    """In-memory sliding-window limiter, safe to share across Gradio's worker threads.

    Clients are kept in least-recently-seen order, so stale ones are evicted from the
    front cheaply, and the oldest are dropped if more than `max_clients` are active.
    """

    def __init__(
        self,
        requests_per_window: int,
        window_seconds: float,
        max_clients: int = RATE_LIMIT_MAX_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_requests = requests_per_window
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, client_key: str) -> bool:
        with self._lock:
            now = self._clock()
            cutoff = now - self.window_seconds

            hits = self._hits.get(client_key)
            if hits is None:
                hits = self._hits[client_key] = deque()
            else:
                self._hits.move_to_end(client_key)
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self.max_requests:
                logger.warning("guard_rate_limit_triggered")
                return False

            hits.append(now)
            self._evict(cutoff)
            return True

    def _evict(self, cutoff: float):
        # Front of the dict = least recently seen; drop clients with no recent hits
        while self._hits:
            oldest_key, oldest_hits = next(iter(self._hits.items()))
            if oldest_hits and oldest_hits[-1] > cutoff:
                break
            del self._hits[oldest_key]
        # Still too many active clients: drop the least recently seen
        evicted = 0
        while len(self._hits) > self.max_clients:
            self._hits.popitem(last=False)
            evicted += 1
        if evicted:
            logger.warning("guard_rate_limit_clients_evicted", extra={"count": evicted})

    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._hits)


rate_limiter: Limiter = SlidingWindowRateLimiter(RATE_LIMIT_REQUESTS_PER_WINDOW, RATE_LIMIT_WINDOW_SECONDS)
