from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from bridge.avrone_cyclekernal_link import (
    FusionEvent,
    HopResult,
    post_to_avrone_bridge,
    post_to_cyclekernal,
    relay_loop,
)
from bridge.contract import (
    clip_text,
    encode_bounded,
    fingerprint,
    is_echo,
    project_health,
    project_mood,
)
from bridge.endpoint_resolver import resolve_avrone_base, resolve_ck_base
from bridge.settings import load_config

ROOT = Path(__file__).resolve().parents[1]


def _quiet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AVRONE_BASE",
        "CK_BASE",
        "FUSION_TOKEN",
        "AVRONE_ADMIN_TOKEN",
        "CK_FUSION_TOKEN",
        "FUSION_STATE_PATH",
        "FUSION_TIMEOUT",
        "FUSION_BACKOFF_BASE",
        "FUSION_BACKOFF_MAX",
        "FUSION_INTERVAL",
        "FUSION_CYCLE_DEADLINE",
        "FUSION_BREAKER_THRESHOLD",
        "FUSION_BREAKER_COOLDOWN",
        "FUSION_MAX_BODY_BYTES",
        "AVRONE_BLACKBOARD_PATH",
        "CK_EVENTS_PATH",
        "AVRONE_SHIM_BASE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_fusion_event_auto_timestamp_and_id():
    event = FusionEvent(source="avrone", kind="finding", run_id="r1", payload={"x": 1})
    assert event.ts > 0
    assert event.id.startswith("ev-")
    assert "x" in event.to_json()


def test_defaults_match_upstream_ports(monkeypatch):
    _quiet_env(monkeypatch)
    cfg = load_config()
    assert cfg.avrone_base == "http://127.0.0.1:8710"
    assert cfg.ck_base == "http://127.0.0.1:8090"
    assert cfg.blackboard_path == "/api/blackboard"
    assert cfg.events_path == "/v1/events"
    assert cfg.mood_path == "/v1/current_mood"
    assert cfg.health_path == "/health"
    assert cfg.status_path == "/api/status"
    assert cfg.max_body_bytes == 4096
    assert cfg.max_text_chars == 4000
    assert cfg.cycle_deadline == 10.0
    assert cfg.breaker_threshold == 1
    assert cfg.breaker_cooldown == 30.0


def test_path_and_token_overrides(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("CK_EVENTS_PATH", "/v1/feedback")
    monkeypatch.setenv("AVRONE_BASE", "http://avrone.internal:8710/")
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", "av")
    monkeypatch.setenv("CK_FUSION_TOKEN", "ck")
    monkeypatch.setenv("FUSION_TOKEN", "shared")
    cfg = load_config()
    assert cfg.ck_events_url == "http://127.0.0.1:8090/v1/feedback"
    assert cfg.avrone_base == "http://avrone.internal:8710"
    assert cfg.avrone_token == "av"
    assert cfg.ck_token == "ck"


def test_cyclekernal_mirror_entries_are_echoes():
    assert is_echo({"agent": "cyclekernal:health", "origin_node": "local", "role": "", "text": "x"})
    assert is_echo({"agent": "  CycleKernal:mood", "text": "x"})
    assert is_echo({"id": "ck-health-abc123", "agent": "unknown", "text": "status"})
    assert is_echo({
        "id": "srv1",
        "agent": "unknown",
        "origin_node": "local",
        "text": '{"status":"ghost-online","agent":"cyclekernal:health"}',
    })
    assert not is_echo({"id": "find0000001", "agent": "scout", "origin_node": "local", "text": "heat"})


def _event_body(text: str) -> dict:
    return {
        "id": "find0000001",
        "source": "avrone",
        "kind": "finding",
        "run_id": "run-1",
        "payload": {
            "id": "find0000001",
            "agent": "scout",
            "role": "agent",
            "kind": "finding",
            "text": text,
            "refs": [],
            "origin_node": "local",
        },
        "ts": 1700000000.0,
    }


def test_clip_and_body_cap():
    from bridge.contract import dumps_utf8

    assert clip_text("  hello  ") == "hello"
    assert clip_text("a" * 4000) == "a" * 4000
    assert clip_text("a" * 4001).endswith("...")
    assert len(clip_text("a" * 4001)) == 4000
    raw = encode_bounded({"id": "abc", "text": "x" * 10000, "payload": {"text": "y" * 10000}}, 4096)
    assert raw is not None and len(raw) <= 4096
    parsed = json.loads(raw)
    assert len(parsed["text"]) < 10000
    assert len(parsed["payload"]["text"]) < 10000

    # 4000 ASCII characters plus the event wrapper does not fit in 4096 bytes.
    wide = _event_body("a" * 4000)
    assert len(dumps_utf8(wide)) > 4096
    fitted = encode_bounded(wide, 4096)
    assert fitted is not None and len(fitted) <= 4096
    got = json.loads(fitted)["payload"]["text"]
    assert got.endswith("...") and len(got) < 4000
    longer = json.loads(fitted)
    longer["payload"] = dict(longer["payload"])
    longer["payload"]["text"] = clip_text("a" * 4000, len(got) + 1)
    assert len(dumps_utf8(longer)) > 4096

    # é is one character and two UTF-8 bytes. The cap is the serialized body.
    accented = "é" * 4000
    assert len(accented.encode("utf-8")) == 8000
    wide = _event_body(accented)
    assert len(dumps_utf8(wide)) > 4096
    fitted = encode_bounded(wide, 4096)
    assert fitted is not None and len(fitted) <= 4096
    got = json.loads(fitted)["payload"]["text"]
    assert "é" in got and len(got.encode("utf-8")) < 8000
    longer = json.loads(fitted)
    longer["payload"] = dict(longer["payload"])
    longer["payload"]["text"] = clip_text(accented, len(got) + 1)
    assert len(dumps_utf8(longer)) > 4096

    # One emoji is four UTF-8 bytes. A character clip of 4000 would be 16 KB.
    emoji = "😀" * 2000
    assert len(emoji.encode("utf-8")) == 8000
    fitted = encode_bounded(_event_body(emoji), 4096)
    assert fitted is not None and len(fitted) <= 4096
    got = json.loads(fitted)["payload"]["text"]
    assert "😀" in got
    longer = json.loads(fitted)
    longer["payload"] = dict(longer["payload"])
    longer["payload"]["text"] = clip_text(emoji, len(got) + 1)
    assert len(dumps_utf8(longer)) > 4096


def test_mood_fingerprint_ignores_timestamp_and_metrics():
    base = {
        "mood": "convergent",
        "mood_dot": 0.724,
        "ns_fitness": 0.611,
        "genome_id": "gn",
        "traits": ["Analytical/Convergent"],
        "promote": {"action": "hold"},
        "timestamp": "t1",
        "metrics": {"tick": 1},
        "explanation": "NS fitness 0.61",
    }
    other = dict(base, timestamp="t2", metrics={"tick": 99}, explanation="different")
    assert fingerprint(project_mood(base)) == fingerprint(project_mood(other))
    moved = dict(base, mood="survival", mood_dot=0.10)
    assert fingerprint(project_mood(base)) != fingerprint(project_mood(moved))
    health = {"status": "ghost-online", "version": "3.1.0", "timestamp": "t1"}
    assert fingerprint(project_health(health)) == fingerprint(project_health(dict(health, timestamp="t2")))


def test_posts_use_contract_paths_and_admin_header(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("FUSION_TOKEN", "sekret")
    seen: list[tuple] = []

    def fake(method, url, **kwargs):
        seen.append((method, url, kwargs.get("token"), kwargs.get("raw")))
        return HopResult(ok=True, status=200, body={"accepted": True, "duplicate": False})

    monkeypatch.setattr("bridge.avrone_cyclekernal_link._request", fake)
    finding = FusionEvent(source="avrone", kind="finding", run_id="r1", payload={"text": "hi"}, id="find0000001")
    mood = FusionEvent(source="cyclekernal", kind="mood", run_id="fusion-relay", payload={"mood": "neutral"}, id="ck-mood-aaaabbbbcccc")
    assert post_to_cyclekernal(finding) is True
    assert post_to_avrone_bridge(mood) is True
    assert seen[0][0] == "POST" and seen[0][1].endswith("/v1/events") and seen[0][2] == "sekret"
    assert seen[1][0] == "POST" and seen[1][1].endswith("/api/blackboard")
    posted = json.loads(seen[1][3])
    assert posted["kind"] == "conclusion"
    assert posted["role"] == "cyclekernal-relay"
    assert posted["origin_node"] == "fusion-bridge"
    assert len(seen[0][3]) <= 4096


def test_post_soft_fails(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setattr(
        "bridge.avrone_cyclekernal_link._request",
        lambda *args, **kwargs: HopResult(ok=False, error="transport"),
    )
    event = FusionEvent(source="avrone", kind="finding", run_id="r1", payload={})
    assert post_to_cyclekernal(event) is False


def test_backoff_doubles_on_timeouts_and_resets(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.5")
    monkeypatch.setenv("FUSION_BACKOFF_MAX", "8")
    sleeps: list[float] = []
    mode = {"value": "fail"}
    posts = {"n": 0}

    def fake(method, url, **kwargs):
        if mode["value"] == "fail":
            return HopResult(ok=False, error="timeout")
        if method == "GET" and "blackboard" in url:
            return HopResult(ok=True, status=200, body={"posts": [], "count": 0})
        if method == "GET":
            return HopResult(
                ok=True,
                status=200,
                body={"mood": "neutral", "mood_dot": 0.5, "status": "ghost-online", "version": "3"},
            )
        posts["n"] += 1
        if posts["n"] == 2:
            mode["value"] = "fail"
        return HopResult(ok=True, status=200, body={"posted": True, "duplicate": False, "id": "x"})

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 1:
            mode["value"] = "ok"

    monkeypatch.setattr("bridge.avrone_cyclekernal_link._request", fake)
    monkeypatch.setattr("bridge.avrone_cyclekernal_link._sleep", sleeper)
    stats = relay_loop(interval=0, max_cycles=3)
    # Cycle 1 times out (sleep 0.5). Cycle 2 is clean, so the multiplier resets.
    # Cycle 3 times out again at the base delay, not at 1.0.
    assert sleeps == [0.5, 0.5]
    assert stats["backoffs"] == 2
    assert stats["timeouts"] >= 3
    assert stats["last_backoff_s"] == 0.5


def test_missing_route_does_not_backoff(monkeypatch):
    _quiet_env(monkeypatch)
    sleeps: list[float] = []

    def fake(method, url, **kwargs):
        if method == "GET" and "blackboard" in url:
            return HopResult(ok=False, status=404, error="missing")
        if method == "GET":
            return HopResult(
                ok=True,
                status=200,
                body={"mood": "neutral", "mood_dot": 0.5, "status": "ghost-online", "version": "3"},
            )
        return HopResult(ok=True, status=200, body={"posted": True, "duplicate": False})

    monkeypatch.setattr("bridge.avrone_cyclekernal_link._request", fake)
    monkeypatch.setattr("bridge.avrone_cyclekernal_link._sleep", sleeps.append)
    stats = relay_loop(interval=0, max_cycles=1)
    assert sleeps == []
    assert stats["backoffs"] == 0
    assert stats["missing_routes"] == 1
    assert stats["mood_mirrored"] == 1
    assert stats["health_mirrored"] == 1
    assert stats["forwarded"] == 0


def test_resolver_probes_real_liveness_routes(monkeypatch):
    _quiet_env(monkeypatch)
    seen: list[str] = []

    def fake(url, timeout=2.0):
        seen.append(url)
        response = mock.Mock()
        response.status = 200
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert resolve_avrone_base() == "http://127.0.0.1:8710"
    assert resolve_ck_base() == "http://127.0.0.1:8090"
    assert seen[0] == "http://127.0.0.1:8710/api/status"
    assert seen[1] == "http://127.0.0.1:8090/health"


def test_resolver_falls_back_to_a_different_shim(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("AVRONE_SHIM_BASE", "http://127.0.0.1:8711")

    def fake(url, timeout=2.0):
        if url.startswith("http://127.0.0.1:8710"):
            raise OSError("down")
        response = mock.Mock()
        response.status = 200
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert resolve_avrone_base() == "http://127.0.0.1:8711"


def test_resolver_soft_fails_when_nothing_answers(monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", mock.Mock(side_effect=OSError("down")))
    assert resolve_avrone_base() is None
    assert resolve_ck_base() is None


def test_bare_blackboard_array_is_tolerated(monkeypatch):
    _quiet_env(monkeypatch)

    def fake(method, url, **kwargs):
        if method == "GET" and "blackboard" in url:
            return HopResult(ok=True, status=200, body=[
                {
                    "id": "find0000001",
                    "kind": "finding",
                    "text": "bare",
                    "run_id": "r",
                    "origin_node": "local",
                }
            ])
        if method == "GET":
            return HopResult(
                ok=True,
                status=200,
                body={"mood": "neutral", "mood_dot": 0.5, "status": "ok", "version": "1"},
            )
        return HopResult(ok=True, status=200, body={"accepted": True, "duplicate": False})

    monkeypatch.setattr("bridge.avrone_cyclekernal_link._request", fake)
    monkeypatch.setattr("bridge.avrone_cyclekernal_link._sleep", lambda _delay: None)
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1
    assert stats["errors"] == 0


def test_docs_name_the_real_ports_and_the_contract_routes():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    contract = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
    assert "8710" in readme and "8090" in readme
    assert "frontend" in readme.lower()
    for required in (
        "/api/blackboard",
        "/v1/events",
        "/v1/current_mood",
        "/health",
        "/api/status",
        "X-Admin-Token",
        "4096",
        "8710",
        "8090",
        "4000",
    ):
        assert required in contract
