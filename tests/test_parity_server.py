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
import zlib

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
            self.header_list = [(k.lower(), v) for k, v in response.getheaders()]
            heads = dict(self.header_list)
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


def raw_exchange(port, data):
    """Send raw bytes and return the response head (status line and headers)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(data)
        chunks = b""
        while b"\r\n\r\n" not in chunks:
            piece = sock.recv(4096)
            if not piece:
                break
            chunks += piece
    finally:
        sock.close()
    return chunks.split(b"\r\n\r\n", 1)[0].decode("latin-1")


def run_dispatcher(s):
    # -- the legacy plug-in dispatcher answers a single segment on EVERY verb --------
    plugins = {"MediaContainer": {"size": 0, "content": "plugins"}}
    for method in ("POST", "PUT", "DELETE", "OPTIONS"):
        for path in ("/foo", "/settings", "/login", "/sync", "/updater", "/zzznotapath",
                     "/identity", "/hubs", "/player", "/web", "/myplex"):
            status, _, doc = s.json(method, path)
            check("%s %s is the dispatcher's empty listing" % (method, path),
                  status == 200 and doc == plugins, (status, doc))
    status, _, doc = s.json("PUT", "/server")
    check("PUT /server is the dispatcher's one-row listing",
          doc == {"MediaContainer": {"size": 1, "content": "plugins", "Directory": [
              {"key": "servers", "title": "servers"}]}}, doc)
    status, _, doc = s.json("DELETE", "/l")
    names = [row.get("key") for row in container(doc).get("Directory") or []]
    check("DELETE /l is the dispatcher's seven rows in the measured order",
          names == ["epg", "augmentation", "dvrs", "shared", "log", "sessions", "library"],
          names)
    status, _, doc = s.json("GET", "/lib")
    library = (container(doc).get("Directory") or [{}])[-1]
    check("the dispatcher's library row carries share as the NUMBER 0",
          library.get("key") == "library" and library.get("share") == 0, library)
    status, _, doc = s.json("POST", "/security")
    check("POST /security is the bare {size: 0}", doc == {"MediaContainer": {"size": 0}}, doc)
    for method in ("PUT", "DELETE", "OPTIONS"):
        status, _, _ = s.json(method, "/search")
        check("%s /search is its own 400" % method, status == 400, status)
    status, _, doc = s.json("PUT", "/playlists")
    check("PUT /playlists falls to the dispatcher", doc == plugins, doc)
    status, _, doc = s.json("DELETE", "/playQueues")
    check("DELETE /playQueues falls to the dispatcher", doc == plugins, doc)

    # -- /log validates on every verb ------------------------------------------------
    for method in ("POST", "PUT", "DELETE", "OPTIONS", "HEAD", "GET"):
        status, _, _ = s.raw(method, "/log")
        check("%s /log without level and message is a 400" % method, status == 400, status)
        status, heads, body = s.raw(method, "/log?level=2&message=parity")
        check("%s /log with both is an empty 200" % method,
              status == 200 and body == b"", (status, body[:80]))
    status, _, _ = s.raw("POST", "/log?level=abc&message=parity")
    check("a non-numeric level is a 400", status == 400, status)

    # -- the download queue ------------------------------------------------------------
    status, _, doc = s.json("POST", "/downloadQueue")
    queue = {"id": 1, "owner": 1, "clientIdentifier": "parity-server", "itemCount": 0,
             "status": "done"}
    check("POST /downloadQueue answers the caller's queue with owner and itemCount",
          doc == {"MediaContainer": {"size": 1, "DownloadQueue": [queue]}}, doc)
    status, _, doc = s.json("POST", "/downloadQueue",
                            {"X-Plex-Client-Identifier": "parity-other"})
    check("a second client's queue is id 2",
          ((container(doc).get("DownloadQueue") or [{}])[0]).get("id") == 2, doc)
    status, _, doc = s.json("GET", "/downloadQueue/2")
    check("/downloadQueue/{id} reads any client's queue by id",
          status == 200 and container(doc).get("size") == 1
          and container(doc)["DownloadQueue"][0].get("clientIdentifier") == "parity-other",
          (status, doc))
    status, _, doc = s.json("GET", "/downloadQueue/1/items")
    check("/downloadQueue/{id}/items is {size: 0}",
          doc == {"MediaContainer": {"size": 0}}, doc)
    for method in ("PUT", "DELETE"):
        status, _, doc = s.json(method, "/downloadQueue")
        check("%s /downloadQueue is the dispatcher's listing" % method, doc == plugins, doc)

    # -- an unknown method honours Connection: close ----------------------------------
    for method in ("PATCH", "FROB"):
        head = raw_exchange(s.port, ("%s /identity HTTP/1.1\r\nHost: x\r\n"
                                     "Connection: close\r\n\r\n" % method).encode())
        check("%s with Connection: close is a 500 answered Connection: close" % method,
              head.startswith("HTTP/1.1 500") and "\r\nConnection: close" in head
              and "Keep-Alive: timeout" not in head, head)

    # -- the doubled trailing slash answers the parent ---------------------------------
    for doubled, parent in (("/media/providers//", "/media/providers"),
                            ("/media/providers/metadata//", "/media/providers/metadata"),
                            ("/servers//", "/servers"), ("/channels//", "/channels"),
                            ("/accounts//", "/accounts"), ("/clients//", "/clients"),
                            ("/resources//", "/resources"), ("/security//", "/security"),
                            ("/:/prefs//", "/:/prefs"), ("/library/metadata/1007//",
                                                         "/library/metadata/1007")):
        _, _, left = s.json("GET", doubled)
        _, _, right = s.json("GET", parent)
        mask = lambda doc: json.loads(re.sub(r'"(updatedAt|checkedAt)":\d+', '"\\1":0',
                                             json.dumps(doc)))
        check("%s answers %s's own document" % (doubled, parent),
              left is not None and mask(left) == mask(right),
              (str(left)[:160], str(right)[:160]))
    status, _, _ = s.json("GET", "/library/metadata/1007/children//")
    check("a doubled slash below an item leaf is a 404", status == 404, status)


def run_browse(s):
    # -- /services/browse walks the /plexsim tree with keys and titles ----------------
    status, _, doc = s.json("GET", "/services/browse")
    check("/services/browse lists / and /plexsim with keys and titles",
          doc == {"MediaContainer": {"size": 2, "Path": [
              {"key": "/services/browse/Lw==", "title": "/", "path": "/"},
              {"key": "/services/browse/L3BsZXhzaW0=", "title": "plexsim",
               "path": "/plexsim"}]}}, doc)
    status, _, doc = s.json("GET", "/services/browse/L3BsZXhzaW0=?includeFiles=1")
    check("/plexsim lists one directory per section and no files",
          doc == {"MediaContainer": {"size": 2, "Path": [
              {"key": "/services/browse/L3BsZXhzaW0vMQ==", "title": "1", "path": "/plexsim/1"},
              {"key": "/services/browse/L3BsZXhzaW0vMg==", "title": "2",
               "path": "/plexsim/2"}]}}, doc)
    status, _, doc = s.json("GET", "/services/browse/L3BsZXhzaW0vMQ==?includeFiles=0")
    check("includeFiles=0 on a leaf directory is {size: 0}",
          doc == {"MediaContainer": {"size": 0}}, doc)
    status, _, doc = s.json("GET", "/services/browse/L3BsZXhzaW0vMQ==?includeFiles=1")
    files = container(doc).get("File") or []
    check("includeFiles=1 lists each distinct file of that directory once, keyed",
          container(doc).get("size") == len(files) == 5
          and len({row.get("path") for row in files}) == 5
          and all(row.get("path", "").startswith("/plexsim/1/") and row.get("key")
                  and row.get("title") == row["path"].rsplit("/", 1)[-1] for row in files)
          and "Path" not in container(doc), doc)
    titles = [row.get("title", "").lower() for row in files]
    check("and sorts them by lower-cased name", titles == sorted(titles), titles)
    status, _, _ = s.json("GET", "/services/browse/L3BsZXhzaW0?includeFiles=1")
    check("unpadded base64 is a 400", status == 400, status)


def run_system(s):
    # -- /clients/profiles: the reference's order, titles only, sized -----------------
    status, _, doc = s.json("GET", "/clients/profiles")
    titles = [row.get("title") for row in container(doc).get("Profile") or []]
    check("/clients/profiles is sized 78", container(doc).get("size") == 78 == len(titles),
          (container(doc).get("size"), len(titles)))
    check("/clients/profiles lists Opera TV App before Opera and Roku-7.x before Roku",
          "Opera" in titles and titles.index("Opera TV App") < titles.index("Opera")
          and titles.index("Roku-7.x") < titles.index("Roku")
          and titles.index("PlayStation 3 App") < titles.index("PlayStation 3"),
          titles[10:32])
    status, _, doc = s.json("GET", "/clients/profiles?type=42")
    check("/clients/profiles?type=42 is the seven local profiles, titles only",
          doc == {"MediaContainer": {"size": 7, "Profile": [{"title": name} for name in (
              "Android", "iOS", "Universal Mobile", "Universal TV", "Windows Phone",
              "Windows", "Xbox One")]}}, doc)

    # -- /media/providers/metadata: served types in numeric order ---------------------
    status, _, doc = s.json("GET", "/media/providers/metadata")
    none = [row for row in container(doc).get("MetadataAgentProvider") or []
            if row.get("identifier") == "tv.plex.agents.none"]
    types = [row.get("type") for row in (none[0].get("MetadataType") if none else [])]
    check("Personal Media's MetadataType rows are in numeric order",
          types == [1, 2, 3, 4, 8, 13], types)

    # -- paging headers -----------------------------------------------------------------
    s.raw("GET", "/system/search?X-Plex-Container-Start=0&X-Plex-Container-Size=1")
    names = [name for name, _ in s.header_list]
    check("/system/search sends no paging headers even when asked to page",
          "x-plex-container-start" not in names
          and "x-plex-container-total-size" not in names, names)
    s.raw("GET", "/system?X-Plex-Container-Start=0&X-Plex-Container-Size=1")
    names = [name for name, _ in s.header_list]
    check("a paged /system sends ONE X-Plex-Container-Start and ONE Total-Size",
          names.count("x-plex-container-start") == 1
          and names.count("x-plex-container-total-size") == 1, names)


def run_resources(s):
    # -- bundled resources and generated images are the oracle's bytes ---------------
    # server.py's `_jpeg`/`_png` placeholders at the measured geometry (zlib level 6 for
    # the PNG, so the deflate stream is zlib's own).
    for path, size, kind in (("/:/resources/movie-fanart.jpg", 33011, "image/jpeg"),
                             ("/:/resources/show-fanart.jpg", 33011, "image/jpeg"),
                             ("/:/resources/artist-fanart.jpg", 32808, "image/jpeg"),
                             ("/:/resources/actor-icon.png", 1561, "image/png"),
                             ("/system/bundle/media/flags/videoResolution/1080", 145,
                              "image/png"),
                             ("/system/bundle/media/flags/audioCodec/ac3", 188, "image/png"),
                             ("/system/agents/attribution/image"
                              "?identifier=com.plexapp.agents.themoviedb", 140, "image/png")):
        status, heads, body = s.raw("GET", path)
        check("%s is the oracle's %d-byte %s" % (path.split("?")[0], size, kind),
              status == 200 and len(body) == size
              and heads.get("content-type", "").startswith(kind),
              (status, len(body), heads.get("content-type")))
    status, _, body = s.raw("GET", "/:/resources/movie-fanart.jpg")
    check("the fanart placeholder is a baseline JPEG with a real scan",
          body[:2] == b"\xff\xd8" and b"\xff\xda" in body and body[-2:] == b"\xff\xd9",
          body[:8])
    for path in ("/:/resources/", "/:/resources/dvr", "/:/resources/dvr/",
                 "/:/resources/category-art"):
        status, heads, body = s.raw("GET", path)
        check("%s is an empty rangeable octet-stream" % path,
              status == 200 and body == b"" and heads.get("accept-ranges") == "bytes"
              and heads.get("content-type") == "application/octet-stream",
              (status, heads))


def run_writes(s):
    # -- a created section's uuid is the oracle's crc32 of "{title}-{type}" -----------
    status, _, _ = s.raw("POST", "/library/sections?name=Parity%20Create&type=movie"
                         "&agent=tv.plex.agents.movie&scanner=Plex%20Movie&language=en-US"
                         "&location=%2Fdata%2Fmovies")
    wanted = "%032x" % (zlib.crc32(b"Parity Create-movie") & 0xFFFFFFFF)
    _, _, doc = s.json("GET", "/media/providers?includeSummary=1")
    uuids = [row.get("uuid") for provider in container(doc).get("MediaProvider") or []
             for feature in provider.get("Feature") or []
             for row in feature.get("Directory") or [] if row.get("title") == "Parity Create"]
    check("a created section's uuid is %032x of crc32(title-type)",
          status in (200, 201) and uuids and set(uuids) == {wanted}, (status, uuids, wanted))


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
        server = Server(port)
        run(server)
        run_dispatcher(server)
        run_browse(server)
        run_system(server)
        run_resources(server)
        run_writes(server)
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
