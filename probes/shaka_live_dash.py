#!/usr/bin/env python3
"""Play a live channel's DASH conversion in Shaka Player 3.3.1 -- the player Plex Web 4.147.1
bundles -- in a real Chrome, and report how far it gets.

Stands up `tests/test_http_mkv.py`'s live channel and a rex (`REX_SERVER`, else the built one),
asks the decision and `start.mpd` as Plex Web does (`protocol=dash`), then plays the MPD for
`SECONDS` and prints the playhead each second. Needs Playwright and Google Chrome (Playwright's
own Chromium has no H.264/AAC).

    REX_SERVER=/path/to/rex python3 probes/shaka_live_dash.py [SECONDS]
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
import test_http_mkv as suite  # noqa: E402
import test_native_transcode as nt  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHAKA = "https://cdnjs.cloudflare.com/ajax/libs/shaka-player/3.3.1/shaka-player.compiled.js"
PAGE = """<!doctype html><video id=v muted autoplay style="width:320px"></video>
<script src="%s"></script><script>
window.log = [];
shaka.polyfill.installAll();
const player = new shaka.Player(document.getElementById('v'));
player.addEventListener('error', e => window.log.push('error ' + e.detail.code));
player.load(%s).then(() => window.log.push('loaded live=' + player.isLive())).catch(e => window.log.push('load error ' + e.code));
</script>"""


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    scratch = tempfile.mkdtemp(prefix="rex-shaka-")
    items, channels, _ = suite.build(scratch)
    catalog = os.path.join(scratch, "catalog.json")
    json.dump({"machineIdentifier": "0" * 39 + "9", "friendlyName": "rex-shaka",
               "sections": [{"key": "1", "type": "movie", "title": "Clips", "uuid": "0" * 39 + "9"}], "items": items}, open(catalog, "w"))
    source = suite.Quiet(("127.0.0.1", nt.free_port()), suite.Source)
    threading.Thread(target=source.serve_forever, daemon=True).start()
    port, process, log = nt.start_server(catalog, source.server_address[1], True, scratch)
    try:
        session = "shaka-live"
        query = nt.dash_query(channels["live-h264"], session)  # Plex Web 4.147.1's recorded DASH request
        print("decision", suite.decide(port, query).get("generalDecisionCode"))
        mpd = "http://127.0.0.1:%d/video/:/transcode/universal/start.mpd?%s" % (port, urllib.parse.urlencode(query))
        page = os.path.join(scratch, "index.html")
        open(page, "w").write(PAGE % (SHAKA, json.dumps(mpd)))
        handler = lambda *args: http.server.SimpleHTTPRequestHandler(*args, directory=scratch)
        web = http.server.ThreadingHTTPServer(("127.0.0.1", nt.free_port()), handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            tab = browser.new_page()
            tab.goto("http://127.0.0.1:%d/index.html" % web.server_address[1])
            times = []
            for second in range(seconds):
                tab.wait_for_timeout(1000)
                times.append(tab.evaluate("document.getElementById('v').currentTime"))
                print("%3ds playhead %.2f %s" % (second + 1, times[-1], tab.evaluate("window.log.join('; ')")))
            browser.close()
        played = times[-1] - next((t for t in times if t > 0), times[-1])
        print("RESULT played %.1fs of the last %ds; log: %s" % (played, seconds, open(log).read().count("ERROR")))
        return 0 if played > seconds * 0.5 else 1
    finally:
        process.terminate()
        process.wait()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
