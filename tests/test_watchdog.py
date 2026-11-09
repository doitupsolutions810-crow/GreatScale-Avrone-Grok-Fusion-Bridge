"""Backoff math and stale-health detection, without waiting on the 2s loop."""
from __future__ import annotations

from bridge.watchdog import StaleTracker, Watchdog


def test_backoff_doubles():
    wd = Watchdog(interval=2.0, probe=lambda _url: (False, None))
    wd.register("svc", "http://127.0.0.1/health", ["/bin/true"])
    wd.tick(0)
    wd.tick(2)
    wd.tick(4)
    wd.tick(8)
    trace = wd.services["svc"].backoff_trace
    assert trace[:4] == [1.0, 2.0, 4.0, 8.0]


def test_frozen_health_is_stale():
    body = {"uptime_s": 1.0, "restarts": 0, "started_at": "frozen"}
    wd = Watchdog(interval=2.0, probe=lambda _url: (True, dict(body)))
    wd.register("svc", "http://127.0.0.1/health", ["/bin/true"])
    wd.tick(0)
    assert wd.services["svc"].restarts == 0
    wd.tick(2.0)
    assert wd.services["svc"].restarts == 1
    assert wd.services["svc"].last_error == "stale-health"


def test_moving_uptime_is_not_stale():
    tracker = StaleTracker(gap=1.5)
    assert tracker.observe("a", {"uptime_s": 1, "restarts": 0, "started_at": "t"}, 0) is False
    assert tracker.observe("a", {"uptime_s": 3, "restarts": 0, "started_at": "t"}, 2) is False
