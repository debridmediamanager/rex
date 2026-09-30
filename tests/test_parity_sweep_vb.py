#!/usr/bin/env python3
"""Rules found by the differential path-segment and header sweeps (`vsweep.py`), each
checked against the exchange a real PMS gave for the same request.

The expected values live in `tests/fixtures/parity_sweep_vb.jsonl`, one redacted
exchange per line, recorded off kids (1.43.4) or zen (1.42.2). A reference's ratingKey is
mapped onto the fixture catalog's item of the same kind before the request is replayed.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_sweep_vb.py
"""
import gzip
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "parity_sweep_vb.jsonl")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

# a reference's item -> the fixture catalog's item of the same kind
KIDS_ITEMS = {"1": "1007", "17": "1008", "8": "1001"}

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


def fixture(rule):
    with open(FIXTURE) as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [row for row in rows if row["rule"] == rule]


def local_path(path):
    return re.sub(r"(/metadata/)([\d,]+)", lambda m: m.group(1) + ",".join(
        KIDS_ITEMS.get(key, key) for key in m.group(2).split(",")), path)


def send(port, method, path, headers=()):
    """One request on its own connection; the answer as (status, [(name, value)], body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    connection.putheader("Host", "127.0.0.1:%d" % port)
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    connection.close()
    if (response.getheader("Content-Encoding") or "") == "gzip" and body:
        body = gzip.decompress(body)
    return response.status, response.getheaders(), body


def start(*extra):
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", *extra, root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if send(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def header(pairs, name):
    values = [value for key, value in pairs if key.lower() == name.lower()]
    return values or None


def replay(port, rule, what, headers=(), order=False):
    """Every fixture exchange of RULE: the same status, the same (decoded) body, and the
    same values of HEADERS -- absent where the reference sends none -- and with ORDER the
    same response header names in the same order (`Date` aside)."""
    for row in fixture(rule):
        req, ref = row["request"], row["response"]
        path = local_path(req["path"])
        status, got, body = send(port, req["method"], path, req["headers"])
        want = {name: header(ref["headers"], name) for name in headers}
        have = {name: header(got, name) for name in headers}
        if order:
            want["order"] = [name for name, _ in ref["headers"] if name.lower() != "date"]
            have["order"] = [name for name, _ in got if name.lower() != "date"]
        check("%s: %s %s is the measured %d" % (what, req["method"], path, ref["status"]),
              status == ref["status"] and body.decode("utf-8", "replace") == ref["body"]
              and want == have,
              "%s %r %s, measured %s" % (status, body[:200], have, want))


def run(port):
    # 1. A leaf the server does not route under `/hubs/sections/{id}/` is the Not Found
    #    document, not the section's Recommended page.
    replay(port, "hub-leaf", "an unknown section hub leaf")

    # 2. A `/web/*` 404 is the error document, gzipped like any other when gzip is
    #    offered (only the 200 asset is never gzipped); `/web/` is the same 302 as `/web`.
    replay(port, "web-404-gzip", "a missing web asset", headers=(
        "Content-Encoding", "X-Plex-Content-Original-Length",
        "X-Plex-Content-Compressed-Length", "Content-Length", "Connection", "Cache-Control"))

    # 3. `/children` of a leaf -- an episode as well as a movie -- is the 400.
    replay(port, "leaf-children", "a leaf's children")

    # 4. A client's FIRST request registers its device before the answer: kids' first
    #    `GET /devices` from a new identifier already lists that identifier's row.
    for row in fixture("devices-own-row"):
        req, ref = row["request"], row["response"]
        status, _, body = send(port, "GET", req["path"], req["headers"])
        rows = (json.loads(body).get("MediaContainer", {}).get("Device") or []) if status == 200 else []
        ident = dict(req["headers"])["X-Plex-Client-Identifier"]
        own = [{k: v for k, v in r.items() if k not in ("id", "createdAt")}
               for r in rows if r.get("clientIdentifier") == ident]
        check("a first GET /devices lists the asking client's own new row",
              status == ref["status"] and own == ref["own"], "%s %s" % (status, own))

    # 5. A ranged container keeps the socket: the 206 names the whole length and then the
    #    slice's, a second request on the socket is served, and the idle socket is then
    #    closed at 20 s with no bytes at all.
    for row in fixture("ranged-keepalive"):
        ref = row["response"]
        sock = socket.create_connection(("127.0.0.1", port), 10)

        def ask(headers):
            sock.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % (row["request"]["path"], port)
                          + "".join("%s: %s\r\n" % tuple(pair) for pair in headers)
                          + "\r\n").encode())

        def drain(limit):
            sock.settimeout(limit)
            data, eof = b"", False
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        eof = True
                        break
                    data += chunk
            except socket.timeout:
                pass
            return data, eof

        def head(data):
            lines = data.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
            return int(lines[0].split()[1]), [line.split(": ", 1)[0] for line in lines[1:]
                                              if line.split(": ", 1)[0] == "Content-Length"]
        ask(row["request"]["headers"])
        first, eof1 = drain(2)
        status, lengths = head(first) if first else (None, [])
        check("a ranged container is the measured 206 with two lengths and stays open",
              status == ref["status"] and len(lengths) == 2 and eof1 is not row["open_after_first"],
              (status, lengths, eof1))
        ask(row["second"]["headers"])
        second, _ = drain(2)
        check("a second request on that socket is served",
              second and head(second)[0] == row["second"]["response"]["status"], second[:80])
        idle, eof3 = drain(30)
        check("the idle socket is then closed with no bytes",
              eof3 == row["idle"]["eof"] and len(idle) == row["idle"]["bytes"], (eof3, idle[:80]))
        sock.close()


    # 6. The multi-id container's `mediaTagVersion` is a number, in any language.
    for row in fixture("multi-id-json"):
        req = row["request"]
        want = json.loads(row["response"]["body"])["MediaContainer"]["mediaTagVersion"]
        path = local_path(req["path"])
        status, _, body = send(port, "GET", path, req["headers"])
        got = json.loads(body)["MediaContainer"].get("mediaTagVersion") if status == 200 else None
        check("%s's mediaTagVersion is a %s" % (path, type(want).__name__),
              status == 200 and type(got) is type(want), repr(got))

    # 7. `/webclient`, `/websocket`, `/webm`: plug-in paths that merely START with `/web` --
    #    the dispatcher's listing, gzipped and in container header order, and the
    #    empty-segment 404 -- not the web client's tree.
    replay(port, "web-prefix-plugin", "a /web-prefixed plug-in path", headers=(
        "Content-Encoding", "X-Plex-Content-Original-Length"), order=True)

    # 8. The bare `/library/metadata` is the Not Found document.
    replay(port, "bare-metadata", "the bare metadata path")


def run_gate():
    # 7b. With the token gate on, a /web-prefixed plug-in path is gated like any other.
    process, port = start("--token", "c83-vb-secret")
    try:
        replay(port, "web-prefix-gate", "a /web-prefixed plug-in path with no token")
    finally:
        process.terminate()
        process.wait(timeout=10)


def run_last(port):
    # 4b. ...so a first request whose platform is a NUMBER lists the row that the measured
    #     `std::bad_cast` 500 (cycle 49, kids and zen) is about. Never sent to a reference;
    #     it leaves the list broken, so it runs last.
    status, _, _ = send(port, "GET", "/devices", [
        ("Accept", "application/json"), ("X-Plex-Client-Identifier", "c83-vb-numeric"),
        ("X-Plex-Platform", "0")])
    check("a first GET /devices carrying a numeric platform is already the 500", status == 500,
          status)


def main():
    process, port = start()
    try:
        run(port)
        run_last(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    run_gate()
    passed = sum(results)
    print("PASS %d/%d sweep parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
