#!/usr/bin/env python3
"""A token sent in the QUERY reaches the requests a route makes of its own server.

Cycle 83's Android leg: Plex for Android sends `X-Plex-Token` as a query parameter on every
image it asks for (`tests/fixtures/android_query_token_transcode.json`, captured through the
pinning proxy). With the token gate on, the native server's photo transcoder read the image
it resizes through its own router carrying only the outer request's HEADERS, so the inner read
was refused and every poster tile answered 404 -- the app drew titles where the posters go.
The oracle answered 200. `/service(s)/ultrablur/colors` goes through the transcoder, and the
Categories shelf reads its genre axis the same way.

Starts its own servers (native by default, `PLEXSIM_SERVER=python` the oracle) on
`tests/catalog_ui.json` with `--token`.
"""
import gzip
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server_command import server_command  # noqa: E402

ROOT = os.path.dirname(HERE)
TOKEN = "inner-token-check"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, detail))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def send(port, path, headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", "127.0.0.1:%d" % port)
    for k, v in headers:
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    if (r.getheader("Content-Encoding") or "") == "gzip" and body:
        body = gzip.decompress(body)
    c.close()
    return r.status, r.getheader("Content-Type") or "", body


def main():
    fixture = json.load(open(os.path.join(HERE, "fixtures", "android_query_token_transcode.json")))
    port = free_port()
    env = dict(os.environ, RD_TOKEN="dummy", PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
    env.pop("TMDB_KEY", None)
    proc = subprocess.Popen(
        server_command("--port", str(port), "--catalog", "tests/catalog_ui.json",
                       "--assert-claimed", "--gdm", "off", "--token", TOKEN, root=ROOT),
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                break
            except OSError:
                time.sleep(0.2)
        header = [("Accept", "application/json"), ("X-Plex-Token", TOKEN)]
        _, _, body = send(port, "/library/metadata/1008", header)
        thumb = json.loads(body)["MediaContainer"]["Metadata"][0]["thumb"]
        # The app's request, with this fixture's own thumb and the server's token.
        query = [(k, thumb if k == "url" else TOKEN if k == "X-Plex-Token" else v)
                 for k, v in fixture["query"]]
        path = fixture["path"] + "?" + urllib.parse.urlencode(query)
        status, ctype, body = send(port, path, [tuple(h) for h in fixture["req_headers"]])
        check("the Android poster request with its token in the query is an image",
              status == 200 and ctype.startswith("image/") and body, "%s %s %d bytes"
              % (status, ctype, len(body)))
        status_h, _, _ = send(port, fixture["path"] + "?" + urllib.parse.urlencode(
            [(k, v) for k, v in query if k != "X-Plex-Token"]), [("X-Plex-Token", TOKEN)])
        check("the same request with the token as a header agrees", status_h == status,
              "%s vs %s" % (status_h, status))
        status_n, _, _ = send(port, fixture["path"] + "?" + urllib.parse.urlencode(
            [(k, v) for k, v in query if k != "X-Plex-Token"]), [])
        check("the same request with no token is refused", status_n == 401, str(status_n))
        for spelling in ("/service/ultrablur/colors", "/services/ultrablur/colors"):
            status, ctype, body = send(port, spelling + "?" + urllib.parse.urlencode(
                [("url", thumb), ("X-Plex-Token", TOKEN)]), [("Accept", "application/json")])
            colors = (json.loads(body).get("MediaContainer") or {}).get("UltraBlurColors") \
                if status == 200 else None
            check("%s with a query token answers the four colours" % spelling,
                  status == 200 and colors, "%s %s" % (status, body[:80]))
        _, _, by_header = send(port, "/library/sections/1/categories", header)
        status, _, by_query = send(port, "/library/sections/1/categories?X-Plex-Token=" + TOKEN,
                                   [("Accept", "application/json")])
        rows_h = (json.loads(by_header).get("MediaContainer") or {}).get("Directory") or []
        rows_q = (json.loads(by_query).get("MediaContainer") or {}).get("Directory") or [] \
            if status == 200 else None
        check("the Categories shelf lists the same genres with a query token",
              rows_h and rows_q == rows_h, "header %d rows, query %s"
              % (len(rows_h), None if rows_q is None else len(rows_q)))
    finally:
        proc.terminate()
        proc.wait(10)
    print("\nPASS %d/%d inner-token checks" % (sum(results), len(results)) if all(results)
          else "\nFAIL %d/%d inner-token checks" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
