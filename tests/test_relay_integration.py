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
    assert any(hit["path"] == "/v1/events" and hit["token"] == TOKEN for hit in ck.hits)
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
    assert failed["backoffs"] == 0
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
    assert stats["backoffs"] == 2
    assert stats["last_backoff_s"] == pytest.approx(0.1)
    assert stats["timeouts"] + stats["transport_errors"] >= 2
    assert elapsed < 5


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
    assert stats["mood_mirrored"] == 1
    assert elapsed < 5
    # The unread finding is not acked, so a later cycle still delivers it.
    av.delay.clear()
    monkeypatch.setenv("FUSION_TIMEOUT", "0.4")
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 1
