#!/usr/bin/env python3
"""Sources are byte servers, registered over rex's API, and rex assumes nothing of one beyond HTTP.

`docs/feed-api.md`: a source is any HTTP server that answers a GET for a file, registered
with `PUT /rex/sources/{name}` and kept in the state file. This suite registers two at once,
neither of them zurg, each pushing under its own name into the same libraries (an item's
`file` places it; its source only says where the bytes are):

- `disk`: Python's own `http.server.SimpleHTTPRequestHandler` over a directory -- the
  real implementation, not a stub of it. It ignores `Range` and answers every GET 200
  with the whole file, as plenty of plain file servers do.
- `front`: basic auth, and every file a 302 to `cdn` on another host, the way a
  storage front hands out presigned links. `cdn` refuses any request carrying an
  `Authorization` header, as S3 does for a presigned URL ("Only one auth mechanism
  allowed"), and honours `Range`.

Then what registration itself promises: a bad one is refused, credentials are never answered
or logged, a source survives a restart before it registers again, a new registration is
what the next part read streams from, and rows made before their source registered -- the
old WebDAV scanner's, and a catalog's feed rows -- play once it has. And deleting a source:
two requests, the first of which removes nothing, and nothing asked of the source's files.

    python3 tests/test_sources.py
"""
import base64
import functools
import http.client
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from server_command import server_command
from startup_wait import poll_until
from test_feed import BODY, BYTES_TOKEN, LEGACY, MOUNT, ROOT, Bytes, check, results, item, catalog, serve
from test_ports import free_port
import test_feed_versions as versions

FRONT_USER, FRONT_PASSWORD = "front-user", "front-password"


def cdn_handler(seen):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, body=b"", extra=()):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Range")))
            if self.headers.get("Authorization"):
                return self.reply(400, b"Only one auth mechanism allowed")
            spec = self.headers.get("Range")
            if spec and spec.startswith("bytes="):
                first, _, last = spec[6:].partition("-")
                first, last = int(first), min(int(last) if last else len(BODY) - 1, len(BODY) - 1)
                return self.reply(206, BODY[first:last + 1],
                                  [("Content-Range", "bytes %d-%d/%d" % (first, last, len(BODY)))])
            self.reply(200, BODY)

        do_HEAD = do_GET
    return Handler


def front_handler(cdn_port, seen):
    expected = "Basic " + base64.b64encode(("%s:%s" % (FRONT_USER, FRONT_PASSWORD)).encode()).decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            status = 302 if self.headers.get("Authorization") == expected else 401
            self.send_response(status)
            if status == 302:
                if self.path.startswith("/files/redirect/"):
                    if "?" in self.path:
                        # The third hop is another origin: `localhost` on the CDN port.
                        # The fragment is client-side only.
                        location = "//localhost:%d/relative.mkv#fragment" % cdn_port
                    else:
                        # Query-only references keep the current path before the CDN hop.
                        location = "?signed=1"
                elif self.path.startswith("/files/"):
                    # Same-origin redirects may vary scheme and DNS host case. The client
                    # must still retain this source's credential for the next hop.
                    location = "HTTP://LOCALHOST:%d/redirect/relative.mkv" % self.server.server_address[1]
                elif self.path.startswith("/redirect/relative.mkv"):
                    if "?" in self.path:
                        location = "//localhost:%d/relative.mkv#fragment" % cdn_port
                    else:
                        location = "?signed=1"
                else:
                    location = "../../redirect/relative.mkv"
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_GET
    return Handler


def start(work, sources, env_extra=None, *args):
    """A server on `work`'s catalog and state, each of `sources` registered as zurg does it."""
    port = free_port()
    env = dict(os.environ, TMDB_KEY="fake", PLEX_TOKEN="fake", REX_PLEXTV_URL="http://127.0.0.1:9")
    for name in ("REX_STATE", "REX_TOKEN"):
        env.pop(name, None)
    env.update(env_extra or {})
    log = open(os.path.join(work, "server.log"), "ab")
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(work, "catalog.json"), "--state",
                       os.path.join(work, "rex.db"), "--assert-claimed", "--gdm", "off", *args, root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    def ready():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2)
            return True
        except Exception:
            return False
    poll_until(ready, 40)
    for name, source in sources.items():
        status, raw = register(port, name, source)
        check("source %s registers" % name, status == 200, (status, raw))
    return port, process, log


def stop(process, log):
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
    log.close()


def register(port, name, body, headers=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return call(port, "/rex/sources/%s" % name, "PUT", data,
                dict({"Content-Type": "application/json"}, **(headers or {})))


def call(port, path, method="GET", body=None, headers=None):
    data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method,
                                     headers=dict({"Accept": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            try:
                return response.status, response.read()
            except http.client.IncompleteRead as short:
                return response.status, short.partial
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def loads(raw):
    """A JSON answer, or {} for one that is not (a server without the API answers HTML)."""
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def registered(port):
    """[(name, url)] of what GET /rex/sources lists."""
    return [(row.get("name"), row.get("url")) for row in loads(call(port, "/rex/sources")[1]).get("sources", [])]


def push(port, source, entry):
    status, raw = call(port, "/rex/feed/%s/items" % source, "POST", {"items": [entry]},
                       {"Content-Type": "application/json"})
    status, raw = call(port, "/rex/feed/%s/items" % source)
    key = {row["id"]: row["ratingKey"] for row in loads(raw).get("items", [])}.get(entry["id"])
    status, raw = call(port, "/library/metadata/%s" % key)
    row = (json.loads(raw)["MediaContainer"].get("Metadata") or [{}])[0] if status == 200 else {}
    return ((row.get("Media") or [{}])[0].get("Part") or [{}])[0].get("key")


def gated():
    """The sources API and the feed are rex's own, answered before the Plex gate: a server
    with `--token` (as a claimed one) takes a registration and a push with no token from its
    own machine, while its library still wants the token. zurg reads a 401 on its
    registration as a rex without the sources API and carries on unregistered."""
    work = tempfile.mkdtemp(prefix="rex-sources-gated-")
    catalog(work)
    port, process, log = start(work, {"plain": {"url": "http://127.0.0.1:9"}}, None, "--token", "gate-secret")
    try:
        status, raw = call(port, "/rex/feed/plain/items", "POST", {"items": [item("movie", ids=False)]},
                           {"Content-Type": "application/json"})
        check("a gated server takes a push with no token", status == 200 and loads(raw).get("created") == 1,
              (status, raw))
        check("while its library still wants one", call(port, "/library/sections")[0] == 401)
    finally:
        stop(process, log)
        shutil.rmtree(work, ignore_errors=True)


def part_key(port, key):
    status, raw = call(port, "/library/metadata/%s" % key)
    row = (json.loads(raw)["MediaContainer"].get("Metadata") or [{}])[0] if status == 200 else {}
    return ((row.get("Media") or [{}])[0].get("Part") or [{}])[0].get("key") or "/none"


def unregistered_rows():
    """Rows keep the source they came from. A catalog's rows -- one the old WebDAV scanner made
    (`fixtures/feed/legacy_scan_rows.json`: no source, its Part's file under zurg's mount) and
    one the feed made for `zurg` -- have no bytes while no source is registered, and read from
    `zurg` the moment it registers, no restart: the scanner's at its file under the source's
    `mount`, the feed's at its own `_path`."""
    work = tempfile.mkdtemp(prefix="rex-sources-rows-")
    stub = Bytes()
    rows = json.loads(json.dumps(LEGACY["items"]))
    movie = item("movie")
    part_file = lambda row: ((row.get("Media") or [{}])[0].get("Part") or [{}])[0].get("file")
    scanned = next(row for row in rows if part_file(row) == movie["file"])
    episode = next(row for row in rows if row.get("type") == "episode")
    fed = "shows/Elsewhere/" + episode["Media"][0]["Part"][0]["file"].rsplit("/", 1)[1]
    episode.update(_source="zurg", _sid=fed, _path=fed)
    for path in (movie["path"], fed):
        stub.files.add("/dav/" + path)
    stub_port = serve(stub.handler())
    catalog(work, rows)
    port, process, log = start(work, {})
    try:
        keys = (part_key(port, scanned["ratingKey"]), part_key(port, episode["ratingKey"]))
        before = [call(port, key, headers={"Range": "bytes=0-9"})[0] for key in keys]
        check("with no source registered, a row's part has no bytes (502)", before == [502, 502], before)
        register(port, "zurg", {"url": "http://127.0.0.1:%d/dav" % stub_port, "bearer": BYTES_TOKEN, "mount": MOUNT})
        after = [call(port, key, headers={"Range": "bytes=0-9"}) for key in keys]
        hits = [seen[1] for seen in stub.seen if seen[0] == "GET"]
        check("once zurg registers, the scanner's row reads its file under zurg's mount, the feed's its path",
              [status for status, _ in after] == [206, 206] and all(raw == BODY[:10] for _, raw in after)
              and hits == ["/dav/" + movie["path"], "/dav/" + fed], (after, hits))
    finally:
        stop(process, log)
        shutil.rmtree(work, ignore_errors=True)


class Untouched(Bytes):
    """The byte stub, recording any DELETE it is sent: deleting a source asks nothing of it."""

    def handler(source):
        base = Bytes.handler(source)

        class Handler(base):
            def do_DELETE(self):
                source.seen.append(("DELETE", self.path, None, None))
                self.reply(204)
        return Handler


def delete_source():
    """`DELETE /rex/sources/{name}` deletes nothing: it answers a confirmation path, valid for
    300 s, that removes the source and every item it pushed -- its held items and its releases,
    a row another source also holds keeping that one's -- from rex alone. The two releases of
    127 Hours (`fixtures/feed/duplicate_releases.json`) come from two sources here, so they are
    one row with a Media each."""
    work = tempfile.mkdtemp(prefix="rex-sources-delete-")
    stub = Untouched()
    one, two = versions.item(0), versions.item(1)
    bellboy, held = item("movie", ids=False), item("new", probe=False)
    for pushed in (one, two, bellboy):
        stub.files.add("/dav/" + pushed["path"])
    stub_port = serve(stub.handler())
    versions.catalog(work)
    source = {"url": "http://127.0.0.1:%d/dav" % stub_port, "bearer": BYTES_TOKEN, "mount": MOUNT}
    port, process, log = start(work, {"a": source, "b": source})
    feed = lambda name, method, leaf, body=None: call(port, "/rex/feed/%s/%s" % (name, leaf), method, body,
                                                      {"Content-Type": "application/json"})
    listing = lambda name, leaf="items": loads(feed(name, "GET", leaf)[1])
    try:
        feed("a", "POST", "items", {"items": [one]})
        feed("b", "POST", "items", {"items": [two, bellboy, held]})
        shared_key = {row["id"]: row["ratingKey"] for row in listing("a").get("items", [])}.get(one["id"])
        b_keys = {row["id"]: row["ratingKey"] for row in listing("b").get("items", [])}
        check("two sources' releases of one title are one row; b also has a film of its own and a held item",
              shared_key and b_keys.get(two["id"]) == shared_key and b_keys.get(bellboy["id"]) not in (None, shared_key)
              and listing("b", "held").get("size") == 1, (shared_key, b_keys, listing("b", "held")))

        status, raw = call(port, "/rex/sources/b", "DELETE")
        asked = loads(raw) if status == 200 else {}
        code = asked.get("confirm", "").rsplit("/", 1)[-1]
        check("the first DELETE answers a confirmation, the source's item count and the window",
              status == 200 and asked.get("confirm") == "/rex/sources/b/confirm/" + code
              and len(code) >= 32 and all(c in "0123456789abcdef" for c in code) and asked.get("items") == 3 and asked.get("expires_in") == 300,
              (status, raw))
        check("and removes nothing", call(port, "/rex/sources/b")[0] == 200 and len(listing("b").get("items", [])) == 2
              and listing("b", "held").get("size") == 1
              and call(port, "/library/metadata/%s" % b_keys.get(bellboy["id"]))[0] == 200)
        status, raw = call(port, "/rex/sources/a", "DELETE")
        a_code = loads(raw).get("confirm", "").rsplit("/", 1)[-1] if status == 200 else ""
        refusals = [call(port, "/rex/sources/b/confirm/" + wrong, "DELETE")[0]
                    for wrong in ("0" * len(code), a_code, code[:-1], code + "0", "")]
        check("a wrong code, another source's code or a mangled one is a 404", refusals == [404] * 5, refusals)
        check("and removes nothing", call(port, "/rex/sources/b")[0] == 200 and len(listing("b").get("items", [])) == 2)
        check("an unregistered source has nothing to delete", call(port, "/rex/sources/nobody", "DELETE")[0] == 404)

        status, raw = call(port, "/rex/sources/b/confirm/" + code, "DELETE")
        check("the confirmation removes the source and its three items",
              status == 200 and loads(raw) == {"deleted": True, "removed": 3}, (status, raw))
        check("its registration is gone, and its feed with it: a push is the 404 that asks it to register",
              call(port, "/rex/sources/b")[0] == 404 and feed("b", "GET", "items")[0] == 404
              and feed("b", "POST", "items", {"items": [bellboy]})[0] == 404)
        check("its film of its own is gone from the library",
              bellboy["id"] in b_keys and call(port, "/library/metadata/%s" % b_keys[bellboy["id"]])[0] == 404)
        status, raw = call(port, "/library/metadata/%s" % shared_key)
        files = [part.get("file") for media in (loads(raw)["MediaContainer"]["Metadata"][0].get("Media") or [])
                 for part in media.get("Part") or []] if status == 200 else []
        check("the row a's release shares keeps a's Media and loses b's", files == [one["file"]], (status, files))
        status, raw = call(port, part_key(port, shared_key), headers={"Range": "bytes=0-9"})
        check("and still streams it", status == 206 and raw == BODY[:10], status)
        check("the same code again is a 404", call(port, "/rex/sources/b/confirm/" + code, "DELETE")[0] == 404)
        check("and no file was deleted at the source", not any(seen[0] == "DELETE" for seen in stub.seen), stub.seen)

        stop(process, log)
        port, process, log = start(work, {})
        status, raw = call(port, "/library/metadata/%s" % shared_key)
        files = [part.get("file") for media in (loads(raw)["MediaContainer"]["Metadata"][0].get("Media") or [])
                 for part in media.get("Part") or []] if status == 200 else []
        check("after a restart the delete holds: a alone registered, b's rows and Media gone",
              [name for name, _ in registered(port)] == ["a"] and bellboy["id"] in b_keys
              and call(port, "/library/metadata/%s" % b_keys[bellboy["id"]])[0] == 404 and files == [one["file"]],
              (registered(port), files))
        register(port, "b", source)
        check("registered again, b starts with nothing", listing("b").get("size") == 0
              and listing("b", "held").get("size") == 0, (listing("b"), listing("b", "held")))
        status, raw = feed("b", "POST", "items", {"items": [bellboy]})
        again = {row["id"]: row["ratingKey"] for row in listing("b").get("items", [])}.get(bellboy["id"])
        check("and its push is a new row on a new key", status == 200 and loads(raw).get("created") == 1
              and again not in (None, b_keys.get(bellboy["id"])), (raw, again))
    finally:
        stop(process, log)
        shutil.rmtree(work, ignore_errors=True)


def main():
    work = tempfile.mkdtemp(prefix="rex-sources-")
    disk_root = os.path.join(work, "disk")
    # The size each source pushes is its file's, as zurg reports a release's real size.
    movie, episode = (dict(item(kind, ids=False), size=len(BODY)) for kind in ("movie", "episode"))
    os.makedirs(os.path.join(disk_root, os.path.dirname(movie["path"])))
    open(os.path.join(disk_root, movie["path"]), "wb").write(BODY)
    os.makedirs(os.path.join(disk_root, "movies", "Plain.Release.2021"))
    open(os.path.join(disk_root, "movies", "Plain.Release.2021", "Plain.Release.2021.mkv"), "wb").write(BODY)
    disk_port = serve(functools.partial(http.server.SimpleHTTPRequestHandler, directory=disk_root))
    cdn_seen, front_seen = [], []
    cdn_port = serve(cdn_handler(cdn_seen))
    front_port = serve(front_handler(cdn_port, front_seen))
    catalog(work)
    port, process, log = start(work, {})
    try:
        # -- registration: what is refused, what is answered ------------------------------
        status, raw = call(port, "/rex/sources")
        # rex leaves a field with no value out of every JSON answer, an empty list included.
        check("nothing registered lists no source", status == 200 and loads(raw) == {"size": 0}, (status, raw))
        status, raw = call(port, "/rex/feed/disk/items", "POST", {"items": [movie]}, {"Content-Type": "application/json"})
        check("a push before its source registered is a 404 naming the registration",
              status == 404 and b"PUT /rex/sources/disk first" in raw, (status, raw))
        for label, name, body in (
                ("a name with a dot", "bad.name", {"url": "http://127.0.0.1:%d" % disk_port}),
                ("an ftp url", "disk", {"url": "ftp://127.0.0.1/x"}),
                ("no url", "disk", {"user": "x"}),
                ("a url carrying its credentials", "disk", {"url": "http://u:p@127.0.0.1:%d" % disk_port}),
                ("a body that is not JSON", "disk", b"not json")):
            status, raw = register(port, name, body)
            check("a registration with %s is a 400" % label, status == 400 and b"error" in raw, (status, raw))
        sources = {
            "disk": {"url": "http://127.0.0.1:%d" % disk_port},
            "front": {"url": "http://localhost:%d/files" % front_port, "user": FRONT_USER, "password": FRONT_PASSWORD},
            "plain": {"url": "http://127.0.0.1:%d" % disk_port},
        }
        answers = {name: register(port, name, body) for name, body in sources.items()}
        check("a registration answers the source", all(status == 200 for status, _ in answers.values())
              and loads(answers["front"][1]) == {"name": "front", "url": sources["front"]["url"], "auth": "basic"}
              and loads(answers["disk"][1]) == {"name": "disk", "url": sources["disk"]["url"]}, answers)
        status, again = register(port, "front", sources["front"])
        check("registering the same again answers the same", status == 200 and again == answers["front"][1], again)
        status, listed = call(port, "/rex/sources")
        status_one, one = call(port, "/rex/sources/front")
        check("GET lists every source and shows one, its auth only by kind",
              status == 200 and [row.get("name") for row in loads(listed).get("sources", [])] == ["disk", "front", "plain"]
              and status_one == 200 and loads(one).get("auth") == "basic", (listed, one))
        check("and never a credential", FRONT_PASSWORD.encode() not in listed + one + again
              and FRONT_USER.encode() not in listed + one + again, (listed, one))
        check("an unknown source is a 404", call(port, "/rex/sources/nobody")[0] == 404)

        disk_part = push(port, "disk", movie)
        front_part = push(port, "front", episode)
        check("each source's push is a row with a part", disk_part and front_part, (disk_part, front_part))

        # -- no prefix to line up: an item lands by its directory -----------------------------
        # zurg's mount_path defaults to empty, and then it pushes no `file`: rex made it
        # `/movies/...`, which the seeded `/zurg/movies` did not hold, so every item was refused.
        plain_movie = {"id": "movies/Plain.Release.2021/Plain.Release.2021.mkv",
                       "path": "movies/Plain.Release.2021/Plain.Release.2021.mkv", "size": len(BODY),
                       "media_info": movie["media_info"]}
        plain_episode = {"id": "shows/Plain.Show.S01.1080p/Plain.Show.S01E01.1080p.mkv",
                         "path": "shows/Plain.Show.S01.1080p/Plain.Show.S01E01.1080p.mkv", "size": len(BODY),
                         "media_info": episode["media_info"]}
        status, raw = call(port, "/rex/feed/plain/items", "POST", {"items": [plain_movie, plain_episode]},
                           {"Content-Type": "application/json"})
        body = json.loads(raw)
        check("a source with no mount pushing no file: both items placed, none refused",
              status == 200 and body.get("created") == 2 and not body.get("rejected"), body)
        status, raw = call(port, "/library/sections/1/all")
        titles = [row.get("title") for row in json.loads(raw)["MediaContainer"].get("Metadata") or []]
        check("the movies directory's item is in the movie library", "Plain Release" in titles, titles)

        # -- a probe with no streams is held, not shown unplayable --------------------------
        # docs/feed-api.md's own example item carried `{"format": {}, "streams": []}`, which
        # rex took for a probe and showed as a row Plex Web cannot play.
        empty = {"id": "movies/Empty.Probe.2022/Empty.Probe.2022.mkv",
                 "path": "movies/Empty.Probe.2022/Empty.Probe.2022.mkv", "size": len(BODY),
                 "media_info": {"format": {}, "streams": []}}
        status, raw = call(port, "/rex/feed/plain/items", "POST", {"items": [empty]},
                           {"Content-Type": "application/json"})
        body = json.loads(raw)
        check("an item whose probe has no streams is held", status == 200 and body.get("held") == 1
              and body.get("created") == 0, body)

        # -- a server that ignores Range: the client still gets the bytes it asked for ----
        status, raw = call(port, disk_part, headers={"Range": "bytes=100-199"})
        check("a range read from a server that answers 200 is the range's bytes, not the file's first",
              status == 206 and raw == BODY[100:200], (status, raw[:8].hex(), BODY[100:108].hex()))
        status, raw = call(port, disk_part, headers={"Range": "bytes=65000-"})
        check("an open-ended range from it is the file's tail", status == 206 and raw == BODY[65000:],
              (status, len(raw)))
        status, raw = call(port, disk_part)
        check("a whole read from it is the whole file", status == 200 and raw == BODY, (status, len(raw)))

        # -- a redirect chain ends at another origin: the source's credential stays with the source ----
        status, raw = call(port, front_part, headers={"Range": "bytes=100-199"})
        check("a part the source redirects elsewhere streams from there", status == 206 and raw == BODY[100:200],
              (status, raw[:60]))
        check("the source itself got its credential", front_seen and front_seen[-1][1], front_seen[-1:])
        check("case-insensitive same-origin redirect keeps its credential",
              any(path == "/redirect/relative.mkv" and authorization for path, authorization in front_seen),
              front_seen)
        check("a query-only redirect keeps its current path", any(path.endswith("?signed=1") for path, _ in front_seen), front_seen)
        check("the other host never sees the source's credential", cdn_seen and not any(a for _, a, _ in cdn_seen),
              cdn_seen)
        check("a protocol-relative redirect drops its fragment", cdn_seen and cdn_seen[-1][0] == "/relative.mkv", cdn_seen)
        check("and gets the Range", cdn_seen and cdn_seen[-1][2] == "bytes=100-199", cdn_seen[-1:])

        # -- a new registration is what the next part read streams from ---------------------
        moved = os.path.join(work, "moved")
        os.makedirs(os.path.dirname(os.path.join(moved, movie["path"])))
        open(os.path.join(moved, movie["path"]), "wb").write(BODY[::-1])
        moved_port = serve(functools.partial(http.server.SimpleHTTPRequestHandler, directory=moved))
        status, _ = register(port, "disk", {"url": "http://127.0.0.1:%d" % moved_port})
        status_read, raw = call(port, disk_part, headers={"Range": "bytes=0-99"})
        check("registered again elsewhere, the next part read streams from there, no restart",
              status == 200 and status_read == 206 and raw == BODY[::-1][:100], (status, status_read, raw[:8]))

        # -- a restart keeps what registered, before anything registers again ---------------
        stop(process, log)
        port, process, log = start(work, {})
        listed = registered(port)
        status_read, raw = call(port, disk_part, headers={"Range": "bytes=0-99"})
        check("a restart keeps the registered sources, the last registration of each",
              listed == [("disk", "http://127.0.0.1:%d" % moved_port), ("front", sources["front"]["url"]),
                         ("plain", sources["plain"]["url"])] and status_read == 206 and raw == BODY[::-1][:100],
              (listed, status_read))
        status, raw = call(port, front_part, headers={"Range": "bytes=100-199"})
        check("with their credentials: the source behind basic auth still streams",
              status == 206 and raw == BODY[100:200], status)
        check("and never logged one", FRONT_PASSWORD not in open(os.path.join(work, "server.log"), errors="replace").read())
    finally:
        stop(process, log)
        shutil.rmtree(work, ignore_errors=True)
    gated()
    unregistered_rows()
    delete_source()
    failed = results.count(False)
    print("\n%d/%d source checks passed" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
