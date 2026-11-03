"""Standalone CognitiveBridge — CycleKernal's trait service without the full stack.

FastAPI app on port 8090. Same trait-update rules CycleKernal uses:
mood nudges stability and focus, evidence shifts coherence and caution,
findings tick curiosity. Decay pulls everything toward 0.5 baseline.
"""
from __future__ import annotations

import time
from fastapi import FastAPI
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


class EventIn(BaseModel):
    source: str
    kind: str
    run_id: str
    payload: dict = {}
    ts: float = 0.0


@app.get("/health")
def health():
    return {"status": "ok", "traits": TRAITS, "events_seen": len(EVENTS), "ts": time.time()}


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
