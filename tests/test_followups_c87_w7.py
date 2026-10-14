#!/usr/bin/env python3
"""Cycle 87 follow-up W7: the framework's url lookup, its peer directories' paging, and
the header order of paged and gzipped answers.

1. `tests/fixtures/c87_w7_url_lookup.json`: `/system/services/url/lookup` is the 464-byte
   traceback only when no `url` argument is given. With one -- empty, relative, a non-URL,
   an http URL, a YouTube link -- plexscratch42, plexlab and the 1.42.2 reference answer a
   404 with an EMPTY, untyped body and the framework cookie, under every Accept, gzip offer,
   paging and HEAD. rex answered the traceback to all of them.
2. `tests/fixtures/c87_w7_peer_paging.json`: the ten `/system/{channels,plugins}/{name}`
   peer directories page: a full window adds `totalSize="0"` and both
   `X-Plex-Container-*` headers. rex served the unpaged document whatever was asked.
3. `tests/fixtures/c87_w7_header_order.json`: the header NAMES in wire order for every
   `/system` family and four core containers, plain, paged, gzipped and both. The window
   headers sit after the type (core) or the cookie (`/system`), not after
   `Cache-Control`; every gzipped `/system` answer takes the framework gzip order.

Every request is replayed on a raw keep-alive socket, because `Connection: close` changes
the header block (it drops `Keep-Alive`) and the order is what item 3 measures.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_followups_c87_w7.py
"""
import gzip
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
FIXTURES = os.path.join(HERE, "fixtures")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit("server exited at start")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return process, port
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def raw(port, method, path, headers):
    """One keep-alive exchange: (status, [(name, value)] in wire order, decoded body)."""
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1:%d" % port,
             "X-Plex-Client-Identifier: followups-c87-w7"]
    lines += ["%s: %s" % (name, value) for name, value in headers]
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        rows = head.decode("latin-1").split("\r\n")
        pairs = [tuple(row.split(": ", 1)) for row in rows[1:] if row]
        lengths = [int(value) for name, value in pairs if name.lower() == "content-length"]
        want = 0 if method == "HEAD" or not lengths else lengths[-1]
        while len(body) < want:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
    finally:
        sock.close()
    if any(name.lower() == "content-encoding" and value == "gzip" for name, value in pairs):
        body = gzip.decompress(body)
    return int(rows[0].split()[1]), pairs, body.decode("utf-8", "replace")


def names_of(pairs):
    return [name for name, _value in pairs]


def lookup(port, fixture):
    for row in fixture["exchanges"]:
        status, pairs, body = raw(port, row["method"], row["path"], row["request_headers"])
        want = [pair for pair in row["headers"]]
        got = [[name, value] for name, value in pairs if name != "Date"]
        label = "%s %s %s is the measured %d" % (
            row["method"], row["path"],
            ",".join("%s=%s" % (k, v) for k, v in row["request_headers"]) or "-",
            row["status"])
        # Every value but the compressed length (zlib's bytes are not PMS's) and the
        # gzipped `Content-Length` that follows from it.
        def comparable(block):
            encoded = any(name == "Content-Encoding" for name, _ in block)
            return [[name, value] for name, value in block
                    if name != "X-Plex-Content-Compressed-Length"
                    and not (encoded and name == "Content-Length")]
        check(label, status == row["status"] and body == row["body"]
              and comparable(got) == comparable(want),
              (status, got, body[:120]))


def peers(port, fixture):
    for row in fixture["exchanges"]:
        status, pairs, body = raw(port, row["method"], row["path"], row["request_headers"])
        found = dict(pairs)
        window = {name: value for name, value in pairs if name.startswith("X-Plex-Container-")}
        measured = {name: value for name, value in row["headers"]
                    if name.startswith("X-Plex-Container-")}
        length = found.get("X-Plex-Content-Original-Length", found.get("Content-Length"))
        measured_all = dict(row["headers"])
        measured_length = measured_all.get("X-Plex-Content-Original-Length",
                                           measured_all.get("Content-Length"))
        check("%s %s [%s] is the measured %d, %s bytes, window %s" % (
            row["method"], row["path"], row["variant"], row["status"], measured_length,
            measured or "none"),
            status == row["status"] and window == measured and length == measured_length
            and body == row["body"],
            (status, window, length, body[:150]))


def order(port, fixture):
    for row in fixture["exchanges"]:
        status, pairs, _body = raw(port, row["method"], row["path"], row["request_headers"])
        check("%s %s [%s] writes the measured header order" % (
            row["method"], row["path"], row["variant"]),
            status == row["status"] and names_of(pairs) == row["header_names"],
            (status, names_of(pairs), "want", row["header_names"]))


def main():
    process, port = start()
    try:
        lookup(port, json.load(open(os.path.join(FIXTURES, "c87_w7_url_lookup.json"))))
        peers(port, json.load(open(os.path.join(FIXTURES, "c87_w7_peer_paging.json"))))
        order(port, json.load(open(os.path.join(FIXTURES, "c87_w7_header_order.json"))))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d c87 W7 follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
