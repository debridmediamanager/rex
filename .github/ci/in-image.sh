#!/usr/bin/env bash
# Runs a command inside the CI toolchain image on dmm, as the runner's own user, in the
# checkout. Builds the image when it is missing: zurg's nightly prunes every unused image.
# The cargo registry and target dir live outside the checkout, which actions/checkout cleans,
# so builds stay incremental from run to run.
set -euo pipefail
ws=$(pwd)
image="rex-ci:$(sha256sum "$ws/.github/ci/Dockerfile" | cut -c1-12)"
cache=/home/ben/rex-ci-cache
mkdir -p "$cache/cargo" "$cache/target"
docker image inspect "$image" >/dev/null 2>&1 \
  || docker build -q -t "$image" -f "$ws/.github/ci/Dockerfile" "$ws/.github/ci" >/dev/null
# Capped so a build cannot starve the database dmm also runs.
exec docker run --rm --init --cpus 6 --memory 16g \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e CARGO_HOME="$cache/cargo" -e CARGO_TARGET_DIR="$cache/target" -e CARGO_TERM_COLOR \
  -e REX_PLEXTV_URL -e JELLYFIN_FFMPEG -e ZURG_CONTRACT -e REX_TEST_PORTS \
  -v "$cache:$cache" -v "$ws:$ws" -w "$ws" \
  "$image" bash -euo pipefail -c "$*"
