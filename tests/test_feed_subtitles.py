#!/usr/bin/env python3
"""A source's sidecar subtitles: an item's `subtitles` are external streams of its own Part.

Reproduced live on 2026-10-23: a Radarr root in zurg's `__magic__`, Bazarr writing
`Movie (2010).en.srt` beside the video, and no subtitle in rex, because the feed carried only
video files. Each `subtitles` entry names a file under the source's url; rex fetches its text as
it reads a Part's bytes (the source's login, its redirects) off the runtime lock, and shows it as
the stream a real PMS shows for an on-disk sidecar: the shape of a real 1.43 library's `.en.srt`
(`fixtures/decisions/rows.json`, `zen:19813`). Its `key` serves the text, as an upload's does.

Then what the field promises across pushes (`docs/feed-api.md`): the same path keeps its id
through a re-push, a new probe and a restart; a changed `size` reads the text again and an
unchanged one never does; `[]` removes the source's subtitles and nothing else, an absent field
keeps them; an entry rex cannot use is left out, logged once, and never costs the item; a file
the source cannot serve yet arrives on a later push; a person's upload is never touched, and a
DELETE of a source's subtitle is refused as an embedded track's is. Two releases of one title
each keep their own.

    python3 tests/test_feed_subtitles.py
"""
import copy
import http.server
import json
import os
import shutil
import sys
import tempfile
import urllib.parse

from test_feed import BYTES_TOKEN, BODY, FIXTURES, MOUNT, Server, catalog, check, item, results, serve
import test_feed_versions as versions

SINTEL = "__magic__/movies/Sintel (2010)"
VIDEO = SINTEL + "/Sintel (2010).mkv"
ENGLISH = SINTEL + "/Sintel (2010).en.srt"
SDH = SINTEL + "/Sintel (2010).en.sdh.srt"
GERMAN = SINTEL + "/Sintel (2010).de.srt"
LATE = SINTEL + "/Sintel (2010).fr.srt"
HUGE = SINTEL + "/Sintel (2010).es.srt"
SRT = "1\n00:00:01,000 --> 00:00:02,500\nThe dragon ♪\n\n".encode()
# Windows-1252, as many a fan-made SRT is: read as rex reads a downloaded one.
CP1252 = "1\n00:00:01,000 --> 00:00:02,000\nÇa va, Sintel\n\n".encode("cp1252")
MEASURED = next(stream for stream in json.load(open(os.path.join(FIXTURES, "decisions", "rows.json")))["rows"]
                ["zen:19813"]["Media"]["Part"][0]["Stream"] if stream.get("key"))


class Files:
    """A source serving each of `files` whole, behind the bearer token, counting its reads."""

    def __init__(self):
        self.files = {}
        self.seen = []

    def handler(source):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(self.path)
                source.seen.append((path, self.headers.get("Authorization")))
                body = source.files.get(path) if self.headers.get("Authorization") == "Bearer " + BYTES_TOKEN else None
                status = 401 if self.headers.get("Authorization") != "Bearer " + BYTES_TOKEN else 200 if body is not None else 404
                body = body if status == 200 else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        return Handler

    def reads(self, path):
        return sum(1 for seen in self.seen if seen[0] == "/dav/" + path)


def sintel(subtitles=None, **extra):
    """An item nothing places, as `__magic__` holds an *arr's import, with its sidecars."""
    pushed = dict(item("new"), id=VIDEO, path=VIDEO, file="%s/%s" % (MOUNT, VIDEO), name="Sintel (2010)",
                  release=SINTEL, hash="c" * 40)
    if subtitles is not None:
        pushed["subtitles"] = subtitles
    pushed.update(extra)
    return pushed


def entry(path, body, **fields):
    return dict({"path": path, "size": len(body)}, **fields)


def externals(row, version=0):
    media = (row.get("Media") or [])[version:version + 1] or [{}]
    return [stream for stream in ((media[0].get("Part") or [{}])[0].get("Stream") or []) if stream.get("key")]


def by_path(server, key):
    return {stream.get("displayTitle"): stream for stream in externals(server.row(key))}


def logged(work, text):
    return open(os.path.join(work, "server.log"), errors="replace").read().count(text)


def main():
    source = Files()
    for pushed in (sintel(), versions.item(0), versions.item(1)):
        source.files["/dav/" + pushed["path"]] = BODY
    source.files["/dav/" + ENGLISH] = SRT
    source.files["/dav/" + SDH] = SRT
    source.files["/dav/" + GERMAN] = CP1252
    source.files["/dav/" + HUGE] = b"x" * (4 * 1024 * 1024 + 1)
    port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-feed-subtitles-")
    catalog(work)
    server = Server(work, port)
    try:
        # -- a sidecar is the measured on-disk external stream -------------------------------
        status, body = server.feed("POST", "items", {"items": [sintel([entry(ENGLISH, SRT, language="en")])]})
        key = server.keys().get(VIDEO)
        streams = externals(server.row(key))
        check("an item with a sidecar is created", status == 200 and body.get("created") == 1 and key, (status, body))
        stream = streams[0] if streams else {}
        print("      the stream: %s" % json.dumps(stream, ensure_ascii=False))
        check("its sidecar is an external stream of its Part", len(streams) == 1, streams)
        # `canAutoSync` is the runtime's own projection for every text subtitle, an upload's too
        # (whether rex could re-time it against this release); zen's PMS had nothing to time it by.
        check("in a real PMS's shape for an on-disk .en.srt: the same fields, the same values",
              set(stream) == set(MEASURED) - {"selected"} and isinstance(stream.get("canAutoSync"), bool)
              and all(stream.get(field) == MEASURED[field] for field in MEASURED
                      if field not in ("id", "key", "selected", "canAutoSync")),
              (stream, MEASURED))
        check("keyed /library/streams/{id}, unselected with no account settings to select it",
              stream.get("key") == "/library/streams/%s" % stream.get("id") and "selected" not in stream, stream)
        reads = [seen for seen in source.seen if seen[0] == "/dav/" + ENGLISH]
        check("its text was read at <url>/<path> with the source's bearer",
              reads == [("/dav/" + ENGLISH, "Bearer " + BYTES_TOKEN)], reads)
        status, raw = server.request(stream.get("key", "/none"))
        check("its key serves the sidecar's text", status == 200 and raw == SRT, (status, str(raw)[:80]))
        sid = stream.get("id")

        # -- kept: no read again, the same id through a push, a new probe and a restart ------
        status, body = server.feed("POST", "items", {"items": [sintel([entry(ENGLISH, SRT, language="en")])]})
        check("the same push again changes nothing and reads nothing",
              body.get("unchanged") == 1 and source.reads(ENGLISH) == 1, (body, source.reads(ENGLISH)))
        probed = sintel([entry(ENGLISH, SRT, language="en")])
        probed["media_info"] = copy.deepcopy(probed["media_info"])
        probed["media_info"]["chapters"] = [{"start_time": "0", "end_time": "60", "tags": {"title": "Opening"}}]
        status, body = server.feed("POST", "items", {"items": [probed]})
        streams = externals(server.row(key))
        check("a push with a new probe keeps the sidecar on its id",
              body.get("updated") == 1 and [s.get("id") for s in streams] == [sid] and source.reads(ENGLISH) == 1,
              (body, streams))
        server.stop()
        server = Server(work, port)
        streams = externals(server.row(key))
        status, raw = server.request("/library/streams/%s" % sid)
        check("a restart keeps it on its id, serving its text", [s.get("id") for s in streams] == [sid]
              and status == 200 and raw == SRT, (streams, status))

        # -- a changed size reads the text again, on the same id -----------------------------
        changed = SRT + b"2\n00:00:03,000 --> 00:00:04,000\nMore\n\n"
        source.files["/dav/" + ENGLISH] = changed
        status, body = server.feed("POST", "items", {"items": [sintel([entry(ENGLISH, changed, language="en")])]})
        status, raw = server.request("/library/streams/%s" % sid)
        check("a new size reads the text again, on the same id",
              body.get("updated") == 1 and source.reads(ENGLISH) == 2 and raw == changed
              and [s.get("id") for s in externals(server.row(key))] == [sid], (body, source.reads(ENGLISH), str(raw)[-20:]))

        # -- more of them, flags and languages as the source says ----------------------------
        sidecars = [entry(ENGLISH, changed, language="en"), entry(SDH, SRT, language="eng", hearing_impaired=True),
                    entry(GERMAN, CP1252, language="ger", forced=True)]
        server.feed("POST", "items", {"items": [sintel(sidecars)]})
        named = by_path(server, key)
        check("an ISO 639-2 bibliographic code is its language (ger -> Deutsch), a forced flag is the stream's",
              (named.get("Deutsch Forced") or {}).get("languageCode") == "deu"
              and (named.get("Deutsch Forced") or {}).get("forced") is True
              and (named.get("Deutsch Forced") or {}).get("extendedDisplayTitle") == "Deutsch Forced (SRT External)", named)
        check("hearing_impaired is the stream's hearingImpaired (English SDH)",
              (named.get("English SDH") or {}).get("hearingImpaired") is True, list(named))
        status, raw = server.request((named.get("Deutsch Forced") or {}).get("key", "/none"))
        check("a Windows-1252 sidecar is served as UTF-8 text", status == 200 and raw == CP1252.decode("cp1252").encode(),
              (status, raw))
        check("the first keeps its id among them", sid and (named.get("English") or {}).get("id") == sid, named)

        # -- an upload stays a person's; a source's sidecar is the source's -----------------
        status, _ = server.request("/library/metadata/%s/subtitles?title=mine.srt&format=srt" % key, "POST", SRT)
        upload = next((s for s in externals(server.row(key)) if s.get("title") == "mine"), {})
        check("a person's upload joins them", status == 200 and upload.get("id"), (status, externals(server.row(key))))
        status, _ = server.request("/library/streams/%s" % sid, "DELETE")
        check("a DELETE of a source's sidecar is refused as an embedded track's is (403), and it stays",
              status == 403 and sid in [s.get("id") for s in externals(server.row(key))], status)
        status, body = server.feed("POST", "items", {"items": [sintel()]})
        check("an item pushed without subtitles keeps them", body.get("unchanged") == 1 and len(externals(server.row(key))) == 4,
              (body, externals(server.row(key))))
        server.feed("POST", "items", {"items": [dict(sintel(), subtitles=None)]})
        check("and so does subtitles: null", len(externals(server.row(key))) == 4, externals(server.row(key)))
        status, body = server.feed("POST", "items", {"items": [sintel([])]})
        left = externals(server.row(key))
        check("subtitles: [] removes the source's sidecars and leaves the upload as it was",
              body.get("updated") == 1 and left == [upload], (body, left))
        status, body = server.feed("POST", "items", {"items": [sintel(sidecars)]})
        left = externals(server.row(key))
        check("pushed again they come back after the upload, which is untouched",
              left[:1] == [upload] and len(left) == 4 and source.reads(ENGLISH) == 3, left)
        english = (by_path(server, key).get("English") or {}).get("id")

        # -- what rex cannot use is left out, said once, and the item stays ------------------
        bad = [{"path": SINTEL + "/Sintel (2010).en.vtt"}, {"path": 7}, {"language": "en"},
               {"path": SINTEL + "/a.srt", "size": "big"}, {"path": SINTEL + "/b.srt", "language": 7},
               {"path": SINTEL + "/c.srt", "forced": "yes"}, {"path": "../x.srt"},
               {"path": HUGE, "size": 4 * 1024 * 1024 + 1}, entry(ENGLISH, changed, language="en")]
        status, body = server.feed("POST", "items", {"items": [sintel(bad)]})
        left = externals(server.row(key))
        check("an item with entries rex cannot use is still taken, keeping the usable one",
              status == 200 and not body.get("rejected") and [s.get("id") for s in left] == [upload.get("id"), english], (body, left))
        check("an entry over 4 MiB by its size is never read", source.reads(HUGE) == 0, source.reads(HUGE))
        said = logged(work, "left out")
        server.feed("POST", "items", {"items": [sintel(bad)]})
        check("each left out is logged with why, once", said == 8 and logged(work, "left out") == said, said)
        server.feed("POST", "items", {"items": [sintel([{"path": HUGE}, entry(ENGLISH, changed, language="en")])]})
        check("a text over 4 MiB with no size is read, refused and logged",
              source.reads(HUGE) == 1 and len(externals(server.row(key))) == 2 and logged(work, "upstream body too large") == 1,
              (source.reads(HUGE), externals(server.row(key))))
        check("a subtitles value that is no list keeps the row's, logged",
              server.feed("POST", "items", {"items": [sintel("Sintel.en.srt")]})[0] == 200
              and len(externals(server.row(key))) == 2 and logged(work, "subtitles must be a list") == 1, externals(server.row(key)))

        # -- a sidecar the source cannot serve yet comes with a later push -------------------
        late = [entry(ENGLISH, changed, language="en"), entry(LATE, SRT, language="fr")]
        status, body = server.feed("POST", "items", {"items": [sintel(late)]})
        check("one the source answers 404 for is absent, the item fine, the failure logged",
              status == 200 and "Français" not in by_path(server, key) and logged(work, "not read: the source answered 404") == 1,
              (body, list(by_path(server, key))))
        source.files["/dav/" + LATE] = SRT
        status, body = server.feed("POST", "items", {"items": [sintel(late)]})
        check("the next push brings it, counted as an update", body.get("updated") == 1 and "Français" in by_path(server, key),
              (body, list(by_path(server, key))))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- two releases of one title: each Part its own ----------------------------------------
    work = tempfile.mkdtemp(prefix="rex-feed-subtitles-")
    versions.catalog(work)
    server = Server(work, port)
    try:
        one, two = versions.item(0), versions.item(1)
        for pushed, language in ((one, "en"), (two, "de")):
            path = pushed["path"].rsplit(".", 1)[0] + ".%s.srt" % language
            source.files["/dav/" + path] = SRT
            pushed["subtitles"] = [entry(path, SRT, language=language)]
        server.feed("POST", "items", {"items": [one]})
        server.feed("POST", "items", {"items": [two]})
        key = server.keys().get(one["id"])
        row = server.row(key)
        check("two releases of one title are one row, each Part with its own sidecar",
              server.keys().get(two["id"]) == key and len(row.get("Media") or []) == 2
              and [s.get("languageTag") for s in externals(row, 0)] == ["en"]
              and [s.get("languageTag") for s in externals(row, 1)] == ["de"],
              [[s.get("languageTag") for s in externals(row, n)] for n in range(len(row.get("Media") or []))])
        two["subtitles"] = []
        server.feed("POST", "items", {"items": [two]})
        row = server.row(key)
        check("a release's [] empties its own Part only",
              [s.get("languageTag") for s in externals(row, 0)] == ["en"] and externals(row, 1) == [],
              [[s.get("languageTag") for s in externals(row, n)] for n in range(2)])
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    failed = results.count(False)
    print("\n%d/%d feed subtitle checks passed" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
