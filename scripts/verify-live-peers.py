#!/usr/bin/env python3
"""Exercise actual AVRONE, CycleKernal and WS relay checkouts on loopback.

Requires their normal Python dependencies. No HTTP peer is replaced with a stub.
The explicit test metrics do not represent physical telemetry or model inference.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge.avrone_cyclekernal_link import relay_loop
from bridge.greatscale_evidence import deliver_evidence, evidence_event
from bridge.settings import load_config


def wait_for(probe, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    raise AssertionError("timeout waiting for " + description)


def local_revision(source):
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source,
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError:
        return None


def git_state(source):
    if local_revision(source) is None:
        return None
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=source, text=True).strip()
    changed = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).splitlines()
    return {"revision": local_revision(source), "tree": tree, "changed_files": changed}


class Service:
    def __init__(self, name, cwd, module, env, directory, port=0):
        self.log_path = directory / (name + ".log")
        self.log = self.log_path.open("a")
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            self.port = listener.getsockname()[1]
            self.base = "http://127.0.0.1:" + str(self.port)
            self.process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", module, "--fd", str(listener.fileno()),
                 "--log-level", "warning", "--no-access-log"],
                cwd=cwd, env=env, pass_fds=(listener.fileno(),),
                stdout=self.log, stderr=subprocess.STDOUT,
            )

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.log.close()


def verify(avrone_source, cycle_source, directory):
    directory.mkdir(parents=True, exist_ok=True)
    sources = {
        "avrone/server.py": avrone_source / "avrone/server.py",
        "avrone/bridge.py": avrone_source / "avrone/bridge.py",
        "avrone/greatscale.py": avrone_source / "avrone/greatscale.py",
        "avrone/trinity.py": avrone_source / "avrone/trinity.py",
        "avrone/swarm_client.py": avrone_source / "avrone/swarm_client.py",
        "avrone/swarm_outbox.py": avrone_source / "avrone/swarm_outbox.py",
        "avrone/swarm_runtime.py": avrone_source / "avrone/swarm_runtime.py",
        "cycle/src/bridge/main.py": cycle_source / "src/bridge/main.py",
        "cycle/src/bridge/traits.py": cycle_source / "src/bridge/traits.py",
        "cycle/src/bridge/ns_fusion.py": cycle_source / "src/bridge/ns_fusion.py",
        "cycle/src/relay/main.py": cycle_source / "src/relay/main.py",
        "fusion/bridge/avrone_cyclekernal_link.py": ROOT / "bridge/avrone_cyclekernal_link.py",
        "fusion/bridge/greatscale_evidence.py": ROOT / "bridge/greatscale_evidence.py",
    }
    source_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}
    avrone_git = git_state(avrone_source)
    token = secrets.token_hex(32)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AVRONE_", "CK_", "FUSION_", "WS_RELAY_"))}
    env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    env.update(AVRONE_ENV_FILE=str(directory / "absent.env"), AVRONE_RUNNER="local",
               AVRONE_LLM_MODE="mock", AVRONE_ADMIN_TOKEN=token,
               AVRONE_WORKSPACE=str(directory / "workspace"),
               AVRONE_DATA_DIR=str(directory / "avrone-data"))
    headers = {"X-Admin-Token": token}
    checks = []

    def checked(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    with ExitStack() as stack:
        http = stack.enter_context(httpx.Client(timeout=3, trust_env=False))

        def start(name, cwd, module, settings, port=0):
            service = Service(name, cwd, module, settings, directory, port)
            stack.callback(service.close)
            return service

        relay = start("ws-relay", cycle_source / "src/relay", "main:app", env)
        wait_for(lambda: http.get(relay.base + "/health").status_code == 200, "WS relay")
        ck_env = dict(env, CK_FUSION_TOKEN=token, CK_EVENTS_PATH=str(directory / "events.json"),
                      WS_RELAY_URL=relay.base.replace("http:", "ws:") + "/ws/out",
                      CK_RELAY_BACKOFF_INITIAL="0.05", CK_RELAY_BACKOFF_MAX="0.1")
        ck = start("cognitive-bridge", cycle_source / "src/bridge", "main:app", ck_env)
        av = start("avrone", avrone_source, "avrone.server:app", env)
        wait_for(lambda: http.get(av.base + "/api/status").status_code == 200, "AVRONE")
        wait_for(lambda: http.get(ck.base + "/health").status_code == 200, "CycleKernal")
        wait_for(lambda: http.get(relay.base + "/health").json()["subscribers"] == 1,
                 "actual WS subscriber")
        checked("relay_mode_without_synthetic_heartbeat",
                http.get(ck.base + "/health").json()["mode"] == "relay")
        metrics = {"Core": .42, "U2": .31, "L2": .07, "L3": .55,
                   "L4": .28, "L5": .65, "entropy": .65, "coupling": .07}
        checked("actual_ws_publish", http.post(relay.base + "/publish", json={"metrics": metrics})
                .json()["delivered"] == 1)
        wait_for(lambda: http.get(ck.base + "/health").json()["relay"]["frames_accepted"] == 1,
                 "metric frame ingestion")
        http.post(relay.base + "/publish", content='{"Core":NaN}').raise_for_status()
        wait_for(lambda: http.get(ck.base + "/health").json()["relay"]["frames_rejected"] == 1,
                 "invalid metric frame rejection")
        checked("invalid_frame_preserves_ws_subscription",
                http.get(relay.base + "/health").json()["subscribers"] == 1)
        checked("blackboard_missing_auth", http.get(av.base + "/api/blackboard").status_code == 401)
        checked("event_missing_auth", http.get(ck.base + "/v1/events").status_code == 401)
        swarm_task = {"message": "Explicit signing-gate test", "mode": "squad", "dispatch": "swarm"}
        checked("actual_swarm_chat_requires_operator",
                http.post(av.base + "/api/chat", json=swarm_task).status_code == 401)
        blocked = http.post(av.base + "/api/chat", json=swarm_task, headers=headers)
        checked("actual_missing_swarm_trust_fails_closed",
                blocked.status_code == 200 and blocked.json().get("status") == "disabled"
                and blocked.json().get("agents") == []
                and blocked.json().get("swarm", {}).get("reason") == "swarm_trust_configuration_unavailable")
        for label, url in (("blackboard", av.base + "/api/blackboard"),
                           ("events", ck.base + "/v1/events")):
            checked(label + "_body_cap_before_auth",
                    http.post(url, content=b"x" * 4097).status_code == 413)
        finding = {"id": "network-proof-1", "run_id": "cross-repo-check", "agent": "Verifier",
                   "role": "Tester", "kind": "finding", "text": "Explicit network test finding"}
        checked("blackboard_finding_accepted", http.post(av.base + "/api/blackboard", json=finding,
                                                         headers=headers).status_code == 200)
        cfg = replace(load_config(), avrone_base=av.base, ck_base=ck.base,
                      avrone_token=token, ck_token=token, timeout=2, interval=0,
                      state_path=str(directory / "relay-state.json"), backoff_base=0,
                      backoff_max=0, events_path="/v1/events", cycle_deadline=5)
        stats = relay_loop(max_cycles=10, config=cfg)
        checked("ten_cycles_without_errors", stats["errors"] == 0 and stats["cycles"] == 10)
        events = http.get(ck.base + "/v1/events", headers=headers).json()["events"]
        checked("finding_forwarded_once", len(events) == 1 and events[0]["id"] == finding["id"])
        board = http.get(av.base + "/api/blackboard", headers=headers).json()["posts"]
        checked("mood_and_health_mirrored",
                {p["agent"] for p in board} >= {"cyclekernal:mood", "cyclekernal:health"})
        again = relay_loop(max_cycles=10, config=cfg)
        checked("restart_of_relay_does_not_duplicate_or_echo",
                again["errors"] == 0 and again["forwarded"] == 0 and
                len(http.get(ck.base + "/v1/events", headers=headers).json()["events"]) == 1)
        sys.path.insert(0, str(avrone_source))
        from avrone.greatscale import evidence_snapshot
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=avrone_source,
                                            text=True).strip()
        snapshot = evidence_snapshot(0, len(checks))
        # These counts describe assertions completed above, not inference or hardware.
        evidence = evidence_event(snapshot, run_id="cross-repo-check", source_revision=revision)
        hop = deliver_evidence(snapshot, run_id="cross-repo-check", source_revision=revision, config=cfg)
        checked("actual_evidence_delivery", hop.ok)
        checked("evidence_retry_deduplicated",
                deliver_evidence(snapshot, run_id="cross-repo-check", source_revision=revision,
                                 config=cfg).body.get("duplicate") is True)
        saved = http.get(ck.base + "/v1/events", headers=headers).json()["events"]
        checked("evidence_payload_preserved", next(e for e in saved if e["id"] == evidence.id)
                ["payload"]["snapshot"] == snapshot)
        old_ck_boot = http.get(ck.base + "/health").json()["boot"]
        old_ck_port = ck.port
        ck.close()
        ck = start("cognitive-bridge-restart", cycle_source / "src/bridge", "main:app", ck_env, old_ck_port)
        wait_for(lambda: http.get(ck.base + "/health").status_code == 200, "CycleKernal restart")
        checked("event_store_survives_service_restart",
                len(http.get(ck.base + "/v1/events", headers=headers).json()["events"]) == 2)
        checked("ck_boot_changes", http.get(ck.base + "/health").json()["boot"] != old_ck_boot)
        after_ck = relay_loop(max_cycles=5, config=cfg)
        checked("ck_restart_reconciled_without_duplicates",
                after_ck["errors"] == 0 and after_ck["reconciled"] >= 1 and
                len(http.get(ck.base + "/v1/events", headers=headers).json()["events"]) == 2)
        old_av_boot = http.get(av.base + "/api/status").json()["boot"]
        old_av_port = av.port
        av.close()
        av = start("avrone-restart", avrone_source, "avrone.server:app", env, old_av_port)
        wait_for(lambda: http.get(av.base + "/api/status").status_code == 200, "AVRONE restart")
        checked("avrone_boot_changes", http.get(av.base + "/api/status").json()["boot"] != old_av_boot)
        checked("blackboard_survives_service_restart",
                finding["id"] in {p["id"] for p in http.get(av.base + "/api/blackboard", headers=headers)
                                  .json()["posts"]})
        after_av = relay_loop(max_cycles=25, config=cfg)
        checked("avrone_restart_and_50_total_cycles_without_echo",
                after_av["errors"] == 0 and after_av["reconciled"] >= 1 and
                len(http.get(ck.base + "/v1/events", headers=headers).json()["events"]) == 2)
        if source_hashes != {name: hashlib.sha256(path.read_bytes()).hexdigest()
                             for name, path in sources.items()}:
            raise AssertionError("runtime source files changed during the network test")
        return {"ok": True, "checks_passed": len(checks), "checks": checks,
                "relay_cycles": 50, "test_feed": "explicit metrics, no synthetic heartbeat",
                "avrone_local_revision": revision,
                "avrone_git": avrone_git,
                "source_sha256": source_hashes,
                "cycle_local_revision": local_revision(cycle_source),
                "inference": "AVRONE mock model mode; no inference or hardware certification",
                "stats": [stats, again, after_ck, after_av]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--avrone-source", type=Path, required=True)
    parser.add_argument("--cycle-source", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fusion-live-") as temporary:
        directory = Path(temporary)
        try:
            report = verify(args.avrone_source.resolve(), args.cycle_source.resolve(), directory)
        except Exception:
            for log in directory.glob("*.log"):
                print(log.name + "\n" + log.read_text()[-5000:], file=sys.stderr)
            raise
    raw = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(raw)
    print(raw, end="")


if __name__ == "__main__":
    main()
