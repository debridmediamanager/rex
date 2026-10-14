#!/usr/bin/env python3
"""A show is "unplayed" by its LEAVES on two lists, and by its own counter everywhere else.

Measured on the 1.42.2 reference (the build rex reports, 2026-09-30, GET only) and checked
in as `tests/fixtures/unwatched_zen.json`. Its TV section holds 509 shows: six PARTLY
watched (0 < viewedLeafCount < leafCount), two fully watched, and not one show or season
carrying a `viewCount` of its own (its plays came through timelines).

- `/library/sections/2/unwatched` answers 507: the six partly watched shows in, the two
  fully watched ones out -- exactly the `unwatchedLeaves=1` set.
- A show's `/children?unwatched=1` keeps each season that still has an unwatched leaf
  (5195 drops its one watched season, 11004 drops its 1-of-1 season and keeps 47-of-52).
  Cycle 71 measured the same on both writable copies with seasons whose OWN `viewCount`
  was 7, so it is not the season's counter. `unwatched!=0` -- the column's other true
  form -- keeps every season, and `=0`, `!=1`, `==1`, `=2` keep none.
- The filter COLUMN is not leaf-based: `all?type=2&unwatched=1` answers all 509, the two
  fully watched shows included, and `unwatched=0` none. (Plex Web's TV "Unwatched" filter
  sends `unwatchedLeaves=1`, the Filter the show Meta advertises, not `unwatched=1`.)

rex read the row's own `viewCount` on both lists, and an episode scrobble stamps one
on its season and show, so one watched episode took the show off `/unwatched` and a
partly watched season off `/children?unwatched=1`.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_followups_fd.py
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
FIXTURE = os.path.join(HERE, "fixtures", "unwatched_zen.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "followups-fd",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome", "X-Plex-Container-Start": "0",
           "X-Plex-Container-Size": "600"}
LIBRARY = "identifier=com.plexapp.plugins.library"

# The fixture catalog's one show: season 30001 holds 1001 alone, 30002 holds 1002-1003,
# 30003 holds 1004 and 30004 holds 1005, 1006, 1010 and 1011.
SHOW = "20001"
SEASONS = ("30001", "30002", "30003", "30004")

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


def rows(port, path):
    status, body = request(port, "GET", path)
    container = (body or {}).get("MediaContainer") or {} if isinstance(body, dict) else {}
    return status, container.get("Metadata") or [], container.get("Directory") or []


def keys(found):
    return [str(row.get("ratingKey")) for row in found]


def fixture_rules():
    """What the 1.42.2 reference answered, read out of the recording -- the expectations below come from it."""
    with open(FIXTURE, encoding="utf-8") as handle:
        recorded = json.load(handle)
    exchanges = {entry["path"]: entry for entry in recorded["exchanges"]}
    shows = recorded["shows"]
    part, full = set(recorded["partly_watched"]), set(recorded["fully_watched"])
    with_leaves = {row["ratingKey"] for row in shows
                   if row.get("viewedLeafCount", 0) < row.get("leafCount", 0)}
    listed = set(exchanges["/library/sections/2/unwatched"]["ratingKeys"])
    check("the 1.42.2 reference: /unwatched is exactly the shows with an unwatched leaf (%d of %d)"
          % (len(listed), len(shows)),
          listed == with_leaves and part <= listed and not full & listed,
          "listed %d, leaves %d" % (len(listed), len(with_leaves)))
    check("the 1.42.2 reference: no show row carries its own viewCount",
          not [row for row in shows if "viewCount" in row])
    column = set(exchanges["/library/sections/2/all?type=2&unwatched=1"]["ratingKeys"])
    check("the 1.42.2 reference: the column's unwatched=1 answers every show, the fully watched included",
          column == {row["ratingKey"] for row in shows} and full <= column
          and not exchanges["/library/sections/2/all?type=2&unwatched=0"]["ratingKeys"],
          len(column))
    leaf_rule = []
    for show in ("5195", "11004", "5303", "6098", "17133"):
        seasons = exchanges["/library/metadata/%s/children" % show]["rows"]
        flagged = [row["ratingKey"] for row in
                   exchanges["/library/metadata/%s/children?unwatched=1" % show]["rows"]]
        wanted = [row["ratingKey"] for row in seasons
                  if row.get("viewedLeafCount", 0) < row.get("leafCount", 0)]
        column = [row["ratingKey"] for row in
                  exchanges["/library/metadata/%s/children?unwatched!=0" % show]["rows"]]
        empty = [form for form in ("unwatched=0", "unwatched!=1", "unwatched==1", "unwatched=2")
                 if exchanges["/library/metadata/%s/children?%s" % (show, form)]["rows"]]
        if flagged != wanted or column != [row["ratingKey"] for row in seasons] or empty:
            leaf_rule.append((show, flagged, wanted, column, empty))
    check("the 1.42.2 reference: a show's /children?unwatched=1 keeps the seasons with an unwatched leaf, "
          "`!=0` keeps all and the other forms none", not leaf_rule, leaf_rule)
    return recorded


def served(port):
    # Two episodes scrobbled: 1001 is season 30001's only one, 1005 one of 30004's four.
    # The show is PARTLY watched, like the 1.42.2 reference's six, and the episode scrobble
    # stamps its own `viewCount` on the season and the show (docs/protocol-traps.md).
    for episode in ("1001", "1005"):
        status, _ = request(port, "GET", "/:/scrobble?key=%s&%s" % (episode, LIBRARY))
        check("scrobble %s answers 200" % episode, status == 200, status)
    _, show, _ = rows(port, "/library/metadata/%s" % SHOW)
    _, seasons, _ = rows(port, "/library/metadata/%s/children" % SHOW)
    counters = {row["ratingKey"]: row.get("viewCount") for row in show + seasons}
    check("precondition: the show and seasons 30001/30004 now carry their own viewCount",
          all(counters.get(key) for key in (SHOW, "30001", "30004"))
          and not counters.get("30002"), counters)

    status, found, _ = rows(port, "/library/sections/2/unwatched")
    check("a partly watched show stays on /library/sections/2/unwatched (the 1.42.2 reference: all six do)",
          status == 200 and keys(found) == [SHOW], (status, keys(found)))
    row = next((r for r in found if str(r.get("ratingKey")) == SHOW), {})
    check("and its row still opens /children?unwatched=1",
          str(row.get("key") or "").endswith("/children?unwatched=1"), row.get("key"))

    status, found, directories = rows(port, "/library/metadata/%s/children?unwatched=1" % SHOW)
    check("/children?unwatched=1 keeps every season with an unwatched leaf, whatever its "
          "own viewCount (the 1.42.2 reference's rows 5195 and 11004; both copies in cycle 71)",
          status == 200 and keys(found) == ["30002", "30003", "30004"], (status, keys(found)))
    check("and carries the All episodes row with the show's own counts",
          [(d.get("key"), d.get("leafCount"), d.get("viewedLeafCount")) for d in directories]
          == [("/library/metadata/%s/allLeaves" % SHOW, 8, 2)], directories)
    status, found, _ = rows(port, "/library/metadata/%s/children?unwatched!=0" % SHOW)
    check("/children?unwatched!=0 is still the column -- the seasons' own counter",
          status == 200 and keys(found) == ["30002", "30003"], (status, keys(found)))
    status, found, _ = rows(port, "/library/metadata/%s/children?unwatched==1" % SHOW)
    check("/children?unwatched==1 is the column's false set, not the leaf flag",
          status == 200 and keys(found) == ["30001", "30004"], (status, keys(found)))
    status, found, _ = rows(port, "/library/metadata/30004/children?unwatched=1")
    check("a SEASON's /children?unwatched=1 drops its watched episode (the 1.42.2 reference's row 17201: 19 of 22)",
          status == 200 and keys(found) == ["1006", "1010", "1011"], (status, keys(found)))

    # Everything watched: the 1.42.2 reference lists neither fully watched show and none of
    # their seasons.
    for episode in ("1002", "1003", "1004", "1006", "1010", "1011"):
        request(port, "GET", "/:/scrobble?key=%s&%s" % (episode, LIBRARY))
    status, found, _ = rows(port, "/library/sections/2/unwatched")
    check("a fully watched show leaves /unwatched (the 1.42.2 reference's rows 6098, 17133)",
          status == 200 and not found, (status, keys(found)))
    status, found, _ = rows(port, "/library/metadata/%s/children?unwatched=1" % SHOW)
    check("and its /children?unwatched=1 is empty",
          status == 200 and not found, (status, keys(found)))

    # A movie is its own leaf: its own counter still decides.
    request(port, "GET", "/:/scrobble?key=1007&%s" % LIBRARY)
    status, found, _ = rows(port, "/library/sections/1/unwatched")
    check("a movie library's /unwatched still drops a watched movie",
          status == 200 and "1007" not in keys(found) and len(found) == 5,
          (status, keys(found)))


def main():
    fixture_rules()
    process, port = start()
    try:
        served(port)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d unwatched follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
