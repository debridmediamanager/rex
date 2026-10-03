#!/usr/bin/env python3
"""Cycle 90, B: a rating write that changes nothing is not a write.

`tests/fixtures/rating_noop_s42.json` (plexscratch42, 1.42.2) drives one write at a time
through both rating paths -- `/:/rate?rating=` and the section path's `userRating.value=`
(the Edit dialog's save) -- on an episode that starts unrated, and reads after each write
the stored rating, whether `lastRatedAt` is unset, kept or new, and whether the write
stamped `viewUpdatedAt` (the section filter `viewUpdatedAt>>=` the second before it):

- a value EQUAL to the stored rating (`7` after `7`, `7.0`, `10.0` after `10`, `0` after
  `0`, either path after the other) stamps nothing: `lastRatedAt` keeps the first rating's
  time and the row's `viewUpdatedAt` does not move;
- a clear (`-1`, or an empty value on the section path) of a row with NO stored rating
  stamps nothing either, and keeps a `lastRatedAt` a `-5` left behind;
- any other negative value is never "unchanged": it clears the stars and stamps both,
  every time;
- the section path is a rating write like `/:/rate`: a change there stamps
  `viewUpdatedAt` too, and `-5` there stamps `lastRatedAt`.

A collection keeps the same `lastRatedAt` rule (python-plexapi's `Collection.rate(7)` on
a collection that already reads 7 -- B's chain does it on every re-created `C63B Coll`).

Both servers re-stamped `lastRatedAt` and `viewUpdatedAt` on every `/:/rate`, stamped
`viewUpdatedAt` on a clear of nothing, and never stamped it on the section path, where a
`-5` cleared `lastRatedAt` instead. CLIENT-VISIBLE: a section's Recently Watched shelf and
its `/onDeck` order by `viewUpdatedAt` (cycle 54), and python-plexapi reads `lastRatedAt`.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle90_b.py
    python3 tests/test_parity_cycle90_b.py --capture BASE TOKEN OUT   # re-measure (writes!)
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "rating_noop_s42.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle90-b", "Accept": "application/json"}
# An unrated episode in a show library, and a show to put in a collection:
# plexscratch42's and catalog_ui's.
IDS = {"s42": {"item": "393", "section": "3", "member": "391"},
       "sim": {"item": "1005", "section": "2", "member": "20001"}}
SEQUENCES = {
    "unchanged_value": "rate:7,rate:7,rate:7.0,sect:7,sect:7.0,sect:8,sect:8,rate:8,rate:10,"
                       "rate:10.0,rate:7.5,rate:7.50,rate:0,rate:0,sect:0",
    "clear": "rate:-1,rate:-1,sect:,sect:-1,rate:7,rate:-1,rate:-1,sect:7,sect:,sect:,sect:7,"
             "sect:-1,sect:-1",
    "other_negative": "rate:-5,rate:-5,rate:-1,sect:-1,sect:,sect:-5,sect:-5,rate:7,sect:-5,"
                      "rate:7,rate:-5",
}
COLLECTION_SEQUENCE = "rate:7,rate:7,rate:8,rate:8,rate:-1,rate:-1"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


class Client:
    def __init__(self, base, token=None):
        parsed = urllib.parse.urlsplit(base)
        self.host, self.port, self.token = parsed.hostname, parsed.port, token

    def __call__(self, method, path):
        if self.token:
            path += ("&" if "?" in path else "?") + "X-Plex-Token=" + self.token
        connection = http.client.HTTPConnection(self.host, self.port, timeout=60)
        try:
            connection.request(method, path, headers=dict(HEADERS, Connection="close"))
            response = connection.getresponse()
            data = response.read()
            try:
                return response.status, json.loads(data) if data.strip() else None
            except ValueError:
                return response.status, None
        finally:
            connection.close()


def next_second():
    """Sleep into the next whole second and return it: every earlier write is before it."""
    now = time.time()
    time.sleep(int(now) + 1 - now + 0.05)
    return int(time.time())


class Driver:
    def __init__(self, client, ids):
        self.client, self.ids = client, ids

    def read(self, key):
        row = self.client("GET", "/library/metadata/%s" % key)[1]["MediaContainer"]["Metadata"][0]
        return row.get("userRating"), row.get("lastRatedAt")

    def stamped(self, key, since):
        body = self.client("GET", "/library/sections/%s/all?type=4&viewUpdatedAt>>=%d"
                           % (self.ids["section"], since))[1]
        return key in [str(row["ratingKey"]) for row in body["MediaContainer"].get("Metadata", [])]

    def write(self, key, kind, value, row_type):
        if kind == "rate":
            path = "/:/rate?key=%s&identifier=com.plexapp.plugins.library&rating=%s" % (key, value)
        else:
            path = "/library/sections/%s/all?type=%s&id=%s&userRating.value=%s" % (
                self.ids["section"], row_type, key, value)
        return self.client("PUT", path)[0]

    def run(self, key, row_type, sequence, viewstamp):
        rows, previous = [], self.read(key)[1]
        for step in sequence.split(","):
            kind, value = step.split(":")
            # `since` is a second no earlier write reached, and the write lands a whole second
            # after it -- the spacing the fixture was measured with, so `>>=` reads the same
            # whether it is inclusive or not.
            since = next_second()
            next_second()
            status = self.write(key, kind, value, row_type)
            rating, rated_at = self.read(key)
            row = {"write": step, "status": status, "userRating": rating,
                   "lastRatedAt": "unset" if rated_at is None else
                   ("new" if rated_at != previous and rated_at >= since else "kept")}
            if viewstamp:
                row["viewUpdatedAt_stamped"] = self.stamped(key, since)
            rows.append(row)
            previous = rated_at
        return rows

    def clear(self, key):
        # A rating then -1: the only write that leaves both columns unset.
        for value in (7, -1):
            self.write(key, "rate", value, 4)

    def collection(self):
        machine = self.client("GET", "/identity")[1]["MediaContainer"]["machineIdentifier"]
        uri = urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/metadata/%s"
                                 % (machine, self.ids["member"]), safe="")
        title = urllib.parse.quote("C90B Rate %d" % time.time())
        status, body = self.client("POST", "/library/collections?sectionId=%s&smart=0&title=%s"
                                           "&type=2&uri=%s" % (self.ids["section"], title, uri))
        return str(body["MediaContainer"]["Metadata"][0]["ratingKey"])

    def cleanup(self, collection):
        self.client("DELETE", "/library/collections/%s" % collection)
        # Joining a collection locks the member's `collection` field.
        self.client("PUT", "/library/sections/%s/all?type=2&id=%s&collection.locked=0"
                    % (self.ids["section"], self.ids["member"]))


def measure(client, ids):
    driver = Driver(client, ids)
    item = ids["item"]
    assert driver.read(item) == (None, None), "the item must start unrated"
    out = {"item": {}, "collection": {}}
    for name, sequence in SEQUENCES.items():
        out["item"][name] = driver.run(item, 4, sequence, True)
        driver.clear(item)
    collection = driver.collection()
    try:
        out["collection"]["unchanged_value"] = driver.run(collection, 18, COLLECTION_SEQUENCE, False)
    finally:
        driver.cleanup(collection)
    return out


def capture(base, token, out_path):
    measured = measure(Client(base, token), IDS["s42"])
    measured.update({"reference": "plexscratch42, PMS 1.42.2", "measured": time.strftime("%Y-%m-%d")})
    json.dump(measured, open(out_path, "w"), indent=1)
    print("wrote", out_path)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def start():
    port = free_port()
    args = ["--port", port, "--catalog", CATALOG, "--assert-claimed", "--gdm", "off"]
    process = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=ENV,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = Client("http://127.0.0.1:%s" % port)
    for _ in range(150):
        if process.poll() is not None:
            break
        try:
            if client("GET", "/identity")[0] == 200:
                return process, client
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


def main():
    if sys.argv[1:2] == ["--capture"]:
        capture(*sys.argv[2:5])
        return 0
    fixture = json.load(open(FIXTURE, encoding="utf-8"))
    process, client = start()
    try:
        got = measure(client, IDS["sim"])
    finally:
        stop(process)
    for part in ("item", "collection"):
        for name, want_rows in fixture[part].items():
            got_rows = got[part].get(name, [])
            check("%s %s ran every write" % (part, name), len(got_rows) == len(want_rows),
                  len(got_rows))
            for index, (w, g) in enumerate(zip(want_rows, got_rows)):
                label = "%s %s step %d %s" % (part, name, index + 1, w["write"])
                for field in ("status", "userRating", "lastRatedAt", "viewUpdatedAt_stamped"):
                    if field in w:
                        check("%s: %s %s" % (label, field, json.dumps(w[field])),
                              g.get(field) == w[field], json.dumps(g.get(field)))
    passed = sum(results)
    print("PASS %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
