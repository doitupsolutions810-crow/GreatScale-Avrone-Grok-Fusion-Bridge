"""Local stand-in for the Avrone half of ``CONTRACT.md``.

This is not Avrone. Avrone (port 8710) has no ``/api/blackboard`` yet. The
process below implements that route so the relay can be exercised on a laptop.
Swap ``AVRONE_BASE`` to a real Avrone once that repo ships the route.

Auth matches Avrone's admin header. When ``FUSION_TOKEN`` or
``AVRONE_ADMIN_TOKEN`` is unset, the stand-in allows the call (loopback demo).
When it is set, a missing or wrong ``X-Admin-Token`` is 401.
"""
from __future__ import annotations

import hmac
import os
import threading
import uuid

from flask import Flask, jsonify, request

try:
    from .contract import MAX_BODY_BYTES, STATUS_PATH, Board, normalize_post
except ImportError:  # python bridge/avrone_shim.py
    from contract import MAX_BODY_BYTES, STATUS_PATH, Board, normalize_post

app = Flask(__name__)
_lock = threading.Lock()
_board = Board()
# Changes when this process starts. The relay resends mirrors when it changes.
_BOOT = uuid.uuid4().hex[:12]


def _expected_token() -> str:
    return (os.environ.get("AVRONE_ADMIN_TOKEN") or os.environ.get("FUSION_TOKEN") or "").strip()


def _body_limit() -> int:
    raw = (os.environ.get("FUSION_MAX_BODY_BYTES") or "").strip()
    try:
        return int(raw) if raw else MAX_BODY_BYTES
    except ValueError:
        return MAX_BODY_BYTES


@app.get(STATUS_PATH)
def status():
    with _lock:
        count = len(_board.posts)
    return jsonify(status="ok", service="avrone-blackboard-stand-in", entries=count, boot=_BOOT)


@app.get("/api/blackboard")
def get_blackboard():
    if not _auth_ok():
        return jsonify(detail="Bad admin token"), 401
    since = request.args.get("since")
    with _lock:
        posts = _board.export(since)
    return jsonify(posts=posts, count=len(posts), boot=_BOOT)


@app.post("/api/blackboard")
def post_blackboard():
    limit = _body_limit()
    length = request.content_length
    if length is not None and length > limit:
        return jsonify(detail=f"request body too large (max {limit} bytes)"), 413
    raw = request.get_data(cache=True) or b""
    if len(raw) > limit:
        return jsonify(detail=f"request body too large (max {limit} bytes)"), 413
    if not _auth_ok():
        return jsonify(detail="Bad admin token"), 401
    data = request.get_json(silent=True)
    post, err = normalize_post(data)
    if err:
        detail = "body must be JSON" if data is None else err
        return jsonify(detail=detail), 400
    assert post is not None
    with _lock:
        stored, duplicate = _board.add(post)
    return jsonify(posted=True, duplicate=duplicate, id=stored["id"])


def _auth_ok() -> bool:
    expected = _expected_token()
    if not expected:
        return True
    got = request.headers.get("X-Admin-Token", "")
    return bool(got) and hmac.compare_digest(got, expected)


@app.get("/health")
def health():
    """Not an Avrone route. Present so a local probe can see the stand-in."""
    return jsonify(status="ok", service="avrone-blackboard-stand-in", boot=_BOOT)


if __name__ == "__main__":
    host = os.environ.get("AVRONE_HOST", "127.0.0.1")
    port = int(os.environ.get("AVRONE_PORT", "8710"))
    app.run(host=host, port=port, debug=False)
