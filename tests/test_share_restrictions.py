#!/usr/bin/env python3
"""Local restrictions can narrow recorded cloud grants, never broaden them."""
import http.server
import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from test_accounts import (FIXTURES, MACHINE, OWNER, SERVER_TOKEN, SHARED, SHARED_ID,
                           VARIANTS, PlexTv, Server, catalog, free_port)
from test_feed import BODY, BYTES_TOKEN, Bytes


def ask(server, token, path, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (server.port, path), data=data,
                                     method=method, headers=dict({"X-Plex-Token": token,
                                     "Accept": "application/json", "Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw[:1] == b"{" else raw
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def main():
    work = tempfile.mkdtemp(prefix="rex-share-restrictions-")
    previous = PlexTv.tokens
    PlexTv.tokens = VARIANTS[1][2]  # Recorded allowSync=1 and label!=Rexhide grant.
    plex = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), PlexTv)
    source = Bytes()
    source.files.add("/dav/4.mkv")
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), source.handler())
    for service in (plex, upstream):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    server = None
    try:
        fixture = json.load(open(os.path.join(FIXTURES, "shared_user_label_s1434.json")))["exchanges"]
        with open(os.path.join(work, "catalog.json"), "w") as out:
            json.dump(catalog(fixture), out)
        with open(os.path.join(work, "claim_state.json"), "w") as out:
            json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "owner",
                       "email": "owner@example.com", "feature_uuids": [], "account_id": 316687974,
                       "view_state_cursor": None, "access_tokens": []}, out)
        server = Server(work, plex.server_address[1])
        route = "/rex/shares/%s/restrictions" % SHARED_ID
        assert ask(server, OWNER, "/rex/sources/acl", "PUT", {
            "url": "http://127.0.0.1:%d/dav" % upstream.server_address[1],
            "mount": "/data", "bearer": BYTES_TOKEN})[0] == 200
        part = "/library/parts/40/1789187071/file.mkv"
        assert ask(server, SHARED, part, headers={"Range": "bytes=0-7"}) == (206, BODY[:8])
        restrictions = {"filterMovies": "contentRating=G", "allowSync": False}
        assert ask(server, OWNER, route, "PUT", restrictions)[0] == 200
        status, body = ask(server, SHARED, "/library/sections/1/all")
        assert status == 200 and [row["ratingKey"] for row in body["MediaContainer"]["Metadata"]] == ["1"], body
        assert ask(server, SHARED, part, headers={"Range": "bytes=0-7"})[0] in (403, 404)
        assert ask(server, SHARED, "/downloadQueue", "POST")[0] == 403
        assert ask(server, SHARED, route, "PUT", {})[0] == 403
        assert ask(server, OWNER, route, "PUT", {"filterMovies": "unknown=anything"})[0] == 400
        assert ask(server, OWNER, route)[1] == restrictions
        server.stop()
        server = Server(work, plex.server_address[1])
        assert ask(server, OWNER, route)[1] == restrictions
        assert ask(server, SHARED, part, headers={"Range": "bytes=0-7"})[0] in (403, 404)
        assert ask(server, OWNER, route, "DELETE")[0] == 204
        assert ask(server, SHARED, part, headers={"Range": "bytes=0-7"}) == (206, BODY[:8])
        assert ask(server, SHARED, "/downloadQueue", "POST")[0] == 200
        # A permissive local filter still cannot expose the cloud-hidden film or TV library.
        assert ask(server, OWNER, route, "PUT", {"filterMovies": "contentRating=G,PG,Not Rated", "allowSync": True})[0] == 200
        assert ask(server, SHARED, "/library/metadata/3")[0] in (403, 404)
        assert ask(server, SHARED, "/library/sections/2/all")[0] in (403, 404)
        print("PASS local sharing restrictions: real byte denial, cloud intersection, owner-only control and restart")
    finally:
        if server is not None:
            server.stop()
        for service in (plex, upstream):
            service.shutdown()
            service.server_close()
        PlexTv.tokens = previous
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
