# GreatScale-Avrone-Grok-Fusion-Bridge

Relay between Avrone's learning bridge and CycleKernal's CognitiveBridge.

| System | Role | Default |
| --- | --- | --- |
| Avrone | Agent server. Public liveness is `GET /api/status`. | `http://127.0.0.1:8710` (`AVRONE_PORT`) |
| CycleKernal CognitiveBridge | Mood service. `GET /v1/current_mood`, `GET /health`, `WS /ws/hud`. | `http://127.0.0.1:8090` |
| This repo | Polls, forwards, mirrors. Soft-fails a dead hop. | `python -m bridge.avrone_cyclekernal_link` |

Port 8080 is CycleKernal's frontend container, not Avrone. The relay default used to disagree with one of the READMEs; both now follow the server defaults above.

The exact bytes are in [CONTRACT.md](CONTRACT.md). AVRONE integration checkpoint `406e727cd478799f5a0bbe091d045e39710903a1` supplies the authenticated blackboard route. CycleKernal [PR #14](https://github.com/doitupsolutions810-crow/CycleKernal/pull/14) serves `POST /v1/events` with `X-Admin-Token`, a 4096-byte 413 before auth, and `duplicate: true`; that is not on `main` yet. The relay calls `GET`/`POST /api/blackboard`, `POST /v1/events`, and the reads that already exist (`/v1/current_mood`, `/health`, probe `/api/status`). A missing hop records an error and the process keeps going. Each cycle has a deadline, and a host that accepts a connection but never answers is skipped instead of being dialed once per event.

## Configure

Read on every call, not at import.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AVRONE_BASE` | `http://127.0.0.1:8710` | Avrone origin. |
| `CK_BASE` | `http://127.0.0.1:8090` | CognitiveBridge origin. |
| `FUSION_TOKEN` | empty | `X-Admin-Token` for both sides. |
| `AVRONE_ADMIN_TOKEN` | falls back to `FUSION_TOKEN` | Avrone only. |
| `CK_FUSION_TOKEN` | falls back to `FUSION_TOKEN` | CycleKernal events only. |
| `FUSION_MAX_BODY_BYTES` | `4096` | Largest JSON body the relay will send. |
| `FUSION_MAX_TEXT_CHARS` | `4000` | Upper bound before the byte fit. The body cap wins: 4000 characters plus the JSON wrapper does not fit in 4096 bytes. |
| `FUSION_TIMEOUT` | `5` | Per-request seconds. |
| `FUSION_INTERVAL` | `2` | Sleep after a clean cycle. |
| `FUSION_BACKOFF_BASE` | `0.5` | First sleep after a timeout or a refused connection. Doubles. |
| `FUSION_BACKOFF_MAX` | `8` | Cap on that sleep. |
| `FUSION_CYCLE_DEADLINE` | `10` | Seconds one cycle may spend dialing. `0` turns it off. |
| `FUSION_BREAKER_THRESHOLD` | `1` | Timeouts or refused connections before that host is skipped. `0` turns it off. |
| `FUSION_BREAKER_COOLDOWN` | `30` | Seconds to skip a host after the breaker opens. |
| `FUSION_STATE_PATH` | empty | Seen-id file. Empty means memory only. |
| `FUSION_CYCLES` | `3` when run as a module | `0` runs until interrupted. |
| `AVRONE_SHIM_BASE` | empty | Second liveness candidate for the resolver. Used only when it differs from `AVRONE_BASE`. |

`CK_EVENTS_PATH` defaults to `/v1/events`. Setting it to `/v1/feedback` hits a master-branch route that only logs the body. That is not the contract.

## Test

```bash
pip install -r requirements.txt
pytest tests/ -v
```

The suite starts stub servers that implement the contract (auth, 4096-byte cap, id dedup) and runs `relay_loop` against them. No Avrone or CycleKernal checkout is required.

## Local stand-ins

Optional processes that implement the same routes on the real default ports, for a laptop demo. They are not the upstream servers.

```bash
pip install -r bridge/requirements.txt
FUSION_TOKEN=dev python -m bridge.cognitive_bridge_standalone   # :8090
FUSION_TOKEN=dev python -m bridge.avrone_shim                   # :8710
FUSION_TOKEN=dev python -m bridge.avrone_cyclekernal_link
python bridge/test_integration.py
```

With the token unset, the stand-ins allow the call so a loopback demo can start. The reference stubs in `tests/` always require the token.

## Compose

`docker-compose.avrone.yml` points the relay at `avrone:8710` and `cognitive-bridge:8090` and probes `/api/status` and `/health`. The Avrone and CognitiveBridge build contexts are not in this repository. Compose will not start those two services until those directories exist. See the gaps below.

## Gaps that remain

- Deploy the tested AVRONE integration branch for `/api/blackboard`. CycleKernal `POST /v1/events` exists on [PR #14](https://github.com/doitupsolutions810-crow/CycleKernal/pull/14) and not yet on `main`. Until both are deployed, that hop fails soft.
- `bridge.greatscale_evidence.evidence_event` accepts revision-pinned measured snapshots and produces bounded `greatscale` / `evidence` events; `deliver_evidence` explicitly sends them to CycleKernal. It validates software-check variance and orientation, not physical claims.
- The relay polls. It does not subscribe to `/ws/events` or `/ws/hud`.
- Delivery is at-least-once. A lost state file is safe only because the peer dedups ids.

## Source recovery and validation

This branch repairs the placeholder regression on main. See [RECOVERY_VALIDATION.md](RECOVERY_VALIDATION.md) and run `python scripts/verify-source-registry.py` to verify the complete declared source set. The relay and evidence suite passes 52 tests. Docker deployment and live model inference have separate gates.
