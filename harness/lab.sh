#!/usr/bin/env bash
# plexlab control script.
#
# Start order is not cosmetic. Two containers share another's network namespace
# (plex -> gw, cdp-relay -> browser), and a namespace-sharing container that
# starts before its host -- or survives its host being recreated -- ends up in a
# namespace that no longer exists. It reports "Up", listens on nothing, and every
# connection to it is refused, which reads exactly like a firewall problem.
set -euo pipefail
cd "$(dirname "$0")"

LAB=$(pwd)
PLEX=http://127.0.0.1:32400

wait_plex() {
  for _ in $(seq 1 40); do
    curl -sf --max-time 3 "$PLEX/identity" >/dev/null 2>&1 && return 0
    sleep 3
  done
  echo "plex did not come up" >&2; return 1
}

install_ca() {
  # The PMS container trusts the gateway CA from its image filesystem, not a
  # volume, so a recreate loses it and every plex.tv call fails TLS -- which
  # signs the server out and drops its claim. Re-apply on every start.
  docker exec -u root plexlab-plex sh -c \
    'cp /gwcerts/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/plexlab-gw.crt \
     && update-ca-certificates' >/dev/null 2>&1 && echo "  gateway CA installed in plex"
}

case "${1:-help}" in
  up)
    docker compose up -d gw
    sleep 8
    docker compose up -d plex            # after gw: shares its netns
    wait_plex
    install_ca
    docker compose restart plex >/dev/null; wait_plex
    docker compose up -d mitm-client
    docker compose up -d browser
    for _ in $(seq 1 40); do
      docker exec plexlab-browser ss -ltn 2>/dev/null | grep -q 9222 && break; sleep 3
    done
    docker compose up -d cdp-relay       # after browser: shares its netns
    sleep 4
    "$0" status
    ;;
  down)    docker compose down ;;
  restart) "$0" down; "$0" up ;;
  status)
    docker compose ps --format '  {{.Name}}\t{{.Status}}'
    echo "  identity : $(curl -s --max-time 4 $PLEX/identity | grep -o 'claimed="[01]"' || echo unreachable)"
    echo "  cdp      : $(curl -s --max-time 4 http://127.0.0.1:9224/json/version | head -c 60 || echo unreachable)"
    echo "  mitmweb  : http://$(hostname -I | awk '{print $2}'):9081  (password plexlab)"
    echo "  desktop  : http://$(hostname -I | awk '{print $2}'):7902"
    ;;
  logs)    shift; docker compose logs -f "$@" ;;
  tail)    # live view of both capture axes
    tail -f capture/client.jsonl capture/outbound.jsonl ;;
  *)
    cat <<EOF
usage: ./lab.sh {up|down|restart|status|logs|tail}

  up       start everything in the order the shared namespaces require
  status   container states, claim state, CDP and UI endpoints
  tail     follow both capture files

capture a client session:
  .venv/bin/python scripts/session-web.py --label <name> \\
      --token <plex.tv account token> --server-token <server token> \\
      --machine <machineIdentifier>

diff a server against a captured corpus:
  .venv/bin/python scripts/replay.py --corpus capture/sessions/<name>.jsonl \\
      --target http://127.0.0.1:32500 --target-token <tok> \\
      --reference $PLEX --reference-token <tok> --out capture/diffs/<name>.md
EOF
    ;;
esac
