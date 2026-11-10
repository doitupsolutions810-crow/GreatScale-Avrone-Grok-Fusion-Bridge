"""Round 5 regression battery.

Original heat/cone artifact directory and verify.sh were not in this repo.
These tests are new.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bridge.avrone_cyclekernal_link import relay_loop
from bridge.strict_fields import FieldError, choose_payload, heat_from_wire, parse_beta_t, parse_cone_angle, parse_heat
from tests.test_relay_integration import TOKEN, _finding, _raw_post, peers  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_heat_bool_is_not_numeric():
    with pytest.raises(FieldError):
        parse_heat(True)
    with pytest.raises(FieldError):
        parse_heat(False)
    assert parse_heat(1)["value"] == 1.0
    assert parse_heat(0)["value"] == 0.0


def test_heat_underflow_keeps_wire_token():
    raw = '{"heat": 1e-400}'
    parsed = heat_from_wire(raw)
    assert parsed["literal"] == "1e-400"
    assert parsed["value"] == 0.0
    assert parsed["underflow"] is True
    assert "1e-400" in raw


def test_cone_angle_rejects_base64_empty_and_decorated_strings():
    assert parse_cone_angle(0.5) == 0.5
    for bad in ("", "AQIDBA==", " 0.5", "0.5 ", "0.5 // no", "/*c*/0.5"):
        with pytest.raises(FieldError):
            parse_cone_angle(bad)


def test_beta_t_emoji_kept_zero_width_and_double_json_rejected():
    assert parse_beta_t("beta \U0001F525") == "beta \U0001F525"
    with pytest.raises(FieldError):
        parse_beta_t("a\u200b")
    with pytest.raises(FieldError):
        parse_beta_t('{"a": 1}')


def test_payload_wins_over_text_and_oversize_is_separate(peers):
    av, ck = peers
    av.inject_post({
        "id": "both00000001",
        "run_id": "run-1",
        "agent": "scout",
        "role": "agent",
        "kind": "finding",
        "text": json.dumps({"heat": 0.9}) + ("b" * 3000),
        "payload": {"heat": 0.25, "note": "structured"},
        "refs": [],
        "ts": 1.0,
        "origin_node": "local",
    })
    winner, chosen = choose_payload(av.board.posts["both00000001"])
    assert winner == "payload" and chosen["heat"] == 0.25
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    stored = ck.log.list()[0]["payload"]
    assert stored["heat"] == 0.25
    assert stored.get("note") == "structured"
    huge = b'{"payload":{"heat":0.1},"text":"' + (b"a" * 5000) + b'"}'
    status, _ = _raw_post(ck.base + "/v1/events", huge, TOKEN)
    assert status == 413
    assert len(ck.log.list()) == 1


def test_identical_ids_keep_the_first_heat(peers):
    _av, ck = peers
    first = None
    for i in range(100):
        body = json.dumps({
            "id": "same00000001",
            "source": "avrone",
            "kind": "finding",
            "run_id": "r",
            "payload": {"heat": i},
            "ts": 1,
        }).encode()
        status, raw = _raw_post(ck.base + "/v1/events", body, TOKEN)
        assert status == 200
        dup = json.loads(raw)["duplicate"]
        if i == 0:
            assert dup is False
            first = i
        else:
            assert dup is True
    stored = ck.log.list()
    assert len(stored) == 1
    assert stored[0]["payload"]["heat"] == first == 0


def test_relay_rejects_bool_heat_explicitly(peers):
    av, ck = peers
    av.inject_post({
        **_finding("heatbool0001", "x"),
        "payload": {"heat": True},
    })
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 0
    assert stats["rejected"] >= 1
    assert ck.log.list() == []


def test_declared_huge_body_does_not_inflate_server_rss(tmp_path):
    port_file = tmp_path / "port"
    stop_file = tmp_path / "stop"
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "from tests.relay_stubs import ContractStub\n"
        f"port_file = Path({str(port_file)!r})\n"
        f"stop_file = Path({str(stop_file)!r})\n"
        "stub = ContractStub('ck', 'fusion-test-token')\n"
        "stub.start()\n"
        "port_file.write_text(str(stub.port))\n"
        "while not stop_file.exists():\n"
        "    time.sleep(0.05)\n"
        "stub.stop()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not port_file.exists():
            time.sleep(0.05)
        assert port_file.exists()
        port = int(port_file.read_text())
        before = _hwm_kb(proc.pid)
        _declare_only(port, 10_000_000 + 80)
        after_header = _hwm_kb(proc.pid)
        _stream_digits(port, 10_000_000)
        after_stream = _hwm_kb(proc.pid)
    finally:
        stop_file.write_text("1")
        proc.wait(timeout=5)
    # Service high-water must not jump by the 10 MB payload.
    assert after_stream - before < 4096, (before, after_header, after_stream)


def _hwm_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    return 0


def _declare_only(port: int, length: int) -> None:
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        header = (
            "POST /v1/events HTTP/1.0\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {length}\r\n"
            "X-Admin-Token: fusion-test-token\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        sock.sendall(header)
        data = b""
        sock.settimeout(2)
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        assert b"413" in data.split(b"\r\n", 1)[0]
    finally:
        sock.close()


def _stream_digits(port: int, n: int) -> None:
    prefix = b'{"source":"avrone","kind":"finding","run_id":"r","id":"bigheat00001","payload":{"heat":"'
    suffix = b'"}}'
    total = len(prefix) + n + len(suffix)
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.settimeout(2)
    try:
        header = (
            "POST /v1/events HTTP/1.0\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {total}\r\n"
            "X-Admin-Token: fusion-test-token\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        sock.sendall(header + prefix)
        remaining = n
        chunk = b"9" * 65536
        while remaining:
            step = min(65536, remaining)
            try:
                sock.sendall(chunk[:step])
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                break
            remaining -= step
        try:
            sock.recv(4096)
        except OSError:
            pass
    finally:
        sock.close()


def test_million_number_list_is_a_declared_size_reject(peers):
    _av, ck = peers
    count = 1_000_000
    # [0,0,...] length without building the list.
    declared = 2 + (2 * count - 1)
    status = _declare_status(ck.port, declared)
    assert status == 413
    assert ck.log.list() == []
    with pytest.raises(FieldError):
        parse_heat([0, 1, 2])


def _declare_status(port: int, length: int) -> int:
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        sock.sendall(
            (
                "POST /v1/events HTTP/1.0\r\n"
                "Host: 127.0.0.1\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {length}\r\n"
                "X-Admin-Token: fusion-test-token\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )
        sock.settimeout(2)
        line = b""
        while b"\n" not in line:
            chunk = sock.recv(1)
            if not chunk:
                break
            line += chunk
        return int(line.split()[1])
    finally:
        sock.close()
