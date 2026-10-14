#!/usr/bin/env python3
"""Cycle 87, territory B2: a MERGE does not recount the collections it takes a member from.

Cycle 87 E4 saw it once beside a playlist (`tests/fixtures/merge_playlist_s43.json`: a
collection kept `childCount` 2 over one child after its member was merged away) and recorded
it unemulated. Measured on plexscratch (PMS 1.43.4) with python-plexapi's `merge()` shape
(`mixins/split_merge.py:19`, `PUT /library/metadata/{a}/merge?ids={b}`) and recorded in
`tests/fixtures/collection_merged_s43.json`:

* a collection whose ONLY member is merged into a movie outside it keeps `childCount` 1 and
  the absorbed member's year as `minYear`/`maxYear`, at once and 20 s later, on
  `/library/collections/{id}`, `/library/metadata/{id}`, section `/collections` and
  `all?type=18`, while `/children` lists nothing and the survivor joins nothing;
* a two-member collection whose older member is merged into the other lists the survivor
  ONCE under `/children` and still reads `childCount` 2 and the two members' year span;
* removing that survivor -- the last member -- keeps the stale 2 and span (the reference
  stops recomputing at zero, cycle 87 B), and the next add recounts from the real members:
  a 2010 movie reads 1/2010, the survivor re-added 2/2010-2015.

Both servers pruned the absorbed key and recounted live, so the solo collection lost its
`childCount` and the pair read 1. A row DELETE that empties a collection stays unmeasured:
the copy answers 400 to every media-item DELETE (`allowMediaDeletion` true, media read-only).

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_parity_cycle87_b2.py
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
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "collection_merged_s43.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle87-b2", "Accept": "application/json"}
SECTION = "1"
# catalog_ui movies with Media and distinct years: Tears of Steel 2012, its copy 2 2012,
# Trust No One 2022, Tuesday 2024, Vinaipayan 2026.
OUTSIDE, SOLO = "1007", "1012"
OLDER, NEWER = "1013", "1014"
ADDED = "1008"
FIELDS = ("childCount", "minYear", "maxYear")

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
        body = response.read()
        try:
            return response.status, json.loads(body) if body.strip() else None
        except ValueError:
            return response.status, None
    finally:
        connection.close()


def start(port=None, state=None):
    port = port or free_port()
    args = ["--port", port, "--catalog", CATALOG, "--assert-claimed", "--gdm", "off"]
    if state:
        args += ["--state", state]
    process = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=ENV,
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
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def metadata(body):
    if not isinstance(body, dict):
        return []
    return ((body or {}).get("MediaContainer") or {}).get("Metadata") or []


def collection_rows(body, rating_key=None):
    rows = [row for row in metadata(body) if row.get("type") == "collection"]
    if rating_key is not None:
        rows = [row for row in rows if str(row.get("ratingKey")) == str(rating_key)]
    return rows


def counts(row):
    return {name: row.get(name) for name in FIELDS}


def span(count, *years):
    years = sorted(str(year) for year in years)
    return {"childCount": count, "minYear": years[0], "maxYear": years[-1]}


def tags(row):
    return [tag.get("tag") for tag in row.get("Collection") or []]


# -- what plexscratch answered ---------------------------------------------------------

def reference_reads(exchanges, key):
    """(path, counts) of every collection-row read of `key` in a block of exchanges."""
    out = []
    for exchange in exchanges:
        if exchange["method"] != "GET" or "/children" in exchange["path"]:
            continue
        rows = collection_rows(exchange["body"], key)
        if rows:
            out.append((exchange["path"], counts(rows[0])))
    return out


def reference_children(exchanges, key):
    return [[row["ratingKey"] for row in metadata(e["body"])] for e in exchanges
            if e["path"].startswith("/library/collections/%s/children" % key)]


def reference_checks():
    fixture = json.load(open(FIXTURE))
    members = fixture["members"]
    for name in ("solo", "pair"):
        exchanges = fixture[name]
        absorbed = members[name]["absorbed"]
        survivor = members[name]["survivor"]
        merge = next(i for i, e in enumerate(exchanges) if "/merge?ids=" in e["path"])
        check("reference %s: the merge is an empty 200" % name,
              exchanges[merge]["status"] == 200 and exchanges[merge]["body"] is None,
              exchanges[merge])
        key = collection_rows(exchanges[2]["body"])[0]["ratingKey"]
        before = reference_reads(exchanges[:merge], key)
        after = reference_reads(exchanges[merge + 1:], key)
        expected = (span(1, absorbed["year"]) if name == "solo"
                    else span(2, absorbed["year"], survivor["year"]))
        check("reference %s: every read before the merge says %s" % (name, expected),
              before and all(seen == expected for _, seen in before), before)
        check("reference %s: all %d reads after the merge still say %s"
              % (name, len(after), expected),
              len(after) == 8 and all(seen == expected for _, seen in after), after)
        children = reference_children(exchanges[merge + 1:], key)
        wanted = [] if name == "solo" else [survivor["ratingKey"]]
        check("reference %s: /children lists %s, with and without paging" % (name, wanted),
              len(children) == 4 and all(listed == wanted for listed in children), children)
        gone = [e["status"] for e in exchanges[merge + 1:]
                if e["path"] == "/library/metadata/%s" % absorbed["ratingKey"]]
        check("reference %s: the absorbed row is a 404" % name, gone == [404], gone)
        if name == "solo":
            survivor_rows = [metadata(e["body"])[0] for e in exchanges[merge + 1:]
                             if e["path"] == "/library/metadata/%s" % survivor["ratingKey"]]
            check("reference solo: the survivor joins no collection",
                  survivor_rows and all(tags(row) == [] for row in survivor_rows),
                  [tags(row) for row in survivor_rows])
    follow = fixture["follow"]
    stale = span(2, members["pair"]["absorbed"]["year"], members["pair"]["survivor"]["year"])
    key = follow[0]["path"].split("/")[3]
    emptied = reference_reads(follow[:8], key)
    check("reference follow: removing the last member keeps the stale %s on the DELETE "
          "and %d reads" % (stale, len(emptied) - 1),
          counts(collection_rows(follow[0]["body"])[0]) == stale
          and len(emptied) == 4 and all(seen == stale for _, seen in emptied), emptied)
    added = members["follow"]["added"]["year"]
    regained = reference_reads(follow[8:16], key)
    check("reference follow: the next add recounts to 1, %s" % added,
          counts(collection_rows(follow[8]["body"])[0]) == span(1, added)
          and all(seen == span(1, added) for _, seen in regained), regained)
    both = span(2, added, members["pair"]["survivor"]["year"])
    again = reference_reads(follow[16:], key)
    check("reference follow: re-adding the survivor reads %s" % both,
          counts(collection_rows(follow[16]["body"])[0]) == both
          and all(seen == both for _, seen in again), again)
    refused = fixture["delete"]
    check("reference: the copy refuses a media-item DELETE (unmeasurable here)",
          refused[1]["method"] == "DELETE" and refused[1]["status"] == 400
          and refused[2]["status"] == 200, [(e["method"], e["status"]) for e in refused])


# -- rex ---------------------------------------------------------------------------

def uri(port, *keys):
    machine = request(port, "GET", "/identity")[1]["MediaContainer"]["machineIdentifier"]
    return "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, ",".join(keys))


def create(port, title, *keys):
    status, body = request(port, "POST", "/library/collections?" + urllib.parse.urlencode(
        dict(type=1, title=title, smart=0, sectionId=SECTION, uri=uri(port, *keys))))
    return collection_rows(body)[0]["ratingKey"]


def reads(port, key):
    out = []
    for path in ("/library/collections/%s" % key, "/library/metadata/%s" % key,
                 "/library/sections/%s/collections" % SECTION,
                 "/library/sections/%s/all?type=18" % SECTION):
        rows = collection_rows(request(port, "GET", path)[1], key)
        out.append((path, counts(rows[0]) if rows else None))
    return out


def children(port, key):
    out = []
    for query in ("", "?X-Plex-Container-Start=0&X-Plex-Container-Size=20"):
        body = request(port, "GET", "/library/collections/%s/children%s" % (key, query))[1]
        out.append([str(row["ratingKey"]) for row in metadata(body)])
    return out


def all_say(port, key, expected, name):
    seen = reads(port, key)
    for path, value in seen:
        check("%s: %s says %s" % (name, path, expected), value == expected, value)


def rex_checks(port):
    year = {}
    for key in (OUTSIDE, SOLO, OLDER, NEWER, ADDED):
        row = metadata(request(port, "GET", "/library/metadata/%s" % key)[1])[0]
        year[key] = str(row["year"])
    check("the catalog movies have the years the checks assume",
          len({year[SOLO], year[OLDER], year[NEWER], year[ADDED]}) == 4, year)

    solo = create(port, "C87B2 Solo", SOLO)
    status, body = request(port, "PUT", "/library/metadata/%s/merge?ids=%s" % (OUTSIDE, SOLO))
    check("solo: the merge is a 200", status == 200, status)
    all_say(port, solo, span(1, year[SOLO]), "solo merged away")
    check("solo: /children lists nothing", children(port, solo) == [[], []], children(port, solo))
    survivor = metadata(request(port, "GET", "/library/metadata/%s" % OUTSIDE)[1])[0]
    check("solo: the survivor joins no collection", tags(survivor) == [], tags(survivor))

    pair = create(port, "C87B2 Pair", OLDER, NEWER)
    request(port, "PUT", "/library/metadata/%s/merge?ids=%s" % (NEWER, OLDER))
    stale = span(2, year[OLDER], year[NEWER])
    all_say(port, pair, stale, "pair merged")
    check("pair: /children lists the survivor once", children(port, pair) == [[NEWER], [NEWER]],
          children(port, pair))

    status, body = request(port, "DELETE", "/library/collections/%s/items/%s" % (pair, NEWER))
    rows = collection_rows(body)
    check("follow: the DELETE of the last member answers the stale %s" % stale,
          status == 200 and rows and counts(rows[0]) == stale, (status, rows))
    all_say(port, pair, stale, "follow emptied")
    request(port, "PUT", "/library/collections/%s/items?uri=%s"
            % (pair, urllib.parse.quote(uri(port, ADDED))))
    all_say(port, pair, span(1, year[ADDED]), "follow re-added")
    request(port, "PUT", "/library/collections/%s/items?uri=%s"
            % (pair, urllib.parse.quote(uri(port, NEWER))))
    all_say(port, pair, span(2, year[ADDED], year[NEWER]), "follow survivor back")
    return solo, span(1, year[SOLO])


def merged_state_keys(port):
    """Merge twice for the --state leg: the pair keeps its stale count across a restart."""
    pair = create(port, "C87B2 Kept", OLDER, NEWER)
    year = {key: str(metadata(request(port, "GET", "/library/metadata/%s" % key)[1])[0]["year"])
            for key in (OLDER, NEWER)}
    request(port, "PUT", "/library/metadata/%s/merge?ids=%s" % (NEWER, OLDER))
    return pair, span(2, year[OLDER], year[NEWER])


# -- every client removal path keeps the count (cycle 87 B2b) ---------------------------

REMOVALS = os.path.join(HERE, "fixtures", "collection_removal_paths_s42.json")


def removal_reference_checks():
    """plexscratch42: no removal path a client sends drops a one-member collection's count.

    `tests/test_followups_c87_e4.py` rebuilt plexscratch42's childCount-less C52Amulti and
    C52Averify by emptying one-member collections through `/children/{id}`, and failed once
    the servers kept the count. Measured: `/children/{id}`, `/items/{id}` and the tag edit
    all keep `childCount` 1 and the member's year; only a collection that never held a
    member (POSTed with no `uri`) has none. The test, not the servers, was wrong.
    """
    fixture = json.load(open(REMOVALS))
    exchanges = fixture["removals"]
    created = [(i, collection_rows(e["body"])[0]) for i, e in enumerate(exchanges)
               if e["method"] == "POST"]
    for index, row in created:
        key, title = row["ratingKey"], row["title"]
        cleanup = next(i for i, e in enumerate(exchanges) if e["method"] == "DELETE"
                       and e["path"].count("/") == 3)
        end = next((i for i, _ in created if i > index), cleanup)
        block = exchanges[index + 1:end]
        removal = [e for e in block if e["method"] in ("DELETE", "PUT")]
        last = removal[-1]
        after = reference_reads(block[block.index(last) + 1:], key)
        check("reference %s: %s answers %d" % (title, last["method"], last["status"]),
              last["status"] == 200, last)
        check("reference %s: emptied, %d reads keep 1, 2010" % (title, len(after)),
              len(after) == 4 and all(seen == span(1, 2010) for _, seen in after), after)
        emptied = reference_children(block[block.index(last) + 1:], key)
        check("reference %s: /children lists nothing" % title, emptied == [[]], emptied)
    nouri = fixture["nouri"]
    rows = collection_rows(nouri[0]["body"])
    reads_ = reference_reads(nouri[1:], rows[0]["ratingKey"]) if rows else []
    check("reference: a collection POSTed with no uri has no childCount or years",
          rows and counts(rows[0]) == {name: None for name in FIELDS}
          and len(reads_) == 4 and all(seen == counts(rows[0]) for _, seen in reads_),
          (rows, reads_))
    standing = {row["title"]: counts(row) for row in collection_rows(nouri[-1]["body"])}
    check("reference: the copy's own C52Amulti/C52Averify read no childCount, C49Empty 1",
          standing.get("C52Amulti") == standing.get("C52Averify")
          == {name: None for name in FIELDS}
          and standing.get("C49Empty", {}).get("childCount") == 1, standing)


def removal_rex_checks(port):
    year = str(metadata(request(port, "GET", "/library/metadata/%s" % NEWER)[1])[0]["year"])
    paths = {
        "children": lambda key, title: request(
            port, "DELETE", "/library/collections/%s/children/%s" % (key, NEWER)),
        "items": lambda key, title: request(
            port, "DELETE", "/library/collections/%s/items/%s" % (key, NEWER)),
        "tag": lambda key, title: request(
            port, "PUT", "/library/sections/%s/all?" % SECTION + urllib.parse.urlencode(
                {"type": 1, "id": NEWER, "collection[].tag.tag-": title})),
    }
    for name, remove in paths.items():
        title = "C87B2b %s" % name
        key = create(port, title, NEWER)
        status, _ = remove(key, title)
        check("%s: the removal is a 200" % name, status == 200, status)
        all_say(port, key, span(1, year), "%s emptied" % name)
        check("%s: /children lists nothing" % name, children(port, key) == [[], []],
              children(port, key))
    key = create(port, "C87B2b pair", OLDER, NEWER)
    request(port, "DELETE", "/library/collections/%s/children/%s" % (key, OLDER))
    all_say(port, key, span(1, year), "children pair, one left")
    request(port, "DELETE", "/library/collections/%s/children/%s" % (key, NEWER))
    all_say(port, key, span(1, year), "children pair emptied")
    status, body = request(port, "POST", "/library/collections?" + urllib.parse.urlencode(
        dict(type=1, title="C87B2b nouri", smart=0, sectionId=SECTION)))
    rows = collection_rows(body)
    check("a collection POSTed with no uri has no childCount or years",
          status == 200 and rows and counts(rows[0]) == {name: None for name in FIELDS},
          (status, rows))
    if rows:
        all_say(port, rows[0]["ratingKey"], {name: None for name in FIELDS}, "nouri")


def main():
    reference_checks()
    removal_reference_checks()
    process, port = start()
    try:
        rex_checks(port)
    finally:
        stop(process)
    process, port = start()
    try:
        removal_rex_checks(port)
    finally:
        stop(process)
    # The stale counts are stored state on the reference (its database never recounts), so a
    # restart on --state must keep them.
    with tempfile.TemporaryDirectory() as tmp:
        state = os.path.join(tmp, "state.pickle")
        process, port = start(state=state)
        try:
            key, stale = merged_state_keys(port)
        finally:
            stop(process)
        process, port = start(port=port, state=state)
        try:
            check("after a --state restart the merged pair still says %s" % stale,
                  reads(port, key)[0][1] == stale, reads(port, key))
        finally:
            stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 87 B2 merged-collection checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
