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


RELAY_ROLE = "cyclekernal-relay"


def _avrone_headers() -> dict:
    """X-Admin-Token for Avrone's /api/blackboard (AVRONE_ADMIN_TOKEN, env only, never logged)."""
    tok = os.environ.get("AVRONE_ADMIN_TOKEN", "")
    return {"X-Admin-Token": tok} if tok else {}


def _ck_headers() -> dict:
    """Bearer token for CycleKernal POST /events (CK_FUSION_TOKEN, falling back to CK_EVENTS_TOKEN)."""
    tok = os.environ.get("CK_FUSION_TOKEN") or os.environ.get("CK_EVENTS_TOKEN") or ""
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _post_json(url: str, body: dict, timeout: float = 5.0, headers: dict | None = None) -> bool:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
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
        "role": RELAY_ROLE,
    }
    # Real Avrone serves the token-gated /api/blackboard; the local shim serves /blackboard.
    if _post_json(f"{AVRONE_BASE}/api/blackboard", body, timeout=timeout, headers=_avrone_headers()):
        return True
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
    auth = _ck_headers()
    if _post_json(f"{CK_BASE}/events", body, timeout=timeout, headers=auth):
        return True
    return _post_json(f"{CK_BASE}/api/events", body, timeout=timeout, headers=auth)


def _get_json(url: str, timeout: float = 5.0, headers: dict | None = None) -> Any | None:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def fetch_blackboard(timeout: float = 5.0) -> list | None:
    """Avrone blackboard entries, or None when unreachable.

    Tries the real token-gated GET /api/blackboard (returns {"posts": [...]}) first, then
    the shim's GET /blackboard (returns a bare list)."""
    for url, headers in ((f"{AVRONE_BASE}/api/blackboard", _avrone_headers()), (f"{AVRONE_BASE}/blackboard", {})):
        data = _get_json(url, timeout=timeout, headers=headers)
        if isinstance(data, dict) and isinstance(data.get("posts"), list):
            return data["posts"]
        if isinstance(data, list):
            return data
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
        entries = fetch_blackboard()
        if entries is None:
            errors += 1
            entries = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Our own mirrored posts must not be relayed back to CycleKernal (feedback loop).
            if entry.get("role") == RELAY_ROLE:
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
