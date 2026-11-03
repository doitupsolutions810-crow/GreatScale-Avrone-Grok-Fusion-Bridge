"""Great Scale QGT -> FusionEvent arithmetic adapter (NOT a provenance certificate)."""
from __future__ import annotations

import math
import re
from typing import Any

from .avrone_cyclekernal_link import FusionEvent

_C40 = re.compile(r'[a-fA-F0-9]{40}\Z')
_C64 = re.compile(r'[a-fA-F0-9]{64}\Z')
CONE = (3 - math.sqrt(5)) / 2


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite real number')
    try:
        val = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{name} outside representable range') from exc
    if not math.isfinite(val):
        raise ValueError(f'{name} must be finite')
    return val


def greatscale_event(snapshot: dict[str, Any], *, run_id: str,
                     source_commit: str, artifact_sha256: str) -> FusionEvent:
    """Encode an independently checkable *arithmetic* claim, not permission to act.

    The source revision and artifact hash are labels supplied by the caller;
    neither a GitHub commit proof nor artifact-byte verification happens here.
    """
    if not isinstance(snapshot, dict) or not isinstance(source_commit, str) or not _C40.fullmatch(source_commit):
        raise ValueError('snapshot object and full 40-hex source commit required')
    if not isinstance(artifact_sha256, str) or not _C64.fullmatch(artifact_sha256):
        raise ValueError('full 64-hex artifact digest required')
    if snapshot.get('topologicalMappingValid') is not True:
        raise ValueError('producer has not reported numerical acceptance')
    chern = snapshot.get('chernNumber')
    if type(chern) is not int:
        raise ValueError('integer Chern number required')
    area = _finite(snapshot.get('metricAreaBudget'), 'metricAreaBudget')
    if area <= 0 or area + 1e-8 < abs(chern):
        raise ValueError('positive area and A_Q >= |C| required')
    saturation = abs(chern) / area
    if saturation > 1 + 1e-8:
        raise ValueError('geometry violates the Chern lower bound')
    saturation = min(1.0, saturation)
    s = (1 - saturation ** 2) / 4
    psi = CONE + s
    reported = _finite(snapshot.get('acceptedPsiTopological'), 'acceptedPsiTopological')
    if not math.isclose(reported, psi, rel_tol=0, abs_tol=1e-6):
        raise ValueError('Great Scale scalar disagrees with signed/absolute channels')
    polar = chern / area
    reported_polar = snapshot.get('signedTopologicalPolarization')
    if reported_polar is not None and not math.isclose(_finite(reported_polar,'polarization'), polar, rel_tol=0, abs_tol=1e-6):
        raise ValueError('topological orientation mismatch')
    payload = {
        'schema': 'great-scale.fusion-evidence/v1',
        'C': chern, 'A_Q': area, 'E_Q': area - abs(chern),
        'S_T': s, 'psi_T': psi, 'CONE': CONE,
        'polarization': polar,
        'source_commit': source_commit.lower(),
        'artifact_sha256': artifact_sha256.lower(),
        'status': 'arithmetic_checked_only',
        'source_revision_verified': False,
        'artifact_digest_verified': False,
        'execution_receipt_verified': False,
        'authorization_verified': False,
    }
    return FusionEvent('greatscale', 'evidence', run_id, payload)
