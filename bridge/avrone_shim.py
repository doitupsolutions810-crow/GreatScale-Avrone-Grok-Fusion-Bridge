"""Thin HTTP shim mirroring Avrone's blackboard shape.

Avrone's real blackboard lives inside its Docker stack. This shim
exposes the same interface on port 8710 (Avrone's real port) so the
relay loop can run end-to-end locally. When the real container comes
up, swap this store for a proxy call — nothing else changes.
"""
from __future__ import annotations

import json
import time
import uuid
from flask import Flask, request, Response

app = Flask(__name__)

BLACKBOARD: list[dict] = []


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
                    mitype="application/json", status=200)


@app.route("/health")
def health():
    return {"status": "ok", "entries": len(BLACKBOARD), "ts": time.time()}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8710, debug=False)
