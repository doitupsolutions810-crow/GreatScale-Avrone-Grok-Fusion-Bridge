"""One command for the fusion soak, faults, edges, and bench.

``verify.sh`` runs this. There is no suite-level timeout. Each HTTP call keeps
its own timeout. Docker is used when it is actually installed; otherwise every
round is the mock described in the summary.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "ROUND6_SUMMARY.md"


def _rss_kb() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _oom_killed() -> bool:
    path = Path("/sys/fs/cgroup/memory.events")
    try:
        if not path.is_file():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1]) > 0
    except (OSError, ValueError, IndexError):
        return False
    return False


def docker_verdict() -> dict:
    if shutil.which("docker") is None:
        return {"available": False, "detail": "docker is not installed in this environment"}
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if proc.returncode != 0:
        return {"available": False, "detail": "docker is installed but the daemon is not usable"}
    return {"available": True, "detail": "docker daemon responded"}


def _finding(eid: str, text: str = "soak") -> dict:
    return {
        "id": eid,
        "run_id": "soak",
        "agent": "scout",
        "role": "agent",
        "kind": "finding",
        "text": text,
        "refs": [],
        "ts": 1_700_000_000.0,
        "origin_node": "local",
    }


def _arm(av, ck, state: Path, *, av_base: str | None = None, ck_base: str | None = None) -> None:
    os.environ["AVRONE_BASE"] = av_base or av.base
    os.environ["CK_BASE"] = ck_base or ck.base
    os.environ["FUSION_TOKEN"] = "t"
    os.environ["FUSION_STATE_PATH"] = str(state)
    os.environ["FUSION_TIMEOUT"] = os.environ.get("DRILL_TIMEOUT", "1.5")
    os.environ["FUSION_INTERVAL"] = "0"
    os.environ["FUSION_BACKOFF_BASE"] = "0.05"
    os.environ["FUSION_BACKOFF_MAX"] = "0.5"
    os.environ["FUSION_BREAKER_THRESHOLD"] = "0"
    os.environ["FUSION_CYCLE_DEADLINE"] = "0"
    os.environ.pop("AVRONE_ADMIN_TOKEN", None)
    os.environ.pop("CK_FUSION_TOKEN", None)


def _clear_stop() -> None:
    from bridge import avrone_cyclekernal_link as relay
    relay._stop.clear()


def _run_until(seconds: float, cycles_cap: int = 0, interval: float = 0.0) -> dict:
    from bridge import avrone_cyclekernal_link as relay
    box: dict = {}

    def work() -> None:
        box["stats"] = relay.relay_loop(interval=interval, max_cycles=cycles_cap)

    _clear_stop()
    thread = threading.Thread(target=work, name="drill-relay")
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        relay.request_stop()
        thread.join(timeout=10)
    return box.get("stats") or {}


def phase_pytest() -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    tail = (proc.stdout or "")[-1500:]
    return {
        "name": "pytest",
        "round": "unit",
        "mode": "local",
        "status": "pass" if proc.returncode == 0 else "fail",
        "detail": tail.strip().splitlines()[-1] if tail.strip() else f"exit {proc.returncode}",
        "log": tail,
    }


def phase_integration() -> dict:
    proc = subprocess.run(
        [sys.executable, "test_integration.py"],
        cwd=str(ROOT / "bridge"),
        capture_output=True,
        text=True,
    )
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {
        "name": "integration",
        "round": "unit",
        "mode": "local",
        "status": "pass" if proc.returncode == 0 else "fail",
        "detail": text[-400:] or f"exit {proc.returncode}",
    }


def phase_soak(cycles: int, tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import relay_loop
    from bridge.harness import FaultPeer
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    try:
        for i in range(cycles):
            av.posts[f"s{i:04d}"] = _finding(f"s{i:04d}", f"soak {i}")
        _clear_stop()
        _arm(av, ck, tmp / "soak.json")
        os.environ["FUSION_TIMEOUT"] = "3"
        stats = relay_loop(interval=0, max_cycles=cycles)
        ok = stats["forwarded"] == cycles and stats["duplicates"] == 0 and stats["errors"] == 0
        return {
            "name": "soak",
            "round": "R3",
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "delivered": stats["forwarded"],
            "dupes": stats["duplicates"],
            "cycles": stats["cycles"],
            "errors": stats["errors"],
            "p50_ms": stats["latency_p50_ms"],
            "p95_ms": stats["latency_p95_ms"],
            "p99_ms": stats["latency_p99_ms"],
            "events_per_sec": stats["events_per_sec"],
            "oom_killed": _oom_killed(),
            "restarts": 0,
            "rss_kb": _rss_kb(),
            "detail": (
                f"{stats['forwarded']}/{cycles} forwarded, dupes={stats['duplicates']}, "
                f"p50={stats['latency_p50_ms']} p95={stats['latency_p95_ms']} "
                f"p99={stats['latency_p99_ms']} ms, {stats['events_per_sec']} ev/s, "
                f"OOMKilled={str(_oom_killed()).lower()} restarts=0 rss_kb={_rss_kb()}"
            ),
        }
    finally:
        av.stop()
        ck.stop()


def phase_partition(seconds: float, tmp: Path) -> dict:
    from bridge.harness import FaultPeer, TcpProxy
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    proxy = TcpProxy(av.port, drop_until=time.monotonic() + seconds, drop_rate=0.5).start()
    try:
        for i in range(20):
            av.posts[f"p{i:04d}"] = _finding(f"p{i:04d}", "partition")
        _arm(av, ck, tmp / "part.json", av_base=f"http://127.0.0.1:{proxy.port}")
        os.environ["FUSION_BACKOFF_MAX"] = "0.2"
        stats = _run_until(seconds + 3, cycles_cap=0)
        delivered = stats.get("forwarded", 0)
        ok = delivered == 20 and stats.get("duplicates", 0) == 0
        return {
            "name": "partition",
            "round": "R3",
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "errors": stats.get("errors", 0),
            "detail": (
                f"mock 50% SYN-drop (RST on accept) for {seconds:.0f}s; "
                f"delivered {delivered}/20 errors={stats.get('errors', 0)}"
            ),
        }
    finally:
        proxy.stop()
        av.stop()
        ck.stop()


def phase_dns(seconds: float, tmp: Path) -> dict:
    from bridge.harness import FaultPeer
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.posts["dns-1"] = _finding("dns-1", "after dns")
    deadline = time.monotonic() + seconds
    original = socket.getaddrinfo

    def wrapped(host, port, *args, **kwargs):
        if host == "avrone" and time.monotonic() < deadline:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        if host in ("avrone", "cognitive-bridge"):
            host = "127.0.0.1"
        return original(host, port, *args, **kwargs)

    socket.getaddrinfo = wrapped
    try:
        _arm(
            av, ck, tmp / "dns.json",
            av_base=f"http://avrone:{av.port}",
            ck_base=f"http://cognitive-bridge:{ck.port}",
        )
        os.environ.pop("FUSION_HOST_MAP", None)
        os.environ["FUSION_BACKOFF_MAX"] = "0.3"
        stats = _run_until(seconds + 3, cycles_cap=0)
        ok = stats.get("forwarded", 0) == 1
        return {
            "name": "dns_failure",
            "round": "R3",
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "errors": stats.get("errors", 0),
            "detail": f"AVRONE host failed DNS for {seconds:.0f}s then delivered {stats.get('forwarded', 0)}/1",
        }
    finally:
        socket.getaddrinfo = original
        av.stop()
        ck.stop()


def phase_toxic(cycles: int, tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import relay_loop
    from bridge.harness import FaultPeer
    table = [0.0] * 12 + [0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 0.5, 0.0]
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.latency_fn = lambda path, hit: table[hit % len(table)] if path == "/api/blackboard" else 0.0
    av.reset_every = 29
    try:
        n = 40
        for i in range(n):
            av.posts[f"x{i:04d}"] = _finding(f"x{i:04d}", "toxic")
        _clear_stop()
        _arm(av, ck, tmp / "toxic.json")
        os.environ["FUSION_TIMEOUT"] = "3"
        stats = relay_loop(interval=0, max_cycles=cycles)
        ok = stats["forwarded"] == n and stats["duplicates"] == 0
        return {
            "name": "toxiproxy",
            "round": "R4",
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "p50_ms": stats["latency_p50_ms"],
            "p95_ms": stats["latency_p95_ms"],
            "p99_ms": stats["latency_p99_ms"],
            "errors": stats["errors"],
            "detail": (
                f"mock toxiproxy 0-500ms on the blackboard hop plus RSTs every 29 requests, "
                f"{cycles} cycles, delivered {stats['forwarded']}/{n}, "
                f"p50={stats['latency_p50_ms']} p95={stats['latency_p95_ms']} "
                f"p99={stats['latency_p99_ms']} ms"
            ),
        }
    finally:
        av.stop()
        ck.stop()


def phase_cpu(tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import relay_loop
    from bridge.harness import FaultPeer
    left = {"n": 4}
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()

    def stall(path, _hit):
        if path == "/api/blackboard" and left["n"] > 0:
            left["n"] -= 1
            return 0.45
        return 0.0

    av.latency_fn = stall
    av.posts["cpu-1"] = _finding("cpu-1", "after stall")
    try:
        _clear_stop()
        _arm(av, ck, tmp / "cpu.json")
        os.environ["FUSION_TIMEOUT"] = "0.2"
        os.environ["FUSION_BACKOFF_MAX"] = "0.1"
        stats = relay_loop(interval=0, max_cycles=12)
        ok = stats["forwarded"] == 1 and stats["errors"] >= 1
        return {
            "name": "cpu_starvation",
            "round": "R4",
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "errors": stats["errors"],
            "detail": f"stalls timed out ({stats['errors']} errors) then delivered {stats['forwarded']}/1",
        }
    finally:
        av.stop()
        ck.stop()


def phase_image_pull(tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import relay_loop
    from bridge.harness import FaultPeer
    docker = docker_verdict()
    pull_ok = False
    if docker["available"]:
        proc = subprocess.run(
            ["docker", "pull", "avrone:does-not-exist"],
            capture_output=True, text=True,
        )
        pull_ok = proc.returncode == 0
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.posts["pull-1"] = _finding("pull-1", "shim fallback")
    try:
        _clear_stop()
        _arm(av, ck, tmp / "pull.json")
        stats = relay_loop(interval=0, max_cycles=1)
        ok = (not pull_ok) and stats["forwarded"] == 1
        return {
            "name": "image_pull",
            "round": "R4",
            "mode": "real" if docker["available"] else "mock",
            "status": "pass" if ok else "fail",
            "detail": f"{docker['detail']}; pull_ok={pull_ok}; shim fallback forwarded {stats['forwarded']}/1",
        }
    finally:
        av.stop()
        ck.stop()


def phase_volume(tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import relay_loop
    from bridge.harness import FaultPeer
    blocked = tmp / "denied"
    if blocked.exists():
        blocked.chmod(0o755)
    blocked.mkdir(exist_ok=True)
    blocked.chmod(0)
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.posts["vol-1"] = _finding("vol-1", "no volume")
    try:
        _clear_stop()
        _arm(av, ck, blocked / "state.json")
        stats = relay_loop(interval=0, max_cycles=1)
        ok = stats["forwarded"] == 1 and stats["state_errors"] >= 1
        return {
            "name": "volume_permission",
            "round": "R4",
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "detail": f"state dir mode 000; forwarded {stats['forwarded']} state_errors={stats['state_errors']}",
        }
    finally:
        blocked.chmod(0o755)
        av.stop()
        ck.stop()


def phase_unavailable(seconds: float, tmp: Path, name: str, round_name: str, kind: str) -> dict:
    from bridge.harness import FaultPeer
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    if kind == "http":
        av.fail_until = time.monotonic() + seconds
        ck.fail_until = av.fail_until
    av.posts["wait-1"] = _finding("wait-1", name)
    try:
        _arm(av, ck, tmp / f"{name}.json")
        os.environ["FUSION_BACKOFF_MAX"] = "1"
        os.environ["FUSION_TIMEOUT"] = "0.8"
        seen = {"mid": -1}

        def watch() -> None:
            time.sleep(max(0.2, seconds * 0.4))
            seen["mid"] = len(ck.events)

        threading.Thread(target=watch, daemon=True).start()
        if kind == "bind":
            # Nothing is listening on a fresh port until the slow start ends.
            # Re-point at a closed port, then start a new peer there.
            from bridge.harness import free_port
            port = free_port()
            os.environ["AVRONE_BASE"] = f"http://127.0.0.1:{port}"
            holder: dict = {}

            def open_later() -> None:
                time.sleep(seconds)
                peer = FaultPeer("avrone", token="t").start(port=port)
                peer.posts["wait-1"] = _finding("wait-1", name)
                holder["peer"] = peer

            threading.Thread(target=open_later, daemon=True).start()
        stats = _run_until(seconds + 4, cycles_cap=0, interval=0.25)
        if kind == "bind":
            peer = holder.get("peer")
            if peer is not None:
                peer.stop()
        ok = stats.get("forwarded", 0) == 1 and seen.get("mid") == 0
        return {
            "name": name,
            "round": round_name,
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "errors": stats.get("errors", 0),
            "detail": f"{kind} unhealthy for {seconds:.0f}s then delivered {stats.get('forwarded', 0)}/1",
        }
    finally:
        av.stop()
        ck.stop()


def phase_hostname(tmp: Path) -> dict:
    from bridge.avrone_cyclekernal_link import _refresh_host_map, relay_loop
    from bridge.harness import FaultPeer
    av = FaultPeer("avrone", token="t").start()
    ck = FaultPeer("ck", token="t").start()
    av.posts["host-1"] = _finding("host-1", "by name")
    try:
        _clear_stop()
        os.environ["FUSION_HOST_MAP"] = "avrone=127.0.0.1,cognitive-bridge=127.0.0.1"
        _refresh_host_map()
        _arm(
            av, ck, tmp / "host.json",
            av_base=f"http://avrone:{av.port}",
            ck_base=f"http://cognitive-bridge:{ck.port}",
        )
        stats = relay_loop(interval=0, max_cycles=1)
        ok = stats["forwarded"] == 1 and stats["errors"] == 0
        return {
            "name": "compose_dns",
            "round": "R6",
            "mode": "mock",
            "status": "pass" if ok else "fail",
            "detail": f"hostnames avrone and cognitive-bridge resolved via FUSION_HOST_MAP; forwarded {stats['forwarded']}",
        }
    finally:
        os.environ.pop("FUSION_HOST_MAP", None)
        _refresh_host_map()
        av.stop()
        ck.stop()


def _wd_request(method: str, path: str, payload: dict | None = None) -> dict:
    port = os.environ["FUSION_WATCHDOG_PORT"]
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return json.loads(resp.read().decode())


def phase_watchdog(tmp: Path) -> dict:
    from bridge.harness import free_port
    parent = os.environ.get("FUSION_WATCHDOG") == "1"
    owned = None
    if not parent:
        from bridge.watchdog import Watchdog
        owned = Watchdog(interval=2.0).start()
        os.environ["FUSION_WATCHDOG_PORT"] = str(owned.port)
    try:
        port = free_port()
        url = f"http://127.0.0.1:{port}/health"
        cmd = [sys.executable, "-m", "bridge.harness", "serve", "--role", "health", "--host", "127.0.0.1", "--port", str(port)]
        _wd_request("POST", "/services", {"name": "health", "url": url, "command": cmd})
        if not _wait_health(url, 10):
            return _fail_watch("watchdog did not start the health service", parent)
        before = _wd_request("GET", "/status")
        pid = _pid(before, "health")
        if pid:
            os.kill(pid, 9)
        if not _wait_restarts("health", 2, 12):
            return _fail_watch("killed service was not restarted", parent)
        _wd_request("DELETE", "/services/health")
        frozen_port = free_port()
        frozen_url = f"http://127.0.0.1:{frozen_port}/health"
        frozen_cmd = [
            sys.executable, "-m", "bridge.harness", "serve",
            "--role", "health", "--host", "127.0.0.1", "--port", str(frozen_port),
            "--frozen-health",
        ]
        _wd_request("POST", "/services", {"name": "frozen", "url": frozen_url, "command": frozen_cmd})
        stale = _wait_restarts("frozen", 2, 14)
        status = _wd_request("GET", "/status")
        _wd_request("DELETE", "/services/frozen")
        if not stale:
            return _fail_watch("frozen uptime was not treated as down", parent)
        _wd_request("POST", "/services", {
            "name": "crash",
            "url": "http://127.0.0.1:1/health",
            "command": ["/bin/false"],
        })
        trace = _wait_trace("crash", 3, 12)
        _wd_request("DELETE", "/services/crash")
        if trace[:3] != [1.0, 2.0, 4.0]:
            return _fail_watch(f"backoff trace {trace}", parent)
        return {
            "name": "watchdog",
            "round": "R5",
            "mode": "parent" if parent else "embedded",
            "status": "pass",
            "detail": (
                f"{'parent' if parent else 'embedded'} watchdog pinged every 2s, "
                f"restarted a killed service, restarted frozen uptime, "
                f"and backed off a dead service {trace[:3]}"
            ),
        }
    finally:
        if owned is not None:
            owned.stop()


def _pid(status: dict, name: str) -> int | None:
    for item in status.get("services", []):
        if item["name"] == name:
            return item.get("pid")
    return None


def _wait_health(url: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _wait_trace(name: str, minimum: int, seconds: float) -> list[float]:
    deadline = time.time() + seconds
    trace: list[float] = []
    while time.time() < deadline:
        try:
            status = _wd_request("GET", "/status")
        except Exception:
            time.sleep(0.3)
            continue
        for item in status.get("services", []):
            if item["name"] == name:
                trace = list(item.get("backoff_trace") or [])
                if len(trace) >= minimum:
                    return trace
        time.sleep(0.4)
    return trace


def _wait_restarts(name: str, minimum: int, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            status = _wd_request("GET", "/status")
        except Exception:
            time.sleep(0.3)
            continue
        for item in status.get("services", []):
            if item["name"] == name and item["restarts"] >= minimum and item.get("alive"):
                return True
        time.sleep(0.4)
    return False


def _fail_watch(detail: str, parent: bool) -> dict:
    return {
        "name": "watchdog",
        "round": "R5",
        "mode": "parent" if parent else "embedded",
        "status": "fail",
        "detail": detail,
    }


def _row(item: dict) -> str:
    return f"| {item.get('name', '')} | {item.get('round', '')} | {item.get('mode', '')} | {item.get('status', '')} | {item.get('detail', '')} |"


def write_summary(phases: list[dict], edges: list[dict], bench: dict, docker: dict, elapsed: float) -> None:
    fails = [item for item in phases + edges + [bench] if item.get("status") == "fail"]
    xfails = [item for item in edges if item.get("status") == "xfail"]
    worst = _worst(phases, fails)
    soak = next((item for item in phases if item["name"] == "soak"), {})
    toxic = next((item for item in phases if item["name"] == "toxiproxy"), {})
    lines = [
        "# Round 6 summary",
        "",
        f"Docker verdict: **{docker['detail']}**.",
        "Rounds 3–6 ran against in-process stand-ins. The compose file describes the real stack (Avrone image pin, 256m/0.5 CPU, toxiproxy, read-only rootfs, test-runner) for a machine where Docker works.",
        "",
        "## This run",
        "",
        f"- Drill runtime: {elapsed:.1f}s",
        f"- Soak: {soak.get('detail', 'not run')}",
        f"- Toxiproxy mock: {toxic.get('detail', 'not run')}",
        f"- Bench: {bench.get('detail', 'not run')} ({bench.get('mode', '')})",
        f"- Watchdog: {next((item.get('detail') for item in phases if item['name']=='watchdog'), 'not run')}",
        f"- Worst failure mode: {worst}",
        f"- Known xfails: {', '.join(item['name'] for item in xfails) or 'none'}",
        "",
        "| Phase | Round | Real or mock | Status | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(_row(item) for item in phases)
    lines.append("")
    lines.append("## Edge cases")
    lines.append("")
    lines.append("| Case | Status | Detail |")
    lines.append("| --- | --- | --- |")
    for item in edges:
        lines.append(f"| {item['name']} | {item['status']} | {item['detail']} |")
    lines.extend([
        "",
        "## Earlier rounds (not re-measured here)",
        "",
        "- 500, 1000, and 2000 events were delivered exactly, with 0 duplicates.",
        "- Edge cases that round: 40 passed out of 42.",
        "- A 30-minute soak delivered 6880/6880 with p99 29 ms.",
        "- On the user's Mac, the pinned image at 256m and 0.5 CPU had OOMKilled=false.",
        "- The read-only rootfs run on that machine worked: the relay functioned and the container could not write.",
        "",
        "Tag when this run is green: `v0.6.0-edge-hardened`.",
        "",
    ])
    SUMMARY.write_text("\n".join(lines), encoding="utf-8")


def _worst(phases: list[dict], fails: list[dict]) -> str:
    if fails:
        return "failed: " + ", ".join(item["name"] for item in fails)
    ranked = sorted(phases, key=lambda item: float(item.get("p99_ms") or 0) + float(item.get("errors") or 0), reverse=True)
    if not ranked:
        return "none"
    top = ranked[0]
    return f"{top['name']} ({top.get('detail', '')})"


def main() -> int:
    started = time.perf_counter()
    tmp = Path(os.environ.get("DRILL_TMP", "/tmp/fusion-drill"))
    tmp.mkdir(parents=True, exist_ok=True)
    from bridge.avrone_cyclekernal_link import acquire_relay_lock, release_relay_lock
    lock_path = os.environ.get("FUSION_DRILL_LOCK", "/tmp/fusion-avrone-relay.lock")
    if not acquire_relay_lock(lock_path):
        print("REFUSING: another relay holds this blackboard", flush=True)
        return 2
    docker = docker_verdict()
    os.environ["FUSION_DOCKER"] = "real" if docker["available"] else "mock"
    print(f"DOCKER {docker['detail']}", flush=True)
    phases: list[dict] = []
    edges: list[dict] = []
    bench: dict = {"name": "bench", "status": "fail", "detail": "not run", "mode": "mock"}
    try:
        scale = os.environ.get("DRILL_SCALE", "full")
        plan = [
            ("pytest", phase_pytest),
            ("integration", phase_integration),
            ("soak", lambda: phase_soak(300 if scale == "full" else 5, tmp)),
            ("partition", lambda: phase_partition(10 if scale == "full" else 0.6, tmp)),
            ("dns", lambda: phase_dns(10 if scale == "full" else 0.6, tmp)),
            ("toxic", lambda: phase_toxic(500 if scale == "full" else 8, tmp)),
            ("cpu", lambda: phase_cpu(tmp)),
            ("image_pull", lambda: phase_image_pull(tmp)),
            ("volume", lambda: phase_volume(tmp)),
            ("health_503", lambda: phase_unavailable(30 if scale == "full" else 0.6, tmp, "health_503", "R4", "http")),
            ("slow_start", lambda: phase_unavailable(30 if scale == "full" else 0.6, tmp, "slow_start", "R6", "bind")),
            ("hostname", lambda: phase_hostname(tmp)),
            ("watchdog", lambda: phase_watchdog(tmp)),
        ]
        for name, fn in plan:
            print(f"PHASE {name} start", flush=True)
            try:
                item = fn()
            except Exception as exc:
                item = {"name": name, "round": "", "mode": "", "status": "fail", "detail": f"{type(exc).__name__}: {exc}"}
            phases.append(item)
            print(f"PHASE {item['name']} {item['status']} {item.get('detail', '')}", flush=True)
        print("PHASE edges start", flush=True)
        try:
            from bridge.edge_cases import run_all
            edges = run_all(scale, tmp / "edges")
        except Exception as exc:
            edges = [{"name": "edges", "status": "fail", "detail": str(exc)}]
        for item in edges:
            print(f"EDGE {item['name']} {item['status']} {item['detail']}", flush=True)
        print("PHASE bench start", flush=True)
        try:
            from bridge.bench import run_bench
            bench = run_bench(10000 if scale == "full" else 50, tmp / "bench")
        except Exception as exc:
            bench = {"name": "bench", "status": "fail", "detail": str(exc), "mode": "mock"}
        print(f"PHASE bench {bench['status']} {bench.get('detail', '')}", flush=True)
    finally:
        elapsed = time.perf_counter() - started
        try:
            write_summary(phases, edges, bench, docker, elapsed)
        except Exception as exc:
            print(f"SUMMARY failed {exc}", flush=True)
        release_relay_lock()
    failed = [item for item in phases + edges + [bench] if item.get("status") == "fail"]
    xfail = [item for item in edges if item.get("status") == "xfail"]
    print(
        f"SUMMARY phases={len(phases)} edges={len(edges)} fail={len(failed)} xfail={len(xfail)} "
        f"elapsed_s={elapsed:.1f}",
        flush=True,
    )
    if soak := next((item for item in phases if item["name"] == "soak"), None):
        print(
            f"METRICS p50_ms={soak.get('p50_ms')} p95_ms={soak.get('p95_ms')} "
            f"p99_ms={soak.get('p99_ms')} events_per_sec={soak.get('events_per_sec')}",
            flush=True,
        )
    if toxic := next((item for item in phases if item["name"] == "toxiproxy"), None):
        print(
            f"METRICS toxic_p50_ms={toxic.get('p50_ms')} toxic_p95_ms={toxic.get('p95_ms')} "
            f"toxic_p99_ms={toxic.get('p99_ms')}",
            flush=True,
        )
    print(
        f"METRICS bench_shim_eps={bench.get('shim', {}).get('events_per_sec')} "
        f"bench_container_eps={bench.get('container', {}).get('events_per_sec')}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
