"""Extra edge cases beyond rounds 5 and 6. These tests are new and short.

The 60 second soak already recorded in artifacts/fusion-bridge/soak.json is not repeated.
A 3 second window here only checks the same conservation identity on a smaller sample.
"""
from __future__ import annotations

import json
import math

import pytest

from bridge.avrone_cyclekernal_link import relay_loop
from bridge.soak_harness import run_soak
from bridge.strict_fields import FieldError, heat_from_wire, parse_beta_t, parse_cone_angle, parse_heat
from tests.test_relay_integration import TOKEN, _finding, _raw_post


def test_non_finite_and_string_heat_are_rejected():
    for raw in ('{"heat": 1e309}', '{"heat": -1e309}', '{"heat": NaN}', '{"heat": Infinity}'):
        try:
            heat_from_wire(raw)
        except FieldError:
            continue
        except json.JSONDecodeError:
            continue
        else:
            parsed = heat_from_wire(raw)
            assert math.isfinite(parsed["value"])
    with pytest.raises(FieldError):
        parse_heat("123456789")
    with pytest.raises(FieldError):
        parse_heat(None)
    small = heat_from_wire('{"heat": 1e-10}')
    assert small["underflow"] is False
    assert small["value"] == pytest.approx(1e-10)
    negative_zero = parse_heat(-0.0)
    assert negative_zero["value"] == 0.0
    assert math.copysign(1.0, negative_zero["value"]) < 0


def test_beta_t_rejects_controls_and_arrays_keeps_symbols():
    with pytest.raises(FieldError):
        parse_beta_t("line\nbreak")
    with pytest.raises(FieldError):
        parse_beta_t("\ufeffbom")
    with pytest.raises(FieldError):
        parse_beta_t("[1, 2]")
    assert parse_beta_t("  spaced  ") == "  spaced  "
    assert parse_cone_angle(0) == 0.0
    with pytest.raises(FieldError):
        parse_cone_angle(None)


def test_cross_source_same_id_is_not_acked(peers, caplog):
    av, ck = peers
    ck.legacy_bare_id = True
    with ck.lock:
        ck.log.add({
            "id": "find0000001",
            "source": "greatscale",
            "kind": "evidence",
            "run_id": "gs",
            "payload": {"text": "greatscale copy", "heat": 0.1},
            "ts": 1.0,
        })
    av.inject_post(_finding("find0000001", "avrone copy"))
    with caplog.at_level("WARNING", logger="fusion.relay"):
        blocked = relay_loop(interval=0, max_cycles=1)
    assert blocked["forwarded"] == 0
    assert blocked["conflicts"] == 1
    assert [event["source"] for event in ck.log.list()] == ["greatscale"]
    assert any("not acking" in record.message for record in caplog.records)
    assert any(hit["idempotency"] == "avrone:find0000001" for hit in ck.hits)

    ck.legacy_bare_id = False
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 1, recovered
    stored = {event["source"]: event["payload"]["text"] for event in ck.log.list()}
    assert stored["greatscale"] == "greatscale copy"
    assert stored["avrone"] == "avrone copy"


def test_same_source_duplicate_is_acked(peers):
    _av, ck = peers
    body = json.dumps({
        "id": "dup000000001",
        "source": "avrone",
        "kind": "finding",
        "run_id": "r",
        "payload": {"heat": 0.2},
        "ts": 1,
    }).encode()
    other = json.dumps({
        "id": "dup000000001",
        "source": "greatscale",
        "kind": "evidence",
        "run_id": "r",
        "payload": {"heat": 0.8},
        "ts": 1,
    }).encode()
    s1, raw1 = _raw_post(ck.base + "/v1/events", body, TOKEN)
    s2, raw2 = _raw_post(ck.base + "/v1/events", body, TOKEN)
    s3, raw3 = _raw_post(ck.base + "/v1/events", other, TOKEN)
    assert s1 == s2 == s3 == 200
    assert json.loads(raw1)["duplicate"] is False
    assert json.loads(raw2)["duplicate"] is True
    assert json.loads(raw2)["source"] == "avrone"
    assert json.loads(raw3)["duplicate"] is False
    heats = {(event["source"], event["payload"]["heat"]) for event in ck.log.list()}
    assert heats == {("avrone", 0.2), ("greatscale", 0.8)}


def test_short_soak_conserves_events():
    """Three seconds, not a replacement for the recorded 60 second soak."""
    result = run_soak(seconds=3.0, target_hz=200.0, queue_max=64)
    assert result["lost"] == 0
    assert result["end_backlog"] == 0
    assert result["seconds_measured"] < 5
    offered = result["counts"]["offered"]
    assert offered > 0
    assert result["counts"]["delivered"] + result["counts"]["rejected_late"] + result["counts"]["rejected_backpressure"] + result["counts"]["duplicate"] + result["counts"]["failed_http"] == offered
