# GreatScale-Avrone-Grok-Fusion-Bridge

Real wiring between three systems that previously never called each other:

| System | Role | Port |
| --- | --- | --- |
| Avrone | Agent blackboard + fleet relay | 8080 |
| CycleKernal | CognitiveBridge (FastAPI) | 8090 |
| greatscale.py | Residual math (heat, cone, evidence) | n/a |

## The three gaps this closes

1. **Avrone findings never reached CycleKernal** — `post_to_cyclekernal` forwards blackboard entries to `/api/events`.
2. **CycleKernal mood stream never reached Avrone** — `post_to_avrone_bridge` maps mood/evidence into Avrone's blackboard as relayed conclusions.
3. **greatscale evidence snapshots posted to nothing** — health mirroring keeps Avrone's telemetry current with CycleKernal's status.

## Quick start

```bash
pip install -r requirements.txt
python -m bridge.avrone_cyclekernal_link   # runs 3 relay cycles
pytest tests/ -v
```

## Design rules

- One event schema (`FusionEvent`) across both systems.
- Soft-fail per hop: one dead endpoint never crashes the relay.
- Dedup by event ID so the blackboard is not re-forwarded.
- Payload text capped at 4 KB to respect Avrone's body limits.
