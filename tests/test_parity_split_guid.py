#!/usr/bin/env python3
"""A split-off row's identity, and the one `includeGuids` gate for unmatched rows.

Both halves are driven from `tests/fixtures/split_guid.json`, recorded on plexscratch
(1.43.4) and plexscratch42 (1.42.2) on 2026-10-01:

- `PUT /library/metadata/17/merge?ids=3` then `PUT /library/metadata/17/split` hands
  the absorbed item back on a NEW ratingKey (58) that is 17 in every field but
  `ratingKey`, `key` and the artwork URLs built from them -- 17's title, its
  `plex://movie/...` guid, its whole `Guid` list, its `Field` locks, `addedAt` and
  `updatedAt` -- holding the absorbed item's Media. 17 keeps its own Media and its
  `updatedAt`. rex minted `local://<new key>`, dropped the locks and stamped 17.
- Under `includeGuids=1`, NO listing gives an unmatched row (`local://` or
  `tv.plex.agents.none://`) a `Guid` key -- the grid, `/unwatched`, Recently Added, the
  hub page, `/hubs` and home's Recently Added -- even where its detail still answers
  the imdb/tmdb list the unmatch kept. rex kept an unmatched movie's stored ids in
  every listing and gave a `local://` row a one-entry list on `/hubs`.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_parity_split_guid.py
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
FIXTURE = os.path.join(HERE, "fixtures", "split_guid.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-split-guid",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}

# The catalog's rows the checks stand on: 1007 the merge target and 1008 the absorbed
# row (one Media each), 1013 a matched movie to unmatch, 1014 a `local://` movie, 1012 a
# matched control; 1001 an episode and 30004 a season to unmatch.
TARGET, ABSORBED, UNMATCH, LOCAL, CONTROL = "1007", "1008", "1013", "1014", "1012"
TARGET_GUID = "plex://movie/520a1a3c2c634f2594808ee9"
TARGET_GUIDS = ["imdb://tt5129250", "tmdb://133701"]
UNMATCH_GUIDS = ["imdb://tt14682800", "tmdb://831395"]

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


def rows(port, path):
    """Every Metadata row of a listing or a hub page, first occurrence per ratingKey."""
    _, body = request(port, "GET", path)
    container = (body or {}).get("MediaContainer", {}) if isinstance(body, dict) else {}
    found = list(container.get("Metadata") or [])
    for hub in container.get("Hub") or []:
        found += hub.get("Metadata") or []
    out = {}
    for row in found:
        out.setdefault(str(row.get("ratingKey")), row)
    return out


def detail(port, key, query=""):
    return rows(port, "/library/metadata/%s%s" % (key, query)).get(key) or {}


def guid_ids(row):
    return None if "Guid" not in row else [g.get("id") for g in row["Guid"] or []]


def media_ids(row):
    return [str(m.get("id")) for m in row.get("Media") or []]


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


def run_split(port, fixture):
    split = fixture["split"]
    first, second = split["before"]["17"]["row"], split["before"]["3"]["row"]
    source, created = split["after"]["17"]["row"], split["after"]["58"]["row"]
    # What the reference measured, read off the fixture rather than restated.
    differing = {k for k in set(source) | set(created) if source.get(k) != created.get(k)}
    carried = sorted(k for k in set(source) & set(created) if k not in differing | {"Media"})
    check("fixture: the split-off row differs from the first only in its own keys and art",
          differing == {"ratingKey", "key", "thumb", "art", "Image", "Media"}, sorted(differing))
    check("fixture: the split-off row carries the first's guid, Guid list and locks",
          created["guid"] == first["guid"] != second["guid"]
          and created["Guid"] == first["Guid"] and created.get("Field") == first.get("Field")
          and created.get("Field"), created.get("guid"))
    check("fixture: the source keeps its Media and updatedAt, the new row the absorbed Media",
          media_ids(source) == media_ids(first) and media_ids(created) == media_ids(second)
          and source["updatedAt"] == split["merged_17"]["row"]["updatedAt"] == first["updatedAt"],
          (media_ids(source), media_ids(created)))

    # A lock on the target, so there is a `Field` list for the split to carry.
    request(port, "PUT", "/library/sections/1/all?type=1&id=%s&summary.locked=1" % TARGET)
    for write in split["writes"]:
        path = write["path"].replace("/17/", "/%s/" % TARGET).replace("ids=3", "ids=" + ABSORBED)
        status, _ = request(port, write["method"], path)
        check("%s answers %s" % (path, write["status"]), status == write["status"], status)
        if "merge" in path:
            merged_at = detail(port, TARGET).get("updatedAt")
            # A second's gap, so a split that stamps the row cannot hide in the same second.
            time.sleep(1.1)
    listing = rows(port, "/library/sections/1/all")
    new = [key for key in listing if key not in (TARGET, "1009", CONTROL, UNMATCH, LOCAL)]
    check("the split hands the absorbed row back on one new ratingKey",
          len(new) == 1 and new[0] != ABSORBED, sorted(listing))
    if len(new) != 1:
        return
    key = new[0]
    after_source, after_new = detail(port, TARGET, "?includeGuids=1"), detail(port, key, "?includeGuids=1")
    check("the split-off row carries the first row's guid",
          after_new.get("guid") == after_source.get("guid") == TARGET_GUID, after_new.get("guid"))
    check("the split-off row's detail carries the first row's Guid list",
          guid_ids(after_new) == TARGET_GUIDS, guid_ids(after_new))
    check("the split-off row carries the first row's Field locks",
          after_new.get("Field") and after_new.get("Field") == after_source.get("Field"),
          (after_new.get("Field"), after_source.get("Field")))
    shared = [name for name in carried if name in after_source]
    unequal = [name for name in shared if after_new.get(name) != after_source.get(name)]
    check("the split-off row equals the first in every field the reference carried over",
          shared and not unequal, unequal)
    check("the source keeps its own Media and the new row the absorbed one",
          media_ids(after_source) == ["100700"] and media_ids(after_new) == ["100800"],
          (media_ids(after_source), media_ids(after_new)))
    check("the split leaves the source's updatedAt where the merge left it",
          after_source.get("updatedAt") == merged_at, (merged_at, after_source.get("updatedAt")))
    for recorded in split["listings_after"]:
        wanted = {row["ratingKey"]: row.get("Guid") for row in recorded["rows"]}
        if wanted.get("58") is None or wanted.get("58") != wanted.get("17"):
            continue
        path = recorded["path"]
        if path.startswith("/hubs?"):
            continue  # home's hubs hold Continue Watching etc.; covered by the gate below
        found = rows(port, path)
        check("%s: the split-off row carries the first's Guid list" % path,
              key in found and guid_ids(found[key]) == TARGET_GUIDS
              and guid_ids(found.get(TARGET) or {}) == TARGET_GUIDS,
              guid_ids(found.get(key) or {}))


def run_unmatched(port, fixture):
    unmatched, older = fixture["unmatched"], fixture["agents_none_1_42"]
    check("fixture: the unmatched movie's detail keeps its ids",
          all(r["rows"][0].get("Guid") == ["imdb://tt1254207", "tmdb://10378", "tvdb://12352"]
              and r["rows"][0]["guid"] == "tv.plex.agents.none://1"
              for r in unmatched["movie_detail"]))
    check("fixture: no listing gives the unmatched movie a Guid key, on 1.43.4 and 1.42.2",
          all(r["rows"] and "Guid" not in r["rows"][0] for r in unmatched["movie_listings"])
          and all(row.get("guid", "").startswith(("local://", "tv.plex.agents.none://"))
                  and "Guid" not in row for r in older["listings"] + older["detail"]
                  for row in r["rows"]))
    status, _ = request(port, "PUT", "/library/metadata/%s/unmatch" % UNMATCH)
    check("unmatch answers 200", status == 200, status)
    row = detail(port, UNMATCH, "?includeGuids=1")
    check("the unmatched movie is on the none agent and its detail keeps its ids",
          row.get("guid") == "tv.plex.agents.none://%s" % UNMATCH
          and guid_ids(row) == UNMATCH_GUIDS, (row.get("guid"), guid_ids(row)))
    for recorded in unmatched["movie_listings"]:
        path = recorded["path"]
        found = rows(port, path)
        for key, label in ((UNMATCH, "the none-agent row"), (LOCAL, "the local:// row")):
            check("%s: %s carries no Guid key" % (path, label),
                  key in found and "Guid" not in found[key], guid_ids(found.get(key) or {}))
        check("%s: a matched row keeps its list" % path,
              CONTROL in found and guid_ids(found[CONTROL]) == ["imdb://tt15479902", "tmdb://946327"],
              guid_ids(found.get(CONTROL) or {}))
    # The show half: an episode and a season unmatch to `local://`, and every child list
    # under `includeGuids=1` leaves them without a key (plexscratch's 9, 11 and 12-14).
    request(port, "PUT", "/library/metadata/1001/unmatch")
    request(port, "PUT", "/library/metadata/30004/unmatch")
    for path, keys in (("/library/metadata/20001/children?includeGuids=1", ["30004"]),
                       ("/library/metadata/30001/children?includeGuids=1", ["1001"]),
                       ("/library/metadata/20001/allLeaves?includeGuids=1", ["1001", "1005"]),
                       ("/library/metadata/20001/grandchildren?includeGuids=1", ["1001", "1005"]),
                       ("/library/sections/2/all?type=4&includeGuids=1", ["1001", "1005"])):
        found = rows(port, path)
        check("%s: the local:// rows carry no Guid key" % path,
              all(k in found and "Guid" not in found[k] for k in keys),
              {k: guid_ids(found.get(k) or {}) for k in keys})
    # The second pass: `/library/all` and both searches follow the same gate, and a SHOW
    # unmatched to the none agent keeps its ids on its detail and nowhere else.
    second = {r["path"]: r["rows"] for r in unmatched["second_pass"]}
    check("fixture: /library/all, the section search and /hubs/search give the none row no Guid",
          all(row["guid"] != "tv.plex.agents.none://1" or "Guid" not in row
              for rows_ in second.values() for row in rows_ if row["type"] != "show")
          and all(any(row["ratingKey"] == "1" for row in second[p])
                  for p in second if "metadata/6" not in p and "sections/2" not in p))
    for recorded, path in (("/library/all?includeGuids=1", None),
                           ("/library/all?type=1&includeGuids=1", None),
                           ("/library/sections/1/search?type=1&query=b&includeGuids=1",
                            "/library/sections/1/search?type=1&query=tuesday&includeGuids=1"),
                           ("/hubs/search?query=big&includeGuids=1",
                            "/hubs/search?query=tuesday&includeGuids=1")):
        path = path or recorded
        found = rows(port, path)
        check("%s: the none-agent row carries no Guid key" % path,
              recorded in second and UNMATCH in found and "Guid" not in found[UNMATCH],
              guid_ids(found.get(UNMATCH) or {}))
    show = second["/library/metadata/6?includeGuids=1"][0]
    check("fixture: the none-agent show's detail keeps its ids, its grid row has no key",
          show["guid"] == "tv.plex.agents.none://6" and show.get("Guid")
          and "Guid" not in second["/library/sections/2/all?includeGuids=1"][0])
    request(port, "PUT", "/library/metadata/20001/unmatch")
    shown = detail(port, "20001", "?includeGuids=1")
    grid = rows(port, "/library/sections/2/all?includeGuids=1").get("20001") or {}
    check("an unmatched show keeps its ids on its detail and has no Guid key on the grid",
          shown.get("guid") == "tv.plex.agents.none://20001"
          and guid_ids(shown) == ["imdb://tt0096697", "tmdb://456"]
          and grid and "Guid" not in grid, (shown.get("guid"), guid_ids(shown), guid_ids(grid)))


def main():
    fixture = json.load(open(FIXTURE))
    for part in (run_split, run_unmatched):
        process, port = start()
        try:
            part(port, fixture)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    passed = sum(results)
    print("PASS %d/%d split and Guid-gate parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
