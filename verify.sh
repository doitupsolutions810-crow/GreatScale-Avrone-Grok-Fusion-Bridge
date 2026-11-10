#!/bin/sh
# NEW HARNESS. This file did not exist in the repository.
# It is not the missing original verify.sh.
# Original heat/cone evidence at artifacts/fusion-bridge was not in the tree.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
cd "$ROOT"
mkdir -p artifacts/fusion-bridge
{
  echo "label=new-harness"
  echo "not_the_missing_original=true"
  echo "original_verify_sh=absent"
  echo "original_drill_py=absent"
  echo "original_artifacts_fusion_bridge=absent"
  date -u +%Y-%m-%dT%H:%M:%SZ
  if docker info >/dev/null 2>&1; then
    echo "docker=up"
  else
    echo "docker=down"
    echo "verification_mode=host-only"
  fi
} | tee artifacts/fusion-bridge/verify-host.txt

python3 -m pytest tests -q --tb=line | tee artifacts/fusion-bridge/pytest.txt
python3 drill.py --soak-seconds 60 --out artifacts/fusion-bridge

if docker info >/dev/null 2>&1; then
  echo "docker container run started" | tee artifacts/fusion-bridge/docker.txt
  docker run --rm -v "$ROOT":/src -w /src python:3.12-slim \
    python3 -m pytest tests/test_round5.py tests/test_round6.py -q --tb=line \
    | tee -a artifacts/fusion-bridge/docker.txt
else
  echo "DOCKER_DOWN: container verification was not run. Results above are host-only." \
    | tee artifacts/fusion-bridge/docker.txt
fi
