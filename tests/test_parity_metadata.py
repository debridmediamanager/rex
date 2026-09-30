#!/usr/bin/env python3
"""Item metadata and the library's secondary leaves answer what the oracle answers.

Each check is a difference the full UI suite's recording found between the native
server and the Python oracle (`server.py`), on `/library/metadata/{id}` and its leaves,
`/library`, `/library/tags`, `/library/arts`, `/library/randomArtwork` and
`/library/people/{id}/media`. The values asserted are the oracle's own -- concrete
shapes, not "equal to the other server" -- so the file passes on the oracle and on a
native build that has closed the gaps.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`, any other build with `PLEXSIM_SERVER=/path`.

    python3 tests/test_parity_metadata.py
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBRARY = {"identifier": "com.plexapp.plugins.library",
           "mediaTagPrefix": "/system/bundle/media/flags/", "mediaTagVersion": 1655122614}

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

    def call(self, method, path, accept="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, headers={
                "Accept": accept, "X-Plex-Client-Identifier": "parity-metadata",
                "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"})
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), body

    def get(self, path):
        status, _headers, body = self.call("GET", path)
        try:
            return status, json.loads(body)["MediaContainer"]
        except ValueError:
            return status, None

    def rows(self, path, key="Metadata"):
        return (self.get(path)[1] or {}).get(key) or []


def raw_exchange(port, request):
    """Send bytes on one socket; answer the response head as text."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(request)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return data.split(b"\r\n\r\n", 1)[0].decode("latin-1")


def run(server):
    # -- containers: the library identity where the oracle carries it -------------
    status, root = server.get("/library")
    check("/library carries the secondary container's art, title2 and library identity",
          status == 200 and root is not None
          and {key: root.get(key) for key in (
              "size", "allowSync", "art", "content", "identifier", "mediaTagPrefix",
              "mediaTagVersion", "title1", "title2")} == dict(
                  LIBRARY, size=3, allowSync=False, art="/:/resources/library-art.png",
                  content="", title1="Plex Library", title2=""), root)

    matches = server.get("/library/metadata/1007/matches?manual=1&title=Tears+of+Steel"
                         "&agent=tv.plex.agents.movie&language=en-US")[1]
    check("matches without a TMDB key is the empty tag container",
          matches == dict(LIBRARY, size=0), matches)

    nearest = server.get("/library/metadata/1007/nearest")[1]
    check("nearest is the tag container with no allowSync", nearest == dict(LIBRARY, size=0),
          nearest)

    extras = [server.get(path)[1] for path in ("/library/metadata/1007/extras",
                                               "/library/metadata/1007/extras/all")]
    check("an item's extras and extras/all carry allowSync and the library identity",
          all(box == dict(LIBRARY, size=0, allowSync=False) for box in extras), extras)

    arts = server.get("/library/arts")[1] or {}
    check("/library/arts carries the library identity",
          all(arts.get(key) == value for key, value in LIBRARY.items())
          and arts.get("allowSync") is False and arts.get("size") == len(arts.get("Metadata") or []),
          {key: value for key, value in arts.items() if key != "Metadata"})

    grand = server.get("/library/metadata/20001/grandchildren")[1] or {}
    check("grandchildren carries the library identity",
          all(grand.get(key) == value for key, value in LIBRARY.items())
          and grand.get("size") == 8,
          {key: value for key, value in grand.items() if key != "Metadata"})

    unknown = server.get("/library/tags?type=99")[1]
    check("a tag type with no table is the empty tag container",
          unknown == dict(LIBRARY, size=0), unknown)

    # -- randomArtwork: every named library, a bare container ---------------------
    empty = server.get("/library/randomArtwork")[1]
    both = server.get("/library/randomArtwork?sections=1,2")[1] or {}
    keys = sorted(row.get("key", "").split("/art/")[0] for row in both.get("Metadata") or [])
    check("randomArtwork lists every named library in a container of size alone",
          empty == {"size": 0} and sorted(both) == ["Metadata", "size"]
          and keys == ["/library/metadata/%s" % key for key in (
              "1007", "1008", "1009", "1012", "1013", "1014", "20001")], (empty, sorted(both), keys))

    # -- allLeaves below a show is empty -------------------------------------------
    sizes = [server.get("/library/metadata/%s/allLeaves" % key)[1] for key in ("1007", "1001")]
    check("a movie's and an episode's allLeaves are empty",
          all(box is not None and box.get("size") == 0 and not box.get("Metadata")
              for box in sizes), [box and box.get("size") for box in sizes])

    # -- the double trailing slash answers the item itself -------------------------
    status, doubled = server.get("/library/metadata/1007//")
    bad = server.call("GET", "/library/metadata/abc//")[0]
    check("/library/metadata/{id}// answers the item's own detail, a non-numeric id a 404",
          status == 200 and doubled is not None and doubled.get("librarySectionID") == 1
          and [row.get("ratingKey") for row in doubled.get("Metadata") or []] == ["1007"]
          and "content" not in doubled and bad == 404, (status, doubled and {
              key: value for key, value in doubled.items() if key != "Metadata"}, bad))

    # -- the tree reads the captured settings and the stored first air date -------
    tree = (server.get("/library/metadata/20001/tree")[1] or {}).get("MetadataItem") or [{}]
    settings = {row.get("id"): row for row in tree[0].get("Setting") or []}
    audio = settings.get("audioLanguage", {}).get("enumValues", "")
    check("a show's tree node carries its first air date and the captured language enum",
          tree[0].get("originallyAvailableAt") == "2007-12-16"
          and audio.startswith(":Account default|en:English|ca-ES:")
          and "fr:Fran" not in audio,
          (tree[0].get("originallyAvailableAt"), audio[:80]))

    # -- includeAlternates: {key, source} blocks on every FULL row ------------------
    movie = (server.rows("/library/metadata/1007?includeAlternates=1") or [{}])[0]
    blocks = [movie.get(name) for name in ("Thumb", "Art", "Theme")]
    check("includeAlternates inlines {key, source} alone, and no Theme",
          blocks[0] and blocks[1] and blocks[2] is None
          and all(sorted(entry) == ["key", "source"] and entry["source"] == "tmdb"
                  for block in blocks[:2] for entry in block), blocks)
    server.call("GET", "/:/timeline?ratingKey=1007&key=%2Flibrary%2Fmetadata%2F1007"
                       "&state=stopped&time=90000&duration=600000")
    deck = server.rows("/library/onDeck?includeAlternates=1")
    show = (server.rows("/library/metadata/20001?includeAlternates=1&includeChildren=1")
            or [{}])[0]
    seasons = (show.get("Children") or {}).get("Metadata") or []
    related = server.get("/library/metadata/1007/related?includeAlternates=1")[1] or {}
    shelf = [row for hub in related.get("Hub") or [] for row in hub.get("Metadata") or []]
    grid = server.rows("/library/sections/1/all?includeAlternates=1")
    check("and on /library/onDeck, a detail row's Children and related, not on a grid",
          deck and all(row.get("Thumb") for row in deck)
          and seasons and all(row.get("Thumb") and "Art" not in row for row in seasons)
          and shelf and all(row.get("Thumb") for row in shelf)
          and grid and not any(row.get("Thumb") for row in grid),
          ([bool(row.get("Thumb")) for row in deck], [bool(row.get("Thumb")) for row in seasons],
           [bool(row.get("Thumb")) for row in shelf], [bool(row.get("Thumb")) for row in grid]))

    # -- a person's media list is the light grid row ------------------------------
    media = server.rows("/library/people/837308/media")
    check("a person's media rows keep Media and cap each tag family like a grid",
          media and all(row.get("Media") for row in media)
          and all(len(row.get("Role") or []) <= 3 for row in media)
          and all("Stream" not in part for row in media for m in row["Media"]
                  for part in m.get("Part") or []),
          [(row.get("ratingKey"), bool(row.get("Media")), len(row.get("Role") or []))
           for row in media])

    # -- an episode carries no show fields in storage -----------------------------
    comedy = server.get("/library/sections/2/all?type=4&genre=244689")[1] or {}
    check("an episode matches no genre filter: its show's genres are not its own",
          comedy.get("size") == 0, comedy.get("size"))

    # -- the empty mutation answer is text/html ------------------------------------
    answers = [server.call("PUT", path) for path in (
        "/library/metadata/1007/refresh", "/library/metadata/1007/analyze",
        "/library/metadata/1007?titleSort.value=zzz+parity&titleSort.locked=1",
        "/library/metadata/20001/prefs?episodeSort=1")]
    check("a mutation's empty 200 is labelled text/html",
          all(status == 200 and body == b"" and headers.get("content-type") == "text/html"
              for status, headers, body in answers),
          [(status, headers.get("content-type"), body[:40]) for status, headers, body in answers])
    status, headers, body = server.call("PUT", "/library/metadata/1007?summary.value=x",
                                        accept="*/*")
    check("and stays text/html for an XML client",
          status == 200 and body == b"" and headers.get("content-type") == "text/html",
          (status, headers.get("content-type"), body[:60]))

    # -- an unknown method honours the request's Connection: close -----------------
    head = raw_exchange(server.port, b"PATCH /library/metadata/1007/extras HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
    check("PATCH is the 500 and closes a Connection: close request",
          head.startswith("HTTP/1.1 500") and "Connection: close" in head
          and "Keep-Alive" not in head, head)

    # -- the tag TABLE outlives the rows -------------------------------------------
    server.call("PUT", "/library/metadata/1007?genre%5B0%5D.tag.tag=Parity%20Minted")
    server.call("PUT", "/library/metadata/1007?genre%5B0%5D.tag.tag=Science%20Fiction"
                       "&genre%5B1%5D.tag.tag=Animation")
    server.call("PUT", "/library/metadata/20001?genre%5B0%5D.tag.tag=Parity%20Show")
    tags = server.get("/library/tags?type=1")[1] or {}
    names = [row.get("tag") for row in tags.get("Directory") or []]
    check("/library/tags lists a minted tag no row carries, and a show's replaced genre goes",
          "Parity Minted" in names and "Parity Show" in names and "Comedy" not in names
          and tags.get("size") == len(names)
          and all(tags.get(key) == value for key, value in LIBRARY.items()), names)

    # -- top users count every view ever recorded ----------------------------------
    for _ in range(2):
        server.call("GET", "/:/scrobble?key=1008&identifier=com.plexapp.plugins.library")
    history = server.rows("/status/sessions/history/all?type=1")
    first = next((row.get("historyKey") for row in history if row.get("ratingKey") == "1008"),
                 None)
    if first:
        server.call("DELETE", first)
    top = server.get("/library/metadata/1008/users/top")[1] or {}
    check("deleting a history row does not take a view off an item's top users",
          first and [row.get("globalViewCount") for row in top.get("Account") or []] == [2],
          (first, top))

    # -- a collection's /children on the metadata path names its stored section id -
    server.call("POST", "/library/collections?type=1&title=Parity%20Kids&smart=0&sectionId=1"
                        "&uri=server%3A%2F%2Fx%2Fcom.plexapp.plugins.library%2Flibrary%2F"
                        "metadata%2F1007")
    collection = next((row.get("ratingKey") for row in
                       server.rows("/library/sections/1/collections")
                       if row.get("title") == "Parity Kids"), None)
    kids = server.get("/library/metadata/%s/children" % collection)[1] or {}
    check("a collection's metadata-path children name the library by the string sectionId",
          collection and kids.get("librarySectionID") == "1",
          (collection, kids.get("librarySectionID")))

    # -- a written item setting shows on the detail read alone ---------------------
    server.call("PUT", "/library/metadata/1008/prefs?useOriginalTitle=1")
    detail = (server.rows("/library/metadata/1008") or [{}])[0]
    listed = [row for path in ("/library/sections/1/all", "/library/recentlyAdded",
                               "/library/people/837308/media")
              for row in server.rows(path) if row.get("ratingKey") == "1008"]
    check("a written setting echoes on the item's detail row and on no listing row",
          detail.get("useOriginalTitle") == "1" and len(listed) == 3
          and all("useOriginalTitle" not in row for row in listed),
          (detail.get("useOriginalTitle"), [row.get("useOriginalTitle") for row in listed]))

    # -- a genre written onto an episode is the episode's own -----------------------
    server.call("PUT", "/library/metadata/1001?genre%5B0%5D.tag.tag=Parity%20Episode")
    episode = (server.rows("/library/metadata/1001") or [{}])[0]
    leaves = [row for row in server.rows("/library/metadata/20001/allLeaves")
              if row.get("ratingKey") == "1001"]
    check("a genre written onto an episode shows on its detail and its list row",
          [tag.get("tag") for tag in episode.get("Genre") or []] == ["Parity Episode"]
          and leaves and [tag.get("tag") for tag in leaves[0].get("Genre") or []]
          == ["Parity Episode"], (episode.get("Genre"), leaves and leaves[0].get("Genre")))

    # -- an unmatched show: episodes' own stills are their posters again ----------
    server.call("PUT", "/library/metadata/20001/unmatch")
    episode = (server.rows("/library/metadata/1001") or [{}])[0]
    season = (server.rows("/library/metadata/30001") or [{}])[0]
    check("after a show unmatch an episode's own still is its coverPoster",
          [image.get("type") for image in episode.get("Image") or []][:1] == ["coverPoster"],
          episode.get("Image"))
    check("and its seasons no longer borrow the show's poster as parentThumb",
          "parentThumb" not in season and season.get("thumb"), season.get("parentThumb"))


def main():
    port = int(os.environ.get("PLEXSIM_TEST_PORT") or free_port())
    env = dict(os.environ, RD_TOKEN="dummy", PLEXSIM_PLEXTV_URL="http://127.0.0.1:9",
               PYTHONDONTWRITEBYTECODE="1")
    for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
        env.pop(name, None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                       "--gdm", "off", "--assert-claimed", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(300):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        run(Server(port))
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    passed = sum(results)
    print("PASS %d/%d item metadata parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
