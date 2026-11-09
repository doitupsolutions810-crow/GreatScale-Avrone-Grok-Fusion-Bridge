# GreatScale Avrone Grok Fusion Bridge

Relay between Avrone's blackboard on port **8710** and CycleKernal's CognitiveBridge on port **8090**. Port 8080 is CycleKernal's frontend container, not Avrone.

Avrone speaks `GET/POST /api/blackboard`, `GET /api/status`, and `X-Admin-Token`. It has no `/health`. CycleKernal speaks `POST /v1/events` and `GET /health`. Bodies over 4096 bytes are rejected with 413 before auth. Posts send `Idempotency-Key`.

```bash
./verify.sh
```

`verify.sh` runs `bridge/drill.py` under `bridge/watchdog.py`. There is no suite timeout. If Docker is running, that same drill runs in the `test-runner` service from `docker-compose.hardened.yml`. This environment has no Docker daemon, so the drill uses mock peers for the partition, DNS, toxiproxy, read-only root, and image-pull rounds and says so in `ROUND6_SUMMARY.md`.

Do not run two relays against one blackboard. The process entrypoint takes `FUSION_RELAY_LOCK`.
