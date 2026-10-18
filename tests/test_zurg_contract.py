#!/usr/bin/env python3
"""zurg's side of the feed, as zurg itself sends it.

The other suites push zurg-shaped items written here. This one replays the contract zurg
generates from its own feed code (`internal/rexfeed/testdata/rex-contract.json` in the zurg
repository, written by its `contract_test.go`): the registration, the snapshot, a push and a
delete, in the order zurg sends them, then the two refusals zurg tells apart by their answers.
CI checks zurg's main out beside rex, so a change on either side that breaks the other fails
here.

    python3 tests/test_zurg_contract.py [path/to/rex-contract.json]

The path defaults to $ZURG_CONTRACT, then to a zurg checkout beside this one.
"""
import json
import os
import shutil
import sys
import tempfile

from test_feed import LEGACY, Bytes, Server, catalog, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(HERE, "..", "..", "zurg", "internal", "rexfeed", "testdata", "rex-contract.json")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ZURG_CONTRACT", DEFAULT)
    contract = json.load(open(path))
    requests = contract["requests"]
    mount = requests[0]["body"]["mount"]
    work = tempfile.mkdtemp(prefix="rex-zurg-contract-")
    catalog(work)
    # The libraries where zurg's mount puts its directories.
    document = json.load(open(os.path.join(work, "catalog.json")))
    document["sections"] = json.loads(json.dumps(LEGACY["sections"]).replace('"/zurg/', '"%s/' % mount))
    json.dump(document, open(os.path.join(work, "catalog.json"), "w"))
    server = Server(work, serve(Bytes().handler()))
    try:
        snapshot = None
        for sent in requests:
            name = "%s %s" % (sent["method"], sent["path"])
            body = sent["body"]
            status, answer = server.request(sent["path"], sent["method"], body, {"Content-Type": "application/json"})
            check("%s answers 200" % name, status == 200, (status, answer))
            answer = answer if isinstance(answer, dict) else {}
            check("%s rejects nothing" % name, not answer.get("rejected"), answer)
            counts = lambda *fields: sum(answer.get(field, 0) for field in fields)
            if "items" in body:
                check("%s accounts for each of its %d items" % (name, len(body["items"])),
                      counts("created", "updated", "held", "unchanged") == len(body["items"]), answer)
            if "ids" in body:
                check("%s removes each of its %d ids" % (name, len(body["ids"])),
                      counts("trashed", "deleted", "released") == len(body["ids"]), answer)
            if sent["path"].endswith("/snapshot"):
                snapshot = body
                _, shown = server.feed("GET", "items")
                _, held = server.feed("GET", "held")
                shown_ids = {row["id"] for row in shown.get("items", []) if not row.get("deleted")}
                held_ids = {row["id"] for row in held.get("held", [])}
                probed = {item["id"] for item in body["items"] if item["media_info"]}
                check("the snapshot shows every probed item", shown_ids == probed, (shown_ids ^ probed))
                check("the snapshot holds every unprobed item",
                      held_ids == {item["id"] for item in body["items"]} - probed, held_ids)
                # What zurg says each file is, rex makes it: an extra a clip, an episode where zurg put it.
                keys = server.keys()
                for item in body["items"]:
                    if not item["media_info"]:
                        continue
                    row = server.row(keys.get(item["id"]))
                    # The kinds rex reads; any other it shows as a film without a word.
                    want = {"extra": "clip", "episode": "episode", "movie": "movie", None: "movie"}.get(item.get("kind"))
                    check("rex knows the kind %r zurg sends" % item.get("kind"), want, item["id"])
                    got = [row.get("type"), row.get("parentIndex"), row.get("index")]
                    expected = [want, item.get("season"), item.get("episode")] if want == "episode" else [want]
                    check("%s is the %s zurg pushed" % (item["id"].rsplit("/", 1)[-1], want),
                          got[:len(expected)] == expected, got)
        _, shown = server.feed("GET", "items")
        gone = {row["id"] for row in shown.get("items", []) if not row.get("deleted")}
        for sent in requests:
            if sent["method"] == "DELETE":
                check("a deleted id is no longer shown", not gone & set(sent["body"]["ids"]), gone)

        # The two 409s, and what zurg tells them apart by (rexfeed.send).
        refusals = contract["refusals"]
        held_only = [item for item in snapshot["items"] if not item["media_info"]]
        status, answer = server.feed("PUT", "snapshot", {"session": "contract-shrink", "part": 0, "complete": True,
                                                         "items": held_only})
        want = refusals["shrink"]
        check("a snapshot that removes most of the library is refused as zurg reads it",
              status == want["status"] and all(key in answer for key in want["keys"]), (status, answer))
        status, answer = server.feed("PUT", "snapshot", {"session": "never-started", "part": 1, "complete": True, "items": []})
        want = refusals["unknown_session"]
        check("a chunk of a session rex does not hold is refused as zurg reads it",
              status == want["status"] and all(key in answer for key in want["keys"]) and "would_remove" not in answer,
              (status, answer))
    finally:
        server.stop()
    shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
