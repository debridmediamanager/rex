#!/usr/bin/env python3
"""Cycle 87 follow-up W5: an EMPTY music library, read the way Plex Web reads it.

Add Library now creates a `type=artist` library on both servers (W4), and plexsim
holds no music, so the library a user adds is empty. Plex Web 4.162.1 and python-plexapi
4.18.2 then read its row, its `/media/providers` directory, its `Meta`, its filter and
sort descriptors, its preferences and its hubs. `tests/fixtures/c87_w5_music_library_s42.json`
is every one of those reads, replayed on plexscratch42 (1.42.2) against the same empty
music library made with the same form, and each check below asserts the measured value.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_w5.py
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
FIXTURE = os.path.join(HERE, "fixtures", "c87_w5_music_library_s42.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
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


class Client:
    def __init__(self, port, fixture):
        self.port = port
        self.headers = dict(fixture["request_headers"], **{
            "X-Plex-Client-Identifier": "followups-c87-w5", "Accept": "application/json"})

    def get(self, method, path, paging=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = dict(self.headers, **(paging or {}))
        try:
            connection.request(method, path, headers=sent)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        try:
            body = json.loads(raw) if raw.strip().startswith(b"{") else raw
        except ValueError:
            body = raw
        return response.status, body


def retarget(value, theirs, ours):
    """The reference's section key rewritten to ours, everywhere it names the library."""
    text = json.dumps(value)
    for pattern in ("/sections/%s/", "/sections/%s\"", "/sections/%s?", "sectionID=%s",
                    "contentDirectoryID=%s", "%%2C%s%%2C"):
        text = text.replace(pattern % theirs, pattern % ours)
    return json.loads(text)


def exchange(fixture, path):
    return next(row for row in fixture["exchanges"] if row["path"] == path)


def run(port, fixture):
    client = Client(port, fixture)
    theirs = fixture["section"]
    status, created = client.get("POST", fixture["created_with"])
    ours = str(created["MediaContainer"]["Directory"][0]["key"]) if status == 201 else "0"
    check("Add Library creates the music library", status == 201, status)
    echo = fixture["created_body"]["MediaContainer"]["Directory"][0]
    got = created["MediaContainer"]["Directory"][0] if status == 201 else {}
    check("the 201 echoes the measured attributes in the measured order",
          list(got) == list(echo), list(got))
    check("the 201 echoes the form's agent, scanner, group and folder",
          [got.get(k) for k in ("agent", "scanner", "metadataAgentProviderGroupId")]
          == [echo[k] for k in ("agent", "scanner", "metadataAgentProviderGroupId")]
          and got.get("Location", [{}])[0].get("path") == echo["Location"][0]["path"], got)

    def measured(path):
        row = exchange(fixture, path)
        return row, retarget(row["body"], theirs, ours)

    def ask(path, paging=None):
        return client.get("GET", path.replace("{S}", ours), paging)

    # -- the library row keeps the form ----------------------------------------
    _row, want = measured("/library/sections")
    want = want["MediaContainer"]["Directory"][0]
    _status, body = ask("/library/sections")
    got = next((d for d in body["MediaContainer"]["Directory"] if str(d["key"]) == ours), {})
    for name in ("type", "agent", "scanner", "language", "metadataAgentProviderGroupId",
                 "contentChangedAt"):
        check("the library row's %s is the form's (%r)" % (name, want[name]),
              got.get(name) == want[name], got.get(name))
    check("the library row's folder is the one the form named",
          [loc.get("path") for loc in got.get("Location") or []]
          == [loc["path"] for loc in want["Location"]], got.get("Location"))
    check("the row's three create stamps are one moment, not 0",
          got.get("createdAt") and got.get("createdAt") == got.get("updatedAt")
          == got.get("scannedAt"), got)
    _row, want = measured("/media/providers")
    want = want["MediaContainer"]["Directory"][0]
    _status, body = ask("/media/providers")
    got = next((d for provider in body["MediaContainer"].get("MediaProvider", [])
                for feature in provider.get("Feature", []) if feature.get("type") == "content"
                for d in feature.get("Directory", []) if d.get("id") == ours), {})
    check("/media/providers names the music agent and scanner",
          (got.get("agent"), got.get("scanner")) == (want["agent"], want["scanner"]),
          (got.get("agent"), got.get("scanner")))
    _row, want = measured("/library/sections/{S}/location")
    _status, body = ask("/library/sections/{S}/location")
    check("the folder axis lists the folder the form named",
          [d.get("title") for d in body["MediaContainer"].get("Directory", [])]
          == [d["title"] for d in want["MediaContainer"]["Directory"]], body)


def main():
    fixture = json.load(open(FIXTURE))
    process, port = start()
    try:
        run(port, fixture)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d c87 W5 follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
