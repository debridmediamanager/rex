#!/usr/bin/env python3
"""Cycle 94, territory D: what Plex for Android 2026.19.1's server dashboard reads.

`/library/all/top` (Top Played, js:1521748 useTopCharts) answers the item's SECTION LISTING row
-- the light projection a grid draws -- plus the librarySection triple, `globalViewCount` and
`userCount` placed straight after `updatedAt` (after `deletedAt` where the row has one), and a
`User` list last. Measured on kids (1.43.4, movies) and zen (1.42.2, a show, a season and
episodes) in `tests/fixtures/c94_d_library_all_top.json`: the top row is the listing row with
exactly those additions, so no `Guid`, `Rating` or `Location` (which only a detail row carries).
A show or season row also drops the listing's per-user aggregate (`viewCount`, `skipCount`,
`lastViewedAt`, `userRating`, `lastRatedAt`) -- zen's Bosch rows against their own listing rows
-- and a SEASON row gains `leafCount` and `viewedLeafCount`, which its listing row does not carry,
just before `addedAt`. An episode row (zen) and a movie row (kids) keep the listing's own
state; whether 1.43.4 also drops a show's aggregate is not measured (kids has no show plays).
plexsim answered the full detail row here.

Drives a real server process (`tests/server_command.py`): the native binary by default, the
oracle with `PLEXSIM_SERVER=python`. Binds a port in 25420-25429.

    python3 tests/test_parity_cycle94_d.py
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
FIXTURE = os.path.join(HERE, "fixtures", "c94_d_library_all_top.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "c94d-android",
           "X-Plex-Product": "Plex for Android (Mobile)", "X-Plex-Version": "2026.19.1",
           "X-Plex-Platform": "Android", "X-Plex-Device-Name": "sdk_gphone64_arm64"}
ADDED = {"librarySectionTitle", "librarySectionID", "librarySectionKey", "globalViewCount",
         "userCount", "User"}
AGGREGATE = {"viewCount", "skipCount", "lastViewedAt", "userRating", "lastRatedAt"}
SEASON_COUNTS = {"leafCount", "viewedLeafCount"}


def expected_extra(top_type):
    return ADDED | SEASON_COUNTS if top_type == "season" else ADDED

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    for port in range(25420, 25430):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    raise SystemExit("no free port in 25420-25429")


def mc(port, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=dict(HEADERS, Connection="close", **(headers or {})))
        raw = connection.getresponse().read()
    finally:
        connection.close()
    try:
        return json.loads(raw).get("MediaContainer", {})
    except ValueError:
        return {}


def start():
    port = free_port()
    process = subprocess.Popen(server_command("--port", str(port), "--catalog", CATALOG,
                                              "--assert-claimed", "--gdm", "off", root=ROOT),
                               cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(150):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            if mc(port, "/identity"):
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def counts_follow_updated(row):
    keys = list(row)
    anchor = "deletedAt" if "deletedAt" in row else "updatedAt"
    at = keys.index(anchor) if anchor in keys else -1
    return at >= 0 and keys[at + 1:at + 3] == ["globalViewCount", "userCount"]


def reference_checks():
    fixture = json.load(open(FIXTURE))
    for exchange in fixture["exchanges"]:
        for index, top in enumerate(exchange["top"]):
            label = "%s %s row %s" % (exchange["server"], exchange["request"]["path"], top["ratingKey"])
            check("reference: %s counts follow updatedAt/deletedAt" % label, counts_follow_updated(top),
                  list(top))
            check("reference: %s carries no Guid, Rating or Location" % label,
                  not ({"Guid", "Rating", "Location"} & set(top)), sorted(top))
            if exchange.get("listing"):
                listing = exchange["listing"][index]
                extra = set(top) - set(listing)
                dropped = set(listing) - set(top)
                check("reference: %s is its listing row plus %s" % (
                    label, sorted(expected_extra(top["type"]))),
                      extra == expected_extra(top["type"]), sorted(extra))
                expect = AGGREGATE & set(listing) if top["type"] in ("show", "season") else set()
                check("reference: %s drops %s" % (label, sorted(expect) or "nothing"),
                      dropped == expect, sorted(dropped))


def seed(port):
    """One movie and one episode watched through ordinary timelines."""
    episode = (mc(port, "/library/sections/2/allLeaves").get("Metadata") or [{}])[0].get("ratingKey")
    for key in ("1007", episode):
        for state, at in (("playing", 10000), ("stopped", 1250000)):
            mc(port, "/:/timeline?ratingKey=%s&key=/library/metadata/%s&state=%s&time=%d&duration=1300000"
               % (key, key, state, at), {"X-Plex-Session-Identifier": "c94d-%s" % key})
    return episode


def listing_row(port, section, key, kind):
    path = "/library/sections/%s/all?type=%d" % (section, kind)
    return next((m for m in mc(port, path).get("Metadata") or [] if m.get("ratingKey") == key), None)


def server_checks(port):
    seed(port)
    for kind, section in ((1, 1), (2, 2), (3, 2)):
        rows = mc(port, "/library/all/top?type=%d&limit=10" % kind).get("Metadata") or []
        check("type=%d answers a played row" % kind, rows, rows)
        for top in rows:
            label = "type=%d row %s" % (kind, top.get("ratingKey"))
            check("%s carries no Guid, Rating or Location" % label,
                  not ({"Guid", "Rating", "Location"} & set(top)),
                  sorted({"Guid", "Rating", "Location"} & set(top)))
            check("%s puts globalViewCount, userCount straight after updatedAt" % label,
                  counts_follow_updated(top), list(top))
            check("%s ends with its User list" % label, list(top)[-1:] == ["User"], list(top)[-3:])
            listing = listing_row(port, section, top.get("ratingKey"), kind)
            if listing is None:
                check("%s has a listing row to compare" % label, False)
                continue
            extra = set(top) - set(listing)
            check("%s is its listing row plus the measured additions" % label,
                  extra == expected_extra(top.get("type")), sorted(extra))
            if top.get("type") == "season":
                keys = list(top)
                check("%s puts leafCount, viewedLeafCount just before addedAt" % label,
                      "addedAt" in keys and keys[keys.index("addedAt") - 2:keys.index("addedAt")]
                      == ["leafCount", "viewedLeafCount"], keys)
            expect = AGGREGATE & set(listing) if top.get("type") in ("show", "season") else set()
            dropped = set(listing) - set(top)
            check("%s drops %s" % (label, sorted(expect) or "nothing"), dropped == expect,
                  sorted(dropped))


def main():
    reference_checks()
    process, port = start()
    try:
        server_checks(port)
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
    print("PASS %d/%d" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
