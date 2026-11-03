# GreatScale-Avrone-Grok-Fusion-Bridge

Relay between Avrone's learning bridge and CycleKernal's CognitiveBridge.

| System | Role | Default |
| --- | --- | --- |
| Avrone | Agent server. Public liveness is `GET /api/status`. | `http://127.0.0.1:8710` (`AVRONE_PORT`) |
| CycleKernal CognitiveBridge | Mood service. `GET /v1/current_mood`, `GET /health`, `WS /ws/hud`. | `http://127.0.0.1:8090` |
| This repo | Polls, forwards, mirrors. Soft-fails a dead hop. | `python -m bridge.avrone_cyclekernal_link` |

Port 8080 is CycleKernal's frontend container, not Avrone. The relay default used to disagree with one of the READMEs; both now follow the server defaults above.

The exact bytes the other two repositories must accept are in [CONTRACT.md](CONTRACT.md). Short version: Avrone still has no blackboard route, and CycleKernal still has no event ingest. The relay calls the routes that document requires (`GET`/`POST /api/blackboard`, `POST /v1/events`) and the reads that already exist (`/v1/current_mood`, `/health`, probe `/api/status`). Until those two new routes ship upstream, the corresponding hop records an error and the process keeps going.

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
| `FUSION_MAX_TEXT_CHARS` | `4000` | Avrone's per-post text clip. |
| `FUSION_TIMEOUT` | `5` | Per-request seconds. |
| `FUSION_INTERVAL` | `2` | Sleep after a clean cycle. |
| `FUSION_BACKOFF_BASE` | `0.5` | First sleep after a timeout or a refused connection. Doubles. |
| `FUSION_BACKOFF_MAX` | `8` | Cap on that sleep. |
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

- Avrone does not implement `/api/blackboard`. CycleKernal `src/bridge/main.py` does not implement `POST /v1/events`. The relay cannot deliver to the real processes until those patches land. CONTRACT.md is the patch spec.
- greatscale evidence is a reserved `source` / `kind`. Nothing in this repo produces it.
- The relay polls. It does not subscribe to `/ws/events` or `/ws/hud`.
- Delivery is at-least-once. A lost state file is safe only because the peer dedups ids.
