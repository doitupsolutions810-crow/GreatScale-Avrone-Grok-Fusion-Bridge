"""More edge cases after the source-scoped dedup pass.

These stay short. The recorded 60 second soak is not repeated.
"""
from __future__ import annotations

import json

import pytest

from bridge.avrone_cyclekernal_link import _idempotency_key, relay_loop
from bridge.contract import dumps_utf8, encode_bounded
from bridge.strict_fields import FieldError, heat_from_wire
from tests.test_relay_integration import TOKEN, _finding, _raw_post


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(FieldError):
        heat_from_wire('{"heat": 0.1, "heat": 1e-400}')
    with pytest.raises(FieldError):
        heat_from_wire('{"heat": 0.2, "meta": {"a": 1, "a": 2}}')
    kept = heat_from_wire('{"heat": 1e-10}')
    assert kept["underflow"] is False


def test_retry_after_bounds_and_idempotency_newline():
    from bridge.strict_fields import parse_retry_after

    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("30.0") == 30.0
    assert parse_retry_after("30.1") is None
    assert parse_retry_after("1e1") is None
    assert parse_retry_after(" 0 ") == 0.0
    assert _idempotency_key("avrone:bad\r\nX-Admin-Token: x") == ""
    assert _idempotency_key("avrone:ok-id") == "avrone:ok-id"


def test_emoji_clip_stays_valid_utf8_where_a_byte_slice_does_not():
    emoji = "😀"
    body = {
        "id": "emoji0000001",
        "source": "avrone",
        "kind": "finding",
        "run_id": "r",
        "payload": {"text": ("a" * 80) + (emoji * 800)},
        "ts": 1,
    }
    full = dumps_utf8(body)
    start = full.find(emoji.encode())
    assert start > 0
    cut = start + 1
    with pytest.raises(UnicodeDecodeError):
        full[:cut].decode("utf-8")
    fitted = encode_bounded(body, max(cut, 256))
    assert fitted is not None
    assert len(fitted) <= max(cut, 256)
    fitted.decode("utf-8")
    assert b"\xed\xa0" not in fitted
    with pytest.raises(ValueError):
        dumps_utf8({"ts": float("nan")})
    assert encode_bounded({"id": "n", "ts": float("inf")}, 4096) is None


def test_duplicate_key_on_the_wire_is_400(peers):
    _av, ck = peers
    body = (
        b'{"id":"dupkey000001","source":"avrone","kind":"finding",'
        b'"run_id":"r","payload":{"heat":0.1,"heat":0.9},"ts":1}'
    )
    status, raw = _raw_post(ck.base + "/v1/events", body, TOKEN)
    assert status == 400
    assert b"JSON" in raw
    assert ck.log.list() == []


def test_empty_payload_does_not_hide_text(peers):
    av, ck = peers
    post = _finding("empty0000001", "keep this text")
    post["payload"] = {}
    av.inject_post(post)
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    stored = next(event for event in ck.log.list() if event["id"] == "empty0000001")
    assert stored["payload"]["text"] == "keep this text"


def test_non_finite_timestamp_is_not_sent_as_nan(peers):
    av, ck = peers
    post = _finding("nan000000001", "finite body")
    post["ts"] = float("nan")
    av.inject_post(post)
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    raw = b"".join(body for path, body in ck.bodies if path == "/v1/events")
    assert b"NaN" not in raw
    assert b"Infinity" not in raw


def test_non_finite_and_deep_payload_are_rejected(peers):
    av, ck = peers
    bad = _finding("inf000000001", "drop")
    bad["payload"] = {"score": float("inf"), "text": "drop"}
    av.inject_post(bad)
    nested = 1
    for _ in range(40):
        nested = {"a": nested}
    deep = _finding("deep00000001", "drop")
    deep["payload"] = nested
    av.inject_post(deep)
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["rejected"] == 2, stats
    assert stats["forwarded"] == 0
    assert ck.log.list() == []


def test_null_seen_and_bad_cursor_do_not_drop_a_real_id(peers, tmp_path):
    av, ck = peers
    (tmp_path / "state.json").write_text(json.dumps({
        "version": 2,
        "seen": [None, True, "already-sent"],
        "cursor": "bad cursor\n",
        "boots": {"avrone": None, "cyclekernal": "boot-1"},
    }), encoding="utf-8")
    av.inject_post(_finding("None", "not swallowed"))
    av.inject_post(_finding("already-sent", "old"))
    stats = relay_loop(interval=0, max_cycles=1)
    ids = [event["id"] for event in ck.log.list()]
    assert "None" in ids
    assert "already-sent" not in ids
    assert stats["state_errors"] == 0

    (tmp_path / "state.json").write_bytes(b"\xff\xfe{")
    av.inject_post(_finding("utf800000001", "after bad bytes"))
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["state_errors"] == 1
    assert any(event["id"] == "utf800000001" for event in ck.log.list())


def test_state_directory_still_forwards(peers, tmp_path, monkeypatch):
    blocked = tmp_path / "state-dir"
    blocked.mkdir()
    monkeypatch.setenv("FUSION_STATE_PATH", str(blocked))
    av, ck = peers
    av.inject_post(_finding("dir000000001", "from a blocked state path"))
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    assert stats["state_errors"] >= 1
    assert any(event["id"] == "dir000000001" for event in ck.log.list())


def test_lookalike_agent_is_forwarded_and_real_echo_is_not(peers):
    av, ck = peers
    real = _finding("echo00000001", "mirror")
    real["agent"] = "CycleKernal:health"
    look = _finding("look00000001", "not a mirror")
    look["agent"] = "cyclekernal\uff1ahealth"
    number = _finding("num000000001", "numeric id")
    number["id"] = 12345
    av.inject_post(real)
    av.inject_post(look)
    av.inject_post(number)
    stats = relay_loop(interval=0, max_cycles=1)
    ids = [event["id"] for event in ck.log.list()]
    assert "echo00000001" not in ids
    assert "look00000001" in ids
    assert any(event_id.startswith("h-") for event_id in ids)
    assert stats["forwarded"] == 2, stats
    again = relay_loop(interval=0, max_cycles=1)
    assert again["forwarded"] == 0


def test_conflict_does_not_block_the_next_post(peers):
    av, ck = peers
    ck.legacy_bare_id = True
    with ck.lock:
        ck.log.add({
            "id": "block0000001",
            "source": "greatscale",
            "kind": "evidence",
            "run_id": "gs",
            "payload": {"text": "greatscale copy"},
            "ts": 1.0,
        })
    av.inject_post(_finding("block0000001", "avrone copy"))
    av.inject_post(_finding("later0000001", "still goes"))
    blocked = relay_loop(interval=0, max_cycles=1)
    assert blocked["conflicts"] == 1, blocked
    assert blocked["forwarded"] == 1, blocked
    by_source = {(event["source"], event["id"]) for event in ck.log.list()}
    assert ("greatscale", "block0000001") in by_source
    assert ("avrone", "block0000001") not in by_source
    assert ("avrone", "later0000001") in by_source

    again = relay_loop(interval=0, max_cycles=1)
    assert again["conflicts"] == 1
    assert again["forwarded"] == 0
    assert sum(1 for event in ck.log.list() if event["id"] == "later0000001") == 1

    ck.legacy_bare_id = False
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 1, recovered
    texts = {
        (event["source"], event["id"]): event["payload"].get("text")
        for event in ck.log.list()
    }
    assert texts[("greatscale", "block0000001")] == "greatscale copy"
    assert texts[("avrone", "block0000001")] == "avrone copy"


def test_huge_health_body_is_not_parsed(peers):
    av, ck = peers
    ck.raw["/health"] = b'{"status":"ok","pad":"' + (b"x" * 200_000) + b'"}'
    av.inject_post(_finding("huge00000001", "still forwarded"))
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    assert stats["health_mirrored"] == 0
    assert stats["transport_errors"] >= 1
