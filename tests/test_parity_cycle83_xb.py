#!/usr/bin/env python3
"""Cycle 83, territory B: what the native server got wrong that B's regressions caught.

- An item's `/themes` and `/clearLogos` candidate rows are `Track` and `Photo` elements
  (`tests/fixtures/candidate_rows_kids.json`, recorded on kids). python-plexapi's
  `themes()` and `logos()` keep only rows of their own tag, so the native server's default
  `Video` element turned an uploaded theme or logo into an empty list
  (`plexapi/mixins/resources.py`, `fetchItems(..., cls=media.Theme|media.Logo)`).
- A play queue belongs to the client the QUERY names as well as the header: Plex Web sends
  every X-Plex-* in the query string, and a real PMS retires that client's older queue on
  its next create and moves ownership on an `own=1` read by that form
  (`tests/fixtures/playqueue_query_client_s42.json`). The native server read the header
  alone, so every Plex Web queue stayed alive and an `own=1` read took nothing.
- `continuous=1` on ONE episode queues it and the episodes after it in its own season
  (`tests/fixtures/playqueue_continuous_s42.json`); the native server queued the episode
  alone. python-plexapi's `PlayQueue.create(item, continuous=True)` sends it.
- A smart show collection over `all?type=2&episode.unwatched=1&limit=3` holds SHOWS
  (`tests/fixtures/plexapi_writes_s42.json`, python-plexapi's write chain); the oracle
  expanded them into episodes.
- A smart playlist over `all?type=4&...&limit=3` holds three episodes; the native server
  ignored the uri's `type`, capped the one show and expanded it.
- A playlist's `deleted` frame names the playlist; the native server's was empty.
- `addItem(show)` grows a queue by the show's episodes; the native server added the show.
- A decision's streams keep their stored language names under any `X-Plex-Language`
  (`tests/fixtures/decision_stream_language_zen.json`) while the item read localises them;
  the oracle localised the decision too.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `SIMPLEX_SERVER=python`.

    python3 tests/test_parity_cycle83_xb.py
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           SIMPLEX_PLEXTV_URL=os.environ.get("SIMPLEX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("SIMPLEX_STATE", "SIMPLEX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
# python-plexapi's own header set: no Accept, so the answer is XML.
HEADERS = {"X-Plex-Client-Identifier": "parity-cycle83-xb", "X-Plex-Product": "PlexAPI",
           "X-Plex-Version": "4.18.3", "X-Plex-Platform": "Darwin"}
MOVIE = "1008"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
       b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 413

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


def request(port, method, path, body=None, query_only=False):
    """`query_only`: no X-Plex-* header at all, Plex Web's shape (they ride in the query)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Connection": "close"} if query_only else dict(HEADERS, Connection="close")
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


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


def measured_elements():
    """The candidate row element of each list, read from the recorded exchange."""
    recorded = json.load(open(os.path.join(HERE, "fixtures", "candidate_rows_kids.json")))
    out = {}
    for exchange in recorded["exchanges"]:
        leaf = exchange["path"].rsplit("/", 1)[1]
        rows = list(ET.fromstring(exchange["body"].encode()))
        out[leaf] = {row.tag for row in rows}
    return out


def run(port):
    measured = measured_elements()
    check("fixture: kids answers Track rows on /themes and Photo rows on /clearLogos",
          measured == {"themes": {"Track"}, "clearLogos": {"Photo"}}, measured)
    for leaf, body in (("themes", MP3), ("clearLogos", PNG)):
        status, _ = request(port, "POST", "/library/metadata/%s/%s" % (MOVIE, leaf), body)
        check("POST /%s with a file is 200" % leaf, status == 200, status)
        status, raw = request(port, "GET", "/library/metadata/%s/%s" % (MOVIE, leaf))
        rows = list(ET.fromstring(raw)) if status == 200 else []
        uploads = [row for row in rows if (row.get("ratingKey") or "").startswith("upload://%s/" % leaf)]
        check("GET /%s lists the upload as one candidate" % leaf, len(uploads) == 1,
              raw[:300])
        tags = sorted({row.tag for row in uploads})
        check("the uploaded %s candidate is a %s element, as kids' rows are"
              % (leaf, "/".join(sorted(measured[leaf]))),
              uploads and set(tags) == measured[leaf], tags)
        check("the uploaded %s candidate is selected" % leaf,
              uploads and uploads[0].get("selected") == "1", uploads and uploads[0].attrib)
    # JSON carries no element name: the private key must not leak into it.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("GET", "/library/metadata/%s/themes" % MOVIE,
                       headers=dict(HEADERS, Accept="application/json", Connection="close"))
    raw = connection.getresponse().read()
    connection.close()
    check("the JSON listing carries no private key", not re.search(rb'"_', raw), raw[:300])


def queue_checks(port):
    measured = json.load(open(os.path.join(HERE, "fixtures", "playqueue_query_client_s42.json")))
    retire = [step["status"] for step in measured["retire"]]
    own = measured["own"]
    check("fixture: the first of two query-named queues is a 404, the second a 200",
          retire == [200, 200, 404, 200], retire)
    check("fixture: an own=1 read by a query-named client steps the version and retires its queue",
          own[2]["playQueueVersion"] == own[0]["playQueueVersion"] + 1 and own[3]["status"] == 404, own)
    status, raw = request(port, "GET", "/identity")
    machine = ET.fromstring(raw).get("machineIdentifier")
    uri = "server%%3A%%2F%%2F%s%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fmetadata%%2F1002" % machine

    def create(client):
        status, raw = request(port, "POST", "/playQueues?type=video&uri=%s&X-Plex-Client-Identifier=%s"
                              "&X-Plex-Product=Plex%%20Web" % (uri, client), query_only=True)
        root = ET.fromstring(raw)
        return root.get("playQueueID"), root.get("playQueueVersion")

    first, _ = create("xb-query-x")
    second, _ = create("xb-query-x")
    statuses = [request(port, "GET", "/playQueues/%s" % first, query_only=True)[0],
                request(port, "GET", "/playQueues/%s" % second, query_only=True)[0]]
    check("a query-named client's second queue retires its first (404, 200)",
          statuses == [404, 200], statuses)
    taken, version = create("xb-query-x")
    other, _ = create("xb-query-y")
    status, raw = request(port, "GET", "/playQueues/%s?own=1&X-Plex-Client-Identifier=xb-query-y" % taken,
                          query_only=True)
    after = ET.fromstring(raw).get("playQueueVersion") if status == 200 else None
    check("an own=1 read by a query-named client steps the version by one",
          after is not None and int(after) == int(version) + 1, (version, after))
    check("... and retires that client's other queue",
          request(port, "GET", "/playQueues/%s" % other, query_only=True)[0] == 404)


def continuous_checks(port):
    measured = json.load(open(os.path.join(HERE, "fixtures", "playqueue_continuous_s42.json")))
    rule_holds = True
    for exchange in measured["exchanges"]:
        key = re.search(r"metadata%2F(\d+)&", exchange["path"]).group(1)
        season = next(keys for keys in measured["seasons"] if key in keys)
        rule_holds &= exchange["ratingKeys"] == season[season.index(key):]
    check("fixture: each continuous queue is the episode and the rest of its season", rule_holds)
    status, raw = request(port, "GET", "/identity")
    machine = ET.fromstring(raw).get("machineIdentifier")
    status, raw = request(port, "GET", "/library/metadata/20001/children")
    seasons = [row.get("ratingKey") for row in ET.fromstring(raw) if row.get("type") == "season"]
    cells = 0
    for season in seasons:
        status, raw = request(port, "GET", "/library/metadata/%s/children" % season)
        episodes = [row.get("ratingKey") for row in ET.fromstring(raw)]
        for position, key in enumerate(episodes):
            for flag, want in (("1", episodes[position:]), ("0", [key])):
                status, raw = request(port, "POST", "/playQueues?type=video&uri=server%%3A%%2F%%2F%s"
                                      "%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fmetadata%%2F%s"
                                      "&continuous=%s&repeat=0" % (machine, key, flag))
                got = [row.get("ratingKey") for row in ET.fromstring(raw)] if status == 200 else status
                check("continuous=%s on episode %s queues %s" % (flag, key, want), got == want, got)
                cells += 1
    check("the fixture show has a season with more than one episode to compare", cells > 2 * len(seasons))


def eventsource(port, trigger, seconds=4):
    """The SSE frames pushed while `trigger` runs."""
    import threading
    import urllib.request
    response = urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:%d/:/eventsource/notifications" % port,
        headers={"Accept": "text/event-stream"}), timeout=seconds + 5)
    threading.Timer(0.5, trigger).start()
    frames, name, deadline = [], None, time.time() + seconds
    try:
        while time.time() < deadline:
            try:
                line = response.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("event:"):
                name = text.split(":", 1)[1].strip()
            elif text.startswith("data:") and name:
                frames.append((name, text.split(":", 1)[1].strip()))
                name = None
    finally:
        response.close()
    return frames


def write_chain_checks(port):
    measured = json.load(open(os.path.join(HERE, "fixtures", "plexapi_writes_s42.json")))
    status, raw = request(port, "GET", "/identity")
    machine = ET.fromstring(raw).get("machineIdentifier")

    def section_uri(query):
        return ("server%%3A%%2F%%2F%s%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fsections%%2F2%%2Fall%%3F%s"
                % (machine, query))

    # A smart show collection reads its query at its own level.
    want = measured["smart_show_collection"]
    check("fixture: the smart show collection holds shows", set(want["member_types"]) == {"Show"}, want)
    query = re.search(r"all%3F(.*)$", want["request"]).group(1)
    status, raw = request(port, "POST", "/library/collections?sectionId=2&smart=1&title=Xb%%20Smart&type=2&uri=%s"
                          % section_uri(query))
    collection = ET.fromstring(raw)[0].get("ratingKey") if status == 200 else None
    status, raw = request(port, "GET", "/library/collections/%s/children" % collection)
    members = [(row.tag, row.get("type")) for row in ET.fromstring(raw)] if status == 200 else status
    check("a smart show collection over an episode filter lists shows", members and
          all(member == ("Directory", "show") for member in members), members)
    status, raw = request(port, "GET", "/library/collections/%s" % collection)
    row = ET.fromstring(raw)[0] if status == 200 else None
    check("... and its childCount counts them", row is not None and members
          and row.get("childCount") == str(len(members)), row is not None and row.attrib)
    request(port, "DELETE", "/library/collections/%s" % collection)

    # A smart episode playlist takes its uri's type and limit.
    want = measured["smart_episode_playlist"]
    check("fixture: the capped smart episode playlist holds three episodes",
          want["leafCount"] == 3 and want["member_types"] == ["Episode"] * 3, want)
    query = re.search(r"all%3F(.*)$", want["request"]).group(1)
    status, raw = request(port, "POST", "/playlists?smart=1&title=Xb%%20SmartPl&type=video&uri=%s"
                          % section_uri(query))
    playlist = ET.fromstring(raw)[0] if status == 200 else None
    check("a smart playlist over all?type=4&limit=3 counts three", playlist is not None
          and playlist.get("leafCount") == "3", playlist is not None and playlist.attrib)
    status, raw = request(port, "GET", "/playlists/%s/items" % playlist.get("ratingKey"))
    items = [row.get("type") for row in ET.fromstring(raw)] if status == 200 else status
    check("... and lists three episodes", items == ["episode"] * 3, items)

    # A playlist's deleted frame names it.
    want = measured["playlist_delete_frame"]
    check("fixture: the deleted frame carries the playlist's title",
          "title='%s'" % want["playlist_title"] in want["frames"][0], want)
    deleted = playlist.get("ratingKey")
    request(port, "PUT", "/playlists/%s?title=Xb%%20Renamed" % deleted)
    frames = eventsource(port, lambda: request(port, "DELETE", "/playlists/%s" % deleted))
    entries = []
    for name, data in frames:
        entry = json.loads(data).get("TimelineEntry") if name == "timeline" else None
        for entry in (entry if isinstance(entry, list) else [entry] if entry else []):
            if str(entry.get("itemID")) == str(deleted) and entry.get("state") == 9:
                entries.append(entry)
    check("a playlist's deleted frame names the playlist as it is now",
          [entry.get("title") for entry in entries] == ["Xb Renamed"], frames)

    # Adding a show to a queue adds its episodes.
    want = measured["queue_add_show"]
    check("fixture: adding a three-episode show grows the queue by three",
          want["size_after"] - want["size_before"] == want["show_leafCount"], want)
    status, raw = request(port, "POST", "/playQueues?type=video&uri=server%%3A%%2F%%2F%s"
                          "%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fmetadata%%2F1007" % machine)
    queue = ET.fromstring(raw)
    status, raw = request(port, "GET", "/library/metadata/20001")
    leaves = int(ET.fromstring(raw)[0].get("leafCount"))
    status, raw = request(port, "PUT", "/playQueues/%s?uri=library%%3A%%2F%%2Fx%%2Fitem%%2Flibrary%%2Fmetadata%%2F20001"
                          % queue.get("playQueueID"))
    after = ET.fromstring(raw) if status == 200 else None
    added = [row.get("type") for row in after][1:] if after is not None else status
    check("addItem(show) grows the queue by the show's %d episodes" % leaves,
          added == ["episode"] * leaves, added)


def decision_language_checks(port):
    measured = json.load(open(os.path.join(HERE, "fixtures", "decision_stream_language_zen.json")))
    tags = {language: {(row["streamType"], row["languageTag"]): row["language"]
                       for row in measured["decision"][language]} for language in ("en", "fr")}
    items = {language: {(row["streamType"], row["languageTag"]): row["language"]
                        for row in measured["item"][language]} for language in ("en", "fr")}
    check("fixture: zen's decision names the French track Français under en and fr",
          tags["en"][(2, "fr")] == tags["fr"][(2, "fr")] == "Français", tags)
    check("fixture: ... and the English subtitle English under fr, where the item read says Anglais",
          tags["fr"][(3, "en")] == "English" and items["fr"][(3, "en")] == "Anglais", (tags, items))
    # Cycle 84: a decision lists only the SELECTED subtitle (zen, plexscratch42;
    # `tests/fixtures/decision_streams_selected.json`), so each subtitle is selected in turn on
    # part 10110 -- 1002003 English, 1002004 French -- and unselected again afterwards.
    for subtitle, sub_tag, sub_name in (("1002003", "en", "English"), ("1002004", "fr", "Français")):
        request(port, "PUT", "/library/parts/10110?subtitleStreamID=%s&allParts=1" % subtitle)
        for language in ("en", "fr"):
            path = ("/video/:/transcode/universal/decision?path=%2Flibrary%2Fmetadata%2F1011"
                    "&mediaIndex=0&partIndex=0&protocol=hls&directPlay=1&directStream=1&location=lan"
                    "&subtitles=none&hasMDE=1&session=xb83-lang-" + language + "&X-Plex-Language=" + language
                    + "&X-Plex-Client-Identifier=xb83-lang&X-Plex-Product=Plex%20Web"
                    "&X-Plex-Platform=Chrome&X-Plex-Version=4.147.1")
            status, raw = request(port, "GET", path, query_only=True)
            streams = [(row.get("streamType"), row.get("languageTag"), row.get("language"))
                       for row in ET.fromstring(raw).iter("Stream")] if status == 200 else status
            want = {("2", "en"): "English", ("3", sub_tag): sub_name}
            got = {(kind, tag): name for kind, tag, name in streams if kind in ("2", "3")} \
                if isinstance(streams, list) else streams
            check("a decision under X-Plex-Language=%s keeps the stored names (subtitle %s selected)"
                  % (language, sub_tag), got == want, streams)
    request(port, "PUT", "/library/parts/10110?subtitleStreamID=0&allParts=1")


def main():
    process, port = start()
    try:
        run(port)
        queue_checks(port)
        continuous_checks(port)
        write_chain_checks(port)
        decision_language_checks(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d cycle-83 B parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
