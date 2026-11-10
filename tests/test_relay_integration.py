"""Relay against in-process stand-ins of the CONTRACT.md routes.

The stubs speak the authenticated, size-capped blackboard and events
contract. They are not Avrone and they are not CycleKernal.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from bridge.avrone_cyclekernal_link import relay_loop
from bridge.contract import EventLog
from tests.relay_stubs import ContractStub

TOKEN = "fusion-test-token"


def _finding(pid: str = "find0000001", text: str = "heat on the cone") -> dict:
    return {
        "id": pid,
        "run_id": "run-1",
        "agent": "scout",
        "role": "agent",
        "kind": "finding",
        "text": text,
        "refs": [],
        "ts": 1700000000.0,
        "origin_node": "local",
    }


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _raw_post(url: str, data: bytes, token: str | None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def peers(tmp_path, monkeypatch):
    av = ContractStub("avrone", TOKEN)
    ck = ContractStub("ck", TOKEN)
    av.start()
    ck.start()
    monkeypatch.setenv("AVRONE_BASE", av.base)
    monkeypatch.setenv("CK_BASE", ck.base)
    monkeypatch.setenv("FUSION_TOKEN", TOKEN)
    monkeypatch.delenv("AVRONE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("CK_FUSION_TOKEN", raising=False)
    monkeypatch.setenv("FUSION_TIMEOUT", "0.4")
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.01")
    monkeypatch.setenv("FUSION_BACKOFF_MAX", "0.08")
    monkeypatch.setenv("FUSION_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("FUSION_INTERVAL", "0")
    try:
        yield av, ck
    finally:
        av.stop()
        ck.stop()


def test_reference_stub_enforces_auth_size_and_shape(peers):
    av, ck = peers
    big = b'{"text":"' + (b"a" * 5000) + b'"}'
    status, _ = _raw_post(av.base + "/api/blackboard", big, "wrong-token")
    assert status == 413  # size is refused before the token is checked
    assert av.board.posts == {}

    status, _ = _raw_post(av.base + "/api/blackboard", b'{"agent":"a","kind":"finding","text":"hi"}', "nope")
    assert status == 401
    status, _ = _raw_post(av.base + "/api/blackboard", b"{", TOKEN)
    assert status == 400
    status, _ = _raw_post(
        av.base + "/api/blackboard",
        b'{"agent":"a","kind":"mood","text":"not a board kind"}',
        TOKEN,
    )
    assert status == 400

    body = b'{"id":"post0000001a","agent":"a","kind":"finding","text":"hi","run_id":"r"}'
    status, raw = _raw_post(av.base + "/api/blackboard", body, TOKEN)
    assert status == 200 and json.loads(raw)["duplicate"] is False
    status, raw = _raw_post(av.base + "/api/blackboard", body, TOKEN)
    assert status == 200 and json.loads(raw)["duplicate"] is True
    assert len(av.board.posts) == 1

    req = urllib.request.Request(av.base + "/api/blackboard")
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(req, timeout=2)
    assert denied.value.code == 401
    with urllib.request.urlopen(av.base + "/api/status", timeout=2) as resp:
        assert resp.status == 200
    with urllib.request.urlopen(ck.base + "/health", timeout=2) as resp:
        assert resp.status == 200
    with urllib.request.urlopen(ck.base + "/v1/current_mood", timeout=2) as resp:
        assert resp.status == 200

    event = b'{"id":"evt000000001","source":"avrone","kind":"finding","run_id":"r","payload":{"text":"hi"},"ts":1}'
    status, raw = _raw_post(ck.base + "/v1/events", event, None)
    assert status == 401
    status, raw = _raw_post(ck.base + "/v1/events", b"{", TOKEN)
    assert status == 400
    status, raw = _raw_post(ck.base + "/v1/events", event, TOKEN)
    assert status == 200 and json.loads(raw)["duplicate"] is False
    status, raw = _raw_post(ck.base + "/v1/events", event, TOKEN)
    assert json.loads(raw)["duplicate"] is True
    assert len(ck.log.list()) == 1
    status, _ = _raw_post(ck.base + "/v1/events", b'{"id":"evt000000002","source":"nope","kind":"finding","run_id":"r","payload":{}}', TOKEN)
    assert status == 400


def test_happy_path_forwards_once_and_mirrors(peers):
    av, ck = peers
    av.inject_post(_finding())
    stats = relay_loop(interval=0, max_cycles=3)
    assert stats["errors"] == 0, stats
    assert stats["forwarded"] == 1
    assert stats["mood_mirrored"] == 1
    assert stats["health_mirrored"] == 1
    assert stats["backoffs"] == 0
    assert stats["cycles"] == 3
    assert len(ck.log.list()) == 1
    event = ck.log.list()[0]
    assert event["source"] == "avrone"
    assert event["id"] == "find0000001"
    assert event["payload"]["text"] == "heat on the cone"
    posts = av.board.export()
    assert len(posts) == 3
    mirrors = [post for post in posts if post["origin_node"] == "fusion-bridge"]
    assert {post["kind"] for post in mirrors} == {"conclusion", "finding"}
    assert all(post["role"] == "cyclekernal-relay" for post in mirrors)
    assert any(
        hit["path"] == "/v1/events" and hit["token"] == TOKEN and hit["idempotency"] == "avrone:find0000001"
        for hit in ck.hits
    )
    mirror_keys = [hit["idempotency"] for hit in av.hits if hit["method"] == "POST"]
    assert mirror_keys
    assert all(key.startswith(("ck-mood-", "ck-health-")) for key in mirror_keys)
    assert any(hit["path"] == "/api/blackboard" and hit["method"] == "GET" and hit["token"] == TOKEN for hit in av.hits)
    assert any(hit["path"] == "/v1/current_mood" and not hit["token"] for hit in ck.hits)
    assert all(len(body) <= 4096 for _, body in ck.bodies)


def test_dedup_and_skips_malformed_entries(peers):
    av, ck = peers
    first = _finding("find0000001", "one")
    second = _finding("find0000002", "two")
    weird = _finding("find0000003", "coerced")
    weird["kind"] = "note"
    av.raw["/api/blackboard"] = json.dumps(
        {"posts": [first, dict(first), "nope", second, weird], "count": 5}
    ).encode()
    stats = relay_loop(interval=0, max_cycles=3)
    assert stats["forwarded"] == 3
    assert stats["malformed"] >= 1
    assert len(ck.log.list()) == 3
    kinds = {event["id"]: event["kind"] for event in ck.log.list()}
    assert kinds["find0000003"] == "finding"


def test_oversize_upstream_text_is_shrunk_before_send(peers):
    av, ck = peers
    av.inject_post(_finding(text="A" * 20000))
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    sent = [body for path, body in ck.bodies if path == "/v1/events"]
    assert sent and all(len(body) <= 4096 for body in sent)
    stored = ck.log.list()[0]["payload"]["text"]
    assert len(stored) < 20000
    assert stored.endswith("...")


def test_bad_auth_is_not_remembered_as_delivered(peers, monkeypatch):
    av, ck = peers
    av.inject_post(_finding())
    monkeypatch.setenv("FUSION_TOKEN", "wrong")
    denied = relay_loop(interval=0, max_cycles=1)
    assert denied["forwarded"] == 0
    assert denied["auth_failures"] >= 1
    assert ck.log.list() == []
    monkeypatch.setenv("FUSION_TOKEN", TOKEN)
    allowed = relay_loop(interval=0, max_cycles=1)
    assert allowed["forwarded"] == 1
    assert len(ck.log.list()) == 1


def test_one_sided_auth_failure_still_mirrors(peers, monkeypatch):
    av, ck = peers
    av.inject_post(_finding())
    monkeypatch.setenv("FUSION_TOKEN", "")
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("CK_FUSION_TOKEN", "nope")
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 0
    assert stats["auth_failures"] >= 1
    assert stats["mood_mirrored"] == 1
    assert ck.log.list() == []
    assert any(post["origin_node"] == "fusion-bridge" for post in av.board.export())


def test_upstream_500_does_not_ack_and_later_succeeds(peers):
    av, ck = peers
    av.inject_post(_finding())
    ck.fail["/v1/events"] = 500
    failed = relay_loop(interval=0, max_cycles=1)
    assert failed["forwarded"] == 0
    assert failed["upstream_errors"] >= 1
    assert failed["mood_mirrored"] == 1
    assert failed["backoffs"] == 1
    assert ck.log.list() == []
    ck.fail.clear()
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 1
    assert len(ck.log.list()) == 1


def test_permanent_413_is_not_retried(peers):
    av, ck = peers
    av.inject_post(_finding())
    ck.fail["/v1/events"] = 413
    first = relay_loop(interval=0, max_cycles=2)
    assert first["forwarded"] == 0
    assert first["rejected"] >= 1
    ck.fail.clear()
    second = relay_loop(interval=0, max_cycles=1)
    assert second["forwarded"] == 0
    assert ck.log.list() == []


def test_malformed_board_does_not_block_the_mood_mirror(peers):
    av, ck = peers
    av.raw["/api/blackboard"] = b"{"
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["malformed"] >= 1
    assert stats["forwarded"] == 0
    assert stats["mood_mirrored"] == 1
    assert stats["backoffs"] == 0


def test_mood_change_posts_again_and_health_clock_does_not(peers):
    av, ck = peers
    first = relay_loop(interval=0, max_cycles=2)
    assert first["mood_mirrored"] == 1
    assert first["health_mirrored"] == 1
    before = len(av.board.export())
    ck.mood["mood"] = "survival"
    ck.mood["mood_dot"] = 0.1
    second = relay_loop(interval=0, max_cycles=1)
    assert second["mood_mirrored"] == 1
    assert second["health_mirrored"] == 0
    assert len(av.board.export()) == before + 1


def test_restart_dedups_and_corrupt_state_recovers(peers, tmp_path):
    av, ck = peers
    av.inject_post(_finding())
    first = relay_loop(interval=0, max_cycles=1)
    assert first["forwarded"] == 1
    assert len(ck.log.list()) == 1
    assert len(av.board.export()) == 3
    second = relay_loop(interval=0, max_cycles=1)
    assert second["forwarded"] == 0
    assert len(ck.log.list()) == 1
    assert len(av.board.export()) == 3

    state = tmp_path / "state.json"
    state.unlink()
    replayed = relay_loop(interval=0, max_cycles=1)
    assert replayed["forwarded"] == 0
    assert replayed["duplicates"] >= 1
    assert len(ck.log.list()) == 1
    assert len(av.board.export()) == 3

    state.write_text("{{", encoding="utf-8")
    av.inject_post(_finding("find0000009", "after corrupt state"))
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["state_errors"] >= 1
    assert recovered["forwarded"] == 1
    assert any(event["id"] == "find0000009" for event in ck.log.list())


def test_peer_down_then_up_retries_unacked_post(peers):
    av, ck = peers
    av.inject_post(_finding())
    ck.accepting = False
    down = relay_loop(interval=0, max_cycles=1)
    assert down["forwarded"] == 0
    assert down["backoffs"] == 1
    assert down["transport_errors"] + down["timeouts"] >= 1
    ck.accepting = True
    up = relay_loop(interval=0, max_cycles=1)
    assert up["forwarded"] == 1, up
    assert len(ck.log.list()) == 1


def test_dead_hop_times_out_or_refuses_and_backs_off(tmp_path, monkeypatch):
    av_port = _closed_port()
    ck_port = _closed_port()
    monkeypatch.setenv("AVRONE_BASE", f"http://127.0.0.1:{av_port}")
    monkeypatch.setenv("CK_BASE", f"http://127.0.0.1:{ck_port}")
    monkeypatch.setenv("FUSION_TOKEN", TOKEN)
    monkeypatch.setenv("FUSION_TIMEOUT", "0.3")
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.05")
    monkeypatch.setenv("FUSION_BACKOFF_MAX", "0.2")
    monkeypatch.setenv("FUSION_STATE_PATH", str(tmp_path / "state.json"))
    started = time.monotonic()
    stats = relay_loop(interval=0, max_cycles=2)
    elapsed = time.monotonic() - started
    assert stats["forwarded"] == 0
    # The first cycle pays for one refused connection per host and backs off.
    # The second cycle is inside the breaker cooldown, so it does not dial again.
    assert stats["backoffs"] == 1
    assert stats["last_backoff_s"] == pytest.approx(0.05)
    assert stats["circuit_opened"] >= 1
    assert stats["circuit_skipped"] >= 1
    assert stats["timeouts"] + stats["transport_errors"] >= 2
    assert elapsed < 2


def test_blackboard_read_timeout_backs_off_and_still_mirrors(peers, monkeypatch):
    av, ck = peers
    av.inject_post(_finding())
    av.delay["GET /api/blackboard"] = 1.0
    monkeypatch.setenv("FUSION_TIMEOUT", "0.2")
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.01")
    started = time.monotonic()
    stats = relay_loop(interval=0, max_cycles=1)
    elapsed = time.monotonic() - started
    assert stats["timeouts"] >= 1, stats
    assert stats["backoffs"] == 1
    assert stats["forwarded"] == 0
    # Avrone already failed to answer. Don't spend another timeout posting the mirror there.
    assert stats["mood_mirrored"] == 0
    assert stats["circuit_skipped"] >= 1
    assert elapsed < 2
    # The unread finding is not acked, so a later cycle still delivers it.
    av.delay.clear()
    monkeypatch.setenv("FUSION_TIMEOUT", "0.4")
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 1
    assert recovered["mood_mirrored"] == 1


def test_cyclekernal_mirrors_are_not_forwarded(peers):
    """Health mirrors must not be posted back to CycleKernal, even if the board drops origin."""
    av, ck = peers
    av.inject_post(_finding())
    av.inject_post({
        "id": "ck-health-deadbeef0001",
        "agent": "cyclekernal:health",
        "role": "",
        "kind": "finding",
        "text": '{"status":"ghost-online","version":"3.1.0"}',
        "run_id": "fusion-relay",
        "origin_node": "local",
        "refs": [],
        "ts": 1.0,
    })
    av.inject_post({
        "id": "srvassigned01",
        "agent": "unknown",
        "role": "agent",
        "kind": "finding",
        "text": json.dumps({"status": "ghost-online", "agent": "cyclekernal:health"}),
        "run_id": "run-1",
        "origin_node": "local",
        "refs": [],
        "ts": 1.0,
    })
    stats = relay_loop(interval=0, max_cycles=3)
    assert stats["forwarded"] == 1
    assert [event["id"] for event in ck.log.list()] == ["find0000001"]
    ours = [post for post in av.board.export() if post.get("origin_node") == "fusion-bridge"]
    assert ours
    assert all("cyclekernal:" in post["text"] for post in ours)


def test_black_hole_host_does_not_cost_a_timeout_per_event(peers, monkeypatch):
    av, ck = peers
    for index in range(21):
        av.inject_post(_finding(f"find{index:08d}", f"event {index}"))
    for path in ("/v1/events", "/v1/current_mood", "/health"):
        ck.delay[path] = 30.0
    monkeypatch.setenv("FUSION_TIMEOUT", "0.3")
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "10")
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "1")
    monkeypatch.setenv("FUSION_BREAKER_COOLDOWN", "30")
    started = time.monotonic()
    stats = relay_loop(interval=0, max_cycles=2)
    elapsed = time.monotonic() - started
    event_hits = [hit for hit in ck.hits if hit["path"] == "/v1/events"]
    assert len(event_hits) == 1, (len(event_hits), stats)
    assert event_hits[0]["idempotency"] == "avrone:find00000000"
    assert stats["forwarded"] == 0
    assert stats["timeouts"] == 1
    assert stats["circuit_opened"] == 1
    assert stats["circuit_skipped"] >= 1
    # 21 events at 0.3s would be over 6s. One attempt plus the cooldown skip is not.
    assert elapsed < 1.5, elapsed
    for path in ("/v1/events", "/v1/current_mood", "/health"):
        ck.delay.pop(path, None)
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 21
    assert len(ck.log.list()) == 21


def test_cycle_deadline_stops_a_hang_when_the_breaker_is_off(peers, monkeypatch):
    av, ck = peers
    for index in range(21):
        av.inject_post(_finding(f"due{index:08d}", f"due {index}"))
    ck.delay["/v1/events"] = 30.0
    monkeypatch.setenv("FUSION_TIMEOUT", "5")
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "0.4")
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "0")
    started = time.monotonic()
    stats = relay_loop(interval=0, max_cycles=1)
    elapsed = time.monotonic() - started
    event_hits = [hit for hit in ck.hits if hit["path"] == "/v1/events"]
    assert len(event_hits) == 1, (len(event_hits), stats)
    assert stats["deadline_stops"] >= 1
    assert stats["forwarded"] == 0
    assert elapsed < 1.5, elapsed


def _body_of_size(n: int) -> bytes:
    prefix = b'{"agent":"a","kind":"finding","run_id":"r","text":"'
    suffix = b'"}'
    fill = n - len(prefix) - len(suffix)
    assert fill > 0
    raw = prefix + (b"a" * fill) + suffix
    assert len(raw) == n
    return raw


def test_http_cap_accepts_4096_and_rejects_4097(peers):
    av, _ck = peers
    ok, _ = _raw_post(av.base + "/api/blackboard", _body_of_size(4096), TOKEN)
    assert ok == 200
    over, _ = _raw_post(av.base + "/api/blackboard", _body_of_size(4097), TOKEN)
    assert over == 413
    assert len(av.board.posts) == 1


def test_four_thousand_char_text_is_sent_under_the_byte_cap(peers):
    av, ck = peers
    av.inject_post(_finding("find00004000", "a" * 4000))
    av.inject_post(_finding("find0000emoji", "😀" * 1500))
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 2, stats
    sent = [body for path, body in ck.bodies if path == "/v1/events"]
    assert sent and all(len(body) <= 4096 for body in sent)
    stored = {event["id"]: event["payload"]["text"] for event in ck.log.list()}
    assert stored["find00004000"].endswith("...") and len(stored["find00004000"]) < 4000
    assert "😀" in stored["find0000emoji"]
    assert len(stored["find0000emoji"].encode("utf-8")) < len(("😀" * 1500).encode("utf-8"))


def test_boot_change_resends_what_the_restarted_peer_lost(peers):
    av, ck = peers
    av.inject_post(_finding())
    first = relay_loop(interval=0, max_cycles=1)
    assert first["forwarded"] == 1
    assert first["mood_mirrored"] == 1
    assert first["reconciled"] == 0

    with ck.lock:
        ck.boot = "boot-2"
        ck.log = EventLog()
    resent = relay_loop(interval=0, max_cycles=1)
    assert resent["reconciled"] >= 1
    assert resent["forwarded"] == 1
    assert [event["id"] for event in ck.log.list()] == ["find0000001"]
    held = relay_loop(interval=0, max_cycles=1)
    assert held["forwarded"] == 0
    assert held["reconciled"] == 0
    assert len(ck.log.list()) == 1

    with av.lock:
        av.boot = "boot-9"
        av.board.posts = type(av.board.posts)(
            (pid, post)
            for pid, post in av.board.posts.items()
            if not str(post.get("agent", "")).startswith("cyclekernal:")
        )
    mirrored = relay_loop(interval=0, max_cycles=1)
    assert mirrored["reconciled"] >= 1
    assert mirrored["mood_mirrored"] == 1
    assert mirrored["forwarded"] == 0
    assert len(ck.log.list()) == 1
    assert any(post.get("origin_node") == "fusion-bridge" for post in av.board.export())
