"""Local stand-in for the CycleKernal half of ``CONTRACT.md``.

The real CognitiveBridge (``src/bridge/main.py``, port 8090) serves
``GET /v1/current_mood``, ``GET /health``, and ``WS /ws/hud``. It does not
serve ``POST /v1/events``. This process implements that missing route and
keeps a static mood document shaped like ``CognitiveState.to_dict()`` so the
relay has something real to mirror.

Posting an event does not change the mood. On the real service, mood comes
from LoopMem metrics, not from Avrone findings.

``GET /v1/events`` is a stand-in debug read. CycleKernal is not required to
implement it.
"""
from __future__ import annotations

import hmac
import os
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from .contract import MAX_BODY_BYTES, EventLog, normalize_event
except ImportError:  # python bridge/cognitive_bridge_standalone.py
    from contract import MAX_BODY_BYTES, EventLog, normalize_event

app = FastAPI(title="CognitiveBridge contract stand-in", version="contract-1")
_lock = threading.Lock()
_log = EventLog()
_mood = {
    "mood": "neutral",
    "traits": ["Neutral-Baseline"],
    "mood_dot": 0.5,
    "ns_fitness": 0.0,
    "genome_id": "gn-standalone",
    "system_prompt_override": "",
    "explanation": "Stand-in mood. Not driven by LoopMem.",
    "metrics": {},
    "entropy": 0.0,
    "coupling": 0.0,
    "core": 0.5,
    "colony": {},
    "promote": {"action": "hold"},
}


def _limit() -> int:
    raw = (os.environ.get("FUSION_MAX_BODY_BYTES") or "").strip()
    try:
        return int(raw) if raw else MAX_BODY_BYTES
    except ValueError:
        return MAX_BODY_BYTES


def _expected() -> str:
    return (os.environ.get("CK_FUSION_TOKEN") or os.environ.get("FUSION_TOKEN") or "").strip()


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/v1/events":
        limit = _limit()
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
        expected = _expected()
        if expected:
            got = request.headers.get("x-admin-token") or ""
            if not got or not hmac.compare_digest(got, expected):
                return JSONResponse({"detail": "Bad admin token"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health():
    return {
        "status": "ghost-online",
        "version": "contract-stand-in",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/current_mood")
def current_mood():
    with _lock:
        body = dict(_mood)
    body["timestamp"] = datetime.now(timezone.utc).isoformat()
    return body


@app.post("/v1/events")
async def receive_event(request: Request):
    raw = await request.body()
    limit = _limit()
    if len(raw) > limit:
        return JSONResponse({"detail": f"request body too large (max {limit} bytes)"}, status_code=413)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"detail": "body must be JSON"}, status_code=400)
    event, err = normalize_event(data)
    if err or event is None:
        return JSONResponse({"detail": err or "body must be JSON"}, status_code=400)
    with _lock:
        duplicate = _log.add(event)
    return {"accepted": True, "duplicate": duplicate, "id": event["id"]}


@app.get("/v1/events")
def list_events():
    """Debug read for the local stand-in. Not part of the required contract."""
    with _lock:
        events = _log.list()
    return {"events": events, "count": len(events)}


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("CK_HOST", "127.0.0.1")
    port = int(os.environ.get("CK_PORT", "8090"))
    uvicorn.run(app, host=host, port=port)
