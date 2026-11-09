import logging
import threading
from datetime import datetime, timedelta, timezone

from digital_twin.spend import CircuitBreaker, DailyBudget


class UtcClock:
    def __init__(self, start=datetime(2026, 11, 9, 23, 59, tzinfo=timezone.utc)):
        self.now = start

    def __call__(self):
        return self.now


class Ticks:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# --- DailyBudget --------------------------------------------------------------

def test_spends_up_to_limit():
    budget = DailyBudget(2, clock=UtcClock())
    assert budget.try_spend()
    assert budget.try_spend()
    assert not budget.try_spend()
    assert budget.used == 2


def test_has_remaining_does_not_spend():
    budget = DailyBudget(1, clock=UtcClock())
    assert budget.has_remaining()
    assert budget.has_remaining()
    assert budget.used == 0


def test_multi_unit_spend_is_all_or_nothing():
    budget = DailyBudget(3, clock=UtcClock())
    budget.try_spend(2)
    assert not budget.try_spend(2)
    assert budget.used == 2


def test_resets_at_utc_midnight():
    clock = UtcClock()
    budget = DailyBudget(1, clock=clock)
    budget.try_spend()
    assert not budget.has_remaining()

    clock.now += timedelta(minutes=1)  # 00:00 UTC on the 10th
    assert budget.has_remaining()
    assert budget.used == 0


def test_uses_utc_not_local_offset():
    # 23:30 at UTC-5 is already 04:30 UTC the next day
    clock = UtcClock(datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc))
    budget = DailyBudget(1, clock=clock)
    budget.try_spend()
    clock.now = datetime(2026, 11, 9, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert budget.has_remaining()


def test_logs_exhaustion_once_per_day(caplog):
    clock = UtcClock()
    budget = DailyBudget(1, clock=clock)
    budget.try_spend()
    with caplog.at_level(logging.WARNING, logger="digital_twin.spend"):
        for _ in range(5):
            budget.try_spend()
            budget.has_remaining()
        assert caplog.text.count("guard_daily_budget_exhausted") == 1

        clock.now += timedelta(days=1)
        budget.try_spend()
        budget.try_spend()
        assert caplog.text.count("guard_daily_budget_exhausted") == 2


def test_budget_thread_safe():
    budget = DailyBudget(100)
    spent = []
    lock = threading.Lock()

    def worker():
        for _ in range(50):
            if budget.try_spend():
                with lock:
                    spent.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(spent) == 100


# --- CircuitBreaker -------------------------------------------------------------

def _opened(threshold=2, timeout=10):
    clock = Ticks()
    breaker = CircuitBreaker(threshold, timeout, clock=clock)
    for _ in range(threshold):
        breaker.record_failure()
    return breaker, clock


def test_opens_after_consecutive_failures():
    breaker = CircuitBreaker(2, 10, clock=Ticks())
    breaker.record_failure()
    assert breaker.can_proceed()
    breaker.record_failure()
    assert not breaker.can_proceed()


def test_success_resets_failure_count():
    breaker = CircuitBreaker(2, 10, clock=Ticks())
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state is CircuitBreaker.State.CLOSED


def test_stays_open_until_timeout():
    breaker, clock = _opened()
    clock.now = 9.9
    assert not breaker.can_proceed()


def test_half_open_admits_exactly_one_probe():
    breaker, clock = _opened()
    clock.now = 10
    assert breaker.can_proceed()
    assert breaker.state is CircuitBreaker.State.HALF_OPEN
    assert not breaker.can_proceed()
    assert not breaker.can_proceed()


def test_probe_success_closes():
    breaker, clock = _opened()
    clock.now = 10
    breaker.can_proceed()
    breaker.record_success()
    assert breaker.state is CircuitBreaker.State.CLOSED
    assert breaker.can_proceed()


def test_probe_failure_reopens_for_a_full_timeout():
    breaker, clock = _opened()
    clock.now = 10
    breaker.can_proceed()
    breaker.record_failure()
    assert breaker.state is CircuitBreaker.State.OPEN
    clock.now = 19
    assert not breaker.can_proceed()
    clock.now = 20
    assert breaker.can_proceed()


def test_lost_probe_is_replaced_after_timeout():
    breaker, clock = _opened()
    clock.now = 10
    breaker.can_proceed()  # probe never reports back
    clock.now = 19
    assert not breaker.can_proceed()
    clock.now = 20
    assert breaker.can_proceed()


def test_half_open_probe_is_exclusive_across_threads():
    breaker, clock = _opened()
    clock.now = 10
    admitted = []
    lock = threading.Lock()

    def worker():
        if breaker.can_proceed():
            with lock:
                admitted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 1
