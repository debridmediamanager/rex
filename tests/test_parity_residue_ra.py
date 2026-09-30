#!/usr/bin/env python3
"""The UI suite's residue, exhausted: what was left after replaying the whole suite's
request stream (the oracle's, then the native server's own) against both servers in
lockstep and neutralising what is random or per-run by measurement.

Each check asserts the MEASURED answer, not the other server's:

- a refusal at `/system/agents` orders its length before its type (kids, 1.43.4:
  `?mediaType=abc` is the 89-byte 400), although the container there is type-first.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_residue_ra.py
"""
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
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = [("Accept", "application/json"), ("X-Plex-Client-Identifier", "parity-residue-ra"),
           ("X-Plex-Product", "Plex Web"), ("X-Plex-Version", "4.147.1"),
           ("X-Plex-Platform", "Chrome")]

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


def raw(port, method, path, body=None, headers=()):
    """One request on its own socket: (status, reason, [header names in order], headers, body)."""
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1:%d" % port]
    lines += ["%s: %s" % pair for pair in HEADERS + list(headers)]
    if body is not None:
        lines.append("Content-Length: %d" % len(body))
    lines.append("Connection: close")
    sock = socket.create_connection(("127.0.0.1", port), 30)
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b""))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    parts = status_line.split(" ", 2)
    names, values = [], {}
    for line in header_lines:
        name, _, value = line.partition(":")
        names.append(name.strip())
        values[name.strip().lower()] = value.strip()
    return int(parts[1]), parts[2] if len(parts) > 2 else "", names, values, payload


def order(names):
    """The response's own headers, less the ones whose place nothing here is about."""
    return [name for name in names if name.lower() not in ("keep-alive",)]


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if process.poll() is not None:
            break
        try:
            if raw(port, "GET", "/identity")[0] == 200:
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


# Measured 2026-09-30 on kids (1.43.4), raw socket, Connection: close.
ERROR_ORDER = ["X-Plex-Protocol", "Content-Length", "Content-Type", "Connection",
               "Cache-Control", "Date"]


def run(port):
    status, reason, names, _, body = raw(port, "GET", "/system/agents?mediaType=abc")
    check("a /system/agents refusal is the 400 with its length before its type",
          status == 400 and order(names) == ERROR_ORDER and len(body) == 89,
          (status, names, len(body)))
    status, reason, names, _, _ = raw(port, "GET", "/system/agents")
    check("... while the /system/agents container stays type-first",
          status == 200 and order(names)[:3] == ["X-Plex-Protocol", "Content-Type",
                                                 "Content-Length"], (status, names))


def main():
    process, port = start()
    try:
        run(port)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d residue checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
