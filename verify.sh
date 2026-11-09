#!/bin/sh
# Default entry: the whole fusion drill, with no suite-level timeout.
# Per-request timeouts stay inside the relay. When Docker works, the same
# drill runs in the test-runner service. Otherwise the mock stack runs here.
set -eu
cd "$(dirname "$0")"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "DOCKER: real — verify.sh inside the test-runner container"
  docker compose -f docker-compose.hardened.yml build test-runner
  docker compose -f docker-compose.hardened.yml run --rm --no-deps test-runner
  exit $?
fi
echo "DOCKER: unavailable — mock equivalents (partition, DNS, toxiproxy, rootfs, image pull)"
exec python3 -m bridge.watchdog -- python3 -m bridge.drill
