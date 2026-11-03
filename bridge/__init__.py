"""Fusion relay between Avrone and CycleKernal.

The HTTP contract the other two repositories must implement is ``CONTRACT.md``.
"""
from __future__ import annotations

from .avrone_cyclekernal_link import (
    FusionEvent,
    post_to_avrone_bridge,
    post_to_cyclekernal,
    relay_loop,
)

__all__ = [
    "FusionEvent",
    "post_to_avrone_bridge",
    "post_to_cyclekernal",
    "relay_loop",
]
__version__ = "2.0.0"
