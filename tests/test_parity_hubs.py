#!/usr/bin/env python3
"""Hubs and search answer the oracle's shapes: the parity checks for route family A.

Each check here is a difference found by recording the UI suite against `server.py`
and against the native server and diffing every exchange (`/hubs*`, `/hubs/search*`,
`/library/sections/{id}/search|autocomplete`, `/library/search`, `/search`,
`/library/matches`). They assert the oracle's concrete values, so each one passes on
`server.py`, failed on the native build before its fix, and passes after it.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the Python oracle with `PLEXSIM_SERVER=python`, any other build with
`PLEXSIM_SERVER=/path/to/plexsim`. Writes go to its own in-memory catalog.

    python3 tests/test_parity_hubs.py
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
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Server:
    def __init__(self, port):
        self.port = port

    def raw(self, path, method="GET", xml=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"X-Plex-Token": "parity", "X-Plex-Client-Identifier": "parity-hubs",
                   "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}
        if not xml:
            headers["Accept"] = "application/json"
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        return response.status, body

    def json(self, path, method="GET"):
        status, body = self.raw(path, method)
        try:
            return status, json.loads(body) if body else None
        except ValueError:
            return status, None

    def container(self, path):
        return (self.json(path)[1] or {}).get("MediaContainer") or {}


def keys(rows):
    return [row.get("ratingKey") for row in rows or []]


def checks(srv):
    # -- /hubs/sections/{id} is a hub container ----------------------------------
    body = srv.container("/hubs/sections/1")
    check("/hubs/sections/1 is a hub container: size, allowSync, identifier, the library, Hub",
          list(body) == ["size", "allowSync", "identifier", "librarySectionID",
                         "librarySectionTitle", "librarySectionUUID", "Hub"], list(body))
    check("/hubs/sections/1 says its librarySectionID as an INT",
          body.get("librarySectionID") == 1, repr(body.get("librarySectionID")))
    body = srv.container("/hubs/sections/02?count=12&includeMeta=1")
    check("/hubs/sections/02 carries no media-tag pair and reads the id as 2",
          "mediaTagPrefix" not in body and "mediaTagVersion" not in body
          and body.get("librarySectionID") == 2,
          {k: v for k, v in body.items() if k != "Hub"})

    # -- XML: a hub that carries an EMPTY collection is written paired -------------
    status, xml = srv.raw("/hubs", xml=True)
    text = xml.decode("utf-8", "replace")
    check("/hubs XML writes the empty Continue Watching hub PAIRED",
          '<Hub key="/hubs/home/continueWatching" title="Continue Watching" type="mixed" '
          'hubIdentifier="home.continue" context="hub.home.continue" size="0" more="0" '
          'style="hero" promoted="1">\n</Hub>\n' in text, text[:600])
    status, xml = srv.raw("/hubs/sections/1", xml=True)
    text = xml.decode("utf-8", "replace")
    check("/hubs/sections/1 XML writes its empty shelves paired, with no media-tag pair",
          '<MediaContainer size="6" allowSync="1" identifier="com.plexapp.plugins.library" '
          'librarySectionID="1" librarySectionTitle="Debrid Movies" '
          'librarySectionUUID="4d3316c14882aeacb807a385e2b089e2a47f563f">\n' in text
          and 'hubIdentifier="movie.inprogress.1" context="hub.movie.inprogress" size="0" '
          'more="0" style="shelf">\n</Hub>\n' in text, text[:600])
    status, xml = srv.raw("/hubs/metadata/1007", xml=True)
    text = xml.decode("utf-8", "replace")
    check("/hubs/metadata/1007 XML self-closes its four collection-less hubs",
          text.count(" />\n") == 4 and "</Hub>" not in text, text[:600])

    # -- /hubs/metadata/{id}: the four artist shelves keyed to the item's library ---
    body = srv.container("/hubs/metadata/1007")
    hubs = body.get("Hub") or []
    check("/hubs/metadata/1007 is a hub container with identifier",
          list(body) == ["size", "allowSync", "identifier", "Hub"]
          and body.get("identifier") == "com.plexapp.plugins.library", list(body))
    check("/hubs/metadata/1007 shelves name the library's own listings",
          [h.get("key") for h in hubs] == [
              "/library/sections/1/all?artist.id=1007&type=10&sort=viewCount:desc&viewCount>=10",
              "/library/sections/1/all?artist.id=1007&type=10&sort=ratingCount:desc&group=title",
              "/library/sections/1/all?artist.id=1007&type=9&sort=year:desc",
              "/library/sections/1/extras/all?extraType=4&type=8&artist.id=1007"],
          [h.get("key") for h in hubs])
    check("/hubs/metadata/1007 shelves are typed track, track, album, clip",
          [h.get("type") for h in hubs] == ["track", "track", "album", "clip"],
          [h.get("type") for h in hubs])
    body = srv.container("/hubs/metadata/01001")
    check("/hubs/metadata/01001 reads the id as digits and keys to item 1001 in section 2",
          [h.get("key") for h in body.get("Hub") or []][:1]
          == ["/library/sections/2/all?artist.id=1001&type=10&sort=viewCount:desc&viewCount>=10"],
          body)

    # -- /hubs/search/voice ---------------------------------------------------------
    body = srv.container("/hubs/search/voice?query=tears")
    hubs = body.get("Hub") or []
    hub = hubs[0] if hubs else {}
    check("voice search ranks the movie hub as /hubs/search does (1009, 1008, 1007)",
          hub.get("hubKey") == "/library/metadata/1009,1008,1007"
          and keys(hub.get("Metadata")) == ["1009", "1008", "1007"], hub.get("hubKey"))
    rows = hub.get("Metadata") or []
    check("voice search rows are light GRID rows: Media kept, three roles, a distance",
          rows and all("Media" in r and len(r.get("Role") or []) == 3 and r.get("distance") == 5
                       for r in rows),
          [(r.get("ratingKey"), "Media" in r, len(r.get("Role") or [])) for r in rows])
    body = srv.container("/hubs/search/voice?type=2&query=Simp")
    rows = ((body.get("Hub") or [{}])[0]).get("Metadata") or []
    check("voice search's show row keeps a distance and drops Location",
          rows and rows[0].get("distance") == 4 and "Location" not in rows[0], rows[:1])
    body = srv.container("/hubs/search/voice?type=8&query=Simp")
    check("voice search with a type that has no hub still says size 0",
          body == {"size": 0}, body)


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
    try:
        srv = Server(port)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if srv.raw("/identity")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            log.seek(0)
            print(log.read().decode("utf-8", "replace")[-2000:])
            print("FAIL  server did not start")
            return 1
        checks(srv)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    passed = sum(results)
    print("%s %d/%d parity checks for hubs and search"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
