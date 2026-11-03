"""Demo against the local stand-ins.

The proof that the relay matches CONTRACT.md is ``pytest tests/``.
This script only checks that the stand-in processes speak the same routes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = os.environ.get("FUSION_TOKEN", "dev-demo-token")


def _wait_for(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _post(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Admin-Token": TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except Exception as exc:
        return 0, {"error": str(exc)}


def _get(url: str, token: bool = False) -> dict | None:
    headers = {"X-Admin-Token": TOKEN} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _dump_logs(logs: list) -> None:
    for log in logs:
        log.seek(0)
        text = log.read().decode("utf-8", errors="replace")[-2000:]
        if text.strip():
            print(text)


def main() -> int:
    env = os.environ.copy()
    env.update({
        "FUSION_TOKEN": TOKEN,
        "AVRONE_BASE": "http://127.0.0.1:8710",
        "CK_BASE": "http://127.0.0.1:8090",
        "AVRONE_PORT": "8710",
        "CK_PORT": "8090",
        "PYTHONPATH": str(ROOT),
    })
    procs = []
    logs = []
    try:
        for module in ("bridge.cognitive_bridge_standalone", "bridge.avrone_shim"):
            log = tempfile.TemporaryFile()
            logs.append(log)
            procs.append(subprocess.Popen(
                [sys.executable, "-m", module],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            ))
        if not _wait_for("http://127.0.0.1:8090/health"):
            print("FAIL: CognitiveBridge stand-in did not start")
            _dump_logs(logs)
            return 1
        if not _wait_for("http://127.0.0.1:8710/api/status"):
            print("FAIL: Avrone stand-in did not start")
            _dump_logs(logs)
            return 1

        os.environ.update({
            "FUSION_TOKEN": TOKEN,
            "AVRONE_BASE": "http://127.0.0.1:8710",
            "CK_BASE": "http://127.0.0.1:8090",
            "FUSION_STATE_PATH": "",
        })
        status, posted = _post("http://127.0.0.1:8710/api/blackboard", {
            "id": "demo00000001",
            "agent": "avrone:demo",
            "kind": "finding",
            "text": "stand-in finding",
            "run_id": "demo",
            "role": "agent",
            "origin_node": "local",
        })
        if status != 200 or posted.get("duplicate"):
            print(f"FAIL: could not post to Avrone stand-in ({status} {posted})")
            _dump_logs(logs)
            return 1

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from bridge.avrone_cyclekernal_link import relay_loop

        result = relay_loop(interval=0, max_cycles=1)
        events = _get("http://127.0.0.1:8090/v1/events")
        board = _get("http://127.0.0.1:8710/api/blackboard", token=True)
        got = [event for event in (events or {}).get("events", []) if event.get("id") == "demo00000001"]
        mirrors = [post for post in (board or {}).get("posts", []) if post.get("origin_node") == "fusion-bridge"]
        if result["forwarded"] < 1 or not got or len(mirrors) < 1:
            print(f"FAIL: forwarded={result['forwarded']} events={len(got)} mirrors={len(mirrors)} stats={result}")
            _dump_logs(logs)
            return 1
        print(
            f"PASS: forwarded={result['forwarded']} mood_mirrored={result['mood_mirrored']} "
            f"health_mirrored={result['health_mirrored']}"
        )
        return 0
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
