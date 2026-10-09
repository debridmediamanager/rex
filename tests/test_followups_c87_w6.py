#!/usr/bin/env python3
"""Cycle 87 follow-up W6: an empty library's composite, and `/system` under a JSON Accept.

1. `tests/fixtures/c87_w6_empty_composite_s42.json`: an EMPTY movie, show and music
   library on plexscratch42 (1.42.2), made with Add Library's form. Their versioned
   `/library/sections/{S}/composite/{n}` -- bare and with the `width`/`height`/`rows`/
   `cols`/`border`/`media`/`repeat` set Plex Web's composite view appends -- and the photo
   transcoder's fetch of it are the ordinary 404 under every Accept. plexsim drew a
   generated PNG for all of them.
2. `tests/fixtures/c87_w6_system_xml_accept.json`: every `/system` path Plex Web reads,
   under four Accept values. Only the bare `/system/agents` answers JSON; `/system`,
   `/system/search`, `/system/library/sections` and `/system/scanners/{N}` answer their
   `application/xml` document whatever Accept says, and the music scanner list is its own
   220-byte form. plexsim answered JSON to a JSON Accept, and 217 bytes for music.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_w6.py
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
COMPOSITE = os.path.join(HERE, "fixtures", "c87_w6_empty_composite_s42.json")
SYSTEM = os.path.join(HERE, "fixtures", "c87_w6_system_xml_accept.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
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


def request(port, method, path, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request(method, path, headers=dict(
            headers, **{"X-Plex-Client-Identifier": "followups-c87-w6"}))
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), raw
    finally:
        connection.close()


def measured_type(row):
    if "content_type" in row:
        return row["content_type"]
    return dict((k.lower(), v) for k, v in row["headers"]).get("content-type")


def composite(port, fixture):
    base = fixture["request_headers"]
    sections = {}
    for kind, made in fixture["created_with"].items():
        status, _headers, raw = request(port, "POST", made["path"],
                                        dict(base, Accept="application/json"))
        ok = status == made["status"]
        check("Add Library makes the empty %s library (%d)" % (kind, made["status"]), ok, status)
        sections[kind] = str(json.loads(raw)["MediaContainer"]["Directory"][0]["key"]) \
            if ok else "0"
    for row in fixture["exchanges"]:
        ours = sections[row["type"]]
        path = row["path"].replace("{S}", ours)
        headers = dict(base, **({"Accept": row["accept"]} if row["accept"] else {}))
        status, got, raw = request(port, "GET", path, headers)
        check("empty %s library: %s (Accept %s) is the measured %d %s" % (
            row["type"], row["path"], row["accept"], row["status"], measured_type(row)),
            status == row["status"] and got.get("content-type") == measured_type(row)
            and raw.decode("utf-8", "replace") == row["body"],
            (status, got.get("content-type"), raw[:80]))
    # The contrast: a library WITH members still answers its composite (kids, 200 jpeg).
    status, got, _raw = request(
        port, "GET", "/library/sections/1/composite/1?width=120&height=120&rows=1&cols=1"
        "&border=0&media=thumb&repeat=1", dict(base, Accept="image/webp,image/*,*/*;q=0.8"))
    check("a library with members still answers its composite with an image",
          status == 200 and got.get("content-type", "").startswith("image/"),
          (status, got.get("content-type")))
    for kind, ours in sections.items():
        status, _got, _raw = request(port, "DELETE", "/library/sections/%s" % ours, base)
        check("Delete Library removes the empty %s library (%d)" % (
            kind, fixture["deleted"][kind]), status == fixture["deleted"][kind], status)


def system(port, fixture):
    base = fixture["request_headers"]
    for row in fixture["exchanges"]:
        headers = dict(base, **({"Accept": row["accept"]} if row["accept"] else {}))
        status, got, raw = request(port, "GET", row["path"], headers)
        text = raw.decode("utf-8", "replace")
        want_type = measured_type(row)
        label = "%s (Accept %s) is the measured %d %s" % (
            row["path"], row["accept"], row["status"], want_type)
        if "body" in row:
            check(label + ", byte for byte",
                  status == row["status"] and got.get("content-type") == want_type
                  and text == row["body"], (status, got.get("content-type"), text[:160]))
        else:
            check(label, status == row["status"] and got.get("content-type") == want_type
                  and text.startswith(row["body_starts"][:5]),
                  (status, got.get("content-type"), text[:60]))
        if row["path"].startswith("/system/scanners/") and row["status"] == 200:
            check("%s (Accept %s) sends no framework cookie" % (row["path"], row["accept"]),
                  "set-cookie" not in got, got.get("set-cookie"))


def main():
    process, port = start()
    try:
        composite(port, json.load(open(COMPOSITE)))
        system(port, json.load(open(SYSTEM)))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d c87 W6 follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
