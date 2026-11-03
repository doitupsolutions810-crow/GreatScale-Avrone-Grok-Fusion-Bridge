# Bridge package

`avrone_cyclekernal_link.py` is the relay. It reads `AVRONE_BASE` (default port **8710**) and `CK_BASE` (default port **8090**) on every call.

`avrone_shim.py` and `cognitive_bridge_standalone.py` are local stand-ins for the routes in `../CONTRACT.md`. Avrone does not serve `/api/blackboard`. CycleKernal does not serve `/v1/events`. The stand-ins do, so the loop can be run without those patches.

`endpoint_resolver.py` probes Avrone at `GET /api/status` and CycleKernal at `GET /health`. A miss returns unreachable. It does not invent a second copy of the same URL.

## Run from the repo root

```bash
pip install -r requirements.txt
pytest tests/ -v

pip install -r bridge/requirements.txt
python -m bridge.cognitive_bridge_standalone
python -m bridge.avrone_shim
python -m bridge.avrone_cyclekernal_link
python bridge/test_integration.py
```

Set `FUSION_TOKEN` on all three processes when you want the stand-ins to require `X-Admin-Token`.
