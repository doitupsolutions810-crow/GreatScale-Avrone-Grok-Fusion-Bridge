"""Manual opt-in test against the real, authenticated draft service contracts.

No mock shim is launched: this command never treats a stand-in as a production proof.
Requires the AVRONE blackboard and CycleKernal events intake from their pending PRs.
"""
from __future__ import annotations

import json
import os
import sys

from avrone_cyclekernal_link import (_exchange, _origin, relay_loop, AVRONE_DEFAULT,
                                    CK_DEFAULT, BridgeConfigError)


def main() -> int:
    if os.getenv('FUSION_INTEGRATION') != '1':
        print('NOT RUN: set FUSION_INTEGRATION=1 after approving live relay effects')
        return 2
    avtoken, cktoken = os.getenv('AVRONE_ADMIN_TOKEN'), os.getenv('CK_FUSION_TOKEN')
    if not avtoken or not cktoken:
        print('NOT RUN: missing AVRONE_ADMIN_TOKEN or CK_FUSION_TOKEN')
        return 2
    try:
        av = _origin('AVRONE_BASE', AVRONE_DEFAULT)
        ck = _origin('CK_BASE', CK_DEFAULT)
        board = _exchange(av, '/api/blackboard', avtoken)
        health = _exchange(ck, '/health', cktoken)
        if not isinstance(board.get('posts'), list) or not isinstance(board.get('boot'), str):
            raise ValueError('AVRONE blackboard contract missing')
        if not isinstance(health.get('boot'), str):
            raise ValueError('CycleKernal boot identity missing')
        result = relay_loop(max_cycles=1)
    except (BridgeConfigError, ValueError, OSError, UnicodeError, RecursionError):
        print('FAIL: live peer contract or transport not available; no success claimed')
        return 1
    print(json.dumps({'live_contract_reachable': True, **result}, sort_keys=True))
    if result['errors']:
        return 1
    if not board['posts']:
        print('PASS: authenticated read/health only; no event-delivery proof (empty board)')
    else:
        print('PASS: acknowledged relay events; verify downstream durable record separately')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
