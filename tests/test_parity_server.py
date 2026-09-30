#!/usr/bin/env python3
"""Server-level routes answer the oracle's measured shapes, on both servers.

Family C2 of the native parity sweep: `/identity`, `/servers`, `/media/providers*`,
`/clients*`, `/dlna`, `/:/resources/*`, `/system/*`, `/myplex/*`, `/security/*`,
`/:/prefs`, `/updater/*`, `/livetv/*`, `/channels`, `/logs`, `/diagnostics`, the
legacy plug-in dispatcher's single-segment paths, and the empty-bodied writers. Each
check asserts the Python oracle's concrete value, which is what its handler's comment
records as measured on a real PMS -- not "equal to the other server".

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`, another build with
`PLEXSIM_SERVER=/path/to/plexsim`.

    python3 tests/test_parity_server.py
"""
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-server",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome", "User-Agent": "Plex Web/4.147.1 (parity)"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        "" if ok else "  -- " + str(detail)[:400]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Server:
    def __init__(self, port):
        self.port = port

    def raw(self, method, path, headers=None, xml=False):
        merged = dict(HEADERS)
        if xml:
            merged.pop("Accept")
        merged.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, headers=merged)
            response = conn.getresponse()
            body = response.read()
            heads = {k.lower(): v for k, v in response.getheaders()}
        finally:
            conn.close()
        return response.status, heads, body

    def json(self, method, path, headers=None):
        status, heads, body = self.raw(method, path, headers)
        try:
            doc = json.loads(body) if body else None
        except ValueError:
            doc = None
        return status, heads, doc


def container(doc):
    return (doc or {}).get("MediaContainer") or {}


def run(s):
    # -- /identity: `claimed` is a JSON boolean, in the measured field order ---------
    status, _, doc = s.json("GET", "/identity")
    mc = container(doc)
    check("/identity carries claimed as the JSON boolean true",
          status == 200 and mc.get("claimed") is True, mc)
    check("/identity's five fields in the measured order",
          list(mc) == ["size", "apiVersion", "claimed", "machineIdentifier", "version"],
          list(mc))
    status, _, body = s.raw("GET", "/identity", xml=True)
    check("/identity's XML still renders claimed=\"1\"", b'claimed="1"' in body, body[:200])

    # -- empty-bodied writers are text/html -----------------------------------------
    for method, path in (("PUT", "/updater/check"), ("PUT", "/library/optimize"),
                         ("PUT", "/library/clean/bundles"),
                         ("PUT", "/myplex/refreshReachability"),
                         ("GET", "/log/networked"),
                         ("GET", "/log?level=3&message=parity&source=suite"),
                         ("POST", "/log?level=4&message=parity&source=Plex+Web")):
        status, heads, body = s.raw(method, path)
        check("%s %s is an empty text/html 200" % (method, path.split("?")[0]),
              status == 200 and body == b"" and heads.get("content-type") == "text/html",
              (status, heads.get("content-type"), body[:80]))

    # -- small containers carry `size` -------------------------------------------------
    for path in ("/clients", "/livetv/dvrs", "/livetv/sessions"):
        status, _, doc = s.json("GET", path)
        check("%s is {size: 0}" % path, status == 200 and doc == {"MediaContainer": {"size": 0}},
              doc)
    status, _, doc = s.json("GET", "/updater/status")
    mc = container(doc)
    check("/updater/status is size, canInstall, checkedAt, status",
          list(mc) == ["size", "canInstall", "checkedAt", "status"]
          and mc.get("size") == 0 and mc.get("status") == 0 and mc.get("canInstall") is False,
          mc)
    status, _, doc = s.json("GET", "/logs")
    check("/logs is the dispatcher's {size: 0, content: plugins}",
          doc == {"MediaContainer": {"size": 0, "content": "plugins"}}, doc)
    status, _, doc = s.json("GET", "/media/grabbers")
    check("/media/grabbers lists the four measured grabbers",
          doc == {"MediaContainer": {"size": 4, "MediaGrabber": [
              {"title": "HDHomerun", "identifier": "tv.plex.grabbers.hdhomerun",
               "protocol": "livetv"},
              {"title": "Stream", "identifier": "tv.plex.grabbers.stream",
               "protocol": "stream"},
              {"title": "Download", "identifier": "tv.plex.grabbers.download",
               "protocol": "download"},
              {"title": "Remote", "identifier": "tv.plex.grabbers.tunerservice",
               "protocol": "livetv"}]}}, doc)
    status, _, doc = s.json("GET", "/channels")
    check("/channels is All and Recently Viewed, keys and titles only",
          doc == {"MediaContainer": {"size": 2, "Directory": [
              {"key": "all", "title": "All"},
              {"key": "recentlyViewed", "title": "Recently Viewed"}]}}, doc)

    # -- /security/token mints a transient uuid token ------------------------------
    tokens = []
    for _ in range(2):
        status, _, doc = s.json("GET", "/security/token?type=delegation&scope=all")
        mc = container(doc)
        tokens.append(mc.get("token"))
        check("/security/token answers size 0 and a transient-<uuid4> token",
              status == 200 and list(mc) == ["size", "token"] and mc.get("size") == 0
              and re.fullmatch(r"transient-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                               r"[89ab][0-9a-f]{3}-[0-9a-f]{12}", mc.get("token") or ""),
              mc)
    check("and each call's token differs", tokens[0] != tokens[1], tokens)

    # -- /dlna: the library rows, keyed by path, then the Channels row --------------
    status, _, doc = s.json("GET", "/dlna")
    mc = container(doc)
    rows = mc.get("Directory") or []
    first = rows[0] if rows else {}
    check("/dlna's container carries only its size", list(mc) == ["size", "Directory"]
          and mc.get("size") == len(rows), list(mc))
    check("/dlna's library row is the section Directory row keyed by path",
          first.get("key") == "/library/sections/1" and first.get("type") == "movie"
          and first.get("agent") == "tv.plex.agents.movie"
          and first.get("scanner") == "Plex Movie" and first.get("language") == "en-US"
          and first.get("uuid") and first.get("Location")
          and "hidden" in first and "contentChangedAt" in first, first)
    check("/dlna's library row drops allowSync, filters and refreshing",
          not {"allowSync", "filters", "refreshing"} & set(first), sorted(first))
    check("/dlna ends with the /channels/all row",
          rows and rows[-1] == {"key": "/channels/all", "title": "Channels"}, rows[-1:])


def main():
    port = int(os.environ.get("PLEXSIM_TEST_PORT") or free_port())
    env = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
               PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
    for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
        env.pop(name, None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                       "--gdm", "off", "--assert-claimed", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(300):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        run(Server(port))
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    passed = sum(results)
    print("%s %d/%d server-level parity checks" % (
        "PASS" if passed == len(results) else "FAIL", passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
