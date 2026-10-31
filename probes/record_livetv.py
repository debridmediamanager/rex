#!/usr/bin/env python3
"""Record what a real PMS with a DVR answers Plex clients' Live TV requests, for
`tests/test_livetv.py` (fixtures in `tests/fixtures/livetv/`).

The reference is plexav: a claimed PMS 1.43.4 with an HDHomeRun of three channels (2.1 Rex News,
4.1 Rex Movies, 7.1 Rex Sports) and an XMLTV guide, in Europe/Amsterdam. Three modes:

    guide     plexav's guide.xml as a source pushes it (`channel.guide`, docs/feed-api.md "Live
              TV"), keeping the airings PMS held: those that had not ended when it read the guide
                python3 probes/record_livetv.py guide guide.xml 1793454567 > tests/fixtures/livetv/guide_s1434.json
    capture   a recorded client session's Live TV exchanges, redacted, with the moment they began
                python3 probes/record_livetv.py capture web-livetv-watch.jsonl > tests/fixtures/livetv/web_watch_s1434.json
    replay    on the lab host: the read-only requests below, each a client's own (its query and
              X-Plex headers as captured), asked of the reference now with its server token
                python3 record_livetv.py replay http://127.0.0.1:32420 web-*.jsonl droid-livetv.jsonl > reads.json

Every fixture is `{"now": <epoch the answers were given at>, "exchanges": [...]}`; the test pins
rex's guide clock there (`REX_LIVETV_NOW`). Tokens, cookies and account names are left out.
"""
import ast
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

LIVETV = re.compile(r"^/(tv\.plex\.providers\.epg\.xmltv:\d+/|livetv/|media/providers$|video/:/transcode/universal/(decision|start\.m3u8)|status/sessions$|:/timeline$)")
# The client headers a request is replayed with; never a token, a cookie or a device token.
KEEP = re.compile(r"^(accept|x-plex-(product|version|client-identifier|platform|platform-version|device|device-name|model|"
                  r"features|language|provider-version|text-format|session-identifier|session-id|client-profile-name|"
                  r"client-profile-extra|playback-session-id|playback-id|incomplete-segments))$", re.I)
ACCOUNT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")
# The lab accounts' names and plex.tv ids, and a public address, as they appear in sessions.
PRIVATE = [(re.compile(r"/users/[0-9a-f]{16}/"), "/users/0000000000000000/"),
           (re.compile(r'"(title|username)":"(plexa630|yowmamasita)"'), r'"\1":"owner"'),
           (re.compile(r'\b(?!(?:10|127|172|192)\.)(\d{1,3}\.){3}\d{1,3}\b'), "203.0.113.1")]
# Query parameters a client sends that are credentials or device ids: never kept.
SECRET = {"X-Plex-Token", "X-Plex-Device-Notification-Token", "X-Plex-Advertising-Identifier"}
# Requests no capture holds, asked beside the captured ones.
EXTRA = [
    "/livetv/dvrs", "/livetv/dvrs/2", "/livetv/dvrs/3", "/livetv/sessions", "/livetv/sessions/nope",
    "/tv.plex.providers.epg.xmltv:2/grid?channelGridKey=4.1&date=2026-11-01",
    "/tv.plex.providers.epg.xmltv:2/grid?channelGridKey=4.1&date=2026-10-30",
    "/tv.plex.providers.epg.xmltv:2/grid?channelGridKey=9.9&date=2026-10-31",
    "/tv.plex.providers.epg.xmltv:2/grid?channelGridKey=4.1",
    "/tv.plex.providers.epg.xmltv:2/hubs/discover?promoted=1&includeTypeFirst=1",
    "/tv.plex.providers.epg.xmltv:2/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fmovie%2FSintel",
    "/tv.plex.providers.epg.xmltv:2/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fepisode%2FFirefly%2F1%2F5",
    "/tv.plex.providers.epg.xmltv:2/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fseason%2FFirefly%2F1",
    "/tv.plex.providers.epg.xmltv:2/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fshow%2FFirefly/children",
    "/tv.plex.providers.epg.xmltv:2/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fmovie%2FNope",
] + ["/tv.plex.providers.epg.xmltv:2/library/search?query=%s&limit=100&searchTypes=livetv&includeCollections=1&includeExternalMedia=1" % query
     for query in ("Firefly", "Sintel", "fire", "bunny", "Episode%203", "rex", "tears%20of", "sintell", "Radiohead")]


def guide(path, held_since):
    root = ET.parse(path).getroot()
    stamp = lambda text: int(dt.datetime.strptime(text, "%Y%m%d%H%M%S %z").timestamp())
    channels = {}
    for channel in root.findall("channel"):
        name = channel.find("display-name").text
        channels[channel.get("id")] = {"number": channel.get("id"), "title": name, "call_sign": name, "guide": []}
    for programme in root.findall("programme"):
        start, end = stamp(programme.get("start")), stamp(programme.get("stop"))
        if end <= held_since:
            continue
        entry = {"start": start, "end": end, "title": programme.findtext("title")}
        if programme.find("desc") is not None:
            entry["summary"] = programme.findtext("desc")
        number = programme.find("episode-num[@system='xmltv_ns']")
        if number is not None:
            season, episode = (int(part) + 1 for part in number.text.split(".")[:2])
            entry.update(type="episode", show=entry["title"], title=programme.findtext("sub-title"), season=season, episode=episode)
        else:
            entry["type"] = "movie"
        categories = [category.text for category in programme.findall("category")]
        if categories:
            entry["categories"] = categories
        if programme.find("new") is not None:
            entry["new"] = True
        channels[programme.get("channel")]["guide"].append(entry)
    return {"source": "plexav guide.xml, airings held from %d" % held_since, "channels": list(channels.values())}


def redact(text):
    if not isinstance(text, str):
        return text
    text = ACCOUNT.sub("owner@example.com", text)
    for pattern, replacement in PRIVATE:
        text = pattern.sub(replacement, text)
    return text


def exchange(record, body, status, content_type, at=None):
    headers = record["req_headers"]
    if isinstance(headers, str):
        headers = ast.literal_eval(headers)
    return {
        "method": record["method"], "path": record["path"],
        "query": [[name, value] for name, value in record.get("query") or [] if name not in SECRET],
        "headers": [[name, value] for name, value in headers if KEEP.match(name)],
        "status": status, "type": content_type, "body": redact(body), "t": round(at if at is not None else record.get("t", 0), 3),
    }


def server_records(paths):
    for path in paths:
        for line in open(path):
            record = json.loads(line)
            # The Android capture keeps its lists as Python's text of them.
            for name in ("query", "req_headers", "res_headers"):
                if isinstance(record.get(name), str):
                    record[name] = ast.literal_eval(record[name])
            if record.get("port") in (32400, 32420, 32421) and LIVETV.match(record.get("path", "")):
                yield record


def capture(path):
    exchanges, start = [], None
    for record in server_records([path]):
        start = start or record["t"]
        headers = record["res_headers"]
        headers = ast.literal_eval(headers) if isinstance(headers, str) else headers
        content_type = dict((name.lower(), value) for name, value in headers).get("content-type", "")
        exchanges.append(exchange(record, record.get("res_body"), record["status"], content_type))
    # The guide's clock is the tune's moment where the session holds one: what was on air then.
    tune = next((item["t"] for item in exchanges if item["path"].endswith("/tune")), start)
    return {"now": int(tune), "source": path.rsplit("/", 1)[-1], "exchanges": exchanges}


def replay(base, paths):
    token = subprocess.run(["docker", "exec", "plexav-plex", "sh", "-c",
                            "grep -o 'PlexOnlineToken=\"[^\"]*' '/config/Library/Application Support/Plex Media Server/Preferences.xml'"],
                           capture_output=True, text=True).stdout.split('"')[1].strip()
    wanted, seen = [], set()
    for record in server_records(paths):
        if record["method"] != "GET" or "/transcode/" in record["path"] or record["path"] in ("/:/timeline", "/status/sessions"):
            continue
        name = (record["path"], tuple(map(tuple, record.get("query") or [])))
        if name not in seen:
            seen.add(name)
            wanted.append(record)
    template = wanted[0]
    for extra in EXTRA:
        path, _, query = extra.partition("?")
        wanted.append(dict(template, path=urllib.parse.unquote(path) if "metadata" not in path else path,
                           query=urllib.parse.parse_qsl(query, keep_blank_values=True)))
    start, exchanges = time.time(), []
    for record in wanted:
        headers = dict((name, value) for name, value in exchange(record, "", 0, "")["headers"])
        query = urllib.parse.urlencode([tuple(pair) for pair in record.get("query") or []])
        request = urllib.request.Request(base + record["path"] + ("?" + query if query else ""),
                                         headers=dict(headers, **{"X-Plex-Token": token}))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, content_type, body = response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as error:
            status, content_type, body = error.code, error.headers.get("Content-Type", ""), error.read()
        exchanges.append(exchange(record, body.decode("utf-8", "replace"), status, content_type, time.time()))
    if time.time() - start > 60:
        sys.exit("the replay took over a minute: what is on air moved under it")
    return {"now": int(start), "source": "replayed against plexav", "exchanges": exchanges}


if __name__ == "__main__":
    mode = sys.argv[1]
    result = {"guide": lambda: guide(sys.argv[2], int(sys.argv[3])), "capture": lambda: capture(sys.argv[2]),
              "replay": lambda: replay(sys.argv[2], sys.argv[3:])}[mode]()
    json.dump(result, sys.stdout, indent=None, separators=(",", ":"))
