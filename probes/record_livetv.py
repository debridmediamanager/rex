#!/usr/bin/env python3
"""Record what a real PMS with a DVR answers Plex clients' Live TV requests, for
`tests/test_livetv.py` (fixtures in `tests/fixtures/livetv/`).

The reference is plexav: a claimed PMS 1.43.4 with an HDHomeRun of three channels (2.1 Rex News,
4.1 Rex Movies, 7.1 Rex Sports) and an XMLTV guide, in Europe/Amsterdam. Its modes:

    guide     plexav's guide.xml as a source pushes it (`channel.guide`, docs/feed-api.md "Live
              TV"), keeping the airings PMS held: those that had not ended when it read the guide
                python3 probes/record_livetv.py guide guide.xml 1793454567 > tests/fixtures/livetv/guide_s1434.json
    capture   a recorded client session's Live TV exchanges, redacted, with the moment they began
                python3 probes/record_livetv.py capture web-livetv-watch.jsonl > tests/fixtures/livetv/web_watch_s1434.json
    headers   the header lines of each Live TV read the clients made (no bodies)
                python3 probes/record_livetv.py headers web-livetv-*.jsonl | gzip > tests/fixtures/livetv/headers_s1434.json.gz
    ask       on the lab host: GETs no capture holds (the provider's search, sections and Guide
              page, a season's episodes, the DVR's device), with Plex Web 4.163.0's headers, a
              browser's Origin and gzip
                python3 record_livetv.py ask http://127.0.0.1:32420 > browse.json
    tune      on the lab host: ONE tune of 2.1 as Plex Web sends it, then reads of what it made
              (`/activities`, the session, its Part key as the tune and the session name it)
                python3 record_livetv.py tune http://127.0.0.1:32420 > tune2.json
    replay    on the lab host: the read-only requests below, each a client's own (its query and
              X-Plex headers as captured), asked of the reference now with its server token
                python3 record_livetv.py replay http://127.0.0.1:32420 web-*.jsonl droid-livetv.jsonl > reads.json
    listing   on the lab host: WRITES plexav's guide.xml (one of GUIDES, or `plexav`, its own back
              from guide.xml.orig), refreshes the guide and asks the provider's listings
                python3 record_livetv.py listing http://127.0.0.1:32420 order | gzip > listing_order_s1434.json.gz

Every fixture is `{"now": <epoch the answers were given at>, "exchanges": [...]}` (a listing's also
its `guide`, as a source pushes it); the test pins
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
KEEP = re.compile(r"^(accept|accept-encoding|origin|x-plex-(product|version|client-identifier|platform|platform-version|device|device-name|model|"
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
        # Measured (`listing held`): an episode's airing is a premiere unless it is
        # `<previously-shown />`; `<new />` changes nothing.
        if programme.find("previously-shown") is not None:
            entry["previously_shown"] = True
        channels[programme.get("channel")]["guide"].append(entry)
    return {"source": "plexav guide.xml, airings held from %d" % held_since, "channels": list(channels.values())}


def redact(text):
    if not isinstance(text, str):
        return text
    text = ACCOUNT.sub("owner@example.com", text)
    for pattern, replacement in PRIVATE:
        text = pattern.sub(replacement, text)
    return text


def exchange(record, body, status, content_type, at=None, answered=None):
    headers = record["req_headers"]
    if isinstance(headers, str):
        headers = ast.literal_eval(headers)
    return {
        "method": record["method"], "path": record["path"],
        "query": [[name, value] for name, value in record.get("query") or [] if name not in SECRET],
        "headers": [[name, value] for name, value in headers if KEEP.match(name)],
        "status": status, "type": content_type, "body": redact(body), "t": round(at if at is not None else record.get("t", 0), 3),
        # The answer's header lines in order, as the client got them (`Date` is the moment's).
        "answered": [[name, value] for name, value in answered or [] if name != "Date"],
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
        exchanges.append(exchange(record, record.get("res_body"), record["status"], content_type, answered=headers))
    # The guide's clock is the tune's moment where the session holds one: what was on air then.
    tune = next((item["t"] for item in exchanges if item["path"].endswith("/tune")), start)
    return {"now": int(tune), "source": path.rsplit("/", 1)[-1], "exchanges": exchanges}


def headers(paths):
    """Each Live TV read the clients made, once, with the header lines the reference answered
    it with and no body: the answers' headers, not their values, at whatever moment."""
    exchanges, seen = [], set()
    for record in server_records(paths):
        name = (record["path"].split("?")[0], json.dumps(sorted(dict(record.get("query") or []).keys())))
        if record["method"] != "GET" or "/transcode/" in record["path"] or name in seen or record["status"] != 200:
            continue
        seen.add(name)
        answered = record["res_headers"]
        content_type = dict((key.lower(), value) for key, value in answered).get("content-type", "")
        exchanges.append(exchange(record, None, record["status"], content_type, answered=answered))
    return {"now": int(exchanges[0]["t"]), "source": "Plex Web's guide, pages, record and search sessions", "exchanges": exchanges}


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
        headers = dict((name, value) for name, value in exchange(record, "", 0, "")["headers"] if name.lower() != "accept-encoding")
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


WEB = {"X-Plex-Product": "Plex Web", "X-Plex-Version": "4.163.0", "X-Plex-Client-Identifier": "rexlivetvprobe2",
       "X-Plex-Platform": "Chrome", "X-Plex-Platform-Version": "151.0", "X-Plex-Features": "external-media,indirect-media,hub-style-list",
       "X-Plex-Model": "hosted", "X-Plex-Device": "Linux", "X-Plex-Device-Name": "Chrome", "X-Plex-Language": "en",
       "X-Plex-Provider-Version": "7.2", "X-Plex-Text-Format": "plain", "Origin": "https://app.plex.tv",
       "Accept-Encoding": "gzip, deflate, br, zstd", "Accept-Language": "en"}
P = "/tv.plex.providers.epg.xmltv:2"
SEARCH = "&limit=100&searchTypes=livetv&includeCollections=1&includeExternalMedia=1"
ASKED = ["%s/hubs/search?query=%s" % (P, word) for word in ("firefly", "news", "rex")] + ["%s/hubs/search?query=firefly&limit=3" % P] + [
    "%s/library/search?query=%s%s" % (P, word, SEARCH) for word in ("news", "sports", "movie", "drama", "foot", "series")] + [
    P + "/sections", P + "/sections/1", P + "/sections/2", P + "/sections/3", P + "/sections/4", P + "/watchnow", P + "/watchnow/all",
    P + "/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fseason%2FFirefly%2F1/children",
    P + "/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fseason%2FFirefly%2F1/children?excludeAllLeaves=1&X-Plex-Container-Start=0&X-Plex-Container-Size=20",
    P + "/metadata/tv%2Eplex%2Exmltv%3A%2F%2Fmovie%2FSintel/children",
    "/media/grabbers/devices", "/media/grabbers/devices/1", "/media/grabbers/devices/1/channels", "/activities", "/"]


def ask(base, token, method, target, accept="application/json", extra=None):
    """One request as Plex Web sends it, as a fixture exchange: the headers sent (no token) and
    the answer's header lines and body, unzipped."""
    import gzip
    headers = dict(WEB, Accept=accept, **(extra or {}))
    request = urllib.request.Request(base + target, method=method, data=b"" if method == "POST" else None,
                                     headers=dict(headers, **{"X-Plex-Token": token}))
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    body = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    path, _, query = target.partition("?")
    return {"method": method, "path": path, "query": [list(pair) for pair in urllib.parse.parse_qsl(query, keep_blank_values=True)],
            "headers": [[name, value] for name, value in headers.items()], "status": getattr(response, "status", None) or response.code,
            "type": response.headers.get("Content-Type", ""), "body": redact(body.decode("utf-8", "replace")), "t": round(time.time(), 3),
            "answered": [[name, value] for name, value in response.headers.items() if name != "Date"]}


def server_token():
    return subprocess.run(["docker", "exec", "plexav-plex", "sh", "-c",
                           "grep -o 'PlexOnlineToken=\"[^\"]*' '/config/Library/Application Support/Plex Media Server/Preferences.xml'"],
                          capture_output=True, text=True).stdout.split('"')[1].strip()


def asked(base):
    token = server_token()
    exchanges = [ask(base, token, "GET", target) for target in ASKED]
    return {"now": int(exchanges[0]["t"]), "source": "plexav, Plex Web's headers, no capture holds these", "exchanges": exchanges}


def tuned(base):
    """ONE tune: plexav records nothing, but a tune opens a capture and a rolling subscription
    that end on their own; ask before running it again."""
    import re
    token = server_token()
    session = "rexsessprobe"
    tune = ask(base, token, "POST", "/livetv/dvrs/2/channels/2.1/tune?X-Plex-Session-Identifier=" + session, "text/plain, */*; q=0.01")
    uuid = re.search(r"/livetv/sessions/([0-9a-f-]{36})", tune["body"]).group(1)
    exchanges = [tune, ask(base, token, "GET", "/activities"), ask(base, token, "GET", "/livetv/sessions/" + uuid)]
    for client in (session, WEB["X-Plex-Client-Identifier"]):
        exchanges.append(ask(base, token, "GET", "/livetv/sessions/%s/%s/index.m3u8?offset=-1.000000" % (uuid, client), "*/*"))
    exchanges.append(ask(base, token, "GET", "/status/sessions"))
    return {"now": int(tune["t"]), "source": "plexav, one tune as Plex Web sends it", "exchanges": exchanges}


# The guides `listing` gives plexav, programmes in the document's order: channel, start in minutes
# from the moment it is written, minutes, title, categories, a season and episode for an episode,
# and an element flagging it (`new`, `previously-shown`). Each order of the document, start,
# title and lineup disagrees with the others.
GUIDES = {
    # Ties at one start (India and Hotel at +60, Zebra and Yak at +150) where the programme named
    # first in the document airs that start later in it; "The" before two titles; a film whose
    # first airing comes before another's while its last comes after; one of each on now.
    "order": [["4.1", 300, 30, "Hotel", ["Movie", "Drama"]], ["2.1", 60, 30, "India", ["Movie", "Drama"]],
              ["4.1", 60, 30, "Hotel", ["Movie", "Drama"]], ["7.1", 120, 30, "The Juliet", ["Movie"]],
              ["7.1", 180, 30, "Kappa", ["Movie"]], ["7.1", 600, 30, "Kappa", ["Movie"]],
              ["4.1", 150, 30, "The Zebra Show", ["Series", "Drama"], [1, 1]], ["2.1", 200, 30, "Yak Show", ["Series", "Comedy"], [1, 1]],
              ["2.1", 250, 30, "The Zebra Show", ["Series", "Drama"], [1, 2]], ["2.1", 150, 30, "Yak Show", ["Series", "Comedy"], [2, 1]],
              ["7.1", -10, 40, "Lima", ["Movie", "Comedy"]], ["4.1", -5, 30, "Yak Show", ["Series", "Comedy"], [1, 3]],
              ["2.1", -90, 30, "Golf", ["Movie"]]],
    # Airings that end a few minutes after plexav reads the guide, read once they have ended.
    "held": [["4.1", -20, 24, "Sierra", ["Movie"]], ["2.1", 60, 30, "Tango", ["Movie"]], ["7.1", -10, 18, "Uniform", ["Movie"]],
             ["4.1", 240, 30, "Sierra", ["Movie"]], ["7.1", 30, 30, "Uniform", ["Movie"]], ["2.1", -5, 7, "Romeo", ["Movie"]],
             ["2.1", 3, 20, "Quebec Show", ["Series"], [1, 1]], ["2.1", 120, 30, "Quebec Show", ["Series"], [1, 2]],
             # Which airings are premieres: an episode `<previously-shown />`, one `<new />`, a film `<new />`.
             ["7.1", 60, 30, "Nova Show", ["Series"], [1, 1], "previously-shown"], ["7.1", 120, 30, "Nova Show", ["Series"], [1, 2], "new"],
             ["4.1", 120, 30, "Vega", ["Movie"], None, "new"]],
}
GUIDE_PATH = "/home/ben/plexav/media/livetv/guide.xml"
ORIGINAL = "/home/ben/plexav/guide.xml.orig"


def xmltv(programmes, at):
    """A guide in plexav's own guide.xml shape."""
    from xml.sax.saxutils import escape
    stamp = lambda moment: dt.datetime.fromtimestamp(moment, dt.timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="rex-listing">']
    for number, name in (("2.1", "Rex News"), ("4.1", "Rex Movies"), ("7.1", "Rex Sports")):
        lines.append('<channel id="%s"><display-name>%s</display-name><display-name>%s</display-name><lcn>%s</lcn></channel>' % (number, name, number, number))
    for channel, start, minutes, title, categories, *rest in programmes:
        begins, number, flag = at + start * 60, (rest + [None])[0], (rest + [None, None])[1]
        body = '<title lang="en">%s</title>' % escape(title) + ('<sub-title lang="en">Episode %d</sub-title>' % number[1] if number else "")
        body += '<desc lang="en">%s.</desc>' % escape(title) + "".join('<category lang="en">%s</category>' % escape(name) for name in categories)
        if number:
            body += '<episode-num system="xmltv_ns">%d.%d.</episode-num>' % (number[0] - 1, number[1] - 1)
        if flag:
            body += "<%s />" % flag
        lines.append('<programme start="%s" stop="%s" channel="%s">%s</programme>' % (stamp(begins), stamp(begins + minutes * 60), channel, body))
    return "\n".join(lines + ["</tv>"]) + "\n"


def reload_guide(base, token):
    """The DVR's Refresh Guide, as Plex Web's DVR settings send it, waited out."""
    ask(base, token, "POST", "/livetv/dvrs/2/reloadGuide")
    started, seen = time.time(), False
    while time.time() - started < 240:
        time.sleep(1)
        activities = json.loads(ask(base, token, "GET", "/activities")["body"])["MediaContainer"].get("Activity", [])
        if any("guide" in (activity.get("type", "") + activity.get("title", "")).lower() for activity in activities):
            seen = True
        elif seen or time.time() - started > 15:
            return


def listing(base, name):
    """On the lab host: give plexav one of GUIDES (or, `plexav`, its own guide back from
    guide.xml.orig), refresh its guide, and ask the provider's listings as Plex Web asks them.
    The one write besides guide.xml is the refresh; Ben allowed both (2026-10-31)."""
    token = server_token()
    at = int(time.time()) // 60 * 60
    text = open(ORIGINAL).read() if name == "plexav" else xmltv(GUIDES[name], at)
    open(GUIDE_PATH, "w").write(text)
    reload_guide(base, token)
    read = int(time.time())
    if name == "held":
        time.sleep(max(0, at + 11 * 60 - time.time()))
    library = "&includeCollections=1&includeExternalMedia=1&includeAdvanced=1&includeMeta=1"
    later = "beginsAt>=now&sort=beginsAt,mediaHeight:desc,mediaAnalysisVersion:desc"
    on_now = "beginsAt<=now&endsAt>=%2B1s"
    targets = ["/all", "/sections/1/all", "/sections/2/all", "/sections/1/all?type=1", "/sections/2/all?type=2", "/sections/2/all?type=4"]
    targets += ["/sections/1/all?type=1&sort=" + sort for sort in (
        "beginsAt", "beginsAt:desc", "titleSort", "titleSort:desc", "originallyAvailableAt,year:nullsLast,titleSort",
        "originallyAvailableAt:desc,year:nullsLast:desc,titleSort")]
    targets += ["/sections/2/all?type=2&sort=" + sort for sort in ("titleSort", "titleSort:desc", "originallyAvailableAt", "originallyAvailableAt:desc")]
    targets += ["/sections/2/all?type=4&sort=" + sort for sort in (
        "beginsAt", "beginsAt:desc", "show.titleSort,season.index,episode.index", "show.titleSort:desc,season.index,episode.index")]
    targets += ["/sections/1/all?type=1&" + later, "/sections/2/all?type=4&" + later, "/all?type=1,4&" + on_now + "&sort=beginsAt,mediaHeight:desc,mediaAnalysisVersion:desc",
                "/sections/1/all?type=1&%s&sort=beginsAt&skipDetails=1" % on_now, "/sections/2/all?type=4&%s&sort=beginsAt&skipDetails=1" % on_now,
                "/sections/2/all?type=4&beginsAt>=%d&beginsAt<=%d&mediaAnalysisVersion=1&sort=beginsAt" % (at + 100 * 60, at + 260 * 60),
                "/sections/1/all?type=1&X-Plex-Container-Start=1&X-Plex-Container-Size=2", "/sections/2/all?type=4&X-Plex-Container-Start=0&X-Plex-Container-Size=2",
                "/sections/1/all?type=1" + library, "/sections/2/all?type=2" + library, "/sections/2/all?" + library[1:],
                "/sections/1/all?type=1&sort=titleSort:desc" + library, "/sections/2/all?type=4&sort=beginsAt:desc" + library, "/sections/3/all?type=4&" + later, "/hubs/onnow/all", "/hubs/onlater/all"]
    exchanges = [ask(base, token, "GET", P + target) for target in targets]
    exchanges.append(ask(base, token, "GET", P + "/sections/1/all?type=1" + library, extra={"X-Plex-Container-Start": "1", "X-Plex-Container-Size": "2"}))
    for word in ("drama", "comedy", "movie", "news", "series"):
        hit = ask(base, token, "GET", "%s/library/search?query=%s%s" % (P, word, SEARCH))
        exchanges.append(hit)
        for key in sorted(set(re.findall(r'"key":"(/tv\.plex\.providers\.epg\.xmltv:2/sections/\d/all\?genre=\d+)"', hit["body"]))):
            exchanges.append(ask(base, token, "GET", key))
            exchanges.append(ask(base, token, "GET", key + "&type=%s&sort=titleSort" % ("1" if "/sections/1/" in key else "2")))
    return {"now": int(exchanges[0]["t"]), "source": "plexav given guide %r at %d, read at %d" % (name, at, read),
            "guide": guide(GUIDE_PATH, read), "exchanges": exchanges}


if __name__ == "__main__":
    mode = sys.argv[1]
    result = {"guide": lambda: guide(sys.argv[2], int(sys.argv[3])), "capture": lambda: capture(sys.argv[2]),
              "headers": lambda: headers(sys.argv[2:]), "ask": lambda: asked(sys.argv[2]), "tune": lambda: tuned(sys.argv[2]),
              "replay": lambda: replay(sys.argv[2], sys.argv[3:]), "listing": lambda: listing(sys.argv[2], sys.argv[3])}[mode]()
    json.dump(result, sys.stdout, indent=None, separators=(",", ":"))
