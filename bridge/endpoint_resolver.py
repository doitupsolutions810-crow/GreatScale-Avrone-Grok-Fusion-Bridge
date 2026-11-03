"""
Endpoint resolver for the Avrone-CycleKernal fusion bridge.

Gap: relay hardcoded bases; real Avrone may be on 8710, shim on 8710,
or either may be down. This probes and resolves without crashing.

Behavior:
1. Probe real Avrone health endpoint first.
2. Fall back to local shim if real is unreachable.
3. Soft-fail (return None) if both are dead — relay handles it.
4. CycleKernal base from CK_BASE env var for remote hosts.
"""
from __future__ import annotations

import os
import urllib.request

REAL_AVRONE = "http://127.0.0.1:8710"
SHIM_AVRONE = "http://127.0.0.1:8710"
DEFAULT_CK = "http://127.0.0.1:8090"


def _probe(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def resolve_avrone_base() -> str | None:
    """Return the live Avrone base, or None if nothing answers."""
    if _probe(REAL_AVRONE):
        return REAL_AVRONE
    if _probe(SHIM_AVRONE):
        return SHIM_AVRONE
    return None


def resolve_ck_base() -> str:
    """CycleKernal base from env, defaulting to local."""
    return os.environ.get("CK_BASE", DEFAULT_CK)


if __name__ == "__main__":
    av = resolve_avrone_base()
    ck = resolve_ck_base()
    print(f"Avrone: {av or 'UNREACHABLE'}")
    print(f"CycleKernal: {ck}")
