"""Relay Avrone blackboard posts to CycleKernal, and mirror mood and health back.

What is real today (verified, not assumed):

- Avrone listens on ``AVRONE_PORT`` default 8710. Public liveness is
  ``GET /api/status``. Run posts are readable at ``GET /api/runs/{run_id}/bridge``
  and are local-or-``X-Admin-Token``. There is no ``/blackboard`` and no
  ``/api/blackboard``.
- CycleKernal CognitiveBridge listens on 8090. It serves ``GET /v1/current_mood``,
  ``GET /health``, and ``WS /ws/hud``. It does not serve ``/events`` or
  ``/api/events``.

This module speaks the contract in ``CONTRACT.md``. Until Avrone adds
``/api/blackboard`` and CycleKernal adds ``POST /v1/events``, those hops fail
soft: one dead side never raises out of :func:`relay_loop`. Reads that already
exist (mood, health) are used as they are.

Delivery is at-least-once. A post id is remembered only after the peer accepts
it, or after a permanent 4xx that will never succeed for that same body.
Timeouts and connection failures back off. Identical mood and health
projections are not posted again. Restart dedup is the state file at
``FUSION_STATE_PATH`` plus the peer's id dedup.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contract import (
    BLACKBOARD_KINDS,
    ORIGIN_NODE,
    RELAY_ROLE,
    RELAY_RUN,
    clip_text,
    encode_bounded,
    fingerprint,
    is_echo,
    posts_from_blackboard,
    project_health,
    project_mood,
    valid_id,
)
from .settings import RelayConfig, load_config

# Patched by tests that need to assert the sleep sequence without waiting.
_sleep = time.sleep

# A 400/413 will not start working on retry. A 404 might, once the other repo ships.
_PERMANENT = frozenset({"too_large", "rejected"})
_TRANSPORT = frozenset({"timeout", "transport"})


@dataclass
class HopResult:
    ok: bool
    status: int | None = None
    error: str | None = None
    body: Any = None


@dataclass
class FusionEvent:
    source: str  # avrone | cyclekernal | greatscale
    kind: str
    run_id: str
    payload: dict
    ts: float = 0.0
    id: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.time()
        if not self.id:
            blob = json.dumps(
                {
                    "source": self.source,
                    "kind": self.kind,
                    "run_id": self.run_id,
                    "payload": self.payload,
                },
                sort_keys=True,
                default=str,
            )
            self.id = "ev-" + fingerprint({"blob": blob})

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _status_error(code: int) -> str:
    if code in (401, 403):
        return "auth"
    if code == 413:
        return "too_large"
    if code in (404, 405):
        return "missing"
    if code >= 500:
        return "upstream"
    return "rejected"


def _request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    raw: bytes | None = None,
    timeout: float = 5.0,
    token: str = "",
) -> HopResult:
    headers = {"Accept": "application/json"}
    data = raw
    if data is None and body is not None:
        data = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if not payload:
                return HopResult(ok=True, status=resp.status, body=None)
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return HopResult(ok=False, status=resp.status, error="bad_json", body=payload[:120])
            return HopResult(ok=True, status=resp.status, body=parsed)
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        return HopResult(ok=False, status=exc.code, error=_status_error(exc.code))
    except (TimeoutError, socket.timeout):
        return HopResult(ok=False, error="timeout")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return HopResult(ok=False, error="timeout")
        return HopResult(ok=False, error="transport", body=str(reason)[:200])
    except OSError as exc:
        return HopResult(ok=False, error="transport", body=str(exc)[:200])
    except Exception as exc:
        # A peer that accepts and then drops the socket raises BadStatusLine,
        # which is not an OSError. That is still a dead hop, not a crash.
        return HopResult(ok=False, error="transport", body=type(exc).__name__)


def _account(stats: dict, hop: HopResult) -> None:
    stats["errors"] += 1
    err = hop.error or "transport"
    if err == "timeout":
        stats["timeouts"] += 1
    elif err == "auth":
        stats["auth_failures"] += 1
    elif err in ("too_large", "rejected"):
        stats["rejected"] += 1
    elif err == "bad_json":
        stats["malformed"] += 1
    elif err == "missing":
        stats["missing_routes"] += 1
    elif err == "upstream":
        stats["upstream_errors"] += 1
    else:
        stats["transport_errors"] += 1


def _load_state(path: str) -> tuple[set[str], str, int]:
    if not path or not Path(path).is_file():
        return set(), "", 0
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("seen"), list):
            raise ValueError("state file is not a seen-set")
        seen = {str(item) for item in data["seen"] if str(item)}
        cursor = data.get("cursor") if isinstance(data.get("cursor"), str) else ""
        return seen, cursor, 0
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return set(), "", 1


def _save_state(path: str, seen: set[str], cursor: str) -> bool:
    if not path:
        return True
    try:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "seen": sorted(seen), "cursor": cursor})
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(dest)
        return True
    except OSError:
        return False


def _remember(stats: dict, path: str, seen: set[str], cursor: str) -> None:
    if not _save_state(path, seen, cursor):
        stats["state_errors"] += 1


def _post_identity(post: dict) -> str:
    eid = post.get("id")
    if valid_id(eid):
        return str(eid)
    projection = {
        "run_id": str(post.get("run_id") or ""),
        "agent": str(post.get("agent") or ""),
        "kind": str(post.get("kind") or ""),
        "text": clip_text(post.get("text")),
    }
    return "h-" + fingerprint(projection)


def _event_from_post(post: dict, pid: str, cfg: RelayConfig) -> FusionEvent:
    kind = str(post.get("kind") or "finding").strip().lower()
    if kind not in BLACKBOARD_KINDS:
        kind = "finding"
    payload = {
        "id": pid,
        "agent": str(post.get("agent") or "agent")[:80],
        "role": str(post.get("role") or "")[:60],
        "kind": kind,
        "text": clip_text(post.get("text"), cfg.max_text_chars),
        "refs": post.get("refs") if isinstance(post.get("refs"), list) else [],
        "origin_node": str(post.get("origin_node") or "")[:80],
    }
    ts = post.get("ts")
    when = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0
    return FusionEvent(
        source="avrone",
        kind=kind,
        run_id=str(post.get("run_id") or "unknown")[:80],
        payload=payload,
        ts=when,
        id=pid,
    )


def _send_bytes(url: str, raw: bytes | None, cfg: RelayConfig, token: str) -> HopResult:
    if raw is None:
        return HopResult(ok=False, error="too_large")
    return _request("POST", url, raw=raw, timeout=cfg.timeout, token=token)


def deliver_to_cyclekernal(event: FusionEvent, cfg: RelayConfig | None = None) -> HopResult:
    cfg = cfg or load_config()
    body = {
        "id": event.id,
        "source": event.source,
        "kind": event.kind,
        "run_id": event.run_id,
        "payload": event.payload,
        "ts": event.ts,
    }
    return _send_bytes(cfg.ck_events_url, encode_bounded(body, cfg.max_body_bytes), cfg, cfg.ck_token)


def deliver_to_avrone(event: FusionEvent, cfg: RelayConfig | None = None) -> HopResult:
    cfg = cfg or load_config()
    # Avrone's board only accepts its five kinds. Mood is a conclusion; health
    # and evidence land as findings. The structured projection is the text.
    kind = "conclusion" if event.kind == "mood" else "finding"
    body = {
        "id": event.id,
        "agent": f"cyclekernal:{event.kind}"[:80],
        "role": RELAY_ROLE,
        "kind": kind,
        "text": json.dumps(event.payload, separators=(",", ":"), default=str),
        "run_id": event.run_id or RELAY_RUN,
        "refs": [],
        "origin_node": ORIGIN_NODE,
    }
    return _send_bytes(
        cfg.avrone_blackboard_url,
        encode_bounded(body, cfg.max_body_bytes),
        cfg,
        cfg.avrone_token,
    )


def post_to_cyclekernal(event: FusionEvent, timeout: float | None = None, config: RelayConfig | None = None) -> bool:
    cfg = config or load_config()
    if timeout is not None and timeout > 0:
        cfg = _replace_timeout(cfg, timeout)
    return deliver_to_cyclekernal(event, cfg).ok


def post_to_avrone_bridge(event: FusionEvent, timeout: float | None = None, config: RelayConfig | None = None) -> bool:
    cfg = config or load_config()
    if timeout is not None and timeout > 0:
        cfg = _replace_timeout(cfg, timeout)
    return deliver_to_avrone(event, cfg).ok


def _replace_timeout(cfg: RelayConfig, timeout: float) -> RelayConfig:
    data = asdict(cfg)
    data["timeout"] = timeout
    return RelayConfig(**data)


def _empty_stats(state_errors: int) -> dict:
    return {
        "forwarded": 0,
        "seen": 0,
        "health_mirrored": 0,
        "mood_mirrored": 0,
        "errors": 0,
        "cycles": 0,
        "timeouts": 0,
        "auth_failures": 0,
        "rejected": 0,
        "malformed": 0,
        "missing_routes": 0,
        "upstream_errors": 0,
        "transport_errors": 0,
        "backoffs": 0,
        "last_backoff_s": 0.0,
        "duplicates": 0,
        "state_errors": state_errors,
    }


def _accepted(stats: dict, hop: HopResult, counter: str) -> None:
    if isinstance(hop.body, dict) and hop.body.get("duplicate"):
        stats["duplicates"] += 1
    else:
        stats[counter] += 1


def _pull_and_forward(cfg: RelayConfig, stats: dict, seen: set[str], cursor: str) -> tuple[bool, str]:
    """Forward new board posts. Returns ``(transport_failure, cursor)``."""
    url = cfg.avrone_blackboard_url
    if cursor:
        url = url + "?" + urllib.parse.urlencode({"since": cursor})
    hop = _request("GET", url, timeout=cfg.timeout, token=cfg.avrone_token)
    if not hop.ok:
        _account(stats, hop)
        return hop.error in _TRANSPORT, cursor
    posts = posts_from_blackboard(hop.body)
    if posts is None:
        stats["errors"] += 1
        stats["malformed"] += 1
        return False, cursor

    new_cursor = cursor
    for post in posts:
        if not isinstance(post, dict):
            stats["errors"] += 1
            stats["malformed"] += 1
            continue
        pid = _post_identity(post)
        if is_echo(post) or pid in seen:
            seen.add(pid)
            new_cursor = pid
            continue
        text = clip_text(post.get("text"), cfg.max_text_chars)
        if not text:
            stats["errors"] += 1
            stats["malformed"] += 1
            seen.add(pid)
            new_cursor = pid
            _remember(stats, cfg.state_path, seen, new_cursor)
            continue
        result = deliver_to_cyclekernal(_event_from_post(post, pid, cfg), cfg)
        if result.ok:
            _accepted(stats, result, "forwarded")
            seen.add(pid)
            new_cursor = pid
            _remember(stats, cfg.state_path, seen, new_cursor)
            continue
        _account(stats, result)
        if result.error in _PERMANENT:
            seen.add(pid)
            new_cursor = pid
            _remember(stats, cfg.state_path, seen, new_cursor)
            continue
        # Auth, 404, 5xx, timeout, bad JSON: leave the id unseen and stop
        # this batch so a later post is not acked ahead of a failed one.
        _remember(stats, cfg.state_path, seen, new_cursor)
        return result.error in _TRANSPORT, new_cursor
    _remember(stats, cfg.state_path, seen, new_cursor)
    return False, new_cursor


def _mirror(
    cfg: RelayConfig,
    stats: dict,
    seen: set[str],
    cursor: str,
    *,
    url: str,
    kind: str,
    project,
    counter: str,
) -> bool:
    hop = _request("GET", url, timeout=cfg.timeout, token="")
    if not hop.ok:
        _account(stats, hop)
        return hop.error in _TRANSPORT
    if not isinstance(hop.body, dict):
        stats["errors"] += 1
        stats["malformed"] += 1
        return False
    projection = project(hop.body)
    eid = f"ck-{kind}-{fingerprint(projection)}"
    if not valid_id(eid) or eid in seen:
        return False
    event = FusionEvent(
        source="cyclekernal",
        kind=kind,
        run_id=RELAY_RUN,
        payload=projection,
        id=eid,
    )
    result = deliver_to_avrone(event, cfg)
    if result.ok:
        _accepted(stats, result, counter)
        seen.add(eid)
        _remember(stats, cfg.state_path, seen, cursor)
        return False
    _account(stats, result)
    if result.error in _PERMANENT:
        seen.add(eid)
        _remember(stats, cfg.state_path, seen, cursor)
        return False
    return result.error in _TRANSPORT


def relay_loop(
    interval: float | None = None,
    max_cycles: int = 100,
    config: RelayConfig | None = None,
) -> dict:
    """Poll, forward, and mirror. ``max_cycles <= 0`` runs until interrupted.

    ``interval`` overrides ``FUSION_INTERVAL`` for the success path. Transport
    failures sleep the backoff instead, starting at ``FUSION_BACKOFF_BASE``
    and doubling up to ``FUSION_BACKOFF_MAX``, then reset after a clean cycle.
    """
    cfg = config or load_config()
    if interval is None:
        interval = cfg.interval
    seen, cursor, state_errors = _load_state(cfg.state_path)
    stats = _empty_stats(state_errors)
    backoff = cfg.backoff_base
    cycle = 0
    while max_cycles <= 0 or cycle < max_cycles:
        transport = False
        failed, cursor = _pull_and_forward(cfg, stats, seen, cursor)
        transport = transport or failed
        if _mirror(
            cfg,
            stats,
            seen,
            cursor,
            url=cfg.ck_mood_url,
            kind="mood",
            project=project_mood,
            counter="mood_mirrored",
        ):
            transport = True
        if _mirror(
            cfg,
            stats,
            seen,
            cursor,
            url=cfg.ck_health_url,
            kind="health",
            project=project_health,
            counter="health_mirrored",
        ):
            transport = True
        _remember(stats, cfg.state_path, seen, cursor)
        if transport:
            delay = min(backoff, cfg.backoff_max)
            stats["backoffs"] += 1
            stats["last_backoff_s"] = delay
            backoff = min(backoff * 2, cfg.backoff_max)
            if delay > 0:
                _sleep(delay)
        else:
            backoff = cfg.backoff_base
            if interval > 0:
                _sleep(interval)
        cycle += 1
    stats["seen"] = len(seen)
    stats["cycles"] = cycle
    return stats


def main() -> int:
    # Discovery is advisory. A probe miss keeps the configured URL so the loop
    # records the dead hop instead of pretending the base was never set.
    from .endpoint_resolver import resolve_avrone_base, resolve_ck_base

    av = resolve_avrone_base()
    ck = resolve_ck_base()
    if av:
        os.environ["AVRONE_BASE"] = av
    if ck:
        os.environ["CK_BASE"] = ck
    raw_cycles = os.environ.get("FUSION_CYCLES", "3").strip() or "3"
    try:
        cycles = int(raw_cycles)
    except ValueError:
        cycles = 3
    stats = relay_loop(max_cycles=cycles)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
