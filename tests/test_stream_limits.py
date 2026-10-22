#!/usr/bin/env python3
"""Exercise WAN limits through real non-loopback connections and recorded grants."""
import concurrent.futures
import http.server
import json
import os
import shutil
import socket
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from test_accounts import (FIXTURES, MACHINE, OWNER, SERVER_TOKEN, SHARED,
                           PlexTv, Server, catalog, free_port)
from test_feed import BODY, BYTES_TOKEN, Bytes
from test_share_restrictions import ask as local_ask


def main():
    # Connecting a UDP socket selects a local interface without sending any packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 9))
        address = route.getsockname()[0]
    assert not address.startswith("127."), "a real non-loopback interface is required"
    work = tempfile.mkdtemp(prefix="rex-stream-limits-")
    plex = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), PlexTv)
    source = Bytes()
    source.files.add("/dav/1.mkv")
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), source.handler())
    for service in (plex, upstream):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    server = None
    try:
        fixture = json.load(open(os.path.join(FIXTURES, "shared_user_s1434.json")))["exchanges"]
        with open(os.path.join(work, "catalog.json"), "w") as out:
            json.dump(catalog(fixture), out)
        with open(os.path.join(work, "claim_state.json"), "w") as out:
            json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "owner",
                       "email": "owner@example.com", "feature_uuids": [], "account_id": 316687974,
                       "view_state_cursor": None, "access_tokens": []}, out)
        server = Server(work, plex.server_address[1])
        assert local_ask(server, OWNER, "/rex/sources/limits", "PUT", {
            "url": "http://127.0.0.1:%d/dav" % upstream.server_address[1],
            "mount": "/data", "bearer": BYTES_TOKEN})[0] == 200
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(token, session, path, query=(), headers=None):
            pairs = list(query) + [("X-Plex-Token", token), ("X-Plex-Session-Identifier", session),
                                  ("X-Plex-Client-Identifier", "same-wan-device"), ("location", "lan")]
            url = "http://%s:%d%s?%s" % (address, server.port, path, urllib.parse.urlencode(pairs))
            req = urllib.request.Request(url, headers=dict({"Accept": "application/json"}, **(headers or {})))
            try:
                with opener.open(req, timeout=30) as response:
                    raw = response.read()
                    return response.status, json.loads(raw) if raw[:1] == b"{" else raw
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        def prefs(**values):
            assert server.ask(OWNER, "PUT", "/:/prefs", list(values.items()))[0] == 200

        def part(token, session):
            return request(token, session, "/library/parts/10/1789187071/file.mkv",
                           headers={"Range": "bytes=0-7", "X-Forwarded-For": "127.0.0.1"})

        def stop(token, session):
            assert request(token, session, "/:/timeline", [("ratingKey", "1"),
                ("key", "/library/metadata/1"), ("state", "stopped"), ("time", "0"),
                ("duration", "10031")])[0] == 200

        prefs(LanNetworksBandwidth="127.0.0.0/8", WanPerUserStreamCount=1,
              WanTotalMaxUploadRate=0, WanPerStreamMaxUploadRate=0)
        assert part(OWNER, "alpha") == (206, BODY[:8])
        # Neither client location=lan nor a forged forwarding hop can turn a WAN peer into LAN.
        assert part(OWNER, "beta")[0] == 503
        # Same device and raw session identifier, different account: a separate allowance.
        assert part(SHARED, "alpha") == (206, BODY[:8])
        stop(OWNER, "alpha")
        assert part(OWNER, "beta") == (206, BODY[:8])
        assert part(SHARED, "beta")[0] == 503
        stop(OWNER, "beta")
        stop(SHARED, "alpha")
        prefs(WanPerUserStreamCount=0, WanTotalMaxUploadRate=6000)
        barrier = threading.Barrier(2)

        def simultaneous(token):
            barrier.wait(timeout=10)
            return token, part(token, "race")[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(simultaneous, (OWNER, SHARED)))
        assert sorted(code for _, code in results) == [206, 503], results
        winner = next(token for token, code in results if code == 206)
        loser = next(token for token, code in results if code == 503)
        stop(winner, "race")
        assert part(loser, "race") == (206, BODY[:8])
        stop(loser, "race")
        prefs(WanTotalMaxUploadRate=0, WanPerStreamMaxUploadRate=1000)
        assert part(OWNER, "too-large")[0] == 503
        # The configured LAN remains exempt from the WAN bitrate policy.
        assert local_ask(server, OWNER, "/library/parts/10/1789187071/file.mkv",
                         headers={"Range": "bytes=0-7"}) == (206, BODY[:8])
        print("PASS WAN limits: actual peer, per-account slots, concurrent total budget, stop release and LAN exemption")
    finally:
        if server is not None:
            server.stop()
        for service in (plex, upstream):
            service.shutdown()
            service.server_close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
