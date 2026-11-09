"""Probe the configured bases without crashing.

Avrone's public liveness route is ``GET /api/status`` (there is no ``/health``
or ``/api/health``). CycleKernal's is ``GET /health``.

``AVRONE_BASE`` (default ``http://127.0.0.1:8710``) is probed first. ``AVRONE_SHIM_BASE``
is a second candidate only when it is set and different, so the fallback is not
the same URL twice. Either miss returns None. The relay still uses the
configured URL and fails that hop soft.
"""
from __future__ import annotations

import os
import urllib.request

try:
    from .settings import load_config
except ImportError:
    from settings import load_config


def _probe(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def resolve_avrone_base(timeout: float = 2.0) -> str | None:
    cfg = load_config()
    if _probe(cfg.avrone_status_url, timeout=timeout):
        return cfg.avrone_base
    shim = os.environ.get("AVRONE_SHIM_BASE", "").strip().rstrip("/")
    if shim and shim != cfg.avrone_base and _probe(shim + cfg.status_path, timeout=timeout):
        return shim
    return None


def resolve_ck_base(timeout: float = 2.0) -> str | None:
    cfg = load_config()
    if _probe(cfg.ck_base + cfg.health_path, timeout=timeout):
        return cfg.ck_base
    return None


if __name__ == "__main__":
    av = resolve_avrone_base()
    ck = resolve_ck_base()
    print(f"Avrone: {av or 'UNREACHABLE'}")
    print(f"CycleKernal: {ck or 'UNREACHABLE'}")
