# GreatScale-Avrone-Grok-Fusion-Bridge

Real bridge between Avrone's agent blackboard and CycleKernal's CognitiveBridge.
No fiction — every file here runs.

## Components

- **avrone_cyclekernal_link.py** — event schema + HTTP clients + relay loop (fails soft on any dead hop)
- **cognitive_bridge_standalone.py** — FastAPI CognitiveBridge on port 8090 (trait updates: mood, evidence, findings + decay)
- **avrone_shim.py** — Flask blackboard shim on port 8710 (mirrors Avrone's real interface; swap for the Docker container later)
- **test_integration.py** — starts both daemons, posts one finding, asserts the trait nudge, tears down

## Run

```bash
pip install -r requirements.txt
python cognitive_bridge_standalone.py   # terminal 1
python avrone_shim.py                   # terminal 2
python test_integration.py              # proves the loop
```

## The gap this closes

1. Avrone posts findings; CycleKernal never saw them → relay forwards them
2. CycleKernal's mood stream never reached Avrone → health mirrored back
3. greatscale evidence snapshots posted to nothing → same event schema catches them
