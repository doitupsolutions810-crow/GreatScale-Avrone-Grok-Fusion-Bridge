"""Boolean flags, source whitespace, and request framing.

These stay short. The recorded 60 second soak is not repeated.
"""
from __future__ import annotations

import socket

from bridge.avrone_cyclekernal_link import relay_loop
from tests.test_relay_integration import TOKEN, _finding


def _http(port: int, request: bytes) -> bytes:
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.settimeout(2)
    try:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        sock.close()


def test_padded_source_matches_and_a_different_name_does_not(peers):
    av, ck = peers
    av.inject_post(_finding("pad000000001", "ours"))
    ck.events_script = [{
        "status": 200,
        "body": {"accepted": True, "duplicate": True, "source": " avrone "},
    }]
    same = relay_loop(interval=0, max_cycles=1)
    assert same["conflicts"] == 0, same
    assert same["duplicates"] == 1
    assert same["forwarded"] == 0
    again = relay_loop(interval=0, max_cycles=1)
    assert again["forwarded"] == 0
    assert ck.log.list() == []

    av.inject_post(_finding("pad000000002", "other"))
    ck.events_script = [{
        "status": 200,
        "body": {"accepted": True, "duplicate": True, "source": " greatscale "},
    }]
    other = relay_loop(interval=0, max_cycles=1)
    assert other["conflicts"] == 1, other
    assert other["forwarded"] == 0
    assert not any(event["id"] == "pad000000002" for event in ck.log.list())
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 1, recovered
    stored = next(event for event in ck.log.list() if event["id"] == "pad000000002")
    assert stored["source"] == "avrone"


def test_string_flags_are_not_booleans(peers):
    av, ck = peers
    av.inject_post(_finding("flag00000001", "string false"))
    ck.events_script = [{
        "status": 200,
        "body": {"accepted": "false", "duplicate": False},
    }]
    bad = relay_loop(interval=0, max_cycles=1)
    assert bad["forwarded"] == 0
    assert bad["malformed"] >= 1
    assert bad["conflicts"] == 0
    assert ck.log.list() == []
    delivered = relay_loop(interval=0, max_cycles=1)
    assert delivered["forwarded"] == 1

    av.inject_post(_finding("flag00000002", "string true"))
    ck.events_script = [{
        "status": 200,
        "body": {"accepted": True, "duplicate": "true", "source": "greatscale"},
    }]
    quoted = relay_loop(interval=0, max_cycles=1)
    assert quoted["conflicts"] == 0
    assert quoted["malformed"] >= 1
    assert not any(event["id"] == "flag00000002" for event in ck.log.list())
    stored = relay_loop(interval=0, max_cycles=1)
    assert stored["forwarded"] == 1
    assert any(event["id"] == "flag00000002" and event["source"] == "avrone" for event in ck.log.list())


def test_204_is_an_ack(peers):
    av, ck = peers
    av.inject_post(_finding("empty2040001", "no body"))
    ck.events_script = [{"status": 204, "raw": b""}]
    acked = relay_loop(interval=0, max_cycles=1)
    assert acked["forwarded"] == 1, acked
    assert ck.log.list() == []
    later = relay_loop(interval=0, max_cycles=1)
    assert later["forwarded"] == 0
    assert ck.log.list() == []


def test_ambiguous_framing_is_rejected_without_reading_a_huge_body(peers):
    _av, ck = peers
    event = (
        '{"id":"frame0000001","source":"avrone","kind":"finding",'
        '"run_id":"r","payload":{"text":"no"},"ts":1}'
    )
    double = (
        "POST /v1/events HTTP/1.0\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Length: 4\r\n"
        "Content-Length: 4\r\n"
        "Content-Type: application/json\r\n"
        f"X-Admin-Token: {TOKEN}\r\n"
        "\r\n"
        + event
    )
    chunked = (
        "POST /v1/events HTTP/1.0\r\n"
        "Host: 127.0.0.1\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Content-Type: application/json\r\n"
        f"X-Admin-Token: {TOKEN}\r\n"
        "\r\n"
        "0\r\n\r\n"
    )
    huge = (
        "POST /v1/events HTTP/1.0\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Length: 999999999999\r\n"
        "Content-Type: application/json\r\n"
        f"X-Admin-Token: {TOKEN}\r\n"
        "\r\n"
    )
    for request, marker in ((double, b" 400 "), (chunked, b" 400 "), (huge, b" 413 ")):
        raw = _http(ck.port, request.encode())
        status = raw.split(b"\r\n", 1)[0]
        assert marker in status, status
    assert ck.log.list() == []
