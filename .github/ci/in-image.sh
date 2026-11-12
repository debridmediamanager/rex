#!/usr/bin/env bash
# Runs a command inside the CI toolchain image on the run's build VM (ci.yml), as the runner's
# own user, in the checkout. The VM's snapshot holds the image, and one for a changed Dockerfile
# is built here. The cargo registry and target dir live outside the checkout, which
# actions/checkout cleans, and the snapshot carries a warm copy, so builds stay incremental.
set -euo pipefail
ws=$(pwd)
image="rex-ci:$(sha256sum "$ws/.github/ci/Dockerfile" | cut -c1-12)"
cache=/home/ben/rex-ci-cache
mkdir -p "$cache/cargo" "$cache/target"
docker image inspect "$image" >/dev/null 2>&1 \
  || docker build -q -t "$image" -f "$ws/.github/ci/Dockerfile" "$ws/.github/ci" >/dev/null
# Uncapped: the VM is the run's alone. It was 6 CPUs and 16 GB while it shared dmm with the
# production database.
exec docker run --rm --init \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e CARGO_HOME="$cache/cargo" -e CARGO_TARGET_DIR="$cache/target" -e CARGO_TERM_COLOR \
  -e REX_PLEXTV_URL -e JELLYFIN_FFMPEG -e ZURG_CONTRACT -e REX_TEST_PORTS \
  -v "$cache:$cache" -v "$ws:$ws" -w "$ws" \
  "$image" bash -euo pipefail -c "$*"
