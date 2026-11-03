# Relay recovery — November 3, 2026

Main revision `3f8cc12a965486d5157431c7d906f04024b904cf` contains literal `PLACEHOLDER` in its README, relay and evidence module. Its tests fail during relay import with `NameError`.

The recovery adapts the complete relay/contract/test implementation from PR #1 revision `8c5ea85e973c52631cd9fc1b14e4392f19a1f14c`, retaining source pins and content hashes in `source-registry.json`. The implementation's original 35 tests passed before changes.

The evidence adapter now transports bounded, finite Great Scale snapshots from an explicit full producer revision. It checks outcome counts, variance and orientation, snapshots caller data and binds it to a deterministic digest. These are software-check observations and mathematical invariants, not a physical inference.

The repaired relay and evidence suite passes **52 tests** in **15.97 seconds**. Its existing HTTP peers are explicit contract stubs. The current AVRONE integration branch supplies the blackboard endpoint (checkpoint `406e727cd478799f5a0bbe091d045e39710903a1`); the CycleKernal event endpoint remains in PR #14. A separate cross-repository network execution is required to establish that combination.

Reproduce with `python -m pytest` and `python scripts/verify-source-registry.py`.
