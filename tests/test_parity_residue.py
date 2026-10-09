#!/usr/bin/env python3
"""The residue: differences left after the route families were ported, found by
replaying the whole UI-suite recording against the oracle and the native server in
lockstep (one request to each, in order), so timing and concurrency drop out and what
remains is behaviour.

- `mediaCount` on a show or season counts each LEAF's versions (`filter_value`), so a
  show whose episodes each have one version matches `mediaCount=1`;
- a show library's `allLeaves` lists its episodes in broadcast order (`typed_rows`),
  which a season's index edit moves;
- an offline timeline that marks a play watched writes its history row under the
  playing client's device, not device 1;
- a remote artwork fetch sends the oracle's urllib headers, with no `Accept`.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_residue.py
"""
import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-residue",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}

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


def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS, Connection="close")
    sent.update(headers or {})
    try:
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        return response.status, json.loads(raw) if raw else None
    except ValueError:
        return response.status, raw


def mc(port, path, method="GET", headers=None):
    status, body = request(port, method, path, headers)
    return (body or {}).get("MediaContainer", {}) if isinstance(body, dict) else {"_status": status}


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


class Stub(http.server.BaseHTTPRequestHandler):
    """A remote image host that remembers the header names of each fetch."""
    seen = []

    def do_GET(self):
        Stub.seen.append([name.lower() for name in self.headers.keys()])
        body = b"\xff\xd8\xff\xe0" + b"\0" * 60
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run(port):
    # mediaCount on a show: each episode's versions, matched as a list.
    def size(query):
        return mc(port, "/library/sections/2/all?" + query).get("size")
    counts = {query: size(query) for query in
              ("type=2&mediaCount=1", "type=2&mediaCount%3E%3E=1", "type=2&mediaCount=0",
               "type=2&mediaCount=2")}
    check("a show matches mediaCount by its episodes' version counts",
          counts == {"type=2&mediaCount=1": 1, "type=2&mediaCount%3E%3E=1": 1,
                     "type=2&mediaCount=0": 0, "type=2&mediaCount=2": 1}, counts)

    # allLeaves in broadcast order, which a season's index edit moves.
    status, _ = request(port, "PUT", "/library/metadata/30001?index.value=41&index.locked=1")
    leaves = [(row.get("ratingKey"), row.get("parentIndex"))
              for row in mc(port, "/library/sections/2/allLeaves").get("Metadata") or []]
    check("a show library's allLeaves follows a season's edited index",
          status == 200 and leaves and leaves[-1] == ("1001", 41)
          and [index for _, index in leaves] == sorted(index for _, index in leaves), leaves)

    # An offline timeline's watched mark: its history row names the playing device.
    client = {"X-Plex-Client-Identifier": "residue-phone",
              "X-Plex-Product": "Plex for Android (Mobile)", "X-Plex-Platform": "Android",
              "X-Plex-Device-Name": "Residue Phone"}
    request(port, "PUT", "/:/unscrobble?identifier=com.plexapp.plugins.library&key=1013")
    request(port, "GET", "/:/timeline?key=%2Flibrary%2Fmetadata%2F1013&ratingKey=1013"
            "&duration=6653888&time=0&state=stopped&offline=1&viewCount=1", headers=client)
    devices = {row.get("clientIdentifier"): row.get("id")
               for row in mc(port, "/devices").get("Device") or []}
    history = mc(port, "/status/sessions/history/all?metadataItemID=1013").get("Metadata") or []
    ids = [row.get("deviceID") for row in history]
    phone = devices.get("residue-phone")
    check("an offline watched mark writes its history row under the playing device",
          phone is not None and ids and str(ids[-1]) == str(phone) and str(phone) != "1",
          "device %s, history deviceIDs %s" % (phone, ids))

    # A split's new row is an item of its own: clearing the source's setting afterwards
    # leaves the new row's alone.
    request(port, "PUT", "/library/metadata/1007/prefs?useOriginalTitle=1")
    request(port, "PUT", "/library/metadata/1007/merge?ids=1008")
    before = {row.get("ratingKey") for row in mc(port, "/library/sections/1/all").get("Metadata") or []}
    request(port, "PUT", "/library/metadata/1007/split")
    after = {row.get("ratingKey") for row in mc(port, "/library/sections/1/all").get("Metadata") or []}
    request(port, "PUT", "/library/metadata/1007/prefs?useOriginalTitle=")
    created = sorted(after - before)
    values = {key: (mc(port, "/library/metadata/%s" % key).get("Metadata") or [{}])[0]
              .get("useOriginalTitle") for key in ["1007"] + created}
    check("a split row keeps its own item setting when the source's is cleared",
          created and values.get("1007") is None
          and all(values.get(key) == "1" for key in created), values)

    # A remote artwork fetch: the oracle's urllib request carries no Accept header.
    stub = http.server.HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/poster.jpg" % stub.server_address[1]
        status, _ = request(port, "GET", "/photo/:/transcode?width=100&height=100&url=%s"
                            % urllib.parse.quote(url, safe=""))
    finally:
        stub.shutdown()
    check("a remote artwork fetch sends no Accept header",
          status == 200 and Stub.seen and all("accept" not in names for names in Stub.seen),
          "status %s, headers %s" % (status, Stub.seen))


def main():
    process, port = start()
    try:
        run(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d residue parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
