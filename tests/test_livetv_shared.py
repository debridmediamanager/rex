#!/usr/bin/env python3
"""A shared user's Live TV, answered as a PMS with a DVR answers it with and without the share's
Live TV (`allowTuners`).

plexav (PMS 1.43.4, an HDHomeRun and an XMLTV guide) was asked the Live TV reads Plex Web and Plex
for Android make, and a tune, by its owner and by a managed user it shared every library with,
once with the share's `allowTuners` 1 and once 0 (`fixtures/livetv/shared_tuners_s1434.json`,
`probes/record_livetv_shared.py`). This claims rex against a stub plex.tv listing that share
(test_accounts' access-token list, every library, its `allowTuners` set), pushes plexav's own guide
(`guide_s1434.json`) as rex's one DVR, and replays each request as each account in the recorded
order: the status, `/media/providers`' providers and What's On's hubs. rex's DVR is 2 and the guide
has plexav's channel 2.1, so the recorded paths replay unchanged. No bytes are read: the tune's
answer is all that is compared, so no FFmpeg is needed.

    python3 tests/test_livetv_shared.py
"""
import gzip
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_accounts as accounts  # noqa: E402
from livetv_source import push  # noqa: E402
from test_perf_livetv import PROBE  # noqa: E402
from test_ports import free_port  # noqa: E402

LIVETV = os.path.join(accounts.FIXTURES, "livetv")
RECORDED = json.load(open(os.path.join(LIVETV, "shared_tuners_s1434.json")))["runs"]
GUIDE = json.load(open(os.path.join(LIVETV, "guide_s1434.json")))
NOW = json.load(gzip.open(os.path.join(LIVETV, "reads_s1434.json.gz"), "rt"))["now"]

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, "" if ok else "  -- " + str(detail)[:400]))


def run(setting, exchanges):
    print("allowTuners=%s" % setting)
    accounts.PlexTv.tokens = accounts.LIST.replace('allowTuners="0"', 'allowTuners="%s"' % setting) \
        .replace('filterMovies="contentRating=G%2CPG"', 'filterMovies=""')
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), accounts.PlexTv)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    work = tempfile.mkdtemp(prefix="rex-livetv-shared-")
    json.dump({"machineIdentifier": accounts.MACHINE, "friendlyName": "plexav", "items": [], "sections": [
        {"key": "1", "type": "movie", "title": "Movies", "uuid": "0" * 39 + "1"}]}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump({"machine": accounts.MACHINE, "token": accounts.SERVER_TOKEN, "owner": "owner", "email": "owner@example.com",
               "feature_uuids": [], "account_id": 316687974, "view_state_cursor": None, "access_tokens": []},
              open(os.path.join(work, "claim_state.json"), "w"))
    server = accounts.Server(work, stub.server_address[1])
    try:
        items = [{"id": "channels/%s.m3u8" % channel["number"], "path": "channels/%s.m3u8" % channel["number"],
                  "display": {"title": channel["title"]}, "media_info": PROBE, "channel": channel} for channel in GUIDE["channels"]]
        answer = push("http://127.0.0.1:%d" % server.port, types.SimpleNamespace(url="http://127.0.0.1:9"), items)
        print("  pushed %s" % json.dumps(answer))
        for exchange in exchanges:
            who, method, path = exchange["who"], exchange["method"], exchange["path"]
            token = accounts.OWNER if who == "owner" else accounts.SHARED
            status, document = server.ask(token, method, path, [tuple(pair) for pair in exchange["query"]])
            label = "allowTuners=%s: %s %s %s" % (setting, who, method, path)
            got, wanted = [status], [exchange["status"]]
            container = (document or {}).get("MediaContainer", {})
            if "providers" in exchange:
                got.append([provider.get("identifier") for provider in container.get("MediaProvider", [])])
                wanted.append(exchange["providers"])
            if "hubs" in exchange:
                # The hubs PMS lists, by identifier: their sizes are the moment's guide and its favourites.
                got.append([hub.get("hubIdentifier") for hub in container.get("Hub", [])])
                wanted.append([hub for hub, _ in exchange["hubs"]])
            check(label, got == wanted, "rex %s, plexav %s" % (got, wanted))
    finally:
        server.stop()
        stub.shutdown()
        shutil.rmtree(work, ignore_errors=True)


def main():
    os.environ.update(TZ="Europe/Amsterdam", REX_LIVETV_NOW=str(NOW))
    for name, exchanges in sorted(RECORDED.items()):
        run(name.split("=")[1], exchanges)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
