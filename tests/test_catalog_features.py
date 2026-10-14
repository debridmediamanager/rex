#!/usr/bin/env python3
"""Focused regressions for catalog shapes absent from the ordinary UI fixture.

Black-box: every check drives a server process (`tests/server_command.py`, the native binary
by default) on `tests/catalog_features.json`, or on a temporary copy carrying the rows or the
edit a check needs, as a client would. Three facts with no route of their own -- the title
fold, the TMDB enrichment and the catalog builder -- are read off the native CLI's test
adapters (`rust/src/main.rs`).

    python3 tests/test_catalog_features.py
"""

import copy
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("REX_TEST_ROOT", os.path.dirname(HERE))
FIXTURE = os.environ.get("REX_CATALOG_FEATURES", os.path.join(HERE, "catalog_features.json"))
NATIVE = os.environ.get("REX_NATIVE_BIN", os.path.join(
    ROOT, "rust", "target", "release", "rex.exe" if os.name == "nt" else "rex"))
ENV = dict(os.environ, REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for _name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY", "PLEX_TOKEN", "ZURG_URL", "REX_GDM_PORTS"):
    ENV.pop(_name, None)

with open(FIXTURE, encoding="utf-8") as _handle:
    BASE = json.load(_handle)
MACHINE = BASE["machineIdentifier"]


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def native_json(command, document):
    """One native CLI adapter call with `document` as its --input."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(document, handle)
    try:
        result = subprocess.run([NATIVE, command, "--input", handle.name],
                                capture_output=True, text=True, check=False)
    finally:
        os.unlink(handle.name)
    if result.returncode:
        raise AssertionError("%s exited %d: %s" % (command, result.returncode, result.stderr.strip()))
    return json.loads(result.stdout)


class Server:
    """A test server on `document` (a catalog dict), written to a private directory."""

    def __init__(self, document=None, args=(), env=None):
        self.directory = tempfile.mkdtemp(prefix="rex-catalog-features-")
        self.catalog = os.path.join(self.directory, "catalog.json")
        with open(self.catalog, "w", encoding="utf-8") as handle:
            json.dump(BASE if document is None else document, handle)
        self.port = free_port()
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", self.catalog, "--assert-claimed",
                           *(args or ("--gdm", "off")), root=ROOT),
            cwd=ROOT, env=dict(ENV, **(env or {})),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            if self.process.poll() is not None:
                break
            try:
                if self.request("GET", "/identity")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        self.stop()
        raise SystemExit("the server did not start on %s" % self.catalog)

    def request(self, method, path, headers=None, accept="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        sent = {"Accept": accept, "Connection": "close"}
        sent.update(headers or {})
        try:
            connection.request(method, path, headers=sent)
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body

    def json(self, path, method="GET", headers=None):
        """The MediaContainer of a 2xx JSON answer, or `{"_status": status}` for anything else."""
        status, _, body = self.request(method, path, headers)
        try:
            document = json.loads(body) if body else None
        except ValueError:
            document = None
        if not 200 <= status < 300 or not isinstance(document, dict):
            return {"_status": status}
        return document.get("MediaContainer", {})

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        shutil.rmtree(self.directory, ignore_errors=True)


def item(document, key):
    return next(row for row in document["items"] if str(row.get("ratingKey")) == str(key))


def first(container):
    return (container.get("Metadata") or [{}])[0]


def hub_answer(server, query, path="/hubs/search"):
    """{hub type: [(title or tag, ratingKey, score, reason, reasonTitle)]} of a search."""
    body = server.json("%s?query=%s&limit=100" % (path, urllib.parse.quote(query)))
    out = {}
    for hub in body.get("Hub") or []:
        rows = (hub.get("Metadata") or []) + (hub.get("Directory") or [])
        if rows:
            out[hub.get("type")] = rows
    return out


def titles(server, query):
    """The typed titles and tag names a search finds, as `search_matches` grouped them."""
    found = hub_answer(server, query)
    typed = {kind: [row.get("title") for row in rows] for kind, rows in found.items()
             if any("title" in row for row in rows)}
    tags = {kind: [row.get("tag") for row in rows] for kind, rows in found.items()
            if all("tag" in row for row in rows)}
    return typed, tags


def hub_score(server, query, rating_key):
    return next((row.get("score") for rows in hub_answer(server, query).values() for row in rows
                 if row.get("ratingKey") == str(rating_key)), None)


def parts_of(container):
    return [str(part.get("id")) for row in container.get("Metadata") or []
            for media in row.get("Media") or [] for part in media.get("Part") or []]


def decision(server, part_index, rating_key=101, extra=None):
    query = {"hasMDE": "1", "path": "/library/metadata/%s" % rating_key,
             "mediaIndex": "0", "partIndex": str(part_index), "X-Plex-Platform": "Chrome"}
    query.update(extra or {})
    return server.json("/video/:/transcode/universal/decision?" + urllib.parse.urlencode(query))


class Zurg(http.server.BaseHTTPRequestHandler):
    """zurg's WebDAV file read, recording the path rex reads each Part from."""

    protocol_version = "HTTP/1.1"
    seen = []
    body = b"\x1a\x45\xdf\xa3" + bytes(996)

    def log_message(self, *args):
        pass

    def _answer(self, send_body):
        Zurg.seen.append(urllib.parse.unquote(self.path))
        self.send_response(200)
        self.send_header("Content-Type", "application/force-download")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        if send_body:
            self.wfile.write(self.body)

    def do_HEAD(self):
        self._answer(False)

    def do_GET(self):
        self._answer(True)


def sse_frames(server, trigger, seconds=4):
    """The TimelineEntry rows the notification channel pushes while `trigger` runs."""
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=seconds + 5)
    sock.sendall(b"GET /:/eventsource/notifications HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 b"Accept: text/event-stream\r\n\r\n")
    timer = threading.Timer(0.5, trigger)
    timer.start()
    data = b""
    deadline = time.time() + seconds
    sock.settimeout(0.3)
    try:
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            data += chunk
    finally:
        timer.join()
        sock.close()
    rows = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            frame = json.loads(line[5:].strip())
        except ValueError:
            continue
        container = frame.get("NotificationContainer", frame) if isinstance(frame, dict) else {}
        entries = container.get("TimelineEntry") or []
        rows.extend(entries if isinstance(entries, list) else [entries])
    return rows


def gdm_stamp(port):
    """(Name, Updated-At) of the GDM block the server answers on udp `port`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.sendto(b"M-SEARCH * HTTP/1.1\r\n", ("127.0.0.1", port))
        block = sock.recvfrom(4096)[0].decode("utf-8")
    except socket.timeout:
        return None, None
    finally:
        sock.close()
    fields = dict(line.split(": ", 1) for line in block.split("\r\n") if ": " in line)
    return fields.get("Name"), fields.get("Updated-At")


# Measured behaviour the Python oracle held and the native server does not, found when
# this suite stopped driving the oracle in-process (2026-10-14). Each still runs: it is
# reported as a known gap while it fails, and fails the suite once it passes, so the entry
# is removed when the native server catches up.
KNOWN_GAPS = {
    "spellfix rebuilds only at the measured optimization call sites":
        "the Butler OptimizeDatabase task and a section delete do not rebuild the spelling index",
    "show merge absorbs its hierarchy and combines matching episode Media":
        "the merged show does not take the absorbed show's Location",
    "duration filter and sort read Media.duration": "sort=duration orders by the row's duration",
    "a mixed folder level renders Directory then Video":
        "a folder row is written <Video type=\"directory\"> in XML",
    "deleting a show's season 0 takes its seasonCount away (a fresh build)":
        "seasonCount stays 1",
    "deleting a show's season 0 takes its seasonCount away (a catalog written before the rule)":
        "seasonCount stays 1",
}


def main():
    failures = []
    gaps = []

    def check(name, condition, detail=""):
        if name in KNOWN_GAPS:
            gaps.append(name)
            if condition:
                print("FAIL", name, "-- passes now: remove it from KNOWN_GAPS")
                failures.append(name)
            else:
                print("GAP ", name, detail, "-- known native gap:", KNOWN_GAPS[name])
            return
        print(("PASS" if condition else "FAIL"), name, detail)
        if not condition:
            failures.append(name)

    # Cycle 41 completed the title-mark gap left by Cycle 32. Across every
    # assigned punctuation, symbol, separator, mark and format scalar in the
    # previously unswept ranges, both PMS versions remove exactly U+0300..U+036F
    # between ASCII sentinels. A generic combining-class test wrongly removed
    # another 699 Mn/Mc scalars and preserved U+034F. The candidate domain is
    # generated by the interpreter's Unicode database, so its size changes
    # between supported Python releases; the removed codepoint set is the
    # measured contract.
    mark_domain = [
        codepoint
        for start, end in ((0x0300, 0x1fff), (0x3000, 0x10ffff))
        for codepoint in range(start, end + 1)
        if unicodedata.category(chr(codepoint)).startswith(("P", "S", "Z", "M"))
        or unicodedata.category(chr(codepoint)) == "Cf"
    ]
    folded = native_json("hierarchy-fold", ["A" + chr(codepoint) + "B" for codepoint in mark_domain])
    removed_marks = [codepoint for codepoint, value in zip(mark_domain, folded) if value == "AB"]
    check("title folding removes exactly the measured combining-diacritics block",
          removed_marks == list(range(0x0300, 0x0370)),
          "%d-domain %d-removed unicode=%s first=%s last=%s"
          % (len(mark_domain), len(removed_marks),
             unicodedata.unidata_version,
             "U+%04X" % removed_marks[0] if removed_marks else "none",
             "U+%04X" % removed_marks[-1] if removed_marks else "none"))

    plain = Server()
    try:
        run_plain(plain, check)
    finally:
        plain.stop()
    run_search_edits(check)
    run_vocabulary(check)
    run_rebuild(check)
    run_additions(check)
    run_durations(check)
    run_zurg(check)
    run_gdm(check)
    run_specials(check)
    run_enrichment(check)

    unchecked = sorted(set(KNOWN_GAPS) - set(gaps))
    for name in unchecked:
        print("FAIL", name, "-- in KNOWN_GAPS but never checked")
    failures += unchecked
    if failures:
        print("FAIL %d catalog feature check(s)" % len(failures))
        return 1
    print("PASS all catalog feature checks (%d known native gaps)" % len(set(gaps)))
    return 0


def run_plain(server, check):
    """The checks the fixture answers as written."""
    # Cycle 37 reconstructed both PMS spellfix tables, including the exact
    # editdist1/rank score, top-ten cap, <=200 cutoff and fallback gate. These
    # titles are the tiny measured plexlab corpus: `zzz` has two candidate words, `job`
    # (110, 140) and `steel` (137, 167), and the fallback reaches the row `steel` names.
    zzz_titles, zzz_tags = titles(server, "zzz")
    check("spellfix candidates use the measured cost and bounded result set",
          zzz_titles == {"movie": ["Tears of Steel"]} and not zzz_tags,
          "%s %s" % (zzz_titles, zzz_tags))
    # Cycle 39 reconstructed the tag vocabulary exactly; the only input not on API rows is
    # this versioned 28-string server-owned table, read from the sidecar.
    expected_static_tags = [
        "Optimized for Mobile", "Optimized for TV", "Original Quality",
        "The Movie Database (Aired)", "TheTVDB (Absolute)",
        "TheTVDB (Aired)", "TheTVDB (Alternate Aired)",
        "TheTVDB (Alternate)", "TheTVDB (Amazon)", "TheTVDB (Arc)",
        "TheTVDB (Blu-ray)", "TheTVDB (BluRay)", "TheTVDB (Combined)",
        "TheTVDB (Disney+)", "TheTVDB (DVD)", "TheTVDB (France)",
        "TheTVDB (International)", "TheTVDB (Legacy)",
        "TheTVDB (Netflix)", "TheTVDB (Ocean Dub)",
        "TheTVDB (Parts (Netflix))", "TheTVDB (Production)",
        "TheTVDB (Sagas)", "TheTVDB (Seasons)",
        "TheTVDB (SkyShowtime)", "TheTVDB (Story)",
        "TheTVDB (Streaming)", "TheTVDB (Superfan)",
    ]
    with open(os.path.join(ROOT, "data", "spellfix_static_tags.json"), encoding="utf-8") as handle:
        static_tags = json.load(handle)
    check("tag spellfix includes the exact measured 1.42.2 static vocabulary",
          static_tags == expected_static_tags, str(static_tags))
    check("misspelled title words reach the measured movie rows",
          titles(server, "teers")[0].get("movie") == ["Tears of Steel"]
          and titles(server, "steal")[0].get("movie") == ["Tears of Steel"]
          and titles(server, "buny")[0].get("movie") == ["Big Buck Bunny"]
          and titles(server, "elefants")[0].get("movie") == ["Elephants Dream"])
    check("fallback ANDs retained tokens and discards only sub-three-byte tokens",
          titles(server, "Tears zzz")[0].get("movie") == ["Tears of Steel"]
          and titles(server, "Tears xy")[0].get("movie") == ["Tears of Steel"]
          and "Tears of Steel" not in titles(server, "Tears xyz")[0].get("movie", []))
    train_titles, _ = titles(server, "train")
    check("an episode-only normal hit leaves the global spelling gate open",
          train_titles.get("episode") == ["The Train Job"]
          and train_titles.get("movie") == ["Elephants Dream"], str(train_titles))

    # Cycle 39's complete 659-cell score-fold sweep found that membership and
    # score tiers are separate passes. Partial tiering applies the measured title
    # table before ASCII case folding; whole-title equality is broader, with the
    # measured long-s position and two-letter macron-o exceptions. The hub score is
    # the tier's: exact 0.93, anchored 0.73, fuzzy 0.33, plus the audience component.
    tier_scores = {
        "TĒars": "0.33055",             # fuzzy
        "TĒar": "0.33055",              # fuzzy
        "Tears Ōf Steel": "0.73055",    # anchored
        "Tears ōf Steel": "0.73055",    # anchored
        "Tearſ of Steel": "0.33055",    # fuzzy
        "TĒars of Steel": "0.93055",    # exact
        "Tears of StĒel": "0.93055",    # exact
        "Tears of ſteel": "0.93055",    # exact
    }
    got_scores = {query: hub_score(server, query, 103) for query in tier_scores}
    check("hub score folding keeps its measured partial and whole-title passes",
          got_scores == tier_scores, str(got_scores))

    # "The Train Job" sorts as "Train Job": `train` anchors on the titleSort, the anchored
    # tier an episode scores (0.71; the oracle's answer).
    anchored_score = hub_score(server, "train", 109)
    check("the anchored search tier reads titleSort", anchored_score == "0.71000",
          str(anchored_score))

    tudik_titles, tudik_tags = titles(server, "Tudik")
    tudik_movies = hub_answer(server, "Tudik").get("movie") or [{}]
    check("the measured people spelling branch emits its tag and associated item",
          tudik_tags.get("actor") == ["Alan Tudyk"]
          and tudik_titles.get("movie") == ["Stacked Feature"]
          and tudik_movies[0].get("reason") == "actor"
          and tudik_movies[0].get("reasonTitle") == "Alan Tudyk",
          "%s %s" % (tudik_titles, tudik_tags))

    profile_rows = server.json("/clients/profiles").get("Profile") or []
    profile_titles = [row.get("title") for row in profile_rows]
    try:
        ordered = (profile_titles.index("Opera TV App") < profile_titles.index("Opera")
                   and profile_titles.index("Roku-3.1") < profile_titles.index("Roku")
                   and profile_titles.index("Samsung SmartTV") < profile_titles.index("Samsung")
                   and profile_titles.index("Sony Blu-ray 2013") < profile_titles.index("Sony Blu-ray")
                   and profile_titles.index("Plex Desktop")
                   < profile_titles.index("Plex Home Theater-RaspberryPi")
                   < profile_titles.index("Plex Home Theater"))
    except ValueError:
        ordered = False
    check("client profiles preserve deterministic resource-file order", ordered,
          str(profile_titles[:8]))

    # Cycle 66: `zh-TW` keeps its full tag -- still no catalogue of its own (English menus),
    # but a real PMS names countries and languages in Traditional Chinese under it. `zh-HK`
    # is English outright. A translated catalogue (`fr`) shows the probe can tell.
    def menus(headers):
        status, _, body = server.request("GET", "/library/sections/1", headers)
        return status, body
    english = menus({"X-Plex-Language": "en"})
    french = menus({"X-Plex-Language": "fr"})
    check("Traditional Chinese request tags select the measured English catalogue",
          english[0] == 200 and french != english
          and server.request("GET", "/library/sections/1?X-Plex-Language=zh-HK")[2] == english[1]
          and menus({"X-Plex-Language": "zh-TW"}) == english,
          "fr differs: %s" % (french != english))

    detail = first(server.json("/library/metadata/101"))
    stacked = (detail.get("Media") or [{}])[0]
    check("a stacked catalog Media carries the summed duration",
          stacked.get("duration") == 333000
          and [p.get("duration") for p in stacked.get("Part") or []] == [111000, 222000],
          str((stacked.get("duration"), [p.get("duration") for p in stacked.get("Part") or []])))

    first_parts = parts_of(decision(server, 0))
    second_parts = parts_of(decision(server, 1))
    invalid = decision(server, 2)
    all_parts = decision(server, -1)
    check("partIndex selects one Part and its own streams",
          first_parts == ["1010"] and second_parts == ["1011"], "%s %s" % (first_parts, second_parts))
    check("an out-of-range multi-part partIndex is a 400", invalid.get("_status") == 400,
          str(invalid.get("_status", 200)))
    check("partIndex -1 returns the measured all-parts conversion refusal",
          all_parts.get("size") == 0 and not all_parts.get("Metadata")
          and all_parts.get("directPlayDecisionCode") == 3000
          and all_parts.get("generalDecisionCode") == 2000
          and all_parts.get("transcodeDecisionCode") == 4005
          and all_parts.get("directPlayDecisionText")
          == "App cannot direct play this item. Multi-part media requires a conversion when all parts are selected.",
          str({k: v for k, v in all_parts.items() if "Decision" in k}))

    dovi = decision(server, 0, 102, {"directPlay": "0"})
    check("the catalog can carry a profile-5 Dolby Vision row to its measured refusal",
          dovi.get("size") == 0 and dovi.get("transcodeDecisionCode") == 2003
          and dovi.get("transcodeDecisionText")
          == "File is unplayable. DoVi (Profile 5) color space is not supported.",
          str({k: v for k, v in dovi.items() if "Decision" in k}))

    parent_row = first(server.json("/library/metadata/101?includeExtras=1"))
    inline = (parent_row.get("Extras") or {}).get("Metadata") or []
    check("the parent advertises its lowest-index trailer and an uncapped inline block",
          parent_row.get("primaryExtraKey") == "/library/metadata/201"
          and (parent_row.get("Extras") or {}).get("size") == 1
          and [row.get("ratingKey") for row in inline] == ["201"]
          and "art" not in inline[0])

    extras = server.json("/library/metadata/101/extras/all")
    section = server.json("/library/sections/1/extras/all?extraType=1")
    section_miss = server.json("/library/sections/1/extras/all?extraType=3")
    item_row = first(extras)
    section_row = first(section)
    forbidden = {"librarySectionID", "librarySectionTitle", "librarySectionKey"}
    check("item and section extras expose the measured full and light projections",
          extras.get("size") == 1 and extras.get("allowSync") is False
          and not (forbidden & set(extras))
          and str(item_row.get("art") or "").startswith("/library/metadata/101/art/")
          and section.get("size") == 1 and not (forbidden & set(section_row))
          and all("Stream" not in part for media in section_row.get("Media") or []
                  for part in media.get("Part") or [])
          and section_miss.get("size") == 0)
    check("a clip carries only its own coverPoster Image on both extras lists",
          item_row.get("Image")
          == [{"alt": "Stacked Feature Trailer", "type": "coverPoster",
               "url": first(server.json("/library/metadata/201")).get("thumb")}]
          and section_row.get("Image") == item_row.get("Image"),
          "item=%s section=%s" % (item_row.get("Image"), section_row.get("Image")))
    untyped_library_keys = [row.get("ratingKey") for row in
                            server.json("/library/all").get("Metadata") or []]
    check("the untyped whole-library listing omits clips",
          "201" not in untyped_library_keys, str(untyped_library_keys))

    clip = server.json("/library/metadata/201")
    clip_row = first(clip)
    check("a faithful clip detail needs no invented section fields",
          clip.get("size") == 1 and clip.get("allowSync") is False
          and not (forbidden & set(clip)) and not (forbidden & set(clip_row))
          and "parentRatingKey" not in clip_row and "_parentRatingKey" not in clip_row)
    art_status = server.request("GET", "/library/metadata/201/art")[0]
    thumb_status, thumb_headers, _ = server.request("GET", "/library/metadata/201/thumb", accept="*/*")
    poster_status, poster_headers, _ = server.request("GET", "/library/metadata/201/poster", accept="*/*")
    check("a clip borrows the art attribute but owns only thumb and poster reads",
          art_status == 404 and thumb_status == 200 and poster_status == 200
          and thumb_headers.get("content-type", "").startswith("image/")
          and poster_headers.get("content-type", "").startswith("image/"),
          "art %s thumb %s %s poster %s %s" % (art_status, thumb_status,
                                              thumb_headers.get("content-type"), poster_status,
                                              poster_headers.get("content-type")))

    xml = server.request("GET", "/library/metadata/101/extras/all", accept="*/*")[2].decode("utf-8")
    match = re.search(r"<Video\b[^>]*", xml)
    attributes = re.findall(r"([A-Za-z0-9_:]+)=", match.group(0)) if match else []
    check("clip XML keeps extraType as its final attribute",
          attributes[-1:] == ["extraType"], str(attributes))

    # Cycle 79: a play queue's `shuffle`/`repeat` flags, cell by cell against what
    # plexscratch42 (1.42.2) and plexscratch (1.43.4) answered, and python-plexapi's own
    # two query shapes (4.17.2 sends `True`, 4.18.2 sends `1`).
    with open(os.path.join(HERE, "fixtures", "playqueue_flags.json"), encoding="utf-8") as handle:
        queue_flags = json.load(handle)
    flag_uri = urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/metadata/108"
                                  % MACHINE, safe="")

    def queue_answer(query):
        body = server.json("/playQueues?%s&uri=%s" % (query, flag_uri), "POST")
        if "_status" in body:
            return [body["_status"], None]
        return [200, body.get("playQueueShuffled")]

    wrong = []
    for flag, cells in queue_flags["cells"].items():
        for value, measured in cells.items():
            expected = set(tuple(cell) for cell in measured.values())
            got = tuple(queue_answer("type=video&%s=%s" % (flag, urllib.parse.quote(value, safe=""))))
            if expected != {got}:
                wrong.append((flag, value, got, sorted(expected)))
    for version, shape in queue_flags["plexapi"].items():
        expected = set(tuple(cell) for key, cell in shape.items() if key != "query")
        got = tuple(queue_answer(shape["query"]))
        if expected != {got}:
            wrong.append(("plexapi " + version, got, sorted(expected)))
    check("a play queue's shuffle and repeat are 0/1 integer flags and anything else is a "
          "400, as both PMS versions answer", not wrong, str(wrong))


def run_search_edits(check):
    """Search answers on rows carrying a sort title, audience ratings and a shared tag."""
    document = copy.deepcopy(BASE)
    # Make the later episode bucket score above the movie. The recovered flat comparator
    # still preserves bucket assembly instead of globally re-sorting it.
    item(document, 109)["titleSort"] = "train"
    # Cycle 38 recovered the final comparator from both PMS binaries. The score is
    # compared at full precision and only formatted to five decimals afterwards. These
    # two rows deliberately serialize to the same value; the higher hidden audience
    # component still wins even though its title sorts later.
    item(document, 101)["audienceRating"] = 8.400
    item(document, 102)["audienceRating"] = 8.372
    for key in (108, 109):
        item(document, key)["Role"] = [{"id": 777, "tag": "Gina Torres", "filter": "actor=777"}]
    server = Server(document)
    try:
        flat = server.json("/library/search?query=train&limit=100")
        rows = flat.get("SearchResult") or []
        check("flat search preserves its measured movie-before-episode buckets",
              [(row.get("Metadata") or {}).get("ratingKey") for row in rows[:2]] == ["105", "109"]
              and len(rows) >= 2 and rows[0].get("score", 0) < rows[1].get("score", 0),
              str([((row.get("Metadata") or {}).get("ratingKey"), row.get("score")) for row in rows]))

        movies = hub_answer(server, "feature").get("movie") or []
        check("hub search sorts the unrounded score before formatting it",
              [row.get("ratingKey") for row in movies[:2]] == ["101", "102"]
              and [row.get("score") for row in movies[:2]] == ["0.33084", "0.33084"],
              str([(row.get("ratingKey"), row.get("score")) for row in movies]))

        counted = server.json("/library/search?query=Gina&limit=100")
        hit = next((result.get("Directory") for result in counted.get("SearchResult") or []
                    if (result.get("Directory") or {}).get("tag") == "Gina Torres"), {})
        check("a search tag count spans every row type in its tag table",
              hit.get("count") == 2, str(hit))
    finally:
        server.stop()


def run_vocabulary(check):
    """A compact reconstruction of the live `emtz` witness. The 5,785 distinct imdb GUID
    strings give that token its measured rank and keep Imtiaz out of the ten-candidate
    vector, exactly as the 1.42.2 reference's visible people hubs do. Without them the
    same people hub names him: the cap, not the distance, is what keeps him out."""
    ranks = {"ents": 2, "hintze": 1, "ends": 2, "entre": 1, "hunts": 2,
             "enter": 4, "amter": 1, "ent": 3, "emde": 1}
    roles = [{"tag": word + " " * index} for word, count in ranks.items() for index in range(count)]
    found = {}
    for ballast in (True, False):
        document = copy.deepcopy(BASE)
        document["items"].append({
            "ratingKey": "47101", "key": "/library/metadata/47101", "type": "movie",
            "title": "Vocabulary Witness", "librarySectionID": 1,
            "Role": roles + [{"tag": "Imtiaz Ali"}],
            "Guid": [{"id": "imdb://tt%07d" % index} for index in range(5785)] if ballast else []})
        server = Server(document)
        try:
            found[ballast] = titles(server, "emtz")[1].get("actor") or []
        finally:
            server.stop()
    check("GUID rank keeps the measured emtz people candidate outside the cap",
          "Imtiaz Ali" not in found[True] and "Imtiaz Ali" in found[False], str(found))


def run_rebuild(check):
    """The real spellfix tables are stale between rebuilds. Both binaries call the rebuild
    from explicit optimization, the Butler task, and section-deletion completion; ordinary
    title edits do not maintain it row by row."""
    server = Server()
    try:
        def rebuilt(query, kind):
            typed = hub_answer(server, query).get(kind) or []
            return [row.get("ratingKey") for row in typed]

        def rename(key, kind, title):
            server.request("PUT", "/library/metadata/%s?type=%d&id=%s&title.value=%s"
                           % (key, kind, key, urllib.parse.quote(title)))

        rename(101, 1, "Zebracorn")
        stale_explicit = rebuilt("zebracrn", "movie")
        server.request("PUT", "/library/optimize")
        fresh_explicit = rebuilt("zebracrn", "movie")
        rename(101, 1, "Quokkahedron")
        stale_butler = rebuilt("quokkahedrn", "movie")
        server.request("POST", "/butler/OptimizeDatabase")
        fresh_butler = rebuilt("quokkahedrn", "movie")
        rename(108, 2, "Xylophonic")
        stale_delete = rebuilt("xylophnic", "show")
        server.request("DELETE", "/library/sections/1")
        fresh_delete = rebuilt("xylophnic", "show")
        check("spellfix rebuilds only at the measured optimization call sites",
              stale_explicit == [] and fresh_explicit == ["101"]
              and stale_butler == [] and fresh_butler == ["101"]
              and stale_delete == [] and fresh_delete == ["108"],
              str([stale_explicit, fresh_explicit, stale_butler, fresh_butler,
                   stale_delete, fresh_delete]))
    finally:
        server.stop()


def show_rows(show_key, season_key, first_key, section, title, episodes=1, **episode):
    rows = [{"ratingKey": show_key, "key": "/library/metadata/%s" % show_key,
             "guid": "local://%s" % show_key, "type": "show", "title": title,
             "librarySectionID": section},
            {"ratingKey": season_key, "key": "/library/metadata/%s" % season_key,
             "type": "season", "title": "Season 1", "index": 1, "parentTitle": title,
             "parentRatingKey": show_key, "librarySectionID": section}]
    for offset in range(episodes):
        key = str(int(first_key) + offset)
        rows.append(dict({"ratingKey": key, "key": "/library/metadata/%s" % key,
                          "type": "episode", "title": "Episode %d" % (offset + 1),
                          "index": offset + 1, "parentIndex": 1, "parentTitle": "Season 1",
                          "parentRatingKey": season_key, "grandparentRatingKey": show_key,
                          "grandparentTitle": title, "librarySectionID": section}, **episode))
    return rows


def run_additions(check):
    """Rows added beside the fixture's own, and the fixture rows a check edits."""
    document = copy.deepcopy(BASE)
    items = document["items"]
    for key, title in (("99", "Cap Shows"), ("98", "Rollup One"), ("94", "Rollup Two"),
                       ("97", "Played Library"), ("96", "Alone Library")):
        document["sections"].append({"key": key, "type": "show", "title": title,
                                     "uuid": "c37-section-%s" % key})

    # Cycle 50 measured the season form on both supported PMS versions. Its OnDeck
    # row is selected for the whole show, then exposed only by the season that owns
    # that episode; it is not each season's first unwatched episode.
    items += [
        {"ratingKey": "45101", "key": "/library/metadata/45101/children",
         "type": "show", "title": "On Deck Show", "librarySectionID": 2},
        {"ratingKey": "45102", "key": "/library/metadata/45102/children",
         "type": "season", "title": "Season 1", "index": 1,
         "parentRatingKey": "45101", "librarySectionID": 2},
        {"ratingKey": "45103", "key": "/library/metadata/45103/children",
         "type": "season", "title": "Season 2", "index": 2,
         "parentRatingKey": "45101", "librarySectionID": 2},
        {"ratingKey": "45104", "key": "/library/metadata/45104", "type": "episode",
         "title": "Watched", "index": 1, "parentIndex": 1,
         "parentRatingKey": "45102", "grandparentRatingKey": "45101",
         "librarySectionID": 2, "viewCount": 1, "lastViewedAt": 100},
        {"ratingKey": "45105", "key": "/library/metadata/45105", "type": "episode",
         "title": "Next", "index": 2, "parentIndex": 1,
         "parentRatingKey": "45102", "grandparentRatingKey": "45101",
         "librarySectionID": 2},
        {"ratingKey": "45106", "key": "/library/metadata/45106", "type": "episode",
         "title": "Later Season", "index": 1, "parentIndex": 2,
         "parentRatingKey": "45103", "grandparentRatingKey": "45101",
         "librarySectionID": 2},
    ]
    item(document, 108)["CommonSenseMedia"] = [{"tag": "Age Twelve"}]
    # Cycle 55: the shelf is the section's roll-up, so this one-episode show is its
    # EPISODE there; either row carries the newest leaf's stamp.
    item(document, 108)["addedAt"] = 300
    item(document, 109)["addedAt"] = 200
    # The inline OnDeck row of the show: its one episode in progress.
    item(document, 109)["viewOffset"] = 100
    item(document, 109)["lastViewedAt"] = 100
    # A children container carries only artwork its parent owns: one season borrowing its
    # show's thumb, one owning its own.
    for show, season, episode, owned in (("45201", "45202", "45203", None),
                                         ("45211", "45212", "45213", "/library/metadata/45212/thumb/21")):
        items += [
            {"ratingKey": show, "key": "/library/metadata/%s/children" % show,
             "type": "show", "title": "Thumb Show %s" % show, "librarySectionID": 2,
             "thumb": "/library/metadata/%s/thumb/20" % show},
            {"ratingKey": season, "key": "/library/metadata/%s/children" % season,
             "type": "season", "title": "Season 1", "index": 1,
             "parentRatingKey": show, "librarySectionID": 2,
             "thumb": owned or "/library/metadata/%s/thumb/20" % show},
            {"ratingKey": episode, "key": "/library/metadata/%s" % episode, "type": "episode",
             "title": "Episode", "index": 1, "parentRatingKey": season,
             "grandparentRatingKey": show, "librarySectionID": 2},
        ]
    items += [
        {"ratingKey": "45301", "key": "/library/metadata/45301/children",
         "type": "show", "title": "Restamp Show", "librarySectionID": 2,
         "thumb": "/library/metadata/45301/thumb/999",
         "art": "/library/metadata/45301/art/999"},
        {"ratingKey": "45302", "key": "/library/metadata/45302/children",
         "type": "season", "title": "Season 1", "index": 1,
         "parentRatingKey": "45301", "librarySectionID": 2,
         "thumb": "/library/metadata/45301/thumb/999",
         "art": "/library/metadata/45301/art/999"},
        {"ratingKey": "45303", "key": "/library/metadata/45303", "type": "episode",
         "title": "Restamped Episode", "index": 1, "parentRatingKey": "45302",
         "grandparentRatingKey": "45301", "librarySectionID": 2,
         "addedAt": 123, "thumb": "/library/metadata/45303/thumb/456",
         "grandparentThumb": "/library/metadata/45301/thumb/999",
         "art": "/library/metadata/45301/art/999"},
    ]
    # Cycle 41 created the same two-Media movie on writable copies of both PMS versions.
    two_media = [copy.deepcopy(item(BASE, 101)["Media"][0]), copy.deepcopy(item(BASE, 102)["Media"][0])]
    item(document, 101)["Media"] = two_media
    media_ids = [str(media.get("id")) for media in two_media]
    # A search tier no fixture title reaches: a prefix of a whole title.
    items += [
        {"ratingKey": "47001", "key": "/library/metadata/47001", "type": "movie",
         "title": "Dream", "librarySectionID": 1},
    ]
    # Cycle 44: a season already written with its own year.
    items += [
        {"ratingKey": "9001", "type": "show", "title": "Load Show", "year": 2008,
         "guid": "plex://show/aaaaaaaaaaaaaaaaaaaaaaaa",
         "key": "/library/metadata/9001/children", "librarySectionID": 2},
        {"ratingKey": "9002", "type": "season", "title": "Season 1", "index": 1,
         "year": 2009, "parentRatingKey": "9001", "parentTitle": "Load Show",
         "guid": "plex://season/bbbbbbbbbbbbbbbbbbbbbbbb",
         "key": "/library/metadata/9002/children", "librarySectionID": 2},
    ]
    # A film really called "Specials" is content, not a generated title.
    items.append({"ratingKey": "47003", "key": "/library/metadata/47003", "type": "movie",
                  "title": "Specials", "index": 0, "librarySectionID": 1})
    # Cycle 41 measured show merge independently on both disposable PMS versions.
    target_show = {
        "ratingKey": "24101", "key": "/library/metadata/24101",
        "guid": "local://24101", "type": "show", "title": "Merge Target",
        "librarySectionID": 2, "librarySectionTitle": "Feature Shows",
        "librarySectionKey": "/library/sections/2", "childCount": 1,
        "leafCount": 1, "viewedLeafCount": 0,
        "Location": [{"id": 1, "path": "/target"}],
    }
    source_show = dict(target_show, ratingKey="24102", key="/library/metadata/24102",
                       guid="local://24102", title="Merge Source",
                       Location=[{"id": 2, "path": "/source"}])
    target_season = {
        "ratingKey": "34101", "key": "/library/metadata/34101",
        "type": "season", "title": "Season 1", "index": 1,
        "parentRatingKey": "24101", "parentKey": "/library/metadata/24101",
        "librarySectionID": 2, "leafCount": 1, "viewedLeafCount": 0,
    }
    source_season = dict(target_season, ratingKey="34102",
                         key="/library/metadata/34102", parentRatingKey="24102",
                         parentKey="/library/metadata/24102")
    target_episode = {
        "ratingKey": "14101", "key": "/library/metadata/14101",
        "type": "episode", "title": "Target Episode", "index": 1,
        "parentIndex": 1, "parentRatingKey": "34101",
        "parentKey": "/library/metadata/34101", "grandparentRatingKey": "24101",
        "grandparentKey": "/library/metadata/24101", "librarySectionID": 2,
        "Media": [{"id": 141010, "Part": [{"id": 141010}]}],
    }
    source_episode = dict(target_episode, ratingKey="14102",
                          key="/library/metadata/14102", title="Source Episode",
                          parentRatingKey="34102", parentKey="/library/metadata/34102",
                          grandparentRatingKey="24102", grandparentKey="/library/metadata/24102",
                          Media=[{"id": 141020, "Part": [{"id": 141020}]}])
    items += [target_show, source_show, target_season, source_season, target_episode, source_episode]
    # Cycle 41 supplied the second large-library witness for Cycle 28's fitted 50-row TV cap.
    for number in range(55):
        show_key, season_key, episode_key = (str(25000 + number), str(35000 + number),
                                             str(15000 + number))
        items += [
            {"ratingKey": show_key, "key": "/library/metadata/" + show_key,
             "guid": "local://" + show_key, "type": "show",
             "title": "Cap Show %02d" % number, "librarySectionID": 99,
             "childCount": 1, "leafCount": 1, "viewedLeafCount": 0},
            {"ratingKey": season_key, "key": "/library/metadata/" + season_key,
             "type": "season", "title": "Season 1", "index": 1,
             "parentRatingKey": show_key, "librarySectionID": 99,
             "leafCount": 1, "viewedLeafCount": 0},
            {"ratingKey": episode_key, "key": "/library/metadata/" + episode_key,
             "type": "episode", "title": "Cap Episode %02d" % number,
             "index": 1, "parentIndex": 1, "parentRatingKey": season_key,
             "grandparentRatingKey": show_key, "librarySectionID": 99,
             "addedAt": 1789700000 + number},
        ]
    # Cycle 41's controlled add history: one new episode remains an episode; a sibling
    # under the same season replaces it with that season. Library 98 holds the first state
    # and library 94 the second.
    items += show_rows("24901", "34901", "14901", 98, "Rollup Show", addedAt=1789800001)
    items += show_rows("24911", "34911", "14911", 94, "Rollup Show", episodes=2)
    item(document, 14912)["addedAt"] = 1789800002
    item(document, 14911)["addedAt"] = 1789800001
    # Cycle 59: a library's Continue Watching lists a SCROBBLED next-up once ANY show in
    # that library has an episode completed in a playback session.
    timed = {"duration": 600000, "Media": [{"duration": 600000, "Part": [{"duration": 600000}]}]}
    for rows in (show_rows("25901", "35901", "15901", 97, "Played Show", 2, **timed),
                 show_rows("25902", "35902", "15911", 97, "Scrobbled Show", 2, **timed),
                 show_rows("25903", "35903", "15921", 96, "Alone Show", 2, **timed)):
        for offset, row in enumerate(rows[2:]):
            for media in row["Media"]:
                media["id"] = int(row["ratingKey"]) * 10
                media["Part"][0]["id"] = int(row["ratingKey"]) * 10
        items += rows
    item(document, 15911).update(viewCount=1, lastViewedAt=1789900001)
    item(document, 15921).update(viewCount=1, lastViewedAt=1789900002)

    server = Server(document)
    try:
        def get(path):
            return server.json(path)

        season_one_row = first(get("/library/metadata/45102?includeOnDeck=1"))
        season_two_row = first(get("/library/metadata/45103?includeOnDeck=1"))
        check("a season exposes only the show's own On Deck episode",
              ((season_one_row.get("OnDeck") or {}).get("Metadata") or {}).get("ratingKey")
              == "45105" and "OnDeck" not in season_two_row,
              "season1=%s season2=%s"
              % (((season_one_row.get("OnDeck") or {}).get("Metadata") or {}).get("ratingKey"),
                 ((season_two_row.get("OnDeck") or {}).get("Metadata") or {}).get("ratingKey")))

        common_sense_detail = first(get("/library/metadata/108"))
        common_sense_grid = next((row for row in get("/library/sections/2/all").get("Metadata") or []
                                  if row.get("ratingKey") == "108"), {})
        check("CommonSenseMedia is detail-only and absent from a light row",
              common_sense_detail.get("CommonSenseMedia") == [{"tag": "Age Twelve"}]
              and common_sense_grid and "CommonSenseMedia" not in common_sense_grid,
              "detail=%s grid=%s"
              % (common_sense_detail.get("CommonSenseMedia"),
                 common_sense_grid.get("CommonSenseMedia")))

        section_recent = next((h for h in get("/hubs/sections/2").get("Hub") or []
                               if h.get("hubIdentifier") == "tv.recentlyadded.2"), {})
        promoted_recent = next((h for h in get("/hubs/promoted?contentDirectoryID=2").get("Hub") or []
                                if h.get("hubIdentifier") == "tv.recentlyadded.2"), {})
        section_stamp = next((r.get("addedAt") for r in section_recent.get("Metadata") or []
                              if r.get("ratingKey") in ("108", "109")), None)
        promoted_stamp = next((r.get("addedAt") for r in promoted_recent.get("Metadata") or []
                               if r.get("ratingKey") in ("108", "109")), None)
        check("the TV Recently Added shelves date a show by its newest leaf",
              section_stamp == 200 and promoted_stamp == 200,
              "section=%s promoted=%s" % (section_stamp, promoted_stamp))
        # Cycle 55, measured on the 1.42.2 reference (twelve of twelve rows): the library's TV
        # shelf IS the section's mixed roll-up -- a one-episode show is its EPISODE there,
        # exactly as on `/hubs/home/recentlyAdded?type=2&sectionID=2` -- not a list of
        # top-level shows.
        rolled = [r.get("ratingKey") for r in
                  get("/hubs/home/recentlyAdded?type=2&sectionID=2").get("Metadata") or []]
        shelf_keys = [r.get("ratingKey") for r in section_recent.get("Metadata") or []]
        check("the library's TV shelf is the section's roll-up",
              shelf_keys == rolled[:len(shelf_keys)] and shelf_keys,
              "shelf=%s rollup=%s" % (shelf_keys[:6], rolled[:6]))

        borrowed_thumb = get("/library/metadata/45202/children").get("thumb")
        owned_thumb = get("/library/metadata/45212/children").get("thumb")
        check("a children container carries only artwork its parent owns",
              borrowed_thumb is None
              and str(owned_thumb).startswith("/library/metadata/45212/thumb/"),
              "borrowed=%r owned=%r" % (borrowed_thumb, owned_thumb))

        restamped_leaf = first(get("/library/metadata/45301/allLeaves"))
        restamped_images = {image.get("type"): image.get("url")
                            for image in restamped_leaf.get("Image") or []}
        check("allLeaves restamps borrowed fields and their derived Image rows together",
              restamped_leaf.get("grandparentThumb") == "/library/metadata/45301/thumb/123"
              and restamped_images.get("coverPoster") == "/library/metadata/45301/thumb/123"
              and restamped_images.get("background") == "/library/metadata/45301/art/123"
              and restamped_images.get("snapshot") == first(get("/library/metadata/45303")).get("thumb"),
              "fields=%r images=%s"
              % (restamped_leaf.get("grandparentThumb"), restamped_images))

        inline_deck_show = first(get("/library/metadata/108?includeOnDeck=1&includeExtras=1"))
        inline_deck_row = (inline_deck_show.get("OnDeck") or {}).get("Metadata") or {}
        check("includeExtras reaches the inline OnDeck row",
              (inline_deck_row.get("Extras") or {}).get("size") == 0,
              str(inline_deck_row.get("Extras")))

        # Unlike a single-Media row, a two-Media decision has no unambiguous fallback:
        # missing, empty, nonnumeric, negative and out-of-range mediaIndex values are 400.
        # Explicit 0/1 each return exactly the selected Media.
        def multi_decision(value=None, present=True):
            query = {"hasMDE": "1", "path": "/library/metadata/101", "partIndex": "0",
                     "X-Plex-Platform": "Chrome"}
            if present:
                query["mediaIndex"] = value
            return get("/video/:/transcode/universal/decision?" + urllib.parse.urlencode(query))

        valid_media = {}
        for index in ("0", "1"):
            body = multi_decision(index)
            valid_media[index] = [media for row in body.get("Metadata") or []
                                  for media in row.get("Media") or []]
        invalid_media = [multi_decision(None, False), multi_decision(""), multi_decision("abc"),
                         multi_decision("-1"), multi_decision("2")]
        check("a multi-Media decision requires one explicit in-range mediaIndex",
              all(answer.get("_status") == 400 for answer in invalid_media)
              and all(len(valid_media[index]) == 1
                      and str(valid_media[index][0].get("id")) == media_ids[int(index)]
                      and valid_media[index][0].get("selected") is True
                      for index in ("0", "1")),
              "valid=%s invalid=%s"
              % ({key: [row.get("id") for row in rows] for key, rows in valid_media.items()},
                 [answer.get("_status", 200) for answer in invalid_media]))
        # A play queue deliberately ignores the selector and carries both Media, in detail
        # order, with neither marked selected.
        queue_media = []
        queue_uri = "server://%s/com.plexapp.plugins.library/library/metadata/101" % MACHINE
        for value, present in ((None, False), ("0", True), ("1", True),
                               ("abc", True), ("-1", True), ("2", True)):
            params = {"type": "video", "uri": queue_uri}
            if present:
                params["mediaIndex"] = value
            body = server.json("/playQueues?" + urllib.parse.urlencode(params), "POST")
            queue_media.append([(str(media.get("id")), media.get("selected"))
                                for row in body.get("Metadata") or []
                                for media in row.get("Media") or []])
        check("a multi-Media queue ignores mediaIndex and keeps every version",
              all(rows == [(media_ids[0], None), (media_ids[1], None)] for rows in queue_media),
              str(queue_media))

        # Cycle 39's tier a fixture title cannot reach: `Ðre` is a prefix of the whole
        # title "Dream", the prefix tier (0.53).
        prefix_score = hub_score(server, "Ðre", 47001)
        check("a prefix of a whole title scores the prefix tier",
              prefix_score == "0.53000", str(prefix_score))

        loaded = first(get("/library/metadata/9002"))
        check("a season written with its own year loads with parentYear instead",
              loaded.get("ratingKey") == "9002" and loaded.get("year") is None
              and loaded.get("parentYear") == 2008,
              "year=%r parentYear=%r" % (loaded.get("year"), loaded.get("parentYear")))

        specials_movie = first(server.json("/library/metadata/47003",
                                           headers={"X-Plex-Language": "fr"}))
        check("a movie titled Specials keeps its own title in French",
              specials_movie.get("title") == "Specials", str(specials_movie.get("title")))

        def recent_cap(path, headers=None):
            return server.json(path, headers=headers)

        cap_first = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99",
                               {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "100"})
        cap_after = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99",
                               {"X-Plex-Container-Start": "50", "X-Plex-Container-Size": "100"})
        cap_limit = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99&limit=100")
        cap_count = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99&count=100")
        check("Recently Added TV exposes and pages its measured 50-row cap",
              [(body.get("size"), body.get("totalSize"), body.get("offset"),
                len(body.get("Metadata") or []))
               for body in (cap_first, cap_after, cap_limit, cap_count)]
              == [(50, 50, 0, 50), (0, 50, 50, 0),
                  (50, 50, 0, 50), (50, 50, 0, 50)],
              str([(body.get("size"), body.get("totalSize"), body.get("offset"))
                   for body in (cap_first, cap_after, cap_limit, cap_count)]))

        def rollup(section):
            return get("/hubs/home/recentlyAdded?type=2&sectionID=%s" % section).get("Metadata") or []
        one_sibling = rollup(98)
        two_siblings = rollup(94)
        check("recent TV rolls one new episode to itself and two siblings to the season",
              [(row.get("type"), row.get("title"), row.get("parentTitle"),
                row.get("grandparentTitle"), row.get("addedAt")) for row in one_sibling]
              == [("episode", "Episode 1", "Season 1", "Rollup Show", 1789800001)]
              and [(row.get("type"), row.get("title"), row.get("parentTitle"),
                    row.get("addedAt")) for row in two_siblings]
              == [("season", "Season 1", "Rollup Show", 1789800002)],
              "one %r two %r" % ([(r.get("type"), r.get("title"), r.get("addedAt")) for r in one_sibling],
                                 [(r.get("type"), r.get("title"), r.get("addedAt")) for r in two_siblings]))

        # A completion in a playback session: a timeline under 90%, then one past it.
        session = {"X-Plex-Client-Identifier": "catalog-features-player",
                   "X-Plex-Session-Identifier": "catalog-features-session",
                   "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}
        for state, offset in (("playing", 10000), ("playing", 300000), ("stopped", 590000)):
            server.request("GET", "/:/timeline?ratingKey=15901&key=%%2Flibrary%%2Fmetadata%%2F15901"
                           "&state=%s&time=%d&duration=600000" % (state, offset), session)
        played = get("/hubs/sections/97/continueWatching/items").get("Metadata") or []
        alone = get("/hubs/sections/96/continueWatching/items").get("Metadata") or []
        everywhere = get("/hubs/continueWatching/items").get("Metadata") or []
        played_keys = [row.get("ratingKey") for row in played]
        alone_keys = [row.get("ratingKey") for row in alone]
        every_keys = [row.get("ratingKey") for row in everywhere]
        check("a library's play history opens the scrobbled next-up gate for every show in it",
              "15912" in played_keys and "15922" not in alone_keys and "15922" in every_keys,
              "library 97 %s, library 96 %s, everywhere %s" % (played_keys, alone_keys, every_keys))

        # Merging a show is not a no-op merely because the parent has no Media: matching
        # episodes combine versions and the absorbed hierarchy vanishes.
        answers = []
        frames = sse_frames(server, lambda: answers.append(
            server.request("PUT", "/library/metadata/24101/merge?ids=24102")[0]))
        target = first(get("/library/metadata/24101"))
        episode_media = [media.get("id") for media in first(get("/library/metadata/14101")).get("Media") or []]
        gone = [get("/library/metadata/%s" % key).get("_status") for key in (24102, 34102, 14102)]
        locations = [row.get("path") for row in target.get("Location") or []]
        check("show merge absorbs its hierarchy and combines matching episode Media",
              answers == [200] and gone == [404, 404, 404]
              and episode_media == [141020, 141010]
              and locations == ["/target", "/source"]
              and (target.get("childCount"), target.get("leafCount")) == (1, 1),
              "answer %r gone %r media %r locations %r counts %r" %
              (answers, gone, episode_media, locations,
               (target.get("childCount"), target.get("leafCount"))))
        deleted_frames, survivor_frames = frames[:3], frames[3:]
        check("show merge sends exact delete and survivor timeline sequence",
              [(frame.get("itemID"), frame.get("type"), frame.get("state"),
                frame.get("title"), frame.get("metadataState"))
               for frame in deleted_frames]
              == [("24102", 2, 9, "", "deleted"),
                  ("34102", 3, 9, "", "deleted"),
                  ("14102", 4, 9, "", "deleted")]
              and [(frame.get("itemID"), frame.get("type"), frame.get("state"),
                    "metadataState" in frame)
                   for frame in survivor_frames]
              == [("14101", -1, 3, False), ("14101", -1, 5, False)]
              and all(str(frame.get("sectionID")) == "2" for frame in frames),
              str(frames))
    finally:
        server.stop()


def run_durations(check):
    """Durations on rows whose own and Media durations disagree."""
    document = copy.deepcopy(BASE)
    item(document, 101)["_libraryAllDuration"] = 888084
    # Cycle 41 adds the differentiating fixture cycle 19 lacked: row duration and
    # Media.duration deliberately disagree, and one Media omits it. Both the filter
    # and the ascending sort read the Media column on a real PMS.
    for key, row_duration, media_duration in ((103, 300, 100), (104, 100, 300), (105, 50, None)):
        row = item(document, key)
        row["duration"] = row_duration
        row["Media"] = [{"id": 5000 + key, "duration": media_duration}]
    server = Server(document)
    try:
        untyped = {row.get("ratingKey"): row for row in server.json("/library/all").get("Metadata") or []}
        typed = {row.get("ratingKey"): row for row in server.json("/library/all?type=1").get("Metadata") or []}
        check("untyped library durations use their stored metadata column",
              untyped.get("101", {}).get("duration") == 888084
              and "102" in untyped and "duration" not in untyped["102"]
              and typed.get("101", {}).get("duration") == 333000
              and typed.get("102", {}).get("duration") == 10000,
              "untyped=%s typed=%s"
              % ({key: row.get("duration") for key, row in untyped.items() if key in ("101", "102")},
                 {key: row.get("duration") for key, row in typed.items() if key in ("101", "102")}))

        def keys(query):
            return [row.get("ratingKey") for row in
                    server.json("/library/sections/1/all?type=1&" + query).get("Metadata") or []
                    if row.get("ratingKey") in ("103", "104", "105")]
        filtered = [keys("duration=100"), keys("duration=300")]
        order = keys("sort=duration")
        check("duration filter and sort read Media.duration",
              filtered == [["103"], ["104"]] and order == ["105", "103", "104"],
              "filters %s order %s" % (filtered, order))
    finally:
        server.stop()


def run_zurg(check):
    """Part reads behind a local zurg: each Part's own file, the folder tree its files
    make, and a download's file name."""
    zurg = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Zurg)
    threading.Thread(target=zurg.serve_forever, daemon=True).start()
    document = copy.deepcopy(BASE)
    # Cycle 41, measured on a level containing both a direct file and a subfolder: JSON
    # carries an untyped directory row followed by the movie row, while XML names those
    # same rows `<Directory>` then `<Video>`.
    item(document, 103)["Media"] = [{"id": 4103, "Part": [{"id": 4103, "file": "/rex/1/A/direct.mkv"}]}]
    item(document, 104)["Media"] = [{"id": 4104, "Part": [{"id": 4104, "file": "/rex/1/A/B/deep.mkv"}]}]
    # Cycle 41 measured this exact plus-named download on disposable copies of both
    # supported PMS versions.
    item(document, 105)["Media"] = [{"id": 4105, "container": "mkv", "Part": [
        {"id": 4105, "key": "/library/parts/4105/1/file.mkv", "container": "mkv", "size": 1000,
         "file": "/rex/1/C41C Plus+Name (2202).mkv"}]}]
    server = Server(document, args=("--gdm", "off", "--zurg-mount", "/rex"),
                    env={"ZURG_URL": "http://127.0.0.1:%d" % zurg.server_address[1]})
    try:
        media = (first(server.json("/library/metadata/101")).get("Media") or [{}])[0]
        read = []
        for part in media.get("Part") or []:
            del Zurg.seen[:]
            server.request("GET", part.get("key"), {"Range": "bytes=0-9"}, accept="*/*")
            read.append(Zurg.seen[-1] if Zurg.seen else None)
        check("each Part reads its own file from zurg",
              read == ["/dav/1/Stacked Feature - cd1.mkv", "/dav/1/Stacked Feature - cd2.mkv"],
              str(read))

        top = server.json("/library/sections/1/folder")
        folder = next((row for row in top.get("Metadata") or [] if row.get("title") == "A"), {})
        xml = server.request("GET", folder.get("key") or "/library/sections/1/folder?parent=1",
                             accept="*/*")[2]
        try:
            mixed_tags = [child.tag for child in ET.fromstring(xml)]
        except ET.ParseError:
            mixed_tags = [xml[:120]]
        check("a mixed folder level renders Directory then Video",
              mixed_tags == ["Directory", "Video"], "%s via %s" % (mixed_tags, folder.get("key")))

        status, headers, _ = server.request("HEAD", "/library/parts/4105/1/file.mkv?download=1",
                                            accept="*/*")
        disposition = headers.get("content-disposition")
        check("download disposition maps plus only in the legacy filename",
              disposition == ('attachment; filename="C41C Plus_Name (2202).mkv"; '
                              "filename*=UTF-8''C41C%20Plus%2BName%20%282202%29%2Emkv"),
              "%s %s" % (status, disposition))
    finally:
        server.stop()
        zurg.shutdown()


def run_gdm(check):
    """Cycle 50 measured the GDM advertisement stamp as a library-lifecycle value, not a
    generic "anything advertised changed" clock. Create, edit and delete a section each
    move it. A FriendlyName write changes Name immediately without touching the stamp,
    and refresh, item edit, collection create and index delete are all non-events. The
    stamp is a second, so each step waits past one before it."""
    ports = (free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM))
    server = Server(args=("--gdm", "auto"), env={"REX_GDM_PORTS": "%d,%d" % ports})
    try:
        _, stamp = gdm_stamp(ports[0])
        time.sleep(1.2)
        quiet = [
            server.request("PUT", "/:/prefs?FriendlyName=Cycle50GDM")[0],
            server.request("GET", "/library/sections/1/refresh")[0],
            server.request("PUT", "/library/metadata/101?type=1&id=101&title.value=Cycle50GDM")[0],
            server.request("POST", "/library/collections?type=1&sectionId=1&title=Cycle50GDM")[0],
            server.request("DELETE", "/library/sections/1/indexes")[0],
        ]
        name, quiet_stamp = gdm_stamp(ports[0])
        check("non-library-lifecycle writes leave GDM Updated-At untouched",
              stamp is not None and quiet_stamp == stamp and name == "Cycle50GDM"
              and all(status < 500 for status in quiet),
              "stamp %s -> %s name=%r statuses %s" % (stamp, quiet_stamp, name, quiet))
        stamps = [quiet_stamp]
        time.sleep(1.2)
        created = server.json("/library/sections?name=Cycle50GDM&type=movie"
                              "&agent=tv.plex.agents.movie&scanner=Plex%20Movie&language=en-US"
                              "&location=%2Ftmp", "POST")
        created_key = str(((created.get("Directory") or [{}])[0]).get("key") or "")
        stamps.append(gdm_stamp(ports[0])[1])
        time.sleep(1.2)
        edited = server.request("PUT", "/library/sections/%s?agent=tv.plex.agents.movie"
                                "&name=Cycle50GDM2" % created_key)[0]
        stamps.append(gdm_stamp(ports[0])[1])
        time.sleep(1.2)
        deleted = server.request("DELETE", "/library/sections/%s" % created_key)[0]
        stamps.append(gdm_stamp(ports[0])[1])
        moved = all(later is not None and earlier is not None and int(later) > int(earlier)
                    for earlier, later in zip(stamps, stamps[1:]))
        check("library create, edit and delete each move GDM Updated-At",
              created_key and moved and edited == 200 and deleted == 200,
              "key=%r stamps=%s edit %s delete %s" % (created_key, stamps, edited, deleted))
    finally:
        server.stop()


def run_specials(check):
    """Cycle 79: season 0, driven from what the 1.42.2 reference answered for its four shows
    that hold one (tests/fixtures/specials_zen.json) and its control show without. Each
    show is rebuilt from one leaf per season of the 1.42.2 reference through the catalog
    builder, and once more as a catalog written before the rule (season 0 stored as
    `Season 0`), which the load has to correct."""
    with open(os.path.join(HERE, "fixtures", "specials_zen.json"), encoding="utf-8") as handle:
        zen_specials = json.load(handle)["shows"]
    leaves = []
    for number, show in enumerate(zen_specials):
        for season in show["seasons"]:
            leaves.append({
                "ratingKey": str(46000 + len(leaves)), "title": "Leaf %d" % len(leaves),
                "_kind": "tv", "_tmdb_id": 79000 + number, "_show_title": show["title"],
                "_season": season["index"], "_episode": 1, "addedAt": 1, "updatedAt": 1})
    built = native_json("hierarchy-build", leaves)

    for legacy in (False, True):
        label = "a catalog written before the rule" if legacy else "a fresh build"
        rows = copy.deepcopy(built)
        if legacy:
            for row in rows:
                if row.get("type") == "season" and row.get("index") == 0:
                    row["title"] = "Season 0"
                if row.get("type") == "episode" and row.get("parentIndex") == 0:
                    row["parentTitle"] = "Season 0"
                if row.get("type") == "show":
                    row.pop("seasonCount", None)
        server = Server({"machineIdentifier": "c79specials", "friendlyName": "c79",
                         "sections": BASE["sections"], "items": rows})
        try:
            shows = {row.get("title"): row for row in rows if row.get("type") == "show"}
            wrong_titles, wrong_counts = [], []
            for show in zen_specials:
                row = shows[show["title"]]
                children = server.json("/library/metadata/%s/children" % row["ratingKey"])
                got_titles = [season.get("title") for season in children.get("Metadata") or []]
                if got_titles != [season["title"] for season in show["seasons"]]:
                    wrong_titles.append((show["title"], got_titles))
                status, _, body = server.request("GET", "/library/metadata/%s" % row["ratingKey"])
                detail = ((json.loads(body).get("MediaContainer") or {}).get("Metadata") or [{}])[0] \
                    if status == 200 else {}
                keys = list(detail)
                placed = ("seasonCount" not in keys if show["seasonCount"] is None else
                          "childCount" in keys
                          and keys[keys.index("childCount") + 1:][:1] == ["seasonCount"])
                xml = server.request("GET", "/library/metadata/%s" % row["ratingKey"],
                                     accept="*/*")[2].decode("utf-8")
                element = re.search(r"<Directory\b[^>]*", xml)
                attrs = re.findall(r' ([A-Za-z]+)="', element.group(0)) if element else []
                xml_placed = ("seasonCount" not in attrs if show["seasonCount"] is None else
                              "childCount" in attrs
                              and attrs[attrs.index("childCount") + 1:][:1] == ["seasonCount"])
                if (detail.get("childCount"), detail.get("seasonCount")) != (
                        show["childCount"], show["seasonCount"]) or not placed or not xml_placed:
                    wrong_counts.append((show["title"], detail.get("childCount"),
                                         detail.get("seasonCount"), placed, xml_placed))
            check("a season 0 is titled Specials, as the 1.42.2 reference titles it (%s)" % label,
                  not wrong_titles, str(wrong_titles))
            check("a show with a season 0 carries seasonCount, without the specials, straight "
                  "after childCount, as the 1.42.2 reference answers (%s)" % label,
                  not wrong_counts, str(wrong_counts))

            bluey = next(show for show in zen_specials if show.get("localised"))
            bluey_key = shows[bluey["title"]]["ratingKey"]
            specials_key = next((season.get("ratingKey") for season in
                                 server.json("/library/metadata/%s/children" % bluey_key).get("Metadata") or []
                                 if season.get("index") == 0), None)
            wrong = []
            for language, expected in sorted(bluey["localised"].items()):
                localised = {"X-Plex-Language": language}
                children = server.json("/library/metadata/%s/children" % bluey_key, headers=localised)
                episodes = server.json("/library/metadata/%s/children" % specials_key, headers=localised)
                got = {"seasons": [season.get("title") for season in children.get("Metadata") or []],
                       "specials_title2": episodes.get("title2"),
                       "specials_parentTitle": episodes.get("parentTitle"),
                       "episode_parentTitle": first(episodes).get("parentTitle")}
                if got != expected:
                    wrong.append((language, got, expected))
            check("Specials is localised as a season title and an episode's parentTitle, and "
                  "kept English as title2, in every language the 1.42.2 reference answers (%s)" % label,
                  not wrong, str(wrong[:3]))

            # Deleting the season 0 recounts the show: the 1.42.2 reference's rule leaves a show
            # without one no `seasonCount` at all, so it goes with the season rather than
            # staying stale.
            gundam_key = shows["Mobile Suit Gundam Wing"]["ratingKey"]
            season_zero = next((season.get("ratingKey") for season in
                                server.json("/library/metadata/%s/children" % gundam_key).get("Metadata") or []
                                if season.get("index") == 0), None)
            server.request("DELETE", "/library/metadata/%s" % season_zero)
            after = first(server.json("/library/metadata/%s" % gundam_key))
            check("deleting a show's season 0 takes its seasonCount away (%s)" % label,
                  after.get("childCount") == 1 and "seasonCount" not in after,
                  "childCount=%s seasonCount=%s" % (after.get("childCount"), after.get("seasonCount")))
        finally:
            server.stop()


def run_enrichment(check):
    """Cycle 44: the TMDB pipeline's own output, driven through the native enrichment with a
    stubbed TMDB answer. Two things it got wrong, both measured on the references:

     * a SEASON was given TMDB's season air-date year as its own `year`. 689 of the 1.42.2
       reference's 742 seasons carry `parentYear` and exactly ONE carries a `year` of its
       own, so a Plex series agent does not produce one -- and carrying it takes
       `parentYear` away, because the year rule reads "has a year of its own" off that
       column;
     * a SEASON and an EPISODE carried no `Guid` at all. The ids were fetched and discarded
       on a note claiming a real PMS leaves those arrays empty. plexlab's season 7 answers
       `tmdb://3846` and `tvdb://15791` and its episode 8 answers three ids; 58 of 60
       episodes of the 1.42.2 reference surveyed carry a block."""
    try:
        answer = native_json("metadata-enrich", {
            "row": {"title": "Stub Show S01E07", "type": "episode"},
            "details": {"id": 1396, "name": "Stub Show", "first_air_date": "2008-09-23",
                        "external_ids": {"imdb_id": "tt0903747"}, "genres": [],
                        "credits": {"cast": [], "crew": []}},
            "season": {"id": 3572, "air_date": "2009-03-08",
                       "external_ids": {"tvdb_id": 15791}, "overview": "Stub season."},
            "episode": {"id": 62085, "name": "Stub Episode", "air_date": "2009-03-08",
                        "external_ids": {"imdb_id": "tt1234567", "tvdb_id": 297989}},
            "parsed": {"title": "Stub Show", "year": None, "season": 1, "episode": 7},
            "kind": "tv", "tmdbId": 1396,
        })
    except AssertionError as error:
        answer = {"ok": False, "row": {}, "error": str(error)}
    check("the pipeline identifies a stub episode", answer.get("ok"), str(answer.get("error", "")))
    probe_row = answer.get("row") or {}
    if answer.get("ok"):
        check("an identified episode carries its own Guid",
              [g["id"] for g in probe_row.get("Guid") or []]
              == ["imdb://tt1234567", "tmdb://62085", "tvdb://297989"],
              str(probe_row.get("Guid")))
        check("the pipeline stores no season year of its own",
              "_season_year" not in probe_row, str(probe_row.get("_season_year")))
        check("the pipeline stores the season's external ids",
              [g["id"] for g in probe_row.get("_season_guids") or []]
              == ["tmdb://3572", "tvdb://15791"], str(probe_row.get("_season_guids")))


if __name__ == "__main__":
    sys.exit(main())
