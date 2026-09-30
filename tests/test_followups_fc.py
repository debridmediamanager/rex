#!/usr/bin/env python3
"""Two HANDOVER §2a follow-ups, each against its measurement:

- a collection built from a SECTION uri holds the section's rows at the collection's own
  level (`type=2` the shows, `3` the seasons, `4` the episodes), never the episodes a play
  queue would expand them into; its `type` is an integer that names the members' type
  whatever the uri holds (`tests/fixtures/collection_section_uri.json`, plexscratch42);
- `X-Plex-Container-Start` / `-Size` request HEADERS win over the query parameters of the
  same name, per parameter, on every paged route family
  (`tests/fixtures/paging_header_precedence.json`, kids, zen and plexscratch42);
Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_fc.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURES = os.path.join(HERE, "fixtures")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY", "PLEXSIM_GDM_PORTS"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "followups-fc",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}
START, SIZE = "X-Plex-Container-Start", "X-Plex-Container-Size"
TYPE_NAMES = {1: "movie", 2: "show", 3: "season", 4: "episode"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def free_port(kind=socket.SOCK_STREAM):
    probe = socket.socket(socket.AF_INET, kind)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS, Connection="close")
    sent.update(headers or {})
    try:
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        head = {name.lower(): value for name, value in response.getheaders()}
    finally:
        connection.close()
    try:
        return response.status, (json.loads(raw) if raw else None), head
    except ValueError:
        return response.status, raw, head


def mc(port, path, method="GET", headers=None):
    status, body, _ = request(port, method, path, headers)
    return (body or {}).get("MediaContainer", {}) if isinstance(body, dict) else {}


def keys(container):
    return [str(row.get("ratingKey")) for row in container.get("Metadata") or []]


def start(extra=(), env=None):
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       *(extra or ("--gdm", "off")), root=ROOT),
        cwd=ROOT, env=dict(ENV, **(env or {})),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
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


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


# -- 1. a collection over a section uri ----------------------------------------------

def uri_parts(uri):
    """(`section`, section id, query) or (`metadata`, [keys], "") or (None, None, "")."""
    if not uri:
        return None, None, ""
    path, _, query = urllib.parse.unquote(uri).partition("?")
    if path.startswith("/library/sections/") and path.endswith("/all"):
        return "section", path.split("/")[3], query
    if path.startswith("/library/metadata/"):
        return "metadata", path.split("/")[3].split(","), ""
    return None, None, ""


def level(cell, section_types):
    raw = cell.get("type")
    if raw is None or raw.lstrip("+-").isdigit() and int(raw) < 0:
        return {"movie": 1, "show": 2}[section_types[cell["sectionId"]]]
    return int(raw)


def filter_part(query):
    return "&".join(pair for pair in query.split("&") if pair and not pair.startswith("type="))


def collections(port, fixture, show, episode, movie, show_title, movie_title):
    machine = mc(port, "/identity").get("machineIdentifier")

    def uri(path):
        return urllib.parse.quote("server://%s/com.plexapp.plugins.library%s" % (machine, path),
                                  safe="")

    # The reference's sections 2 and 3 are show libraries, 1 a movie one; here 2 and 1.
    ref_types = {"1": "movie", "2": "show", "3": "show"}
    sim_section = {"1": "1", "2": "2", "3": "2"}
    ref_keys = {"15": show, "21": episode}                    # 391 has no counterpart here
    ref_titles = {"Twofold": show_title, "Elephants Dream": movie_title}

    def listing(section, number, query):
        extra = filter_part(query)
        return keys(mc(port, "/library/sections/%s/all?type=%d%s"
                       % (section, number, "&" + extra if extra else "")))

    def create(params):
        status, body, _ = request(port, "POST", "/library/collections?title=fc&smart=0&" + params)
        row = ((body or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0] \
            if isinstance(body, dict) else {}
        children = None
        if row.get("ratingKey"):
            children = keys(mc(port, "/library/collections/%s/children" % row["ratingKey"]))
        return status, row, children

    def delete(row):
        if row.get("ratingKey"):
            request(port, "DELETE", "/library/collections/%s" % row["ratingKey"])

    listings = fixture["listings"]
    for cell in fixture["cells"]:
        kind, named, query = uri_parts(cell.get("uri"))
        label = "collection %s" % cell["request"].split("?", 1)[1]
        # The fixture holds its own rule: a section uri of the collection's library is
        # read at the collection's level with the uri's filters, whatever its `type=`.
        if cell["status"] == 200 and kind == "section" and cell.get("children") is not None:
            number = level(cell, ref_types)
            fits = named == cell["sectionId"] and number in (
                (1,) if ref_types[named] == "movie" else (2, 3, 4))
            extra = filter_part(query)
            wanted = listings.get("%s?type=%d%s" % (named, number, "&" + extra if extra else "")) \
                if fits else []
            if wanted is not None:
                got = sorted((key for key, _ in cell["children"]), key=int)
                check("fixture: %s holds the library's rows at its level" % label,
                      got == wanted, "%s != %s" % (got, wanted))
        # Replay it here, translated to this catalog.
        section = sim_section[cell["sectionId"]]
        params = ["sectionId=%s" % section]
        if cell.get("type") is not None:
            params.insert(0, "type=%s" % urllib.parse.quote(cell["type"], safe=""))
        expected = None
        if kind == "section":
            if named != cell["sectionId"]:
                continue                        # another show library: mirrored below
            for old, new in ref_titles.items():
                query = query.replace("=" + old, "=" + urllib.parse.quote(new))
            params.append("uri=" + uri("/library/sections/%s/all%s"
                                       % (section, "?" + query if query else "")))
            if cell.get("children") is not None:
                expected = listing(section, level(cell, ref_types), query) \
                    if cell["children"] else []
        elif kind == "metadata":
            if any(key not in ref_keys and key != "391" for key in named):
                continue                        # a row of another show library: mirrored
            params.append("uri=" + uri("/library/metadata/%s" % ",".join(
                ref_keys[key] for key in named if key in ref_keys)))
            if cell.get("children") is not None:
                expected = [ref_keys[k] for k, _ in cell["children"] if k in ref_keys]
        elif cell.get("children") is not None:
            expected = []
        status, row, children = create("&".join(params))
        wanted = {"status": cell["status"], "subtype": cell.get("subtype")}
        got = {"status": status, "subtype": row.get("subtype")}
        if expected is not None:
            wanted["children"] = sorted(expected)
            got["children"] = sorted(children or [])
        check("%s -> %s" % (label, wanted), got == wanted, got)
        for add in cell.get("adds") or []:
            _, _, add_query = uri_parts(add["uri"])
            for old, new in ref_titles.items():
                add_query = add_query.replace("=" + old, "=" + urllib.parse.quote(new))
            before = set(children or [])
            add_status = request(port, "PUT", "/library/collections/%s/items?uri=%s" % (
                row.get("ratingKey"), uri("/library/sections/%s/all%s"
                                          % (section, "?" + add_query if add_query else ""))))[0]
            after = set(keys(mc(port, "/library/collections/%s/children" % row.get("ratingKey"))))
            wanted_add = before | set(listing(section, level(cell, ref_types), add_query))
            check("collection add %s into a %s collection holds its level's rows"
                  % (add["uri"], row.get("subtype")),
                  add_status == add["status"] and after == wanted_add,
                  "HTTP %s, %s != %s" % (add_status, sorted(after), sorted(wanted_add)))
        delete(row)

    # The cross-library cells, mirrored: the reference took nothing from another library
    # (`type=2&sectionId=3` over section 2's show, over its key, and over section 2's
    # uri). This catalog has one show library, so the same rule is driven the other way:
    # a movie collection in the show library over a movie's key and the movie library.
    for label, spec in (("over another library's movie", "/library/metadata/%s" % movie),
                        ("over another library's section", "/library/sections/1/all")):
        status, row, children = create("type=1&sectionId=2&uri=" + uri(spec))
        check("a movie collection in the show library %s holds nothing" % label,
              (status, row.get("subtype"), children) == (200, "movie", []),
              (status, row.get("subtype"), children))
        delete(row)


# -- 2. paging headers win over the query -------------------------------------------

def paging(port, fixture, show, movie, collection, playlist):
    # The fixture's own rule, cell by cell: the header wins per parameter.
    for cell in fixture["cells"]:
        query, headers = cell["query"], cell["headers"]
        if "abc" in headers.values():
            ok = cell["status"] == 500
        else:
            start = int(headers.get(START, query.get(START)))
            ok = (cell["status"] == 200 and cell["offset"] == start
                  and cell["h_start"] == str(start))
        if not ok:
            check("fixture: %s %s q%s h%s" % (cell["ref"], cell["path"], query, headers),
                  False, cell)
    families = sorted({cell["path"] for cell in fixture["cells"]})
    check("fixture: the measured families cover listings, children, hubs and lists",
          {"/library/sections/1/all", "/library/metadata/6/children", "/hubs",
           "/playlists", "/library/sections/1/collections"} <= set(families), families)
    here = ["/library/sections/1/all", "/library/sections/2/all?type=4",
            "/library/metadata/%s/children" % show, "/library/metadata/%s/allLeaves" % show,
            "/hubs", "/hubs/sections/1", "/library/recentlyAdded", "/library/onDeck",
            "/library/sections", "/hubs/search?query=tears", "/status/sessions/history/all",
            "/:/prefs", "/library/metadata/%s" % movie, "/identity", "/playlists",
            "/library/sections/1/collections", "/library/collections/%s/children" % collection,
            "/playlists/%s/items" % playlist, "/library/all?type=18"]
    forms = [({START: "0", SIZE: "2"}, {START: "1", SIZE: "1"}),
             ({}, {START: "1", SIZE: "1"}),
             ({START: "0", SIZE: "2"}, {SIZE: "1"}),
             ({START: "0", SIZE: "2"}, {START: "1"}),
             ({START: "1", SIZE: "1"}, {START: "0", SIZE: "3"}),
             ({START: "0", SIZE: "2"}, {START: "abc"})]
    for path in here:
        for query, headers in forms:
            sep = "&" if "?" in path else "?"
            target = path + sep + urllib.parse.urlencode(query) if query else path
            status, body, head = request(port, "GET", target, headers)
            label = "paging %s q%s h%s" % (path, query, headers)
            if "abc" in headers.values():
                check(label + " is a 500", status == 500, "HTTP %s" % status)
                continue
            start = int(headers.get(START, query.get(START)))
            size = int(headers.get(SIZE, query.get(SIZE)))
            box = (body or {}).get("MediaContainer", {}) if isinstance(body, dict) else {}
            total = box.get("totalSize")
            # A show's "All episodes" Directory rides OUTSIDE the page (protocol-traps).
            rows = len(box["Metadata"]) if "Metadata" in box else \
                sum(len(v) for v in box.values() if isinstance(v, list))
            wanted = (200, start, str(start), min(size, max(0, (total or 0) - start)))
            got = (status, box.get("offset"), head.get("x-plex-container-start"), rows)
            check(label + " answers the header's window", got == wanted,
                  "%s != %s (totalSize %s)" % (got, wanted, total))


def main():
    process, port = start()
    try:
        show_row = (mc(port, "/library/sections/2/all").get("Metadata") or [{}])[0]
        show, show_title = str(show_row.get("ratingKey")), show_row.get("title") or ""
        seasons = keys(mc(port, "/library/metadata/%s/children" % show))
        seasons = [key for key in seasons if key != show]
        episodes = keys(mc(port, "/library/sections/2/all?type=4"))
        movie_row = (mc(port, "/library/sections/1/all").get("Metadata") or [{}])[0]
        movie, movie_title = str(movie_row.get("ratingKey")), movie_row.get("title") or ""

        with open(os.path.join(FIXTURES, "collection_section_uri.json")) as handle:
            collections(port, json.load(handle), show, episodes[0], movie, show_title,
                        movie_title)

        machine = mc(port, "/identity").get("machineIdentifier")
        movies = keys(mc(port, "/library/sections/1/all"))
        made = []
        for index in range(3):
            made.append(keys(mc(port, "/library/collections?type=1&smart=0&sectionId=1"
                                "&title=fcpage%d&uri=%s" % (index, urllib.parse.quote(
                                    "server://%s/com.plexapp.plugins.library/library/metadata/%s"
                                    % (machine, ",".join(movies[:3])), safe="")), "POST"))[0])
            keys(mc(port, "/playlists?type=video&smart=0&title=fcpage%d&uri=%s" % (
                index, urllib.parse.quote(
                    "server://%s/com.plexapp.plugins.library/library/metadata/%s"
                    % (machine, ",".join(movies[:3])), safe="")), "POST"))
        playlist = keys(mc(port, "/playlists"))[0]
        with open(os.path.join(FIXTURES, "paging_header_precedence.json")) as handle:
            paging(port, json.load(handle), show, movie, made[0], playlist)

    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
