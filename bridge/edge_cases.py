"""Additive edge cases. Existing tests are not modified.

``scale="unit"`` keeps the suite short. ``scale="full"`` is what ``drill.py``
runs: the 10k burst is real, and the known upstream bugs stay xfail.
"""
from __future__ import annotations

import gzip
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from bridge.harness import FaultPeer


def _result(name: str, status: str, detail: str, **extra) -> dict:
    out = {"name": name, "status": status, "detail": detail}
    out.update(extra)
    return out


def _request(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 3.0):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _event(eid: str, source: str = "avrone", text: str = "edge", ts: float | None = None) -> dict:
    return {
        "id": eid,
        "source": source,
        "kind": "finding",
        "run_id": "run-edge",
        "payload": {"text": text},
        "ts": time.time() if ts is None else ts,
    }


def _post(peer: FaultPeer, body: dict, headers: dict | None = None, path: str = "/v1/events"):
    raw = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    return _request(peer.base + path, raw, hdrs)


def case_clock_skew() -> dict:
    peer = FaultPeer("avrone", token="t").start()
    try:
        now = time.time()
        ahead, behind = now + 3600, now - 3600
        headers = {"X-Admin-Token": "t"}
        for eid, ts in (("skew-ahead", ahead), ("skew-behind", behind)):
            status, _, raw = _post(peer, {
                "id": eid, "kind": "finding", "text": eid, "run_id": "run", "ts": ts,
            }, headers, "/api/blackboard")
            if status != 200:
                return _result("clock_skew", "fail", f"{eid} -> {status} {raw[:120]!r}")
        got = {post["id"]: post["ts"] for post in peer.posts.values()}
        if abs(got["skew-ahead"] - ahead) > 1 or abs(got["skew-behind"] - behind) > 1:
            return _result("clock_skew", "fail", f"timestamps rewritten: {got}")
        return _result("clock_skew", "pass", "±1h timestamps stored as given")
    finally:
        peer.stop()


def case_idempotency_conflict() -> dict:
    peer = FaultPeer("ck", token="t").start()
    try:
        headers = {"X-Admin-Token": "t", "Idempotency-Key": "same-key"}
        first, _, _ = _post(peer, _event("idem-1", text="one"), headers)
        second, _, body = _post(peer, _event("idem-1", text="two"), headers)
        stored = list(peer.events.values())
        if first != 200 or second != 409:
            return _result("idempotency_conflict", "fail", f"statuses {first}, {second}")
        if len(stored) != 1 or stored[0]["payload"]["text"] != "one":
            return _result("idempotency_conflict", "fail", "original body was replaced")
        return _result("idempotency_conflict", "pass", "same key + different body is 409; original kept")
    finally:
        peer.stop()


def case_token_rotation() -> dict:
    peer = FaultPeer("avrone", token="new-token")
    peer.previous_token = "old-token"
    peer.start()
    try:
        headers = {"Content-Type": "application/json"}
        body = json.dumps({"id": "rot-1", "kind": "finding", "text": "rotate", "run_id": "run"}).encode()
        old = _request(peer.base + "/api/blackboard", body, {**headers, "X-Admin-Token": "old-token"})
        peer.previous_token = ""
        stale = _request(peer.base + "/api/blackboard", body, {**headers, "X-Admin-Token": "old-token"})
        current = _request(peer.base + "/api/blackboard", body, {**headers, "X-Admin-Token": "new-token"})
        if old[0] != 200 or stale[0] != 401 or current[0] != 200:
            return _result("token_rotation", "fail", f"old={old[0]} stale={stale[0]} current={current[0]}")
        return _result("token_rotation", "pass", "previous token worked, then was removed")
    finally:
        peer.stop()


def case_half_open() -> dict:
    peer = FaultPeer("ck", token="t")
    peer.read_timeout = 0.4
    peer.start()
    try:
        host, port = "127.0.0.1", peer.port
        hung = socket.create_connection((host, port), timeout=2)
        time.sleep(0.8)
        hung.close()
        status, _, _ = _post(peer, _event("half-open-1"), {"X-Admin-Token": "t"})
        if status != 200:
            return _result("half_open", "fail", f"server did not recover ({status})")
        return _result("half_open", "pass", "idle TCP dropped; next request accepted")
    finally:
        peer.stop()


def case_slowloris() -> dict:
    peer = FaultPeer("ck", token="t")
    peer.read_timeout = 0.4
    peer.start()
    try:
        sock = socket.create_connection(("127.0.0.1", peer.port), timeout=2)
        sock.sendall(b"POST /v1/events HTTP/1.1\r\nHost: localhost\r\nContent-Length: 80\r\n")
        time.sleep(0.2)
        sock.sendall(b"X-Admin-Token: t\r\n")
        time.sleep(0.8)
        try:
            sock.sendall(b"\r\n")
        except OSError:
            pass
        sock.close()
        status, _, _ = _post(peer, _event("slow-1"), {"X-Admin-Token": "t"})
        if status != 200:
            return _result("slowloris", "fail", f"server stuck after slow body ({status})")
        return _result("slowloris", "pass", "slow request body timed out; server still accepts")
    finally:
        peer.stop()


def case_disk_full_blackboard() -> dict:
    peer = FaultPeer("avrone", token="t")
    peer.disk_full = True
    peer.start()
    try:
        status, _, body = _post(peer, {
            "id": "disk-1", "kind": "finding", "text": "no space", "run_id": "run",
        }, {"X-Admin-Token": "t"}, "/api/blackboard")
        if status != 507 or b"ENOSPC" not in body:
            return _result("disk_full", "fail", f"{status} {body[:80]!r}")
        if peer.posts:
            return _result("disk_full", "fail", "post was stored despite ENOSPC")
        return _result("disk_full", "pass", "blackboard write returns 507 ENOSPC and stores nothing")
    finally:
        peer.stop()


def case_state_enospc(tmp: Path) -> dict:
    import errno
    from bridge.avrone_cyclekernal_link import relay_loop

    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.posts["enospc1"] = {
        "id": "enospc1", "run_id": "run", "agent": "scout", "role": "agent",
        "kind": "finding", "text": "still forward", "refs": [], "ts": 1700000000.0,
        "origin_node": "local",
    }
    real = os.replace

    def boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    os.replace = boom
    try:
        _apply_env(av, ck, tmp / "missing-state.json")
        stats = relay_loop(interval=0, max_cycles=1)
    finally:
        os.replace = real
        av.stop()
        ck.stop()
    if stats["forwarded"] != 1 or stats["state_errors"] < 1:
        return _result("state_enospc", "fail", json.dumps({k: stats[k] for k in ("forwarded", "state_errors", "errors")}))
    return _result("state_enospc", "pass", "ENOSPC on the state file is counted; the event still forwards")


def case_sigterm(tmp: Path) -> dict:
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    ck.latency_fn = lambda path, _hit: 0.8 if path == "/v1/events" else 0.0
    av.posts["sig-1"] = {
        "id": "sig-1", "run_id": "run", "agent": "scout", "role": "agent",
        "kind": "finding", "text": "in flight", "refs": [], "ts": 1700000000.0,
        "origin_node": "local",
    }
    state = tmp / "sig-state.json"
    env = os.environ.copy()
    env.update({
        "AVRONE_BASE": av.base,
        "CK_BASE": ck.base,
        "FUSION_TOKEN": "t",
        "FUSION_STATE_PATH": str(state),
        "FUSION_TIMEOUT": "5",
        "FUSION_INTERVAL": "0",
        "FUSION_BACKOFF_BASE": "0.05",
        "FUSION_BACKOFF_MAX": "0.2",
        "FUSION_BREAKER_THRESHOLD": "0",
        "FUSION_CYCLE_DEADLINE": "0",
        "FUSION_CYCLES": "0",
        "FUSION_RELAY_LOCK": str(tmp / "sig.lock"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    })
    for key in ("AVRONE_ADMIN_TOKEN", "CK_FUSION_TOKEN", "FUSION_HOST_MAP"):
        env.pop(key, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "bridge.avrone_cyclekernal_link"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.35)
        proc.send_signal(__import__("signal").SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
            return _result("sigterm", "fail", "relay did not exit after SIGTERM")
        if proc.returncode != 0:
            err = (proc.stderr.read() or b"")[:300]
            return _result("sigterm", "fail", f"exit {proc.returncode} {err!r}")
        if not state.is_file():
            return _result("sigterm", "fail", "state file missing after shutdown")
        saved = json.loads(state.read_text(encoding="utf-8"))
        if "sig-1" not in saved.get("seen", []):
            return _result("sigterm", "fail", "in-flight event was not recorded")
        return _result("sigterm", "pass", "SIGTERM finished the in-flight post and wrote state")
    finally:
        if proc.poll() is None:
            proc.kill()
        av.stop()
        ck.stop()


def case_corrupt_state(tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import relay_loop
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.posts["cor-1"] = {
        "id": "cor-1", "run_id": "run", "agent": "scout", "role": "agent",
        "kind": "finding", "text": "after corruption", "refs": [], "ts": 1700000000.0,
        "origin_node": "local",
    }
    state = tmp / "corrupt.json"
    state.write_text("{not-json", encoding="utf-8")
    try:
        _apply_env(av, ck, state)
        stats = relay_loop(interval=0, max_cycles=1)
    finally:
        av.stop()
        ck.stop()
    if stats["state_errors"] < 1 or stats["forwarded"] != 1:
        return _result("corrupt_state", "fail", f"errors={stats['state_errors']} forwarded={stats['forwarded']}")
    return _result("corrupt_state", "pass", "corrupt state is discarded; the post is forwarded again")


def case_burst(n: int, idle_s: float) -> dict:
    peer = FaultPeer("ck", token="t").start()
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", peer.port, timeout=5)

        def send(eid: str, source: str = "avrone") -> int:
            raw = json.dumps(_event(eid, source=source)).encode()
            conn.request("POST", "/v1/events", body=raw, headers={
                "Content-Type": "application/json",
                "X-Admin-Token": "t",
            })
            resp = conn.getresponse()
            resp.read()
            return resp.status

        for i in range(n):
            if send(f"b{i}") != 200:
                return _result("burst", "fail", f"first burst failed at {i}")
        time.sleep(idle_s)
        for i in range(n):
            if send(f"c{i}") != 200:
                return _result("burst", "fail", f"second burst failed at {i}")
        replay = send("b0")
        conn.close()
        if replay != 200 or len(peer.events) != n * 2:
            return _result("burst", "fail", f"stored={len(peer.events)} replay={replay}")
        return _result("burst", "pass", f"{n}+idle+{n} stored exactly, replay of b0 is a duplicate", delivered=n * 2, dupes=0)
    finally:
        peer.stop()


def case_mixed_case_headers() -> dict:
    peer = FaultPeer("ck", token="t").start()
    try:
        raw = json.dumps(_event("mix-1")).encode()
        status, body = _raw(
            peer.port,
            (
                "POST /v1/events HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Content-Length: {len(raw)}\r\n"
                "Content-Type: application/json\r\n"
                "x-AdMiN-tOkEn: t\r\n"
                "iDeMpOtEnCy-KeY: mix-key\r\n"
                "\r\n"
            ).encode() + raw,
        )
        if status != 200 or len(peer.events) != 1:
            return _result("mixed_case_headers", "fail", f"{status} {body[:80]!r}")
        return _result("mixed_case_headers", "pass", "mixed-case admin and idempotency headers accepted")
    finally:
        peer.stop()


def case_gzip_body() -> dict:
    peer = FaultPeer("ck", token="t").start()
    try:
        payload = gzip.compress(json.dumps(_event("gz-1")).encode())
        status, body = _raw(
            peer.port,
            (
                "POST /v1/events HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Encoding: gzip\r\n"
                "X-Admin-Token: t\r\n"
                "\r\n"
            ).encode() + payload,
        )
        if status != 200 or "gz-1" not in {v["id"] for v in peer.events.values()}:
            return _result("gzip_body", "fail", f"{status} {body[:80]!r}")
        return _result("gzip_body", "pass", "gzip body inflated under the cap and stored")
    finally:
        peer.stop()


def case_http10() -> dict:
    peer = FaultPeer("ck", token="t").start()
    try:
        raw = json.dumps(_event("h10")).encode()
        status, body = _raw(
            peer.port,
            (
                "POST /v1/events HTTP/1.0\r\n"
                "Host: localhost\r\n"
                f"Content-Length: {len(raw)}\r\n"
                "Content-Type: application/json\r\n"
                "X-Admin-Token: t\r\n"
                "\r\n"
            ).encode() + raw,
        )
        if status != 200 or b"HTTP/1.0" not in body.split(b"\r\n", 1)[0]:
            return _result("http10", "fail", body[:80].decode("latin1", "replace"))
        return _result("http10", "pass", "HTTP/1.0 client received HTTP/1.0 and the event was stored")
    finally:
        peer.stop()


def case_lone_surrogate() -> dict:
    peer = FaultPeer("avrone", token="t")
    peer.surrogate_bug = True
    peer.start()
    try:
        status, _, _ = _post(peer, {
            "id": "surr-1", "kind": "finding", "text": "bad \ud800 char", "run_id": "run",
        }, {"X-Admin-Token": "t"}, "/api/blackboard")
        if status != 200:
            return _result("lone_surrogate", "fail", f"post itself failed ({status})")
        got, _, _ = _request(peer.base + "/api/blackboard", headers={"X-Admin-Token": "t"})
        if got == 500:
            return _result(
                "lone_surrogate",
                "xfail",
                "known upstream: lone-surrogate post makes Avrone GET /api/blackboard 500",
            )
        if got == 200:
            return _result("lone_surrogate", "pass", "GET stayed 200")
        return _result("lone_surrogate", "fail", f"GET {got}")
    finally:
        peer.stop()


def case_invalid_utf8() -> dict:
    peer = FaultPeer("ck", token="t")
    peer.utf8_bug = True
    peer.start()
    try:
        raw = b'{"id":"utf-1","source":"avrone","kind":"finding","run_id":"run","payload":{"text":"ab\xe2\x82"},"ts":1}'
        status, _, _ = _request(
            peer.base + "/v1/events",
            raw,
            {"Content-Type": "application/json", "X-Admin-Token": "t"},
        )
        if status == 500:
            return _result("invalid_utf8", "xfail", "known upstream: CK invalid UTF-8 returns 500")
        if status == 400:
            return _result("invalid_utf8", "pass", "invalid UTF-8 is 400")
        return _result("invalid_utf8", "fail", f"status {status}")
    finally:
        peer.stop()


def case_dedup_source_id() -> dict:
    """Legacy id-only dedup collapses two sources. That move is the known bug."""
    peer = FaultPeer("ck", token="t")
    peer.dedup_mode = "id"
    peer.start()
    try:
        headers = {"X-Admin-Token": "t"}
        first, _, _ = _post(peer, _event("shared", source="avrone", text="a"), headers)
        second, _, body = _post(peer, _event("shared", source="greatscale", text="g"), headers)
        second_body = json.loads(body.decode())
        if first == 200 and second == 200 and second_body.get("duplicate") is True and len(peer.events) == 1:
            return _result(
                "dedup_source_id",
                "xfail",
                "known upstream: dedup is moving to (source, id); id-only peers still collapse sources",
            )
        if len(peer.events) == 2:
            return _result("dedup_source_id", "pass", "sources kept apart")
        return _result("dedup_source_id", "fail", f"unexpected {first} {second} stored={len(peer.events)}")
    finally:
        peer.stop()


def case_correct_dedup() -> dict:
    peer = FaultPeer("ck", token="t").start()
    try:
        headers = {"X-Admin-Token": "t"}
        _post(peer, _event("shared", source="avrone", text="a"), headers)
        status, _, body = _post(peer, _event("shared", source="greatscale", text="g"), headers)
        parsed = json.loads(body.decode())
        if status != 200 or parsed.get("duplicate") is True or len(peer.events) != 2:
            return _result("dedup_correct", "fail", f"stored={len(peer.events)} {parsed}")
        return _result("dedup_correct", "pass", "harness dedup is (source, id)")
    finally:
        peer.stop()


def case_cap_before_auth() -> dict:
    peer = FaultPeer("avrone", token="t").start()
    try:
        status, _, _ = _request(
            peer.base + "/api/blackboard",
            b"x" * 4097,
            {"X-Admin-Token": "wrong", "Content-Type": "application/json"},
        )
        if status != 413:
            return _result("cap_before_auth", "fail", f"expected 413 before auth, got {status}")
        return _result("cap_before_auth", "pass", "4097 bytes is 413 with a bad token")
    finally:
        peer.stop()


def case_relay_lock(tmp: Path) -> dict:
    # Run in a child so a drill that already holds a different lock does not
    # look like it owns this blackboard too.
    path = tmp / "one-relay.lock"
    code = (
        "import subprocess, sys\n"
        "from bridge.avrone_cyclekernal_link import acquire_relay_lock, release_relay_lock\n"
        "path = sys.argv[1]\n"
        "if not acquire_relay_lock(path):\n"
        "    sys.exit(3)\n"
        "try:\n"
        "    proc = subprocess.run([sys.executable, '-c', "
        "'import sys; from bridge.avrone_cyclekernal_link import acquire_relay_lock; "
        "sys.exit(0 if acquire_relay_lock(sys.argv[1]) else 2)', path], "
        "capture_output=True, text=True)\n"
        "    sys.exit(0 if proc.returncode == 2 else 4)\n"
        "finally:\n"
        "    release_relay_lock()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        return _result("single_relay_lock", "fail", f"exit {proc.returncode} {(proc.stderr or '')[:200]}")
    return _result("single_relay_lock", "pass", "a second process cannot lock the same blackboard")


def case_readonly_rootfs(tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import relay_loop
    root = tmp / "rootfs"
    if root.exists():
        root.chmod(0o755)
    root.mkdir(exist_ok=True)
    root.chmod(0o555)
    writable = True
    try:
        (root / "nope").write_text("x", encoding="utf-8")
    except OSError:
        writable = False
    tmpfs = tmp / "tmp"
    tmpfs.mkdir(exist_ok=True)
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.posts["ro-1"] = {
        "id": "ro-1", "run_id": "run", "agent": "scout", "role": "agent",
        "kind": "finding", "text": "readonly", "refs": [], "ts": 1700000000.0,
        "origin_node": "local",
    }
    try:
        _apply_env(av, ck, tmpfs / "state.json")
        stats = relay_loop(interval=0, max_cycles=1)
    finally:
        av.stop()
        ck.stop()
        root.chmod(0o755)
    if writable or stats["forwarded"] != 1:
        return _result("readonly_rootfs", "fail", f"writable={writable} forwarded={stats['forwarded']}")
    return _result("readonly_rootfs", "pass", "mock read-only rootfs rejected a write; relay state on tmpfs delivered")


def _raw(port: int, payload: bytes) -> tuple[int, bytes]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
    finally:
        sock.close()
    data = b"".join(chunks)
    line = data.split(b"\r\n", 1)[0]
    parts = line.split()
    status = int(parts[1]) if len(parts) > 1 else 0
    return status, data


def _apply_env(av: FaultPeer, ck: FaultPeer, state: Path) -> None:
    os.environ["AVRONE_BASE"] = av.base
    os.environ["CK_BASE"] = ck.base
    os.environ["FUSION_TOKEN"] = "t"
    os.environ["FUSION_STATE_PATH"] = str(state)
    os.environ["FUSION_TIMEOUT"] = "2"
    os.environ["FUSION_INTERVAL"] = "0"
    os.environ["FUSION_BACKOFF_BASE"] = "0.01"
    os.environ["FUSION_BACKOFF_MAX"] = "0.05"
    os.environ["FUSION_BREAKER_THRESHOLD"] = "0"
    os.environ["FUSION_CYCLE_DEADLINE"] = "0"
    os.environ.pop("AVRONE_ADMIN_TOKEN", None)
    os.environ.pop("CK_FUSION_TOKEN", None)
    os.environ.pop("FUSION_HOST_MAP", None)


_ENV_KEYS = (
    "AVRONE_BASE", "CK_BASE", "FUSION_TOKEN", "FUSION_STATE_PATH", "FUSION_TIMEOUT",
    "FUSION_INTERVAL", "FUSION_BACKOFF_BASE", "FUSION_BACKOFF_MAX", "FUSION_BREAKER_THRESHOLD",
    "FUSION_CYCLE_DEADLINE", "AVRONE_ADMIN_TOKEN", "CK_FUSION_TOKEN", "FUSION_HOST_MAP",
)


def run_all(scale: str = "unit", tmp: Path | None = None) -> list[dict]:
    root = tmp or Path(os.environ.get("EDGE_TMP", "/tmp/fusion-edge"))
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("enospc", "sigterm", "corrupt", "lock", "ro"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    n = 10000 if scale == "full" else 40
    idle = 1.0 if scale == "full" else 0.05
    try:
        cases = _cases(root, n, idle)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for item in cases:
        if item["name"] == "burst":
            item["name"] = f"burst_{n}"
    return cases


def _cases(root: Path, n: int, idle: float) -> list[dict]:
    specs = [
        ("clock_skew", case_clock_skew),
        ("idempotency_conflict", case_idempotency_conflict),
        ("token_rotation", case_token_rotation),
        ("half_open", case_half_open),
        ("slowloris", case_slowloris),
        ("disk_full", case_disk_full_blackboard),
        ("state_enospc", lambda: case_state_enospc(root / "enospc")),
        ("sigterm", lambda: case_sigterm(root / "sigterm")),
        ("corrupt_state", lambda: case_corrupt_state(root / "corrupt")),
        ("burst", lambda: case_burst(n, idle)),
        ("mixed_case_headers", case_mixed_case_headers),
        ("gzip_body", case_gzip_body),
        ("http10", case_http10),
        ("cap_before_auth", case_cap_before_auth),
        ("single_relay_lock", lambda: case_relay_lock(root / "lock")),
        ("readonly_rootfs", lambda: case_readonly_rootfs(root / "ro")),
        ("dedup_correct", case_correct_dedup),
        ("lone_surrogate", case_lone_surrogate),
        ("invalid_utf8", case_invalid_utf8),
        ("dedup_source_id", case_dedup_source_id),
    ]
    results = []
    for name, fn in specs:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(_result(name, "fail", f"{type(exc).__name__}: {exc}"))
    return results
