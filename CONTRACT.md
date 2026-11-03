# Fusion bridge HTTP contract

Version 1. The relay in this repository speaks only this document.

Reviewed against upstream default-branch files (do not treat this as a claim that those files already implement the new routes):

| Repo | SHA | File |
| --- | --- | --- |
| [Avrone](https://github.com/doitupsolutions810-crow/Avrone) | `9bb0e750aa40947f7f78b6ef41dcfa9afe6ae202` | `avrone/server.py`, `avrone/bridge.py` |
| [CycleKernal](https://github.com/doitupsolutions810-crow/CycleKernal) | `cbbcce9b6c3864e8710a5607033e351364dab605` on `main` | `src/bridge/main.py` had no event route |
| [CycleKernal PR #14](https://github.com/doitupsolutions810-crow/CycleKernal/pull/14) | `6aa783bb561356b7d8a7ac63769439b00401d6ec` | `POST /v1/events` is implemented on that branch |

## Ports

| Service | Bind | Where that default lives |
| --- | --- | --- |
| Avrone | `127.0.0.1:8710` | `AVRONE_PORT` default in `avrone/server.py`. Compose publishes `${AVRONE_PUBLISH_PORT:-8710}`. |
| CycleKernal CognitiveBridge | `0.0.0.0:8090` | Hardcoded `uvicorn.run(..., port=8090)` in both `src/bridge/main.py` and `master/src/bridge/main.py`. |

Port **8080** is CycleKernal's frontend container (`8080:80` in that repo's compose file). It is not Avrone.

This relay reads bases at call time:

| Variable | Default |
| --- | --- |
| `AVRONE_BASE` | `http://127.0.0.1:8710` |
| `CK_BASE` | `http://127.0.0.1:8090` |
| `AVRONE_BLACKBOARD_PATH` | `/api/blackboard` |
| `CK_EVENTS_PATH` | `/v1/events` |
| `CK_MOOD_PATH` | `/v1/current_mood` |
| `CK_HEALTH_PATH` | `/health` |
| `AVRONE_STATUS_PATH` | `/api/status` |
| `FUSION_CYCLE_DEADLINE` | `10` seconds. `0` disables the cap. |
| `FUSION_BREAKER_THRESHOLD` | `1` failed dial (timeout or connection refused) opens the host. `0` disables it. |
| `FUSION_BREAKER_COOLDOWN` | `30` seconds. While open, calls to that host are not sent. |

`CK_EVENTS_PATH=/v1/feedback` reaches a route that **already exists** on CycleKernal's master bridge. That route logs the body and returns `{"status":"ingested"}`. It does not authenticate, does not cap the body, and does not store an id. Pointing the relay at it is not an implementation of this contract.

## Auth

Fusion writes and the new Avrone read use Avrone's existing admin header:

```
X-Admin-Token: <token>
```

| Variable | Sent to |
| --- | --- |
| `FUSION_TOKEN` | both sides, unless a specific token is set |
| `AVRONE_ADMIN_TOKEN` | Avrone only; wins over `FUSION_TOKEN` |
| `CK_FUSION_TOKEN` | CycleKernal `POST /v1/events` only; wins over `FUSION_TOKEN` |

Compare the token with a constant-time compare, the way `require_admin` already does.

| Situation | Status | Body |
| --- | --- | --- |
| Token configured, header missing or wrong | 401 | `{"detail":"Bad admin token"}` |
| Route is off because no token is configured | 403 | `{"detail":"Admin access is off. Set AVRONE_ADMIN_TOKEN to enable it."}` |

The relay treats 401 and 403 as auth failures. It does **not** remember the post as delivered, so a corrected token is retried on the next cycle. Auth failures do not use the transport backoff.

`GET /v1/current_mood` and `GET /health` stay public, matching CycleKernal today. The relay does not send `X-Admin-Token` on those two reads. `GET /api/status` stays public; the relay uses it only as a liveness probe.

## Body cap

Avrone's process-wide `BodyLimit` is 16 MB (`AVRONE_MAX_BODY_BYTES`). Do not shrink that. The cap below applies only to the two new routes.

Avrone's in-process bridge clips post `text` to **4000 characters** (`avrone/bridge.py` `MAX_TEXT`). That clip does **not** fit these HTTP routes. A 4000-character text plus the JSON wrapper is already more than **4096 bytes**, and a multibyte character costs 2–4 UTF-8 bytes, so the body cap rejects the request before any character clip can run.

The cap on `POST /api/blackboard` and `POST /v1/events` is the length of the raw UTF-8 body:

| Check | Rule |
| --- | --- |
| Declared `Content-Length` > 4096 | **413** immediately, connection closed, body not stored. Check this **before** auth. |
| Buffered body > 4096 | 413, body not stored |
| Body of exactly 4096 bytes | Accept, then validate the JSON. |
| Sender-side text | Clip `text` / `payload.text` so `len(utf8_json) <= 4096`. Count the serialized document, including the wrapper and JSON escapes, not the Python character length. Mark a cut with `...`. |

413 body: `{"detail":"request body too large (max 4096 bytes)"}`.

The relay never sends a body over `FUSION_MAX_BODY_BYTES` (default 4096). A 4000-character payload is shortened until the wrapped document fits. A 413 from a peer is still permanent for that id. A body the relay cannot shrink under the cap is dropped and remembered so it cannot block the queue.

## Avrone

### Already implemented

Do not replace these.

| Method | Path | Auth | Behavior today |
| --- | --- | --- | --- |
| GET | `/api/status` | public | Liveness. Probe only checks HTTP 200. |
| GET | `/api/runs` | loopback or `X-Admin-Token` | Run index. |
| GET | `/api/runs/{run_id}/bridge` | loopback or `X-Admin-Token` | `{"run_id", "count", "posts"}` for one run. Read-only. |
| WS | `/ws/events` | loopback or token (`?token=` or header) | Live codex stream. The relay does not open it. |

A post in `avrone/bridge.py` is:

```json
{
  "id": "0123456789ab",
  "run_id": "run",
  "agent": "scout",
  "role": "agent",
  "kind": "finding",
  "text": "what the agent concluded",
  "refs": [],
  "ts": 1700000000.0,
  "origin_node": "local"
}
```

`kind` is one of `finding`, `question`, `answer`, `correction`, `conclusion`. `id` matches `^[A-Za-z0-9_.:-]{1,80}$`. The ring keeps 500 posts (`AVRONE_BRIDGE_MAX_POSTS`, hard cap 5000). An id the ring has evicted is not resurrected.

There is **no** HTTP route that lists every run's posts, and **no** HTTP route that accepts a post from outside the process. `bridge.py` says the fleet relay is a different layer.

### Required: `GET /api/blackboard`

Fan-in of bridge posts this node is willing to share, oldest first.

```
GET /api/blackboard?since=<post id>
X-Admin-Token: <token>
```

`since` is optional. Unknown or omitted `since` returns the current ring (same rule as `Bridge.export`). A known `since` returns posts strictly after that id.

```json
{
  "posts": [ { "id": "0123456789ab", "run_id": "run", "agent": "scout", "role": "agent", "kind": "finding", "text": "...", "refs": [], "ts": 1700000000.0, "origin_node": "local" } ],
  "count": 1
}
```

`count` is the number of posts in this response. Non-list `posts`, or a body that is not this object, is a malformed read. The relay also accepts a bare JSON array so a partial server can still drain; new servers must send the object.

Suggested backing store: walk this node's bridges and call `export`. One implementation may also keep a single bridge whose `run_id` is `fusion-relay` for posts the relay writes.

### Required: `POST /api/blackboard`

```
POST /api/blackboard
Content-Type: application/json
X-Admin-Token: <token>
```

```json
{
  "id": "ck-mood-0123456789ab",
  "run_id": "fusion-relay",
  "agent": "cyclekernal:mood",
  "role": "cyclekernal-relay",
  "kind": "conclusion",
  "text": "{\"mood\":\"convergent\",\"mood_dot\":0.72}",
  "refs": [],
  "origin_node": "fusion-bridge"
}
```

| Field | Rule |
| --- | --- |
| `kind` | Required after defaulting. Must be one of the five bridge kinds. Anything else, including `mood` and `health`, is **400**. |
| `text` | Required after clipping. Empty is **400**. |
| `id` | Optional. If present, must match the id pattern or **400**. If omitted, the server assigns `uuid4().hex[:12]`. |
| `run_id` | Optional, default `run`, clipped to 80 characters. The relay sends `fusion-relay` for mirrors. |
| `agent`, `role`, `origin_node` | Clipped to 80, 60, and 80. |
| `refs` | List of ids, max 20, invalid entries dropped. Unlike `Bridge.post`, unknown ids are **not** rejected: this route is a fleet aggregate and does not have one run's ref set. |
| `ts` | Finite number greater than 0, else the server clock. |

Same id already stored, or already evicted from the ring:

```json
{"posted": true, "duplicate": true, "id": "ck-mood-0123456789ab"}
```

Do not overwrite the stored post (same rule as `merge_remote`).

New post, HTTP 200:

```json
{"posted": true, "duplicate": false, "id": "ck-mood-0123456789ab"}
```

Malformed JSON, or a JSON value that is not an object: **400** `{"detail":"body must be JSON"}`.

The relay posts CycleKernal mood as `kind: "conclusion"` and health as `kind: "finding"`, with `origin_node: "fusion-bridge"` and `role: "cyclekernal-relay"`. It will not forward those posts back to CycleKernal.

## CycleKernal

### Already implemented (`src/bridge/main.py`)

| Method | Path | Auth | Body today |
| --- | --- | --- | --- |
| GET | `/v1/current_mood` | none | `CognitiveState.to_dict()`: `mood`, `traits`, `mood_dot`, `ns_fitness`, `genome_id`, `promote`, `metrics`, `timestamp`, `explanation`, ... |
| GET | `/health` | none | `{"status":"ghost-online","version":"3.1.0","timestamp":"..."}` |
| WS | `/ws/hud` | none | Same mood object. The relay does not open it. |

Mood is produced by LoopMem metrics (`map_metrics_to_traits`), not by inbound Avrone posts. Keep that. `timestamp`, `metrics`, and `explanation` change on every heartbeat. The relay fingerprints only `mood`, `mood_dot` and `ns_fitness` rounded to 2 decimals, `genome_id`, `traits`, and `promote.action`. Health is fingerprinted on `status`, `version`, and `mood` only, so a fresh `timestamp` does not repost.

`master/src/bridge/main.py` also has `POST /v1/feedback`. That is not this contract. See Ports above.

### `POST /v1/events` (CycleKernal PR #14)

Implemented on [PR #14](https://github.com/doitupsolutions810-crow/CycleKernal/pull/14) (`6aa783bb`), not yet on `main`. The relay already speaks it.

What that branch serves:

- `POST /v1/events` and the alias `POST /events`.
- Auth: `X-Admin-Token`, `Authorization: Bearer`, or `X-Events-Token`. `CK_FUSION_TOKEN` wins over `FUSION_TOKEN`. `CK_EVENTS_TOKEN` and `CK_EVENTS_TOKEN_PREVIOUS` also match. No token configured is 403. A wrong token is 401 `{"detail":"Bad admin token"}`.
- A declared or buffered body over 4096 bytes (or a lower `CK_EVENTS_MAX_BYTES`) is 413 **before** the token is checked. Exactly 4096 is accepted.
- A repeated id, including one already evicted from the ring, returns `{"accepted": true, "duplicate": true, "id": "..."}` and does not append or replace. The route does not move mood.
- `GET /health` also returns `mode`, `mood`, `relay`, and `events`. Those change every read. The relay still fingerprints only `status`, `version`, and `mood`, so a ticking relay counter cannot repost health.

The relay also sends `Idempotency-Key` set to the Avrone entry id (the same string as the JSON `id`). PR #14 dedupes on that id and ignores the header. The header is there so a retry of the same entry is one key even for a proxy that never looks inside the body.

### Required body (what PR #14 checks)

```
POST /v1/events
Content-Type: application/json
X-Admin-Token: <token>
```

```json
{
  "id": "find0000001",
  "source": "avrone",
  "kind": "finding",
  "run_id": "run-1",
  "payload": {
    "id": "find0000001",
    "agent": "scout",
    "role": "agent",
    "kind": "finding",
    "text": "heat on the cone",
    "refs": [],
    "origin_node": "local"
  },
  "ts": 1700000000.0
}
```

| Field | Rule |
| --- | --- |
| `id` | Required. Id pattern. Dedup key. |
| `source` | Required. `avrone`, `cyclekernal`, or `greatscale`. |
| `kind` | Required. A blackboard kind, or `mood`, `evidence`, `health`. |
| `run_id` | Required non-empty string, clipped to 80 characters. |
| `payload` | Required JSON object. The relay puts the clipped post here, not the raw upstream row. |
| `ts` | Finite number greater than 0, else the server clock. |

HTTP 200, first time:

```json
{"accepted": true, "duplicate": false, "id": "find0000001"}
```

HTTP 200, same id again. Do not append a second copy and do not apply the payload twice:

```json
{"accepted": true, "duplicate": true, "id": "find0000001"}
```

| Failure | Status |
| --- | --- |
| Body over 4096 bytes | 413, not stored |
| Bad or missing token, when a token is configured | 401 |
| Token not configured, so the route is off | 403 |
| Malformed JSON, non-object, missing field, bad id, bad source, bad kind, non-object payload | 400 `{"detail":"..."}` |

`GET /v1/events` is **not** required. The local stand-in in this repo exposes it for debugging only.

This route stores and acknowledges the event. It does not have to move `mood` or `traits`. Those stay on the LoopMem path that already feeds `/v1/current_mood`.

## What the relay does with the routes

One cycle, in order:

1. `GET /api/blackboard`. Forward each new post with `POST /v1/events` and `Idempotency-Key: <that post's id>`. Skip the relay's own mirrors: `origin_node` `fusion-bridge`, `role` `cyclekernal-relay`, agent `cyclekernal:*` (any kind after the colon, case-insensitive), ids minted as `ck-health-` / `ck-mood-` / `ck-evidence-`, or text that is the mirror JSON with that agent embedded. Health mirroring must not be posted back, or CycleKernal's `events` counter on `/health` would churn a new mirror every cycle.
2. `GET /v1/current_mood`. If the stable projection changed, `POST /api/blackboard` a conclusion whose text includes `"agent":"cyclekernal:mood"`.
3. `GET /health`. If the stable projection changed, `POST /api/blackboard` a finding whose text includes `"agent":"cyclekernal:health"`.

Each cycle has a deadline (`FUSION_CYCLE_DEADLINE`, default 10s). A request uses `min(FUSION_TIMEOUT, time left in the cycle)` and a cycle will not start a call with less than 50ms left. A host that times out or refuses the connection opens a circuit after `FUSION_BREAKER_THRESHOLD` failures (default 1). Further calls to that same origin are not sent until `FUSION_BREAKER_COOLDOWN` (default 30s); then one probe is allowed. A 500 is not a black hole and does not open the circuit. This is what stops a CycleKernal that accepts the TCP connection and never answers from costing a full timeout per queued event (a live run paid about 15s per event, 215s for 21 events).

An id is remembered only after a 2xx, or after a permanent 4xx (400, 413, other non-auth 4xx). Timeouts, connection failures, 401/403, 404/405, and 5xx leave the id unseen so a later cycle retries it. A transport failure backs off from `FUSION_BACKOFF_BASE` (default 0.5s), doubling to `FUSION_BACKOFF_MAX` (default 8s), and the multiplier resets after a cycle with no transport failure. 404 and 500 do not use that backoff; they wait for `FUSION_INTERVAL`.

`GET /health` (and Avrone `GET /api/status`, and `GET /api/blackboard`) include `boot`: a string or integer epoch that stays constant until that process restarts and changes when it does. The first value the relay sees is recorded and does not cause a resend. A later change means that peer lost its memory: the relay forgets ids it delivered **to that peer** and sends them again. CycleKernal's boot clears forwarded Avrone entry ids (mood and health mirror ids stay). Avrone's boot clears `ck-mood-` / `ck-health-` / `ck-evidence-` ids (forwarded findings stay). A missing `boot` leaves the saved state alone, so an older peer does not look like a restart on every poll.

`FUSION_STATE_PATH` persists that as `{"version":2,"seen":[...],"cursor":"...","boots":{"avrone":"...","cyclekernal":"..."}}`. A version-1 file (no `boots`) still loads. A missing file starts empty. A corrupt file is discarded (`state_errors`). Deleting the file re-sends; a peer that still has the id answers `duplicate: true`. A peer that restarted does not, which is why `boot` exists.

Direction:

| From | To | Why |
| --- | --- | --- |
| Avrone board post | `POST /v1/events` | Findings CycleKernal never saw. |
| `GET /v1/current_mood` | `POST /api/blackboard` | Mood that never reached Avrone. |
| `GET /health` | `POST /api/blackboard` | Coarse liveness on the same board. |

## Reference behavior covered by `tests/`

`tests/relay_stubs.py` is a small implementation of this document. `pytest tests/` runs the relay against it. The cases are: happy path and echo suppression, dedup, malformed entries, bad kind coerced only on the client, 4 KB cap, 413 before auth, bad token not acked, one side authorized and the other not, HTTP 500 then recovery, permanent 413, malformed JSON, mood change versus a ticking health clock, process restart, corrupt state, peer down then up, dead port with backoff, and a read timeout that still mirrors the other side.

## Out of scope for the other repos' first patch

- Opening `/ws/events` or `/ws/hud` from this relay. Polling is the contract.
- Changing Avrone's 16 MB process body cap.
- Driving CognitiveBridge mood from Avrone events.
- A greatscale producer. `source: "greatscale"` and `kind: "evidence"` are reserved so a later producer can post `/v1/events` without a second schema. This repository does not emit those.
