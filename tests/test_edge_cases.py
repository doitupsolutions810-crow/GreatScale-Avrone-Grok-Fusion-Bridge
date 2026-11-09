"""Additive edge cases. The older bridge tests are left untouched."""
from __future__ import annotations

from bridge.edge_cases import run_all

XFAILS = {"lone_surrogate", "invalid_utf8", "dedup_source_id"}


def test_edge_suite(tmp_path):
    results = run_all("unit", tmp_path)
    failed = [item for item in results if item["status"] == "fail"]
    assert not failed, failed
    xfails = {item["name"] for item in results if item["status"] == "xfail"}
    assert xfails == XFAILS
