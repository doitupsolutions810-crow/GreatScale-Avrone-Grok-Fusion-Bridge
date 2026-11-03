"""Wire Avrone's agent blackboard to CycleKernal's CognitiveBridge.

Gap 1: Avrone posts findings; CycleKernal never sees them.
Gap 2: CycleKernal's mood stream never reaches Avrone's telemetry.
Gap 3: greatscale evidence snapshots exist as a tool but post to nothing.

This module: one event schema, one HTTP client, one relay loop.
All hops fail soft — one dead side never crashes the other.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


CK_BASE = os.environ.get("CK_BASE", "http://127.0.0.1:8090")
AVRONE_BASE = os.environ.get("AVRONE_BASE", "http://127.0.0.1:8710")


@dataclass
class FusionEvent:
    source: str  # avrone | cyclekernal
    kind: str  # finding | mood | evidence | health
    run_id: str
    payload: dict
    ts: float = 0.0

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _post_json(url: str, body: dict, timeout: float = 5.0) -> bool:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def post_to_avrone_bridge(event: FusionEvent, timeout: float = 5.0) -> bool:
    """Send a CycleKernal mood/evidence event into Avrone's blackboard."""
    body = {
        "agent": f"cyclekernal:{event.kind}",
        "kind": "finding" if event.kind != "mood" else "conclusion",
        "text": json.dumps(event.payload)[:4000],
        "role": "cyclekernal-relay",
    }
    return _post_json(f"{AVRONE_BASE}/blackboard", body, timeout=timeout)


def post_to_cyclekernal(event: FusionEvent, timeout: float = 5.0) -> bool:
    """Send an Avrone finding into CycleKernal's CognitiveBridge."""
    body = {
        "source": event.source,
        "kind": event.kind,
        "run_id": event.run_id,
        "payload": event.payload,
        "ts": event.ts,
    }
    # Prefer /events (standalone) then /api/events for full stack compatibility
    if _post_json(f"{CK_BASE}/events", body, timeout=timeout):
        return True
    return _post_json(f"{CK_BASE}/api/events", body, timeout=timeout)


def _get_json(url: str, timeout: float = 5.0) -> Any | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def relay_loop(interval: float = 2.0, max_cycles: int = 100) -> dict:
    """Poll Avrone's blackboard, forward new events to CycleKernal,
    and mirror CycleKernal health back. Fails soft on any single hop.

    Returns stats including forwarded, seen, health_mirrored, errors, cycles.
    """
    seen_ids: set[str] = set()
    forwarded = 0
    health_mirrored = 0
    errors = 0

    for cycle in range(max_cycles):
        # Pull from Avrone blackboard
        entries = _get_json(f"{AVRONE_BASE}/blackboard")
        if entries is None:
            errors += 1
            entries = []
        elif not isinstance(entries, list):
            errors += 1
            entries = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("id") or json.dumps(entry, sort_keys=True, default=str)
            if eid in seen_ids:
                continue
            seen_ids.add(eid)

            # Prefer structured payload; fall back to text/raw entry
            payload = entry.get("payload")
            if payload is None:
                text = entry.get("text")
                if isinstance(text, str) and text.startswith("{"):
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = {"text": text}
                else:
                    payload = entry

            event = FusionEvent(
                source="avrone",
                kind=entry.get("kind", "finding"),
                run_id=str(entry.get("run_id", "unknown")),
                payload=payload if isinstance(payload, dict) else {"raw": payload},
            )
            if post_to_cyclekernal(event):
                forwarded += 1
            else:
                errors += 1

        # Mirror CycleKernal health back to Avrone
        health = _get_json(f"{CK_BASE}/health")
        if health is not None:
            ok = post_to_avrone_bridge(
                FusionEvent(
                    source="cyclekernal",
                    kind="health",
                    run_id="relay",
                    payload=health if isinstance(health, dict) else {"status": str(health)},
                )
            )
            if ok:
                health_mirrored += 1
            else:
                errors += 1
        else:
            errors += 1

        if interval > 0:
            time.sleep(interval)

    return {
        "forwarded": forwarded,
        "seen": len(seen_ids),
        "health_mirrored": health_mirrored,
        "errors": errors,
        "cycles": max_cycles,
    }


if __name__ == "__main__":
    print(relay_loop(interval=1.0, max_cycles=5))
