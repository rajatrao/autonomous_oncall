#!/usr/bin/env bash
# Build everything locally: the self-contained agent definition and the container images.
#   ./scripts/build.sh            agent YAML + validation + images
#   ./scripts/build.sh agent      agent YAML + validation only
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
DOCKER_AGENT=${DOCKER_AGENT:-}
if [[ -z $DOCKER_AGENT ]]; then
  if command -v docker-agent >/dev/null; then DOCKER_AGENT=docker-agent
  elif docker agent version >/dev/null 2>&1; then DOCKER_AGENT="docker agent"
  fi
fi

$PY scripts/build_agent.py
if [[ -n $DOCKER_AGENT ]]; then
  # Resolve the config exactly as Docker Agent will (defaults applied, flavors checked).
  ONCALL_SCRIPTED_MODEL_URL=http://127.0.0.1:7199/v1 $DOCKER_AGENT debug config dist/oncall-agent.yaml >/dev/null
  for flavor in scripted-model anthropic openai openai-compatible dmr ollama; do
    ONCALL_SCRIPTED_MODEL_URL=http://127.0.0.1:7199/v1 ONCALL_REASONING_MODEL=r ONCALL_FAST_MODEL=f \
    ONCALL_MODEL_BASE_URL=http://127.0.0.1:1/v1 ONCALL_OLLAMA_URL=http://127.0.0.1:11434/v1 \
      $DOCKER_AGENT debug config dist/oncall-agent.yaml "$flavor" >/dev/null
  done
  echo "dist/oncall-agent.yaml validated with: $DOCKER_AGENT debug config"
else
  echo "Docker Agent not found; skipped validation (install: https://docs.docker.com/ai/docker-agent/)" >&2
fi
[[ ${1:-all} == agent ]] && exit 0
docker compose build
