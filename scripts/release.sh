#!/usr/bin/env bash
# Release: version-check, build, validate, then publish the agent to an OCI registry with
# `docker agent share push` and the product images with `docker buildx`.
#
#   REGISTRY=docker.io/myorg ./scripts/release.sh 0.1.0 [--push]
#
# Optional: SIGNING_KEY=file://path/to/ed25519   sign the agent artifact (DSSE / in-toto)
#           PLATFORMS=linux/amd64,linux/arm64      image platforms
# Without --push it does everything except publishing (a dry run).
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${1:?usage: release.sh <semver> [--push]}
PUSH=${2:-}
REGISTRY=${REGISTRY:?set REGISTRY, e.g. docker.io/myorg}
PLATFORMS=${PLATFORMS:-linux/amd64,linux/arm64}
DOCKER_AGENT=${DOCKER_AGENT:-docker agent}

[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || { echo "not a semver: $VERSION" >&2; exit 1; }
[[ $(cat VERSION) == "$VERSION" ]] || { echo "VERSION file says $(cat VERSION), not $VERSION" >&2; exit 1; }
grep -q "^## \[$VERSION\]" CHANGELOG.md || { echo "CHANGELOG.md has no entry for $VERSION" >&2; exit 1; }

./scripts/test.sh
./scripts/build.sh agent

AGENT_REF="$REGISTRY/oncall-agent:$VERSION"
MINOR_REF="$REGISTRY/oncall-agent:${VERSION%.*}"
KEY_ARGS=()
[[ -n ${SIGNING_KEY:-} ]] && KEY_ARGS=(--key "$SIGNING_KEY")

for target in gateway mcp; do
  args=(docker buildx build --platform "$PLATFORMS" --target "$target"
        --label "org.opencontainers.image.version=$VERSION"
        --label "org.opencontainers.image.source=https://github.com/rajatrao/oncall"
        -t "$REGISTRY/oncall-$target:$VERSION" .)
  if [[ $PUSH == --push ]]; then "${args[@]}" --push; else "${args[@]}"; fi
done

if [[ $PUSH == --push ]]; then
  $DOCKER_AGENT share push dist/oncall-agent.yaml "$AGENT_REF" "${KEY_ARGS[@]}"
  $DOCKER_AGENT share push dist/oncall-agent.yaml "$MINOR_REF" "${KEY_ARGS[@]}"
  echo "Published $AGENT_REF (and $MINOR_REF)."
  echo "Run it:   docker agent run $AGENT_REF"
  echo "Serve it: docker agent serve api $AGENT_REF --pull-interval 10"
else
  echo "Dry run complete. Would push $AGENT_REF, $MINOR_REF and $REGISTRY/oncall-{gateway,mcp}:$VERSION"
fi
