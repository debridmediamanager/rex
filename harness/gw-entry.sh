#!/bin/bash
# Egress gateway for the PMS container, which shares this network namespace.
#
# The OUTPUT chain, not PREROUTING: PMS's connections originate inside this very
# namespace, so they never traverse PREROUTING. uid 8888 (mitmgw) is excluded or
# the proxy's own upstream connections would be redirected back into itself --
# which is also why mitmproxy runs as 8888 and not the image default 1000, the
# uid PMS itself runs as.
#
# Never flush this chain. Docker's embedded DNS (127.0.0.11) is implemented as
# DNAT rules in nat/OUTPUT inside the container namespace; flushing it leaves
# every name unresolvable while TCP to a literal IP still works, which reads as
# a proxy fault and is not one.
set -e
UID_MITM=$(id -u mitmgw)
# Only the confdir. Chowning /capture recursively takes the whole
# capture tree away from the host user and every other writer.
mkdir -p /gwcerts && chown -R "$UID_MITM" /gwcerts
touch /capture/outbound.jsonl && chown "$UID_MITM" /capture/outbound.jsonl

for port in 80 443; do
  iptables -t nat -C OUTPUT -p tcp --dport $port \
      -m owner ! --uid-owner "$UID_MITM" -j REDIRECT --to-ports 8080 2>/dev/null \
    || iptables -t nat -A OUTPUT -p tcp --dport $port \
      -m owner ! --uid-owner "$UID_MITM" -j REDIRECT --to-ports 8080
done
echo "[gw] outbound 80/443 -> :8080, excluding uid $UID_MITM (mitmgw)"
getent hosts plex.tv >/dev/null && echo "[gw] DNS ok" || echo "[gw] WARNING: DNS broken"

exec setpriv --reuid="$UID_MITM" --regid="$UID_MITM" --clear-groups \
  mitmdump --mode transparent \
    --listen-host 0.0.0.0 --listen-port 8080 \
    --set confdir=/gwcerts \
    --set stream_large_bodies=1m \
    --ssl-insecure --showhost \
    -s /scripts/tap.py --set taplog=/capture/outbound.jsonl
