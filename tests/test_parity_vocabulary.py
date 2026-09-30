#!/usr/bin/env python3
"""The gaps a differential VOCABULARY sweep found (cycle 83).

`handover/tools/vsweep.py` appended every parameter name the oracle's source reads to
every request path Plex Web builds and diffed the oracle against the native server.
Each rule below was then measured on kids (1.43.4) and zen (1.42.2), and every check
asserts that measured answer on `tests/catalog_ui.json`, not "the two servers agree":

- the columns a listing REFUSES (`art`, `banner`, `theme`, `thumb`, `slug`, `extraType`,
  `parentIndex`, `userFields`) are a 500 wherever the filter grammar runs, and on a hub
  page's TV shelf on the hub-refusal terms (non-empty value, show library in scope);
- `guid=` is a grammar field everywhere: a case-sensitive prefix, comma is OR;
- the filter field table is the Meta's: `dovi` and `network` filter, `role` is no field;
- `/hubs/promoted` scoped to a movie library runs no TV filters;
- `/library/metadata/{id}/allLeaves` ignores `sort=`;
- the `include*` blocks reach a hub page's and `/common?id=`'s FULL rows;
- `includeGuids=1` never gives an unmatched `local://` row a Guid, and reaches
  `/grandchildren`;
- `X-Plex-Container-*` as QUERY parameters on `/folder` and `/recentlyViewedShows`.

    python3 tests/test_parity_vocabulary.py            # native
    PLEXSIM_SERVER=python python3 tests/test_parity_vocabulary.py
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
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-vocabulary",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}

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


def request(port, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS, Connection="close")
    sent.update(headers or {})
    try:
        connection.request("GET", path, headers=sent)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    return response.status, (body or {}).get("MediaContainer") if isinstance(body, dict) else None


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
            if request(port, "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def join(path, extra):
    return path + ("&" if "?" in path else "?") + extra


def keys(mc, element="Metadata"):
    return [row.get("ratingKey") or row.get("key") or row.get("title")
            for row in (mc or {}).get(element) or []]


def hub(mc, prefix):
    return next((h for h in (mc or {}).get("Hub") or []
                 if str(h.get("hubIdentifier", "")).startswith(prefix)), None)


# The consumers of the listing grammar that kids and zen refuse the refused columns on.
GRAMMAR_PATHS = [
    "/library/metadata/20001/children?excludeAllLeaves=1",
    "/library/metadata/20001/grandchildren",
    "/library/sections/1/genre",
    "/library/sections/1/year?type=1",
    "/library/sections/1/common?type=1&id=1007",
    "/library/sections/1/search?type=1&query=th",
    "/library/sections/1/autocomplete?type=1&title.query=th",
    "/library/sections/1/unwatched",
    "/library/sections/1/firstCharacter",
    "/library/sections/1/collection?type=1",
    "/library/sections/2/recentlyViewedShows",
]
REFUSED = ["art", "banner", "theme", "thumb", "slug", "extraType", "parentIndex", "userFields"]


def run(port):
    # 1. The refused columns: a 500 wherever the grammar runs.
    wrong = [(path, name, status) for path in GRAMMAR_PATHS for name in REFUSED
             for status in [request(port, join(path, name + "=1"))[0]] if status != 500]
    check("the refused columns are a 500 on every listing-grammar consumer", not wrong, wrong)
    base = [(path, request(port, path)[0]) for path in GRAMMAR_PATHS]
    check("...and each of those paths answers 200 without them",
          all(status == 200 for _path, status in base), base)
    status, _ = request(port, "/library/sections/1/extras/all?extraType=1")
    check("the section extras leaf still filters on extraType (200)", status == 200, status)

    # ...and on a hub page with a TV shelf, on the hub-refusal terms.
    hub_pages = ["/hubs", "/hubs/sections/2?count=12",
                 "/hubs/promoted?contentDirectoryID=2&count=12"]
    wrong = [(path, name, status) for path in hub_pages for name in REFUSED
             for status in [request(port, join(path, name + "=1"))[0]] if status != 500]
    check("the refused columns are a 500 on /hubs, a show hub page and show-scoped promoted",
          not wrong, wrong)
    lenient = [(path, name, status) for path in hub_pages for name in ("art", "userFields")
               for status in [request(port, join(path, name + "="))[0]] if status != 200]
    lenient += [("/hubs/sections/1", name, status) for name in REFUSED
                for status in [request(port, "/hubs/sections/1?count=12&%s=1" % name)[0]]
                if status != 200]
    check("...an empty value, and a movie library's hub page, are 200s", not lenient, lenient)

    # 2. guid= is a grammar field on every consumer: case-sensitive prefix, comma OR.
    sizes = {}
    for label, query in (("none", ""), ("1", "guid=1"), ("plex", "guid=plex"),
                         ("PLEX", "guid=PLEX")):
        for path in ("/library/sections/1/genre", "/library/metadata/20001/children",
                     "/library/sections/1/search?type=1&query=t"):
            _status, mc = request(port, join(path, query) if query else path)
            sizes[(label, path.split("?")[0])] = (mc or {}).get("size")
    expected = {}
    for path in ("/library/sections/1/genre", "/library/metadata/20001/children",
                 "/library/sections/1/search"):
        expected[("1", path)] = 0
        expected[("PLEX", path)] = 0
        expected[("plex", path)] = sizes[("none", path)]
    got = {key: sizes[key] for key in expected}
    check("guid= filters the axes, /children and /search as a case-sensitive prefix",
          got == expected and all(sizes[("none", p)] for _l, p in expected), sizes)
    _status, mc = request(port, "/hubs/sections/2?count=12&guid=1")
    shelf = hub(mc, "tv.recentlyadded.")
    check("guid=1 empties the show hub page's TV Recently Added shelf",
          shelf is not None and shelf.get("size") == 0, shelf and shelf.get("size"))
    pair = "plex://movie/614ee97b618cd92600cef3e1,plex://movie/60a69a74ba9733002ddf78f6"
    for path in ("/library/sections/1/all", "/library/all?type=1"):
        _status, mc = request(port, join(path, "guid=" + pair))
        check("a comma in guid= is OR on %s" % path, sorted(keys(mc)) == ["1012", "1013"],
              keys(mc))
    _status, mc = request(port, "/library/sections/1/all?guid%3C=plex://movie/520a1a3c")
    check("guid< (the <= key) is the same prefix", sorted(keys(mc)) == ["1007", "1008", "1009"],
          keys(mc))

    # 3. The field table is the Meta's: dovi and network filter, role is unknown.
    table = {}
    for path in ("/library/sections/1/all", "/library/sections/2/all",
                 "/library/sections/1/genre", "/library/metadata/20001/grandchildren"):
        element = "Directory" if path.endswith("genre") else "Metadata"
        whole = len(keys(request(port, path)[1], element))
        table[path] = (whole,) + tuple(
            len(keys(request(port, join(path, query))[1], element))
            for query in ("dovi=1", "network=1", "role=1"))
    check("dovi=1 and network=1 filter (to nothing here) and role=1 is every row",
          all(whole > 0 and dovi == 0 and network == 0 and role == whole
              for whole, dovi, network, role in table.values()), table)

    # 4. /hubs/promoted scoped to a movie library runs no TV filter.
    movie_only = "/hubs/promoted?contentDirectoryID=1&pinnedContentDirectoryID=1,2&count=12"
    got = {name: request(port, join(movie_only, name))[0]
           for name in ("extraType=1", "parentIndex=1", "resolution=1", "art=1")}
    check("movie-scoped /hubs/promoted answers 200 to TV-shelf refusals",
          set(got.values()) == {200}, got)

    # 5. allLeaves ignores sort=.
    plain = keys(request(port, "/library/metadata/20001/allLeaves")[1])
    got = {sort: (status, keys(mc)) for sort in ("1", "zzz", "title:desc", "")
           for status, mc in [request(port, "/library/metadata/20001/allLeaves?sort=" + sort)]}
    check("allLeaves ignores sort= (200, broadcast order)",
          plain and all(status == 200 and order == plain for status, order in got.values()),
          got)

    # 6. include* blocks on a hub page's and /common?id='s FULL rows.
    blocks = {"includeChildren": "Children", "includeExtras": "Extras",
              "includeOnDeck": "OnDeck", "includePreferences": "Preferences",
              "includeRelated": "Related"}
    got = {}
    for option, element in blocks.items():
        _status, mc = request(port, "/hubs/sections/2?count=12&%s=1" % option)
        shelf = hub(mc, "tv.recentlyadded.") or {}
        rows = shelf.get("Metadata") or []
        light = [row for h in (mc or {}).get("Hub") or [] if h is not shelf
                 for row in h.get("Metadata") or []]
        got[option] = (bool(rows) and all(element in row for row in rows),
                       any(element in row for row in light))
    check("each include* block reaches the show hub page's full rows and no light row",
          all(value == (True, False) for value in got.values()), got)
    got = {}
    for option, element in blocks.items():
        _status, mc = request(port, "/library/sections/1/common?type=1&id=1007&%s=1" % option)
        got[element] = element in ((mc or {}).get("Metadata") or [{}])[0]
    check("a movie's /common?id= row takes Extras and Related and nothing else",
          got == {"Children": False, "Extras": True, "OnDeck": False, "Preferences": False,
                  "Related": True}, got)
    got = {}
    for option, element in blocks.items():
        _status, mc = request(port, "/library/sections/2/common?type=2&id=20001&%s=1" % option)
        got[element] = element in ((mc or {}).get("Metadata") or [{}])[0]
    check("a show's /common?id= row takes Extras and Related and nothing else",
          got == {"Children": False, "Extras": True, "OnDeck": False, "Preferences": False,
                  "Related": True}, got)

    # 7. includeGuids: never on a local:// row, and it reaches /grandchildren.
    _status, mc = request(port, "/hubs/sections/1?count=12&includeGuids=1")
    rows = {row.get("ratingKey"): row.get("Guid") for h in (mc or {}).get("Hub") or []
            for row in h.get("Metadata") or []}
    check("a hub page's local:// row carries no Guid under includeGuids=1",
          "1014" in rows and rows["1014"] is None and rows.get("1013"), rows)
    _status, mc = request(port, "/library/sections/1/all?includeGuids=1")
    rows = {row.get("ratingKey"): row.get("Guid") for row in (mc or {}).get("Metadata") or []}
    check("a grid's local:// row carries no Guid under includeGuids=1",
          "1014" in rows and rows["1014"] is None and rows.get("1012"), rows)
    _status, mc = request(port, "/library/metadata/20001/grandchildren?includeGuids=1")
    rows = [(row.get("guid"), row.get("Guid")) for row in (mc or {}).get("Metadata") or []]
    check("/grandchildren honours includeGuids=1",
          rows and all(guids == [{"id": guid}] for guid, guids in rows), rows)
    _status, mc = request(port, "/library/metadata/20001/grandchildren")
    check("...and carries no Guid without it",
          not any("Guid" in row for row in (mc or {}).get("Metadata") or []), mc)

    # 8. Query-parameter paging on /folder and /recentlyViewedShows.
    def window(path):
        status, mc = request(port, path)
        mc = mc or {}
        return (status, mc.get("size"), mc.get("offset"), mc.get("totalSize"))
    folder = "/library/sections/1/folder"
    got = {query: window(join(folder, query)) for query in
           ("X-Plex-Container-Size=1", "X-Plex-Container-Start=1&X-Plex-Container-Size=1",
            "X-Plex-Container-Start=abc&X-Plex-Container-Size=2")}
    check("/folder pages only on Start AND Size as query parameters",
          got == {"X-Plex-Container-Size=1": (200, 6, None, None),
                  "X-Plex-Container-Start=1&X-Plex-Container-Size=1": (200, 1, 1, 6),
                  "X-Plex-Container-Start=abc&X-Plex-Container-Size=2":
                      (500, None, None, None)}, got)
    shows = "/library/sections/2/recentlyViewedShows"
    got = {query: window(join(shows, query) if query else shows) for query in
           ("", "X-Plex-Container-Size=1", "X-Plex-Container-Start=1",
            "X-Plex-Container-Start=1&X-Plex-Container-Size=1",
            "X-Plex-Container-Start=1&X-Plex-Container-Size=abc")}
    check("/recentlyViewedShows: a default window, a lone Size unpaged, a lone Start ignored",
          got == {"": (200, 1, 0, 1), "X-Plex-Container-Size=1": (200, 1, None, None),
                  "X-Plex-Container-Start=1": (200, 1, 0, 1),
                  "X-Plex-Container-Start=1&X-Plex-Container-Size=1": (200, 0, 1, 1),
                  "X-Plex-Container-Start=1&X-Plex-Container-Size=abc":
                      (500, None, None, None)}, got)

    # 9. A scope that names no relative of the row reads the row itself.
    got = {}
    for query in ("episode.title=Tuesday", "show.title=Tuesday", "season.title=Tuesday",
                  "artist.title=Tuesday", "movie.genre%21=1"):
        got[query] = keys(request(port, "/library/sections/1/all?" + query)[1])
    whole = keys(request(port, "/library/sections/1/all")[1])
    status, _ = request(port, "/library/sections/1/all?episode.addedAt=")
    check("a scope naming no relative of a movie reads the movie itself",
          all(got[q] == ["1013"] for q in list(got)[:4])
          and sorted(got["movie.genre%21=1"]) == sorted(whole) and len(whole) == 6
          and status == 500, (got, status))

    # 10. unwatchedLeaves is the Meta's boolean: true for the literal 1 only.
    got = {value: (lambda r: (r[0], len(keys(r[1]))))(
        request(port, "/library/sections/1/all?unwatchedLeaves=" + value))
        for value in ("", "abc", "-1", "1,2", "0", "1")}
    check("unwatchedLeaves on a movie grid: every row unless the value is 1",
          got == {"": (200, 6), "abc": (200, 6), "-1": (200, 6), "1,2": (200, 6),
                  "0": (200, 6), "1": (200, 0)}, got)

    # 11. An include* option turns on for the integer ONE only.
    got = {}
    for value in ("-1", "2", "01", "%2B1", "0"):
        _status, mc = request(port, "/hubs/sections/2?count=12&includeExtras=%s"
                              "&includeRelated=%s&includeChildren=%s&includeOnDeck=%s"
                              % ((value,) * 4))
        rows = (hub(mc, "tv.recentlyadded.") or {}).get("Metadata") or [{}]
        got[value] = sorted(k for k in ("Extras", "Related", "Children", "OnDeck")
                            if k in rows[0])
    on = ["Children", "Extras", "OnDeck", "Related"]
    check("include* blocks: 01 and +1 turn them on, -1, 2 and 0 do not",
          got == {"-1": [], "2": [], "01": on, "%2B1": on, "0": []}, got)

    # 12. A tag-table axis under a leaf-count filter is empty; a column axis filters.
    def entries(path):
        status, mc = request(port, path)
        return status, len((mc or {}).get("Directory") or [])
    got = {path: entries(path) for path in (
        "/library/sections/1/genre?unwatchedLeaves=0",
        "/library/sections/1/director?type=1&viewedLeafCount=0",
        "/library/sections/2/genre?type=2&unwatchedLeaves=1",
        "/library/sections/1/actor?type=1&unviewedLeafCount%3E%3E=0",
        "/library/sections/1/year?type=1&unwatchedLeaves=0",
        "/library/sections/1/genre")}
    check("leaf-count filters empty the tag axes and leave the year axis whole",
          got == {"/library/sections/1/genre?unwatchedLeaves=0": (200, 0),
                  "/library/sections/1/director?type=1&viewedLeafCount=0": (200, 0),
                  "/library/sections/2/genre?type=2&unwatchedLeaves=1": (200, 0),
                  "/library/sections/1/actor?type=1&unviewedLeafCount%3E%3E=0": (200, 0),
                  "/library/sections/1/year?type=1&unwatchedLeaves=0": (200, 4),
                  "/library/sections/1/genre": (200, 6)}, got)

    # 13. An empty sort= is ignored on /search, /children and /grandchildren.
    got = {path: request(port, path)[0] for path in (
        "/library/sections/1/search?type=1&query=t&sort=",
        "/library/metadata/20001/children?sort=",
        "/library/metadata/20001/grandchildren?sort=",
        "/library/metadata/20001/children?sort=zzz")}
    check("an empty sort= is a 200 on search, children and grandchildren; zzz still 500s",
          list(got.values()) == [200, 200, 200, 500], got)

    # 14. location= is the Meta's tag, compared as a section Location id.
    got = {value: (lambda r: (r[0], len(keys(r[1]))))(
        request(port, "/library/sections/1/all?location=" + value))
        for value in ("", "-1", "1,2", "1", "abc")}
    check("location=: empty and abc 500, -1 none, 1 and 1,2 every row",
          got == {"": (500, 0), "-1": (200, 0), "1,2": (200, 6), "1": (200, 6),
                  "abc": (500, 0)}, got)


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
    print("PASS %d/%d vocabulary parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
