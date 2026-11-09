"""Transport measured Great Scale software-check snapshots without claiming physical proof.

The mathematical producer is AVRONE's revision-pinned avrone/greatscale.py.
This module validates its outcome-variance fields and carries its source pin.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

try:
    from .avrone_cyclekernal_link import FusionEvent, deliver_to_cyclekernal
    from .contract import encode_bounded, valid_id
except ImportError:
    from avrone_cyclekernal_link import FusionEvent, deliver_to_cyclekernal
    from contract import encode_bounded, valid_id


def evidence_event(snapshot: dict[str, Any], *, run_id: str, source_revision: str) -> FusionEvent:
    if not isinstance(snapshot, dict) or not valid_id(run_id):
        raise ValueError("snapshot must be an object and run_id a valid identifier")
    if not isinstance(source_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("source_revision must be the full producer commit")
    failures, runs = snapshot.get("failures"), snapshot.get("runs")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (failures, runs)):
        raise ValueError("failures and runs must be measured integer counts")
    if not 0 <= failures <= runs or not 1 <= runs <= 1_000_000_000:
        raise ValueError("outcome counts are outside the producer domain")
    p = failures / runs
    for name, expected in (("p", p), ("S", p * (1 - p)), ("variance", p * (1 - p))):
        value = snapshot.get(name)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError("snapshot variance does not match its outcome counts")
    orientation = "pass" if failures == 0 else "fail" if failures == runs else "mixed"
    if snapshot.get("orientation") != orientation:
        raise ValueError("snapshot orientation does not match its outcome counts")
    try:
        raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("snapshot must be finite UTF-8 JSON") from None
    if len(raw) > 2048:
        raise ValueError("snapshot exceeds its transport budget")
    # Snapshot before publishing: the caller cannot mutate evidence after its ID is bound.
    frozen = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    identity = hashlib.sha256((run_id + ":" + source_revision + ":" + digest).encode()).hexdigest()
    event = FusionEvent(source="greatscale", kind="evidence", run_id=run_id,
                        id="gs-" + identity[:40], payload={
                            "snapshot": frozen, "snapshot_sha256": digest,
                            "producer_revision": source_revision,
                            "producer_module": "avrone/greatscale.py",
                            "interpretation": "Software-check outcome variance; no physical inference.",
                        })
    if encode_bounded(json.loads(event.to_json()), 4096) is None:
        raise ValueError("evidence event exceeds the peer body cap")
    return event


def deliver_evidence(snapshot: dict[str, Any], *, run_id: str, source_revision: str, config=None):
    return deliver_to_cyclekernal(evidence_event(snapshot, run_id=run_id,
                                               source_revision=source_revision), config)
