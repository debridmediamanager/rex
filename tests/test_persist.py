#!/usr/bin/env python3
"""`--state`: what the UI changes survives a restart, and nothing else changes.

Drives real server processes on a private copy of `catalog_ui.json`. Each write
below is one the suites already prove the server stores; this suite proves the
store outlives the process. Every surviving read is compared against the SAME
read taken before the restart, and against a fresh instance's, so a check cannot
pass because the write never landed in the first place.

    python3 tests/test_persist.py
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
import urllib.parse


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBRARY = "com.plexapp.plugins.library"
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "persist-suite",
           "X-Plex-Device-Name": "Persist Suite", "X-Plex-Platform": "Chrome",
           "X-Plex-Product": "Plex Web"}

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Instance:
    def __init__(self, catalog, state=None):
        self.port = free_port()
        command = [sys.executable, os.path.join(ROOT, "server.py"), "--port", str(self.port),
                   "--catalog", catalog, "--gdm", "off", "--assert-claimed"]
        if state:
            command += ["--state", state]
        env = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1")
        env.pop("PLEXSIM_STATE", None)
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.log)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        self.log.seek(0)
        raise SystemExit("plexsim did not start:\n" + self.log.read().decode(errors="replace"))

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers=dict(HEADERS, **(headers or {})))
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        try:
            return response.status, json.loads(body) if body else None
        except ValueError:
            return response.status, body.decode(errors="replace")

    def stop(self, sig=signal.SIGTERM):
        self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")


def rows(body):
    return ((body or {}).get("MediaContainer") or {}).get("Metadata") or []


def first(body):
    return (rows(body) or [{}])[0]


# The reads a user would see change: one row, list, setting or history page per
# write below. `type=0,1` because a scrobble's history row is `viewType` 1, which the
# page's default leaves out.
READS = ("/library/metadata/1007", "/library/metadata/1008", "/library/metadata/1009",
         "/library/metadata/1001", "/library/metadata/20001", "/library/metadata/30001",
         "/library/sections", "/library/sections/1/all", "/library/sections/1/collections",
         "/playlists", "/status/sessions/history/all?type=0,1", "/:/prefs")


def snapshot(instance, reads=READS):
    return {path: instance.request("GET", path) for path in reads}


def write_everything(instance, machine):
    uri = lambda key: urllib.parse.quote(
        "server://%s/%s/library/metadata/%s" % (machine, LIBRARY, key), safe="")
    writes = [
        ("GET", "/:/scrobble?key=1007&identifier=" + LIBRARY),
        ("GET", "/:/scrobble?key=1001&identifier=" + LIBRARY),
        ("GET", "/:/rate?key=1008&identifier=%s&rating=8" % LIBRARY),
        ("GET", "/:/progress?key=1009&time=65000&identifier=" + LIBRARY),
        ("PUT", "/library/metadata/1008?title.value=Persisted%20Title&title.locked=1"),
        ("PUT", "/library/metadata/1008?summary.value=Kept%20across%20a%20restart"),
        ("POST", "/playlists?type=video&title=Kept%%20List&smart=0&uri=%s" % uri(1007)),
        ("POST", "/library/collections?type=1&title=Kept%%20Collection&smart=0"
                 "&sectionId=1&uri=%s" % uri(1008)),
        ("PUT", "/:/prefs?FriendlyName=Persisted%20Server"),
        ("POST", "/library/sections?name=Added%20Library&type=movie"
                 "&agent=tv.plex.agents.movie&scanner=Plex%20Movie&language=en-US"
                 "&location=%2Fadded"),
        ("DELETE", "/library/metadata/1010"),
    ]
    statuses = [instance.request(method, path)[0] for method, path in writes]
    # A second client, seen only in this run: the device table is the Devices page.
    statuses.append(instance.request("GET", "/identity", {
        "X-Plex-Client-Identifier": "persist-second-device",
        "X-Plex-Device-Name": "Second Device"})[0])
    return all(200 <= status < 300 for status in statuses), statuses


def differing(before, after):
    return [path for path in before if before[path] != after.get(path)]


def main():
    work = tempfile.mkdtemp(prefix="plexsim-persist-")
    try:
        catalog = os.path.join(work, "catalog.json")
        shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
        machine = json.load(open(catalog))["machineIdentifier"]
        state = os.path.join(work, "state.pickle")

        fresh_instance = Instance(catalog)
        fresh = snapshot(fresh_instance)
        fresh_instance.stop()

        # -- writes survive a SIGTERM taken before the saver's interval --------
        first_run = Instance(catalog, state)
        ok, statuses = write_everything(first_run, machine)
        check("every write is accepted", ok, "statuses %s" % statuses)
        written = snapshot(first_run)
        changed = differing(fresh, written)
        # Every read must show its write, or the restart comparison proves nothing.
        check("every read shows its write before the restart",
              set(changed) == set(READS), "unchanged: %s" % sorted(set(READS) - set(changed)))
        first_run.stop(signal.SIGTERM)
        check("SIGTERM leaves a state file", os.path.exists(state), first_run.output()[-800:])

        second_run = Instance(catalog, state)
        restored = snapshot(second_run)
        lost = differing(written, restored)
        check("every read is identical after a restart", not lost,
              "; ".join("%s: before=%s after=%s" % (path, json.dumps(written[path])[:300],
                                                    json.dumps(restored.get(path))[:300])
                        for path in lost))
        devices = {device.get("clientIdentifier"): device for device in
                   second_run.request("GET", "/devices")[1]["MediaContainer"]["Device"]}
        check("the device table outlives the process that saw the device",
              (devices.get("persist-second-device") or {}).get("name") == "Second Device",
              "devices %s" % sorted(devices))
        check("the restored server keeps its new name",
              second_run.request("GET", "/")[1]["MediaContainer"].get("friendlyName")
              == "Persisted Server")

        # -- a crash loses at most the saver's interval -----------------------
        second_run.request("GET", "/:/rate?key=1007&identifier=%s&rating=4" % LIBRARY)
        time.sleep(4)
        second_run.stop(signal.SIGKILL)
        third_run = Instance(catalog, state)
        check("a write survives SIGKILL once the saver has run",
              first(third_run.request("GET", "/library/metadata/1007")[1]).get("userRating")
              == 4.0)
        third_run.stop()

        # -- without --state a restart is still the undo button ----------------
        plain = Instance(catalog)
        again = snapshot(plain)
        plain.stop()
        check("without --state every read is the fixture's again",
              not differing(fresh, again), "differ: %s" % differing(fresh, again))

        # -- a rebuilt catalog keeps view state and settings, not structure ------
        document = json.load(open(catalog))
        document["items"] = [row for row in document["items"] if row["ratingKey"] != 1011]
        json.dump(document, open(catalog, "w"))
        rebuilt = Instance(catalog, state)
        movie = first(rebuilt.request("GET", "/library/metadata/1007")[1])
        rated = first(rebuilt.request("GET", "/library/metadata/1008")[1])
        episode = first(rebuilt.request("GET", "/library/metadata/1001")[1])
        playlists = rows(rebuilt.request("GET", "/playlists")[1])
        sections = ((rebuilt.request("GET", "/library/sections")[1] or {})
                    .get("MediaContainer") or {}).get("Directory") or []
        check("a rebuilt catalog keeps each row's view state",
              movie.get("viewCount") == 1 and movie.get("userRating") == 4.0
              and rated.get("userRating") == 8.0 and episode.get("viewCount") == 1,
              "movie=%s rated=%s episode=%s" % (
                  {k: movie.get(k) for k in ("viewCount", "userRating")},
                  rated.get("userRating"), episode.get("viewCount")))
        check("a rebuilt catalog keeps the server settings",
              rebuilt.request("GET", "/")[1]["MediaContainer"].get("friendlyName")
              == "Persisted Server")
        check("a rebuilt catalog drops edits, lists and library changes",
              rated.get("title") == first(fresh["/library/metadata/1008"][1]).get("title")
              and not playlists and len(sections) == 2,
              "title=%s playlists=%d sections=%d" % (rated.get("title"), len(playlists),
                                                      len(sections)))
        check("the superseded state file is kept aside", os.path.exists(state + ".stale"))
        rebuilt.stop()

        # -- a state file that cannot be read never stops the server ------------
        with open(state, "wb") as handle:
            handle.write(b"\x80\x04not a pickle")
        corrupt = Instance(catalog, state)
        check("an unreadable state file starts fresh and is kept aside",
              corrupt.request("GET", "/identity")[0] == 200
              and os.path.exists(state + ".corrupt"))
        corrupt.stop()
        # A pickle that names a class is refused rather than run.
        import collections
        import pickle
        with open(state, "wb") as handle:
            pickle.dump({"format": 1, "catalog": collections.OrderedDict()}, handle)
        refusing = Instance(catalog, state)
        alive = refusing.request("GET", "/identity")[0] == 200
        check("a state file naming a class is refused, not loaded",
              alive and "state files hold no classes" in refusing.output(),
              refusing.output()[-400:])
        refusing.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
