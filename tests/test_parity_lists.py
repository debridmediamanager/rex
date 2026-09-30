#!/usr/bin/env python3
"""Playlists, collections and artwork answer what the Python oracle answers.

Each check pins a value the oracle (`server.py`) answers for the UI suite's own request,
found by recording `tests/test_ui_surface.py` against the oracle and the native server
and diffing every exchange. Each one failed on the native build before its fix.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_lists.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Server:
    def __init__(self, port):
        self.port = port
        self.machine = None

    def call(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-lists",
                "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}
        sent.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        return response.status, response.headers, payload

    def json(self, method, path, **kwargs):
        status, headers, payload = self.call(method, path, **kwargs)
        try:
            document = json.loads(payload) if payload else None
        except ValueError:
            document = None
        return status, headers, (document or {}).get("MediaContainer") or {}

    def uri(self, key):
        return urllib.parse.quote(
            "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (self.machine, key),
            safe="")


def rows(container):
    return container.get("Metadata") or []


def test_playlist_over_a_show(server):
    status, _, made = server.json(
        "POST", "/playlists?title=c16items&type=video&smart=0&uri=" + server.uri(1007))
    playlist = rows(made)[0]["ratingKey"] if rows(made) else None
    status, _, added = server.json(
        "PUT", "/playlists/%s/items?uri=%s" % (playlist, server.uri(20001)))
    check("a show added to a playlist adds its eight episodes",
          (status, added.get("leafCountAdded"), added.get("leafCountRequested"))
          == (200, 8, 8), (status, added.get("leafCountAdded"),
                           added.get("leafCountRequested")))
    listed = rows(added)[0] if rows(added) else {}
    check("the PUT's playlist row counts nine leaves",
          listed.get("leafCount") == 9 and listed.get("duration") == 11222000,
          (listed.get("leafCount"), listed.get("duration")))
    _, _, items = server.json("GET", "/playlists/%s/items" % playlist)
    got = [(row.get("ratingKey"), row.get("type"), row.get("playlistItemID"))
           for row in rows(items)]
    check("the list holds the movie then the show's episodes, never the show",
          len(got) == 9 and got[0][:2] == ("1007", "movie")
          and all(kind == "episode" for _, kind, _ in got[1:]), got)
    ids = [item for _, _, item in got]
    check("each episode takes its own server-wide playlistItemID",
          ids == list(range(1, 10)), ids)
    check("the items container's duration is in seconds and counts nine leaves",
          (items.get("duration"), items.get("leafCount"), items.get("size"))
          == (11222, 9, 9), (items.get("duration"), items.get("leafCount"), items.get("size")))
    _, _, shows = server.json("GET", "/playlists/%s/items?type=2" % playlist)
    got = [(row.get("ratingKey"), row.get("type"), "playlistItemID" in row)
           for row in rows(shows)]
    check("type=2 reaches UP to the show the episodes belong to, with no item id",
          got == [("20001", "show", False)], got)
    _, _, seasons = server.json("GET", "/playlists/%s/items?type=3" % playlist)
    got = [row.get("ratingKey") for row in rows(seasons)]
    check("type=3 answers each member's season once, in member order",
          got == ["30001", "30002", "30003", "30004"], got)
    _, _, episodes = server.json("GET", "/playlists/%s/items?type=4" % playlist)
    got = [row.get("playlistItemID") for row in rows(episodes)]
    check("type=4 answers the episodes with their item ids", got == list(range(2, 10)), got)
    status, _, second = server.json(
        "POST", "/playlists?title=next&type=video&smart=0&uri=" + server.uri(1002))
    other = rows(second)[0]["ratingKey"] if rows(second) else None
    _, _, items = server.json("GET", "/playlists/%s/items" % other)
    got = [row.get("playlistItemID") for row in rows(items)]
    check("the next list's item id follows the eight episodes", got == [10], got)
    return playlist, other


def test_empty_answers(server, playlist, other):
    status, headers, payload = server.call("PUT", "/playlists/%s?title=fine" % playlist)
    check("a playlist edit is an empty text/html 200",
          (status, headers.get("Content-Type"), payload) == (200, "text/html", b""),
          (status, headers.get("Content-Type"), payload))
    status, headers, payload = server.call("DELETE", "/playlists/%s" % other)
    check("a playlist delete is an empty text/html 204",
          (status, headers.get("Content-Type"), payload) == (204, "text/html", b""),
          (status, headers.get("Content-Type"), payload))
    status, _, made = server.json(
        "POST", "/library/collections?type=1&title=parity&smart=0&sectionId=1&uri="
        + server.uri(1007))
    collection = rows(made)[0]["ratingKey"] if rows(made) else None
    status, headers, payload = server.call(
        "PUT", "/library/collections/%s?title=renamed" % collection)
    check("a collection rename is an empty text/html 200",
          (status, headers.get("Content-Type"), payload) == (200, "text/html", b""),
          (status, headers.get("Content-Type"), payload))
    status, headers, payload = server.call("DELETE", "/library/collections/%s" % collection)
    check("a collection delete is an empty text/html",
          (headers.get("Content-Type"), payload) == ("text/html", b""),
          (status, headers.get("Content-Type"), payload))
    status, headers, payload = server.call(
        "PUT", "/library/metadata/1007/poster?url=%2Flibrary%2Fmetadata%2F1007%2Fthumb%2F1")
    check("selecting a poster answers the new path as a text/html body",
          status == 200 and headers.get("Content-Type") == "text/html"
          and payload.startswith(b"/library/metadata/1007/thumb/"),
          (status, headers.get("Content-Type"), payload[:60]))
    status, headers, payload = server.call(
        "PUT", "/library/metadata/%s/art?url=http%%3A%%2F%%2Fexample.com%%2Fp.jpg" % playlist)
    check("a playlist's artwork write answers its path as a text/html body",
          status == 200 and headers.get("Content-Type") == "text/html"
          and payload.startswith(b"/library/metadata/%s/art/" % playlist.encode()),
          (status, headers.get("Content-Type"), payload[:60]))


def main():
    port = int(os.environ.get("PLEXSIM_TEST_PORT") or free_port())
    env = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
               PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
    for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
        env.pop(name, None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                       "--gdm", "off", "--assert-claimed", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    server = Server(port)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                status, _, identity = server.json("GET", "/identity")
                if status == 200:
                    server.machine = identity.get("machineIdentifier")
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            log.seek(0)
            print(log.read().decode("utf-8", "replace")[-2000:])
            check("the server starts", False)
            return 1
        playlist, other = test_playlist_over_a_show(server)
        test_empty_answers(server, playlist, other)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    passed = sum(results)
    print("%s %d/%d parity checks for playlists, collections and artwork"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
