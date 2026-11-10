"""Shared rules for the fusion HTTP contract.

Avrone's in-process learning bridge (avrone/bridge.py, reviewed at
9bb0e750aa40947f7f78b6ef41dcfa9afe6ae202) stores posts as::

    {id, run_id, agent, role, kind, text, refs, ts, origin_node}

with kind in finding|question|answer|correction|conclusion, text clipped to
4000 characters, and ids matching ``^[A-Za-z0-9_.:-]{1,80}$``. That object
has no HTTP write route and no fleet-wide read. ``GET /api/runs/{run_id}/bridge``
is per run and read-only.

CycleKernal's CognitiveBridge (src/bridge/main.py, reviewed at
cbbcce9b6c3864e8710a5607033e351364dab605) exposes ``GET /v1/current_mood``,
``GET /health``, and ``WS /ws/hud``. It has no event ingest. The master tree's
``POST /v1/feedback`` only logs the body.

``CONTRACT.md`` is the route-level spec. This module is the piece both the
relay and the local stand-ins import so the clip, id, and kind rules cannot
drift from that document.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections import OrderedDict
from typing import Any

BLACKBOARD_PATH = "/api/blackboard"
STATUS_PATH = "/api/status"
EVENTS_PATH = "/v1/events"
MOOD_PATH = "/v1/current_mood"
HEALTH_PATH = "/health"

# avrone/bridge.py KINDS and MAX_TEXT.
BLACKBOARD_KINDS = ("finding", "question", "answer", "correction", "conclusion")
# Kinds a producer may POST to CycleKernal. Mood and health are relay-authored
# mirrors; evidence is reserved for greatscale, which this repo does not produce.
EVENT_KINDS = BLACKBOARD_KINDS + ("mood", "evidence", "health")
EVENT_SOURCES = ("avrone", "cyclekernal", "greatscale")

MAX_TEXT_CHARS = 4000
MAX_BODY_BYTES = 4096
MAX_POSTS = 500
MAX_REFS = 20
MAX_AGENT = 80
MAX_ROLE = 60
MAX_ORIGIN = 80
MAX_RUN = 80

ORIGIN_NODE = "fusion-bridge"
RELAY_ROLE = "cyclekernal-relay"
RELAY_RUN = "fusion-relay"

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def clip_text(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """Avrone ``_clip``: strip, then keep ``limit`` characters, marking a cut with ``...``."""
    t = str("" if text is None else text).strip()
    if limit < 1:
        limit = 1
    if len(t) <= limit:
        return t
    if limit <= 3:
        return t[:limit]
    return t[: limit - 3] + "..."


def round_metric(value: Any) -> float | None:
    """Two-decimal bucket so a jittery heartbeat does not repost every poll."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, 2)


def project_mood(body: dict) -> dict:
    """Stable mood view. Timestamp, metrics, and explanation are intentionally left out.

    CycleKernal's synthetic heartbeat rewrites those on every tick. The label,
    the rounded scores, the genome, and the trait list are the mirror.
    """
    promote = body.get("promote") if isinstance(body.get("promote"), dict) else {}
    traits = body.get("traits")
    return {
        "mood": body.get("mood"),
        "mood_dot": round_metric(body.get("mood_dot")),
        "ns_fitness": round_metric(body.get("ns_fitness")),
        "genome_id": str(body.get("genome_id") or "")[:MAX_ORIGIN],
        "traits": list(traits) if isinstance(traits, list) else [],
        "action": promote.get("action"),
    }


def project_health(body: dict) -> dict:
    """Health view that ignores ``timestamp`` so a fresh clock does not repost."""
    return {
        "status": body.get("status"),
        "version": body.get("version"),
        "mood": body.get("mood"),
    }


def fingerprint(projection: dict) -> str:
    raw = json.dumps(projection, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _finite_ts(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return time.time()
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return time.time()
    return number


def normalize_refs(refs: Any) -> list[str]:
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        return []
    out: list[str] = []
    for item in refs:
        ref = str(item).strip()
        if valid_id(ref) and ref not in out:
            out.append(ref)
        if len(out) >= MAX_REFS:
            break
    return out


def normalize_post(data: Any, *, max_text: int = MAX_TEXT_CHARS) -> tuple[dict | None, str | None]:
    """Validate a blackboard write. Returns ``(post, None)`` or ``(None, detail)``."""
    if not isinstance(data, dict):
        return None, "body must be a JSON object"
    kind = str(data.get("kind") or "finding").strip().lower()
    if kind not in BLACKBOARD_KINDS:
        return None, "kind must be one of " + ", ".join(BLACKBOARD_KINDS)
    text = clip_text(data.get("text"), max_text)
    if not text:
        return None, "text is required"
    if data.get("id") not in (None, ""):
        pid = str(data["id"])
        if not valid_id(pid):
            return None, "bad post id"
    else:
        pid = uuid.uuid4().hex[:12]
    agent = str(data.get("agent") or "agent")[:MAX_AGENT]
    post = {
        "id": pid,
        "run_id": str(data.get("run_id") or "run")[:MAX_RUN],
        "agent": agent,
        "role": str(data.get("role") or "")[:MAX_ROLE],
        "kind": kind,
        "text": text,
        "refs": normalize_refs(data.get("refs")),
        "ts": _finite_ts(data.get("ts")),
        "origin_node": str(data.get("origin_node") or "local")[:MAX_ORIGIN],
    }
    return post, None


def normalize_event(data: Any) -> tuple[dict | None, str | None]:
    """Validate a ``POST /v1/events`` body."""
    if not isinstance(data, dict):
        return None, "body must be a JSON object"
    for field in ("id", "source", "kind", "run_id"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            return None, f"{field} is required"
    if not valid_id(data["id"]):
        return None, "bad event id"
    if data["source"] not in EVENT_SOURCES:
        return None, "source must be one of " + ", ".join(EVENT_SOURCES)
    if data["kind"] not in EVENT_KINDS:
        return None, "kind must be one of " + ", ".join(EVENT_KINDS)
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"
    event = {
        "id": data["id"],
        "source": data["source"],
        "kind": data["kind"],
        "run_id": data["run_id"][:MAX_RUN],
        "payload": payload,
        "ts": _finite_ts(data.get("ts")),
    }
    return event, None


def dumps_utf8(value: dict) -> bytes:
    """Compact UTF-8 JSON. Characters stay as UTF-8, not ``\\u`` escapes.

    The 4096-byte cap is the length of these bytes. A 4000-character text is
    not a safe clip: the wrapper around it, and any multibyte character, can
    push the body over the cap before a peer ever looks at ``text``.
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _text_at(obj: dict, where: str) -> str | None:
    if where == "text":
        value = obj.get("text")
        return value if isinstance(value, str) else None
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    return None


def _with_text(obj: dict, where: str, text: str) -> dict:
    out = dict(obj)
    if where == "text":
        out["text"] = text
        return out
    payload = dict(out.get("payload") or {})
    payload["text"] = text
    out["payload"] = payload
    return out


def _longest_text(obj: dict, where: str, original: str, max_bytes: int) -> tuple[dict, bytes] | None:
    """Longest ``clip_text`` of ``original`` whose serialized object is <= ``max_bytes``.

    The search is on the UTF-8 length of the whole JSON document, so a
    multibyte character costs its real encoded size and JSON escapes count too.
    """
    def trial(limit: int) -> tuple[dict, bytes]:
        if limit >= len(original):
            text = original
        elif limit <= 0:
            text = ""
        else:
            text = clip_text(original, limit)
        updated = _with_text(obj, where, text)
        return updated, dumps_utf8(updated)

    try:
        full_obj, full_raw = trial(len(original))
    except (TypeError, ValueError):
        return None
    if len(full_raw) <= max_bytes:
        return full_obj, full_raw
    lo, hi = 0, len(original) - 1
    best: tuple[dict, bytes] | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            updated, raw = trial(mid)
        except (TypeError, ValueError):
            return best
        if len(raw) <= max_bytes:
            best = (updated, raw)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def encode_bounded(body: dict, max_bytes: int) -> bytes | None:
    """UTF-8 JSON of ``body`` that is at most ``max_bytes``.

    Shrinks ``text`` and ``payload.text`` to the longest clip that still fits
    the wrapper. Returns None when even a stub body cannot fit. Callers treat
    that as a permanent reject so one oversized event cannot block the loop.
    """
    try:
        raw = dumps_utf8(body)
        obj = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if len(raw) <= max_bytes:
        return raw
    for _ in range(4):
        if len(raw) <= max_bytes:
            return raw
        progressed = False
        for where in ("payload", "text"):
            original = _text_at(obj, where)
            if not original:
                continue
            fitted = _longest_text(obj, where, original, max_bytes)
            progressed = True
            if fitted is None:
                # This field cannot absorb the overflow. Blank it and fit the other.
                obj = _with_text(obj, where, "")
                raw = dumps_utf8(obj)
                continue
            obj, raw = fitted
            if len(raw) <= max_bytes:
                return raw
        if not progressed:
            break
    if isinstance(obj.get("payload"), dict):
        obj["payload"] = {"truncated": True}
        try:
            raw = dumps_utf8(obj)
        except (TypeError, ValueError):
            return None
        if len(raw) <= max_bytes:
            return raw
        original = _text_at(obj, "text")
        if original is not None:
            fitted = _longest_text(obj, "text", original, max_bytes)
            if fitted is not None and len(fitted[1]) <= max_bytes:
                return fitted[1]
    return None if len(raw) > max_bytes else raw


def is_mirrored_agent(value: Any) -> bool:
    """True for the relay's own ``cyclekernal:<kind>`` agent, whatever the board kept."""
    return str(value or "").strip().lower().startswith("cyclekernal:")


def _text_is_mirror(text: Any) -> bool:
    """A board that drops agent/role/origin still echoes the mirror JSON we wrote."""
    if not isinstance(text, str) or not text.startswith("{") or "cyclekernal:" not in text.lower():
        return False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    return (
        is_mirrored_agent(parsed.get("agent"))
        or parsed.get("origin_node") == ORIGIN_NODE
        or str(parsed.get("role") or "").strip() == RELAY_ROLE
    )


def is_echo(post: dict) -> bool:
    """Posts this relay wrote. Forwarding them back to CycleKernal loops health.

    Live boards do not always keep ``origin_node`` and ``role``. A mirrored
    entry is still recognizable when its agent is ``cyclekernal:*``, its id
    is one this relay minted (``ck-health-``, ``ck-mood-``), or the text is
    the mirror document with that agent embedded.
    """
    if not isinstance(post, dict):
        return False
    if post.get("origin_node") == ORIGIN_NODE:
        return True
    if str(post.get("role") or "").strip() == RELAY_ROLE:
        return True
    if is_mirrored_agent(post.get("agent")):
        return True
    pid = str(post.get("id") or "")
    if pid.startswith(("ck-mood-", "ck-health-", "ck-evidence-")):
        return True
    return _text_is_mirror(post.get("text"))


class Board:
    """In-memory ring with id dedup and tombstones for evicted posts.

    Avrone's ``Bridge.merge_remote`` will not resurrect an id the ring has
    dropped. The fusion route should do the same. Tombstones are capped so a
    long-lived stand-in cannot grow without bound.
    """

    def __init__(self, limit: int = MAX_POSTS):
        self.limit = limit
        self.posts: OrderedDict[str, dict] = OrderedDict()
        self.evicted: set[str] = set()

    def add(self, post: dict) -> tuple[dict, bool]:
        """Return ``(stored_post, duplicate)``. A duplicate is not rewritten."""
        pid = post["id"]
        current = self.posts.get(pid)
        if current is not None:
            return current, True
        if pid in self.evicted:
            return post, True
        self.posts[pid] = post
        while len(self.posts) > self.limit:
            old, _ = self.posts.popitem(last=False)
            self.evicted.add(old)
        if len(self.evicted) > self.limit * 10:
            self.evicted = set(list(self.evicted)[-(self.limit * 10):])
        return post, False

    def export(self, since: str | None = None) -> list[dict]:
        posts = [dict(p) for p in self.posts.values()]
        if since:
            ids = [p["id"] for p in posts]
            if since in ids:
                posts = posts[ids.index(since) + 1:]
        return posts


class EventLog:
    """``/v1/events`` log deduped on ``(source, id)``.

    The same id from two producers is two events. A repeated pair keeps the
    first body and does not replace it.
    """

    def __init__(self) -> None:
        self.events: OrderedDict[tuple[str, str], dict] = OrderedDict()

    def add(self, event: dict) -> bool:
        """Return True when this ``(source, id)`` was already stored."""
        key = (str(event.get("source") or ""), str(event["id"]))
        if key in self.events:
            return True
        self.events[key] = event
        return False

    def bare_id(self, event_id: str) -> dict | None:
        """First stored event with this id, any source. For the legacy bare-id bug."""
        for event in self.events.values():
            if event.get("id") == event_id:
                return event
        return None

    def list(self) -> list[dict]:
        return [dict(event) for event in self.events.values()]


def posts_from_blackboard(body: Any) -> list | None:
    """Accept the contract object. Also accept a bare list so a partial server still drains.

    Servers MUST send ``{"posts": [...], "count": N}``. None means the body is not a board.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("posts"), list):
        return body["posts"]
    return None
