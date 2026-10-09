#!/usr/bin/env python3
"""Cycle 87 follow-up W4: Add Library's refusals, replayed from a real PMS.

`tests/fixtures/c87_w4_add_library_refusals_s42.json` holds 135 malformed
`POST /library/sections` forms and what plexscratch42 (1.42.2) answered to each --
re-run on plexscratch (1.43.4), which agreed on 134 (the other names a group 1.43.4
holds). A real PMS refuses in two stages: an ABSENT `name`/`type`/`agent`/`language`
or a non-integer `metadataAgentProviderGroupId` is the generic HTML 400, and past that
every failing check adds a CRLF-terminated line to ONE 400 that carries no
Content-Type at all. plexsim answered the HTML page to every refusal and stopped at
the first failure, and it refused the legacy agents the Add Library dialog itself
offers (`com.plexapp.agents.imdb` with `en`, `com.plexapp.agents.none` with `xn`).

The forms the reference ACCEPTED are replayed too, the music ones included (a real
PMS creates a `type=artist` library with the music agent, `Plex Music` and group 3).

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_w4.py
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
FIXTURE = os.path.join(HERE, "fixtures", "c87_w4_add_library_refusals_s42.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Client-Identifier": "followups-c87-w4", "X-Plex-Product": "Plex Web",
           "X-Plex-Version": "4.162.1", "X-Plex-Platform": "Chrome"}

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


def request(port, method, path, headers=None):
    """(status, header list in wire order, body bytes)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS)
    sent.update(headers or {})
    try:
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        return response.status, response.getheaders(), raw
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
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def section_titles(port):
    _status, _headers, raw = request(port, "GET", "/library/sections",
                                     {"Accept": "application/json"})
    return [row.get("title") for row in
            json.loads(raw)["MediaContainer"].get("Directory", [])]


def run(port):
    with open(FIXTURE, encoding="utf-8") as handle:
        fixture = json.load(handle)
    before = section_titles(port)

    for row in fixture["refusals"]:
        headers = {"Accept": row["accept"]} if row["accept"] else {}
        status, sent, raw = request(port, "POST", row["path"], headers)
        names = {name.lower(): value for name, value in sent}
        wanted_type = {name.lower(): value for name, value in row["headers"]}.get(
            "content-type")
        body = raw.decode("utf-8", "replace")
        check("Add Library %s answers the measured %d, %s and body" % (
                  row["label"], row["status"], wanted_type or "no Content-Type"),
              status == row["status"] and names.get("content-type") == wanted_type
              and body == row["body"],
              "got %s %s %r, want %r" % (status, names.get("content-type"), body[:300],
                                         row["body"][:300]))

    check("no refused form created a library", section_titles(port) == before,
          "%s -> %s" % (before, section_titles(port)))

    orders = fixture["raw_header_orders"]
    bogus = next(row for row in fixture["refusals"] if row["label"] == "type-bogus")

    def order(headers):
        status, sent, raw = request(port, "POST", bogus["path"], headers)
        return status, [name for name, _ in sent], {k.lower(): v for k, v in sent}, raw

    status, names, _values, _raw = order({"Accept": "application/json"})
    check("the untyped refusal orders Connection/Keep-Alive before Content-Length",
          status == 400 and names == orders["plain"], names)
    status, names, values, raw = order({"Accept-Encoding": "gzip, deflate, br, zstd",
                                        "Origin": "https://app.plex.tv"})
    lengths = orders["gzip_lengths_for_type_bogus"]
    check("the untyped refusal is gzipped on offer, in the gzip family's order",
          names == orders["gzip_offered_with_origin"]
          and values.get("x-plex-content-original-length") == str(lengths["original"])
          and values.get("x-plex-content-compressed-length") == str(lengths["compressed"])
          and len(raw) == lengths["compressed"], (names, values))
    status, names, _values, _raw = order({"Connection": "close"})
    check("a closing untyped refusal names `Connection: close` alone",
          names == orders["connection_close_request"], names)

    for row in fixture["accepted"]:
        status, _sent, raw = request(port, "POST", row["path"], {"Accept": "application/json"})
        try:
            created = json.loads(raw)["MediaContainer"]["Directory"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            created = {}
        wanted = row["body"]["MediaContainer"]["Directory"][0]
        check("Add Library %s is the measured 201" % row["label"],
              status == row["status"] and created.get("title") == wanted["title"]
              and created.get("type") == wanted["type"],
              "got %s %r" % (status, raw[:300]))


def main():
    process, port = start()
    try:
        run(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d c87 W4 follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
