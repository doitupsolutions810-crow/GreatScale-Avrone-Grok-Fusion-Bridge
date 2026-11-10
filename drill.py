#!/usr/bin/env python3
"""NEW harness. This file did not exist in the repository.

It is not the missing original drill.py and it does not read
/workspace/artifacts/fusion-bridge (that directory was not present).
The 60 second soak is a finite window. It does not disable deadlines.
"""
from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
from pathlib import Path

from bridge.soak_harness import run_soak


def main() -> int:
    parser = argparse.ArgumentParser(description="New Fusion Bridge soak drill")
    parser.add_argument("--soak-seconds", type=float, default=60.0)
    parser.add_argument("--out", type=Path, default=Path("artifacts/fusion-bridge"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    note = {
        "label": "new-harness",
        "not_original_verify_or_drill": True,
        "original_artifacts_path_missing": True,
        "python": sys.version,
        "platform": platform.platform(),
        "ipv6_loopback": _ipv6(),
    }
    (args.out / "environment.json").write_text(json.dumps(note, indent=2), encoding="utf-8")
    result = run_soak(seconds=args.soak_seconds)
    (args.out / "soak.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in result if k != "samples"}, indent=2))
    if result["lost"] != 0:
        return 1
    if result["end_backlog"] != 0:
        return 1
    return 0


def _ipv6() -> bool:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.bind(("::1", 0))
        sock.close()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
