#!/usr/bin/env python3
"""Apply the official client's direct-play gate to rex's catalog.

This re-implements the checks in plex-for-kodi's
`MediaDecisionEngine.canDirectPlay` (plexnet/mediadecisionengine.py), which every
official client runs on the `Media` attributes BEFORE the server is asked for a
decision:

    height > maxResolution                      -> transcode
    bitrate > maxBitrate                        -> transcode
    frameRate > 30 and height >= 1080           -> transcode  (no 1080p60 support)
    videoCodec == hevc and not allow_hevc       -> transcode

rex has no encoder, so a "transcode" verdict here is not a quality
downgrade, it is an item that will not play at all.  Running this against the
catalog says which rows are actually deliverable to which client class, instead
of discovering it one silent failure at a time.

The profiles are the client-side caps those clients ship with, not server
policy, and they are the reason honest `Media` numbers matter.
"""

import argparse
import json
import os

# (label, max height, max bitrate kbps, allows hevc, supports 1080p60)
PROFILES = [
    ("TV / 4K HEVC-capable", 2160, 80000, True, True),
    ("TV / 1080p h264-only", 1088, 20000, False, False),
    ("mobile over cellular", 720, 4000, True, False),
    ("kodi default (allow_hevc off)", 1088, 20000, False, False),
]


def verdict(media, max_height, max_bitrate, allow_hevc, supports_1080p60):
    height = media.get("height") or 0
    bitrate = media.get("bitrate") or 0
    codec = (media.get("videoCodec") or "").lower()
    frame_rate = _numeric_frame_rate(media)

    if height > max_height:
        return False, "height %d > max %d" % (height, max_height)
    if bitrate > max_bitrate:
        return False, "bitrate %d > max %d kbps" % (bitrate, max_bitrate)
    if not supports_1080p60 and frame_rate > 30 and height >= 1080:
        return False, "%.0f fps at %dp needs 1080p60 support" % (frame_rate, height)
    if codec == "hevc" and not allow_hevc:
        return False, "hevc disabled on this client"
    # Not part of canDirectPlay, but a container/codec the client cannot demux
    # fails just as hard, and vp8/vp9 in webm is the common case for us.
    if codec in ("vp8", "vp9", "av1"):
        return False, "%s is not broadly direct-playable on Plex clients" % codec
    return True, "direct play"


def _numeric_frame_rate(media):
    token = str(media.get("videoFrameRate") or "")
    known = {"24p": 23.976, "PAL": 25.0, "NTSC": 29.97, "30p": 30.0, "50p": 50.0, "60p": 59.94}
    if token in known:
        return known[token]
    try:
        return float(token.rstrip("p"))
    except ValueError:
        return 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default=os.path.join(os.path.dirname(__file__), "catalog.json"))
    args = parser.parse_args()

    with open(args.catalog) as handle:
        document = json.load(handle)

    for item in document["items"]:
        media = item["Media"][0]
        print(
            "\n%s\n    %s %s %dx%d %s %s kbps %s"
            % (
                item["title"],
                media.get("container"),
                media.get("videoCodec"),
                media.get("width") or 0,
                media.get("height") or 0,
                media.get("audioCodec"),
                media.get("bitrate"),
                media.get("videoFrameRate"),
            )
        )
        for label, height, bitrate, hevc, p60 in PROFILES:
            ok, reason = verdict(media, height, bitrate, hevc, p60)
            print("      %-32s %-5s %s" % (label, "PLAY" if ok else "FAIL", reason))


if __name__ == "__main__":
    main()
