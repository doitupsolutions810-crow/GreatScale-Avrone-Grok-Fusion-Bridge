from __future__ import annotations

import json
import time
from unittest import mock

import pytest

from bridge.avrone_cyclekernal_link import (
    RELAY_ROLE,
    fetch_blackboard,
    FusionEvent,
    post_to_avrone_bridge,
    post_to_cyclekernal,
    relay_loop,
)


def test_fusion_event_auto_timestamp():
    e = FusionEvent(source="avrone", kind="finding", run_id="r1", payload={"x": 1})
    assert e.ts > 0
    assert "x" in e.to_json()


def test_post_to_cyclekernal_success():
    event = FusionEvent(source="avrone", kind="finding", run_id="r1", payload={"ok": True})
    mock_resp = mock.Mock()
    mock_resp.status = 200
    mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
    mock_resp.__exit__ = mock.Mock(return_value=False)
    with mock.patch("urllib.request.urlopen", return_value=mock_resp) as m:
        assert post_to_cyclekernal(event) is True
        m.assert_called_once()
        req = m.call_args[0][0]
        assert req.full_url.endswith("/events")  # CognitiveBridge serves POST /events (falls back to /api/events)
        assert req.method == "POST"


def test_post_to_cyclekernal_soft_fail():
    event = FusionEvent(source="avrone", kind="finding", run_id="r1", payload={})
    with mock.patch("urllib.request.urlopen", side_effect=ConnectionError("down")):
        assert post_to_cyclekernal(event) is False


def test_post_to_avrone_bridge_mood_kind():
    event = FusionEvent(source="cyclekernal", kind="mood", run_id="r2", payload={"valence": 0.7})
    mock_resp = mock.Mock()
    mock_resp.status = 200
    mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
    mock_resp.__exit__ = mock.Mock(return_value=False)
    with mock.patch("urllib.request.urlopen", return_value=mock_resp) as m:
        assert post_to_avrone_bridge(event) is True
        body = json.loads(m.call_args[0][0].data)
        assert body["kind"] == "conclusion"
        assert body["role"] == "cyclekernal-relay"


def test_relay_loop_forwards_and_mirrors():
    blackboard = [
        {"id": "e1", "kind": "finding", "run_id": "r1", "text": "hello", "payload": {"t": 1}},
        {"id": "e1", "kind": "finding", "run_id": "r1", "text": "hello", "payload": {"t": 1}},  # dup
        {"id": "e2", "kind": "finding", "run_id": "r2", "text": "world", "payload": {"t": 2}},
    ]
    health = {"status": "ok", "uptime": 12}

    def fake_urlopen(req, timeout=5):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        resp = mock.Mock()
        resp.status = 200
        if "blackboard" in url:
            resp.read.return_value = json.dumps(blackboard).encode()
        elif "health" in url:
            resp.read.return_value = json.dumps(health).encode()
        else:
            resp.read.return_value = b"{\"ok\": true}"
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        return resp

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with mock.patch("time.sleep"):
            stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 2
    assert stats["health_mirrored"] == 1
    assert stats["errors"] == 0


def test_relay_loop_soft_fail_on_blackboard_error():
    with mock.patch("urllib.request.urlopen", side_effect=ConnectionError("nope")), mock.patch("time.sleep"):
        stats = relay_loop(interval=0, max_cycles=2)
    assert stats["forwarded"] == 0
    assert stats["errors"] >= 2


# ---- Contract with the real services (Avrone #20 /api/blackboard, CycleKernal /events auth) ----

def _recorder(responses):
    """urlopen fake: responses maps a URL suffix to (status, json-able body) or an Exception."""
    calls = []

    def fake(req, timeout=5):
        url = req.full_url
        calls.append(req)
        for suffix, value in responses.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                status, body = value
                resp = mock.Mock()
                resp.status = status
                resp.read.return_value = json.dumps(body).encode()
                resp.__enter__ = mock.Mock(return_value=resp)
                resp.__exit__ = mock.Mock(return_value=False)
                return resp
        raise OSError(f"unexpected url {url}")

    return fake, calls


def test_avrone_admin_token_sent_on_api_blackboard_only_from_env(monkeypatch):
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", "adm-test")
    fake, calls = _recorder({"/api/blackboard": (200, {"posts": [{"id": "p1", "text": "t"}], "count": 1})})
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        assert fetch_blackboard() == [{"id": "p1", "text": "t"}]
    assert calls[0].get_header("X-admin-token") == "adm-test"
    monkeypatch.delenv("AVRONE_ADMIN_TOKEN")
    fake, calls = _recorder({"/api/blackboard": (200, {"posts": []})})
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        assert fetch_blackboard() == []
    assert calls[0].get_header("X-admin-token") is None


def test_fetch_blackboard_falls_back_to_shim_list_shape():
    import urllib.error
    fake, calls = _recorder({
        "/api/blackboard": urllib.error.HTTPError("u", 404, "nf", {}, None),
        "/blackboard": (200, [{"id": "s1", "text": "shim"}]),
    })
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        assert fetch_blackboard() == [{"id": "s1", "text": "shim"}]
    assert calls[0].full_url.endswith("/api/blackboard")
    assert calls[1].full_url.endswith("/blackboard") and not calls[1].full_url.endswith("/api/blackboard")


def test_fetch_blackboard_none_when_both_unreachable_or_malformed():
    fake, _ = _recorder({"/api/blackboard": (200, {"unexpected": True}), "/blackboard": (200, "nope")})
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        assert fetch_blackboard() is None


def test_cyclekernal_bearer_token_from_env(monkeypatch):
    monkeypatch.setenv("CK_FUSION_TOKEN", "ck-test")
    fake, calls = _recorder({"/events": (200, {"ok": True})})
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        assert post_to_cyclekernal(FusionEvent(source="avrone", kind="finding", run_id="r", payload={})) is True
    assert calls[0].get_header("Authorization") == "Bearer ck-test"
    assert b"ck-test" not in calls[0].data, "token must never be in the body"


def test_cyclekernal_events_token_fallback_and_absent(monkeypatch):
    monkeypatch.delenv("CK_FUSION_TOKEN", raising=False)
    monkeypatch.setenv("CK_EVENTS_TOKEN", "legacy")
    fake, calls = _recorder({"/events": (200, {"ok": True})})
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        post_to_cyclekernal(FusionEvent(source="avrone", kind="finding", run_id="r", payload={}))
    assert calls[0].get_header("Authorization") == "Bearer legacy"
    monkeypatch.delenv("CK_EVENTS_TOKEN")
    fake, calls = _recorder({"/events": (200, {"ok": True})})
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        post_to_cyclekernal(FusionEvent(source="avrone", kind="finding", run_id="r", payload={}))
    assert calls[0].get_header("Authorization") is None


def test_mirror_post_goes_to_api_blackboard_with_token_then_shim(monkeypatch):
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN", "adm-test")
    fake, calls = _recorder({"/api/blackboard": (401, {"detail": "Bad admin token"}), "/blackboard": (200, {"ok": True})})
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        assert post_to_avrone_bridge(FusionEvent(source="cyclekernal", kind="health", run_id="relay", payload={})) is True
    assert calls[0].full_url.endswith("/api/blackboard") and calls[0].get_header("X-admin-token") == "adm-test"
    assert calls[1].get_header("X-admin-token") is None, "token is never sent to the unauthenticated shim path"


def test_relay_never_forwards_its_own_mirrored_posts():
    posts = [
        {"id": "a", "kind": "finding", "text": "real", "role": "researcher"},
        {"id": "b", "kind": "finding", "text": "{\"status\": \"ok\"}", "role": RELAY_ROLE},
    ]
    fake, calls = _recorder({"/api/blackboard": (200, {"posts": posts}), "/events": (200, {"ok": True}), "/health": (200, {"status": "ok"})})
    with mock.patch("urllib.request.urlopen", side_effect=fake), mock.patch("time.sleep"):
        stats = relay_loop(interval=0, max_cycles=3)
    assert stats["forwarded"] == 1
    forwarded = [json.loads(c.data) for c in calls if c.full_url.endswith("/events")]
    assert all(f["run_id"] != "relay" for f in forwarded)
