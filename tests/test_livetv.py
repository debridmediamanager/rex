#!/usr/bin/env python3
"""Live TV: a source's live channels are one DVR, answered as a real PMS with a DVR answers the
Plex clients' guide, pages, search and tune, and watched through rex's own live conversion.

The reference is plexav, a claimed 1.43.4 PMS in Europe/Amsterdam with an HDHomeRun of three
channels and an XMLTV guide, owned by an account without a Plex Pass (so no recording). Its
answers are `tests/fixtures/livetv/` (`probes/record_livetv.py`), each set recorded at one moment
(`now`): rex runs with its guide clock there (`REX_LIVETV_NOW`), TZ=Europe/Amsterdam, and the
recorded guide pushed by a source of three real live HLS channels (`tests/livetv_source.py`).

    tunerless  a live channel pushed without `channel` leaves every Live TV answer as it was; with
               one it is a DVR, and `channel: null` ends that
    reads      Plex Web's and Plex for Android's guide, What's On, show page, search and the DVR's
               own reads (`reads_s1434`), value for value
    web        Plex Web 4.163.0 tunes 2.1 and plays it (`web_watch_s1434`): the tune, the decision
               (live, the session's key), start.m3u8, segments that decode, its beats, Now Playing,
               and its stop
    android    Plex for Android 2026.21.0 does the same from its Live TV tab (`android_watch_s1434`)
    session    a film tuned and its session read back (`tune_s1434`)
    browse     what no client recording holds (`browse_s1434`): the provider's search hubs and
               category hits, its sections and Guide page, a season's episodes, the DVR's device,
               and the `livetv` owner feature
    tune2      one tune as Plex Web sends it, without a Plex Pass (`tune2_s1434`): its headers, its
               activities, the session and its Part key's 404
    listings   the provider's `/sections/<n>/all` and What's On keys, filtered, sorted and paged, on
               guides written for plexav and on its own (`listing_*_s1434`)

    python3 tests/test_livetv.py      # needs ffmpeg (REX_TRANSCODER, else PATH's)
"""
import gzip
import http.client
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from livetv_source import LiveSource, push  # noqa: E402
from server_command import server_command, test_ffmpeg  # noqa: E402
from startup_wait import poll_until  # noqa: E402
from test_ports import free_port  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "livetv")
FFMPEG = test_ffmpeg()
FFPROBE = shutil.which("ffprobe") if FFMPEG else None
if FFMPEG and os.path.exists(os.path.join(os.path.dirname(FFMPEG), "ffprobe")):
    FFPROBE = os.path.join(os.path.dirname(FFMPEG), "ffprobe")
PREFIX = "/tv.plex.providers.epg.xmltv:2"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A database's own numbers and clocks: an airing's, a tag's or a session item's id, when PMS
# read its guide, and the server's name.
VOLATILE = {"id", "addedAt", "createdAt", "refreshedAt", "lastSeenAt", "updatedAt", "machineIdentifier", "uuid",
            "mediaTagVersion", "friendlyName", "timeStamp", "hubKey", "mediaSubscriptionID", "sessionKey",
            "contentChangedAt"}

DVR_OWN = {"lineupTitle", "lineup", "uri", "deviceAuth", "deviceId", "make", "model", "modelNumber", "title", "tuners", "summary"}

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-70s %s" % ("PASS" if ok else "FAIL", label, detail if not ok else ""))
    return ok


def fixture(name):
    path = os.path.join(FIXTURES, name)
    return json.load(gzip.open(path, "rt") if name.endswith(".gz") else open(path))


# The header lines of the last answer `request` read, in order, repeats kept.
LAST = {"headers": []}


def request(port, method, path, query=(), headers=(), timeout=60):
    target = path + ("?" + urllib.parse.urlencode([tuple(pair) for pair in query]) if query else "")
    # http.client, not urllib: urllib sends `Connection: close`, which no Plex client does, and
    # the answer's connection headers are among those compared.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, target, headers=dict(headers))
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    LAST["headers"] = list(response.getheaders())
    if response.getheader("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return response.status, response.getheader("Content-Type", ""), body


# Header values that are the moment's or the body's own; the rest must be the reference's.
MOMENTARY = {"Date", "Content-Length", "X-Plex-Content-Original-Length", "X-Plex-Content-Compressed-Length",
             "X-Plex-Container-Total-Size", "X-Plex-Activity"}


def header_check(label, exchange):
    """The last answer's header lines against the reference's, in order: every name, and every
    value but the moment's (an activity's must be a uuid)."""
    if not exchange.get("answered"):
        return True
    got = [(name, value) for name, value in LAST["headers"] if name != "Date"]
    wanted = [(name, value) for name, value in exchange["answered"] if name != "Date"]
    differences = []
    if [name for name, _ in got] != [name for name, _ in wanted]:
        differences.append("names %s, reference %s" % ([name for name, _ in got], [name for name, _ in wanted]))
    for (name, value), (_, reference) in zip(got, wanted):
        if name == "X-Plex-Activity" and not UUID.fullmatch(value):
            differences.append("X-Plex-Activity %r is no uuid" % value)
        elif name not in MOMENTARY and value != reference:
            differences.append("%s: %r, reference %r" % (name, value, reference))
    return check("%s: headers" % label, not differences, "; ".join(differences)[:400])


def tree(element):
    """An XML element as the JSON PMS answers it: attributes, children by name."""
    node = dict(element.attrib)
    for child in element:
        node.setdefault(child.tag, []).append(tree(child))
    return node


def document(body, content_type):
    text = body.decode() if isinstance(body, bytes) else body
    if "xml" in content_type:
        root = ET.fromstring(text)
        return {root.tag: tree(root)}
    return json.loads(text)


def normal(value):
    return UUID.sub("<uuid>", value) if isinstance(value, str) else value


def diff(reference, got, path="", out=None):
    """Every element and attribute the reference has that rex does not, or the other way, and
    every value that differs, but the volatile ones'."""
    out = [] if out is None else out
    if isinstance(reference, dict) and isinstance(got, dict):
        for name in reference:
            if name not in got:
                out.append("%s.%s missing (%s)" % (path, name, json.dumps(reference[name])[:60]))
        for name in got:
            if name not in reference:
                out.append("%s.%s extra (%s)" % (path, name, json.dumps(got[name])[:60]))
        for name in reference:
            if name not in got or name == "TranscodeSession":
                continue
            left, right = reference[name], got[name]
            if isinstance(left, dict) and isinstance(right, list) and len(right) == 1:
                right = right[0]
            if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
                diff(left, right, "%s.%s" % (path, name), out)
            elif name in VOLATILE or (name == "filter" and str(left).startswith("genre=")):
                continue
            elif name == "key" and "?genre=" in str(left) and str(left).split("?genre=")[0] == str(right).split("?genre=")[0]:
                continue  # a tag's id is its database's
            elif name in ("ratingKey", "key") and str(left).isdigit():
                continue  # PMS mints an item per session or subscription; rex's is the channel's row
            elif normal(left) != normal(right):
                out.append("%s.%s = %s, reference %s" % (path, name, json.dumps(right)[:70], json.dumps(left)[:70]))
    elif isinstance(reference, list) and isinstance(got, list):
        if len(reference) != len(got):
            out.append("%s: %d rows, reference %d" % (path, len(got), len(reference)))
        for at, (left, right) in enumerate(zip(reference, got)):
            diff(left, right, "%s[%d]" % (path, at), out)
    elif normal(reference) != normal(got):
        out.append("%s = %s, reference %s" % (path, json.dumps(got)[:70], json.dumps(reference)[:70]))
    return out


def start_rex(scratch, now, transcoder=False):
    port = free_port()
    catalog = os.path.join(scratch, "catalog-%d.json" % port)
    json.dump({"machineIdentifier": "0000000000000000000000000000000000000017", "friendlyName": "rex-livetv",
               "sections": [{"key": "1", "type": "movie", "title": "Movies", "uuid": "0" * 39 + "1"}], "items": []},
              open(catalog, "w"))
    env = dict(os.environ, TZ="Europe/Amsterdam", REX_TRANSCODER=FFMPEG if transcoder else "off",
               REX_TRANSCODE_HW="none", REX_TRANSCODE_DIR=os.path.join(scratch, "segments-%d" % port),
               REX_PLEXTV_URL="http://127.0.0.1:%d" % free_port())
    if now:
        env["REX_LIVETV_NOW"] = str(now)
    log = open(os.path.join(scratch, "rex-%d.log" % port), "w")
    process = subprocess.Popen(server_command("--port", port, "--catalog", catalog, "--assert-claimed", root=ROOT),
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)

    def ready():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2)
            return True
        except Exception:
            return False

    if not poll_until(ready, 40):
        raise RuntimeError("rex did not start: %s" % log.name)
    return port, process


def push_guide(port, source, guide):
    items = [source.item(channel["number"], channel["title"], channel) for channel in guide["channels"]]
    answer = push("http://127.0.0.1:%d" % port, source, items)
    return answer


def provider(body):
    providers = json.loads(body)["MediaContainer"].get("MediaProvider", [])
    return next((entry for entry in providers if entry.get("protocols") == "livetv"), None)


def replay(port, exchange, query=None):
    headers = [(name, value) for name, value in exchange["headers"]]
    # A recording made with urllib asked `Connection: close`, which its answer's headers show.
    if ["Connection", "close"] in exchange.get("answered", []):
        headers.append(("Connection", "close"))
    return request(port, exchange["method"], exchange["path"], exchange["query"] if query is None else query, headers)


def compare(label, port, exchange, tolerated=()):
    """A recorded exchange replayed: rex's status, its body against the reference's, and the
    header lines where the recording kept them."""
    status, content_type, body = replay(port, exchange)
    if status != exchange["status"]:
        return check(label, False, "status %s, reference %s" % (status, exchange["status"]))
    header_check(label, exchange)
    if status != 200 or exchange["body"] is None:
        return check(label, True)
    if exchange["path"] == "/media/providers":
        reference, got = json.loads(exchange["body"]), json.loads(body)
        differences = diff(provider(exchange["body"]), provider(body), "livetv")
        for name in ("size", "allowTuners"):
            if reference["MediaContainer"][name] != got["MediaContainer"][name]:
                differences.append("%s = %s, reference %s" % (name, got["MediaContainer"][name], reference["MediaContainer"][name]))
        return check(label, not differences, "; ".join(differences[:6]))
    reference, got = document(exchange["body"], exchange["type"]), document(body, content_type)
    hubs = reference.get("MediaContainer", {}).get("Hub", [])
    if exchange["path"].endswith("/hubs/search") and sum(1 for hub in hubs if hub.get("size")) > 1:
        # plexav orders the hubs of one search by PMS's unstable sort, which rex runs on the
        # library's hub order and ranks: right for a search with hits in one hub (`firefly`,
        # `rex`), not for `news`, whose Movies and Genres hits it ordered unlike the library (its
        # input order then is unknown). The hubs are compared by type.
        for side in (reference, got):
            side.get("MediaContainer", {}).get("Hub", []).sort(key=lambda hub: hub.get("hubIdentifier", ""))
    differences = diff(reference, got)
    differences = [line for line in differences if line.split(" ")[0].rsplit(".", 1)[-1] not in tolerated]
    if exchange["path"].startswith(("/livetv/dvrs", "/media/grabbers/devices")):
        # The device is rex's sources, not an HDHomeRun: its names, and the settings' help text,
        # are rex's own (CLAUDE.md: whole-sentence help text is rex's wording).
        differences = [line for line in differences if line.split(" ")[0].rsplit(".", 1)[-1] not in DVR_OWN]
    return check(label, not differences, "%d: %s" % (len(differences), "; ".join(differences[:6])))


def label_of(exchange):
    query = "&".join("%s=%s" % tuple(pair) for pair in exchange["query"] if not pair[0].startswith("X-Plex"))
    return ("%s %s?%s" % (exchange["method"], exchange["path"].replace(PREFIX, "<epg>"), query))[:68]


def tunerless(scratch, source, guide):
    print("tunerless")
    port, process = start_rex(scratch, None)
    try:
        first = guide["channels"][0]
        before = {path: request(port, "GET", path, headers=[("Accept", "application/json")]) for path in
                  ("/livetv", "/livetv/dvrs", "/livetv/sessions", "/livetv/epg", "/livetv/sessions/x", "/media/subscriptions",
                   PREFIX + "/lineups/dvr/channels", "/media/grabbers")}
        answer = push("http://127.0.0.1:%d" % port, source, [source.item(first["number"], first["title"], None)])
        status, _, body = request(port, "GET", "/media/providers", headers=[("Accept", "application/json")])
        container = json.loads(body)["MediaContainer"]
        check("a live channel without `channel`: one provider, allowTuners false, no `livetv` owner feature",
              container["size"] == 1 and provider(body) is None and container["allowTuners"] is False
              and "livetv" not in container["ownerFeatures"].split(","),
              json.dumps({"size": container["size"], "allowTuners": container["allowTuners"]}))
        after = {path: request(port, "GET", path, headers=[("Accept", "application/json")]) for path in before}
        for path in before:
            check("unchanged without a channel: %s" % path, before[path][:1] + before[path][2:] == after[path][:1] + after[path][2:],
                  "%s %s" % (after[path][0], after[path][2][:80]))
        status, _, _ = request(port, "POST", "/livetv/dvrs/2/channels/2.1/tune")
        check("unchanged without a channel: POST tune", status == 404, str(status))
        answer = push_guide(port, source, {"channels": [first]})
        status, _, body = request(port, "GET", "/media/providers", headers=[("Accept", "application/json")])
        check("`channel` makes it a DVR: the Live TV provider, allowTuners true",
              provider(body) is not None and json.loads(body)["MediaContainer"]["allowTuners"] is True, json.dumps(answer))
        status, _, body = request(port, "GET", PREFIX + "/lineups/dvr/channels", headers=[("Accept", "application/json")])
        check("its lineup lists the channel", status == 200 and json.loads(body)["MediaContainer"]["size"] == 1, body[:120])
        item = source.item(first["number"], first["title"], None)
        item["channel"] = None
        answer = push("http://127.0.0.1:%d" % port, source, [item])
        status, _, body = request(port, "GET", "/media/providers", headers=[("Accept", "application/json")])
        check("`channel: null` ends it", provider(body) is None and json.loads(body)["MediaContainer"]["allowTuners"] is False, json.dumps(answer))
        rejected = push("http://127.0.0.1:%d" % port, source, [dict(source.item(first["number"], first["title"], {"number": "2/1"}))])
        check("a bad channel is refused by name", rejected.get("rejected") and "channel.number" in rejected["rejected"][0]["reason"], json.dumps(rejected))
    finally:
        process.kill()
        process.wait()


def reads(scratch, source, guide):
    recorded = fixture("reads_s1434.json.gz")
    print("reads (the reference at %d)" % recorded["now"])
    port, process = start_rex(scratch, recorded["now"])
    try:
        print("  pushed %s" % json.dumps(push_guide(port, source, guide)))
        seen = set()
        for exchange in recorded["exchanges"]:
            name = (exchange["path"], json.dumps(exchange["query"]))
            if name in seen:
                continue
            seen.add(name)
            compare(label_of(exchange), port, exchange)
        # Plex Web's own reads of the guide, pages and search, for their header lines.
        for exchange in fixture("headers_s1434.json.gz")["exchanges"]:
            if exchange["path"] not in ("/status/sessions", "/:/timeline"):
                compare("Plex Web: " + label_of(exchange), port, exchange)
    finally:
        process.kill()
        process.wait()


def decodes(port, base, scratch, label, count=3):
    """The session's media playlist and its first segments, decoded by FFmpeg."""
    deadline, names = time.time() + 40, []
    while time.time() < deadline and len(names) < count:
        status, _, body = request(port, "GET", base + "index.m3u8")
        names = [line for line in body.decode().splitlines() if line and not line.startswith("#")] if status == 200 else []
        time.sleep(1)
    check("%s: the media playlist grows, no ENDLIST" % label, len(names) >= count and b"#EXT-X-ENDLIST" not in body, body[-200:])
    path = os.path.join(scratch, "%s.ts" % label.replace(" ", "-"))
    statuses = []
    with open(path, "wb") as handle:
        for name in names[:count]:
            status, _, segment = request(port, "GET", base + name.split("?")[0])
            statuses.append(status)
            handle.write(segment)
    frames = (subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                              "stream=nb_read_frames", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split() or [""])[0]
    check("%s: segments %s decode (%s frames)" % (label, statuses, frames), statuses == [200] * count and frames.isdigit() and int(frames) > 30, frames)


def tune(port, exchange, label, tolerated=()):
    """The tune replayed and compared; rex's session uuid and its ratingKey."""
    status, content_type, body = replay(port, exchange)
    differences = diff(document(exchange["body"], exchange["type"]), document(body, content_type)) if status == 200 else ["status %d" % status]
    differences = [line for line in differences if line.split(" ")[0].rsplit(".", 1)[-1] not in tolerated]
    check("%s: the tune" % label, not differences, "%d: %s" % (len(differences), "; ".join(differences[:8])))
    # Plex Web refuses a tune whose answer names no activity: "Could not tune channel (No
    # activity)" (Plex Web 4.160/4.163 against rex, 2026-10-31). Every recorded tune has one.
    activity = dict(LAST["headers"]).get("X-Plex-Activity")
    check("%s: the tune names its activity, exposed to a browser" % label,
          activity and ("Access-Control-Expose-Headers", "X-Plex-Activity") in LAST["headers"], str(LAST["headers"]))
    if exchange.get("answered"):
        header_check("%s: the tune" % label, exchange)
    key = re.search(r"/livetv/sessions/([0-9a-f-]{36})", body.decode())
    rating = re.search(r'ratingKey"?[=:] ?"(\d+)"', body.decode())
    if activity and key:
        # Measured (plexav's /activities after Plex Web's tune): the session's grabber.grab.
        _, _, listed = request(port, "GET", "/activities", headers=[("Accept", "application/json")])
        rows = json.loads(listed)["MediaContainer"].get("Activity", [])
        row = next((row for row in rows if row.get("uuid") == activity), {})
        check("%s: /activities lists the session's grabber.grab" % label,
              row.get("type") == "grabber.grab" and row.get("subtitle") == "Live TV - Session"
              and row.get("Context", {}).get("transcode_session") == key.group(1), json.dumps(rows)[:300])
    return (key.group(1) if key else None), (rating.group(1) if rating else None)


# What a capture recorded that the reference no longer answers, by the attribute's name: Plex Web's
# and Android's sessions ran on an account with a Plex Pass and a recording of Firefly scheduled,
# and Plex Web's tune was the first after the DVR was set up. plexav answered them since without
# the Pass (`tune_s1434`, `reads_s1434`: no `subscribe` and two pivots, no `canNormalizeLoudness`)
# and later tunes describe the capture's picture (`width`, `height`, `aspectRatio`), its
# `channelCallSign` and the Part's `size`, as Android's did; rex records nothing.
TOLERATED = {
    "Plex Web": {"width", "height", "aspectRatio", "pixelAspectRatio", "channelCallSign", "size", "canNormalizeLoudness", "hasVoiceActivity"},
    "Plex for Android": {"grandparentSubscriptionID", "grandparentSubscriptionType", "canNormalizeLoudness", "hasVoiceActivity"},
}


def watch(scratch, source, guide, name, label):
    recorded = fixture(name)
    print("%s (the reference at %d)" % (label, recorded["now"]))
    port, process = start_rex(scratch, recorded["now"], transcoder=True)
    try:
        push_guide(port, source, guide)
        exchanges = recorded["exchanges"]
        at = next(index for index, exchange in enumerate(exchanges) if exchange["path"].endswith("/tune"))
        for exchange in exchanges[:at]:
            # The providers list is the Plex Pass one then (`TOLERATED`); `reads` checks today's
            # body, this its header lines.
            if exchange["path"] == "/media/providers":
                replay(port, exchange)
                header_check("%s: %s" % (label, label_of(exchange)), exchange)
            elif exchange["path"] not in ("/status/sessions", "/:/timeline"):
                compare("%s: %s" % (label, label_of(exchange)), port, exchange, TOLERATED[label])
        uuid, rating = tune(port, exchanges[at], label, TOLERATED[label])
        if not check("%s: a session key" % label, uuid and rating):
            return
        recorded_uuid = UUID.search(exchanges[at]["body"]).group(0)
        recorded_rating = re.search(r'ratingKey"?[=:] ?"(\d+)"', exchanges[at]["body"]).group(1)
        swap = lambda pairs: [[name, rating if name == "ratingKey" and value == recorded_rating else value.replace(recorded_uuid, uuid)]
                              for name, value in pairs]
        session = None
        for exchange in exchanges[at + 1:]:
            query = swap(exchange["query"])
            path = exchange["path"]
            if path == "/video/:/transcode/universal/decision":
                status, content_type, body = replay(port, exchange, query)
                header_check("%s: the decision" % label, exchange)
                reference, got = document(exchange["body"], exchange["type"]), document(body, content_type) if status == 200 else {}
                container = got.get("MediaContainer", {})
                video = (container.get("Video") or [{}])[0]
                wanted = {name: value for name, value in reference["MediaContainer"].items() if not isinstance(value, list)}
                differences = diff(wanted, {name: value for name, value in container.items() if not isinstance(value, list)}, "container")
                differences += diff({name: value for name, value in reference["MediaContainer"]["Video"][0].items() if not isinstance(value, list)},
                                    {name: value for name, value in video.items() if not isinstance(value, list)}, "Video")
                media = (video.get("Media") or [{}])[0]
                if media.get("origin") != "livetv" or media.get("uuid") != uuid:
                    differences.append("Media origin/uuid %s %s" % (media.get("origin"), media.get("uuid")))
                check("%s: the decision is a live conversion of the session" % label, status == 200 and not differences,
                      "%d: %s" % (len(differences), "; ".join(differences[:6])))
            elif path == "/video/:/transcode/universal/start.m3u8":
                status, _, body = replay(port, exchange, query)
                header_check("%s: start.m3u8" % label, exchange)
                session = dict(query).get("session")
                lines = body.decode().splitlines() if status == 200 else []
                shape = [line.split(":")[0] if line.startswith("#") else line.split("?")[0] for line in lines]
                reference = [line.split(":")[0] if line.startswith("#") else line.split("?")[0] for line in exchange["body"].splitlines()]
                check("%s: start.m3u8 names the session's playlist" % label, status == 200 and shape == reference, "%s %s" % (status, lines))
                decodes(port, "/video/:/transcode/universal/session/%s/base/" % session, scratch, label)
            elif path == "/:/timeline":
                # The reference answered `complete` to Android's beats at its live edge once the
                # one before was under 90% (17448 of 17448 ms after 7427 of 7427), and `ignore` to
                # Plex Web's 8000 of 8000 after beats at 0: a library item's rule.
                status, content_type, body = replay(port, exchange, query)
                state = json.loads(body)["MediaContainer"].get("playbackState") if status == 200 else None
                reference = json.loads(exchange["body"])["MediaContainer"]["playbackState"]
                check("%s: a beat at %s of %s ms is %r, as the reference's" % (label, dict(query).get("time"), dict(query).get("duration"), reference),
                      state == reference, "%s %s" % (status, body[:120]))
            elif path == "/status/sessions" and json.loads(exchange["body"])["MediaContainer"]["size"]:
                status, content_type, body = replay(port, exchange, query)
                reference = json.loads(exchange["body"])["MediaContainer"]["Metadata"][0]
                rows = json.loads(body)["MediaContainer"].get("Metadata", []) if status == 200 else []
                keep = lambda row: {name: value for name, value in row.items() if not isinstance(value, (list, dict))}
                differences = diff(keep(reference), keep(rows[0])) if rows else ["no session"]
                check("%s: Now Playing shows the live session" % label, not differences, "; ".join(differences[:6]))
                break
        # The channel keeps no resume point and is never watched; a stop ends the session.
        key = re.search(r'ratingKey="?(\d+)', request(port, "GET", "/livetv/sessions/" + uuid, headers=[("Accept", "application/xml")])[2].decode())
        stop = [("key", "/livetv/sessions/" + uuid), ("ratingKey", key.group(1) if key else ""), ("state", "stopped"),
                ("time", "60000"), ("duration", "60000"), ("X-Plex-Client-Identifier", "rex-livetv-test")]
        status, _, _ = request(port, "GET", "/:/timeline", stop, [("Accept", "application/json")])
        status_after, _, _ = request(port, "GET", "/livetv/sessions/" + uuid, headers=[("Accept", "application/json")])
        _, _, listed = request(port, "GET", "/activities", headers=[("Accept", "application/json")])
        check("%s: a stopped beat ends the session and its activity" % label,
              status == 200 and status_after == 404 and json.loads(listed)["MediaContainer"]["size"] == 0, "%s %s %s" % (status, status_after, listed[:120]))
        if key:
            _, _, body = request(port, "GET", "/library/metadata/" + key.group(1), headers=[("Accept", "application/json")])
            row = json.loads(body)["MediaContainer"]["Metadata"][0]
            check("%s: the channel keeps no resume point and is not watched" % label, "viewOffset" not in row and not row.get("viewCount"),
                  json.dumps({name: row.get(name) for name in ("viewOffset", "viewCount")}))
        if session:
            request(port, "GET", "/video/:/transcode/universal/stop", [("session", session)])
    finally:
        process.kill()
        process.wait()


def session(scratch, source, guide):
    recorded = fixture("tune_s1434.json")
    print("session (the reference at %d)" % recorded["now"])
    port, process = start_rex(scratch, recorded["now"])
    try:
        push_guide(port, source, guide)
        exchanges = recorded["exchanges"]
        uuid, _ = tune(port, exchanges[0], "a film")
        recorded_uuid = UUID.search(exchanges[0]["body"]).group(0)
        for exchange in exchanges[1:]:
            compare(label_of(dict(exchange, path=exchange["path"].replace(recorded_uuid, "<uuid>"))), port,
                    dict(exchange, path=exchange["path"].replace(recorded_uuid, uuid or "none")))
    finally:
        process.kill()
        process.wait()


def browse(scratch, source, guide):
    """The provider's pages no client recording holds, asked as Plex Web asks (`browse_s1434`):
    search hubs and category hits, the sections and the Guide page, a season's episodes, the
    DVR's device, and the owner features of a server with a DVR."""
    recorded = fixture("browse_s1434.json.gz")
    print("browse (the reference at %d)" % recorded["now"])
    port, process = start_rex(scratch, recorded["now"])
    try:
        push_guide(port, source, guide)
        for exchange in recorded["exchanges"]:
            if exchange["path"] == "/activities":
                continue  # the reference's own recordings' activities
            if exchange["path"] == "/":
                features = json.loads(exchange["body"])["MediaContainer"]["ownerFeatures"].split(",")
                _, _, body = request(port, "GET", "/", headers=[("Accept", "application/json")])
                own = json.loads(body)["MediaContainer"]["ownerFeatures"].split(",")
                check("a DVR names the `livetv` owner feature and not `dvr`, as plexav without a Plex Pass",
                      "livetv" in features and "dvr" not in features and "livetv" in own and "dvr" not in own, ",".join(name for name in own if not UUID.match(name)))
                continue
            compare(label_of(exchange), port, exchange)
    finally:
        process.kill()
        process.wait()


UNFIXED = {"premiere", "skipChildren", "duration"}


def listed(exchange, body, now):
    """A listing's rows in plexav's order, but each tie's rows by ratingKey and each row's Media
    by start: plexav breaks a tie, and lists a row's airings, in the order it read its XMLTV
    document, which a source's per-channel guides cannot say. Ties are rows of one key under
    the listing's sort: its first airing inside the filters (`beginsAt`, the films' and episodes'
    default), its last for `:desc`."""
    rows = body.get("MediaContainer", {}).get("Metadata", [])
    for row in rows:
        row.get("Media", []).sort(key=lambda media: (media["beginsAt"], media.get("channelID", 0)))
    query = dict(exchange["query"])
    kind = query.get("type", "2" if "/sections/2/" in exchange["path"] else "1")
    sort = query.get("sort", "titleSort" if kind == "2" else "beginsAt").split(",")[0]
    if not sort.startswith("beginsAt"):
        return body
    moment = lambda value: now if value == "now" else now + int(value[:-1]) if value.endswith("s") else int(value)
    bounds = [(name[:-1], name[-1], moment(value)) for name, value in exchange["query"] if name[-1:] in "<>"]
    def key(row):
        inside = [media["beginsAt"] for media in row.get("Media", [])
                  if all(media[field] >= at if side == ">" else media[field] <= at for field, side, at in bounds)]
        return (max if sort.endswith(":desc") else min)(inside or [0])
    if rows:
        body["MediaContainer"]["Metadata"] = [row for _, tie in itertools.groupby(rows, key) for row in sorted(tie, key=lambda row: row["ratingKey"])]
    return body


def listings(scratch, source, names=("order", "held", "plexav")):
    """The provider's listings (`listing_*_s1434`): plexav given guides written for it, whose
    document order, starts, titles and lineup disagree, and its own guide back; each answer
    replayed against the same guide pushed to rex. A search's category hit is opened by the key
    rex's own search gives it: PMS numbers its tags in the order it read them, rex in the order
    they first air."""
    for name in names:
        recorded = fixture("listing_%s_s1434.json.gz" % name)
        print("listings: %s (the reference at %d)" % (name, recorded["now"]))
        port, process = start_rex(scratch, recorded["now"])
        try:
            push_guide(port, source, recorded["guide"])
            genres = {}
            for exchange in recorded["exchanges"]:
                if exchange["path"].endswith("/library/search"):
                    _, _, body = replay(port, exchange)
                    ours = {(hit["Directory"]["tag"], hit["Directory"]["librarySectionID"]): hit["Directory"]["id"]
                            for hit in json.loads(body)["MediaContainer"].get("SearchResult", []) if "Directory" in hit}
                    for hit in json.loads(exchange["body"])["MediaContainer"].get("SearchResult", []):
                        if "Directory" in hit:
                            genres[str(hit["Directory"]["id"])] = str(ours.get((hit["Directory"]["tag"], hit["Directory"]["librarySectionID"])))
                    continue
                if exchange["path"] == PREFIX + "/all" and not exchange["query"]:
                    continue  # every item in the order PMS read them: no key rex emits opens it
                query = [[field, genres.get(value, value) if field == "genre" else value] for field, value in exchange["query"]]
                label = "%s %s" % (name, label_of(exchange))
                status, content_type, body = replay(port, exchange, query)
                if status != exchange["status"]:
                    check(label, False, "status %s, reference %s" % (status, exchange["status"]))
                    continue
                header_check(label, exchange)
                if status != 200:
                    check(label, True)
                    continue
                reference = listed(exchange, document(exchange["body"], exchange["type"]), recorded["now"])
                got = listed(exchange, document(body, content_type), recorded["now"])
                paging = {"X-Plex-Container-Start", "X-Plex-Container-Size"}
                if paging & {field for field, _ in exchange["query"] + exchange["headers"]}:
                    # A page can end inside a tie: it is checked as the window of rex's whole listing.
                    whole = dict(exchange, query=[pair for pair in query if pair[0] not in paging],
                                 headers=[pair for pair in exchange["headers"] if pair[0] not in paging])
                    keys = [row["ratingKey"] for row in json.loads(replay(port, whole)[2])["MediaContainer"].get("Metadata", [])]
                    window = json.loads(body)["MediaContainer"]
                    check(label + ": the window of the whole listing", [row["ratingKey"] for row in window.get("Metadata", [])]
                          == keys[window["offset"]:window["offset"] + window["size"]])
                    for side in (reference, got):
                        side["MediaContainer"].pop("Metadata", None)
                differences = [line for line in diff(reference, got) if line.split(" ")[0].rsplit(".", 1)[-1] not in UNFIXED]
                check(label, not differences, "%d: %s" % (len(differences), "; ".join(differences[:6])))
                if name == "order" and exchange["path"].endswith("/sections/1/all") and not exchange["query"]:
                    # rex's tie: India and Hotel first air at one start, India on the lineup's first channel.
                    titles = [row["title"] for row in json.loads(body)["MediaContainer"].get("Metadata", [])]
                    check("order: a tie goes in the order the programmes first air, then lineup", titles == ["Lima", "India", "Hotel", "The Juliet", "Kappa"], titles)
        finally:
            process.kill()
            process.wait()


def tune2(scratch, source, guide):
    """One tune as Plex Web sends it, to plexav without a Plex Pass (`tune2_s1434`): the answer and
    its headers, the session's activities, the session, and its Part key, which plexav answers
    404 whichever client the key names."""
    recorded = fixture("tune2_s1434.json.gz")
    print("tune2 (the reference at %d)" % recorded["now"])
    port, process = start_rex(scratch, recorded["now"])
    try:
        push_guide(port, source, guide)
        exchanges = recorded["exchanges"]
        uuid, _ = tune(port, exchanges[0], "Plex Web, no Plex Pass")
        recorded_uuid = UUID.search(exchanges[0]["body"]).group(0)
        subscription = re.search(r'MediaSubscription key="(\d+)"', exchanges[0]["body"]).group(1)
        for exchange in exchanges[1:]:
            path = exchange["path"].replace(recorded_uuid, uuid or "none")
            if exchange["path"] == "/activities":
                status, _, body = replay(port, dict(exchange, path=path))
                mine = json.loads(body)["MediaContainer"].get("Activity", [])
                theirs = [row for row in json.loads(exchange["body"])["MediaContainer"]["Activity"]
                          if row.get("Context", {}).get("subscriptionID") == subscription]
                wipe = lambda rows: sorted([dict(row, uuid=None, Context={name: value for name, value in row["Context"].items()
                                                                          if name not in ("subscriptionID", "transcode_session")})
                                            for row in rows], key=lambda row: row["type"])
                check("/activities lists the session's grab and its subscription's refresh, as plexav",
                      wipe(mine) == wipe(theirs), json.dumps(mine)[:300])
                continue
            compare(label_of(dict(exchange, path=exchange["path"].replace(recorded_uuid, "<uuid>"))), port, dict(exchange, path=path))
    finally:
        process.kill()
        process.wait()


def main():
    if not FFMPEG or not FFPROBE:
        print("SKIP: no ffmpeg (set REX_TRANSCODER)")
        return 0
    scratch = tempfile.mkdtemp(prefix="rex-livetv-test-")
    guide = fixture("guide_s1434.json")
    source = LiveSource(FFMPEG, FFPROBE, free_port(), root=os.path.join(scratch, "source"))
    try:
        source.wait()
        wanted = sys.argv[1:] or ["tunerless", "reads", "browse", "listings", "web", "android", "session", "tune2"]
        for phase in wanted:
            {"tunerless": lambda: tunerless(scratch, source, guide), "reads": lambda: reads(scratch, source, guide),
             "web": lambda: watch(scratch, source, guide, "web_watch_s1434.json.gz", "Plex Web"),
             "android": lambda: watch(scratch, source, guide, "android_watch_s1434.json.gz", "Plex for Android"),
             "session": lambda: session(scratch, source, guide), "browse": lambda: browse(scratch, source, guide),
             "tune2": lambda: tune2(scratch, source, guide), "listings": lambda: listings(scratch, source)}[phase]()
    finally:
        source.close()
        if os.environ.get("REX_KEEP_TMP"):
            print("scratch kept: %s" % scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    failed = [label for label, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
