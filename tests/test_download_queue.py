"""Offline downloads: Plex for Android's download flow against rex, checked against what a
claimed 1.42.2 PMS answered for the same requests (tests/fixtures/download_queue_s1422.json).

The reference converts every item to one mp4 (the profile's `static` target), one at a time:
`waiting` -> `processing` (with its TranscodeSession) -> `available`; the file is an attachment
named `<item>.mp4` read whole or by Range; a key the queue holds answers its own item; deletes
are 204. Needs ffmpeg (REX_TRANSCODER, else PATH's), as test_native_transcode.py does.
"""

import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import test_native_transcode as transcode
from test_native_transcode import FFMPEG, FFPROBE, MEDIA, ROOT, Zurg, check, make_clip, probe, results, row, start_server
from test_ports import free_port

FIXTURE = json.load(open(os.path.join(ROOT, "tests/fixtures/download_queue_s1422.json")))
RECORDED = {row_["label"]: row_ for row_ in FIXTURE["default_params"]}
APP = {name: value for name, value in RECORDED["create"]["req_headers"]}


def call(port, method, path, query=(), range_=None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if query:
        url += "?" + urllib.parse.urlencode([tuple(pair) for pair in query])
    headers = dict(APP, **({"Range": range_} if range_ else {}))
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, list(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, list(error.headers.items()), error.read()


def body(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def names(headers):
    return [name for name, _ in headers]


def streams_of(path):
    try:
        return probe(path)
    except subprocess.CalledProcessError:
        return {"streams": [], "format": {}}


def poll_items(port, queue, until, timeout=180):
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, _, raw = call(port, "GET", "/downloadQueue/%s/items" % queue)
        items = (body(raw) or {}).get("MediaContainer", {}).get("DownloadQueueItem", [])
        seen.append(items)
        if until(items):
            break
        time.sleep(0.5)
    return seen


def start_persistent(catalog, zurg_port, scratch):
    port = free_port()
    environment = dict(os.environ, REX_TRANSCODER=FFMPEG, REX_TRANSCODE_HW="none",
                       REX_TRANSCODE_DIR=os.path.join(scratch, "restart-cleared-transcoder"))
    log = open(os.path.join(scratch, "persistent-%d.log" % port), "w")
    process = subprocess.Popen(transcode.server_command(
        "--port", port, "--catalog", catalog, "--state", os.path.join(scratch, "downloads.db"),
        "--assert-claimed", root=ROOT), cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
    def ready():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2).close()
            return True
        except Exception:
            return False
    transcode.poll_until(ready, 40)
    transcode.register_source(port, "zurg", {"url": "http://127.0.0.1:%d/dav" % zurg_port, "mount": "/zurg"})
    return port, process, log.name


def main():
    if not FFMPEG or not FFPROBE:
        print("SKIP: no ffmpeg (set REX_TRANSCODER)")
        return 0
    scratch = tempfile.mkdtemp(prefix="rex-download-test-")
    # The recording's library: 30 s 1280x720 h264/aac mkv clips.
    h264 = ["-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p"]
    items = []
    for key, name in (("1", "Bunny.mkv"), ("4", "Train.mkv"), ("5", "Bushwhacked.mkv")):
        MEDIA[name] = os.path.join(scratch, name)
        make_clip(MEDIA[name], h264, ["-c:a", "aac", "-ac", "2"], 30)
        items.append(row(key, name.split(".")[0], name, probe(MEDIA[name])["streams"], os.path.getsize(MEDIA[name]), 30))
    catalog = os.path.join(scratch, "catalog.json")
    json.dump({"machineIdentifier": "0000000000000000000000000000000000000508", "friendlyName": "rex-downloads",
               "sections": [{"key": "1", "type": "movie", "title": "Clips", "uuid": "0" * 36 + "508"}],
               "items": items}, open(catalog, "w"))
    zurg_port = free_port()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", zurg_port), Zurg)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print("with an encoder")
    port, process, log = start_persistent(catalog, zurg_port, scratch)
    try:
        status, _, raw = call(port, "POST", "/downloadQueue")
        queue = (body(raw) or {}).get("MediaContainer", {}).get("DownloadQueue", [{}])[0]
        check("a client's queue is created as recorded",
              status == 200 and sorted(queue) == sorted(RECORDED["create"]["resp_json"]["MediaContainer"]["DownloadQueue"][0]),
              raw[:200])
        qid = queue.get("id")
        status, _, raw = call(port, "POST", "/downloadQueue/%s/add" % qid, RECORDED["add-original"]["query"])
        added = (body(raw) or {}).get("MediaContainer", {})
        recorded = RECORDED["add-original"]["resp_json"]["MediaContainer"]
        check("add answers AddedQueueItems {key, id}, as recorded",
              status == 200 and added.get("size") == 1 and [sorted(i) for i in added.get("AddedQueueItems", [])]
              == [sorted(i) for i in recorded["AddedQueueItems"]] and added["AddedQueueItems"][0]["key"] == "/library/metadata/1",
              raw[:200])
        first = added.get("AddedQueueItems", [{}])[0].get("id")
        status, _, raw = call(port, "POST", "/downloadQueue/%s/add" % qid, RECORDED["add-720kbps"]["query"])
        capped = ((body(raw) or {}).get("MediaContainer", {}).get("AddedQueueItems") or [{}])[0].get("id")
        status, _, raw = call(port, "POST", "/downloadQueue/%s/add" % qid, RECORDED["add-two-keys"]["query"])
        two = (body(raw) or {}).get("MediaContainer", {}).get("AddedQueueItems", []) or [{}, {}]
        check("a key the queue holds answers its own item (recorded: key 1 twice is id 1)",
              len(two) == 2 and two[1] == {"key": "/library/metadata/1", "id": first} and two[0].get("id") not in (first, capped),
              raw[:200])
        seen = poll_items(port, qid, lambda rows: len(rows) == 3 and all(r.get("status") == "available" for r in rows))
        final = seen[-1] if seen else []
        check("every item is converted and ends available", len(final) == 3 and all(r.get("status") == "available" for r in final),
              [r.get("status") for r in final])
        recorded_final = RECORDED["items-6"]["resp_json"]["MediaContainer"]["DownloadQueueItem"][0]
        check("an item row has the recorded fields and DecisionResult",
              final and sorted(final[0]) == sorted(recorded_final) and final[0]["DecisionResult"] == recorded_final["DecisionResult"],
              final[:1])
        states = [[r.get("status") for r in rows] for rows in seen]
        check("one item converts at a time, the rest wait (recorded)",
              any("processing" in s and "waiting" in s for s in states) and all(s.count("processing") <= 1 for s in states),
              states[:6])
        running = [r for rows in seen for r in rows if r.get("status") == "processing" and "TranscodeSession" in r]
        recorded_session = RECORDED["items-1"]["resp_json"]["MediaContainer"]["DownloadQueueItem"][0]["TranscodeSession"]
        check("a running item carries the recorded TranscodeSession fields",
              running and sorted(running[0]["TranscodeSession"]) == sorted(recorded_session)
              and running[0]["TranscodeSession"]["context"] == "static" and running[0]["TranscodeSession"]["container"] == "mp4",
              running[:1])
        status, _, raw = call(port, "GET", "/downloadQueue/%s/item/%s" % (qid, first))
        check("a bare item path is the recorded 404", status == RECORDED["item-1"]["status"] == 404, status)
        status, _, raw = call(port, "GET", "/downloadQueue/%s/item/%s/decision" % (qid, first))
        decided = (body(raw) or {}).get("MediaContainer", {})
        recorded = RECORDED["decision-1"]["resp_json"]["MediaContainer"]
        media = (decided.get("Metadata") or [{}])[0].get("Media", [{}])[0]
        part = media.get("Part", [{}])[0]
        rmedia = recorded["Metadata"][0]["Media"][0]
        check("the decision answers the recorded container fields and codes",
              status == 200 and set(recorded) - {"size"} <= set(decided)
              and all(decided.get(k) == recorded[k] for k in ("generalDecisionCode", "directPlayDecisionCode", "transcodeDecisionCode",
                                                             "directPlayDecisionText", "generalDecisionText", "allowSync")),
              {k: decided.get(k) for k in ("generalDecisionCode", "directPlayDecisionText")})
        check("its Media is the mp4 download: optimized, no protocol, streams direct, as recorded",
              media.get("container") == rmedia["container"] == "mp4" and media.get("optimizedForStreaming") is True
              and "protocol" not in media and part.get("decision") == "transcode"
              and all(s.get("location") == "direct" for s in part.get("Stream", [])) and len(part.get("Stream", [])) == 2,
              media)
        status, headers, _ = call(port, "HEAD", "/downloadQueue/%s/item/%s/media" % (qid, first))
        recorded = RECORDED["media-head-1"]
        check("HEAD of the media is the recorded 200 attachment",
              status == 200 and dict(headers).get("Content-Type") == "application/octet-stream"
              and dict(headers).get("Content-Disposition") == "attachment; filename=\"%s.mp4\"; filename*=UTF-8''%s%%2Emp4" % (first, first)
              and dict(headers).get("Accept-Ranges") == "bytes",
              headers)
        status, headers, raw = call(port, "GET", "/downloadQueue/%s/item/%s/media" % (qid, first), range_="bytes=0-1023")
        full_status, full_headers, full = call(port, "GET", "/downloadQueue/%s/item/%s/media" % (qid, first))
        check("a Range read is the recorded 206 with Content-Range",
              status == 206 and len(raw) == 1024 and dict(headers).get("Content-Range") == "bytes 0-1023/%d" % len(full) and full[:1024] == raw,
              (status, dict(headers).get("Content-Range")))
        out = os.path.join(scratch, "download.mp4")
        open(out, "wb").write(full)
        info = streams_of(out)
        codecs = sorted(s["codec_name"] for s in info["streams"])
        check("the whole file is a playable 30 s h264/aac mp4",
              full_status == 200 and "mp4" in info["format"].get("format_name", "") and codecs == ["aac", "h264"]
              and abs(float(info["format"].get("duration", 0)) - 30) < 1.5,
              (full_status, codecs, info["format"].get("duration")))
        status, _, small = call(port, "GET", "/downloadQueue/%s/item/%s/media" % (qid, capped))
        open(out, "wb").write(small)
        video = next((s for s in streams_of(out)["streams"] if s["codec_type"] == "video"), {"width": 0, "height": 0})
        check("the capped item is converted down to the requested 640x360",
              status == 200 and 0 < video["width"] <= 640 and 0 < video["height"] <= 360 and len(small) < len(full) / 3,
              (video["width"], video["height"], len(small), len(full)))
        # SIGTERM flushes the queue's state. Restart deliberately clears the SAME
        # transcoder directory: offline MP4s must not live there.
        process.terminate()
        process.wait(timeout=30)
        port, process, log = start_persistent(catalog, zurg_port, scratch)
        status, headers, restored = call(port, "GET", "/downloadQueue/%s/item/%s/media" % (qid, first),
                                         range_="bytes=0-1023")
        check("completed downloads survive restart and retain exact Range bytes",
              status == 206 and restored == full[:1024]
              and dict(headers).get("Content-Range") == "bytes 0-1023/%d" % len(full),
              (status, dict(headers).get("Content-Range")))
        status, _, _ = call(port, "DELETE", "/downloadQueue/%s/items/%s" % (qid, first))
        _, _, raw = call(port, "GET", "/downloadQueue/%s/items" % qid)
        left = (body(raw) or {}).get("MediaContainer", {}).get("DownloadQueueItem", [])
        check("deleting an item is the recorded 204 and it leaves the list",
              status == 204 and [r["id"] for r in left] == [capped, two[0].get("id")], (status, [r["id"] for r in left]))
        status, _, _ = call(port, "DELETE", "/downloadQueue/%s/items/%s,%s" % (qid, capped, two[0].get("id")))
        _, _, raw = call(port, "GET", "/downloadQueue/%s/items" % qid)
        check("deleting the rest leaves the recorded empty list",
              status == 204 and body(raw) == RECORDED["items-empty"]["resp_json"], raw)
        status, _, _ = call(port, "GET", "/downloadQueue/%s/item/%s/media" % (qid, first))
        check("a deleted item's media is gone", status == 404, status)
        # An active conversion is re-decided from persisted source/account state,
        # not treated as completed or resumed from an incomplete MP4.
        _,_,created = call(port, "POST", "/downloadQueue")
        qid = body(created)["MediaContainer"]["DownloadQueue"][0]["id"]
        previous_rate = transcode.SLOW_RATE
        transcode.SLOW_RATE = 65536
        try:
            _,_,added = call(port, "POST", "/downloadQueue/%s/add" % qid, RECORDED["add-original"]["query"])
            resumed_id = body(added)["MediaContainer"]["AddedQueueItems"][0]["id"]
            pending = poll_items(port, qid, lambda rows: any(r.get("status") == "processing" for r in rows), timeout=15)
            check("restart scenario interrupts a real active conversion",
                  pending and any(r.get("status") == "processing" for r in pending[-1]), pending[-1:] if pending else [])
            process.terminate()
            process.wait(timeout=30)
        finally:
            transcode.SLOW_RATE = previous_rate
        port, process, log = start_persistent(catalog, zurg_port, scratch)
        resumed = poll_items(port, qid, lambda rows: any(r.get("id") == resumed_id and r.get("status") == "available" for r in rows), timeout=30)
        status,_,resumed_bytes = call(port, "GET", "/downloadQueue/%s/item/%s/media" % (qid, resumed_id))
        open(out, "wb").write(resumed_bytes)
        resumed_probe = streams_of(out)
        check("an interrupted download restarts and publishes a complete playable file",
              status == 200 and sorted(s.get("codec_name") for s in resumed_probe["streams"]) == ["aac", "h264"]
              and abs(float(resumed_probe.get("format", {}).get("duration", 0)) - 30) < 1.5,
              (status, [r.get("status") for r in resumed[-1]] if resumed else []))
    finally:
        process.terminate()
        process.wait()

    print("without an encoder")
    port, process, log = start_server(catalog, zurg_port, False, scratch)
    try:
        _, _, raw = call(port, "POST", "/downloadQueue")
        qid = (body(raw) or {}).get("MediaContainer", {}).get("DownloadQueue", [{}])[0].get("id")
        status, _, raw = call(port, "POST", "/downloadQueue/%s/add" % qid, RECORDED["add-original"]["query"])
        _, _, raw = call(port, "GET", "/downloadQueue/%s/items" % qid)
        rows = (body(raw) or {}).get("MediaContainer", {}).get("DownloadQueueItem", [])
        # Not measured: the reference always has an encoder. rex's refusal is its own decision's.
        check("with no encoder the item is an error carrying the refusal",
              status == 200 and len(rows) == 1 and rows[0].get("status") == "error"
              and rows[0].get("DecisionResult", {}).get("generalDecisionCode") == 2000, rows)
    finally:
        process.terminate()
        process.wait()

    httpd.shutdown()
    httpd.server_close()
    passed = sum(1 for _, ok in results if ok)
    print("%d/%d checks passed" % (passed, len(results)))
    if passed == len(results):
        shutil.rmtree(scratch)
    else:
        print("Failed download media and logs retained at", scratch)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
