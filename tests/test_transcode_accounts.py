#!/usr/bin/env python3
"""Real generated conversions retain account ownership without changing bearer URLs.

Run after building rex with REX_TRANSCODER pointing to FFmpeg. Uses recorded
PMS access grants and the recorded Plex Web conversion request, no cloud account.
"""
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import urllib.parse

from test_accounts import (FIXTURES, MACHINE, OWNER, SERVER_TOKEN, SHARED,
                           SHARED_ID, PlexTv, Server, catalog)
from test_native_transcode import (FFMPEG, FFPROBE, MEDIA, Zurg, make_clip,
                                   plex_web_query, probe, row)
from test_ports import free_port
from test_share_restrictions import ask as local_ask


def main():
    assert FFMPEG and FFPROBE, "FFmpeg and ffprobe are required (set REX_TRANSCODER)"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 9))
        address = route.getsockname()[0]
    assert not address.startswith("127."), "a non-loopback interface is required for WAN allowances"
    work = tempfile.mkdtemp(prefix="rex-transcode-accounts-")
    plex = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), PlexTv)
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Zurg)
    for service in (plex, upstream):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    server = None
    previous_ffmpeg = os.environ.get("REX_TRANSCODER")
    try:
        os.environ["REX_TRANSCODER"] = FFMPEG
        clip = os.path.join(work, "accounts.mkv")
        make_clip(clip, ["-c:v", "libx264", "-preset", "ultrafast", "-g", "48",
                         "-pix_fmt", "yuv420p"], ["-c:a", "aac", "-ac", "2"], 30)
        MEDIA["accounts.mkv"] = clip
        fixture = json.load(open(os.path.join(FIXTURES, "shared_user_s1434.json")))["exchanges"]
        document = catalog(fixture)
        generated = row("1", "Big Buck Bunny", "accounts.mkv", probe(clip)["streams"],
                        os.path.getsize(clip), 30)
        # Keep the recorded rating, section and access-control context, replacing
        # only the media with the actual generated source and its parsed streams.
        items = document["items"]
        movie = next(item for item in items if str(item["ratingKey"]) == "1")
        movie["Media"] = generated["Media"]
        movie["duration"] = 30000
        json.dump(document, open(os.path.join(work, "catalog.json"), "w"))
        json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": "owner",
                   "email": "owner@example.com", "feature_uuids": [], "account_id": 316687974,
                   "view_state_cursor": None, "access_tokens": []},
                  open(os.path.join(work, "claim_state.json"), "w"))
        server = Server(work, plex.server_address[1])
        assert local_ask(server, OWNER, "/rex/sources/accounts", "PUT", {
            "url": "http://127.0.0.1:%d" % upstream.server_address[1],
            "mount": "/zurg/movies"})[0] == 200

        def request(token, path, query=(), playback=None):
            pairs = list(query)
            if token is not None:
                pairs.append(("X-Plex-Token", token))
            if playback is not None:
                pairs.append(("X-Plex-Session-Identifier", playback))
            connection = http.client.HTTPConnection(address, server.port, timeout=60)
            try:
                connection.request("GET", path + "?" + urllib.parse.urlencode(pairs),
                    headers={"Accept": "application/json", "X-Plex-Client-Identifier": "accounts-device",
                             "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"})
                response = connection.getresponse()
                raw = response.read()
                return response.status, json.loads(raw) if raw[:1] == b"{" else raw
            finally:
                connection.close()

        def prefs(**values):
            assert server.ask(OWNER, "PUT", "/:/prefs", list(values.items()))[0] == 200

        def conversion(token, opaque, resolution, leaf="decision", playback="same-playback"):
            query = plex_web_query("1", opaque, videoResolution=resolution,
                                   maxVideoBitrate="1000", directStream="1",
                                   **{"X-Plex-Session-Identifier": playback})
            return request(token, "/video/:/transcode/universal/" + leaf, query)

        def beat(token, state):
            status, body = request(token, "/:/timeline", [("ratingKey", "1"),
                ("key", "/library/metadata/1"), ("state", state), ("time", "1000"),
                ("duration", "30000")], "same-playback")
            assert status == 200, body

        def live():
            status, body = request(OWNER, "/status/sessions")
            assert status == 200, body
            return {int(item["User"]["id"]): item for item in body["MediaContainer"]["Metadata"]}

        def segment(opaque, width, height):
            status, data = request(None,
                "/video/:/transcode/universal/session/%s/base/00000.ts" % opaque)
            assert status == 200 and data, (status, data[:200])
            path = os.path.join(work, opaque + ".ts")
            open(path, "wb").write(data)
            video = next(stream for stream in probe(path)["streams"] if stream["codec_type"] == "video")
            assert (video["width"], video["height"]) == (width, height), video
            subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"], check=True)

        prefs(LanNetworksBandwidth="127.0.0.0/8", WanPerUserStreamCount=1,
              WanTotalMaxUploadRate=0, WanPerStreamMaxUploadRate=0, TranscodeCountLimit=1)
        status, body = conversion(OWNER, "opaque-owner", "640x360")
        assert status == 200 and body["MediaContainer"]["transcodeDecisionCode"] == 1001, body
        assert conversion(OWNER, "opaque-owner", "640x360", "start.m3u8")[0] == 200
        beat(OWNER, "playing")
        # A different account's same raw playback ID is not an excluded redecision.
        status, body = conversion(SHARED, "opaque-shared", "320x180")
        assert status == 200 and body["MediaContainer"]["transcodeDecisionCode"] == 4005, body
        prefs(TranscodeCountLimit=2)
        status, body = conversion(SHARED, "opaque-shared", "320x180")
        assert status == 200 and body["MediaContainer"]["transcodeDecisionCode"] == 1001, body
        assert conversion(SHARED, "opaque-shared", "320x180", "start.m3u8")[0] == 200
        beat(SHARED, "playing")
        current = live()
        assert set(current) == {1, int(SHARED_ID)}, current
        for account, opaque, size in ((1, "opaque-owner", (640, 360)),
                                      (int(SHARED_ID), "opaque-shared", (320, 180))):
            item = current[account]
            assert item["Session"]["id"] == "same-playback", item
            assert item["TranscodeSession"]["key"] == "/transcode/sessions/" + opaque, item
            media = item["Media"][0]
            assert (media["width"], media["height"]) == size, media
            segment(opaque, *size)
        beat(OWNER, "stopped")
        assert request(None, "/video/:/transcode/universal/session/opaque-owner/base/index.m3u8")[0] == 404
        assert request(None, "/video/:/transcode/universal/session/opaque-shared/base/index.m3u8")[0] == 200
        assert set(live()) == {int(SHARED_ID)}
        # Start the owner again. A bearer stop of the SHARED conversion presented
        # by OWNER must release the captured owner's slot, not the request viewer's.
        assert conversion(OWNER, "opaque-owner-next", "640x360")[0] == 200
        assert conversion(OWNER, "opaque-owner-next", "640x360", "start.m3u8")[0] == 200
        beat(OWNER, "playing")
        assert request(OWNER, "/video/:/transcode/universal/stop", [("session", "opaque-shared")])[0] == 200
        current = live()
        assert current[1]["Player"]["state"] == "playing", current
        assert current[int(SHARED_ID)]["Player"]["state"] == "stopped", current
        assert conversion(OWNER, "owner-blocked", "640x360", playback="other-playback")[0] == 503
        assert conversion(SHARED, "shared-free", "320x180", playback="other-playback")[0] == 200
        assert request(None, "/video/:/transcode/universal/session/opaque-owner-next/base/index.m3u8")[0] == 200
        print("PASS conversion accounts: real decoded media, projection, cap, timeline stop and bearer allowance ownership")
    finally:
        if server is not None:
            server.stop()
        for service in (plex, upstream):
            service.shutdown()
            service.server_close()
        MEDIA.pop("accounts.mkv", None)
        if previous_ffmpeg is None:
            os.environ.pop("REX_TRANSCODER", None)
        else:
            os.environ["REX_TRANSCODER"] = previous_ffmpeg
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
