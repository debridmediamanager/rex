#!/usr/bin/env python3
"""An unclaimed rex with no `--token` serves no client, only its claim, black-box.

Measured on plexscratch, a never-claimed PMS 1.43.4, on 2026-10-13 (findings, "The gate an
unclaimed server keeps"): `/identity`, `/web`, `/manage`, `/security/token` and the three
static files stay open; everything else needs a peer and every `X-Forwarded-For` hop on
loopback or, with `allowedNetworks` empty, a private network (with it set, in it), and a
`Host` naming localhost or the address the request arrived on. rex answered everyone.

Since 2026-10-19 rex keeps that gate for its claim alone: with no claim it has no token to ask
Plex's agent with, so it refuses every client route, loopback included. `--assert-claimed`,
the suites' stand-in for a claim, keeps the measured gate for every route.

    python3 tests/test_unclaimed_gate.py
"""
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(HERE, "catalog_ui.json")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- %r" % (detail,)))


def own_address():
    """This machine's non-loopback IPv4 address, or None: a UDP connect sends nothing."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


def status(address, port, path, method="GET", headers=None):
    conn = http.client.HTTPConnection(address, port, timeout=15)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def start(port, *args):
    log = tempfile.TemporaryFile()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for name in ("REX_TOKEN", "PLEX_TOKEN", "REX_STATE"):
        env.pop(name, None)
    proc = subprocess.Popen(server_command("--port", port, "--catalog", CATALOG, "--gdm", "off",
                                           *args, root=ROOT),
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(150):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc, log
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    raise SystemExit("rex did not start")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def unclaimed(port):
    """A plain unclaimed server: its claim through the measured gate, no client route at all."""
    local = "127.0.0.1"
    for path in ("/library/sections", "/:/prefs", "/status/sessions", "/hubs"):
        check("unclaimed, loopback is refused %s" % path, status(local, port, path) == 401)
    check("unclaimed, a forwarded private client is refused the library",
          status(local, port, "/library/sections", headers={"X-Forwarded-For": "192.168.1.5"}) == 401)
    for path, wanted in (("/identity", 200), ("/crossdomain.xml", 200), ("/rex/sources", 200)):
        got = status(local, port, path)
        check("unclaimed, %s stays open" % path, got == wanted, got)
    # No token: the claim route's own 400 once the gate lets it through, 401 when it does not.
    claim = "/myplex/claim"
    check("unclaimed, loopback reaches the claim", status(local, port, claim, method="POST") == 400)
    check("unclaimed, a localhost Host reaches the claim",
          status(local, port, claim, method="POST", headers={"Host": "localhost:%d" % port}) == 400)
    check("unclaimed, a forwarded private client reaches the claim",
          status(local, port, claim, method="POST",
                 headers={"X-Forwarded-For": "192.168.1.5, 10.1.2.3, fd00::5"}) == 400)
    for hop in ("8.8.8.8", "100.85.1.1", "192.168.1.5, 8.8.8.8"):
        check("unclaimed, a forwarded %s cannot claim" % hop,
              status(local, port, claim, method="POST", headers={"X-Forwarded-For": hop}) == 401)
    check("unclaimed, a Host naming evil.example cannot claim",
          status(local, port, claim, method="POST", headers={"Host": "evil.example:%d" % port}) == 401)
    address = own_address()
    if address is None:
        print("SKIP the non-loopback claim check: no non-loopback IPv4 address")
    else:
        private = any(address.startswith(prefix) for prefix in ("10.", "192.168.")) or (
            address.startswith("172.") and 16 <= int(address.split(".")[1]) <= 31)
        got = status(address, port, claim, method="POST")
        check("unclaimed, a peer at %s %s the claim" % (address, "reaches" if private else "is refused"),
              got == (400 if private else 401), got)
        got = status(address, port, "/library/sections")
        check("unclaimed, a peer at %s is refused the library" % address, got == 401, got)


def asserted(port):
    """`--assert-claimed`: the measured unclaimed gate, on every route."""
    local = "127.0.0.1"
    check("loopback reads the library", status(local, port, "/library/sections") == 200)
    check("a localhost Host is local",
          status(local, port, "/library/sections", headers={"Host": "localhost:%d" % port}) == 200)
    check("a forwarded private client is let in",
          status(local, port, "/library/sections",
                 headers={"X-Forwarded-For": "192.168.1.5, 10.1.2.3, fd00::5"}) == 200)
    for hop in ("8.8.8.8", "100.85.1.1", "169.254.1.1", "192.168.1.5, 8.8.8.8", "nonsense"):
        check("a forwarded %s is refused" % hop,
              status(local, port, "/library/sections", headers={"X-Forwarded-For": hop}) == 401)
    for host in ("evil.example:%d" % port, "192.0.2.10:%d" % port):
        check("a Host naming %s is refused" % host.split(":")[0],
              status(local, port, "/library/sections", headers={"Host": host}) == 401)
    public = {"X-Forwarded-For": "8.8.8.8"}
    for path, wanted in (("/identity", 200), ("/web/index.html", None), ("/manage", 302),
                         ("/crossdomain.xml", 200), ("/favicon.ico", 200),
                         ("/security/token?type=delegation&scope=all", None),
                         ("/", 401), ("/:/prefs", 401), ("/status/sessions", 401)):
        got = status(local, port, path, headers=public)
        check("from the internet %s is %s" % (path, wanted or "not refused for its token"),
              got == wanted if wanted else got != 401, got)

    address = own_address()
    if address is None:
        print("SKIP the non-loopback peer checks: no non-loopback IPv4 address")
    else:
        private = any(address.startswith(prefix) for prefix in ("10.", "192.168.")) or (
            address.startswith("172.") and 16 <= int(address.split(".")[1]) <= 31)
        got = status(address, port, "/library/sections")
        check("a peer at %s is %s while allowedNetworks is empty"
              % (address, "let in" if private else "refused"),
              got == (200 if private else 401), got)
        check("allowedNetworks is written from loopback",
              status(local, port, "/:/prefs?allowedNetworks=203.0.113.0/24", method="PUT") == 200)
        check("a peer outside a set allowedNetworks is refused",
              status(address, port, "/library/sections") == 401)
        check("loopback is let in whatever allowedNetworks says",
              status(local, port, "/library/sections") == 200)
        check("a forwarded hop cannot vouch for a refused peer",
              status(address, port, "/library/sections",
                     headers={"X-Forwarded-For": "203.0.113.9"}) == 401)
        check("allowedNetworks naming the peer lets it in",
              status(local, port, "/:/prefs?allowedNetworks=%s/32" % address, method="PUT") == 200
              and status(address, port, "/library/sections") == 200)


def main():
    logs = []
    for leg, args in ((unclaimed, ()), (asserted, ("--assert-claimed",))):
        port = free_port()
        proc, log = start(port, *args)
        logs.append(log)
        try:
            leg(port)
        finally:
            stop(proc)
    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    if passed != len(results):
        for log in logs:
            log.seek(0)
            print(log.read().decode(errors="replace")[-3000:])
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
