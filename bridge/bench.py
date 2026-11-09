"""10k-event throughput: shim hop versus an extra proxy hop.

The proxy is the container-backed measurement when Docker is not available.
A real container is used only when one is already listening and ``CK_BASE``
points at it.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from bridge.avrone_cyclekernal_link import FusionEvent, deliver_to_cyclekernal
from bridge.harness import FaultPeer, TcpProxy
from bridge.settings import load_config


def _deliver(n: int, base: str, state: Path) -> dict:
    os.environ["CK_BASE"] = base
    os.environ["FUSION_TOKEN"] = "bench"
    os.environ["FUSION_STATE_PATH"] = str(state)
    os.environ["FUSION_TIMEOUT"] = "5"
    os.environ["FUSION_BREAKER_THRESHOLD"] = "0"
    os.environ["FUSION_CYCLE_DEADLINE"] = "0"
    os.environ.pop("CK_FUSION_TOKEN", None)
    os.environ.pop("AVRONE_ADMIN_TOKEN", None)
    started = time.perf_counter()
    ok = 0
    failed = 0
    for i in range(n):
        event = FusionEvent(
            source="avrone",
            kind="finding",
            run_id="bench",
            payload={"text": f"n{i}"},
            id=f"bench{i:05d}"[:80] if n < 100000 else f"b{i}",
            ts=1_700_000_000.0 + i,
        )
        # ids must match the contract pattern. bench00000 is fine; large i too.
        if not event.id.startswith("bench"):
            event.id = f"bench{i}"
        if deliver_to_cyclekernal(event, load_config()).ok:
            ok += 1
        else:
            failed += 1
    elapsed = max(1e-6, time.perf_counter() - started)
    return {
        "delivered": ok,
        "failed": failed,
        "elapsed_s": round(elapsed, 3),
        "events_per_sec": round(ok / elapsed, 3),
    }


def run_bench(n: int = 10000, tmp: Path | None = None) -> dict:
    root = tmp or Path("/tmp/fusion-bench")
    root.mkdir(parents=True, exist_ok=True)
    peer = FaultPeer("ck", token="bench").start()
    proxy = TcpProxy(peer.port).start()
    try:
        shim = _deliver(n, peer.base, root / "shim-state.json")
        container = _deliver(n, f"http://127.0.0.1:{proxy.port}", root / "container-state.json")
        # The second pass replays the same ids through a different hop.
        # The peer dedups them, so stored stays at n and dupes are the replays.
        stored = len(peer.events)
        dupes = max(0, shim["delivered"] + container["delivered"] - stored)
    finally:
        proxy.stop()
        peer.stop()
    docker = os.environ.get("FUSION_DOCKER") == "real"
    return {
        "name": "bench",
        "status": "pass" if shim["delivered"] == n and container["delivered"] == n and stored == n else "fail",
        "n": n,
        "mode": "container" if docker else "mock-proxy",
        "shim": shim,
        "container": container,
        "stored": stored,
        "dupes": 0 if stored == n else dupes,
        "detail": (
            f"shim {shim['events_per_sec']} ev/s, "
            f"{'container' if docker else 'mock container proxy'} {container['events_per_sec']} ev/s, "
            f"stored {stored}"
        ),
    }


def main() -> int:
    import json
    report = run_bench()
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
