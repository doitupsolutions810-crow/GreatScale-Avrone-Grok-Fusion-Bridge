"""Standalone CognitiveBridge — CycleKernal's trait service without the full stack.

FastAPI app on port 8090. Same trait-update rules CycleKernal uses:
mood nudges stability and focus, evidence shifts coherence and caution,
findings tick curiosity. Decay pulls everything toward 0.5 baseline.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="CognitiveBridge Standalone")

TRAITS = {
    "stability": 0.5,
    "focus": 0.5,
    "coherence": 0.5,
    "caution": 0.5,
    "curiosity": 0.5,
}

EVENTS: list[dict] = []
SEEN_EVENTS: set[tuple[str, str]] = set()
_CK_STARTED = time.time()
_CK_BOOT = os.urandom(6).hex()
_CK_RESTARTS = int(os.environ.get("CK_RESTARTS", "0") or "0")


class EventIn(BaseModel):
    source: str
    kind: str
    run_id: str
    payload: dict = {}
    ts: float = 0.0


@app.get("/health")
def health():
    now = time.time()
    return {
        "status": "ok",
        "version": "standalone",
        "traits": TRAITS,
        "events_seen": len(EVENTS),
        "ts": now,
        "started_at": _CK_STARTED,
        "uptime_s": round(now - _CK_STARTED, 3),
        "restarts": _CK_RESTARTS,
        "boot": _CK_BOOT,
        "mood": "neutral",
    }


@app.get("/v1/current_mood")
def current_mood():
    return {
        "mood": "neutral",
        "traits": ["Neutral-Baseline"],
        "mood_dot": 0.5,
        "ns_fitness": 0.0,
        "genome_id": "gn-standalone",
        "promote": {"action": "hold"},
        "timestamp": time.time(),
        "metrics": {"tick": time.time()},
    }


def _token_eq(got: str, expected: str) -> bool:
    if not expected:
        return False
    left, right = got.encode("utf-8"), expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


@app.post("/v1/events")
@app.post("/api/events")
async def contract_events(request: Request):
    """Contract intake. ``/api/events`` stays for the legacy helper. 413 is before auth."""
    limit = 4096
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            return JSONResponse({"detail": "bad content-length"}, status_code=400)
        if size > limit:
            return JSONResponse(
                {"detail": f"request body too large (max {limit} bytes)"},
                status_code=413,
            )
    raw = await request.body()
    if len(raw) > limit:
        return JSONResponse({"detail": f"request body too large (max {limit} bytes)"}, status_code=413)
    expected = (os.environ.get("CK_FUSION_TOKEN") or os.environ.get("FUSION_TOKEN") or "").strip()
    if expected:
        got = request.headers.get("x-admin-token") or ""
        if not _token_eq(got, expected):
            return JSONResponse({"detail": "Bad admin token"}, status_code=401)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"detail": "body must be JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"detail": "body must be JSON"}, status_code=400)
    kind = str(data.get("kind") or "finding")
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    source = str(data.get("source") or "avrone")
    run_id = str(data.get("run_id") or "run")
    try:
        ts = float(data.get("ts") or time.time())
    except (TypeError, ValueError):
        ts = time.time()
    eid = str(data.get("id") or "")
    key = (source, eid) if eid else None
    if key and key in SEEN_EVENTS:
        return {"accepted": True, "duplicate": True, "id": eid, "traits": TRAITS}
    event = EventIn(source=source, kind=kind, run_id=run_id, payload=payload, ts=ts)
    EVENTS.append(event.model_dump())
    if key:
        SEEN_EVENTS.add(key)
    _apply_trait_update(event)
    return {"accepted": True, "duplicate": False, "id": eid or "accepted", "traits": TRAITS}


@app.get("/traits")
def traits():
    return TRAITS


@app.post("/events")
def receive_event(event: EventIn):
    if not event.ts:
        event.ts = time.time()
    EVENTS.append(event.model_dump())
    _apply_trait_update(event)
    return {"accepted": True, "traits": TRAITS}


@app.post("/reset")
def reset():
    for k in TRAITS:
        TRAITS[k] = 0.5
    EVENTS.clear()
    return {"reset": True}


def _apply_trait_update(event: EventIn) -> None:
    """CycleKernal's trait-update rules, simplified."""
    kind = event.kind
    payload = event.payload or {}
    if kind == "mood":
        mood = payload.get("mood", "neutral")
        delta = 0.02 if mood in ("positive", "calm", "focused") else -0.02
        TRAITS["stability"] = _clamp(TRAITS["stability"] + delta)
        TRAITS["focus"] = _clamp(TRAITS["focus"] + delta * 0.5)
    elif kind == "evidence":
        strength = float(payload.get("strength", 0.5))
        TRAITS["coherence"] = _clamp(TRAITS["coherence"] + (strength - 0.5) * 0.1)
        TRAITS["caution"] = _clamp(TRAITS["caution"] + (0.5 - strength) * 0.05)
    elif kind == "finding":
        TRAITS["curiosity"] = _clamp(TRAITS["curiosity"] + 0.02)
    # Decay toward baseline
    for k in TRAITS:
        TRAITS[k] = _clamp(TRAITS[k] + (0.5 - TRAITS[k]) * 0.01)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8090)
