"""Relay Avrone blackboard posts to CycleKernal, and mirror mood and health back.

What is real today (verified, not assumed):

- Avrone listens on ``AVRONE_PORT`` default 8710. Public liveness is
  ``GET /api/status``. Run posts are readable at ``GET /api/runs/{run_id}/bridge``
  and are local-or-``X-Admin-Token``. There is no ``/blackboard`` and no
  ``/api/blackboard``.
- CycleKernal CognitiveBridge listens on 8090 and serves ``GET /v1/current_mood``,
  ``GET /health``, and ``WS /ws/hud``. ``POST /v1/events`` is implemented on
  CycleKernal PR #14 (``X-Admin-Token``, 4096-byte 413 before auth,
  ``duplicate: true``), not on ``main`` yet.

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

import contextvars
import contextvars
import json
import math
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
from .strict_fields import FieldError, choose_payload, parse_retry_after, validate_known_fields

# Patched by tests that need to assert the sleep sequence without waiting.
_sleep = time.sleep

# A 400/413 will not start working on retry. A 404 might, once the other repo ships.
_PERMANENT = frozenset({"too_large", "rejected"})
_TRANSPORT = frozenset({"timeout", "transport"})
# 429 and 500 are answered failures. Retry them with a bounded sleep, not a spin.
_BACKOFF = frozenset({"timeout", "transport", "throttle", "upstream"})
# Opens the per-host breaker. A 500 responded; it is not a black hole.
_BREAKER_ERRORS = frozenset({"timeout", "transport"})

# One relay_loop call shares these across its cycles. A new call starts clean.
_cycle_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("fusion_deadline", default=None)
_cycle_breaker: contextvars.ContextVar["_HostBreaker | None"] = contextvars.ContextVar("fusion_breaker", default=None)
_cycle_stats: contextvars.ContextVar[dict | None] = contextvars.ContextVar("fusion_stats", default=None)


def _host_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class _HostBreaker:
    """Skip a host that accepts connections and then does not answer.

    The first timeout or refused connection opens the circuit. Further calls
    to that host fail immediately until ``cooldown`` elapses, then one probe
    is allowed. Threshold 0 disables the breaker.
    """

    def __init__(self, threshold: int, cooldown: float):
        self.threshold = threshold
        self.cooldown = cooldown
        self._hosts: dict[str, dict] = {}

    def blocked(self, url: str) -> bool:
        if self.threshold <= 0:
            return False
        state = self._hosts.get(_host_of(url))
        if not state or not state["open"]:
            return False
        if time.monotonic() >= state["until"]:
            if state["probing"]:
                return True
            state["probing"] = True
            return False
        return True

    def observe(self, url: str, error: str | None) -> bool:
        """Record the outcome. Return True when this call opened the circuit."""
        if self.threshold <= 0:
            return False
        host = _host_of(url)
        if error not in _BREAKER_ERRORS:
            if error is None:
                self._hosts.pop(host, None)
            return False
        state = self._hosts.setdefault(
            host, {"failures": 0, "open": False, "until": 0.0, "probing": False}
        )
        state["failures"] += 1
        state["probing"] = False
        if state["open"] or state["failures"] < self.threshold:
            if state["open"]:
                state["until"] = time.monotonic() + self.cooldown
            return False
        state["open"] = True
        state["until"] = time.monotonic() + max(0.0, self.cooldown)
        return True


def _idempotency_key(value: str) -> str:
    key = (value or "").strip()
    if not key or any(char in key for char in "\r\n\x00"):
        return ""
    return key[:200]


@dataclass
class HopResult:
    ok: bool
    status: int | None = None
    error: str | None = None
    body: Any = None
    retry_after: float | None = None


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
    if code == 429:
        return "throttle"
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
    idempotency_key: str = "",
) -> HopResult:
    breaker = _cycle_breaker.get()
    if breaker is not None and breaker.blocked(url):
        return HopResult(ok=False, error="circuit")
    deadline_at = _cycle_deadline.get()
    capped_by_deadline = False
    if deadline_at is not None:
        remaining = deadline_at - time.monotonic()
        # Don't start a call that cannot finish inside this cycle.
        if remaining <= 0.05:
            return HopResult(ok=False, error="deadline")
        if remaining < timeout:
            capped_by_deadline = True
            timeout = remaining
    headers = {"Accept": "application/json"}
    data = raw
    if data is None and body is not None:
        data = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    key = _idempotency_key(idempotency_key)
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if not payload:
                result = HopResult(ok=True, status=resp.status, body=None)
            else:
                try:
                    parsed = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    result = HopResult(ok=False, status=resp.status, error="bad_json", body=payload[:120])
                else:
                    result = HopResult(ok=True, status=resp.status, body=parsed)
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        retry_after = None
        if exc.code == 429 and exc.headers is not None:
            retry_after = parse_retry_after(exc.headers.get("Retry-After"))
        result = HopResult(
            ok=False,
            status=exc.code,
            error=_status_error(exc.code),
            retry_after=retry_after,
        )
    except (TimeoutError, socket.timeout):
        result = HopResult(ok=False, error="timeout")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            result = HopResult(ok=False, error="timeout")
        else:
            result = HopResult(ok=False, error="transport", body=str(reason)[:200])
    except OSError as exc:
        result = HopResult(ok=False, error="transport", body=str(exc)[:200])
    except Exception as exc:
        # A peer that accepts and then drops the socket raises BadStatusLine,
        # which is not an OSError. That is still a dead hop, not a crash.
        result = HopResult(ok=False, error="transport", body=type(exc).__name__)
    stats = _cycle_stats.get()
    if breaker is not None:
        opened = breaker.observe(url, None if result.ok else result.error)
        if opened and stats is not None:
            stats["circuit_opened"] += 1
    if capped_by_deadline and result.error == "timeout" and stats is not None:
        stats["deadline_stops"] += 1
    return result


def _account(stats: dict, hop: HopResult) -> None:
    err = hop.error or "transport"
    # A skipped call did not touch the peer. Don't count it as a hop failure.
    if err == "circuit":
        stats["circuit_skipped"] += 1
        return
    if err == "deadline":
        stats["deadline_stops"] += 1
        return
    stats["errors"] += 1
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
    elif err == "throttle":
        stats["throttled"] += 1
        if hop.retry_after is not None:
            stats["_sleep_override"] = hop.retry_after
    else:
        stats["transport_errors"] += 1


class _Track:
    """Delivered ids plus the boot id last seen for each peer.

    ``seen`` is what we will not send again. ``boots`` is how we notice a
    peer restarted and lost that data. An empty previous boot is the first
    observation and does not resend.
    """

    def __init__(self, seen: set[str], cursor: str, boots: dict[str, str]):
        self.seen = seen
        self.cursor = cursor
        self.boots = boots


def _load_state(path: str) -> tuple[_Track, int]:
    if not path or not Path(path).is_file():
        return _Track(set(), "", {}), 0
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("seen"), list):
            raise ValueError("state file is not a seen-set")
        seen = {str(item) for item in data["seen"] if str(item)}
        cursor = data.get("cursor") if isinstance(data.get("cursor"), str) else ""
        raw_boots = data.get("boots") if isinstance(data.get("boots"), dict) else {}
        boots = {str(key): str(value) for key, value in raw_boots.items() if str(value).strip()}
        return _Track(seen, cursor, boots), 0
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return _Track(set(), "", {}), 1


def _save_state(path: str, track: _Track) -> bool:
    if not path:
        return True
    try:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "version": 2,
            "seen": sorted(track.seen),
            "cursor": track.cursor,
            "boots": track.boots,
        })
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(dest)
        return True
    except OSError:
        return False


def _remember(stats: dict, path: str, track: _Track) -> None:
    if not _save_state(path, track):
        stats["state_errors"] += 1


def _boot_value(body: Any) -> str:
    """``boot`` on /health or a board read. String or integer epoch. Missing is empty."""
    if not isinstance(body, dict) or "boot" not in body:
        return ""
    value = body["boot"]
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return ""
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text or any(char in text for char in "\r\n\x00"):
        return ""
    return text[:80]


def _note_boot(track: _Track, side: str, boot: str) -> bool:
    """Remember ``boot``. Return True when a changed boot forgot delivered ids."""
    if side not in ("avrone", "cyclekernal") or not boot:
        return False
    previous = track.boots.get(side, "")
    if previous == boot:
        return False
    track.boots[side] = boot
    if not previous:
        return False
    if side == "cyclekernal":
        track.seen = {item for item in track.seen if item.startswith(("ck-mood-", "ck-health-", "ck-evidence-"))}
        track.cursor = ""
    else:
        track.seen = {item for item in track.seen if not item.startswith(("ck-mood-", "ck-health-", "ck-evidence-"))}
        track.cursor = ""
    return True


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
    winner, chosen = choose_payload(post)
    if winner == "payload":
        # Structured payload wins over text. text is not parsed on top of it.
        payload = chosen
    else:
        payload = {
            "id": pid,
            "agent": str(post.get("agent") or "agent")[:80],
            "role": str(post.get("role") or "")[:60],
            "kind": kind,
            "text": clip_text(post.get("text"), cfg.max_text_chars),
            "refs": post.get("refs") if isinstance(post.get("refs"), list) else [],
            "origin_node": str(post.get("origin_node") or "")[:80],
        }
        if winner == "text" and "text" not in chosen:
            payload = chosen
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


def _send_bytes(
    url: str,
    raw: bytes | None,
    cfg: RelayConfig,
    token: str,
    idempotency_key: str,
) -> HopResult:
    if raw is None:
        return HopResult(ok=False, error="too_large")
    return _request(
        "POST",
        url,
        raw=raw,
        timeout=cfg.timeout,
        token=token,
        idempotency_key=idempotency_key,
    )


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
    return _send_bytes(
        cfg.ck_events_url,
        encode_bounded(body, cfg.max_body_bytes),
        cfg,
        cfg.ck_token,
        event.id,
    )


def deliver_to_avrone(event: FusionEvent, cfg: RelayConfig | None = None) -> HopResult:
    cfg = cfg or load_config()
    # Avrone's board only accepts its five kinds. Mood is a conclusion; health
    # and evidence land as findings. The structured projection is the text.
    kind = "conclusion" if event.kind == "mood" else "finding"
    # The agent is also inside the text. A board that keeps only text still
    # echoes cyclekernal:* and the next read will not forward it.
    mirror = dict(event.payload)
    mirror["agent"] = f"cyclekernal:{event.kind}"
    body = {
        "id": event.id,
        "agent": mirror["agent"][:80],
        "role": RELAY_ROLE,
        "kind": kind,
        "text": json.dumps(mirror, separators=(",", ":"), default=str),
        "run_id": event.run_id or RELAY_RUN,
        "refs": [],
        "origin_node": ORIGIN_NODE,
    }
    return _send_bytes(
        cfg.avrone_blackboard_url,
        encode_bounded(body, cfg.max_body_bytes),
        cfg,
        cfg.avrone_token,
        event.id,
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
        "throttled": 0,
        "transport_errors": 0,
        "backoffs": 0,
        "last_backoff_s": 0.0,
        "duplicates": 0,
        "state_errors": state_errors,
        "circuit_opened": 0,
        "circuit_skipped": 0,
        "deadline_stops": 0,
        "reconciled": 0,
    }


def _accepted(stats: dict, hop: HopResult, counter: str) -> None:
    if isinstance(hop.body, dict) and hop.body.get("duplicate"):
        stats["duplicates"] += 1
    else:
        stats[counter] += 1


def _pull_and_forward(cfg: RelayConfig, stats: dict, track: _Track) -> bool:
    """Forward new board posts. Returns whether the hop was a transport failure."""
    url = cfg.avrone_blackboard_url
    if track.cursor:
        url = url + "?" + urllib.parse.urlencode({"since": track.cursor})
    hop = _request("GET", url, timeout=cfg.timeout, token=cfg.avrone_token)
    if not hop.ok:
        _account(stats, hop)
        return hop.error in _BACKOFF
    if isinstance(hop.body, dict) and _note_boot(track, "avrone", _boot_value(hop.body)):
        stats["reconciled"] += 1
        # The request used the previous cursor. A restarted board treats an
        # unknown since as "everything", but refetch without since so a peer
        # that drops unknown cursors cannot hide the posts we now must resend.
        if track.cursor == "":
            refetch = _request("GET", cfg.avrone_blackboard_url, timeout=cfg.timeout, token=cfg.avrone_token)
            if refetch.ok:
                hop = refetch
    posts = posts_from_blackboard(hop.body)
    if posts is None:
        stats["errors"] += 1
        stats["malformed"] += 1
        return False

    new_cursor = track.cursor
    for post in posts:
        if not isinstance(post, dict):
            stats["errors"] += 1
            stats["malformed"] += 1
            continue
        pid = _post_identity(post)
        if is_echo(post) or pid in track.seen:
            track.seen.add(pid)
            new_cursor = pid
            continue
        text = clip_text(post.get("text"), cfg.max_text_chars)
        if not text and not isinstance(post.get("payload"), dict):
            stats["errors"] += 1
            stats["malformed"] += 1
            track.seen.add(pid)
            new_cursor = pid
            track.cursor = new_cursor
            _remember(stats, cfg.state_path, track)
            continue
        event = _event_from_post(post, pid, cfg)
        try:
            if isinstance(event.payload, dict):
                validate_known_fields(event.payload)
        except FieldError:
            stats["rejected"] += 1
            stats["errors"] += 1
            track.seen.add(pid)
            new_cursor = pid
            track.cursor = new_cursor
            _remember(stats, cfg.state_path, track)
            continue
        result = deliver_to_cyclekernal(event, cfg)
        if result.ok:
            _accepted(stats, result, "forwarded")
            track.seen.add(pid)
            new_cursor = pid
            track.cursor = new_cursor
            _remember(stats, cfg.state_path, track)
            continue
        _account(stats, result)
        if result.error in _PERMANENT:
            track.seen.add(pid)
            new_cursor = pid
            track.cursor = new_cursor
            _remember(stats, cfg.state_path, track)
            continue
        # Auth, 404, 5xx, timeout, bad JSON: leave the id unseen and stop
        # this batch so a later post is not acked ahead of a failed one.
        track.cursor = new_cursor
        _remember(stats, cfg.state_path, track)
        return result.error in _BACKOFF
    track.cursor = new_cursor
    _remember(stats, cfg.state_path, track)
    return False


def _mirror(
    cfg: RelayConfig,
    stats: dict,
    track: _Track,
    *,
    url: str,
    kind: str,
    project,
    counter: str,
) -> bool:
    hop = _request("GET", url, timeout=cfg.timeout, token="")
    if not hop.ok:
        _account(stats, hop)
        return hop.error in _BACKOFF
    if not isinstance(hop.body, dict):
        stats["errors"] += 1
        stats["malformed"] += 1
        return False
    if kind == "health" and _note_boot(track, "cyclekernal", _boot_value(hop.body)):
        stats["reconciled"] += 1
        _remember(stats, cfg.state_path, track)
    projection = project(hop.body)
    eid = f"ck-{kind}-{fingerprint(projection)}"
    if not valid_id(eid) or eid in track.seen:
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
        track.seen.add(eid)
        _remember(stats, cfg.state_path, track)
        return False
    _account(stats, result)
    if result.error in _PERMANENT:
        track.seen.add(eid)
        _remember(stats, cfg.state_path, track)
        return False
    return result.error in _BACKOFF


def relay_loop(
    interval: float | None = None,
    max_cycles: int = 100,
    config: RelayConfig | None = None,
) -> dict:
    """Poll, forward, and mirror. ``max_cycles <= 0`` runs until interrupted.

    ``interval`` overrides ``FUSION_INTERVAL`` for the success path. Transport
    failures sleep the backoff instead, starting at ``FUSION_BACKOFF_BASE``
    and doubling up to ``FUSION_BACKOFF_MAX``, then reset after a clean cycle.

    Each cycle stops at ``FUSION_CYCLE_DEADLINE``. A host that times out or
    refuses a connection opens a circuit (``FUSION_BREAKER_THRESHOLD``,
    default one failure) and later calls to that host return immediately
    until ``FUSION_BREAKER_COOLDOWN`` elapses.
    """
    cfg = config or load_config()
    if interval is None:
        interval = cfg.interval
    track, state_errors = _load_state(cfg.state_path)
    stats = _empty_stats(state_errors)
    backoff = cfg.backoff_base
    cycle = 0
    breaker = _HostBreaker(cfg.breaker_threshold, cfg.breaker_cooldown)
    token_breaker = _cycle_breaker.set(breaker)
    token_stats = _cycle_stats.set(stats)
    try:
        while max_cycles <= 0 or cycle < max_cycles:
            deadline_at = time.monotonic() + cfg.cycle_deadline if cfg.cycle_deadline > 0 else None
            token_deadline = _cycle_deadline.set(deadline_at)
            try:
                transport = False
                if _pull_and_forward(cfg, stats, track):
                    transport = True
                if _mirror(
                    cfg,
                    stats,
                    track,
                    url=cfg.ck_mood_url,
                    kind="mood",
                    project=project_mood,
                    counter="mood_mirrored",
                ):
                    transport = True
                resent = stats["reconciled"]
                if _mirror(
                    cfg,
                    stats,
                    track,
                    url=cfg.ck_health_url,
                    kind="health",
                    project=project_health,
                    counter="health_mirrored",
                ):
                    transport = True
                # CycleKernal's boot id is on /health, which we read after the
                # forward. A restart forgets delivered ids; send them again now.
                if stats["reconciled"] != resent and _pull_and_forward(cfg, stats, track):
                    transport = True
                _remember(stats, cfg.state_path, track)
                if transport:
                    override = stats.pop("_sleep_override", None)
                    delay = min(backoff, cfg.backoff_max)
                    if isinstance(override, (int, float)):
                        delay = min(float(override), cfg.backoff_max)
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
            finally:
                _cycle_deadline.reset(token_deadline)
    finally:
        _cycle_breaker.reset(token_breaker)
        _cycle_stats.reset(token_stats)
    stats["seen"] = len(track.seen)
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
