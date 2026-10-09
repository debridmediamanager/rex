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


def test_small_shapes(server):
    _, _, sorts = server.json("GET", "/playlists/sorts")
    first = (sorts.get("Directory") or [{}])[0]
    check("/playlists/sorts marks no sort active",
          "active" not in first and "activeDirection" not in first, first)
    _, _, found = server.json("GET", "/library/metadata/1007/subtitles?language=en")
    check("the subtitle search container carries no allowSync",
          list(found) == ["size", "identifier", "mediaTagPrefix", "mediaTagVersion"],
          list(found))
    _, _, both = server.json("GET", "/library/metadata/20001,1007")
    check("a comma-joined read names the first row's library as an integer",
          both.get("librarySectionID") == 2, repr(both.get("librarySectionID")))
    _, _, listing = server.json("GET", "/library/sections/2/collections")
    check("a show library's collection list is titled All Shows",
          listing.get("title2") == "All Shows" and listing.get("nocache") is True,
          (listing.get("title2"), listing.get("nocache")))
    _, _, typed = server.json("GET", "/library/sections/2/collections?type=18")
    check("an explicit type=18 is the /all container, without nocache",
          "nocache" not in typed and typed.get("title2") == "All Shows",
          (typed.get("nocache"), typed.get("title2")))
    _, _, grid = server.json("GET", "/library/sections/1/all?type=1")
    _, _, movies = server.json("GET", "/library/sections/1/collections?type=1")
    check("type=1 on the collections leaf answers the grid's own rows, no Guid",
          movies.get("Metadata") == grid.get("Metadata")
          and not any("Guid" in row for row in rows(movies)),
          [sorted(set(row) ^ set(other)) for row, other in zip(rows(movies), rows(grid))][:2])
    for spelling in ("42", "042", "00042"):
        _, _, background = server.json("GET", "/playlists?type=" + spelling)
        row = rows(background)[0] if rows(background) else {}
        check("/playlists?type=%s is the background list, addedAt last" % spelling,
              list(row)[-1:] == ["addedAt"] and row.get("ratingKey") == "39999",
              list(row))
    _, _, empty = server.json("GET", "/playlists/all?type=42")
    check("/playlists/all?type=42 is an empty container", empty == {"size": 0}, empty)


def test_written_prefs_stay_on_the_detail_row(server):
    server.call("PUT", "/library/metadata/20001/prefs?episodeSort=1&useOriginalTitle=1")
    _, _, detail = server.json("GET", "/library/metadata/20001")
    shown = rows(detail)[0] if rows(detail) else {}
    check("the show's detail row carries its written settings",
          (shown.get("episodeSort"), shown.get("useOriginalTitle")) == ("1", "1"),
          (shown.get("episodeSort"), shown.get("useOriginalTitle")))
    _, _, both = server.json("GET", "/library/metadata/20001,1007")
    show = rows(both)[0] if rows(both) else {}
    check("a comma-joined read carries no written setting",
          "episodeSort" not in show and "useOriginalTitle" not in show, sorted(show))
    _, _, made = server.json(
        "POST", "/playlists?title=prefs&type=video&smart=0&uri=" + server.uri(30001))
    playlist = rows(made)[0]["ratingKey"] if rows(made) else None
    _, _, shows = server.json("GET", "/playlists/%s/items?type=2" % playlist)
    show = rows(shows)[0] if rows(shows) else {}
    check("a playlist's show row carries no written setting",
          show.get("ratingKey") == "20001" and "episodeSort" not in show
          and "useOriginalTitle" not in show, sorted(show))
    _, _, made = server.json(
        "POST", "/library/collections?type=2&title=prefs&smart=0&sectionId=2&uri="
        + server.uri(20001))
    collection = rows(made)[0]["ratingKey"] if rows(made) else None
    for path in ("/library/collections/%s/children", "/library/metadata/%s/children"):
        _, _, children = server.json("GET", path % collection)
        show = rows(children)[0] if rows(children) else {}
        check("%s carries no written setting" % (path % "N"),
              show.get("ratingKey") == "20001" and "episodeSort" not in show
              and "useOriginalTitle" not in show, sorted(show))
    server.call("DELETE", "/playlists/%s" % playlist)
    server.call("DELETE", "/library/collections/%s" % collection)
    server.call("PUT", "/library/metadata/20001/prefs?episodeSort=-1&useOriginalTitle=-1")


IMAGE = b"\x89PNG\r\n\x1a\n" + b"plexsim parity upload " * 3
IMAGE_SHA1 = __import__("hashlib").sha1(IMAGE).hexdigest()


def test_list_artwork(server):
    _, _, made = server.json(
        "POST", "/playlists?title=c36upload&type=video&smart=0&uri=" + server.uri(1007))
    playlist = rows(made)[0]["ratingKey"] if rows(made) else None
    _, _, made = server.json(
        "POST", "/library/collections?title=c36uploadc&type=1&smart=0&sectionId=1&uri="
        + server.uri(1007))
    collection = rows(made)[0]["ratingKey"] if rows(made) else None
    _, _, before = server.json("GET", "/library/metadata/%s/posters" % playlist)
    first = rows(before)[0] if rows(before) else {}
    stamp = str((rows(made) or [{}])[0].get("updatedAt"))
    check("a playlist's composite candidate leads with key and names a real stamp",
          list(first) == ["key", "ratingKey", "thumb", "selected"]
          and "/composite/1700000000" not in first.get("key", ""), first)
    for key, name in ((playlist, "playlist"), (collection, "collection")):
        for leaf, kind in (("posters", "posters"), ("arts", "art")):
            status, headers, payload = server.call(
                "POST", "/library/metadata/%s/%s" % (key, leaf), body=IMAGE)
            check("a %s takes a %s upload as an empty 200" % (name, leaf),
                  (status, payload) == (200, b""), (status, payload[:40]))
            _, _, listed = server.json("GET", "/library/metadata/%s/%s" % (key, leaf))
            expected = "upload://%s/%s" % (kind, IMAGE_SHA1)
            got = [(row.get("key"), row.get("selected")) for row in rows(listed)]
            if leaf == "posters":
                # Cycle 88, measured on plexscratch42 (1.42.2): a list's poster uploads
                # come FIRST under their `/library/metadata/{id}/file?url=` key, and the
                # composite last with an explicit `selected`
                # (`tests/fixtures/*_poster_upload_s42.json`).
                served = "/library/metadata/%s/file?url=%s" % (
                    key, urllib.parse.quote(expected, safe="").replace(".", "%2E"))
                check("the %s's posters list leads with the upload, keyed by its sha1 and "
                      "selected, and ends with the unselected composite" % name,
                      got[:1] == [(served, True)] and len(got) == 2
                      and got[1][1] is False and "/composite/" in str(got[1][0]), got)
                continue
            check("the %s's %s list ends with the upload, keyed by its sha1 and selected"
                  % (name, leaf), got[-1:] == [(expected, True)]
                  and all(selected is None for _, selected in got[:-1]), got)
    _, _, row = server.json("GET", "/playlists/%s" % playlist)
    row = rows(row)[0] if rows(row) else {}
    check("the uploads give the playlist row its own thumb, art and Image",
          str(row.get("thumb", "")).startswith("/library/metadata/%s/thumb/" % playlist)
          and str(row.get("art", "")).startswith("/library/metadata/%s/art/" % playlist)
          and [image.get("type") for image in row.get("Image") or []]
          == ["coverPoster", "background"], row)
    server.call("PUT", "/library/metadata/%s/poster?url=http%%3A%%2F%%2Fexample.com%%2Fa.jpg"
                % collection)
    _, _, listed = server.json("GET", "/library/metadata/%s/posters" % collection)
    got = [row.get("key") for row in rows(listed)]
    check("a picked url is the collection's selected candidate",
          got[-1:] == ["http://example.com/a.jpg"], got)
    _, _, listing = server.json("GET", "/library/sections/1/collections")
    mine = [row for row in rows(listing) if row.get("ratingKey") == collection]
    check("the section's collection list carries no Field lock list",
          mine and "Field" not in mine[0], mine)
    for leaf in ("clearLogos", "themes"):
        _, _, empty = server.json("GET", "/library/metadata/%s/%s" % (playlist, leaf))
        check("a playlist's %s is the library tag container" % leaf,
              list(empty) == ["size", "identifier", "mediaTagPrefix", "mediaTagVersion"],
              list(empty))
    server.call("DELETE", "/playlists/%s" % playlist)
    server.call("DELETE", "/library/collections/%s" % collection)


def test_transcoder_and_envelope(server):
    status, headers, payload = server.call(
        "GET", "/photo/:/transcode?url=%2Flibrary%2Fmetadata%2F1014%2Fthumb%2F1"
        "&width=200&height=300")
    size = (int.from_bytes(payload[16:20], "big"), int.from_bytes(payload[20:24], "big")) \
        if payload.startswith(b"\x89PNG") else None
    check("a transcoded row with no image is the generated tile at the TRANSCODER's size",
          (status, size, len(payload)) == (200, (200, 300), 840), (status, size, len(payload)))
    status, headers, payload = server.call("PATCH", "/playlists",
                                           headers={"Connection": "close"})
    check("a PATCH from a closing client is a 500 that closes",
          (status, headers.get("Connection"), headers.get("Keep-Alive"))
          == (500, "close", None),
          (status, headers.get("Connection"), headers.get("Keep-Alive")))
    server.call("POST", "/library/metadata/1008/themes", body=IMAGE)
    _, _, detail = server.json("GET", "/library/metadata/1008")
    theme = (rows(detail) or [{}])[0].get("theme")
    status, headers, payload = server.call("GET", theme or "/library/metadata/1008/theme/0")
    check("an uploaded theme is served with a byte range unit",
          (status, headers.get("Accept-Ranges"), payload) == (200, "bytes", IMAGE),
          (status, headers.get("Accept-Ranges"), len(payload)))


def main():
    port = int(os.environ.get("PLEXSIM_TEST_PORT") or free_port())
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
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
        test_small_shapes(server)
        test_written_prefs_stay_on_the_detail_row(server)
        test_list_artwork(server)
        test_transcoder_and_envelope(server)
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
