import threading
from types import SimpleNamespace

import pytest

from digital_twin.ratelimit import UNKNOWN_CLIENT, SlidingWindowRateLimiter, client_key

PROXY_PEER = "10.21.157.68"  # Render's internal proxy, as seen on the socket


def _request(xff=None, host=PROXY_PEER, header_name="x-forwarded-for"):
    headers = {header_name: xff} if xff is not None else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- client_key ---------------------------------------------------------------

def test_render_shape_picks_client_added_by_cloudflare():
    # <client>, <Cloudflare edge> as appended by Cloudflare then Render
    assert client_key(_request("99.61.165.29, 104.22.17.40"), trusted_proxy_count=2) == "99.61.165.29"


def test_forged_header_cannot_change_the_key():
    honest = client_key(_request("99.61.165.29, 104.22.17.40"), trusted_proxy_count=2)
    for forged in ["1.1.1.1", "1.1.1.1, 2.2.2.2", "not-an-ip", "8.8.8.8, 9.9.9.9, 7.7.7.7"]:
        # Proxies append, so whatever the client sent ends up to the left
        request = _request(f"{forged}, 99.61.165.29, 104.22.17.40")
        assert client_key(request, trusted_proxy_count=2) == honest


def test_single_proxy_takes_rightmost_entry():
    assert client_key(_request("1.1.1.1, 203.0.113.7"), trusted_proxy_count=1) == "203.0.113.7"


def test_zero_proxies_ignores_header():
    assert client_key(_request("203.0.113.7", host="198.51.100.2"), trusted_proxy_count=0) == "198.51.100.2"


def test_too_few_hops_falls_back_to_socket_peer():
    # Only one entry but two proxies expected: the entry could be client-written
    assert client_key(_request("203.0.113.7"), trusted_proxy_count=2) == PROXY_PEER


def test_invalid_trusted_hop_falls_back_to_socket_peer():
    assert client_key(_request("garbage, 104.22.17.40"), trusted_proxy_count=2) == PROXY_PEER


def test_missing_header_uses_socket_peer():
    assert client_key(_request(None, host="198.51.100.2"), trusted_proxy_count=2) == "198.51.100.2"


def test_title_case_header_in_plain_dict():
    request = _request("99.61.165.29, 104.22.17.40", header_name="X-Forwarded-For")
    assert client_key(request, trusted_proxy_count=2) == "99.61.165.29"


def test_ignores_empty_hops():
    assert client_key(_request("99.61.165.29, , 104.22.17.40,"), trusted_proxy_count=2) == "99.61.165.29"


def test_ipv6_is_bucketed_by_64():
    a = client_key(_request("2001:db8:1:2::1, 104.22.17.40"), trusted_proxy_count=2)
    b = client_key(_request("2001:db8:1:2:ffff::9, 104.22.17.40"), trusted_proxy_count=2)
    c = client_key(_request("2001:db8:1:3::1, 104.22.17.40"), trusted_proxy_count=2)
    assert a == b == "2001:db8:1:2::/64"
    assert c != a


def test_ipv4_mapped_ipv6_is_treated_as_ipv4():
    assert client_key(_request("::ffff:203.0.113.7, 104.22.17.40"), trusted_proxy_count=2) == "203.0.113.7"


def test_no_request_or_address_is_unknown():
    assert client_key(None) == UNKNOWN_CLIENT
    assert client_key(SimpleNamespace(headers={}, client=None), trusted_proxy_count=2) == UNKNOWN_CLIENT


# --- SlidingWindowRateLimiter ---------------------------------------------------

def test_allows_up_to_limit_then_blocks():
    limiter = SlidingWindowRateLimiter(2, 10, clock=FakeClock())
    assert limiter.allow("a")
    assert limiter.allow("a")
    assert not limiter.allow("a")


def test_window_expiry_frees_capacity():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(2, 10, clock=clock)
    limiter.allow("a")
    clock.now += 5
    limiter.allow("a")
    assert not limiter.allow("a")

    clock.now += 5  # first hit is now exactly one window old
    assert limiter.allow("a")
    assert not limiter.allow("a")


def test_denied_requests_do_not_extend_the_window():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(1, 10, clock=clock)
    limiter.allow("a")
    for _ in range(5):
        clock.now += 1
        assert not limiter.allow("a")
    clock.now += 5
    assert limiter.allow("a")


def test_clients_are_isolated():
    limiter = SlidingWindowRateLimiter(1, 10, clock=FakeClock())
    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")


def test_stale_clients_are_evicted():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(5, 10, clock=clock)
    for key in ["a", "b", "c"]:
        limiter.allow(key)
    clock.now += 11
    limiter.allow("d")
    assert limiter.tracked_clients() == 1


def test_tracked_clients_are_capped():
    limiter = SlidingWindowRateLimiter(5, 10, max_clients=3, clock=FakeClock())
    for i in range(10):
        limiter.allow(f"client-{i}")
    assert limiter.tracked_clients() == 3


def test_cap_evicts_least_recently_seen():
    limiter = SlidingWindowRateLimiter(1, 10, max_clients=2, clock=FakeClock())
    limiter.allow("a")
    limiter.allow("b")
    assert not limiter.allow("a")  # touches "a", so "b" is now the oldest
    limiter.allow("c")  # evicts "b"
    assert not limiter.allow("a")  # "a" kept its history
    assert limiter.allow("b")  # "b" started fresh


@pytest.mark.parametrize("threads", [8])
def test_thread_safe_under_contention(threads):
    limiter = SlidingWindowRateLimiter(50, 60)
    allowed = []
    lock = threading.Lock()

    def worker():
        for _ in range(25):
            if limiter.allow("shared"):
                with lock:
                    allowed.append(1)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert len(allowed) == 50
