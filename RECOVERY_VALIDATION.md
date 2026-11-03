# Relay recovery — November 3, 2026

Main revision `3f8cc12a965486d5157431c7d906f04024b904cf` contains literal `PLACEHOLDER` in its README, relay and evidence module. Its tests fail during relay import with `NameError`.

The recovery adapts the complete relay/contract/test implementation from PR #1 revision `8c5ea85e973c52631cd9fc1b14e4392f19a1f14c`, retaining source pins and content hashes in `source-registry.json`. The implementation's original 35 tests passed before changes.

The evidence adapter now transports bounded, finite Great Scale snapshots from an explicit full producer revision. It checks outcome counts, variance and orientation, snapshots caller data and binds it to a deterministic digest. These are software-check observations and mathematical invariants, not a physical inference.

The repaired relay and evidence suite passes **52 tests** in **15.97 seconds**. Its existing HTTP peers are explicit contract stubs. The current AVRONE integration branch supplies the blackboard endpoint (checkpoint `406e727cd478799f5a0bbe091d045e39710903a1`); the CycleKernal event endpoint remains in PR #14. The subsequent real-service execution below establishes the software transport combination with the targeted 413 repair.

Reproduce with `python -m pytest` and `python scripts/verify-source-registry.py`.

## Actual service execution

`python scripts/verify-live-peers.py --avrone-source /path/to/Avrone --cycle-source /path/to/CycleKernal` starts actual checkouts on loopback, supplies explicit metrics through the actual WS Relay, and checks forwarding, mood/health mirroring, authorization, body caps, evidence payloads, duplicate retries and service restarts. It completes **50 cycles and 21 assertions**. No HTTP peer is a stand-in. The AVRONE model mode is explicitly mock; software transport success does not establish live inference or physical telemetry. The JSON report includes runtime module SHA-256 digests.

The first network attempt exposed a TCP reset while an early oversized request forced `Connection: close`. The matching repairs are AVRONE `0b2515516b3689ddfc6787ba9ac76e00c22735c9` and CycleKernal `6e7cbec29e5185e41e314fc67d70a3ac24104ea6` (PR #15, based on PR #14 source `f3a763fe230b6de3450c235c165773c5ea9550bb`). The HTTP server now drains unread frames. AVRONE's focused transport selection passes 97 tests; CycleKernal's `tests/` selection passes 47. A repeat Fusion regression passes all 52 tests in 15.41 seconds.
