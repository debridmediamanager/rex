#!/usr/bin/env python3
"""Cycle 85, territory B: a TAB anywhere in a request header's value is a 400.

Measured over raw sockets on plexlab (PMS 1.43.4) and plexscratch42 (PMS 1.42.2), identical,
and recorded in `tests/fixtures/header_tab_roku.json`: `X-Plex-Platform:\\tRoku`,
`: \\tRoku`, `:\\t Roku`, `:  \\tRoku`, `Ro\\tku`, `Roku\\t`, `X-Plex-Version:\\t9.7.1` and an
unknown header's leading TAB are each a plain 400, beside a control that is a 200.

Cycle 84's one-optional-space rewrite (44b2f43) left the oracle's TAB check where it was,
BEFORE the rewrite, on the values Python's parser had already stripped of their leading
whitespace -- so a leading TAB (alone, or after the one optional space) passed as a 200
with the platform `\\tRoku`, while the native server refused it. B's `regress64b_ows`
caught it at 5/7 on the oracle.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `REX_SERVER=python`.

    python3 tests/test_parity_cycle85_b.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "header_tab_roku.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def identity(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", "/identity", headers={"Connection": "close"})
        return connection.getresponse().status
    finally:
        connection.close()


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if identity(port) == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def raw_providers(port, lines):
    """`GET /media/providers` over a raw socket: http.client refuses a TAB in a value."""
    head = [b"GET /media/providers HTTP/1.1", b"Host: 127.0.0.1:%d" % port,
            b"Accept: application/json", b"X-Plex-Client-Identifier: c85b-tab",
            b"X-Plex-Product: Plex for Roku", b"Connection: close"]
    head += [line.encode("latin-1") for line in lines]
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        sock.sendall(b"\r\n".join(head) + b"\r\n\r\n")
        data = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return int(data.split(b" ", 2)[1])


def main():
    cells = json.load(open(FIXTURE))["cells"]
    process, port = start()
    try:
        for cell in cells:
            statuses = {answer["status"] for answer in cell["answers"].values()}
            check("reference: plexlab and plexscratch42 agree on %s" % cell["name"],
                  len(statuses) == 1, cell["answers"])
            want = statuses.pop()
            got = raw_providers(port, cell["lines"])
            check("%r is a %d, as both references answer" % (cell["lines"], want),
                  got == want, got)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d cycle-85 B parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
