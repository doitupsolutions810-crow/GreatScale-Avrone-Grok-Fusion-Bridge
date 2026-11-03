from __future__ import annotations

import json
import time
from unittest import mock

import pytest

from bridge.avrone_cyclekernal_link import (
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
        assert req.full_url.endswith("/api/events")
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
