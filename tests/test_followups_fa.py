#!/usr/bin/env python3
"""`flattenSeasons=1` changes what an episode ROW says and nothing else.

Measured on plexscratch (PMS 1.43.4, 2026-09-30) and checked in as
`tests/fixtures/flatten_seasons_reference.jsonl`: Firefly read through 35 paths with the
setting unwritten, again after `PUT /library/metadata/6/prefs?flattenSeasons=1`, and
again after a scrobble of S1E1. With the setting on

- every episode row -- detail, `/children`, `/allLeaves`, the section listings and
  subsets, search, On Deck, the hub pages, a play queue -- says `skipParent: true` and
  carries neither `parentRatingKey` nor `parentKey`; the show row says `skipChildren`;
- NO reader's row set moves: a season's `/children` still lists its episodes,
  `/library/recentlyAdded` still rolls them up to their SEASONS,
  `recentlyAddedGrandparents` and the Recently Added shelves still answer the SHOW;
- a scrobble still stamps the season: `viewCount: 1`, `lastViewedAt` and
  `viewedLeafCount: 1` on season 7, and its On Deck is the next episode;
- a history row is its own projection: it keeps `parentKey` and says no `skipParent`.

The oracle took the two keys off the STORED episode, so every reader that finds a
season's episodes by `parentRatingKey` lost them (Recently Added seasons, the
Grandparents leaf, season view counts, the unmatch cascade); the native server kept the
stored row but grouped a flattened show's episodes under no season on the Recently
Added shelves, to match the oracle. Each check replays a recorded request against the
fixture catalog (Firefly's ids mapped onto The Simpsons') and compares the facts above,
row by row, with what the reference answered.

The unmatch cascade is the one reader NOT re-measured with the setting on: an unmatch
cannot be undone on a server with no route to a metadata provider. Its checks assert the
cascade as docs/protocol-traps.md records it for an ordinary show (`local://<id>`,
`Episode N`, `parentGuid` following the season, an emptied summary, no `year`).

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `REX_SERVER=python`.

    python3 tests/test_followups_fa.py
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "flatten_seasons_reference.jsonl")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "followups-fa",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}

# The reference's Firefly onto the fixture's Simpsons: the show, a season with more than
# one episode and its first two episodes (the scrobbled one and the next), and another
# season with one of its episodes.
SHOW, SEASON, WATCHED, NEXT = "20001", "30002", "1002", "1003"
IDS = {"6": SHOW, "7": SEASON, "8": WATCHED, "9": NEXT, "11": "30004", "12": "1005"}
KINDS = ("show", "season", "episode")

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        return response.status, json.loads(raw) if raw else None
    except ValueError:
        return response.status, raw


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


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def rows_of(body):
    """Every show, season and episode row of one answer, with the element path it sits at."""
    found = []

    def walk(node, where):
        if isinstance(node, dict):
            if node.get("ratingKey") and node.get("type") in KINDS:
                found.append((where, node))
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value, where + "/" + key)
        elif isinstance(node, list):
            for value in node:
                walk(value, where)

    walk(body, "")
    return found


def facts(body, kinds=KINDS, history=False):
    """The distinct facts `flattenSeasons` and a scrobble can move, as a sorted list.

    An episode: whether it names its season and whether it says `skipParent`. A show or a
    season: `skipChildren` and the view state a watched leaf rolls up into it. A set
    rather than a sequence, because the two libraries differ in size (two seasons of
    three against four of one to four) and agree in kind.
    """
    out = set()
    for where, row in rows_of(body):
        kind = row["type"]
        if kind not in kinds:
            continue
        if kind == "episode":
            fact = (where, kind, "parentKey" in row, row.get("skipParent"))
            if not history:
                # A history row never carries a `parentRatingKey`, flattened or not.
                fact += ("parentRatingKey" in row,)
        else:
            fact = (where, kind, row.get("skipChildren"), row.get("viewCount"),
                    row.get("viewedLeafCount"), "lastViewedAt" in row)
        out.add(fact)
    return sorted(out, key=repr)


def mapped(path, machine):
    path = re.sub(r"/metadata/(\d+)", lambda m: "/metadata/" + IDS[m.group(1)], path)
    path = re.sub(r"key=(\d+)", lambda m: "key=" + IDS[m.group(1)], path)
    path = path.replace("%2Fmetadata%2F6", "%2Fmetadata%2F" + SHOW).replace("MACHINE", machine)
    # The reference's query names one Firefly episode; the fixture's names its own.
    return path.replace("query=train", "query=fixture")


def load_fixture():
    exchanges = []
    with open(FIXTURE, encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            if "phase" in entry:
                exchanges.append(entry)
    return exchanges


def replay(port, machine):
    """The recorded session against the server, one check per recorded read."""
    queue = None
    for entry in load_fixture():
        phase, path = entry["phase"], entry["path"]
        if path.startswith("/playQueues/"):
            # The reference's queue id is its own; this one is whatever the POST made.
            if queue is None:
                check("[%s] GET the play queue" % phase, False, "no queue was created")
                continue
            path = "/playQueues/%s" % queue
        target = mapped(path, machine)
        status, body = request(port, entry["method"], target)
        if phase == "write":
            check("the write answers %s, as recorded: %s %s"
                  % (entry["status"], entry["method"], target), status == entry["status"], status)
            continue
        if entry["method"] == "POST":
            queue = ((body or {}).get("MediaContainer") or {}).get("playQueueID") \
                if isinstance(body, dict) else None
        # A search hub's show row depends on the query word, which differs; the episodes
        # it finds are what the setting touches.
        kinds = ("episode",) if "query=" in path else KINDS
        history = "/history/" in path
        want = facts(entry["body"], kinds, history)
        got = facts(body, kinds, history) if isinstance(body, dict) else status
        check("[%s] %s %s" % (phase, entry["method"], target),
              status == entry["status"] and got == want,
              "status %s, reference %s | server %s" % (status, want, got))


def named(port, machine):
    """The readers the follow-up list named, each against the concrete value the reference answered.

    Run after `replay`, so the show is flattened and the first episode of SEASON is
    watched. Reference, same state: `/library/recentlyAdded` lists `season 11` and
    `season 7` for Firefly and no episode, season 7 with `viewCount: 1` and
    `viewedLeafCount: 1`; `recentlyAddedGrandparents` lists `show 6`; the Recently Added
    shelves list `show 6`; `/library/metadata/7?includeOnDeck=1` carries episode 9.
    """
    def metadata(path):
        body = request(port, "GET", path)[1]
        return ((body or {}).get("MediaContainer") or {}).get("Metadata") or [] \
            if isinstance(body, dict) else []

    tv = [(row.get("type"), row.get("ratingKey")) for row in metadata("/library/recentlyAdded")
          if row.get("type") in KINDS]
    seasons = [row.get("ratingKey") for row in metadata("/library/metadata/%s/children" % SHOW)
               if row.get("type") == "season"]
    check("Recently Added lists a flattened show's SEASONS, every one and no episode",
          tv and sorted(tv) == sorted(("season", key) for key in seasons), tv)
    season = next((row for row in metadata("/library/recentlyAdded")
                   if row.get("ratingKey") == SEASON), {})
    check("and the watched season's row there counts its watched leaf",
          season.get("viewCount") == 1 and season.get("viewedLeafCount") == 1
          and season.get("lastViewedAt"),
          {k: season.get(k) for k in ("viewCount", "viewedLeafCount", "lastViewedAt")})
    owners = [(row.get("type"), row.get("ratingKey"))
              for row in metadata("/library/sections/2/recentlyAddedGrandparents")]
    check("the Grandparents leaf answers the flattened show, once",
          owners == [("show", SHOW)], owners)
    for path in ("/hubs/home/recentlyAdded?type=2", "/hubs/home/recentlyAdded?type=2&sectionID=2"):
        shelf = [(row.get("type"), row.get("ratingKey"), row.get("skipChildren"))
                 for row in metadata(path)]
        check("%s rolls a flattened show's episodes up to the SHOW" % path,
              shelf == [("show", SHOW, True)], shelf)
    body = request(port, "GET", "/hubs/sections/2?count=13")[1]
    hubs = {hub.get("hubIdentifier"): [(row.get("type"), row.get("ratingKey"))
                                       for row in hub.get("Metadata") or []]
            for hub in ((body or {}).get("MediaContainer") or {}).get("Hub") or []}
    check("the library's Recently Added shelf is the one show row",
          hubs.get("tv.recentlyadded.2") == [("show", SHOW)], hubs.get("tv.recentlyadded.2"))
    detail = (metadata("/library/metadata/%s?includeOnDeck=1" % SEASON) or [{}])[0]
    deck = (detail.get("OnDeck") or {}).get("Metadata") or {}
    check("a flattened show's season still has its On Deck, the next episode",
          deck.get("ratingKey") == NEXT and deck.get("skipParent") is True
          and "parentRatingKey" not in deck,
          {k: deck.get(k) for k in ("ratingKey", "skipParent", "parentRatingKey")})
    check("the watched season's own row carries the play",
          detail.get("viewCount") == 1 and detail.get("viewedLeafCount") == 1
          and detail.get("lastViewedAt"),
          {k: detail.get(k) for k in ("viewCount", "viewedLeafCount", "lastViewedAt")})
    children = [(row.get("ratingKey"), row.get("skipParent"), row.get("parentRatingKey"),
                 row.get("parentIndex") is not None, bool(row.get("parentTitle")))
                for row in metadata("/library/metadata/%s/children" % SEASON)]
    check("a season's /children still lists its episodes, each without its season's key",
          children == [(WATCHED, True, None, True, True), (NEXT, True, None, True, True)],
          children)

    # `=0` puts all of it back, and in place: the key order of the row does not move.
    before = (metadata("/library/metadata/%s" % NEXT) or [{}])[0]
    request(port, "PUT", "/library/metadata/%s/prefs?flattenSeasons=0" % SHOW)
    after = (metadata("/library/metadata/%s" % NEXT) or [{}])[0]
    check("flattenSeasons=0 gives the episode its season back",
          "skipParent" in before and "skipParent" not in after
          and after.get("parentRatingKey") == SEASON
          and after.get("parentKey") == "/library/metadata/%s" % SEASON,
          {k: after.get(k) for k in ("skipParent", "parentRatingKey", "parentKey")})
    request(port, "PUT", "/library/metadata/%s/prefs?flattenSeasons=-1" % SHOW)

    # The library-wide write, where the Edit Library dialog puts it (cycle 15).
    request(port, "PUT", "/library/sections/2/prefs?flattenSeasons=1")
    wide = [(row.get("type"), row.get("ratingKey")) for row in metadata("/library/recentlyAdded")
            if row.get("type") in KINDS]
    leaf = (metadata("/library/metadata/%s" % NEXT) or [{}])[0]
    check("the library-wide setting flattens the rows and moves no reader either",
          leaf.get("skipParent") is True and "parentRatingKey" not in leaf
          and sorted(wide) == sorted(("season", key) for key in seasons),
          "leaf %s, recentlyAdded %s" % ({k: leaf.get(k) for k in ("skipParent", "parentRatingKey")},
                                         wide))
    request(port, "PUT", "/library/sections/2/prefs?flattenSeasons=0")
    request(port, "PUT", "/library/metadata/%s/prefs?flattenSeasons=1" % SHOW)


def unmatch(port):
    """A show's unmatch reaches a flattened show's episodes (rule measured unflattened)."""
    def metadata(path):
        body = request(port, "GET", path)[1]
        return ((body or {}).get("MediaContainer") or {}).get("Metadata") or [] \
            if isinstance(body, dict) else []

    season_of = {}
    for season in metadata("/library/metadata/%s/children" % SHOW):
        for leaf in metadata("/library/metadata/%s/children" % season["ratingKey"]):
            season_of[leaf["ratingKey"]] = season["ratingKey"]
    status, _ = request(port, "PUT", "/library/metadata/%s/unmatch" % SHOW)
    leaves = metadata("/library/metadata/%s/allLeaves" % SHOW)
    check("unmatching a flattened show answers 200 and its episodes are still listed",
          status == 200 and len(leaves) == len(season_of) == 8, "%s, %d leaves" % (status, len(leaves)))
    stale = [(row.get("ratingKey"), row.get("guid"), row.get("title")) for row in leaves
             if row.get("guid") != "local://%s" % row.get("ratingKey")
             or row.get("title") != "Episode %s" % row.get("index")]
    check("every episode becomes local://<id> with the generated `Episode N`", not stale, stale)
    parents = [(row.get("ratingKey"), row.get("parentGuid")) for row in leaves
               if row.get("parentGuid") != "local://%s" % season_of.get(row.get("ratingKey"))]
    check("every episode's parentGuid follows its unmatched season", not parents, parents)
    kept = [(row.get("ratingKey"), row.get("summary"), row.get("year")) for row in leaves
            if row.get("summary") or "year" in row]
    check("every episode's summary is emptied and its year dropped", not kept, kept)
    art = [(row.get("ratingKey"), row.get("parentThumb")) for row in leaves
           if "parentThumb" in row]
    check("no episode keeps a parentThumb its unmatched season no longer has", not art, art)
    flat = [(row.get("ratingKey"), row.get("skipParent"), row.get("parentRatingKey"))
            for row in leaves if row.get("skipParent") is not True or "parentRatingKey" in row]
    check("and the rows are still the flattened projection", not flat, flat)


def main():
    process, port = start()
    try:
        machine = request(port, "GET", "/identity")[1]["MediaContainer"]["machineIdentifier"]
        replay(port, machine)
        named(port, machine)
        unmatch(port)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d flattenSeasons follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
