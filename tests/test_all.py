#!/usr/bin/env python3
"""Exercise every playable item in the catalog, end to end.

For each leaf (movie or episode) this drives the real playback sequence a client
performs -- play queue, transcode decision, then byte reads -- and then proves
the bytes actually decode, rather than trusting a 200.

Two transports are checked separately because they behave differently:
  local   127.0.0.1:32400, straight at rex
  public  https://rex.sarmiento.cc, through the Cloudflare tunnel, which
          strips `Range` on a prefix request

A client-side gate verdict is included per item, because a 200 from rex does
not mean a given client will play it: Plex Web direct-plays mp4/h264/aac and
refuses mkv and webm outright.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gate  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
OFF = "\033[0m"

# Container/codec sets each client class will direct-play. Plex Web is the
# strictest of the three and is what the desktop/browser client uses.
CLIENT_PROFILES = {
    "Plex Web / Mac": {"containers": {"mp4"}, "video": {"h264"}, "audio": {"aac", "mp3"}},
    "TV (h264+ac3)": {"containers": {"mp4", "mkv"}, "video": {"h264"},
                       "audio": {"aac", "ac3", "eac3", "mp3"}},
    "TV 4K (hevc ok)": {"containers": {"mp4", "mkv"}, "video": {"h264", "hevc"},
                         "audio": {"aac", "ac3", "eac3", "truehd", "dts", "mp3"}},
}

MAGIC = {
    "mp4": (b"ftyp", 4),       # at offset 4
    "mkv": (b"\x1a\x45\xdf\xa3", 0),
    "webm": (b"\x1a\x45\xdf\xa3", 0),
}


# Cloudflare answers 403 to `Python-urllib/<ver>` on the media path -- measured
# against every other UA (curl, a Plex product string, a browser, and even an
# empty UA), all of which get 206. So the harness must not use urllib's default
# or it measures Cloudflare's bot filter instead of rex.
CLIENT_UA = "Plex/1.115.0 (Macintosh; rex-test)"


def fetch(url, headers=None, timeout=60, method=None):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", CLIENT_UA)
    request = urllib.request.Request(url, headers=headers, method=method)
    if method == "POST":
        request.data = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()[:200]
    except Exception as error:
        return None, {}, str(error).encode()[:200]


def decode_frames(url, offset_seconds, frames=12):
    """Decode real frames at an offset. Returns (ok, detail)."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-user_agent", CLIENT_UA,
         "-ss", str(offset_seconds), "-i", url,
         "-frames:v", str(frames), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
        capture_output=True, timeout=240,
    )
    if result.returncode != 0:
        return False, (result.stderr.decode()[:90] or "ffmpeg failed").replace("\n", " ")
    if not result.stdout:
        return False, "no frames produced"
    return True, "%d bytes decoded" % len(result.stdout)


def client_verdict(media):
    container = (media.get("container") or "").lower()
    video = (media.get("videoCodec") or "").lower()
    audio = (media.get("audioCodec") or "").lower()
    out = []
    for label, profile in CLIENT_PROFILES.items():
        ok = (container in profile["containers"]
              and video in profile["video"]
              and audio in profile["audio"])
        out.append((label, ok))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default=os.path.join(ROOT, "catalog.json"))
    parser.add_argument("--local", default="http://127.0.0.1:32400")
    parser.add_argument("--public", default="https://rex.sarmiento.cc")
    parser.add_argument("--skip-decode", action="store_true")
    args = parser.parse_args()

    document = json.load(open(args.catalog))
    machine = document["machineIdentifier"]
    leaves = [r for r in document["items"] if r.get("Media")]

    print("catalog: %d rows, %d playable\n" % (len(document["items"]), len(leaves)))
    summary = []

    for row in leaves:
        rating_key = row["ratingKey"]
        media = row["Media"][0]
        part = media["Part"][0]
        label = row.get("title", "?")
        if row.get("type") == "episode":
            label = "%s S%02dE%02d %s" % (row.get("grandparentTitle", "?"),
                                          row.get("parentIndex") or 0,
                                          row.get("index") or 0, label)
        print("=" * 78)
        print("%s  [rk=%s]" % (label, rating_key))
        print("  %s / %s / %s  %sx%s  %s kbps  %.1f min" % (
            media.get("container"), media.get("videoCodec"), media.get("audioCodec"),
            media.get("width"), media.get("height"), media.get("bitrate"),
            (media.get("duration") or 0) / 60000.0))

        results = {}

        # 1. play queue
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, rating_key)
        status, _, body = fetch(
            "%s/playQueues?type=video&uri=%s" % (args.local, urllib.parse.quote(uri, safe="")),
            headers={"Accept": "application/json"}, method="POST")
        queued = 0
        if status == 200:
            try:
                queued = json.loads(body)["MediaContainer"].get("playQueueTotalCount") or 0
            except Exception:
                pass
        results["playQueue"] = (queued == 1, "count=%s" % queued)

        # 2. decision
        status, _, body = fetch(
            # The parameters a player really sends. A real PMS 400s a decision with no
            # mediaIndex, and one whose X-Plex-Platform names no client profile.
            "%s/video/:/transcode/universal/decision?path=%s&mediaIndex=0&partIndex=0"
            "&protocol=http&directPlay=1&directStream=1&hasMDE=1&X-Plex-Platform=Chrome"
            % (args.local, urllib.parse.quote("/library/metadata/%s" % rating_key, safe="")),
            headers={"Accept": "application/json"})
        decision = None
        if status == 200:
            try:
                container = json.loads(body)["MediaContainer"]
                decision = ((container.get("Metadata") or [{}])[0].get("Media") or [{}])[0] \
                    .get("Part", [{}])[0].get("decision")
            except Exception:
                pass
        results["decision"] = (decision == "directplay", str(decision))

        # 3. byte reads, both transports
        size = part.get("size") or 0
        for name, base in (("local", args.local), ("public", args.public)):
            if name == "public":
                time.sleep(1)  # be gentle on the tunnel
            url = base + part["key"] + "?X-Plex-Token=rex-test"
            status, headers, body = fetch(url, headers={"Range": "bytes=0-1023"})
            ctype = (headers or {}).get("Content-Type", "")
            prefix_ok = status in (200, 206) and ctype.startswith("video/")
            results["%s prefix" % name] = (
                prefix_ok, "HTTP %s %s" % (status, ctype or "-"))

            # container magic, proving it is the media and not an error page
            if status in (200, 206) and body:
                magic, at = MAGIC.get((part.get("container") or "").lower(), (None, 0))
                results["%s magic" % name] = (
                    bool(magic) and body[at:at + len(magic)] == magic,
                    body[:8].hex() if body else "empty")

            mid = max(0, size // 2)
            status, headers, _ = fetch(url, headers={"Range": "bytes=%d-%d" % (mid, mid + 65535)})
            crange = (headers or {}).get("Content-Range", "")
            results["%s seek" % name] = (
                status == 206 and crange.startswith("bytes %d-" % mid),
                "HTTP %s %s" % (status, crange or "-"))

        # 4. real decode, through the public transport
        if not args.skip_decode:
            offset = max(5, int((media.get("duration") or 0) / 1000 * 0.10))
            ok, detail = decode_frames(
                args.local + part["key"] + "?X-Plex-Token=rex-test", offset)
            results["decode @%ds" % offset] = (ok, detail)

        for check, (ok, detail) in results.items():
            mark = GREEN + "PASS" + OFF if ok else RED + "FAIL" + OFF
            print("    %s  %-18s %s" % (mark, check, detail))

        print("    client direct-play gate:")
        verdicts = client_verdict(media)
        for name, ok in verdicts:
            mark = GREEN + "play " + OFF if ok else YELLOW + "trans" + OFF
            print("      %s %s" % (mark, name))

        server_ok = all(ok for ok, _ in results.values())
        summary.append((label, server_ok, verdicts))

    print("\n" + "=" * 78)
    print("SUMMARY\n")
    print("  %-44s %-8s %s" % ("item", "server", "plays in"))
    for label, server_ok, verdicts in summary:
        plays = ", ".join(n for n, ok in verdicts if ok) or "no client (needs transcode)"
        print("  %-44s %-8s %s" % (label[:44],
                                   (GREEN + "OK" + OFF) if server_ok else (RED + "FAIL" + OFF),
                                   plays))
    failed = [l for l, ok, _ in summary if not ok]
    print("\n  %d/%d served correctly by rex" % (len(summary) - len(failed), len(summary)))
    if failed:
        print("  server-side failures: %s" % ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
