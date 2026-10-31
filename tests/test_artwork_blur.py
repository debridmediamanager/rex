#!/usr/bin/env python3
"""Artwork analysis: `thumbBlurHash`, `artBlurHash` and `UltraBlurColors`, as plexav answers them.

plexav (PMS 1.43.4) holds rex's own synthetic covers (`probes/make_blur_images.py`, the files in
`fixtures/artwork/`) as the `Rex Blur` albums of its music library; `fixtures/artwork/plexav_blur.json`
(`probes/record_blur.py`) is what it answered for them and which rows carry which field for Plexamp's
exact requests. A source here serves the same covers to rex as the same albums' artwork:

* each album and artist with its own image carries `UltraBlurColors`, whatever the request; a track,
  which borrows its album's cover, never does;
* `thumbBlurHash`/`artBlurHash` only when `includeFields` names them, and only of the row's own image
  (an album, whose background is its artist's, has no `artBlurHash`), before `year` as plexav writes it;
* the hash decodes to plexav's colours and the corners are plexav's, within the tolerances below; a
  film's or show's poster is hashed 3x4, every other image 4x3, as PMS hashes them;
* a film's, a show's, a season's and an episode's colours are those of its own background, else its
  own poster, as plexav's are; with no image that can be read a film keeps rex's fixed palette, an
  album shows neither field, and an image not read yet answers as plexav's unanalysed row (neither)
  without the listing waiting for it;
* `/services/ultrablur/colors?url=` answers the image's colours;
* kept by `--state`: a restart answers them on its first listing without reading the covers again.

    python3 tests/test_artwork_blur.py
"""
import http.server
import json
import math
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse

# This suite measures the analysis every other suite's server leaves off (`server_command.py`).
os.environ["REX_ARTWORK_ANALYSIS"] = "on"

import music_rig as M
from music_rig import check, results
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "fixtures", "artwork")
FIXTURE = json.load(open(os.path.join(ART, "plexav_blur.json")))
PROBE = json.load(open(os.path.join(HERE, "fixtures", "music", "media.json")))["tracks"][0]["probe"]
# How far rex's answer for one cover may be from plexav's, per channel of 0-255. The hash decoded to
# 32x32 pixels: on average and at the worst pixel (measured: 0 on the PNGs and most JPEGs, 1.2 and 24
# on quadrants.jpg, whose hard edges libjpeg-turbo's reduced-size decode, which PMS hashes, smooths
# differently). Each corner colour (measured: at most 4, the JPEG decoders apart).
HASH_MEAN, HASH_TOLERANCE = 2.0, 32
COLOR_TOLERANCE = 6

BASE83 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"


def decode_hash(blurhash, size=32):
    """Wolt's BlurHash decoder: the picture a hash stands for, `size` pixels square."""
    def number(text):
        value = 0
        for char in text:
            value = value * 83 + BASE83.index(char)
        return value

    def linear(value):
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    def srgb(value):
        value = max(0.0, min(1.0, value))
        return round(value * 12.92 * 255) if value <= 0.0031308 else round((1.055 * value ** (1 / 2.4) - 0.055) * 255)

    flag = number(blurhash[0])
    across, down = flag % 9 + 1, flag // 9 + 1
    maximum = (number(blurhash[1]) + 1) / 166
    factors = []
    for index in range(across * down):
        if index == 0:
            value = number(blurhash[2:6])
            factors.append((linear(value >> 16), linear((value >> 8) & 255), linear(value & 255)))
        else:
            value = number(blurhash[4 + index * 2:6 + index * 2])
            factors.append(tuple(math.copysign(((q - 9) / 9) ** 2, q - 9) * maximum
                                 for q in (value // 361, value // 19 % 19, value % 19)))
    pixels = []
    for y in range(size):
        for x in range(size):
            color = [0.0, 0.0, 0.0]
            for j in range(down):
                for i in range(across):
                    basis = math.cos(math.pi * x * i / size) * math.cos(math.pi * y * j / size)
                    for channel in range(3):
                        color[channel] += factors[j * across + i][channel] * basis
            pixels.append([srgb(value) for value in color])
    return (across, down), pixels


def hash_distance(ours, theirs):
    """The worst pixel's difference, or 999 when the two are further apart on average than HASH_MEAN."""
    (shape_a, a), (shape_b, b) = decode_hash(ours), decode_hash(theirs)
    if shape_a != shape_b:
        return 999
    differences = [abs(x - y) for p, q in zip(a, b) for x, y in zip(p, q)]
    return max(differences) if sum(differences) / len(differences) <= HASH_MEAN else 999


def color_distance(ours, theirs):
    corners = ("topLeft", "topRight", "bottomRight", "bottomLeft")
    if not ours or not theirs or list(ours) != list(corners):
        return 999
    return max(abs(int(ours[c][i:i + 2], 16) - int(theirs[c][i:i + 2], 16)) for c in corners for i in (0, 2, 4))


def brief(row):
    return {key: row.get(key) for key in ("thumb", "art", "thumbBlurHash", "artBlurHash", "UltraBlurColors") if key in row}


def cover_server(root):
    """A source serving `root`, holding back whatever sits under `slow/` until `release` is set."""
    release = threading.Event()
    reads = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            relative = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip("/")
            reads.append(relative)
            if relative.startswith("slow/"):
                release.wait(60)
            path = os.path.join(root, relative)
            data = open(path, "rb").read() if os.path.isfile(path) else None
            self.send_response(200 if data is not None else 404)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data or b"")))
            self.end_headers()
            self.wfile.write(data or b"")

    port = free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, release, reads


SECTIONS = [
    {"key": "2", "type": "movie", "title": "Movies", "agent": "tv.plex.agents.movie", "scanner": "Plex Movie",
     "language": "en-US", "uuid": "0000000000000000000000000000000000000a02", "Location": [{"id": 2, "path": "/data/movies"}]},
    {"key": "3", "type": "show", "title": "TV Shows", "agent": "tv.plex.agents.series", "scanner": "Plex TV Series",
     "language": "en-US", "uuid": "0000000000000000000000000000000000000a03", "Location": [{"id": 3, "path": "/data/shows"}]},
]


def items(source):
    """One track a cover (the album's poster, the artist's `artist.jpg`), a film and an episode."""
    pushed = []
    covers = sorted(name for name in os.listdir(ART) if name.endswith((".jpg", ".png")) and name != "artist.jpg")
    for number, name in enumerate(covers, 1):
        title = "Blur " + name.split(".")[0].title()
        pushed.append({"id": "music/%s.flac" % name, "path": "music/Rex Blur/%s/01.flac" % title, "kind": "track",
                       "track": 1, "media_info": PROBE,
                       "display": {"title": title + " Tone", "artist": {"title": "Rex Blur", "poster": source + "/artist.jpg"},
                                   "album": {"title": title, "date": "2026-01-01", "poster": source + "/" + name}}})
    # An artist with a background of its own; an album whose cover is not there; one read late.
    pushed.append({"id": "music/backdrop.flac", "path": "music/Backdrop/One/01.flac", "kind": "track", "track": 1,
                   "media_info": PROBE, "display": {"title": "Backdrop Tone", "artist": {
                       "title": "Backdrop", "poster": source + "/solid.png", "art": source + "/wide.jpg"},
                       "album": {"title": "Backdrop One", "poster": source + "/gone.jpg"}}})
    pushed.append({"id": "music/late.flac", "path": "music/Late/Late/01.flac", "kind": "track", "track": 1,
                   "media_info": PROBE, "display": {"title": "Late Tone", "artist": {"title": "Late"},
                                                    "album": {"title": "Late Album", "poster": source + "/slow/quadrants.jpg"}}})
    pushed.append({"id": "movies/film.mkv", "path": "movies/Film/film.mkv", "kind": "movie", "media_info": None,
                   "display": {"title": "Blur Film", "thumb": source + "/stripes.jpg", "art": source + "/wide.jpg"}})
    pushed.append({"id": "movies/blank.mkv", "path": "movies/Blank/blank.mkv", "kind": "movie", "media_info": None,
                   "display": {"title": "Blank Film", "thumb": source + "/gone.jpg"}})
    pushed.append({"id": "shows/ep.mkv", "path": "shows/Show/ep.mkv", "kind": "episode", "season": 1, "episode": 1,
                   "media_info": None,
                   "display": {"title": "Blur Episode", "thumb": source + "/diagonal.jpg",
                               "show": {"title": "Blur Show", "poster": source + "/gradient.png", "art": source + "/wide.jpg"},
                               "season": {"poster": source + "/quadrants.jpg"}}})
    return pushed


def main():
    work = tempfile.mkdtemp(prefix="rex-blur-")
    root = os.path.join(work, "covers")
    os.makedirs(os.path.join(root, "slow"))
    for name in os.listdir(ART):
        shutil.copy(os.path.join(ART, name), os.path.join(root, name))
    shutil.copy(os.path.join(ART, "quadrants.jpg"), os.path.join(root, "slow", "quadrants.jpg"))
    port, release, reads = cover_server(root)
    agent_url, _ = M.agent_server([])
    source = "http://127.0.0.1:%d" % port
    state = os.path.join(work, "rex.db")
    rex = M.Rex(work, port, agent_url, SECTIONS, args=("--state", state))
    try:
        pushed = items(source)
        for item in pushed:
            if item["media_info"] is None:
                item["media_info"] = json.load(open(os.path.join(HERE, "fixtures", "feed", "display_archive_1023.json")))["items"][0]["media_info"]
        status, body = rex.push(pushed)
        check("the covers' tracks, a film, a film with no cover and an episode are created",
              status == 200 and body.get("created") == len(pushed), (status, body))

        # plexav: a row it has not analysed yet answers neither field, and rex's listing does not
        # wait for an image still being read.
        fresh = FIXTURE["fresh"] or []
        check("plexav's album before its analysis answers neither field",
              fresh and not fresh[0]["rows"][0].get("thumbBlurHash") and not fresh[0]["rows"][0].get("UltraBlurColors"), fresh[:1])
        started = time.time()
        late = [row for row in rex.rows("/library/sections/1/all?type=9&includeFields=thumbBlurHash") if row["title"] == "Late Album"]
        took = time.time() - started
        check("an album whose cover is still being read answers neither, and its listing does not wait",
              late and "thumbBlurHash" not in late[0] and "UltraBlurColors" not in late[0] and took < 2, (late, took))
        release.set()

        def albums():
            return {row["title"]: row for row in rex.rows("/library/sections/1/all?type=9&includeFields=thumbBlurHash")}
        poll_until(lambda: all("thumbBlurHash" in row for title, row in albums().items() if title != "Backdrop One"), 60)
        mine = albums()
        theirs = {entry["cover"]: entry for entry in FIXTURE["rows"].values() if entry.get("cover")}
        for name, entry in sorted(theirs.items()):
            if name == "artist.jpg":
                continue
            row = mine.get("Blur " + name.split(".")[0].title()) or {}
            check("%s: the hash decodes to plexav's colours (mean within %.0f, worst pixel %d)" % (name, HASH_MEAN, HASH_TOLERANCE),
                  row.get("thumbBlurHash") and hash_distance(row["thumbBlurHash"], entry["thumbBlurHash"]) <= HASH_TOLERANCE,
                  (row.get("thumbBlurHash"), entry["thumbBlurHash"],
                   row.get("thumbBlurHash") and hash_distance(row["thumbBlurHash"], entry["thumbBlurHash"])))
            check("%s: the corners are plexav's (within %d)" % (name, COLOR_TOLERANCE),
                  color_distance(row.get("UltraBlurColors"), entry["UltraBlurColors"]) <= COLOR_TOLERANCE,
                  (row.get("UltraBlurColors"), entry["UltraBlurColors"]))
        solid = mine.get("Blur Solid") or {}
        keys = list(solid)
        theirs_keys = ["index", "thumbBlurHash", "year", "thumb"]
        check("thumbBlurHash sits after index and before year, as plexav writes it",
              [key for key in keys if key in theirs_keys] == theirs_keys, keys)
        check("UltraBlurColors after Image, before the tags", "UltraBlurColors" in keys
              and keys.index("UltraBlurColors") == (keys.index("Image") + 1 if "Image" in keys else keys.index("UltraBlurColors")), keys)
        check("an album whose cover cannot be read answers neither field",
              "thumbBlurHash" not in mine.get("Backdrop One", {}) and "UltraBlurColors" not in mine.get("Backdrop One", {}),
              mine.get("Backdrop One"))

        # Which rows carry which field, as plexav's do for Plexamp's requests.
        plain = {row["title"]: row for row in rex.rows("/library/sections/1/all?type=9")}
        check("without includeFields an album carries UltraBlurColors and no hash (plexav's)",
              "UltraBlurColors" in plain["Blur Solid"] and "thumbBlurHash" not in plain["Blur Solid"], plain["Blur Solid"])
        tracks = rex.rows("/library/sections/1/all?type=10&includeFields=thumbBlurHash,artBlurHash")
        check("a track carries neither field, whatever includeFields asks (plexav's)",
              tracks and not any(key in row for row in tracks for key in ("thumbBlurHash", "artBlurHash", "UltraBlurColors")),
              [list(row) for row in tracks[:1]])
        def artist_rows():
            return {row["title"]: row for row in rex.rows("/library/sections/1/all?type=8&includeFields=thumbBlurHash,artBlurHash")}
        poll_until(lambda: "artBlurHash" in artist_rows().get("Backdrop", {}) and "thumbBlurHash" in artist_rows().get("Rex Blur", {}), 30)
        artists = artist_rows()
        backdrop, blur = artists.get("Backdrop", {}), artists.get("Rex Blur", {})
        artist_entry = theirs["artist.jpg"]
        check("an artist with its own background answers artBlurHash; one without none (plexav's)",
              backdrop.get("artBlurHash") and "artBlurHash" not in blur, (backdrop.get("artBlurHash"), list(blur)))
        check("artist.jpg: the artist's hash and corners are plexav's",
              blur.get("thumbBlurHash") and hash_distance(blur["thumbBlurHash"], artist_entry["thumbBlurHash"]) <= HASH_TOLERANCE
              and color_distance(blur.get("UltraBlurColors"), artist_entry["UltraBlurColors"]) <= COLOR_TOLERANCE,
              (blur.get("thumbBlurHash"), blur.get("UltraBlurColors"), artist_entry))
        wide = theirs["wide.jpg"]["UltraBlurColors"]
        check("an artist's colours are its background's, not its poster's (plexav's)",
              color_distance(backdrop.get("UltraBlurColors"), wide) <= COLOR_TOLERANCE, (backdrop.get("UltraBlurColors"), wide))
        album_art = rex.rows("/library/sections/1/all?type=9&includeFields=artBlurHash")
        check("an album, whose background is its artist's, has no artBlurHash (plexav's)",
              album_art and not any("artBlurHash" in row for row in album_art), [list(row) for row in album_art[:1]])
        children = rex.rows("/library/metadata/%s/children?excludeFields=summary&includeFields=thumbBlurHash" % blur.get("ratingKey"))
        detail = rex.rows("/library/metadata/%s?includeFields=thumbBlurHash" % solid.get("ratingKey"))
        check("an artist's albums, an album's detail row carry the hash as listed (plexav's)",
              children and all("thumbBlurHash" in row for row in children)
              and detail and detail[0].get("thumbBlurHash") == solid.get("thumbBlurHash"), (children[:1], detail[:1]))
        status, body = rex.request("/hubs/sections/1?count=6&includeFields=thumbBlurHash")
        shelf = [row for hub in (body.get("MediaContainer") or {}).get("Hub") or [] for row in hub.get("Metadata") or []
                 if row.get("type") in ("album", "artist")]
        check("hub rows carry the hash too (plexav's)", shelf and all("thumbBlurHash" in row for row in shelf if row["title"] != "Backdrop One"),
              [(row["title"], list(row)) for row in shelf[:2]])

        # Films, shows, seasons and episodes: colours of their own background, else their poster.
        film = {row["title"]: row for row in rex.rows("/library/sections/2/all?includeFields=thumbBlurHash,artBlurHash")}
        poll_until(lambda: "thumbBlurHash" in ({row["title"]: row for row in rex.rows(
            "/library/sections/2/all?includeFields=thumbBlurHash")}.get("Blur Film") or {}), 30)
        film = {row["title"]: row for row in rex.rows("/library/sections/2/all?includeFields=thumbBlurHash,artBlurHash")}
        check("a film's colours are its background's, its art's hash plexav's for it",
              color_distance(film["Blur Film"].get("UltraBlurColors"), wide) <= COLOR_TOLERANCE
              and hash_distance(film["Blur Film"].get("artBlurHash", "0"), theirs["wide.jpg"]["thumbBlurHash"]) <= HASH_TOLERANCE,
              brief(film["Blur Film"]))
        # PMS hashes a film's or show's poster with 3x4 components (plexav's Firefly poster: `T...`),
        # every other image, a season's poster included, with 4x3 (`L...`).
        check("a film's poster hash is 3x4, as PMS hashes film and show posters",
              film["Blur Film"].get("thumbBlurHash", "").startswith("T"), brief(film["Blur Film"]))
        check("a film with no image that can be read keeps rex's fixed palette and no hash",
              film["Blank Film"].get("UltraBlurColors") == {"topLeft": "1a1a1e", "topRight": "24242a",
                                                            "bottomRight": "15151a", "bottomLeft": "1f1f26"}
              and "thumbBlurHash" not in film["Blank Film"], film["Blank Film"])

        def tv(kind):
            return (rex.rows("/library/sections/3/all?type=%s&includeFields=thumbBlurHash,artBlurHash" % kind) or [{}])[0]
        poll_until(lambda: "thumbBlurHash" in tv(4) and "thumbBlurHash" in tv(3) and "artBlurHash" in tv(2), 30)
        show, season, episode = tv(2), tv(3), tv(4)
        check("a show's colours are its background's (plexav's Firefly), its poster hashed 3x4",
              color_distance(show.get("UltraBlurColors"), wide) <= COLOR_TOLERANCE
              and show.get("thumbBlurHash", "").startswith("T"), brief(show))
        check("a season's are its own poster's and an episode's its still's, neither has artBlurHash (plexav's)",
              color_distance(season.get("UltraBlurColors"), theirs["quadrants.jpg"]["UltraBlurColors"]) <= COLOR_TOLERANCE
              and color_distance(episode.get("UltraBlurColors"), theirs["diagonal.jpg"]["UltraBlurColors"]) <= COLOR_TOLERANCE
              and "artBlurHash" not in season and "artBlurHash" not in episode
              and season.get("thumbBlurHash", "").startswith("L") and episode.get("thumbBlurHash", "").startswith("L"),
              (brief(season), brief(episode)))

        # The service answers the image's colours.
        for name, row in (("solid.png", solid), ("wide.jpg", film["Blur Film"])):
            leaf = "thumb" if name == "solid.png" else "art"
            status, body = rex.request("/services/ultrablur/colors?url=" + urllib.parse.quote(row.get(leaf, ""), safe=""))
            colors = ((body or {}).get("MediaContainer") or {}).get("UltraBlurColors") if status == 200 else None
            entry = theirs[name]
            check("/services/ultrablur/colors of %s's image: plexav's colours" % name,
                  colors and color_distance(colors[0], entry["service"]["MediaContainer"]["UltraBlurColors"][0]) <= COLOR_TOLERANCE,
                  (status, colors))
        rex.stop()

        # Kept by --state: the first listing after a restart has them, no cover read again.
        before = len(reads)
        rex = M.Rex(work, port, agent_url, SECTIONS, args=("--state", state))
        again = {row["title"]: row for row in rex.rows("/library/sections/1/all?type=9&includeFields=thumbBlurHash")}
        check("after a restart the first listing answers the same hashes and colours",
              all(again.get(title, {}).get("thumbBlurHash") == row.get("thumbBlurHash")
                  and again.get(title, {}).get("UltraBlurColors") == row.get("UltraBlurColors") for title, row in mine.items()),
              [(title, again.get(title, {}).get("thumbBlurHash")) for title in mine])
        time.sleep(1)
        check("and no cover is read again", [path for path in reads[before:] if path != "gone.jpg"] == [], reads[before:])
    finally:
        release.set()
        rex.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
