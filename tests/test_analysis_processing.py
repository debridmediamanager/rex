#!/usr/bin/env python3
"""Real generated source HTTP -> manual analysis -> Plex artifacts, state and ACLs.
Requires rex, FFmpeg and ffprobe. No external service or production credentials.
"""
import contextlib
import http.server
import json
import os
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import test_native_transcode as t
from test_accounts import MACHINE, SERVER_TOKEN, OWNER, SHARED, PlexTv


class Source(t.Zurg):
    entered = threading.Event()
    release = threading.Event()
    block = False
    fail_revision = False

    def do_GET(self):
        if type(self).fail_revision and "/new-revision/" in self.path:
            self.send_response(503)
            self.send_header("Content-Length","0")
            self.end_headers()
            return
        if self.headers.get("Authorization") != "Bearer analysis-source-secret":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if type(self).block:
            type(self).entered.set()
            assert type(self).release.wait(30), "test source was not released"
        super().do_GET()


class Server:
    def __init__(self, directory, plex_port, transcoder=True):
        self.port = t.free_port()
        self.log = open(os.path.join(directory, "analysis.log"), "ab")
        env = dict(os.environ, REX_TRANSCODER=os.path.join(directory,"ffmpeg-engine"), REX_TRANSCODE_HW="none",
                   REX_PLEXTV_URL="http://127.0.0.1:%d" % plex_port, REX_DIRECT_URL="off")
        if not transcoder:
            env.pop("REX_TRANSCODER", None)
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN"):
            env.pop(name, None)
        self.process = subprocess.Popen(t.server_command(
            "--port", self.port, "--catalog", os.path.join(directory, "catalog.json"),
            "--state", os.path.join(directory, "state.db"), "--gdm", "off", root=t.ROOT),
            cwd=t.ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        t.poll_until(lambda: self.ask("/library/sections")[0] == 200, 40)

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=30)
        self.log.close()

    def ask(self, path, method="GET", token=OWNER, params=(), headers=None):
        pairs = list(params) + [("X-Plex-Token", token)]
        url = "http://127.0.0.1:%d%s?%s" % (self.port, path, urllib.parse.urlencode(pairs))
        req = urllib.request.Request(url, method=method, headers=dict({"Accept": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()
        except (OSError, urllib.error.URLError):
            return 0, {}, b""

    def detail(self, key="1"):
        status, _, raw = self.ask("/library/metadata/" + key,
                                  params=[("includeMarkers", "1"), ("includeChapters", "1")])
        assert status == 200, (status, raw)
        return json.loads(raw)["MediaContainer"]["Metadata"][0]

    def analyze(self, key="1"):
        status, _, raw = self.ask("/library/metadata/%s/analyze" % key, method="PUT")
        assert status == 200, (status, raw)


class Events:
    """Read the same actual SSE activity channel captured from the disposable PMS."""

    def __init__(self, server):
        self.notes = []
        self.response = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/:/eventsource/notifications?filters=activity" % server.port,
            headers={"Accept": "text/event-stream", "X-Plex-Token": OWNER}), timeout=3600)

        def read():
            try:
                for line in self.response:
                    if line.startswith(b"data:"):
                        frame = json.loads(line[5:])
                        note = frame.get("ActivityNotification")
                        if note and note["Activity"].get("type") == "media.analysis":
                            self.notes.append(note)
            except (OSError, ValueError):
                pass
        threading.Thread(target=read, daemon=True).start()

    def assert_failed(self, key):
        with open(os.path.join(t.ROOT, "tests", "fixtures", "analysis_failure_s1435.json")) as source:
            reference = json.load(source)["notifications"]
        t.poll_until(lambda: any(note["event"] == "ended" and
                     str(note["Activity"].get("Context", {}).get("ratingKey")) == key
                     for note in self.notes), 30)
        notes = [note for note in self.notes
                 if str(note["Activity"].get("Context", {}).get("ratingKey")) == key]
        # PMS reports this failed Analyze through its chapter-thumbnail activity;
        # compare the failure lifecycle, not operation-specific labels/context.
        def lifecycle(note):
            activity = note["Activity"]
            return (note["event"], activity["progress"], activity["cancellable"],
                    activity["userID"], "error" in activity, activity.get("error"))
        assert [lifecycle(note) for note in notes] == [
            lifecycle(frame["ActivityNotification"]) for frame in reference], notes
        assert len({note["uuid"] for note in notes}) == 1, notes
        assert all(note["uuid"] == note["Activity"]["uuid"] for note in notes), notes

def ffmpeg(*args):
    return subprocess.run([t.FFMPEG, "-v", "error", "-nostdin", *args],
                          check=True, capture_output=True, timeout=90).stdout


def ready(server, key="1"):
    detail = server.detail(key)
    part = detail["Media"][0]["Part"][0]
    return ("sd" in part.get("indexes", "").split(",") and
            any(m["type"] == "intro" for m in detail.get("Marker", [])) and
            all(c.get("thumb") for c in detail.get("Chapter", [])) and bool(detail.get("Chapter")))


def state(server, key):
    status,_,raw = server.ask("/rex/analysis")
    assert status == 200, (status,raw)
    return next((job for job in json.loads(raw)["jobs"] if str(job["ratingKey"]) == key),{})


def selections(detail):
    return [(str(part["id"]),str(stream["id"]),stream.get("selected"))
            for media in detail["Media"] for part in media["Part"]
            for stream in part.get("Stream",[]) if stream.get("streamType") in (2,3)]


def valid_jpeg(raw):
    assert raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9")
    subprocess.run([t.FFMPEG, "-v", "error", "-nostdin", "-f", "image2pipe", "-i", "pipe:0",
                    "-frames:v", "1", "-f", "null", "-"], input=raw, check=True,
                   capture_output=True, timeout=30)


def queue_artifacts(server):
    # Plex Web's Chapter selector reads queue metadata, not the separate detail
    # response. Timings without image URLs render empty cells despite valid JPEGs.
    status, _, raw = server.ask("/playQueues", method="POST", params=[
        ("type", "video"), ("includeChapters", "1"), ("includeMarkers", "1"),
        ("uri", "server://%s/com.plexapp.plugins.library/library/metadata/1" % MACHINE)])
    assert status == 200, (status, raw)
    item = json.loads(raw)["MediaContainer"]["Metadata"][0]
    assert any(marker["type"] == "intro" for marker in item.get("Marker", [])), item
    for offset in (0, 10000):
        chapter = next(chapter for chapter in item["Chapter"] if chapter["startTimeOffset"] == offset)
        assert chapter.get("thumb"), ("play queue lost generated chapter image", chapter)
        status, _, image = server.ask("/photo/:/transcode", params=[
            ("url", chapter["thumb"]), ("width", "180"), ("height", "102"), ("minSize", "1")])
        assert status == 200, (status, chapter)
        valid_jpeg(image)
    status, _, _ = server.ask("/playQueues", method="POST", token=SHARED, params=[
        ("type", "video"), ("includeChapters", "1"),
        ("uri", "server://%s/com.plexapp.plugins.library/library/metadata/1" % MACHINE)])
    assert status in (403, 404), status


def artifacts(server):
    detail = server.detail()
    part = detail["Media"][0]["Part"][0]
    base = "/library/parts/%s/indexes/sd" % part["id"]
    status, headers, bif = server.ask(base)
    assert status == 200 and bif[:8] == b"\x89BIF\r\n\x1a\n", (status, bif[:100])
    version, count, multiplier = struct.unpack_from("<III", bif, 8)
    assert version == 0 and count >= 4 and multiplier > 0
    table = [struct.unpack_from("<II", bif, 64 + 8*i) for i in range(count + 1)]
    assert table[-1][0] == 0xffffffff and table[-1][1] == len(bif)
    for index in range(count):
        valid_jpeg(bif[table[index][1]:table[index+1][1]])
    status, _, preview = server.ask(base + "/10000")
    assert status == 200
    valid_jpeg(preview)
    selected = max(i for i in range(count) if table[i][0] * multiplier <= 10000)
    assert preview == bif[table[selected][1]:table[selected+1][1]]
    chapter_pixels = []
    for chapter in detail["Chapter"]:
        status, _, image = server.ask(chapter["thumb"])
        assert status == 200
        valid_jpeg(image)
        pixels = subprocess.run([t.FFMPEG,"-v","error","-nostdin","-f","image2pipe","-i","pipe:0",
                                 "-frames:v","1","-vf","scale=1:1","-pix_fmt","rgb24","-f","rawvideo","-"],
                                input=image,check=True,capture_output=True,timeout=30).stdout
        chapter_pixels.append(pixels)
        assert server.ask(chapter["thumb"], token=SHARED)[0] in (403, 404)
    assert len(chapter_pixels[0]) == 3
    red, green, blue = chapter_pixels[0]
    assert blue > red + 80 and blue > green + 80, chapter_pixels
    assert chapter_pixels[1] != chapter_pixels[0], "chapter boundaries must select source frames, not one fixed JPEG"
    assert server.ask(base, token=SHARED)[0] in (403, 404)
    assert server.ask(base + "/10000", token=SHARED)[0] in (403, 404)
    status, headers, partial = server.ask(base, headers={"Range": "bytes=0-63"})
    assert status == 206 and partial == bif[:64]
    return base, bif


def main():
    assert t.FFMPEG and t.FFPROBE, "FFmpeg and ffprobe required"
    with tempfile.TemporaryDirectory(prefix="rex-analysis-http-") as directory, contextlib.ExitStack() as stack:
        # Instrument actual FFmpeg process starts, then exec the real decoder;
        # this proves season processing reuses the group's marker computation.
        engine = os.path.join(directory,"ffmpeg-engine")
        calls = os.path.join(directory,"ffmpeg-calls.jsonl")
        with open(engine,"w") as out:
            out.write("#!/usr/bin/env python3\nimport os,sys,json\n"
                      "fd=os.open(%r,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)\n"
                      "os.write(fd,(json.dumps(sys.argv[1:])+'\\n').encode());os.close(fd)\n"
                      "os.execv(%r,[%r]+sys.argv[1:])\n" % (calls,t.FFMPEG,t.FFMPEG))
        os.chmod(engine,0o700)
        rows = []
        for key, offset, color in (("1", 3, "blue"), ("2", 9, "red")):
            name = "episode-%s.mkv" % key
            path = os.path.join(directory, name)
            ffmpeg("-y", "-f", "lavfi", "-i", "color=%s:s=160x90:r=8:d=%d" % (color, offset),
                   "-f", "lavfi", "-i", "testsrc2=s=160x90:r=8:d=16", "-f", "lavfi", "-i",
                   "color=%s:s=160x90:r=8:d=%d" % (color, 40-offset-16),
                   "-f", "lavfi", "-i", "anullsrc=r=2000:cl=mono:d=40",
                   "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
                   "-c:v", "ffv1", "-c:a", "pcm_s16le", "-threads", "1", path)
            t.MEDIA[name] = path
            row = t.row(key, "Source placed episode", name, t.probe(path)["streams"], os.path.getsize(path), 40)
            row.update(type="episode", parentRatingKey="10", grandparentRatingKey="9", parentIndex=1,
                       index=int(key), librarySectionID=99, librarySectionKey="/library/sections/99")
            row["Media"][0]["_chapters"] = [{"index":1,"startTimeOffset":0,"endTimeOffset":10000},
                                          {"index":2,"startTimeOffset":10000,"endTimeOffset":40000}]
            rows.append(row)
        failed = json.loads(json.dumps(rows[0]))
        failed.update(ratingKey="3", key="/library/metadata/3", parentRatingKey="different-season")
        failed["Media"][0]["Part"][0].update(id=333, file="/zurg/missing.mkv", key="/library/parts/333/file.mkv")
        rows += [failed, {"ratingKey":"9","type":"show","title":"Source show","librarySectionID":99},
                 {"ratingKey":"10","type":"season","parentRatingKey":"9","index":1,"librarySectionID":99}]
        with open(os.path.join(directory,"catalog.json"),"w") as out:
            json.dump({"machineIdentifier":MACHINE,"sections":[
                {"key":"99","type":"show","title":"Private","Location":[{"id":99,"path":"/zurg"}]},
                {"key":"100","type":"movie","title":"Spare","Location":[{"id":100,"path":"/unused"}]}
            ],"items":rows},out)
        with open(os.path.join(directory,"claim_state.json"),"w") as out:
            json.dump({"machine":MACHINE,"token":SERVER_TOKEN,"owner":"owner","email":"owner@example.com",
                       "feature_uuids":[],"account_id":316687974,"view_state_cursor":None,"access_tokens":[]},out)
        upstream = stack.enter_context(socketserver.ThreadingTCPServer(("127.0.0.1",0),Source))
        upstream.daemon_threads = True
        plex = stack.enter_context(http.server.ThreadingHTTPServer(("127.0.0.1",0),PlexTv))
        for service in (upstream,plex):
            threading.Thread(target=service.serve_forever,daemon=True).start()
            stack.callback(service.shutdown)
        # Missing engine diagnostics remain useful privately, never as PMS fields.
        server = Server(directory, plex.server_address[1], transcoder=False)
        try:
            t.register_source(server.port, "zurg", {
                "url": "http://127.0.0.1:%d/dav" % upstream.server_address[1],
                "mount": "/zurg", "bearer": "analysis-source-secret"})
            events = Events(server)
            server.analyze()
            t.poll_until(lambda: state(server, "1").get("state") == "failed", 30)
            events.assert_failed("1")
        finally:
            server.close()
        server = Server(directory,plex.server_address[1])
        try:
            source = {"url":"http://127.0.0.1:%d/dav" % upstream.server_address[1],"mount":"/zurg","bearer":"analysis-source-secret"}
            t.register_source(server.port,"zurg",source)
            assert "indexes" not in server.detail()["Media"][0]["Part"][0]
            pristine = server.detail()
            selected_tracks = selections(pristine)
            assert not any(media.get("hasVoiceActivity") for media in pristine["Media"])
            server.analyze("10")
            t.poll_until(lambda:ready(server) and ready(server,"2"),120)
            queue_artifacts(server)
            with open(calls) as log:
                invocations = [json.loads(line) for line in log]
            intro_reads = [args for args in invocations if any("fps=2,scale=32:18" in arg for arg in args)]
            assert len(intro_reads) == 2, "a two-episode season must decode each intro exactly once: %r" % intro_reads
            voice_reads = sum("highpass=f=300,lowpass=f=3400" in args for args in invocations)
            assert voice_reads > 0, "silent audio must actually be measured, not inferred from missing flags"
            assert not any(media.get("hasVoiceActivity") for media in server.detail()["Media"]), "silence is not voice activity"
            assert selections(server.detail()) == selected_tracks
            marker = next(m for m in server.detail()["Marker"] if m["type"] == "intro")
            assert abs(marker["startTimeOffset"]-3000) <= 1000 and abs(marker["endTimeOffset"]-19000) <= 1000
            base,bif = artifacts(server)
            before = server.detail()
            Source.block = True; Source.entered.clear(); Source.release.clear()
            server.analyze()
            assert Source.entered.wait(10)
            assert server.ask("/library/sections/99/indexes",method="DELETE")[0] == 200
            after = server.detail()
            assert "indexes" not in after["Media"][0]["Part"][0]
            assert after["Marker"] == before["Marker"] and after["Chapter"] == before["Chapter"]
            assert server.ask(base)[0] == 404
            for chapter in after["Chapter"]:
                status,_,image = server.ask(chapter["thumb"])
                assert status == 200
                valid_jpeg(image)
            Source.block = False; Source.release.set()
            t.poll_until(lambda:state(server,"1").get("state") == "completed",120)
            assert "indexes" not in server.detail()["Media"][0]["Part"][0], "a deleted in-flight BIF must not resurrect"
            assert server.ask(base)[0] == 404
            server.analyze()
            t.poll_until(lambda:ready(server),120)
            with open(calls) as log:
                repeated = [json.loads(line) for line in log]
                assert sum(any("fps=2,scale=32:18" in arg for arg in args) for args in repeated) == 2
                assert sum("highpass=f=300,lowpass=f=3400" in args for args in repeated) == voice_reads, "repeat Analyze must reuse measured silence"
            assert not any(media.get("hasVoiceActivity") for media in server.detail()["Media"])
            assert selections(server.detail()) == selected_tracks
            base,bif = artifacts(server)
            server.close()
            server = Server(directory,plex.server_address[1])
            t.poll_until(lambda:ready(server),30)
            assert artifacts(server)[1] == bif
            # A source-registration mutation immediately invalidates old artifacts,
            # even before a replacement worker finishes; unrelated requests stay live.
            Source.block = True; Source.entered.clear(); Source.release.clear()
            server.analyze()
            assert Source.entered.wait(10)
            before = time.monotonic()
            assert server.ask("/identity")[0] == 200 and time.monotonic()-before < 2
            Source.fail_revision = True
            source["url"] += "/new-revision"
            t.register_source(server.port,"zurg",source)
            assert "indexes" not in server.detail()["Media"][0]["Part"][0]
            assert server.ask(base)[0] == 404
            Source.block = False; Source.release.set()
            # The replacement source refuses media. Waiting for that job's real
            # failure proves the old blocked worker has finished on the one lane;
            # none of its now-stale output may have been published.
            server.analyze()
            t.poll_until(lambda:state(server,"1").get("state") == "failed",120)
            assert state(server,"1").get("error")
            rejected = server.detail()
            assert "indexes" not in rejected["Media"][0]["Part"][0]
            assert not rejected.get("Marker")
            assert all("thumb" not in chapter for chapter in rejected.get("Chapter",[]))
            assert server.ask(base)[0] == 404
            Source.fail_revision = False
            server.analyze()
            t.poll_until(lambda:ready(server),120)
            artifacts(server)
            events = Events(server)
            server.analyze("3")
            t.poll_until(lambda:state(server,"3").get("state") == "failed",30)
            assert state(server,"3").get("error")
            events.assert_failed("3")
            bad = server.detail("3")
            assert "indexes" not in bad["Media"][0]["Part"][0]
            assert not bad.get("Marker")
            assert all("thumb" not in c for c in bad.get("Chapter",[]))
            assert server.ask("/library/parts/333/indexes/sd")[0] == 404
            # Corrupting a persisted artifact must remove the advertisement at once
            # and must not be served, then remain invalid after restoration.
            base,_ = artifacts(server)
            bifs = []
            for root,_,files in os.walk(os.path.join(directory,"state.db.analysis")):
                bifs += [os.path.join(root,name) for name in files if name.endswith(".bif")]
            assert bifs
            for path in bifs:
                with open(path,"wb") as out: out.write(b"corrupt")
            assert "indexes" not in server.detail()["Media"][0]["Part"][0]
            assert server.ask(base)[0] == 404
            server.close()
            server = Server(directory,plex.server_address[1])
            t.poll_until(lambda:state(server,"3").get("state") == "failed",30)
            assert "indexes" not in server.detail()["Media"][0]["Part"][0]
            assert server.ask(base)[0] == 404
            # Removing a library retires its generated data, not only its metadata URLs.
            status,_,response = server.ask("/library/sections/99",method="DELETE")
            assert status == 200, (status,response)
            assert server.ask("/library/metadata/1")[0] == 404
            assert server.ask(base)[0] == 404
            cache = os.path.join(directory,"state.db.analysis")
            t.poll_until(lambda: not any(files for _,_,files in os.walk(cache)),10)
            assert not any(files for _,_,files in os.walk(cache)), "deleted library still owns generated files"
        finally:
            Source.block = False; Source.fail_revision = False; Source.release.set()
            server.close()
    print("generated media analysis HTTP/persistence/ACL regressions passed")


if __name__ == "__main__":
    main()
