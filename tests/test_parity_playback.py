#!/usr/bin/env python3
"""Playback, sessions and statistics answer the way the Python oracle answers them.

Each check pins a value, a shape or a header the oracle (`server.py`, `sessions.py`)
produces for the exact request, where the native port answered something else: the
play-queue item counter, the session model behind `/status/sessions`, the device table,
the per-device byte counters behind `/statistics/bandwidth`, the resource sampler behind
`/statistics/resources`, and the empty `text/html` answer every mutating endpoint gives.

Drives a real server process (`tests/server_command.py`), so it runs against the native
binary by default and against the oracle with `PLEXSIM_SERVER=python`:

    python3 tests/test_parity_playback.py
    PLEXSIM_SERVER=python python3 tests/test_parity_playback.py
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MACHINE = "f1x7u4e0000000000000000000000000000000f1"
ZONE = "Europe/Berlin"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Client:
    def __init__(self, port, identifier="parity-c1", **extra):
        self.port = port
        self.headers = {"Accept": "application/json", "X-Plex-Client-Identifier": identifier,
                        "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
                        "Connection": "close"}
        self.headers.update(extra)

    def raw(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            sent = dict(self.headers)
            sent.update(headers or {})
            conn.request(method, path, headers={k: v for k, v in sent.items() if v is not None})
            response = conn.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()

    def json(self, method, path, headers=None):
        status, headers, body = self.raw(method, path, headers)
        try:
            document = json.loads(body) if body else None
        except ValueError:
            document = None
        return status, headers, document


def container(document):
    return (document or {}).get("MediaContainer") or {}


def header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def queue_uri(path):
    return urllib.parse.quote("server://%s/com.plexapp.plugins.library%s" % (MACHINE, path),
                              safe="")


def timeline(client, key, state, time_ms, extra=""):
    return client.json("GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                       "&state=%s&time=%d&duration=600000%s" % (key, key, state, time_ms, extra))


def main():
    port = int(os.environ.get("PLEXSIM_TEST_PORT") or free_port())
    # The statistics buckets are CALENDAR buckets in the server's local time; a zone with
    # summer time is what tells a local January 1st from a UTC one.
    env = dict(os.environ, PLEXSIM_PLEXTV_URL="http://127.0.0.1:9",
               PYTHONDONTWRITEBYTECODE="1", TZ=ZONE)
    for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
        env.pop(name, None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                       "--gdm", "off", "--assert-claimed", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        run(port)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    passed = sum(results)
    print("PASS %d/%d parity checks (playback, sessions, statistics)" % (passed, len(results))
          if passed == len(results) else
          "FAIL %d/%d parity checks (playback, sessions, statistics)" % (passed, len(results)))
    return 0 if passed == len(results) else 1


def run(port):
    web = Client(port)

    # --- the idle surface -------------------------------------------------------------
    status, _, body = web.json("GET", "/status/sessions")
    check("an idle /status/sessions is a bare {size: 0}",
          status == 200 and body == {"MediaContainer": {"size": 0}}, body)
    status, _, body = web.json("GET", "/status/sessions/background")
    check("/status/sessions/background is a bare {size: 0}",
          body == {"MediaContainer": {"size": 0}}, body)
    status, _, body = web.json("GET", "/transcode/sessions")
    check("/transcode/sessions is a bare {size: 0}", body == {"MediaContainer": {"size": 0}}, body)
    status, _, body = web.json("GET", "/activities")
    check("/activities with nothing running is a bare {size: 0}",
          body == {"MediaContainer": {"size": 0}}, body)

    # --- play queues ------------------------------------------------------------------
    status, _, body = web.json(
        "POST", "/playQueues?type=video&shuffle=0&repeat=0&own=1&uri=%s"
        % queue_uri("/library/sections/1/all"))
    queue = container(body)
    ids = [row.get("playQueueItemID") for row in queue.get("Metadata") or []]
    check("a process's first queue item is id 2 (the counter steps before it mints)",
          status == 200 and ids[:1] == [2] and queue.get("playQueueSelectedItemID") == 2,
          (status, ids, queue.get("playQueueSelectedItemID")))
    check("a play queue is the library container without allowSync",
          list(queue)[:4] == ["size", "identifier", "mediaTagPrefix", "mediaTagVersion"]
          and "allowSync" not in queue, list(queue)[:6])
    queue_id = queue.get("playQueueID")
    last = max(ids) if ids else 0
    count = len(ids)
    status, _, body = web.json("PUT", "/playQueues/%s/shuffle?repeat=0" % queue_id)
    shuffled = container(body)
    rows = shuffled.get("Metadata") or []
    rest = sorted(row.get("playQueueItemID", 0) for row in rows[1:])
    check("a shuffle keeps the selected row first with its id and mints the rest by slot",
          status == 200 and rows and rows[0].get("playQueueItemID") == 2
          and rest == list(range(last + 2, last + count + 1)),
          (status, [row.get("playQueueItemID") for row in rows], last, count))
    last = last + count
    status, _, body = web.json("PUT", "/playQueues/%s/unshuffle?repeat=0" % queue_id)
    rows = container(body).get("Metadata") or []
    natural = [row.get("ratingKey") for row in rows]
    check("an unshuffle restores the natural order and mints every other slot again",
          status == 200 and natural == ["1007", "1008", "1009", "1012", "1013", "1014"]
          and sorted(row.get("playQueueItemID", 0) for row in rows[1:])
          == list(range(last + 2, last + count + 1)),
          (status, natural, [row.get("playQueueItemID") for row in rows]))
    version = container(body).get("playQueueVersion")
    second = rows[1]["playQueueItemID"] if len(rows) > 1 else 0
    timeline(web, "1008", "playing", 1000, "&playQueueItemID=%d" % second)
    status, _, body = web.json("GET", "/playQueues/%s" % queue_id)
    check("a timeline moves the queue's selection without stepping its version",
          container(body).get("playQueueSelectedItemID") == second
          and container(body).get("playQueueVersion") == version,
          (container(body).get("playQueueSelectedItemID"), second,
           container(body).get("playQueueVersion"), version))
    timeline(web, "1008", "stopped", 1000)

    # --- the empty answer of a mutating endpoint --------------------------------------
    status, headers, raw = web.raw(
        "GET", "/:/scrobble?key=1009&identifier=com.plexapp.plugins.library")
    check("scrobble answers an empty 200 typed text/html",
          status == 200 and raw == b"" and header(headers, "Content-Type") == "text/html",
          (status, header(headers, "Content-Type")))
    status, headers, raw = web.raw(
        "GET", "/:/unscrobble?key=1009&identifier=com.plexapp.plugins.library")
    check("unscrobble answers an empty 200 typed text/html",
          status == 200 and header(headers, "Content-Type") == "text/html",
          (status, header(headers, "Content-Type")))

    # --- a session opened by a timeline -----------------------------------------------
    probe = Client(port, "parity-probe", **{"X-Plex-Device-Name": "Parity"})
    status, _, body = timeline(probe, "1007", "playing", 20000)
    status, _, body = probe.json("GET", "/status/sessions")
    rows = container(body).get("Metadata") or []
    row = rows[0] if rows else {}
    # The key is a server counter whose first value is 2: the queue's timeline above took
    # it, so this session is the second, 3 -- a string.
    check("a timeline alone lists one session, keyed by the server counter as a string",
          len(rows) == 1 and row.get("sessionKey") == "3", [r.get("sessionKey") for r in rows])
    check("a session row carries the ITEM's duration and no `state` of its own",
          row.get("duration") == 734167 and "state" not in row,
          (row.get("duration"), row.get("state")))
    check("a session row's User is {id: '1', thumb: '', title}",
          set((row.get("User") or {})) == {"id", "thumb", "title"}
          and (row.get("User") or {}).get("id") == "1"
          and (row.get("User") or {}).get("thumb") == "", row.get("User"))
    streams = [stream for media in row.get("Media") or [] for part in media.get("Part") or []
               for stream in part.get("Stream") or [] if stream.get("streamType") == 1]
    check("a session's video stream keeps its full extendedDisplayTitle",
          streams and streams[0].get("extendedDisplayTitle") == "1080p (VP8)",
          [stream.get("extendedDisplayTitle") for stream in streams])
    check("a session row carries the requesting peer's address",
          (row.get("Player") or {}).get("address") == "127.0.0.1", row.get("Player"))
    ratings = row.get("Rating") or []
    check("a session row's Rating values are strings",
          ratings and all(isinstance(rating.get("value"), str) for rating in ratings), ratings)

    # An edit made while the session plays is on the next session read.
    web.raw("PUT", "/library/sections/1/all?type=1&id=1007&title.value=Parity%20Title"
                   "&title.locked=1")
    _, _, body = probe.json("GET", "/status/sessions")
    rows = container(body).get("Metadata") or []
    check("the session row is projected from the LIVE row, so an edit shows at once",
          rows and rows[0].get("title") == "Parity Title", [r.get("title") for r in rows])

    # A decision arms the Session block and the timeline's Bandwidths.
    decided = Client(port, "parity-decided", **{"X-Plex-Session-Identifier": "parity-sess"})
    decided.json("GET", "/video/:/transcode/universal/decision?path=%2Flibrary%2Fmetadata%2F1014"
                        "&mediaIndex=0&partIndex=0&protocol=http&directPlay=1&directStream=1"
                        "&hasMDE=1")
    status, _, body = timeline(decided, "1014", "playing", 30000)
    bandwidth = ((container(body).get("Bandwidths") or [{}])[0].get("Bandwidth") or [{}])[0]
    check("a decided session's timeline Bandwidth is {time: 0, bandwidth, resolution}",
          list(bandwidth) == ["time", "bandwidth", "resolution"] and bandwidth.get("time") == 0
          and bandwidth.get("resolution") == "1080p" and isinstance(bandwidth.get("bandwidth"), int),
          bandwidth)
    check("a timeline answers size first, then the five fields, then Bandwidths",
          list(container(body)) == ["size", "playbackState", "skipCount", "viewCount",
                                    "viewOffset", "Bandwidths"], list(container(body)))
    _, _, body = probe.json("GET", "/status/sessions")
    mine = [r for r in container(body).get("Metadata") or []
            if (r.get("Session") or {}).get("id") == "parity-sess"]
    session = (mine[0].get("Session") if mine else None) or {}
    check("a decided session carries Session {id, bandwidth, location}",
          set(session) == {"id", "bandwidth", "location"} and session.get("location") == "lan",
          session)
    check("the next session takes the next key, 4",
          mine and mine[0].get("sessionKey") == "4", [r.get("sessionKey") for r in mine])

    status, headers, raw = web.raw(
        "GET", "/status/sessions/terminate?sessionId=parity-sess&reason=parity")
    check("terminate answers an empty 200 typed text/html",
          status == 200 and header(headers, "Content-Type") == "text/html",
          (status, header(headers, "Content-Type")))
    _, _, body = timeline(decided, "1014", "playing", 31000)
    text = container(body).get("terminationText")
    check("the kill is delivered through the next timeline in the owner's sentence",
          container(body).get("terminationCode") == 2006 and isinstance(text, str)
          and text.endswith(" asked to stop your playback from this server: parity")
          and text.startswith('"'), container(body))
    _, _, body = timeline(decided, "1014", "stopped", 32000)
    check("and it stays armed through the stopping timeline",
          container(body).get("terminationCode") == 2006, container(body))
    timeline(probe, "1007", "stopped", 21000)

    # --- statistics buckets are local calendar buckets ----------------------------------
    local = datetime.now(ZoneInfo(ZONE))
    year = int(local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
               .timestamp())
    day = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    hour = int(local.replace(minute=0, second=0, microsecond=0).timestamp())
    buckets = {}
    for span in (0, 3, 4):
        _, _, body = web.json("GET", "/statistics/media?timespan=%d" % span)
        buckets[span] = sorted({row.get("at") for row in
                                container(body).get("StatisticsMedia") or []})
    check("media statistics bucket a play at the local year, day and hour",
          buckets == {0: [year], 3: [day], 4: [hour]}, (buckets, year, day, hour))
    _, _, body = web.json("GET", "/statistics/bandwidth?timespan=0")
    years = sorted({row.get("at") for row in container(body).get("StatisticsBandwidth") or []})
    check("bandwidth statistics roll the year up at local January 1st", years == [year],
          (years, year))

    # --- the device table and the byte counters --------------------------------------
    status, _, body = web.json("GET", "/devices")
    devices = container(body).get("Device") or []
    check("/devices lists the clients seen, id order, the anonymous one first, no server row",
          status == 200 and devices and devices[0].get("id") == 1
          and devices[0].get("clientIdentifier") == ""
          and [row.get("id") for row in devices] == list(range(1, len(devices) + 1))
          and all(list(row) == ["id", "name", "platform", "clientIdentifier", "createdAt"]
                  for row in devices)
          and MACHINE not in [row.get("clientIdentifier") for row in devices],
          devices)
    named = [row for row in devices if row.get("clientIdentifier") == "parity-probe"]
    check("a device's name and platform are what its requests carried",
          named and named[0].get("name") == "Parity" and named[0].get("platform") == "Chrome",
          named)
    status, _, body = web.json("GET", "/statistics/bandwidth?timespan=6")
    stats = container(body)
    series = stats.get("StatisticsBandwidth") or []
    check("bandwidth statistics answer size, Device, Account, StatisticsBandwidth in order",
          status == 200 and list(stats) == ["size", "Device", "Account", "StatisticsBandwidth"]
          and stats.get("size") == len(series) + len(stats.get("Device") or []) + 1,
          list(stats))
    check("each bandwidth row is one device's LAN bytes for one second",
          series and all(list(row) == ["accountID", "deviceID", "timespan", "at", "lan", "bytes"]
                         and row["lan"] is True and row["timespan"] == 6 and row["bytes"] > 0
                         for row in series), series[:3])
    ids = {row.get("clientIdentifier"): row.get("id") for row in stats.get("Device") or []}
    mine = ids.get("parity-c1")
    check("the bytes are counted against the device that asked",
          mine is not None and any(row.get("deviceID") == mine for row in series)
          and len({row.get("deviceID") for row in series}) > 1,
          sorted({row.get("deviceID") for row in series}))
    account = (stats.get("Account") or [{}])[0]
    check("the bandwidth Account row is the owner's full account row",
          account.get("key") == "/accounts/1" and account.get("id") == 1
          and "name" in account and "title" not in account, account)
    status, _, body = web.json("GET", "/statistics/bandwidth?timespan=6&deviceID=%s" % mine)
    check("deviceID= keeps only that device's rows",
          container(body).get("StatisticsBandwidth")
          and all(row.get("deviceID") == mine
                  for row in container(body).get("StatisticsBandwidth") or []),
          container(body).get("StatisticsBandwidth"))
    _, _, body = web.json("GET", "/statistics/bandwidth?timespan=6&accountID=999")
    check("an account with no rows answers size 0 alone",
          body == {"MediaContainer": {"size": 0}}, body)

    Client(port, "parity-numeric", **{"X-Plex-Platform": "0"}).json("GET", "/identity")
    status, _, body = web.json("GET", "/devices")
    check("a numeric platform makes /devices the measured 500", status == 500, status)
    status, _, body = web.json("GET", "/devices/1")
    device = (container(body).get("Device") or [{}])[0]
    check("/devices/{id} carries the devices identifier and the oracle's row shape",
          status == 200 and container(body).get("identifier") == "com.plexapp.system.devices"
          and list(device) == ["id", "name", "platform", "clientIdentifier", "createdAt"]
          and device.get("id") == 1 and device.get("clientIdentifier") == "",
          (status, container(body)))
    numeric = None
    for number in range(2, 12):
        _, _, body = web.json("GET", "/devices/%d" % number)
        rows = container(body).get("Device") or []
        if rows and rows[0].get("clientIdentifier") == "parity-numeric":
            numeric = rows[0]
    check("a numeric platform is stored as the integer it spells",
          numeric is not None and numeric.get("platform") == 0
          and not isinstance(numeric.get("platform"), (float, bool)), numeric)

    status, _, body = web.json("GET", "/statistics/bandwidth?timespan=6")
    check("bandwidth statistics are 500 while a numeric platform is stored", status == 500,
          status)

    # --- the download queue ------------------------------------------------------------
    status, _, body = web.json("POST", "/downloadQueue")
    check("POST /downloadQueue answers {size 1, DownloadQueue [{id, owner, clientIdentifier,"
          " itemCount, status}]}",
          status == 200 and body == {"MediaContainer": {"size": 1, "DownloadQueue": [
              {"id": 1, "owner": 1, "clientIdentifier": "parity-c1", "itemCount": 0,
               "status": "done"}]}}, body)
    status, _, body = Client(port, "parity-other").json("GET", "/downloadQueue/1")
    check("any client reads a download queue back by its number",
          status == 200 and container(body).get("size") == 1, (status, body))

    # --- decisions -----------------------------------------------------------------------
    _, _, body = web.json("GET", "/video/:/transcode/universal/decision?path=%2Flibrary"
                                 "%2Fmetadata%2F1007&mediaIndex=0&partIndex=0&protocol=dash"
                                 "&directPlay=0&directStream=1&hasMDE=1")
    check("a refused decision names the protocol it was asked for",
          container(body).get("transcodeDecisionText")
          == "Cannot convert this item. No conversion profile found for protocol dash.",
          container(body).get("transcodeDecisionText"))
    _, _, body = web.json("GET", "/video/:/transcode/universal/decision?path=%2Flibrary"
                                 "%2Fmetadata%2F1007&mediaIndex=0&partIndex=5&protocol=dash"
                                 "&directPlay=1&directStream=1&hasMDE=1")
    media = ((container(body).get("Metadata") or [{}])[0].get("Media") or [{}])[0]
    check("an out-of-range partIndex decides the first part",
          len(media.get("Part") or []) == 1, media.get("Part"))
    video = [stream for part in media.get("Part") or [] for stream in part.get("Stream") or []
             if stream.get("streamType") == 1]
    check("a decision's video stream keeps its full extendedDisplayTitle",
          video and video[0].get("extendedDisplayTitle") == "1080p (VP8)",
          [stream.get("extendedDisplayTitle") for stream in video])

    # --- history --------------------------------------------------------------------------
    _, _, body = web.json("GET", "/status/sessions/history/all?sort=viewedAt%3Adesc")
    rows = container(body).get("Metadata") or []
    key = rows[0].get("historyKey") if rows else None
    status, _, body = web.json("GET", key or "/status/sessions/history/1")
    check("one history row reads back in a container carrying only size",
          status == 200 and list(container(body)) == ["size", "Metadata"], container(body))

    # --- the doubled trailing slash --------------------------------------------------------
    _, _, doubled = web.json("GET", "/status/sessions//")
    _, _, single = web.json("GET", "/status/sessions")
    check("/status/sessions// answers the session listing itself",
          doubled == single and "content" not in container(doubled), doubled)
    status, _, doubled = web.json("GET", "/butler//")
    check("/butler// answers the ButlerTasks document", "ButlerTasks" in (doubled or {}),
          list(doubled or {}))

    # --- the unknown verb ------------------------------------------------------------------
    status, headers, _ = web.raw("PATCH", "/status/sessions")
    check("a PATCH is a 500 that honours the request's Connection: close",
          status == 500 and header(headers, "Connection") == "close"
          and header(headers, "Keep-Alive") is None,
          (status, header(headers, "Connection"), header(headers, "Keep-Alive")))

    # --- a part whose provider link cannot be resolved ----------------------------------
    status, headers, raw = web.raw("GET", "/library/parts/10070/1/file.webm")
    check("a part the provider refuses is the 502 document naming Bad Gateway",
          status == 502 and raw == b"<html><head><title>Bad Gateway</title></head><body>"
                                   b"<h1>502 Bad Gateway</h1></body></html>", (status, raw[:120]))

    # --- the platform header wins over the query ---------------------------------------
    roku = Client(port, "parity-roku")
    timeline(roku, "1009", "playing", 4000,
             "&X-Plex-Client-Identifier=parity-roku&X-Plex-Platform=Roku&X-Plex-Device=Roku")
    _, _, body = web.json("GET", "/status/sessions")
    players = [r.get("Player") or {} for r in container(body).get("Metadata") or []
               if (r.get("Player") or {}).get("machineIdentifier") == "parity-roku"]
    check("a Player's platform is the X-Plex-Platform header when both are sent",
          players and players[0].get("platform") == "Chrome"
          and players[0].get("device") == "Roku", players)
    timeline(roku, "1009", "stopped", 4000)

    # --- a playlist scrobble is its own history row ----------------------------------------
    status, _, body = web.json(
        "POST", "/playlists?type=video&title=ParityList&smart=0&uri=%s"
        % queue_uri("/library/metadata/1007"))
    playlist = str((container(body).get("Metadata") or [{}])[0].get("ratingKey") or "")
    web.raw("GET", "/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % playlist)
    _, _, body = web.json("GET", "/status/sessions/history/all?type=1&metadataItemID=%s"
                                 % playlist)
    rows = container(body).get("Metadata") or []
    check("scrobbling a playlist writes it a history row with no type and no section",
          len(rows) == 1 and rows[0].get("ratingKey") == playlist
          and rows[0].get("title") == "ParityList" and rows[0].get("librarySectionID") == ""
          and "type" not in rows[0], rows)

    # --- an uploaded sidecar ---------------------------------------------------------------
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    subtitle = b"1\n00:00:00,000 --> 00:00:02,000\nparity\n\n"
    conn.request("POST", "/library/metadata/1014/subtitles?title=paritysub.srt&format=srt",
                 body=subtitle, headers=dict(web.headers, **{"Content-Type": "text/plain"}))
    conn.getresponse().read()
    conn.close()
    _, _, body = web.json("GET", "/library/metadata/1014")
    keys = [stream.get("key") for media in ((container(body).get("Metadata") or [{}])[0]
                                             .get("Media") or [])
            for part in media.get("Part") or [] for stream in part.get("Stream") or []
            if stream.get("title") == "paritysub"]
    status, headers, raw = web.raw("GET", keys[0]) if keys else (None, {}, b"")
    check("an uploaded sidecar's bytes are served as text/html",
          status == 200 and raw == subtitle and header(headers, "Content-Type") == "text/html",
          (keys, status, header(headers, "Content-Type")))
    status, headers, _ = web.raw("DELETE", keys[0]) if keys else (None, {}, b"")
    check("deleting an uploaded sidecar answers an empty 200 typed text/html",
          status == 200 and header(headers, "Content-Type") == "text/html",
          (status, header(headers, "Content-Type")))

    # --- a session outlives its row ----------------------------------------------------------
    gone = Client(port, "parity-gone")
    timeline(gone, "1013", "playing", 6000)
    web.raw("DELETE", "/library/metadata/1013")
    status, _, body = web.json("GET", "/library/metadata/1013")
    _, _, listing = web.json("GET", "/status/sessions")
    keys = [r.get("ratingKey") for r in container(listing).get("Metadata") or []]
    check("a session on a row deleted since still lists it",
          status == 404 and "1013" in keys, (status, keys))

    # --- resources --------------------------------------------------------------------------
    samples = []
    for _ in range(3):
        _, _, body = web.json("GET", "/statistics/resources?timespan=6")
        samples.append(container(body).get("StatisticsResources") or [])
        time.sleep(1.1)
    last_series = samples[-1]
    check("resource statistics grow a per-second series across polls",
          len(samples[0]) >= 1 and len(last_series) > len(samples[0]),
          [len(sample) for sample in samples])
    fields = ["timespan", "at", "hostCpuUtilization", "processCpuUtilization",
              "hostMemoryUtilization", "processMemoryUtilization"]
    check("each sample carries real CPU numbers and honest 0.0 memory",
          last_series and all(list(sample) == fields for sample in last_series)
          and any(isinstance(sample.get("hostCpuUtilization"), float)
                  and sample.get("hostCpuUtilization") > 0 for sample in last_series)
          and all(isinstance(sample.get("processCpuUtilization"), float) for sample in last_series)
          and all(sample.get("hostMemoryUtilization") == 0.0
                  and isinstance(sample.get("hostMemoryUtilization"), float)
                  for sample in last_series), last_series[-2:])


if __name__ == "__main__":
    sys.exit(main())
