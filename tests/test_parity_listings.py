#!/usr/bin/env python3
"""Library listing parity: the section containers, leaves and rows as the Python oracle built them.

Each check asserts what the Python oracle answers (itself measured against a real PMS)
for one request of the library-listing family -- `/library/sections`, a section's
leaves (`sorts`, `filters`, `folder`, `{tag}/top`, `arts`, `allLeaves`, `albums`,
`moment`, `computePath`, `onDeck`, ...), `/library/all`, `/library/shared/*` and the
double-slash pass-through routes -- found by diffing the native server's answers
against the oracle's over the whole UI suite.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_parity_listings.py
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
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-listings",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}
MOVIE_ART, MOVIE_THUMB = "/:/resources/movie-fanart.jpg", "/:/resources/movie.png"
SHOW_ART, SHOW_THUMB = "/:/resources/show-fanart.jpg", "/:/resources/show.png"
LIBRARY_TRIPLE = {"identifier": "com.plexapp.plugins.library",
                  "mediaTagPrefix": "/system/bundle/media/flags/",
                  "mediaTagVersion": 1655122614}

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
            break
        try:
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def request(port, method, path, headers=None, body=None, keep_alive=False):
    """(status, {lower header: value}, parsed JSON or raw bytes)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS)
    sent.update(headers or {})
    if not keep_alive:
        sent["Connection"] = "close"
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        head = {name.lower(): value for name, value in response.getheaders()}
    finally:
        connection.close()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw
    return response.status, head, parsed


def mc(port, path, method="GET"):
    status, _, body = request(port, method, path)
    return (body or {}).get("MediaContainer", {}) if status == 200 and isinstance(body, dict) \
        else {"_status": status}


def has(container, **expected):
    wrong = {k: (container.get(k, "<absent>"), v) for k, v in expected.items()
             if container.get(k, "<absent>") != v}
    return not wrong, wrong


def lacks(container, *names):
    present = [name for name in names if name in container]
    return not present, present


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
    print("PASS %d/%d listing parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


def run(port):
    # -- containers --------------------------------------------------------------
    sections = mc(port, "/library/sections")
    ok, bad = lacks(sections, "identifier", "mediaTagPrefix", "mediaTagVersion")
    check("/library/sections carries title1 and allowSync only, no library triple",
          ok and sections.get("allowSync") is False and sections.get("title1") == "Plex Library",
          bad or sections)
    ok, bad = lacks(mc(port, "/library/sections/all"), "identifier", "mediaTagPrefix")
    check("/library/sections/all is the same bare container", ok, bad)

    root = mc(port, "/library")
    ok, bad = has(root, size=3, allowSync=False, art="/:/resources/library-art.png", content="",
                  title1="Plex Library", title2="", **LIBRARY_TRIPLE)
    check("/library carries the library-art secondary pair and the library triple", ok, bad)
    for path in ("/library//", "/library/sections/1//"):
        doubled = mc(port, path)
        parent = mc(port, path[:-2])
        check("%s answers its parent's own container" % path,
              doubled == parent and doubled.get("size"), (doubled, parent))

    for leaf in ("sorts", "filters"):
        for section, art, thumb, title in (("1", MOVIE_ART, MOVIE_THUMB, "Debrid Movies"),
                                           ("2", SHOW_ART, SHOW_THUMB, "Debrid TV")):
            found = mc(port, "/library/sections/%s/%s" % (section, leaf))
            expected = dict(allowSync=False, art=art, thumb=thumb, content="secondary",
                            title1=title, viewGroup="secondary", viewMode=65592,
                            **LIBRARY_TRIPLE)
            if section == "2":
                expected["nocache"] = True
            ok, bad = has(found, **expected)
            check("/library/sections/%s/%s is the secondary container" % (section, leaf), ok, bad)

    folder = mc(port, "/library/sections/1/folder")
    ok, bad = has(folder, allowSync=True, art=MOVIE_ART, thumb=MOVIE_THUMB, content="secondary",
                  title1="Debrid Movies", title2="By Folder", viewGroup="secondary",
                  viewMode=65592, librarySectionID=1, **LIBRARY_TRIPLE)
    check("/folder is the secondary container titled By Folder", ok, bad)

    arts = mc(port, "/library/sections/1/arts")
    ok, bad = has(arts, allowSync=False, art=MOVIE_ART, thumb=MOVIE_THUMB,
                  title1="Debrid Movies", viewGroup="movie", viewMode=65592, **LIBRARY_TRIPLE)
    check("/arts is the library's container", ok and arts.get("size") == 6, bad or arts)
    show_arts = mc(port, "/library/sections/2/arts")
    ok, bad = has(show_arts, viewGroup="episode", nocache=True, art=SHOW_ART)
    check("a show library's /arts says viewGroup episode and nocache", ok, bad)

    for leaf in ("albums", "moment", "allLeaves"):
        found = mc(port, "/library/sections/1/%s" % leaf)
        ok, bad = has(found, art=MOVIE_ART, thumb=MOVIE_THUMB, viewMode=65592, **LIBRARY_TRIPLE)
        check("/%s carries the resource art and thumb" % leaf, ok, bad)
    path = mc(port, "/library/sections/1/computePath?startID=1007&endID=1007")
    ok, bad = has(path, size=0, art=MOVIE_ART, thumb=MOVIE_THUMB, title1="Debrid Movies")
    ok2, bad2 = lacks(path, "viewMode", "allowSync", "nocache")
    check("/computePath carries art and thumb and neither viewMode nor allowSync",
          ok and ok2, (bad, bad2))

    for section, view_group, art in (("1", "movie", MOVIE_ART), ("2", "episode", SHOW_ART)):
        deck = mc(port, "/library/sections/%s/onDeck" % section)
        expected = dict(art=art, librarySectionID=int(section), title2="On Deck",
                        viewGroup=view_group, viewMode=65592, allowSync=True, **LIBRARY_TRIPLE)
        if section == "2":
            expected.update(mixedParents=True, nocache=True)
        ok, bad = has(deck, **expected)
        check("/library/sections/%s/onDeck is the listing container" % section, ok, bad)

    # -- sort= ----------------------------------------------------------------------------
    import zlib
    keys = [r["ratingKey"] for r in mc(port, "/library/sections/1/all").get("Metadata") or []]
    expected = sorted(keys, key=lambda k: zlib.crc32(("12345/%s" % k).encode()))
    seeded = [r["ratingKey"] for r in
              mc(port, "/library/sections/1/all?sort=random%3A12345").get("Metadata") or []]
    check("a seeded random sort orders by CRC-32 of <seed>/<ratingKey>",
          seeded == expected and len(keys) > 2, (seeded, expected))
    backwards = [r["ratingKey"] for r in
                 mc(port, "/library/sections/1/all?sort=random:12345:desc").get("Metadata") or []]
    check("random:<seed>:desc is that order reversed", backwards == expected[::-1], backwards)
    prefixed = [r["ratingKey"] for r in mc(
        port, "/library/sections/1/all?type=1&sort=movie.random%3A99").get("Metadata") or []]
    check("a type-prefixed random key takes a seed too",
          prefixed == sorted(keys, key=lambda k: zlib.crc32(("99/%s" % k).encode())), prefixed)
    episodes = mc(port, "/library/sections/2/all?type=4&sort=episode.index:desc").get("Metadata") or []
    order = [(r.get("index"), r.get("parentIndex")) for r in episodes]
    check("an episode sort breaks ties in broadcast order, not by title",
          order == sorted(order, key=lambda pair: (-(pair[0] or 0), pair[1] or 0))
          and len(order) > 3, order)

    # -- type= and group= ---------------------------------------------------------------
    both = mc(port, "/library/sections/1/all?type=2&type=1")
    ok, bad = has(both, size=0, allowSync=True, art=MOVIE_ART, librarySectionID=1,
                  title2="All Debrid Movies", viewGroup="movie", viewMode=65592)
    check("two different types select nothing, in the ordinary listing container",
          ok and not both.get("Metadata"), bad or both)
    same = mc(port, "/library/sections/1/all?type=1&type=1")
    check("the same type twice is that type", len(same.get("Metadata") or []) == 6,
          same.get("size"))
    grouped = mc(port, "/library/sections/2/all?type=4&group=season.index").get("Metadata") or []
    seasons = {r.get("parentRatingKey") for r in
               mc(port, "/library/sections/2/all?type=4").get("Metadata") or []}
    check("a prefixed group= reads the season's index: one episode per season",
          len(grouped) == len(seasons) == 4, [r.get("ratingKey") for r in grouped])

    # -- excludeElements=Part -------------------------------------------------------------
    rows = mc(port, "/library/sections/1/all?excludeElements=Part").get("Metadata") or []
    counts = [m.get("partCount") for r in rows for m in r.get("Media") or []]
    check("excludeElements=Part counts each Media's Parts on a section listing",
          counts and all(count == 1 for count in counts)
          and not any("Part" in m for r in rows for m in r.get("Media") or []), counts)

    # -- season leaf counts ---------------------------------------------------------------
    bucket = mc(port, "/library/sections/2/firstCharacter/%23?type=3").get("Metadata") or []
    check("a season in a firstCharacter bucket keeps its leaf counts",
          bucket and all("leafCount" in r and "viewedLeafCount" in r for r in bucket),
          [(r.get("ratingKey"), r.get("leafCount")) for r in bucket])
    grid = mc(port, "/library/sections/2/all?type=3").get("Metadata") or []
    check("a season in the type=3 grid carries no leaf counts",
          grid and not any("leafCount" in r for r in grid), grid[:1])

    # -- the tag tables ------------------------------------------------------------
    genre = mc(port, "/library/sections/1/genre/top")
    rows = genre.get("Directory") or []
    ok, bad = has(genre, art=MOVIE_ART, thumb=MOVIE_THUMB, **LIBRARY_TRIPLE)
    check("/genre/top is the library container", ok, bad)
    check("/genre/top rows keep the tag's integer id and carry no title",
          rows and all(isinstance(r.get("id"), int) and "title" not in r for r in rows),
          rows[:2])
    check("/genre/top rows are ascending by id",
          [r.get("id") for r in rows] == sorted(r.get("id") for r in rows), rows)
    label = mc(port, "/library/sections/1/label/top")
    ok, bad = has(label, size=0, **LIBRARY_TRIPLE)
    check("an untyped /top (label) is an empty library container", ok, bad)
    for name, status in (("decade", 200), ("network", 200), ("unwatched", 200),
                         ("audioLanguage", 200), ("platform", 404), ("zzz", 404),
                         ("thumb", 404)):
        got = request(port, "GET", "/library/sections/1/%s/top" % name)[0]
        check("/%s/top answers %d" % (name, status), got == status, got)
    resolution = mc(port, "/library/sections/1/resolution/top")
    check("/resolution/top is the resolution bucket, the whole library",
          len(resolution.get("Metadata") or []) == 6, resolution.get("size"))

    # -- a show's stored listing columns -------------------------------------------------
    summary = next(r for r in mc(port, "/library/sections?includeSummary=1")["Directory"]
                   if r.get("type") == "show")["Summary"]
    check("a show library's Summary counts the show's own stored duration",
          summary.get("duration") == 9 * 1311769, summary)
    before = (mc(port, "/library/metadata/20001").get("Metadata") or [{}])[0]
    request(port, "PUT", "/library/metadata/1001?type=4&id=1001"
            "&originallyAvailableAt.value=1990-01-01&originallyAvailableAt.locked=1")
    after = (mc(port, "/library/metadata/20001").get("Metadata") or [{}])[0]
    check("a show's first air date is stored at load, not recomputed from its episodes",
          before.get("originallyAvailableAt") == after.get("originallyAvailableAt")
          == "2007-12-16", (before.get("originallyAvailableAt"),
                            after.get("originallyAvailableAt")))

    # -- /library/shared -------------------------------------------------------------
    shared = mc(port, "/library/shared/all")
    first = (shared.get("Metadata") or [{}])[0]
    check("a shared library row keeps the section row's attribute order",
          list(first)[:3] == ["art", "key", "thumb"]
          and list(first).index("contentChangedAt") > list(first).index("directory"),
          list(first))
    status, head, xml = request(port, "GET", "/library/shared/all",
                                {"Accept": "application/xml"})
    check("a shared library row renders as <Directory>",
          b"<Directory art=" in xml and b"<Video " not in xml, xml[:300])
    shared = mc(port, "/library/shared/all?includePreferences=1")
    show_row = next(r for r in shared.get("Metadata") or [] if r.get("type") == "show")
    sort = next(s for s in show_row["Preferences"]["Setting"] if s.get("id") == "episodeSort")
    check("a shared show library's episodeSort is the per-show variant",
          sort.get("summary") == "How to sort the episodes for this show."
          and sort.get("enumValues") == "-1:Library default|0:Oldest first|1:Newest first",
          sort)

    # -- Add Library -----------------------------------------------------------------
    form = ("name=parity+probe&type=movie&agent=tv.plex.agents.movie&scanner=Plex+Movie"
            "&language=en-US&location=%2Fdata%2Fprobe")
    status, head, body = request(port, "POST", "/library/sections?" + form)
    row = ((body or {}).get("MediaContainer") or {}).get("Directory", [{}])[0] \
        if isinstance(body, dict) else {}
    check("Add Library answers 201 naming the library in Location",
          status == 201 and head.get("location") == "/library/sections/3", (status, head))
    check("the created row's uuid is the CRC-32 of name-type, its Location the form's",
          row.get("uuid") == "000000000000000000000000%08x" % __import__("zlib").crc32(
              b"parity probe-movie")
          and row.get("Location") == [{"id": 1, "path": "/data/probe"}], row)
    # The measured echo (plexscratch42, W4's `ok-movie` and W5's music create): no
    # `refreshing`, and the group and the two create stamps before `content`.
    check("the created row is the bare create projection",
          [k for k in row if k != "Location"] == [
              "key", "type", "title", "agent", "scanner", "language",
              "metadataAgentProviderGroupId", "uuid", "updatedAt", "createdAt",
              "content", "directory", "contentChangedAt", "hidden"]
          and "identifier" not in (body or {}).get("MediaContainer", {}), row)
    status, head, xml = request(port, "POST", "/library/sections?" + form.replace(
        "parity+probe", "parity+xml"), {"Accept": "application/xml"})
    check("Add Library answers XML to an XML client",
          status == 201 and head.get("content-type") == "text/xml;charset=utf-8"
          and xml.startswith(b"<?xml"), (status, head))
    for label, bad in (("an unknown numeric type", "type=9"),
                       ("an unknown language", "language=zz-ZZ"),
                       ("a missing location", "location=")):
        name, value = bad.split("=")
        pairs = [p for p in form.split("&") if not p.startswith(name + "=")]
        if value:
            pairs.append(bad)
        got = request(port, "POST", "/library/sections?" + "&".join(pairs))[0]
        check("Add Library refuses %s with a 400" % label, got == 400, got)
    got = request(port, "POST", "/library/sections?" + form.replace(
        "type=movie", "type=1").replace("tv.plex.agents.movie", "tv.plex.agents.none")
        .replace("parity+probe", "parity+numeric"))
    check("a numeric type and the none agent are accepted",
          got[0] == 201 and got[2]["MediaContainer"]["Directory"][0]["type"] == "movie", got[:2])
    got = request(port, "POST", "/library/sections?name=W15d&type=show"
                  "&agent=tv.plex.agents.series&scanner=Plex+TV+Series&language=en-US"
                  "&location=%2Fdata%2Fw15d&prefs%5BflattenSeasons%5D=1")
    created = got[2]["MediaContainer"]["Directory"][0] if got[0] == 201 else {}
    check("a create's prefs are a prefs write, which stamps updatedAt",
          isinstance(created.get("updatedAt"), int) and created["updatedAt"] > 0, created)

    # -- a library's prefs ------------------------------------------------------------
    for query in ("originalTitles=true", "respectTags=01", "hidden=%207",
                  "autoCollectionThreshold=1_0"):
        request(port, "PUT", "/library/sections/1/prefs?" + query)
    values = {s["id"]: s.get("value") for s in
              mc(port, "/library/sections/1/prefs").get("Setting") or []}
    check("a prefs write stores bool true for `true`, false for `01`, and int() values",
          (values.get("originalTitles"), values.get("respectTags"), values.get("hidden"),
           values.get("autoCollectionThreshold")) == ("true", "false", "7", "10"), values)
    reshaped = {s["id"]: s.get("value") for s in
                mc(port, "/library/sections/1/prefs?agent=tv.plex.agents.none")
                .get("Setting") or []}
    check("an agent-reshaped form keeps the library's written values",
          reshaped.get("hidden") == "7", reshaped)

    # -- the listing row projection ----------------------------------------------------
    request(port, "PUT", "/library/metadata/20001/prefs?episodeSort=1&flattenSeasons=1"
            "&showOrdering=dvd&useOriginalTitle=1&languageOverride=en")
    request(port, "PUT", "/library/metadata/1007/prefs?useOriginalTitle=1")
    setting_names = ("episodeSort", "flattenSeasons", "showOrdering", "useOriginalTitle",
                     "languageOverride")
    request(port, "PUT", "/:/scrobble?key=1007&identifier=com.plexapp.plugins.library")
    for path in ("/library/sections/2/all?type=2", "/library/sections/1/all", "/library/all",
                 "/library/all/top?type=1",
                 "/library/sections/1/newest", "/library/sections/1/folder",
                 "/library/sections/2/recentlyViewedShows?type=2"):
        rows = mc(port, path).get("Metadata") or []
        leaked = sorted({name for row in rows for name in setting_names if name in row})
        check("%s rows carry no item-setting attributes" % path, rows and not leaked, leaked)
    detail = (mc(port, "/library/metadata/20001").get("Metadata") or [{}])[0]
    check("the show's DETAIL row still carries its written settings",
          detail.get("episodeSort") == "1" and detail.get("showOrdering") == "dvd", detail.keys())
    # An unmatched show loses its poster, so its episodes borrow none.
    request(port, "PUT", "/library/metadata/20001/unmatch")
    seasons = mc(port, "/library/sections/2/all?type=3").get("Metadata") or []
    check("an unmatched show's seasons no longer carry its poster as parentThumb",
          seasons and not any("parentThumb" in r for r in seasons),
          [r.get("parentThumb") for r in seasons])
    episodes = mc(port, "/library/sections/2/all?type=4").get("Metadata") or []
    posterless = [row for row in episodes if not row.get("grandparentThumb")]
    check("an episode without a show poster leads its Image list with its own coverPoster",
          posterless and all((row.get("Image") or [{}])[0].get("type") == "coverPoster"
                             and "snapshot" not in [i.get("type") for i in row["Image"]]
                             for row in posterless),
          [(r.get("ratingKey"), r.get("Image")) for r in posterless[:2]])

    # -- a library's timeline ---------------------------------------------------------
    uri = ("server%3A%2F%2Ff1x7u4e0000000000000000000000000000000f1%2Fcom.plexapp.plugins"
           ".library%2Flibrary%2Fmetadata%2F1007")
    status, _, body = request(port, "POST", "/library/collections?type=1&title=Timeline+Probe"
                              "&smart=0&sectionId=1&uri=" + uri)
    made = ((body or {}).get("MediaContainer") or {}).get("Metadata", [{}])[0] \
        if isinstance(body, dict) else {}
    request(port, "DELETE", "/library/collections/%s" % made.get("ratingKey"))
    rows = mc(port, "/library/sections/1/timeline").get("Metadata") or []
    stub = next((r for r in rows if r.get("ratingKey") == made.get("ratingKey")), None)
    check("a deleted collection is a state-9 stub on its library's timeline",
          stub == {"ratingKey": made.get("ratingKey"),
                   "key": "/library/metadata/%s" % made.get("ratingKey"), "guid": "",
                   "type": "movie", "title": "", "summary": "", "state": 9,
                   "metadataState": "deleted"}, (status, stub, [r.get("ratingKey") for r in rows]))

    # -- /common -------------------------------------------------------------------------
    everyone = "%2C".join(["1007", "1008", "1009", "1012", "1013", "1014"])
    status, _, body = request(port, "POST", "/library/collections?type=1&title=Common+Probe"
                              "&smart=0&sectionId=1&uri=" + uri.replace("1007", everyone))
    made = ((body or {}).get("MediaContainer") or {}).get("Metadata", [{}])[0] \
        if isinstance(body, dict) else {}
    common = (mc(port, "/library/sections/1/common?type=1").get("Metadata") or [{}])[0]
    tags = [t for t in common.get("Collection") or [] if t.get("tag") == "Common Probe"]
    check("a shared collection tag on /common carries the collection's guid",
          tags and tags[0].get("guid") == made.get("guid"),
          (status, common.get("Collection")))

    # -- type=18 ------------------------------------------------------------------------
    grid = mc(port, "/library/sections/1/all?type=18")
    ok, bad = has(grid, allowSync=True, art=MOVIE_ART, librarySectionID=1,
                  title2="All Debrid Movies", viewGroup="movie", viewMode=65592)
    rows = grid.get("Metadata") or []
    check("a section's type=18 listing is the listing container over light rows",
          ok and rows and not any("librarySectionID" in r or "librarySectionKey" in r
                                  for r in rows), bad or rows[:1])
    everywhere = mc(port, "/library/all?type=18").get("Metadata") or []
    check("/library/all?type=18 rows carry the collection's coverPoster Image",
          everywhere and all((r.get("Image") or [{}])[0].get("type") == "coverPoster"
                             for r in everywhere), everywhere[:1])
    untyped = [r for r in mc(port, "/library/all").get("Metadata") or []
               if r.get("type") == "collection"]
    check("the untyped /library/all lists collections with their Image too",
          untyped and all(r.get("Image") for r in untyped), untyped[:1])

    # -- empty write answers ---------------------------------------------------------
    for method, path in (("PUT", "/library/sections/1/analyze"),
                         ("PUT", "/library/sections/1/emptyTrash"),
                         ("DELETE", "/library/sections/1/indexes")):
        status, head, _ = request(port, method, path)
        check("%s %s answers an empty text/html 200" % (method, path),
              status == 200 and head.get("content-type") == "text/html"
              and head.get("content-length") == "0", (status, head))

    # -- a section scan's force flag ----------------------------------------------------
    forced = {value: request(port, "GET", "/library/sections/1/refresh?force=%s" % value)[0]
              for value in ("-1", "2", "abc", "", "1.5", "+1")}
    check("a scan's force flag is digits after leading minus signs",
          forced == {"-1": 200, "2": 200, "abc": 400, "": 400, "1.5": 400, "+1": 400}, forced)

    # -- an unrecognised method honours the request's connection state -------------
    for method in ("PATCH", "FROB"):
        status, head, _ = request(port, method, "/library/sections")
        check("%s with Connection: close answers 500 and closes" % method,
              status == 500 and head.get("connection") == "close"
              and "keep-alive" not in head, (status, head))


if __name__ == "__main__":
    sys.exit(main())
