from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

CK_BASE = "http://127.0.0.1:8090"  # CycleKernal CognitiveBridge
AVRONE_BASE = "http://127.0.0.1:8080"  # Avrone server


@dataclass
class FusionEvent:
    """One event schema shared by both systems."""
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


def post_to_avrone_bridge(event: FusionEvent, timeout: float = 5.0) -> bool:
    """Send a CycleKernal mood/evidence event into Avrone's blackboard."""
    body = json.dumps(
        {
            "agent": f"cyclekernal:{event.kind}",
            "kind": "finding" if event.kind != "mood" else "conclusion",
            "text": json.dumps(event.payload)[:4000],
            "role": "cyclekernal-relay",
        }
    ).encode()
    req = urllib.request.Request(
        f"{AVRONE_BASE}/api/bridge/post",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def post_to_cyclekernal(event: FusionEvent, timeout: float = 5.0) -> bool:
    """Send an Avrone finding into CycleKernal's CognitiveBridge."""
    body = json.dumps(
        {
            "source": event.source,
            "kind": event.kind,
            "run_id": event.run_id,
            "payload": event.payload,
            "ts": event.ts,
        }
    ).encode()
    req = urllib.request.Request(
        f"{CK_BASE}/api/events",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def relay_loop(
    interval: float = 2.0,
    max_cycles: int = 100,
    avrone_url: str = f"{AVRONE_BASE}/api/blackboard",
    ck_health_url: str = f"{CK_BASE}/api/health",
) -> dict[str, Any]:
    """Poll Avrone's blackboard, forward new events to CycleKernal,
    and mirror CycleKernal health back. Fails soft on any single hop."""
    seen_ids: set[str] = set()
    stats = {"forwarded": 0, "health_mirrored": 0, "errors": 0}

    for _ in range(max_cycles):
        # Pull from Avrone blackboard
        try:
            with urllib.request.urlopen(avrone_url, timeout=5) as resp:
                entries = json.loads(resp.read().decode())
        except Exception:
            stats["errors"] += 1
            time.sleep(interval)
            continue

        if not isinstance(entries, list):
            stats["errors"] += 1
            time.sleep(interval)
            continue

        for entry in entries:
            eid = str(entry.get("id", ""))
            if not eid or eid in seen_ids:
                continue
            seen_ids.add(eid)
            event = FusionEvent(
                source="avrone",
                kind=str(entry.get("kind", "finding")),
                run_id=str(entry.get("run_id", "")),
                payload=entry.get("payload") or {"text": entry.get("text", "")},
            )
            if post_to_cyclekernal(event):
                stats["forwarded"] += 1
            else:
                stats["errors"] += 1

        # Mirror CycleKernal health back to Avrone
        try:
            with urllib.request.urlopen(ck_health_url, timeout=5) as resp:
                health = json.loads(resp.read().decode())
            h_event = FusionEvent(
                source="cyclekernal",
                kind="health",
                run_id="health-check",
                payload=health,
            )
            if post_to_avrone_bridge(h_event):
                stats["health_mirrored"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1

        time.sleep(interval)

    return stats


if __name__ == "__main__":
    print(relay_loop(max_cycles=3))
