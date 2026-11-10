"""Round 6 regression battery.

Original verify.sh was not in this repo. These tests are new.
IPv6 is exercised with a real bind to ::1 when the kernel allows it.
"""
from __future__ import annotations

import json
import socket

import pytest

from bridge.avrone_cyclekernal_link import relay_loop
from bridge.settings import load_config
from bridge.strict_fields import parse_retry_after
from tests.relay_stubs import ContractStub
from tests.test_relay_integration import TOKEN, _finding, _raw_post


def _ipv6_ok() -> bool:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.bind(("::1", 0))
        sock.close()
        return True
    except OSError:
        return False


def test_retry_after_parser_rejects_invalid_values():
    assert parse_retry_after("1") == 1.0
    assert parse_retry_after("0.05") == 0.05
    assert parse_retry_after(None) is None
    for bad in ("", "-1", "+1", "soon", "999999", "Wed, 21 Oct 2015 07:28:00 GMT"):
        assert parse_retry_after(bad) is None


def test_429_honors_retry_after_then_recovers(peers, monkeypatch):
    av, ck = peers
    av.inject_post(_finding("rate00000001", "rate"))
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.01")
    monkeypatch.setenv("FUSION_BACKOFF_MAX", "0.2")
    ck.events_script = [{"status": 429, "retry_after": "0.05"} for _ in range(20)]
    stats = relay_loop(interval=0, max_cycles=21)
    hits = [hit for hit in ck.hits if hit["path"] == "/v1/events"]
    gaps = [b["t"] - a["t"] for a, b in zip(hits, hits[1:])]
    assert stats["forwarded"] == 1, stats
    assert stats["throttled"] == 20
    assert len(ck.log.list()) == 1
    assert gaps and min(gaps) >= 0.04
    assert max(gaps) < 1.0
    assert stats["conflicts"] == 0 if "conflicts" in stats else True


def test_invalid_retry_after_uses_bounded_backoff(peers, monkeypatch):
    av, ck = peers
    av.inject_post(_finding("rate00000002", "rate"))
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.05")
    monkeypatch.setenv("FUSION_BACKOFF_MAX", "0.2")
    ck.events_script = [
        {"status": 429, "retry_after": "-1"},
        {"status": 429, "retry_after": "soon"},
        {"status": 429, "retry_after": "1000000"},
    ]
    started = __import__("time").perf_counter()
    stats = relay_loop(interval=0, max_cycles=4)
    elapsed = __import__("time").perf_counter() - started
    assert stats["forwarded"] == 1
    assert stats["throttled"] == 3
    assert elapsed < 2.0, elapsed
    hits = [hit for hit in ck.hits if hit["path"] == "/v1/events"]
    gaps = [b["t"] - a["t"] for a, b in zip(hits, hits[1:])]
    assert gaps and min(gaps) >= 0.04
    assert max(gaps) < 1.0


def test_500_with_two_second_delay_then_recovers(peers, monkeypatch):
    av, ck = peers
    av.inject_post(_finding("slow00000001", "slow"))
    monkeypatch.setenv("FUSION_TIMEOUT", "5")
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "12")
    monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.05")
    monkeypatch.setenv("FUSION_BACKOFF_MAX", "0.2")
    ck.events_script = [{"status": 500, "delay": 2.0} for _ in range(10)]
    started = __import__("time").perf_counter()
    stats = relay_loop(interval=0, max_cycles=12)
    elapsed = __import__("time").perf_counter() - started
    hits = [hit for hit in ck.hits if hit["path"] == "/v1/events"]
    gaps = [b["t"] - a["t"] for a, b in zip(hits, hits[1:])]
    assert stats["forwarded"] == 1, stats
    assert stats["upstream_errors"] == 10
    assert len(ck.log.list()) == 1
    assert len(hits) == 11
    assert min(gaps) >= 1.8
    assert max(gaps) < 8.0
    assert elapsed < 40.0


def test_ipv6_literal_and_loopback_equivalence():
    if not _ipv6_ok():
        pytest.skip("IPv6 loopback bind is unavailable in this environment")
    v4 = ContractStub("ck", TOKEN)
    v6 = ContractStub("ck", TOKEN)
    v4.start("127.0.0.1")
    v6.start("::1")
    body = json.dumps({
        "id": "ip600000001",
        "source": "avrone",
        "kind": "finding",
        "run_id": "r",
        "payload": {"heat": 0.5, "cone_angle": 1.25, "beta_t": "beta \U0001F525"},
        "ts": 1,
    }).encode()
    try:
        s4, raw4 = _raw_post(v4.base + "/v1/events", body, TOKEN)
        s6, raw6 = _raw_post(v6.base + "/v1/events", body, TOKEN)
        assert s4 == s6 == 200
        assert json.loads(raw4)["duplicate"] is False
        assert json.loads(raw6)["duplicate"] is False
        assert v4.log.list()[0]["payload"] == v6.log.list()[0]["payload"]
        infos = socket.getaddrinfo("localhost", None, type=socket.SOCK_STREAM)
        shown_host = infos[0][4][0]
        target = v6 if ":" in shown_host else v4
        local_body = json.dumps({
            "id": "local0000001",
            "source": "avrone",
            "kind": "finding",
            "run_id": "r",
            "payload": {"heat": 0.5},
            "ts": 1,
        }).encode()
        status, _ = _raw_post(f"http://localhost:{target.port}/v1/events", local_body, TOKEN)
        assert status == 200
        assert any(event["id"] == "local0000001" for event in target.log.list())
    finally:
        v4.stop()
        v6.stop()


def test_avrone_url_ipv6_literal(monkeypatch):
    if not _ipv6_ok():
        pytest.skip("IPv6 loopback bind is unavailable in this environment")
    av = ContractStub("avrone", TOKEN)
    ck = ContractStub("ck", TOKEN)
    av.start("::1")
    ck.start("127.0.0.1")
    av.inject_post(_finding("v6find000001", "from v6"))
    try:
        monkeypatch.delenv("AVRONE_BASE", raising=False)
        monkeypatch.setenv("AVRONE_URL", av.base)
        monkeypatch.setenv("CK_BASE", ck.base)
        monkeypatch.setenv("FUSION_TOKEN", TOKEN)
        monkeypatch.setenv("FUSION_STATE_PATH", "")
        monkeypatch.setenv("FUSION_BACKOFF_BASE", "0.01")
        monkeypatch.setenv("FUSION_BACKOFF_MAX", "0.05")
        cfg = load_config()
        assert cfg.avrone_base.startswith("http://[::1]:")
        stats = relay_loop(interval=0, max_cycles=1)
        assert stats["forwarded"] == 1, stats
        assert ck.log.list()[0]["payload"]["text"] == "from v6"
    finally:
        av.stop()
        ck.stop()
