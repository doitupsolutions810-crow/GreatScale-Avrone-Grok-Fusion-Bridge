# Authenticated AVRONE → CycleKernal Fusion Relay (draft integration)

This module replaces the previous placeholder with a bounded, credential-gated HTTP relay and a separate Great Scale arithmetic evidence adapter. It is **not deployed or background-running**. The upstream API endpoints are currently in open pull requests, not the default branches:

- AVRONE [blackboard PR #20](https://github.com/doitupsolutions810-crow/Avrone/pull/20): `GET/POST /api/blackboard`, `X-Admin-Token`, reply `{posts,count,boot}` / `{posted,duplicate,id}`.
- CycleKernal [events PR #14](https://github.com/doitupsolutions810-crow/CycleKernal/pull/14): `POST /v1/events`, `X-Admin-Token`, reply `{accepted,duplicate,id}` and `{boot}` on `GET /health`.

The relay accepts only explicitly configured origins. HTTP is restricted to loopback; remote URLs must use HTTPS. Credentials are read from the operator environment and never printed. Redirects are not followed. Each call has a 3-second deadline, 4096-byte write cap and 1 MiB read cap. Unknown/duplicate-key/nonfinite JSON and incompatible envelopes are refused. AVRONE posts are clipped to 1800 UTF-8 bytes with the original text digest and a `truncated` flag. Up to 1000 posts are processed per snapshot; acknowledged local dedup is memory-bounded.

A post counts as **forwarded** only if CycleKernal replies with `accepted: true` and the exact original ID. A lost acknowledgement is retried; downstream durable `(source,id)` idempotency is therefore a prerequisite. Relay restart or AVRONE `boot` changes may replay posts. Delivery can still miss posts evicted from AVRONE's bounded ring while the relay is offline, so this is not guaranteed exactly-once or lossless transport. No CycleKernal → AVRONE feedback is emitted automatically, preventing a feedback loop. Explicit `post_to_avrone_bridge` is available only with a token and maps non-blackboard kinds to `conclusion`.

## Local contract tests (no live traffic)

```bash
python3 -m pip install pytest
python3 -m pytest -q tests
```

## Operator-approved live run (only after both upstream PRs are deployed)

```bash
export AVRONE_BASE=http://127.0.0.1:8710
export CK_BASE=http://127.0.0.1:8090
export AVRONE_ADMIN_TOKEN='set-in-secret-manager'
export CK_FUSION_TOKEN='set-in-secret-manager'
export FUSION_INTEGRATION=1
cd bridge
python3 test_integration.py
```

The opt-in script does **not** launch local unauthenticated shims. For longer-running relay missions the operator can call `relay_loop(interval=1.0, max_cycles=None)` under a managed service with bounded per-request timeouts and explicit cancellation; that service was **not started by this PR**. The prior `avrone_shim.py` and `cognitive_bridge_standalone.py` are legacy demos, not proof that production endpoints are connected. The legacy `docker-compose.avrone.yml` references absent build directories and remains unsuitable for production until separate deployment wiring is reviewed.

## Great Scale adapter

`greatscale_evidence.greatscale_event(...)` validates a producer-reported QGT tuple `(C,A_Q,Ψ_T)` against `A_Q >= |C|` and `Ψ_T = φ⁻² + [1-(C/A_Q)²]/4`; it keeps signed polarization `C/A_Q`. Its event explicitly marks source commit verification, artifact digest verification, authorization and executed receipt verification as `false`. It does **not** turn a numerical claim into an execution or cryptographic proof. This adapter is pure and does not send HTTP automatically.
