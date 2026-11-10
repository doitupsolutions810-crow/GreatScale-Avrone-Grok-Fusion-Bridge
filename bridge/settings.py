"""Environment-backed relay settings.

Defaults follow the upstream servers reviewed for this bridge:

- Avrone ``avrone/server.py`` binds ``AVRONE_PORT`` default **8710**
  (``AVRONE_HOST`` default ``127.0.0.1``). Port 8080 is CycleKernal's
  frontend container, not Avrone.
- CycleKernal ``src/bridge/main.py`` binds **8090**.

Every value is read when :func:`load_config` is called, not at import.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .contract import (
    BLACKBOARD_PATH,
    EVENTS_PATH,
    HEALTH_PATH,
    MAX_BODY_BYTES,
    MAX_TEXT_CHARS,
    MOOD_PATH,
    STATUS_PATH,
)

DEFAULT_AVRONE_PORT = 8710
DEFAULT_CK_PORT = 8090
DEFAULT_AVRONE_BASE = f"http://127.0.0.1:{DEFAULT_AVRONE_PORT}"
DEFAULT_CK_BASE = f"http://127.0.0.1:{DEFAULT_CK_PORT}"


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: str) -> str:
    raw = _env_str(name, default) or default
    return raw if raw.startswith("/") else "/" + raw


@dataclass(frozen=True)
class RelayConfig:
    avrone_base: str
    ck_base: str
    avrone_token: str
    ck_token: str
    timeout: float
    max_body_bytes: int
    max_text_chars: int
    backoff_base: float
    backoff_max: float
    interval: float
    cycle_deadline: float
    breaker_threshold: int
    breaker_cooldown: float
    state_path: str
    blackboard_path: str
    events_path: str
    mood_path: str
    health_path: str
    status_path: str

    @property
    def avrone_blackboard_url(self) -> str:
        return self.avrone_base + self.blackboard_path

    @property
    def ck_events_url(self) -> str:
        return self.ck_base + self.events_path

    @property
    def ck_mood_url(self) -> str:
        return self.ck_base + self.mood_path

    @property
    def ck_health_url(self) -> str:
        return self.ck_base + self.health_path

    @property
    def avrone_status_url(self) -> str:
        return self.avrone_base + self.status_path


def load_config() -> RelayConfig:
    shared = _env_str("FUSION_TOKEN")
    # A service-specific token wins so one side can be rotated without the other.
    avrone_token = _env_str("AVRONE_ADMIN_TOKEN") or shared
    ck_token = _env_str("CK_FUSION_TOKEN") or shared
    backoff_base = _env_float("FUSION_BACKOFF_BASE", 0.5)
    backoff_max = _env_float("FUSION_BACKOFF_MAX", 8.0)
    if backoff_max < backoff_base:
        backoff_max = backoff_base
    timeout = _env_float("FUSION_TIMEOUT", 5.0)
    if timeout <= 0:
        timeout = 5.0
    # One black-hole attempt must not be repeated for every queued event.
    # 10s covers a healthy batch and still cuts a 15s-per-event hang short.
    cycle_deadline = _env_float("FUSION_CYCLE_DEADLINE", 10.0)
    breaker_threshold = _env_int("FUSION_BREAKER_THRESHOLD", 1)
    if breaker_threshold < 0:
        breaker_threshold = 0
    breaker_cooldown = _env_float("FUSION_BREAKER_COOLDOWN", 30.0)
    max_body = _env_int("FUSION_MAX_BODY_BYTES", MAX_BODY_BYTES)
    if max_body < 256:
        max_body = 256
    max_text = _env_int("FUSION_MAX_TEXT_CHARS", MAX_TEXT_CHARS)
    if max_text < 8:
        max_text = 8
    return RelayConfig(
        avrone_base=(_env_str("AVRONE_BASE") or _env_str("AVRONE_URL") or DEFAULT_AVRONE_BASE).rstrip("/"),
        ck_base=_env_str("CK_BASE", DEFAULT_CK_BASE).rstrip("/"),
        avrone_token=avrone_token,
        ck_token=ck_token,
        timeout=timeout,
        max_body_bytes=max_body,
        max_text_chars=max_text,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
        interval=_env_float("FUSION_INTERVAL", 2.0),
        cycle_deadline=cycle_deadline,
        breaker_threshold=breaker_threshold,
        breaker_cooldown=breaker_cooldown,
        state_path=_env_str("FUSION_STATE_PATH"),
        blackboard_path=_env_path("AVRONE_BLACKBOARD_PATH", BLACKBOARD_PATH),
        events_path=_env_path("CK_EVENTS_PATH", EVENTS_PATH),
        mood_path=_env_path("CK_MOOD_PATH", MOOD_PATH),
        health_path=_env_path("CK_HEALTH_PATH", HEALTH_PATH),
        status_path=_env_path("AVRONE_STATUS_PATH", STATUS_PATH),
    )
