#!/usr/bin/env bash
# plexscratch control script. Run it from its own directory on the lab host.
#
#   ./scratch.sh make     copy the lab's config ONCE into ./pristine, sanitised
#   ./scratch.sh up       start from ./config (created from ./pristine if absent)
#   ./scratch.sh reset    throw ./config away and start again from ./pristine
#   ./scratch.sh status   containers, identity, and proof it cannot reach plex.tv
#   ./scratch.sh token    the token clients send (the copy's own .LocalAdminToken)
#   ./scratch.sh down     stop
#
# `make` is the only command that reads the lab, and it writes nothing there: it
# pauses the lab's PMS for the seconds the copy takes -- the database is in WAL mode
# and PMS writes it continuously, so an unpaused copy can be inconsistent -- and
# unpauses it on every exit path.
set -euo pipefail
cd "$(dirname "$0")"

LAB=${PLEXLAB_DIR:-../plexlab}
PMS="Library/Application Support/Plex Media Server"
IMAGE=plexinc/pms-docker@sha256:e0ab27395614a8e1a4fdf84c6bc60ac664915cfdde70c52d030c7728a1c48e14
BIND=${SCRATCH_BIND:-100.85.222.1}
PORT=${SCRATCH_PORT:-32410}
URL="http://$BIND:$PORT"

wait_up() {
  for _ in $(seq 1 60); do
    curl -sf --max-time 3 "$URL/identity" >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "plexscratch did not come up" >&2; return 1
}

sqlite() {
  docker run --rm --user 1000:1000 --network none \
    --entrypoint "/usr/lib/plexmediaserver/Plex SQLite" \
    -v "$PWD/pristine/config:/config" "$IMAGE" "$@"
}

case "${1:-help}" in
  make)
    if [ -e pristine ]; then
      echo "pristine/ exists; remove it to take a new copy of the lab" >&2; exit 1
    fi
    mkdir -p pristine/config transcode
    trap 'docker unpause plexlab-plex >/dev/null 2>&1 || true' EXIT
    docker pause plexlab-plex >/dev/null
    # Logs hold the plex.tv token and certificate id; Cache holds the encrypted
    # cloud-account state and the plex.direct certificate; the rest is per-machine.
    tar -C "$LAB/config" --anchored \
        --exclude="./$PMS/Logs" --exclude="./$PMS/Cache" \
        --exclude="./$PMS/Crash Reports" --exclude="./$PMS/Diagnostics" \
        --exclude="./$PMS/.LocalAdminToken" --exclude="./$PMS/Setup Plex.html" \
        --exclude="./$PMS/plexmediaserver.pid" \
        -cf - . | tar -C pristine/config -xf -
    docker unpause plexlab-plex >/dev/null
    trap - EXIT
    echo "  copied (lab paused for the copy only)"

    python3 - "$LAB/config/$PMS/Preferences.xml" "pristine/config/$PMS/Preferences.xml" <<'PY'
import hashlib, os, re, sys, uuid
lab, copy = sys.argv[1], sys.argv[2]
old = open(lab).read()
secrets = [m.group(1).encode() for key in ("PlexOnlineToken", "CertificateUUID",
                                           "ProcessedMachineIdentifier", "MachineIdentifier",
                                           "AnonymousMachineIdentifier")
           for m in [re.search(r'(?<![A-Za-z])%s="([^"]+)"' % key, old)] if m]
text = open(copy).read()
for key in ("PlexOnlineToken", "PlexOnlineUsername", "PlexOnlineMail", "PlexOnlineHome",
            "CertificateUUID", "CertificateVersion", "PubSubServerPing"):
    text = re.sub(r'\s%s="[^"]*"' % key, "", text)
machine = str(uuid.uuid4())
text = re.sub(r'(?<![A-Za-z])MachineIdentifier="[^"]*"', 'MachineIdentifier="%s"' % machine, text)
text = re.sub(r'ProcessedMachineIdentifier="[^"]*"',
              'ProcessedMachineIdentifier="%s"' % hashlib.sha1(machine.encode()).hexdigest(), text)
text = re.sub(r'AnonymousMachineIdentifier="[^"]*"',
              'AnonymousMachineIdentifier="%s"' % uuid.uuid4(), text)
text = re.sub(r'FriendlyName="[^"]*"', 'FriendlyName="plexscratch"', text)
open(copy, "w").write(text)
# The token and the certificate id must not survive ANYWHERE in the copy; the old
# machine id may (the database names past devices), which is harmless offline.
leaks = []
for root, _, files in os.walk(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(copy))))):
    for name in files:
        data = open(os.path.join(root, name), "rb").read()
        if any(secret in data for secret in secrets[:2]):
            leaks.append(os.path.join(root, name))
if leaks:
    sys.exit("token or certificate id survived in: %s" % leaks)
print("  sanitised: token, account and certificate removed; new machine identifiers")
PY

    DB="/config/$PMS/Plug-in Support/Databases"
    for db in com.plexapp.plugins.library.db com.plexapp.plugins.library.blobs.db; do
      result=$(sqlite "$DB/$db" "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;" | tail -1)
      echo "  $db: $result"
      [ "$result" = "ok" ] || { echo "integrity check failed" >&2; exit 1; }
    done
    ;;
  up)
    [ -e pristine/config ] || { echo "run ./scratch.sh make first" >&2; exit 1; }
    [ -e config ] || cp -a pristine/config config
    mkdir -p transcode
    # --force-recreate keeps the namespace-sharing hop inside the CURRENT plex container.
    docker compose up -d --force-recreate
    wait_up
    "$0" status
    ;;
  reset)
    docker compose down >/dev/null 2>&1 || true
    rm -rf config transcode
    "$0" up
    ;;
  down)
    docker compose down
    ;;
  token)
    cat "config/$PMS/.LocalAdminToken"
    ;;
  status)
    docker compose ps --format '  {{.Name}}\t{{.Status}}'
    echo "  identity : $(curl -s --max-time 4 "$URL/identity" | tr -d '\n' | grep -o '<MediaContainer[^>]*>' || echo unreachable)"
    echo "  sections : HTTP $(curl -s --max-time 6 -o /dev/null -w '%{http_code}' \
        -H "X-Plex-Token: $("$0" token)" "$URL/library/sections") with the token"
    if docker exec plexscratch-plex sh -c 'curl -s -m 5 -o /dev/null https://plex.tv' 2>/dev/null; then
      echo "  plex.tv  : REACHABLE -- the isolation is broken, stop it now" >&2
      exit 1
    fi
    echo "  plex.tv  : unreachable (isolated)"
    echo "  api      : $URL"
    ;;
  *)
    sed -n 2,15p "$0"
    ;;
esac
