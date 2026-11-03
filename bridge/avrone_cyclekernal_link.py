"""Wire Avrone's agent blackboard to CycleKernal's CognitiveBridge.

Gap 1: Avrone posts findings; CycleKernal never sees them.
Gap 2: CycleKernal's mood stream never reaches Avrone's telemetry.
Gap 3: greatscale evidence snapshots exist as a tool but post to nothing.

This module: one event schema, one HTTP client, one relay loop.
All hops fail soft — one dead side never crashes the other.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import asdict, dataclass


CK_BASE = "http://127.0.0.1:8090"       # CycleKernal CognitiveBridge
AVRONE_BASE = "http://127.0.0.1:8710"  # Avrone blackboard (or shim)


@dataclass
class FusionEvent:
    source: str      # avrone | cyclekernal
    kind: str        # finding | mood | evidence | health
    run_id: str
    payload: dict
    ts: float = 0.0

    def __post_init__(self):
        if not self.ts:
            self.ts = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def post_to_avrone_bridge(event: FusionEvent, timeout: float = 5.0) -> bool:
    """Send a CycleKernal mood/evidence event into Avrone's blackboard."""
    body = json.dumps({
        "agent": f"cyclekernal:{event.kind}",
        "kind": "finding" if event.kind != "mood" else "conclusion",
        "text": json.dumps(event.payload)[:4000],
        "role": "cyclekernal-relay",
    }).encode()
    req = urllib.request.Request(
        f"{AVRONE_BASE}/blackboard", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def post_to_cyclekernal(event: FusionEvent, timeout: float = 5.0) -> bool:
    """Send an Avrone finding into CycleKernal's CognitiveBridge."""
    body = json.dumps({
        "source": event.source,
        "kind": event.kind,
        "run_id": event.run_id,
        "payload": event.payload,
        "ts": event.ts,
    }).encode()
    req = urllib.request.Request(
        f"{CK_BASE}/events", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def relay_loop(interval: float = 2.0, max_cycles: int = 100) -> dict:
    """Poll Avrone's blackboard, forward new events to CycleKernal,
    and mirror CycleKernal health back. Fails soft on any single hop."""
    seen_ids: set = set()
    forwarded = 0
    for cycle in range(max_cycles):
        # Pull from Avrone blackboard
        try:
            with urllib.request.urlopen(f"{AVRONE_BASE}/blackboard", timeout=5) as resp:
                entries = json.loads(resp.read().decode())
        except Exception:
            entries = []

        for entry in entries:
            eid = entry.get("id") or json.dumps(entry, sort_keys=True)
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            event = FusionEvent(
                source="avrone",
                kind=entry.get("kind", "finding"),
                run_id=entry.get("run_id", "unknown"),
                payload=entry.get("payload", entry),
            )
            if post_to_cyclekernal(event):
                forwarded += 1

        # Mirror CycleKernal health back to Avrone
        try:
            with urllib.request.urlopen(f"{CK_BASE}/health", timeout=5) as resp:
                health = json.loads(resp.read().decode())
            post_to_avrone_bridge(FusionEvent(
                source="cyclekernal", kind="health",
                run_id="relay", payload=health,
            ))
        except Exception:
            pass

        time.sleep(interval)

    return {"forwarded": forwarded, "seen": len(seen_ids), "cycles": max_cycles}


if __name__ == "__main__":
    print(relay_loop(interval=1.0, max_cycles=5))
