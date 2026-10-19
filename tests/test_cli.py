#!/usr/bin/env python3
"""The server's command line is `main()`'s argparse, on both servers.

What a caller of the Python oracle could rely on and a port can silently drop: `-h`/`--help`
anywhere among the options prints the help and exits 0 without serving; an option
argparse does not know is refused with exit 2; `--flag=value` and a unique prefix of
a flag are accepted; with no `--web-root` the `webclient` folder beside the
program is the client served at `/web`; and a `--catalog` that does not exist is seeded,
so one command sets a server up.

Drives real server processes (`tests/server_command.py`).

    python3 tests/test_cli.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from server_command import server_command
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:400]))


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def run(*args):
    """Run the server command to completion; a server that starts is a failure."""
    try:
        done = subprocess.run(server_command(*args, root=ROOT), cwd=ROOT, env=ENV,
                              capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        return None
    return done


def serving(*args):
    """Start a server, wait for /identity, and hand back (process, port)."""
    port = free_port()
    process = subprocess.Popen(server_command(*[a.replace("PORT", str(port)) for a in args],
                                              root=ROOT),
                               cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    def ready():
        if process.poll() is not None:
            return True
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2).read()
            return True
        except Exception:
            return False
    served = poll_until(ready, 20)
    if served and process.poll() is None:
        return process, port
    return process, None


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main():
    # argparse acts on `-h`/`--help` where it is reached: after other options, and
    # before an unknown one it would otherwise refuse at the end.
    for label, args in (
        ("--help after server options", ["--port", "PORT", "--catalog", CATALOG, "--help"]),
        ("-h after server options", ["--catalog", CATALOG, "--port", "PORT", "-h"]),
        ("--help before an unknown option", ["--port", "PORT", "--help", "--bogus"]),
        ("--hel, the unique prefix of --help", ["--port", "PORT", "--hel"]),
    ):
        port = free_port()
        done = run(*[a.replace("PORT", str(port)) for a in args])
        ok = (done is not None and done.returncode == 0
              and done.stdout.startswith(b"usage: ")
              and b"--web-root WEB_ROOT" in done.stdout
              and b"show this help message and exit" in done.stdout
              and not listening(port))
        check("%s prints the help and exits 0 without serving" % label, ok,
              "timed out (it served)" if done is None else
              (done.returncode, done.stdout[:200], done.stderr[-300:]))

    # An option `main()` does not declare is refused, not swallowed.
    for label, args, wanted in (
        ("--no-gdm, which argparse does not declare", ["--no-gdm"], b"unrecognized arguments: --no-gdm"),
        # Sources register over the API now (docs/feed-api.md); the file is gone.
        ("--sources, which no longer exists", ["--sources", "sources.json"],
         b"unrecognized arguments: --sources sources.json"),
        ("a stray positional argument", ["stray"], b"unrecognized arguments: stray"),
        ("--h, which could be --help, --host or --heartbeat-interval", ["--h"], b"ambiguous option: --h"),
        ("a value that looks like an option", ["--token", "--verbose"], b"argument --token: expected one argument"),
        ("a --port that is not an integer", ["--port", "abc"], b"argument --port: invalid int value: 'abc'"),
    ):
        # A port of its own, so a server that wrongly starts never touches 32400.
        done = run("--port", str(free_port()), "--catalog", CATALOG, "--gdm", "off", *args)
        ok = (done is not None and done.returncode == 2 and wanted in done.stderr
              and b"usage: " in done.stderr)
        check("refuses %s with exit 2" % label, ok,
              "timed out (it served)" if done is None else (done.returncode, done.stderr[-300:]))

    # `--flag=value` and a unique prefix are argparse spellings of the same options.
    for label, args in (
        ("--port=N and --catalog=FILE", ["--port=PORT", "--catalog=" + CATALOG, "--gdm=off"]),
        ("the prefixes --por, --cat and --gd", ["--por", "PORT", "--cat", CATALOG, "--gd", "off"]),
    ):
        process, port = serving(*args)
        try:
            check("serves on %s" % label, port is not None)
        finally:
            stop(process)

    # One command: a catalog that does not exist is seeded as `rex catalog --seed` would,
    # named by --friendly-name, its libraries under --zurg-mount.
    scratch = tempfile.mkdtemp(prefix="rex-cli-seed-")
    seeded = os.path.join(scratch, "rex.db")
    # --assert-claimed: an unclaimed server serves no client, and this reads the library.
    process, port = serving("--port", "PORT", "--catalog", seeded, "--friendly-name", "Seeded Den",
                            "--zurg-mount", "/srv/zurg", "--gdm", "off", "--assert-claimed")
    try:
        def get(path):
            request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                             headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())["MediaContainer"]
        root = get("/") if port else {}
        sections = get("/library/sections").get("Directory", []) if port else []
        locations = sorted((row.get("type"), location.get("path")) for row in sections
                           for location in row.get("Location", []))
        check("a --catalog that does not exist is seeded on start: named, its libraries under the mount",
              port is not None and root.get("friendlyName") == "Seeded Den"
              and locations == [("movie", "/srv/zurg/movies"), ("show", "/srv/zurg/shows")]
              and os.path.exists(seeded) and open(seeded, "rb").read(16) == b"SQLite format 3\x00",
              (port, root.get("friendlyName"), locations))
    finally:
        stop(process)
        shutil.rmtree(scratch, ignore_errors=True)

    # With neither flag the guide's one command is `rex --catalog rex.db`: the server is named
    # after its host, as a real PMS is, and its libraries are under zurg's default mount.
    scratch = tempfile.mkdtemp(prefix="rex-cli-seed-")
    process, port = serving("--port", "PORT", "--catalog", os.path.join(scratch, "rex.db"), "--gdm", "off",
                            "--assert-claimed")
    try:
        request = urllib.request.Request("http://127.0.0.1:%d/library/sections" % port,
                                         headers={"Accept": "application/json"}) if port else None
        sections = json.loads(urllib.request.urlopen(request, timeout=10).read())["MediaContainer"] if port else {}
        identity = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=10).read().decode() if port else ""
        locations = sorted(location.get("path") for row in sections.get("Directory", [])
                           for location in row.get("Location", []))
        check("seeded with no --friendly-name and no --zurg-mount: the host's name, under /mnt/zurg",
              'friendlyName="%s"' % socket.gethostname() in identity
              and locations == ["/mnt/zurg/movies", "/mnt/zurg/shows"], (identity[:300], locations))
    finally:
        stop(process)
        shutil.rmtree(scratch, ignore_errors=True)

    # No --web-root: the `webclient` beside the program is served at /web. Use the
    # checkout's own when there is one; otherwise lay down a marker and remove it.
    webclient = os.path.join(ROOT, "webclient")
    made = not os.path.exists(webclient)
    marker = b"<!doctype html><title>rex test_cli marker</title>"
    if made:
        os.makedirs(webclient)
        with open(os.path.join(webclient, "index.html"), "wb") as handle:
            handle.write(marker)
    try:
        process, port = serving("--port", "PORT", "--catalog", CATALOG, "--gdm", "off")
        try:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/web/index.html" % port,
                                            timeout=10) as response:
                    status, body = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, body = error.code, error.read()
            except Exception as error:
                status, body = None, repr(error).encode()
            ok = status == 200 and (body == marker if made else len(body) > 0)
            check("with no --web-root, /web serves the webclient folder beside the program",
                  ok, (status, body[:200]))
        finally:
            stop(process)
    finally:
        if made:
            shutil.rmtree(webclient, ignore_errors=True)

    passed = sum(results)
    print("PASS %d/%d command-line checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
