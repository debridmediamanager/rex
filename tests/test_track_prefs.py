"""The tracks a Part starts on follow the owner's plex.tv settings, as on a real PMS.

A claimed rex reads the owner's `/api/v2/user` profile (here from a stub plex.tv answering
the profile a real account sent: audio English, subtitles always, English) and selects
those tracks on every Part nobody chose tracks for. The Part is a real one: Dark Matter
S01E03 of a German release, whose ffprobe (`fixtures/track_selection/
dark_matter_s01e03_ffprobe.json`) lists German audio first and a forced German subtitle
as default. A real PMS with that account started it on English audio and English
subtitles; rex started it on German with none, on every episode. A track chosen by hand
stays chosen, across a restart and the next settings read.

    python3 tests/test_track_prefs.py
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

from server_command import server_command
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
PROBE = json.load(open(os.path.join(FIXTURES, "track_selection", "dark_matter_s01e03_ffprobe.json")))
LEGACY = json.load(open(os.path.join(FIXTURES, "feed", "legacy_scan_rows.json")))
MACHINE = "00000000000000000000000000000000000000fe"
SERVER_TOKEN = "track-prefs-server-token"
# The profile element a real account's /api/v2/user carried on 2026-10-15.
PROFILE = ('<profile autoSelectAudio="1" defaultAudioAccessibility="0" defaultAudioLanguage="en" '
           'defaultAudioLanguages="" defaultSubtitleLanguage="en" defaultSubtitleLanguages="" '
           'autoSelectSubtitle="2" defaultSubtitleAccessibility="0" defaultSubtitleForced="0" '
           'watchedIndicator="1" mediaReviewsVisibility="0" mediaReviewsLanguages="" mediaPostsVisibility="1"/>')
RELEASE = "shows/Dark.Matter.2024.S01.German.DL.Atmos.2160p.ATVP.WEB.H265-ZeroTwo"
FILE = "Dark.Matter.2024.S01E03.German.DL.Atmos.2160p.ATVP.WEB.H265-ZeroTwo.mkv"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


class PlexTv(http.server.BaseHTTPRequestHandler):
    users = []

    def log_message(self, *args):
        pass

    def answer(self):
        # `REX_PLEXTV_URL` sends `<base>/<host><path>`.
        path = self.path.split("?")[0]
        if path == "/servers.plex.tv/api/v2/user":
            PlexTv.users.append(self.path)
            body = '<?xml version="1.0" encoding="UTF-8"?>\n<user id="1" username="owner">%s</user>\n' % PROFILE
        else:
            body = '<?xml version="1.0" encoding="UTF-8"?>\n<MediaContainer size="0"/>\n'
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_PUT = do_POST = answer


class Server:
    def __init__(self, work, stub_port):
        self.port = free_port()
        env = dict(os.environ, PLEX_TOKEN="fake", REX_PLEXTV_URL="http://127.0.0.1:%d" % stub_port)
        for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY", "REX_METADATA_URL", "REX_METADATA_KEY"):
            env.pop(name, None)
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", os.path.join(work, "catalog.json"),
                           "--state", os.path.join(work, "rex.db"), "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        poll_until(lambda: self.request("/identity")[0] == 200, 40)

    def request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
            headers={"Accept": "application/json", "X-Plex-Token": SERVER_TOKEN,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw[:1] == b"{" else raw
        except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError) as error:
            return getattr(error, "code", 0), None

    def streams(self, key):
        status, body = self.request("/library/metadata/%s" % key)
        if status != 200:
            return []
        return body["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0].get("Stream") or []

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=20)
        self.log.close()


def selected(streams, kind):
    return [(s.get("languageCode"), bool(s.get("forced"))) for s in streams
            if s.get("streamType") == kind and s.get("selected")]


def main():
    work = tempfile.mkdtemp(prefix="rex-trackprefs-")
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), PlexTv)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    json.dump({"machineIdentifier": MACHINE, "friendlyName": "rex-tracks", "sections": LEGACY["sections"],
               "items": []}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "owner", "email": "owner@example.com",
               "feature_uuids": [], "account_id": 1, "view_state_cursor": None, "access_tokens": []},
              open(os.path.join(work, "claim_state.json"), "w"))
    server = Server(work, stub.server_address[1])
    try:
        status, _ = server.request("/rex/sources/zurg", "PUT", {"url": "http://127.0.0.1:9/dav", "mount": "/zurg"})
        path = "%s/%s" % (RELEASE, FILE)
        status, body = server.request("/rex/feed/zurg/items", "POST", {"items": [{
            "id": path, "path": path, "file": "/zurg/" + path, "name": FILE, "release": RELEASE,
            "size": 8000000000, "hash": "b" * 40, "media_info": PROBE}]})
        check("the real episode is pushed", status == 200 and body.get("created") == 1, (status, body))
        status, body = server.request("/rex/feed/zurg/items")
        key = {row["id"]: row["ratingKey"] for row in (body or {}).get("items", [])}.get(path)
        poll_until(lambda: PlexTv.users and selected(server.streams(key), 2) == [("eng", False)], 30)
        streams = server.streams(key)
        check("the owner's settings were read from plex.tv", PlexTv.users, PlexTv.users)
        check("the episode starts on English audio, as the real PMS started it",
              selected(streams, 2) == [("eng", False)], selected(streams, 2))
        check("and on the English subtitle, not the forced German one flagged default",
              selected(streams, 3) == [("eng", False)], selected(streams, 3))
        german = next(s["id"] for s in streams if s.get("streamType") == 2 and s.get("languageCode") == "deu")
        part = server.request("/library/metadata/%s" % key)[1]["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0]["id"]
        status, _ = server.request("/library/parts/%s?audioStreamID=%s&allParts=1" % (part, german), "PUT")
        check("German chosen by hand is selected", status == 200 and selected(server.streams(key), 2) == [("deu", False)],
              (status, selected(server.streams(key), 2)))
        server.stop()
        reads = len(PlexTv.users)
        server = Server(work, stub.server_address[1])
        poll_until(lambda: len(PlexTv.users) > reads, 30)
        check("after a restart and the next settings read, the hand-chosen German stays",
              len(PlexTv.users) > reads and selected(server.streams(key), 2) == [("deu", False)],
              (len(PlexTv.users), selected(server.streams(key), 2)))
    finally:
        server.stop()
        stub.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
