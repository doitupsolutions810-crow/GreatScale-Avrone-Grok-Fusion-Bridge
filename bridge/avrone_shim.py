"""Thin HTTP shim mirroring Avrone's blackboard shape.

The legacy ``/blackboard`` and ``/health`` routes stay so the original
integration script keeps working. ``/api/blackboard`` and ``/api/status``
are the contract routes (4096-byte cap before auth, ``X-Admin-Token``,
optional ``AVRONE_ADMIN_TOKEN_PREVIOUS``).
"""
from __future__ import annotations

import hmac
import json
import os
import time
import uuid
from flask import Flask, jsonify, request, Response

try:
    from .contract import normalize_post
except ImportError:
    from contract import normalize_post

app = Flask(__name__)

BLACKBOARD: list[dict] = []
BOOT = uuid.uuid4().hex
STARTED = time.time()
RESTARTS = int(os.environ.get("AVRONE_RESTARTS", "0") or "0")


@app.route("/blackboard", methods=["GET"])
def get_blackboard():
    return Response(json.dumps(BLACKBOARD), mimetype="application/json")


@app.route("/blackboard", methods=["POST"])
def post_blackboard():
    data = request.get_json(force=True, silent=True) or {}
    entry = {
        "id": str(uuid.uuid4()),
        "agent": data.get("agent", "unknown"),
        "kind": data.get("kind", "finding"),
        "text": data.get("text", "")[:4000],
        "role": data.get("role", "agent"),
        "ts": time.time(),
    }
    BLACKBOARD.append(entry)
    # Keep only last 500 entries
    if len(BLACKBOARD) > 500:
        del BLACKBOARD[:-500]
    return Response(json.dumps({"posted": True, "id": entry["id"]}),
                    mimetype="application/json", status=200)


def _eq(got: str, expected: str) -> bool:
    if not expected or not isinstance(got, str):
        return False
    left, right = got.encode("utf-8"), expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _auth_ok() -> bool:
    expected = (os.environ.get("AVRONE_ADMIN_TOKEN") or os.environ.get("FUSION_TOKEN") or "").strip()
    previous = os.environ.get("AVRONE_ADMIN_TOKEN_PREVIOUS") or ""
    if not expected:
        return True
    got = request.headers.get("X-Admin-Token") or ""
    return _eq(got, expected) or _eq(got, previous)


def _health_body() -> dict:
    now = time.time()
    return {
        "status": "ok",
        "entries": len(BLACKBOARD),
        "ts": now,
        "started_at": STARTED,
        "uptime_s": round(now - STARTED, 3),
        "restarts": RESTARTS,
        "boot": BOOT,
    }


@app.route("/health")
def health():
    return _health_body()


@app.get("/api/status")
def api_status():
    return jsonify(**_health_body())


@app.get("/api/blackboard")
def api_get_blackboard():
    if not _auth_ok():
        return jsonify(detail="Bad admin token"), 401
    return jsonify(posts=list(BLACKBOARD), count=len(BLACKBOARD), boot=BOOT)


@app.post("/api/blackboard")
def api_post_blackboard():
    limit = 4096
    # Cap is checked before the token, matching Avrone's fusion route.
    if request.content_length is not None and request.content_length > limit:
        return jsonify(detail=f"request body too large (max {limit} bytes)"), 413
    raw = request.get_data(cache=True) or b""
    if len(raw) > limit:
        return jsonify(detail=f"request body too large (max {limit} bytes)"), 413
    if not _auth_ok():
        return jsonify(detail="Bad admin token"), 401
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify(detail="body must be JSON"), 400
    post, err = normalize_post(data)
    if err or post is None:
        return jsonify(detail=err or "body must be JSON"), 400
    for existing in BLACKBOARD:
        if existing.get("id") == post["id"]:
            return jsonify(posted=True, duplicate=True, id=post["id"])
    BLACKBOARD.append(post)
    if len(BLACKBOARD) > 500:
        del BLACKBOARD[:-500]
    return jsonify(posted=True, duplicate=False, id=post["id"])


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8710, debug=False)
