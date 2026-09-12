#!/usr/bin/env python3
"""Drive plexsim with python-plexapi -- a real Plex client library.

python-plexapi is the same XML contract every official client parses, and it is
strict: a missing attribute or a wrongly shaped child element raises here rather
than degrading quietly.  This covers the browse half.  The playback half is the
Range reads at the end, which is the part pd_inject never had.

    .venv/bin/python plexsim/conformance.py [--base http://127.0.0.1:32400]
"""

import argparse
import sys
import time

import requests
from plexapi.server import PlexServer

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results = []


def check(label, condition, detail=""):
    results.append(bool(condition))
    print("  %s  %-52s %s" % (PASS if condition else FAIL, label, detail))
    return bool(condition)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:32400")
    parser.add_argument("--token", default="plexsim-test-token")
    args = parser.parse_args()

    print("\n=== 1. connect + identity ===")
    server = PlexServer(args.base, args.token)
    check("PlexServer() handshake", True, server.friendlyName)
    check("machineIdentifier present", bool(server.machineIdentifier), server.machineIdentifier)
    check("version parsed", bool(server.version), server.version)

    print("\n=== 2. library sections ===")
    sections = server.library.sections()
    check("sections listed", len(sections) >= 1, "%d section(s)" % len(sections))
    section = sections[0]
    check("section type", section.type == "movie", section.type)
    check("section title", bool(section.title), section.title)

    print("\n=== 3. section listing ===")
    items = section.all()
    check("items returned", len(items) >= 1, "%d item(s)" % len(items))
    for item in items:
        print("        - %-34s %s" % (item.title[:34], item.ratingKey))

    print("\n=== 4. item detail + Media/Part/Stream ===")
    # Pick the best direct-play candidate: h264 is what every client decodes.
    target = None
    for item in items:
        full = server.fetchItem(int(item.ratingKey))
        media = full.media[0] if full.media else None
        if media and media.videoCodec == "h264":
            target = full
            break
    if target is None:
        target = server.fetchItem(int(items[0].ratingKey))

    check("reloaded item", bool(target.title), target.title)
    media = target.media[0]
    check("Media.container", bool(media.container), media.container)
    check("Media.videoCodec", bool(media.videoCodec), media.videoCodec)
    check("Media.audioCodec", bool(media.audioCodec), media.audioCodec)
    check("Media.bitrate", bool(media.bitrate), "%s kbps" % media.bitrate)
    check(
        "Media resolution",
        bool(media.width and media.height),
        "%sx%s (%s)" % (media.width, media.height, media.videoResolution),
    )
    check("Media.videoFrameRate", bool(media.videoFrameRate), media.videoFrameRate)
    check("Media.duration", bool(media.duration), "%.1f min" % (media.duration / 60000))

    part = media.parts[0]
    check("Part.key", part.key.startswith("/library/parts/"), part.key)
    check("Part.size", bool(part.size), "%.2f GB" % (part.size / 1e9))
    check("Part.container", bool(part.container), part.container)

    streams = part.streams
    video_streams = [s for s in streams if s.streamType == 1]
    audio_streams = [s for s in streams if s.streamType == 2]
    subtitle_streams = [s for s in streams if s.streamType == 3]
    check("video stream", len(video_streams) == 1, video_streams[0].displayTitle)
    check("audio stream(s)", len(audio_streams) >= 1, audio_streams[0].displayTitle)
    check(
        "audio stream selected",
        any(getattr(s, "selected", False) for s in audio_streams),
        "%d track(s)" % len(audio_streams),
    )
    check(
        "subtitle stream(s) parsed",
        True,
        "%d track(s): %s"
        % (
            len(subtitle_streams),
            ", ".join(s.displayTitle or "?" for s in subtitle_streams[:4]),
        ),
    )

    print("\n=== 5. search ===")
    needle = target.title.split()[0]
    hits = server.search(needle)
    check("search returns the item", any(h.ratingKey == target.ratingKey for h in hits),
          "%d hit(s) for %r" % (len(hits), needle))

    print("\n=== 6. play queue ===")
    session = requests.Session()
    response = session.post(
        args.base + "/playQueues",
        params={
            "type": "video",
            "uri": "server://%s/com.plexapp.plugins.library/library/metadata/%s"
            % (server.machineIdentifier, target.ratingKey),
            "continuous": 0,
            "X-Plex-Token": args.token,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    queue = response.json().get("MediaContainer", {})
    check("playQueueID issued", bool(queue.get("playQueueID")), str(queue.get("playQueueID")))
    check("queue carries the item", queue.get("playQueueTotalCount") == 1,
          "count=%s" % queue.get("playQueueTotalCount"))

    print("\n=== 7. transcode decision ===")
    response = session.get(
        args.base + "/video/:/transcode/universal/decision",
        params={
            "path": "/library/metadata/%s" % target.ratingKey,
            "mediaIndex": 0,
            "partIndex": 0,
            "protocol": "*",
            "directPlay": 1,
            "directStream": 1,
            "hasMDE": 1,
            "X-Plex-Token": args.token,
            "X-Plex-Client-Profile-Name": "Chrome",
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    decision = response.json().get("MediaContainer", {})
    mde = decision.get("mdeDecisionCode")
    check("mdeDecisionCode direct play", mde == 1000, str(mde))
    check(
        "1000 <= mde < 2000 (kodi ServerDecision.isSuccess)",
        isinstance(mde, int) and 1000 <= mde < 2000,
        decision.get("mdeDecisionText"),
    )
    decision_rows = decision.get("Metadata") or []
    part_decision = None
    if decision_rows:
        part_decision = (
            decision_rows[0].get("Media", [{}])[0].get("Part", [{}])[0].get("decision")
        )
    check("Part.decision echoed (plx-native reads this)", part_decision == "directplay",
          str(part_decision))

    print("\n=== 8. the stream: Range reads on Part.key ===")
    part_url = args.base + part.key
    response = session.get(
        part_url, params={"X-Plex-Token": args.token},
        headers={"Range": "bytes=0-99"}, timeout=60,
    )
    check("206 Partial Content", response.status_code == 206, str(response.status_code))
    check("Accept-Ranges: bytes", response.headers.get("Accept-Ranges") == "bytes",
          str(response.headers.get("Accept-Ranges")))
    content_range = response.headers.get("Content-Range") or ""
    check("Content-Range with true total", content_range.endswith("/%d" % part.size),
          content_range)
    check("100 bytes returned", len(response.content) == 100, "%d bytes" % len(response.content))
    check(
        "Content-Type is a media type",
        (response.headers.get("Content-Type") or "").startswith("video/"),
        str(response.headers.get("Content-Type")),
    )
    check(
        "matroska magic at offset 0",
        response.content[:4] == b"\x1a\x45\xdf\xa3",
        response.content[:4].hex(),
    )

    # A seek is just another Range, and it is the read a debrid backend is most
    # likely to fail: the CDN has to honour a mid-file offset, not only a prefix.
    midpoint = part.size // 2
    started = time.time()
    response = session.get(
        part_url, params={"X-Plex-Token": args.token},
        headers={"Range": "bytes=%d-%d" % (midpoint, midpoint + 65535)}, timeout=60,
    )
    elapsed = time.time() - started
    check("mid-file seek 206", response.status_code == 206,
          "%s in %.2fs" % (response.status_code, elapsed))
    check("seek returned 64 KiB", len(response.content) == 65536,
          "%d bytes" % len(response.content))
    check(
        "seek Content-Range offset correct",
        (response.headers.get("Content-Range") or "").startswith("bytes %d-" % midpoint),
        str(response.headers.get("Content-Range")),
    )

    print("\n=== 9. timeline / watch state ===")
    session.get(
        args.base + "/:/timeline",
        params={
            "ratingKey": target.ratingKey,
            "key": "/library/metadata/%s" % target.ratingKey,
            "identifier": "com.plexapp.plugins.library",
            "state": "playing",
            "time": 60000,
            "duration": media.duration,
            "X-Plex-Token": args.token,
        },
        timeout=15,
    )
    reloaded = server.fetchItem(int(target.ratingKey))
    check("viewOffset persisted", reloaded.viewOffset == 60000, str(reloaded.viewOffset))

    total = len(results)
    passed = sum(results)
    print("\n%s  %d/%d checks passed\n" % (PASS if passed == total else FAIL, passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
