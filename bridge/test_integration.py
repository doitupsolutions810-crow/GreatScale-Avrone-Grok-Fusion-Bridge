"""Integration test: start both daemons, post one finding from Avrone,
assert it arrives at CycleKernal's traits endpoint with the right nudge,
then tear everything down. This is the proof the fiction is gone."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request


def _wait_for(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _post(url: str, payload: dict) -> bool:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    py = sys.executable
    procs = []
    try:
        # Start CognitiveBridge on 8090
        p1 = subprocess.Popen(
            [py, "cognitive_bridge_standalone.py"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p1)
        # Start Avrone shim on 8710
        p2 = subprocess.Popen(
            [py, "avrone_shim.py"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p2)

        if not _wait_for("http://127.0.0.1:8090/health"):
            print("FAIL: CognitiveBridge did not start")
            return 1
        if not _wait_for("http://127.0.0.1:8710/health"):
            print("FAIL: Avrone shim did not start")
            return 1

        # Reset traits to clean baseline
        _post("http://127.0.0.1:8090/reset", {})

        # Post one finding from the Avrone side
        ok = _post("http://127.0.0.1:8710/blackboard", {
            "agent": "avrone:test-agent",
            "kind": "finding",
            "text": json.dumps({"summary": "integration test finding", "score": 0.8}),
            "role": "agent",
        })
        if not ok:
            print("FAIL: could not post to Avrone shim")
            return 1

        # Run one relay cycle
        from avrone_cyclekernal_link import relay_loop
        result = relay_loop(interval=0.5, max_cycles=3)

        # Check traits moved
        with urllib.request.urlopen("http://127.0.0.1:8090/traits") as resp:
            traits = json.loads(resp.read().decode())

        curiosity = traits.get("curiosity", 0.5)
        if curiosity <= 0.5:
            print(f"FAIL: curiosity did not nudge (got {curiosity})")
            return 1
        if result["forwarded"] < 1:
            print(f"FAIL: relay forwarded {result['forwarded']} events")
            return 1

        print(f"PASS: curiosity={curiosity:.3f}, forwarded={result['forwarded']}, "
              f"seen={result['seen']}")
        return 0
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
