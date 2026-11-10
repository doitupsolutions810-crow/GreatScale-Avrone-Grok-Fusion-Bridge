"""Breaker probe, retryable 4xx, and a few more body shapes.

These stay short. The recorded 60 second soak is not repeated.
"""
from __future__ import annotations

import json
import socket

from bridge.avrone_cyclekernal_link import _HostBreaker, _status_error, relay_loop
from tests.test_relay_integration import TOKEN, _finding


def test_probe_that_gets_an_http_answer_closes_the_circuit():
    breaker = _HostBreaker(1, 0)
    url = "http://127.0.0.1:9/v1/events"
    assert breaker.observe(url, "timeout") is True
    assert breaker.blocked(url) is False
    breaker.arm_probe(url)
    assert breaker.blocked(url) is True
    assert breaker.observe(url, "upstream") is False
    assert breaker.blocked(url) is False
    # A 500 while the circuit is closed does not open it.
    assert breaker.observe(url, "upstream") is False
    assert breaker.blocked(url) is False


def test_threshold_two_needs_two_failures():
    breaker = _HostBreaker(2, 30)
    url = "http://ck/v1/events"
    assert breaker.observe(url, "timeout") is False
    assert breaker.blocked(url) is False
    assert breaker.observe(url, "transport") is True
    assert breaker.blocked(url) is True
    assert _status_error(408) == "upstream"
    assert _status_error(425) == "upstream"
    assert _status_error(400) == "rejected"


def test_dropped_probe_then_500_then_delivery(peers, monkeypatch):
    """One relay_loop, three cycles. A dropped dial must not stick the breaker."""
    av, ck = peers
    monkeypatch.setenv("FUSION_TIMEOUT", "0.4")
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "1")
    monkeypatch.setenv("FUSION_BREAKER_COOLDOWN", "0.05")
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.05")
    monkeypatch.setenv("FUSION_BACKOFF_MAX", "0.05")
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "5")
    ck.events_script = [{"drop": True}, {"status": 500}]
    av.inject_post(_finding("probe0000001", "after the probe"))
    stats = relay_loop(interval=0, max_cycles=3)
    assert stats["circuit_opened"] >= 1, stats
    assert stats["forwarded"] == 1, stats
    assert [event["id"] for event in ck.log.list()] == ["probe0000001"]
    assert stats["circuit_skipped"] >= 1


def test_408_and_html_200_are_not_acks(peers):
    av, ck = peers
    av.inject_post(_finding("http40800001", "retry"))
    ck.events_script = [{"status": 408}]
    waiting = relay_loop(interval=0, max_cycles=1)
    assert waiting["forwarded"] == 0
    assert waiting["upstream_errors"] >= 1
    assert ck.log.list() == []
    delivered = relay_loop(interval=0, max_cycles=1)
    assert delivered["forwarded"] == 1

    av.inject_post(_finding("html00000001", "not html"))
    ck.events_script = [{"status": 200, "raw": "<html>nope</html>"}]
    junk = relay_loop(interval=0, max_cycles=1)
    assert junk["forwarded"] == 0
    assert junk["malformed"] >= 1
    assert not any(event["id"] == "html00000001" for event in ck.log.list())
    recovered = relay_loop(interval=0, max_cycles=1)
    assert any(event["id"] == "html00000001" for event in ck.log.list())
    assert recovered["forwarded"] == 1


def test_negative_content_length_is_not_read_as_the_body(peers):
    _av, ck = peers
    sock = socket.create_connection(("127.0.0.1", ck.port), timeout=2)
    try:
        payload = (
            '{"id":"neg000000001","source":"avrone","kind":"finding",'
            '"run_id":"r","payload":{"text":"no"},"ts":1}'
        )
        request = (
            "POST /v1/events HTTP/1.0\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Length: -1\r\n"
            "Content-Type: application/json\r\n"
            f"X-Admin-Token: {TOKEN}\r\n"
            "\r\n"
            + payload
        )
        sock.sendall(request.encode())
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    status = data.split(b"\r\n", 1)[0]
    assert b" 400 " in status
    assert ck.log.list() == []


def test_board_shapes_and_loose_kinds(peers):
    av, ck = peers
    av.raw["/api/blackboard"] = b'{"posts":[],"count":0,"boot":"boot-1"}'
    empty = relay_loop(interval=0, max_cycles=1)
    assert empty["forwarded"] == 0
    assert empty["malformed"] == 0

    av.raw["/api/blackboard"] = b'{"posts":null,"count":0,"boot":"boot-1"}'
    missing = relay_loop(interval=0, max_cycles=1)
    assert missing["malformed"] >= 1
    assert missing["forwarded"] == 0
    av.raw.pop("/api/blackboard", None)

    loose = _finding("kind00000001", "kept")
    loose["kind"] = " FINDING "
    listed = _finding("list00000001", "from text")
    listed["payload"] = ["not", "an", "object"]
    av.inject_post(loose)
    av.inject_post(listed)
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 2, stats
    by_id = {event["id"]: event for event in ck.log.list()}
    assert by_id["kind00000001"]["kind"] == "finding"
    assert by_id["list00000001"]["payload"]["text"] == "from text"
    assert json.dumps(by_id["list00000001"]["payload"])
