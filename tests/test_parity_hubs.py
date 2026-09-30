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

    # -- /library/sections/{id}/autocomplete -----------------------------------------
    rows = srv.container("/library/sections/1/autocomplete?type=1&limit=10&title.query=Ste") \
        .get("Metadata") or []
    check("autocomplete rows are light grid rows without their library fields",
          keys(rows) == ["1007", "1008", "1009"]
          and all("Media" in r and "librarySectionID" not in r and len(r.get("Role") or []) <= 3
                  for r in rows),
          [(r.get("ratingKey"), "Media" in r, "librarySectionID" in r) for r in rows])
    rows = srv.container("/library/sections/2/autocomplete?type=4&title.query=e") \
        .get("Metadata") or []
    check("autocomplete's queryRange is the FIRST occurrence, not the matching word",
          [r.get("title.queryRange") for r in rows[:2]] == ["0,0", "6,6"],
          [(r.get("title"), r.get("title.queryRange")) for r in rows[:2]])
    status, body = srv.json("/library/sections/1/autocomplete?type=1&summary.query=the")
    rows = ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
    check("autocomplete answers a text field other than title (summary)",
          status == 200 and keys(rows) == ["1007", "1008", "1009", "1012", "1013"],
          (status, keys(rows)))
    rows = srv.container("/library/sections/1/autocomplete?type=1&limit=0&title.query=t") \
        .get("Metadata") or []
    check("autocomplete's limit=0 answers one row", keys(rows) == ["1007"], keys(rows))
    rows = srv.container("/library/sections/1/autocomplete?type=1&actor.query=d") \
        .get("Directory") or []
    check("autocomplete's tag rows are alphabetical and carry a person's tagKey and thumb",
          [r.get("tag") for r in rows] == ["Denise Rebergen", "Derek de Lint"]
          and all(r.get("tagKey") and r.get("thumb") for r in rows),
          rows)

    # -- /hubs/items is the named shelf, built as its page builds it -----------------
    body = srv.container("/hubs/items?identifier=movie.recentlyadded.1")
    rows = body.get("Metadata") or []
    check("/hubs/items is a hub container of the shelf's own light rows",
          list(body) == ["size", "allowSync", "identifier", "Metadata"]
          and rows and all("Media" in r and len(r.get("Role") or []) <= 3 for r in rows),
          (list(body), [("Media" in r, len(r.get("Role") or [])) for r in rows]))
    rows = srv.container("/hubs/items?identifier=movie.recentlyadded.1&excludeElements=Media") \
        .get("Metadata") or []
    check("/hubs/items with excludeElements=Media counts the dropped Media",
          rows and all(r.get("mediaCount") == 1 and "Media" not in r for r in rows),
          [r.get("mediaCount") for r in rows])
    status, body = srv.json("/hubs/items?identifier=home.movies.recent&contentDirectoryID=2")
    check("/hubs/items scoped to a library without the shelf's rows is an empty 200",
          status == 200 and body == {"MediaContainer": {
              "size": 0, "allowSync": True, "identifier": "com.plexapp.plugins.library"}},
          (status, body))
    status, body = srv.json("/hubs/items?identifier=movie.recentlyadded.1&count=abc")
    rows = ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
    check("/hubs/items with a count that does not parse is the shelf's default six",
          status == 200 and len(rows) == 6, (status, len(rows)))

    # -- the measured double-slash routes answer their parent's own response ----------
    body = srv.container("/hubs//")
    check("/hubs// answers /hubs itself",
          body.get("identifier") == "com.plexapp.plugins.library" and body.get("size") == 8
          and len(body.get("Hub") or []) == 8, {k: v for k, v in body.items() if k != "Hub"})
    rows = srv.container("/library/metadata/1007//").get("Metadata") or []
    check("/library/metadata/1007// answers the item's detail", keys(rows) == ["1007"], rows[:1])
    status, _ = srv.raw("/library/metadata/1007/children//")
    check("a doubled slash after a leaf the table does not name is still a 404",
          status == 404, status)

    # -- /library/matches rows are light grid rows without their library fields -------
    rows = srv.container("/library/matches?type=1&title=TEARS%20OF%20STEEL&year=2012") \
        .get("Metadata") or []
    check("/library/matches rows keep Media and leave the library fields to nobody",
          keys(rows) == ["1007"]
          and all(r.get("score") == 100 and "Media" in r and "librarySectionID" not in r
                  and len(r.get("Role") or []) <= 3 for r in rows),
          [(r.get("ratingKey"), "Media" in r, "librarySectionID" in r) for r in rows])

    # -- a show's genre is not its episodes' ------------------------------------------
    hubs = srv.container("/hubs/search?query=comedy&limit=50").get("Hub") or []
    by_id = {h.get("hubIdentifier"): h for h in hubs}
    genre = ((by_id.get("genre") or {}).get("Directory") or [{}])[0]
    check("search's Comedy genre row counts the show alone, and finds no episode by it",
          genre.get("count") == 1 and (by_id.get("episode") or {}).get("size") == 0,
          (genre.get("count"), (by_id.get("episode") or {}).get("size")))
    rows = srv.container("/library/sections/2/autocomplete?type=4&genre.query=c") \
        .get("Directory") or []
    check("an episode autocomplete offers no show genre", rows == [], rows)

    # -- an unknown verb's connection state is the request's --------------------------
    heads = []
    for connection in ("close", "keep-alive"):
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        try:
            sock.sendall(("PATCH /hubs HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Plex-Token: parity"
                          "\r\nConnection: %s\r\n\r\n" % connection).encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        lines = data.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
        heads.append([line for line in lines if line.lower().startswith(("http/", "connection"))])
    check("PATCH /hubs with Connection: close is a 500 that closes; keep-alive keeps",
          heads == [["HTTP/1.1 500 Internal Server Error", "Connection: close"],
                    ["HTTP/1.1 500 Internal Server Error", "Connection: Keep-Alive"]], heads)


def write_checks(srv):
    """Checks that write: run last, after every read-only check."""
    # -- a written item setting echoes on the item's OWN detail read alone ----------
    srv.raw("/library/metadata/20001/prefs?episodeSort=1", "PUT")
    srv.raw("/library/metadata/1008/prefs?useOriginalTitle=1", "PUT")
    shelves = srv.container("/hubs/sections/2?count=12&includeMeta=1").get("Hub") or []
    shows = [row for hub in shelves for row in hub.get("Metadata") or []
             if row.get("ratingKey") == "20001"]
    check("a show's written episodeSort stays off its shelf rows",
          shows and not any("episodeSort" in row for row in shows),
          [row.get("episodeSort") for row in shows])
    hubs = srv.container("/hubs/search?query=tears&limit=30").get("Hub") or []
    movies = [row for hub in hubs for row in hub.get("Metadata") or []
              if row.get("ratingKey") == "1008"]
    check("a movie's written useOriginalTitle stays off its search rows",
          movies and not any("useOriginalTitle" in row for row in movies),
          [row.get("useOriginalTitle") for row in movies])
    # -- autocomplete lists its rows in sort-title order ------------------------------
    srv.raw("/library/metadata/1009?titleSort.value=Aardvark&titleSort.locked=1", "PUT")
    rows = srv.container("/library/sections/1/autocomplete?type=1&title.query=tears") \
        .get("Metadata") or []
    check("autocomplete orders its rows by sort title",
          keys(rows) == ["1009", "1007", "1008"], keys(rows))
    # -- Recent Playlists ties back to the /playlists order (smart, then title) --------
    machine = (srv.json("/identity")[1] or {}).get("MediaContainer", {}).get("machineIdentifier")
    uri = urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/metadata/1007"
                             % machine, safe="")
    order = None
    for _attempt in range(3):
        made = []
        for title in ("zeta list", "alpha list", "mid list"):
            body = srv.json("/playlists?type=video&title=%s&smart=0&uri=%s"
                            % (urllib.parse.quote(title), uri), "POST")[1] or {}
            made.append(((body.get("MediaContainer") or {}).get("Metadata") or [{}])[0])
        stamps = {row.get("addedAt") for row in made}
        hubs = srv.container("/hubs?identifier=home.playlists").get("Hub") or [{}]
        order = [row.get("title") for row in hubs[0].get("Metadata") or []]
        if len(stamps) == 1:
            break
        for row in made:
            srv.raw("/playlists/%s" % row.get("ratingKey"), "DELETE")
    check("Recent Playlists made in one second keep the title order",
          order == ["alpha list", "mid list", "zeta list"], order)
    # -- the in-library search box lists episodes in (show, season, index) order -------
    srv.raw("/library/metadata/30001?index.value=41", "PUT")
    rows = srv.container("/library/sections/2/search?type=4&query=e").get("Metadata") or []
    check("an episode search lists a renumbered season's episode after the others",
          keys(rows)[-1:] == ["1001"] and len(rows) == 8, keys(rows))
    show = (srv.container("/library/metadata/20001").get("Metadata") or [{}])[0]
    movie = (srv.container("/library/metadata/1008").get("Metadata") or [{}])[0]
    check("...and both echo on the item's own detail read",
          show.get("episodeSort") == "1" and movie.get("useOriginalTitle") == "1",
          (show.get("episodeSort"), movie.get("useOriginalTitle")))

    # -- a flattened show's episodes do not roll up on Recently Added TV --------------
    srv.raw("/library/metadata/20001/prefs?flattenSeasons=1", "PUT")
    rows = srv.container("/hubs/home/recentlyAdded?type=2").get("Metadata") or []
    check("Recently Added TV lists a flattened show's episodes, not the show",
          [r.get("type") for r in rows] == ["episode"] * 8
          and keys(rows)[:3] == ["1011", "1010", "1006"], [(r.get("ratingKey"), r.get("type"))
                                                             for r in rows])
    srv.raw("/library/metadata/20001/prefs?flattenSeasons=-1", "PUT")
    rows = srv.container("/hubs/home/recentlyAdded?type=2").get("Metadata") or []
    check("...and rolls them up into the show again once it is not flattened",
          [(r.get("ratingKey"), r.get("type")) for r in rows] == [("20001", "show")],
          [(r.get("ratingKey"), r.get("type")) for r in rows])

    # -- an episode whose show has no poster lists its own still as the coverPoster ----
    srv.raw("/library/metadata/20001/unmatch", "PUT")
    episode = (srv.container("/library/metadata/1003").get("Metadata") or [{}])[0]
    check("after its show is unmatched an episode's still is its coverPoster",
          "grandparentThumb" not in episode
          and [image.get("type") for image in episode.get("Image") or []][:1] == ["coverPoster"],
          (episode.get("grandparentThumb"), episode.get("Image")))


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
        write_checks(srv)
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
