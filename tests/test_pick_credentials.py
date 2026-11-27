#!/usr/bin/env python3
"""A picture picked by url keeps no credential in rex's state file, black-box (Fizzy card 859).

`PUT /library/metadata/{id}/{poster,art,clearLogo}?url=` stored the url as it came, token and
password included, in the state file (`rex.db`) and in every row a client reads, so the token
outlived the request and went into any database a user shared. A real PMS keeps the picture it
fetched for a pick, not the url: plexlab's `user_thumb_url` for a poster picked by url is
`metadata://posters/<agent>_<sha1>`. rex now fetches such a url at once and keeps its bytes under
the url without its credential, and a start cleans what an older build stored.

Two legs:

* the cleanup: `fixtures/pick_credentials_s859.sql` is a database the unfixed build (36c147df)
  wrote after five picks carrying a marker credential (an item's poster and backdrop, a
  playlist's poster, a collection's backdrop and clear logo). A start must leave none of the
  markers anywhere in the database's files, and keep each pick without its credential.
* a pick now: a poster picked by a public url carrying a token is stored without it, and still
  served, before and after a restart. It needs raw.githubusercontent.com; offline it is skipped.

    python3 tests/test_pick_credentials.py
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from server_command import server_command
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "pick_credentials_s859.sql")
TOKEN = "REPLICATE859SERVER"
MARKER = re.compile(rb"REPLICATE859(?!SERVER)[A-Za-z]+")
POSTER = "https://raw.githubusercontent.com/Kometa-Team/Default-Images/master/genre/Family.jpg"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- %s" % (str(detail)[:900],)))


def quote(text):
    return urllib.parse.quote(text, safe="")


def markers(directory):
    """Every marker credential left in any file of the database (its WAL and journal too)."""
    found = set()
    for name in os.listdir(directory):
        with open(os.path.join(directory, name), "rb") as handle:
            found.update(match.decode() for match in MARKER.findall(handle.read()))
    return sorted(found)


class Server:
    def __init__(self, catalog, state=None):
        self.port = int(os.environ.get("REX_TEST_PORT") or free_port())
        args = ["--port", self.port, "--catalog", catalog, "--gdm", "off", "--token", TOKEN, "--assert-claimed"]
        if state:
            args += ["--state", state]
        env = dict(os.environ, REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
        for name in ("REX_TOKEN", "PLEX_TOKEN", "REX_STATE"):
            env.pop(name, None)
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        self.stop()
        raise SystemExit("rex did not start: %s" % self.output()[-2000:])

    def call(self, method, path):
        path += ("&" if "?" in path else "?") + "X-Plex-Token=" + TOKEN
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method,
                                         headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def json(self, path):
        return json.loads(self.call("GET", path)[1] or b"{}").get("MediaContainer") or {}

    def selected(self, key, leaf):
        return [row.get("key") for row in self.json("/library/metadata/%s/%s" % (key, leaf)).get("Metadata") or []
                if row.get("selected")]

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def output(self):
        self.log.seek(0)
        return self.log.read().decode("utf-8", "replace")


def cleanup_leg():
    directory = tempfile.mkdtemp(prefix="rex-859-")
    try:
        catalog = os.path.join(directory, "rex.db")
        with open(FIXTURE, encoding="utf-8") as handle:
            database = sqlite3.connect(catalog)
            database.executescript(handle.read())
            database.close()
        before = markers(directory)
        check("the unfixed build's database holds the five picks' credentials", len(before) == 5, before)
        # The database is the catalog and its own state; the launcher's --no-state is lifted by naming it.
        server = Server(catalog, catalog)
        try:
            item_poster = server.selected(1008, "posters")
            item_art = server.selected(1007, "arts")
            list_poster = server.selected(40001, "posters")
            list_art = server.selected(50001, "arts")
        finally:
            server.stop()
        check("a start leaves no credential in the database's files", not markers(directory), markers(directory))
        check("the item's poster pick is kept without its token",
              item_poster == [POSTER], item_poster)
        check("the item's backdrop pick is kept without its password",
              item_art == ["https://art.example.invalid/bg.jpg"], item_art)
        check("the playlist's poster pick is kept without its token",
              list_poster == ["https://cdn.example.invalid/p.jpg"], list_poster)
        check("the collection's backdrop pick is kept without its token",
              list_art == ["https://cdn.example.invalid/c.jpg"], list_art)
        check("rex said nothing of the credentials", "REPLICATE859" not in server.output().replace(TOKEN, ""),
              [line for line in server.output().splitlines() if "REPLICATE859" in line][:4])
        rows = sqlite3.connect(catalog).execute(
            "select body from row where coll = 'state:collections'"
        ).fetchall()
        check("the collection's clear logo pick is kept without its token",
              any("https://cdn.example.invalid/l.png" in (body or "") for (body,) in rows), rows[:1])
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def pick_leg():
    try:
        with urllib.request.urlopen(POSTER, timeout=20) as response:
            original = response.read()
    except OSError as error:
        print("SKIP a pick now: %s is unreachable (%s)" % (POSTER, error))
        return
    directory = tempfile.mkdtemp(prefix="rex-859-")
    try:
        catalog = os.path.join(directory, "catalog.json")
        shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
        state = os.path.join(directory, "rex.db")
        server = Server(catalog, state)
        try:
            status, path = server.call("PUT", "/library/metadata/1008/poster?url="
                                       + quote(POSTER + "?X-Plex-Token=REPLICATE859now"))
            path = path.decode()
            check("the pick answers its new path", status == 200 and path.startswith("/library/metadata/1008/thumb/"),
                  (status, path))
            row = (server.json("/library/metadata/1008?includeAlternates=1").get("Metadata") or [{}])[0]
            check("no client reads the token back (the row's Thumb alternates)",
                  row.get("Thumb") and "REPLICATE859" not in json.dumps(row), row.get("Thumb"))
            check("the pick is selected, named without its token", server.selected(1008, "posters") == [POSTER],
                  server.selected(1008, "posters"))
            served = server.call("GET", path)
            check("the picture is served", served[0] == 200 and served[1] == original, (served[0], len(served[1])))
        finally:
            server.stop()
        check("the state file holds no token", not markers(directory), markers(directory))
        uploads = [key for (key,) in sqlite3.connect(state).execute("select key from row where coll = 'state:uploads'")]
        check("the picture fetched at the pick is kept under the bare url", POSTER in uploads, uploads)
        server = Server(catalog, state)
        try:
            served = server.call("GET", path)
        finally:
            server.stop()
        check("the kept picture is served after a restart", served[0] == 200 and served[1] == original,
              (served[0], len(served[1])))
        fetched = [line for line in server.output().splitlines() if "ARTWORK fetch" in line and "Family.jpg" in line]
        check("it is served from what was kept, not fetched again", not fetched, fetched)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main():
    cleanup_leg()
    pick_leg()
    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
