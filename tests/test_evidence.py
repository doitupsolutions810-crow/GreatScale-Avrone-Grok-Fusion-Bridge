import hashlib
import json

import pytest

from bridge.greatscale_evidence import evidence_event

REVISION = "4232b83ece8662fa7c28a4cd59e660f3d203261a"


def snapshot(failures=1, runs=4):
    p = failures / runs
    return {"failures": failures, "runs": runs, "p": p, "S": p * (1-p), "variance": p * (1-p),
            "orientation": "pass" if failures == 0 else "fail" if failures == runs else "mixed"}


@pytest.mark.parametrize("failures,runs", [(0, 4), (1, 4), (4, 4), (1, 1000000000)])
def test_measured_snapshot_is_bound_to_event_and_source(failures, runs):
    source = snapshot(failures, runs)
    event = evidence_event(source, run_id="run-1", source_revision=REVISION)
    raw = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    assert event.payload["snapshot_sha256"] == hashlib.sha256(raw).hexdigest()
    assert event.source == "greatscale" and event.kind == "evidence"
    assert event.id == evidence_event(source, run_id="run-1", source_revision=REVISION).id
    assert len(event.to_json().encode()) <= 4096
    source["failures"] = 90
    assert event.payload["snapshot"]["failures"] == failures


@pytest.mark.parametrize("changes", [{"failures": True}, {"runs": 0}, {"failures": 8},
    {"p": float("nan")}, {"S": float("inf")}, {"p": True}, {"variance": 0.1},
    {"orientation": "verified"}, {"extra": "\ud800"}, {"extra": "x" * 3000}])
def test_bad_measurements_and_unbounded_data_are_refused(changes):
    value = snapshot();value.update(changes)
    with pytest.raises(ValueError):
        evidence_event(value, run_id="r", source_revision=REVISION)


@pytest.mark.parametrize("run_id,revision", [("bad\n", REVISION), ("r", "main"), ("r", "a" * 39)])
def test_evidence_requires_complete_identifiers(run_id, revision):
    with pytest.raises(ValueError):
        evidence_event(snapshot(), run_id=run_id, source_revision=revision)
