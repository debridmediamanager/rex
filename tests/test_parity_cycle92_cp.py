#!/usr/bin/env python3
"""Cycle 92, territory C+P: a second server on a catalog whose default state file is held.

Measured on plexscratch (PMS 1.43.4, `tests/fixtures/pms_second_instance.json`): a
second Plex Media Server started on the data directory another one is serving prints
"Plex Media Server is already running. Will not start..." and exits 1 at once, without
touching the pid file or the log. There is never a second server whose writes live
only in memory.

plexsim's default state file (`plexsim.db`/`state.pickle` beside the catalog) is that
data directory. A second server that found it held ran WITHOUT a state file, and its
clients saw what a PMS never shows: everything they wrote (a rating, a watched flag,
a playlist, the server's name) gone after a restart; and when the second server was
the first to come back, it took the other server's file and showed THAT server's
writes instead of its own. Now it refuses to start, as PMS does. An explicit `--state`
or `--no-state` names a different arrangement and still starts.

Drives real server processes (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle92_cp.py
"""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBRARY = "com.plexapp.plugins.library"
MEASURED = json.load(open(os.path.join(HERE, "fixtures", "pms_second_instance.json")))
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-cycle92-cp",
           "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


class Instance:
    """A server with no state flag unless `args` names one; `wait` False returns as
    soon as the process either listens or exits."""

    def __init__(self, catalog, args=()):
        self.port = free_port()
        command = server_command("--port", self.port, "--catalog", catalog,
                                 "--gdm", "off", "--assert-claimed", *args, root=ROOT)
        if not args:
            command.remove("--no-state")
        env = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
                   PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
            env.pop(name, None)
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        for _ in range(150):
            if listening(self.port) or self.proc.poll() is not None:
                break
            time.sleep(0.1)

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers=HEADERS)
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        return response.status, (json.loads(body) if body else None)

    def stop(self, sig=signal.SIGTERM):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")


def item(instance, key):
    body = instance.request("GET", "/library/metadata/%s" % key)[1]
    return (((body or {}).get("MediaContainer") or {}).get("Metadata") or [{}])[0]


def main():
    work = tempfile.mkdtemp(prefix="plexsim-c92cp-")
    catalog = os.path.join(work, "catalog.json")
    shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
    started = []
    try:
        owner = Instance(catalog)
        started.append(owner)
        check("the first server on the catalog starts and holds the default file",
              listening(owner.port), owner.output()[-400:])
        owner.request("GET", "/:/scrobble?key=1007&identifier=" + LIBRARY)

        second = Instance(catalog)
        started.append(second)
        try:
            code = second.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            code = None
        was_serving = code is None and listening(second.port)
        if was_serving:
            # The unfixed behaviour: a stateless second server. Show what its client sees.
            second.request("GET", "/:/rate?key=1008&identifier=%s&rating=8" % LIBRARY)
        check("a second server on a held default file exits %d, as a second PMS does"
              % MEASURED["exit"], code == MEASURED["exit"],
              "exit %s, serving %s; log %s" % (code, was_serving, second.output()[-400:]))
        line = MEASURED["stdout"].strip()
        check("it says what a second PMS says: %r" % line, line in second.output(),
              second.output()[-400:])
        check("it never answers a client", not was_serving)
        check("the first server still answers and keeps its write",
              item(owner, "1007").get("viewCount") == 1, item(owner, "1007"))

        other = Instance(catalog, args=("--no-state",))
        started.append(other)
        check("a second server with --no-state still starts", listening(other.port),
              other.output()[-400:])
        other.stop()
        explicit = Instance(catalog, args=("--state", os.path.join(work, "own.state")))
        started.append(explicit)
        check("a second server with its own --state still starts", listening(explicit.port),
              explicit.output()[-400:])
        explicit.stop()

        owner.stop(signal.SIGTERM)
        again = Instance(catalog)
        started.append(again)
        check("after the first server stops, a new default start takes the file and its write",
              listening(again.port) and item(again, "1007").get("viewCount") == 1,
              again.output()[-400:])
        check("and holds nothing the refused server was sent",
              item(again, "1008").get("userRating") is None, item(again, "1008"))
        again.stop()
    finally:
        for instance in started:
            instance.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("PASS %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
