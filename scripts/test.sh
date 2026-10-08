#!/usr/bin/env bash
# Run the test suite.
#   ./scripts/test.sh              unit, contract, safety and gateway tests
#   ./scripts/test.sh --agent      also the agent-behaviour tests that drive the real docker-agent
#                                  binary against mocked tools and the scripted model
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
$PY scripts/build_agent.py --check
if [[ ${1:-} == --agent ]]; then
  shift
  $PY -m pytest "$@"
else
  $PY -m pytest -m "not docker_agent" "$@"
fi
