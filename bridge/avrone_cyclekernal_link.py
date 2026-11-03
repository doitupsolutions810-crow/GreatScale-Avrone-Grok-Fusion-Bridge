"""Credential-gated AVRONE -> CycleKernal event relay for proposed API contracts.

This is an opt-in *transport*, not an execution authority or a provenance verifier.
It has no arbitrary URL/command input, follows no redirects and treats uncertain
acknowledgements as unresolved. Downstream idempotency is required for retries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

AVRONE_DEFAULT = "http://127.0.0.1:8710"
CK_DEFAULT = "http://127.0.0.1:8090"
KINDS = frozenset({"finding", "question", "answer", "correction", "conclusion", "mood", "evidence", "health"})
EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
MAX_BODY_BYTES = 4096
MAX_REPLY_BYTES = 1_048_576
MAX_POSTS = 1000
TIMEOUT = 3


class BridgeConfigError(ValueError):
    """Configuration cannot safely identify a peer or credential."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(name: str, default: str) -> str:
    value = os.getenv(name) or default
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise BridgeConfigError(f"{name}: invalid port") from exc
    host = parsed.hostname
    if (not host or not port or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or parsed.scheme not in ("http", "https")):
        raise BridgeConfigError(f"{name}: expected a bare HTTP(S) origin")
    if parsed.scheme == "http" and host not in ("127.0.0.1", "localhost", "::1"):
        raise BridgeConfigError(f"{name}: remote origin requires TLS")
    return value.rstrip("/")


def _json_bytes(obj: Any) -> bytes:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise BridgeConfigError("invalid JSON event payload") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for k, v in pairs:
        if k in obj:
            raise ValueError("duplicate JSON key")
        obj[k] = v
    return obj


def _parse_json(content: bytes) -> dict[str, Any]:
    def fail_constant(_: str) -> None:
        raise ValueError("nonfinite JSON")
    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON float")
        return result
    reply = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=fail_constant, parse_float=finite_float)
    if not isinstance(reply, dict):
        raise ValueError("expected JSON object")
    return reply


def _exchange(base: str, path: str, token: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not token:
        raise BridgeConfigError("peer credential required")
    body = _json_bytes(payload) if payload is not None else None
    if body is not None and len(body) > MAX_BODY_BYTES:
        raise BridgeConfigError("event exceeds 4096 byte API contract")
    req = Request(base + path, data=body, method="POST" if body is not None else "GET",
                  headers={"X-Admin-Token": token, "Accept": "application/json",
                           **({"Content-Type": "application/json"} if body is not None else {})})
    opener = build_opener(NoRedirect())
    with opener.open(req, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise ValueError("peer rejected event")
        data = response.read(MAX_REPLY_BYTES + 1)
        if len(data) > MAX_REPLY_BYTES:
            raise ValueError("peer response oversized")
        return _parse_json(data)


@dataclass(frozen=True)
class FusionEvent:
    source: str
    kind: str
    run_id: str
    payload: dict[str, Any]
    id: str = ""
    ts: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.source not in ("avrone", "cyclekernal", "greatscale") or self.kind not in KINDS:
            raise ValueError("source/kind outside the fusion event contract")
        if not isinstance(self.run_id, str) or not 0 < len(self.run_id) <= 80:
            raise ValueError("run_id must be a bounded string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)) or not math.isfinite(self.ts) or self.ts <= 0:
            raise ValueError("event timestamp must be positive and finite")
        if not self.id:
            canonical = _json_bytes([self.source, self.kind, self.run_id, self.payload])
            object.__setattr__(self, "id", hashlib.sha256(canonical).hexdigest()[:32])
        if not EVENT_ID.fullmatch(self.id):
            raise ValueError("id must be a bounded ASCII event key")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "kind": self.kind,
                "run_id": self.run_id, "payload": self.payload, "ts": self.ts}

    def to_json(self) -> str:
        return _json_bytes(self.to_dict()).decode("utf-8")


def post_to_cyclekernal(event: FusionEvent) -> bool:
    """Only a confirmed, matching accepted response counts as acknowledgement."""
    try:
        token = os.getenv("CK_FUSION_TOKEN", "")
        if not token or event.source not in ("avrone", "greatscale"):
            return False
        reply = _exchange(_origin("CK_BASE", CK_DEFAULT), "/v1/events", token, payload=event.to_dict())
        return reply.get("accepted") is True and reply.get("id") == event.id
    except (BridgeConfigError, ValueError, OSError, UnicodeError, RecursionError):
        return False


def post_to_avrone_bridge(event: FusionEvent) -> bool:
    """Explicit operator action for feedback; relay_loop never mirrors automatically."""
    try:
        token = os.getenv("AVRONE_ADMIN_TOKEN", "")
        if not token or event.source != "cyclekernal":
            return False
        text = event.to_json()
        if len(text) > 3000:
            return False
        body = {"id": event.id, "run_id": event.run_id,
                "agent": "cyclekernal", "role": "cyclekernal-relay",
                "kind": "conclusion" if event.kind in ("mood", "health", "evidence") else event.kind,
                "text": text, "refs": []}
        reply = _exchange(_origin("AVRONE_BASE", AVRONE_DEFAULT), "/api/blackboard", token, payload=body)
        return reply.get("posted") is True and reply.get("id") == event.id
    except (BridgeConfigError, ValueError, OSError, UnicodeError, RecursionError):
        return False


def relay_loop(interval: float = 1.0, max_cycles: int | None = 1) -> dict[str, int]:
    """Forward bounded AVRONE blackboard snapshots; no implicit reverse loop.

    On timeout or malformed/negative ack, do NOT mark the event seen. Replays
    after a restart rely on the receiver's durable (source,id) idempotency.
    """
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 <= interval <= 60:
        raise ValueError("interval must be finite in [0,60]")
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles must be positive or None")
    stats = {"forwarded": 0, "seen": 0, "errors": 0, "duplicates": 0, "rejected": 0,
             "cycles": 0, "health_mirrored": 0}
    if not os.getenv("AVRONE_ADMIN_TOKEN") or not os.getenv("CK_FUSION_TOKEN"):
        stats["errors"] = 1  # explicit fail-closed, never probe unauthenticated peers
        return stats
    try:
        avrone_base = _origin("AVRONE_BASE", AVRONE_DEFAULT)
        _origin("CK_BASE", CK_DEFAULT)
    except BridgeConfigError:
        stats["errors"] = 1
        return stats
    acknowledged: set[tuple[str, str]] = set()  # bounded below; receiver owns durable dedup
    boot = None
    while max_cycles is None or stats["cycles"] < max_cycles:
        stats["cycles"] += 1
        try:
            result = _exchange(avrone_base, "/api/blackboard", os.environ["AVRONE_ADMIN_TOKEN"])
            posts = result.get("posts")
            if not isinstance(posts, list) or len(posts) > MAX_POSTS:
                raise ValueError("invalid or overlarge blackboard snapshot")
            new_boot = result.get("boot")
            if not isinstance(new_boot, str) or len(new_boot) > 128 or not new_boot:
                raise ValueError("missing peer boot identity")
            if boot is not None and boot != new_boot:
                acknowledged.clear()
            boot = new_boot
            for post in posts:
                if not isinstance(post, dict):
                    stats["rejected"] += 1
                    continue
                post_id = post.get("id")
                kind = post.get("kind")
                run_id = post.get("run_id")
                if not isinstance(post_id, str) or not EVENT_ID.fullmatch(post_id) or kind not in KINDS or not isinstance(run_id, str) or not 0 < len(run_id) <= 80:
                    stats["rejected"] += 1
                    continue
                key = (new_boot, post_id)
                if key in acknowledged:
                    stats["duplicates"] += 1
                    continue
                text = post.get("text", "")
                if not isinstance(text, str):
                    stats["rejected"] += 1
                    continue
                encoded = text.encode("utf-8", errors="replace")
                clipped = encoded[:1800].decode("utf-8", errors="ignore")
                payload = {"text": clipped, "truncated": len(encoded) > 1800,
                           "text_sha256": hashlib.sha256(encoded).hexdigest(),
                           "agent": str(post.get("agent") or "")[:80],
                           "role": str(post.get("role") or "")[:60]}
                try:
                    event = FusionEvent("avrone", kind, run_id, payload, id=post_id)
                except (ValueError, BridgeConfigError):
                    stats["rejected"] += 1
                    continue
                stats["seen"] += 1
                if post_to_cyclekernal(event):
                    acknowledged.add(key)
                    if len(acknowledged) > 5000:
                        acknowledged.clear()  # receiver handles idempotent replays
                    stats["forwarded"] += 1
                else:
                    stats["errors"] += 1
        except (BridgeConfigError, ValueError, OSError, UnicodeError, RecursionError):
            stats["errors"] += 1
        if max_cycles is None or stats["cycles"] < max_cycles:
            time.sleep(interval)
    return stats


if __name__ == "__main__":
    print(json.dumps(relay_loop(max_cycles=1), sort_keys=True))
