#!/usr/bin/env python3
"""plexsim -- a Plex Media Server that owns no files.

It answers the PMS REST surface real Plex clients call, and serves the bytes of
its OWN catalog: every item's `Part` resolves to a debrid CDN URL at read time.
That is the half pd_inject never had -- pd_inject's library rows were download
buttons that pushed a torrent into Real-Debrid and then refreshed a real Plex
server, so nothing was ever streamed FROM it.

Two things decide whether a client plays an item, and plexsim has to satisfy
both:

  1. The client's own gate, run on the `Media` attributes before the server is
     asked anything (see mediainfo.py).  Hence honest numbers from a real probe.
  2. `/video/:/transcode/universal/decision`, which must answer "direct play"
     with a `Part.decision` the stricter clients read off the body.

The part handler has two modes because which one clients accept is exactly the
open question:

  proxy     (default) stream the bytes through plexsim, forwarding Range and
            rewriting Content-Type -- RD's CDN answers
            `application/force-download`, which is not a media type.
  redirect  302 straight to the CDN.  Saves all the bandwidth; official clients
            withhold the Plex token off-origin (plex-for-kodi's
            `isRequestToServer`), and some may not follow it at all.
"""

import argparse
import io
import json
import logging
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim as claim_module  # noqa: E402
import heartbeat as heartbeat_module  # noqa: E402
import library as library_module  # noqa: E402
import mediainfo as mediainfo_module  # noqa: E402
import metadata as metadata_module  # noqa: E402
from debrid import Resolver  # noqa: E402
from xmlout import to_xml  # noqa: E402

logger = logging.getLogger("plexsim")

# Version matters: plex-for-kodi derives `supportsFeature('streamingBrain')`
# purely from the advertised server version (>= 1.2.0) and skips server
# decisions entirely below it, so under-reporting changes client behaviour.
SERVER_VERSION = "1.42.2.10156-plexsim"

# Upstream reads are issued with a browser-ish UA; RD's CDN is fine with
# anything, but a bare urllib default is the kind of thing a CDN rate-limits.
UPSTREAM_UA = "plexsim/0.1"

# The account's Plex Pass entitlement list, as a real PMS on this same account
# reports it. Kept in a sidecar file rather than inline: it is ~200 opaque uuids
# and belongs to the account, not to plexsim's logic.
# Filter/sort descriptors for a movie section, taken from a real PMS and
# retargeted onto plexsim's section 1. The web client requests the section
# listing with `includeMeta=1` and uses this to build the grid; without it the
# client fetched /library/sections/1/all four times and rendered no items.
# Per-library-type preference sets, captured from a real PMS. `flattenSeasons`
# lives here, and a show library that reports NO settings gives the client
# nothing to read for it -- plexsim returned an empty Setting list.
# Server-wide preferences. A real PMS answers /:/prefs with ~175 settings;
# plexsim answered with an empty list, the same gap as the section prefs.
_SERVER_PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_prefs.json")
try:
    with open(_SERVER_PREFS_PATH) as _handle:
        SERVER_PREFS = json.load(_handle)
except (OSError, ValueError):
    SERVER_PREFS = []

_SECTION_PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "section_prefs.json")
try:
    with open(_SECTION_PREFS_PATH) as _handle:
        SECTION_PREFS = json.load(_handle)
except (OSError, ValueError):
    SECTION_PREFS = {}

# Filter/sort/field descriptors per library TYPE, captured from a real PMS. A
# movie section's Meta has 2 Types; a SHOW section's has four (show, season,
# episode, folder) with their own filters and sorts, so retargeting the movie
# template onto a show library gave the client a show grid whose filter bar
# could only talk about movies.
SECTION_META = {}
for _kind, _name in (("movie", "section_meta.json"), ("show", "section_meta_show.json")):
    _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _name)
    try:
        with open(_path) as _handle:
            SECTION_META[_kind] = json.load(_handle)
    except (OSError, ValueError):
        pass

_OWNER_FEATURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner_features.txt")
try:
    with open(_OWNER_FEATURES_PATH) as _handle:
        OWNER_FEATURES = _handle.read().strip()
except OSError:
    OWNER_FEATURES = "pass,sync,hwtranscode"

CONTENT_TYPES = {
    "mkv": "video/x-matroska",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mpegts": "video/mp2t",
}

# The secondary filter axes a section offers, and where each one reads from. A
# TAG axis keys its entries by tag id and a FIELD axis by the value itself --
# measured against a real PMS: `?genre=24` but `?contentRating=PG`.
SECONDARY_TAGS = {
    "genre": "Genre",
    "collection": "Collection",
    "director": "Director",
    "writer": "Writer",
    "producer": "Producer",
    "actor": "Role",
    "country": "Country",
    "label": "Label",
}
SECONDARY_FIELDS = {
    "year": "year",
    "decade": "year",
    "contentRating": "contentRating",
    "studio": "studio",
}
SECONDARY_TITLES = {
    "genre": "By Genre",
    "year": "By Year",
    "decade": "By Decade",
    "contentRating": "By Content Rating",
    "collection": "By Collection",
    "director": "By Director",
    "writer": "By Writer",
    "producer": "By Producer",
    "actor": "By Actor",
    "country": "By Country",
    "studio": "By Studio",
    "label": "By Label",
}

# `/library/sections/{id}/sorts`, copied from a real PMS movie section. The
# element name is `Directory`, not `Sort` -- plexsim answered with `Sort` and a
# two-entry list, which is not what the legacy sort bar reads.
SECTION_SORTS = [
    {"default": "asc", "defaultDirection": "asc", "descKey": "titleSort:desc",
     "key": "titleSort", "title": "Title"},
    {"defaultDirection": "desc", "descKey": "originallyAvailableAt:desc",
     "key": "originallyAvailableAt", "title": "Release Date"},
    {"defaultDirection": "desc", "descKey": "rating:desc", "key": "rating",
     "title": "Critic Rating"},
    {"defaultDirection": "desc", "descKey": "audienceRating:desc",
     "key": "audienceRating", "title": "Audience Rating"},
    {"defaultDirection": "desc", "descKey": "duration:desc", "key": "duration",
     "title": "Duration"},
    {"defaultDirection": "desc", "descKey": "addedAt:desc", "key": "addedAt",
     "title": "Date Added"},
    {"defaultDirection": "desc", "descKey": "lastViewedAt:desc",
     "key": "lastViewedAt", "title": "Date Viewed"},
    {"defaultDirection": "asc", "descKey": "mediaHeight:desc", "key": "mediaHeight",
     "title": "Resolution"},
    {"defaultDirection": "desc", "descKey": "random:desc", "key": "random",
     "title": "Randomly"},
]

# `/library/sections/{id}/filters`. Only the axes plexsim can actually answer
# from its rows: a real PMS lists 28, including tuner and photo ones.
SECTION_FILTERS = [
    ("genre", "Genre", "string"),
    ("year", "Year", "integer"),
    ("decade", "Decade", "integer"),
    ("contentRating", "Content Rating", "string"),
    ("collection", "Collection", "string"),
    ("director", "Director", "string"),
    ("actor", "Actor", "string"),
    ("writer", "Writer", "string"),
    ("producer", "Producer", "string"),
    ("country", "Country", "string"),
    ("studio", "Studio", "string"),
    ("label", "Labels", "string"),
    ("unwatched", "Unwatched", "boolean"),
    ("inProgress", "In Progress", "boolean"),
    ("unmatched", "Unmatched", "boolean"),
]

# `/playlists/sorts`, from a real PMS.
PLAYLIST_SORTS = [
    {"default": "asc", "defaultDirection": "asc", "descKey": "titleSort:desc",
     "key": "titleSort", "title": "Name"},
    {"defaultDirection": "desc", "descKey": "viewCount:desc", "key": "viewCount",
     "title": "Plays"},
    {"defaultDirection": "desc", "descKey": "lastViewedAt:desc",
     "key": "lastViewedAt", "title": "Last Played"},
    {"defaultDirection": "desc", "descKey": "duration:desc", "key": "duration",
     "title": "Duration"},
    {"defaultDirection": "desc", "descKey": "addedAt:desc", "key": "addedAt",
     "title": "Date Added"},
    {"defaultDirection": "desc", "descKey": "updatedAt:desc", "key": "updatedAt",
     "title": "Date Modified"},
]

# `/system/agents` -- only the agents plexsim identifies with. mediaType is
# stripped from the response and used to answer the client's filtered request,
# the way a real PMS's MediaType block does.
SYSTEM_AGENTS = [
    {"identifier": "tv.plex.agents.movie", "name": "Plex Movie", "hasPrefs": False,
     "id": 1, "primary": True, "mediaType": 1,
     "Language": [{"code": "en-US", "title": "English"}]},
    {"identifier": "tv.plex.agents.series", "name": "Plex Series", "hasPrefs": False,
     "id": 2, "primary": True, "mediaType": 2,
     "Language": [{"code": "en-US", "title": "English"}]},
    {"identifier": "tv.plex.agents.none", "name": "Plex Personal Media",
     "hasPrefs": False, "id": 4, "primary": True, "mediaType": 1,
     "Language": [{"code": "en-US", "title": "English"}]},
]


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


class Catalog:
    """The library plexsim serves, loaded from a JSON file.

    A row is one playable item: its Plex metadata plus the restricted debrid
    link its single Part resolves to.  `ratingKey` is assigned from the row
    order so the key space is stable across restarts for a fixed catalog.
    """

    def __init__(self, path):
        with open(path) as handle:
            document = json.load(handle)

        self.machine = document["machineIdentifier"]
        self.friendly = document.get("friendlyName", "plexsim")
        self.sections = document["sections"]
        self.items = {}
        self._by_section = {}
        self._children = {}

        # A section listing shows only TOP-LEVEL rows: movies in a movie
        # section, shows in a show section. Listing seasons or episodes at the
        # top of a library is not what a real PMS does and the grid renders
        # them wrongly.
        top_level = {"movie", "show"}
        for row in document["items"]:
            rating_key = int(row["ratingKey"])
            self.items[rating_key] = row
            if row.get("type") in top_level:
                self._by_section.setdefault(str(row["librarySectionID"]), []).append(rating_key)
            parent = row.get("parentRatingKey")
            if parent:
                self._children.setdefault(str(parent), []).append(rating_key)

        self._number_tags()
        self._retitle_streams()

    # Tag ids are how the whole filter surface is addressed: a real PMS's
    # secondary axes key their entries by tag id (`/all?genre=24`), not by the
    # tag's text. TMDB gives plexsim tag names and nothing else, so the ids are
    # minted here -- deterministically from the text, so they survive a restart
    # and a catalog rebuild, and so the `filter` string on each tag agrees.
    TAG_FIELDS = ("Genre", "Collection", "Director", "Writer", "Producer", "Role",
                  "Country", "Label", "Similar", "Mood", "Style")
    TAG_FILTERS = {"Genre": "genre", "Collection": "collection", "Director": "director",
                   "Writer": "writer", "Producer": "producer", "Role": "actor",
                   "Country": "country", "Label": "label", "Similar": "similar",
                   "Mood": "mood", "Style": "style"}

    def _number_tags(self):
        for row in self.items.values():
            self.number_tags(row)

    def _retitle_streams(self):
        """Recompute every Stream's display titles from the row's own fields.

        A catalog built before mediainfo.py's split was corrected carries the
        codec in `displayTitle` and no `extendedDisplayTitle` at all, and those
        two strings are exactly what the client's quality and track pickers
        print. Derived from what the row already holds, so it is a correction
        rather than an invention.
        """
        for row in self.items.values():
            for media in row.get("Media") or []:
                for part in media.get("Part") or []:
                    for stream in part.get("Stream") or []:
                        mediainfo_module.stream_titles(stream)

    def number_tags(self, row):
        """Give every tag on one row an id and a filter string.

        Called again after anything that can WRITE tags -- a match re-identifies
        a row straight from TMDB, which supplies names and no ids, and a row
        whose tags lost their ids drops out of every secondary filter while
        still looking correct on its own detail page.
        """
        for field in self.TAG_FIELDS:
            for tag in row.get(field) or []:
                if not isinstance(tag, dict) or not tag.get("tag"):
                    continue
                if not tag.get("id"):
                    seed = "%s:%s" % (field, tag["tag"])
                    # A small positive int, the way a real PMS's ids look.
                    tag["id"] = zlib.crc32(seed.encode()) % 900000 + 1000
                name = self.TAG_FILTERS.get(field)
                if name and not tag.get("filter"):
                    tag["filter"] = "%s=%s" % (name, tag["id"])

    def section(self, section_id):
        for section in self.sections:
            if str(section["key"]) == str(section_id):
                return section
        return None

    def section_items(self, section_id):
        return [self.items[k] for k in self._by_section.get(str(section_id), [])]

    def children(self, rating_key):
        """Direct children in index order: seasons of a show, episodes of a season."""
        keys = self._children.get(str(rating_key), [])
        rows = [self.items[k] for k in keys]
        return sorted(rows, key=lambda r: (r.get("index") or 0))

    def leaves(self, rating_key):
        """Every episode beneath a show, across its seasons."""
        out = []
        for child in self.children(rating_key):
            if child.get("type") == "episode":
                out.append(child)
            else:
                out.extend(self.leaves(child["ratingKey"]))
        return out

    # -- mutation --------------------------------------------------------
    #
    # The catalog is loaded from JSON, but the Plex UI writes: Delete, Split
    # Apart, Merge and "Add Library" all change which rows exist. Nothing here
    # touches catalog.json -- a restart is the undo button.

    # Key ranges stay disjoint by kind so a ratingKey identifies its own row
    # type on sight, the way the built catalog's already do.
    KEY_BASES = {"movie": 1000, "episode": 1000, "show": 20000, "season": 30000}

    def next_key(self, row_type):
        base = self.KEY_BASES.get(row_type, 1000)
        used = [k for k in self.items if base <= k < base + 10000]
        return (max(used) + 1) if used else base + 1

    def add(self, row):
        """Register a new row, keeping the section and child indexes in step."""
        rating_key = int(row["ratingKey"])
        self.items[rating_key] = row
        if row.get("type") in ("movie", "show"):
            self._by_section.setdefault(str(row["librarySectionID"]), []).append(rating_key)
        parent = row.get("parentRatingKey")
        if parent:
            self._children.setdefault(str(parent), []).append(rating_key)
        return row

    def remove(self, rating_key):
        """Remove a row and everything beneath it. Returns the keys removed."""
        rating_key = int(rating_key)
        row = self.items.get(rating_key)
        if row is None:
            return set()
        removed = {rating_key}
        for child in list(self.children(rating_key)):
            removed |= self.remove(child["ratingKey"])
        self.items.pop(rating_key, None)
        for keys in self._by_section.values():
            if rating_key in keys:
                keys.remove(rating_key)
        self._children.pop(str(rating_key), None)
        parent = row.get("parentRatingKey")
        if parent and rating_key in self._children.get(str(parent), []):
            self._children[str(parent)].remove(rating_key)
        return removed

    def add_section(self, title, section_type, uuid_value):
        """"Add Library" in the UI. plexsim has no filesystem to scan, so the
        section starts empty -- which is what a real PMS gives you for an empty
        folder too, rather than an error."""
        keys = [int(s["key"]) for s in self.sections if str(s["key"]).isdigit()]
        key = str((max(keys) + 1) if keys else 1)
        section = {"key": key, "type": section_type, "title": title, "uuid": uuid_value}
        self.sections.append(section)
        self._by_section.setdefault(key, [])
        return section

    def remove_section(self, section_id):
        """Delete Library: the section and every row in it."""
        section = self.section(section_id)
        if section is None:
            return None
        for row in list(self.section_items(section_id)):
            self.remove(row["ratingKey"])
        self.sections = [s for s in self.sections if str(s["key"]) != str(section_id)]
        self._by_section.pop(str(section_id), None)
        return section

    def search(self, query):
        needle = query.lower().strip()
        if not needle:
            return []
        return [
            row
            for row in self.items.values()
            if row.get("type") in ("movie", "show", "episode")
            and (needle in str(row.get("title", "")).lower()
                 or needle in str(row.get("grandparentTitle", "")).lower()
                 or needle in str(row.get("originalTitle", "")).lower())
        ]


# --------------------------------------------------------------------------
# metadata projection
# --------------------------------------------------------------------------


def _public_row(row):
    """A catalog row as the API may expose it.

    Every key beginning with `_` is internal -- the debrid link and the resolved
    TMDB artwork URLs -- and must never reach a client. Stripping only `_debrid`
    leaked `_poster_url` and `_art_url` into metadata responses.

    The `Image` array is added here because BOTH the section listing and the
    item detail need it: a real PMS sends it on both, and the client reads it to
    choose artwork.
    """
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    title = out.get("title") or ""
    images = []
    if out.get("thumb"):
        images.append({"alt": title, "type": "coverPoster", "url": out["thumb"]})
    if out.get("art"):
        images.append({"alt": title, "type": "background", "url": out["art"]})
    if images:
        out["Image"] = images
    return out


def _listing_row(row):
    """A row as a section listing returns it.

    A real PMS does NOT strip `Media` here -- verified against a live movie
    section -- so this is the same projection as the detail row.
    """
    return _public_row(row)


def _full(row):
    return _public_row(row)


# --------------------------------------------------------------------------
# poster placeholder
# --------------------------------------------------------------------------


def _png(width, height, rgb):
    """A solid-colour PNG, built without a third-party imaging library.

    Clients render an empty poster tile when art 404s; a real image keeps the
    grid honest about which rows exist while plexsim has no artwork source.
    """
    width = max(1, min(int(width), 2000))
    height = max(1, min(int(height), 2000))
    raw = bytearray()
    row = bytes(rgb) * width
    for _ in range(height):
        raw.append(0)  # filter type 0 for each scanline
        raw.extend(row)

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------


class PlexSim:
    def __init__(self, catalog, resolver, *, part_mode="proxy", public_url=None,
                 owner_username="plexsim", claim=None, web_root=None,
                 assert_claimed=False, owner_email=None, tmdb=None):
        self.catalog = catalog
        self.resolver = resolver
        self.part_mode = part_mode
        self.public_url = public_url
        # A server is claimed BY ONE OWNER account; every other user reaches it
        # through that owner's sharing, not through a second claim. This is the
        # owner plexsim reports, and it must match the account whose device list
        # carries the registration.
        self.owner_username = owner_username
        # Holds the server's own plex.tv token once a client claims it.
        self.claim = claim
        # Directory holding the extracted Plex Web client, served at /web.
        self.web_root = web_root
        self.assert_claimed = assert_claimed
        # Verified against fun and zen: both report the owner's email here.
        self.owner_email = owner_email or owner_username
        # Reported on /myplex/account the way fun and zen do.
        self.public_address = ""
        self.private_address = ""
        self.unknown = []
        self.requests = []
        self.lock = threading.Lock()
        # Playback bookkeeping a client expects to survive between calls.
        self.play_queues = {}
        self.play_queue_sources = {}
        self.play_queue_shuffled = {}
        self.next_play_queue = 1
        self.sessions = {}
        # Everything the UI can change: watch state, edits, playlists,
        # collections, play history. In memory only -- catalog.json is input.
        self.library = library_module.Library(catalog, notify=self.notify_activity_event)
        # Preferences the client has written back through PUT /:/prefs. A real
        # PMS persists these; plexsim keeps them for the life of the process so
        # a settings page round-trips instead of silently reverting.
        self.pref_overrides = {}
        # Per-section settings, keyed (section id, setting id): `flattenSeasons`
        # and the rest of the library-type set live in their own id space.
        self.section_pref_overrides = {}
        # Section ids with a scan in flight, which /library/sections reports as
        # `refreshing` and the Libraries page reads to swap its Scan button.
        self.refreshing = set()
        # Recent log lines, for /diagnostics/logs -- the Troubleshooting page's
        # Download Logs button, which is only drawn when the server advertises
        # `diagnostics` containing "logs".
        self.log_lines = []
        # TMDB, when a key is configured: Fix Match searches it live. Without
        # one the match dialog honestly reports no candidates rather than
        # offering a list plexsim cannot act on.
        self.tmdb = tmdb
        # guid -> (kind, tmdb id) for candidates handed out by /matches, so the
        # match the user picks resolves back to the record it came from.
        self.match_candidates = {}
        # Open notification sockets. A real PMS pushes library and task events
        # down this channel and clients redraw from them instead of polling; the
        # four types it actually sends were measured on the reference (see
        # docs/findings.md), and plexsim pushed none of them.
        self.sockets = []

    # -- notifications ---------------------------------------------------

    def add_socket(self, sink):
        with self.lock:
            self.sockets.append(sink)

    def drop_socket(self, sink):
        with self.lock:
            if sink in self.sockets:
                self.sockets.remove(sink)

    def notify(self, kind, element, rows):
        """Fan one event out to every open channel, in that channel's own format.

        The two transports are NOT the same wire format, which is easy to miss
        because they carry the same events. Measured on the reference:

          websocket  {"NotificationContainer": {"type": k, "size": n, "El": [rows]}}
          eventsource  `event: k` then `data: {"El": {one row}}`

        -- no container, no size, and one object rather than a list. A client
        that receives nothing here falls back to polling and never sees a change
        made from another device.
        """
        with self.lock:
            sinks = list(self.sockets)
        for sink in sinks:
            try:
                sink(kind, element, rows)
            except Exception:
                self.drop_socket(sink)

    def notify_activity_event(self, event, activity):
        """Called by the library when an activity starts or ends."""
        self.notify_activity(activity, event=event)

    def notify_activity(self, activity, event="started"):
        self.notify("activity", "ActivityNotification",
                    [{"event": event, "uuid": activity["uuid"], "Activity": activity}])

    def notify_status(self, title, name="LIBRARY_UPDATE"):
        self.notify("status", "StatusNotification",
                    [{"title": title, "description": "", "notificationName": name}])

    def notify_progress(self, message):
        self.notify("progress", "ProgressNotification", [{"message": message}])

    # Plex's numeric type codes, as a TimelineEntry carries them.
    TIMELINE_TYPES = {"movie": 1, "show": 2, "season": 3, "episode": 4}

    def notify_timeline(self, row, state=3, metadata_state="queued"):
        """A TimelineEntry for one row, with the field set measured off a real
        refresh: identifier, sectionID and itemID as STRINGS, the numeric type,
        the title, `state` and `metadataState`."""
        self.notify("timeline", "TimelineEntry", [{
            "identifier": "com.plexapp.plugins.library",
            "sectionID": str(row.get("librarySectionID", "")),
            "itemID": str(row.get("ratingKey", "")),
            "type": self.TIMELINE_TYPES.get(row.get("type"), 1),
            "title": row.get("title") or "",
            "state": state,
            "metadataState": metadata_state,
            "updatedAt": int(time.time()),
        }])

    # -- helpers ---------------------------------------------------------

    # Child element names a container can carry. A real PMS emits NO element
    # when a list is empty -- its responses are XML underneath, where an empty
    # list simply has no children -- so `Metadata: []` is a shape no real server
    # returns, and a client can read it as "there IS state here and it is empty"
    # rather than "there is none".
    CHILD_KEYS = ("Metadata", "Directory", "Hub", "Video", "Setting", "Agent",
                  "Account", "Device", "Activity", "Playlist", "SearchResult",
                  "MetadataItem", "StatisticsBandwidth", "StatisticsMedia",
                  "StatisticsResources", "Filter", "Sort", "Location")

    @classmethod
    def _prune(cls, body):
        for key in cls.CHILD_KEYS:
            if key in body and isinstance(body[key], list) and not body[key]:
                del body[key]
        return body

    def bare_container(self, **fields):
        """A MediaContainer with no library-container defaults.

        /media/providers on a real PMS has no `identifier`, `mediaTagPrefix`,
        `mediaTagVersion` or `allowSync`, and no `claimed` either -- claim state
        lives on /identity and /myplex/account. Sending extras is not harmless:
        it is how plexsim ended up asserting `claimed` in a place a real server
        never does.
        """
        return {"MediaContainer": self._prune(dict(fields))}

    def container(self, **fields):
        body = {
            "size": 0,
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": 1655122614,
            "allowSync": False,
        }
        body.update(fields)
        for key in ("Metadata", "Directory", "Hub", "Video"):
            if key in fields and isinstance(fields[key], list):
                body["size"] = len(fields[key])
                break
        return {"MediaContainer": self._prune(body)}

    def _owner(self):
        """What a real PMS reports as myPlexUsername: the owner's EMAIL."""
        if self.claim is not None and self.claim.claimed:
            return self.claim.email or self.claim.owner or self.owner_email
        return self.owner_email

    def show_detail(self, row, detail, query):
        """Augment a show row for its detail page.

        Diffed against a real show: the client asks with `includeOnDeck=1` and
        `includeExtras=1`, and a real PMS answers with an `OnDeck` episode and
        an `Extras` element. Without `OnDeck` the show page has no episode to
        offer, which is what left the episode area empty. `index`, `duration`,
        `originallyAvailableAt`, `Guid` and a `Preferences` block are also on a
        real show row and absent here.
        """
        leaves = self.catalog.leaves(row["ratingKey"])

        detail.setdefault("index", 1)
        if leaves:
            # A show's duration is its typical episode length.
            durations = [l.get("duration") for l in leaves if l.get("duration")]
            if durations:
                detail.setdefault("duration", int(sum(durations) / len(durations)))
            earliest = sorted(l.get("originallyAvailableAt") or "" for l in leaves)
            earliest = [d for d in earliest if d]
            if earliest:
                detail.setdefault("originallyAvailableAt", earliest[0])

        if row.get("guid") and "Guid" not in detail:
            detail["Guid"] = [{"id": row["guid"]}]

        if query.get("includeExtras") == "1":
            detail["Extras"] = {"size": 0}

        if query.get("includeOnDeck") == "1":
            # First episode with no view offset, else the first episode.
            unwatched = [l for l in leaves if not l.get("viewOffset") and not l.get("viewCount")]
            pick = (unwatched or leaves or [None])[0]
            if pick is not None:
                detail["OnDeck"] = {"Metadata": _full(pick)}

        if query.get("includePreferences") == "1":
            detail["Preferences"] = {
                "Setting": [
                    {
                        "id": "episodeSort",
                        "label": "Episode sorting",
                        "summary": "How to sort the episodes for this show.",
                        "type": "text",
                        "default": "-1",
                        "value": "-1",
                        "hidden": False,
                        "advanced": False,
                        "group": "",
                        "enumValues": "-1:Library default|0:Oldest first|1:Newest first",
                    }
                ]
            }
        return detail

    def child_container(self, parent, rows, leaf, query=None):
        """`/children` and `/allLeaves`, matched against a real PMS.

        Paging is not cosmetic here. Plex Web asks for a show's seasons with
        `X-Plex-Container-Start=0&X-Plex-Container-Size=20`, and a real PMS
        answers with `offset` and `totalSize` alongside `size` (measured on
        fun: size=20, totalSize=37, offset=0). The web client turns a missing
        `totalSize` into `undefined` and its pre-play descendant list renders
        `null` unless that value is truthy -- which is why the Seasons block
        never appeared, no matter how correct the rows were.

        The parent's own identity is repeated on the CONTAINER, not just on the
        rows -- `parentTitle`, `parentIndex`, `parentYear`, `thumb`, `art`,
        `theme`, `viewMode` and, for an episode list, the whole
        `grandparent*` set. The desktop client rendered an empty season list
        while these were missing, even though the rows themselves were correct.
        """
        section = self.catalog.section(parent.get("librarySectionID")) or {}
        is_episode_list = bool(rows) and rows[0].get("type") == "episode"

        fields = {
            "Metadata": [_full(r) for r in rows],
            "key": str(parent.get("ratingKey")),
            "nocache": True,
            "viewGroup": "episode" if is_episode_list else "season",
            # Measured on fun: a SEASON list is 65593 and an EPISODE list is
            # 65592. These were inverted here, so the client was handed the
            # episode layout for a season list and rendered no seasons.
            "viewMode": 65592 if is_episode_list else 65593,
            "librarySectionID": parent.get("librarySectionID"),
            "librarySectionTitle": parent.get("librarySectionTitle"),
            "librarySectionUUID": section.get("uuid"),
            "title1": parent.get("librarySectionTitle"),
            "summary": parent.get("summary") or "",
        }

        if parent.get("type") == "season":
            # An episode list is presented as the SHOW's, with the season as
            # the parent -- hence grandparent* on the container.
            show_key = parent.get("parentRatingKey")
            show = self.catalog.items.get(int(show_key)) if show_key else None
            fields.update(
                {
                    "title2": parent.get("title"),
                    "parentIndex": parent.get("index"),
                    "parentTitle": parent.get("parentTitle"),
                    "parentYear": parent.get("year"),
                    "thumb": parent.get("thumb"),
                    "art": parent.get("art"),
                    "theme": "/library/metadata/%s/theme/1" % show_key if show_key else None,
                }
            )
            if show:
                fields.update(
                    {
                        "grandparentRatingKey": show.get("ratingKey"),
                        "grandparentTitle": show.get("title"),
                        "grandparentThumb": show.get("thumb"),
                        "grandparentStudio": show.get("studio"),
                        "grandparentContentRating": show.get("contentRating"),
                        "grandparentTheme": "/library/metadata/%s/theme/1" % show.get("ratingKey"),
                        "grandparentYear": show.get("year"),
                    }
                )
        else:
            fields.update(
                {
                    "title2": parent.get("title"),
                    "parentIndex": parent.get("index") or 1,
                    "parentTitle": parent.get("title"),
                    "parentYear": parent.get("year"),
                    "thumb": parent.get("thumb"),
                    "art": parent.get("art"),
                    "theme": "/library/metadata/%s/theme/1" % parent.get("ratingKey"),
                }
            )
        if leaf == "allLeaves":
            # Measured against the reference: the allLeaves container says
            # `mixedParents` (its rows come from more than one season) and
            # carries neither `thumb` nor `summary`, both of which /children
            # does. Same endpoint family, different field set.
            fields["mixedParents"] = True
            fields.pop("thumb", None)
            fields.pop("summary", None)

        # A real PMS emits offset/totalSize only when the client pages.
        query = query or {}
        if "X-Plex-Container-Start" in query or "X-Plex-Container-Size" in query:
            total = len(rows)
            start = int(query.get("X-Plex-Container-Start", 0) or 0)
            size = int(query.get("X-Plex-Container-Size", total) or total)
            page = rows[start : start + size]
            fields["Metadata"] = [_full(r) for r in page]
            fields["offset"] = start
            fields["totalSize"] = total

        return self.container(**{k: v for k, v in fields.items() if v is not None})

    def section_directory(self, section, shared=False):
        """One section as a Directory row.

        The attribute set is the reference's: `filters`, the agent/scanner pair,
        `metadataAgentProviderGroupId` and the four timestamps were all absent,
        and `filters` is what tells a client the section HAS a filter bar. The
        timestamps come from the catalog's own rows rather than being invented --
        the newest `addedAt` in the section is when it last changed.
        """
        key = section["key"]
        is_show = section.get("type") == "show"
        rows = self.catalog.section_items(key)
        newest = max([r.get("addedAt") or 0 for r in rows] or [0])
        row = {
            "key": ("/library/sections/%s/all?type=%d" % (key, 2 if is_show else 1))
                   if shared else key,
            "type": section.get("type"),
            "title": section.get("title"),
            "agent": "tv.plex.agents.series" if is_show else "tv.plex.agents.movie",
            "scanner": "Plex TV Series" if is_show else "Plex Movie",
            "language": "en-US",
            "metadataAgentProviderGroupId": 2 if is_show else 1,
            "uuid": section.get("uuid"),
            "updatedAt": newest,
            "createdAt": newest,
            "scannedAt": newest,
            "contentChangedAt": len(rows),
            "filters": True,
            "refreshing": str(key) in self.refreshing,
            "allowSync": False,
            "content": True,
            "directory": True,
            "hidden": 0,
        }
        row["Location"] = [{"id": 1, "path": "/plexsim/%s" % key}]
        if shared:
            # Measured: the shared view carries empty art/thumb and no composite.
            row["art"] = ""
            row["thumb"] = ""
        else:
            row["composite"] = "/library/sections/%s/composite/1" % key
        return row

    def section_meta(self, section_id, section_type):
        """Per-section filter/sort descriptors, retargeted from the template.

        Every key in the captured blob names the section it came from, in Type
        keys, Sort firstCharacterKeys and Filter keys alike, so the retarget is
        a rewrite of the whole document rather than of the three fields that
        happen to be at the top.
        """
        template = SECTION_META.get("show" if section_type == "show" else "movie")
        if not template:
            return None
        blob = json.dumps(template)
        blob = re.sub(r"/library/sections/\d+/", "/library/sections/%s/" % section_id, blob)
        return json.loads(blob)

    def _claimed(self):
        # `assert_claimed` reports a claim plexsim does not hold. It is honest
        # about what it is: a simulator telling the client not to nag. The
        # alternative is POST /api/claim/exchange, and doing that repeatedly
        # with fabricated server identities is what triggered an account-wide
        # plex.tv token reset on 2026-09-11 and signed two real servers out.
        # The locally served web client reads claim state from this server's own
        # /identity and /myplex/account, so asserting it here is sufficient --
        # app.plex.tv would not be fooled, because it reads plex.tv instead.
        if self.assert_claimed:
            return True
        return self.claim is not None and self.claim.claimed

    def part_url(self, part_key):
        """Absolute URL for a Part, used where a container must carry one."""
        if self.public_url:
            return self.public_url.rstrip("/") + part_key
        return part_key

    # -- routing ---------------------------------------------------------

    def handle(self, method, path, body, headers):
        parsed = urllib.parse.urlparse(path)
        route = parsed.path
        query = {
            k: v[-1]
            for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()
        }
        segments = [s for s in route.split("/") if s]

        # A Part read is not a container response; it returns a streaming
        # sentinel the handler turns into a body. Only for the READ verbs: the
        # same prefix with PUT is track selection, which is a normal endpoint.
        if route.startswith("/library/parts/") and method in ("GET", "HEAD"):
            return ("stream", segments, query)

        container = self._route(method, route, segments, query, body, headers)
        return container

    def _route(self, method, route, segments, query, body, headers):
        catalog = self.catalog

        # -- identity / capability ---------------------------------------
        if route in ("/", "/identity"):
            # Matched field-for-field against a real claimed PMS 1.43.4, which
            # answers /identity with exactly FIVE fields and nothing else. The
            # earlier version of this handler returned the whole server-info
            # blob (certificate, myPlex*, transcoder flags, ownerFeatures) and
            # the web app still called the server unclaimed -- extra fields do
            # not help and `apiVersion` was missing, which a real PMS always
            # sends. Claim state lives here and in /myplex/account, nowhere else.
            return {
                "MediaContainer": {
                    "size": 0,
                    "apiVersion": "1.1.1",
                    "claimed": self._claimed(),
                    "machineIdentifier": catalog.machine,
                    "version": SERVER_VERSION,
                }
            }

        if route == "/media/providers":
            return self.media_providers()

        # The claim flow: a client hands the server a short-lived claim token
        # and the SERVER redeems it. This is what the "Claim this server"
        # button calls.
        if route == "/myplex/claim":
            return self.do_claim(query)
        if route == "/myplex/account":
            return self.myplex_account()
        if route == "/myplex/refreshReachability":
            # The Remote Access page's "Retry" button. A real PMS 404s the GET
            # (measured) and accepts the PUT, so answering the GET would be
            # inventing a capability.
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            return ("empty", 200)

        # -- server preferences ------------------------------------------
        # Every Settings page except Agents and Libraries is this one endpoint:
        # the client renders the Setting list by `group`, and writes a change
        # back as PUT /:/prefs?<id>=<value>. Answering the write with a
        # MediaContainer is wrong -- a real PMS returns an empty 200 -- and
        # dropping it means the page silently reverts on its next read.
        if route in ("/:/prefs", "/:/prefs/"):
            if method == "PUT":
                return self.set_prefs(query)
            settings = self.server_prefs()
            return self.bare_container(size=len(settings), Setting=settings)

        # -- system ------------------------------------------------------
        if route.startswith("/system/agents"):
            return self.system_agents(query)
        if segments[:2] == ["system", "scanners"]:
            return self.system_scanners(segments[2] if len(segments) > 2 else None)
        if route.startswith("/system/appstore"):
            # Measured: a real PMS 404s the appstore paths -- the channel store
            # is gone, and the client's Plugins page copes with that.
            return ("error", 404, "plexsim: not found")
        if route.startswith("/:/plugins"):
            # The Plugins page. A real PMS lists its built-in agents here;
            # plexsim has no plug-in host, so the honest answer is none.
            return self.bare_container(size=0, Plugin=[])
        if route == "/system/services/url/lookup":
            return self.bare_container(size=0)

        if segments[:1] == ["accounts"]:
            return self.accounts(segments[1] if len(segments) > 1 else None)
        if segments[:1] == ["devices"] or route == "/devices.xml":
            if method == "DELETE":
                return ("empty", 200)
            return self.devices()
        if route == "/clients" or route.startswith("/clients/profiles"):
            # No Plex client is ever *hosted* by plexsim, so this is empty for
            # the same reason a headless PMS's is.
            return self.bare_container(size=0)

        if segments[:1] == ["statistics"]:
            return self.statistics(segments[1] if len(segments) > 1 else "")

        if route == "/updater/status":
            return self.updater_status()
        if route in ("/updater/check", "/updater/apply"):
            if route == "/updater/apply":
                logger.warning("UPDATER apply requested -- plexsim does not update itself")
            return ("empty", 200)

        if segments[:1] == ["activities"]:
            if method == "DELETE" and len(segments) > 1:
                self.library.cancel_activity(segments[1])
                return ("empty", 200)
            activities = self.library.activities()
            return self.bare_container(size=len(activities), Activity=activities)

        if route == "/status/sessions/history/all":
            return self.history(query)
        if route == "/status/sessions/terminate":
            return ("empty", 200)
        if route.startswith("/status/sessions"):
            # A real PMS omits Metadata entirely when nothing is playing rather
            # than sending an empty list.
            return self.bare_container(size=0)
        if segments[:2] == ["transcode", "sessions"]:
            if method == "DELETE":
                return ("empty", 200)
            return self.bare_container(size=0)

        if route == "/security/token":
            # The client asks for a delegation token before handing a URL to
            # another device. plexsim has no second party to delegate to, but
            # the shape is what the caller reads.
            return self.bare_container(size=0, token="transient-" + catalog.machine[:8])
        if route == "/security/resources":
            return ("error", 400, "plexsim: bad request")

        if route == "/logs":
            # Not the zip -- measured: a container with a `content` string. The
            # Console page reads this; /diagnostics/logs is the download.
            return self.bare_container(size=0, content="plugins")
        if route == "/log":
            # The web client posts its OWN errors here. Answering with a
            # container is wrong (a real PMS answers empty), and dropping them
            # means the client's logging silently goes nowhere -- so they land in
            # plexsim's log, where Download Logs will hand them back.
            level = query.get("level") or "?"
            logger.info("CLIENT LOG [%s] %s: %s", level, query.get("source") or "client",
                        (query.get("message") or "")[:400])
            return ("empty", 200)
        if route == "/system":
            # Measured: the system provider's two directories, nothing else.
            return self.bare_container(
                size=2, noHistory=0, replaceParent=0, identifier="com.plexapp.system",
                Directory=[{"key": "library", "title": "Library Sections",
                            "name": "Library Sections"},
                           {"key": "plugins", "title": "Plug-ins", "name": "Plug-ins"}])
        if route == "/system/proxy":
            # A real PMS refuses this without the headers its own clients send.
            return ("error", 400, "plexsim: bad request")
        if route == "/servers":
            return self.bare_container(size=1, Server=[{
                "name": catalog.friendly,
                "host": self.private_address or "127.0.0.1",
                "address": self.private_address or "127.0.0.1",
                "port": 32400,
                "machineIdentifier": catalog.machine,
                "version": SERVER_VERSION,
            }])
        if segments[:1] == ["butler"]:
            return self.butler(method, segments)
        if segments[:1] == ["player"]:
            # Player commands go to a PLAYER, not to a server: a real PMS answers
            # 400 for every one of them. plexsim answered 200 with a container,
            # which tells a companion client the command was accepted.
            return ("error", 400, "plexsim: bad request")
        if segments[:1] == ["livetv"]:
            # No tuner. `livetv: 0` on /media/providers already hides the UI;
            # this is the shape a real PMS uses when it has no DVR configured.
            return self.bare_container(size=0)
        if route.startswith("/library/platforms") or route.startswith("/hubs/sections/home"):
            return ("error", 404, "plexsim: not found")

        if route == "/diagnostics/logs":
            return ("zip", self.log_archive())
        if route == "/diagnostics/databases":
            # `diagnostics` deliberately does not advertise "databases" -- see
            # media_providers() -- so the UI never draws this button. Answer
            # honestly if something asks anyway.
            return ("error", 404, "plexsim: no database")

        # -- library -----------------------------------------------------
        if route == "/library/sections":
            if method == "POST":
                return self.create_section(query)
            rows = [self.section_directory(s) for s in catalog.sections]
            # Measured on fun and on the 1.43.4 reference: this container carries
            # `title1` and allowSync only -- no identifier and no media-tag pair,
            # which the generic library container was adding.
            return self.bare_container(size=len(rows), allowSync=False,
                                       title1="Plex Library", Directory=rows)

        if route in ("/library", "/library/"):
            # The library root, measured: three directories and the secondary
            # container's art/title pair. python-plexapi asks for this first, and
            # it was never routed -- the old catch-all's empty container is what
            # hid that, which is the same trap from the other side.
            return self.bare_container(
                size=3, allowSync=False, art="/:/resources/library-art.png",
                content="", identifier="com.plexapp.plugins.library",
                mediaTagPrefix="/system/bundle/media/flags/",
                mediaTagVersion=1655122614, title1="Plex Library", title2="",
                Directory=[{"key": "sections", "title": "Library Sections"},
                           {"key": "recentlyAdded", "title": "Recently Added Content"},
                           {"key": "onDeck", "title": "On Deck Content"}])

        if route == "/library/shared/all":
            # What the client reads to decide which libraries a share covers.
            # Measured: its own identifier (`...library.shared`, not the library
            # one), rows that render as <Directory>, and a `key` pointing at the
            # section's LISTING rather than at the section.
            rows = [self.section_directory(s, shared=True) for s in catalog.sections]
            return self.bare_container(
                size=len(rows), allowSync=False,
                identifier="com.plexapp.plugins.library.shared",
                Metadata=rows, _xml={"Metadata": "Directory"})

        if route == "/library/optimize":
            # "Optimize Database". GET is a 404 on a real PMS: PUT only.
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            logger.info("OPTIMIZE database -- plexsim has none, nothing to do")
            return ("empty", 200)
        if route == "/library/clean/bundles":
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            return ("empty", 200)

        if route == "/library/tags":
            return self.tag_container(size=0)
        if route == "/library/matches":
            # Match is per item: /library/metadata/{id}/matches. The bare
            # endpoint is the provider's advertised key, and a real PMS answers
            # 400 for it.
            return ("error", 400, "plexsim: bad request")
        if route == "/library/people":
            return ("error", 404, "plexsim: not found")

        if segments[:2] == ["library", "collections"]:
            return self.collections_route(method, segments, query)
        if segments[:1] == ["playlists"]:
            return self.playlists_route(method, segments, query)

        if segments[:2] == ["library", "sections"] and len(segments) >= 3:
            if not segments[2].isdigit():
                # `/library/sections/prefs?type=movie` and friends: a real PMS
                # answers 400 rather than treating the word as a section id.
                return ("error", 400, "plexsim: bad request")
            return self.library_section(segments, query, method)

        if segments[:2] == ["library", "metadata"] and len(segments) >= 3:
            return self.library_metadata(segments, query, method)

        if route in ("/library/onDeck", "/library/recentlyAdded"):
            rows = [_listing_row(r) for r in list(catalog.items.values())[:12]]
            return self.container(Metadata=rows)

        if route == "/library/all":
            rows = [_listing_row(r) for r in catalog.items.values()]
            return self.container(Metadata=rows)

        if route == "/library/all/top":
            # Most played first -- what the Home screen's "Top" shelf reads.
            wanted = {"1": "movie", "2": "show", "4": "episode"}.get(query.get("type"))
            rows = [r for r in catalog.items.values()
                    if (not wanted or r.get("type") == wanted) and r.get("viewCount")]
            rows.sort(key=lambda r: -(r.get("viewCount") or 0))
            limit = int(query.get("limit", 12) or 12)
            return self.container(Metadata=[_listing_row(r) for r in rows[:limit]])

        if segments[:2] == ["library", "streams"]:
            # Sidecar subtitle extraction. plexsim never repackages, so every
            # subtitle stays embedded in the part and there is nothing to serve.
            # Measured: a real PMS answers 501 here for a stream it cannot
            # extract, which is what "not implemented" honestly means.
            return ("error", 501, "plexsim: streams are embedded in the part")

        if segments[:2] == ["library", "parts"] and method == "PUT":
            # Audio/subtitle track selection from the player or the Get Info
            # dialog: PUT /library/parts/{partID}?subtitleStreamID=&allParts=1.
            return self.select_stream(segments[2] if len(segments) > 2 else "", query)

        if route == "/library/search":
            # `universalsearch`. A real PMS answers the bare query with an empty
            # container; the hub search below is what the UI actually uses.
            return self.bare_container(size=0)

        # -- hubs --------------------------------------------------------
        if route == "/hubs":
            return self.hubs(query)
        if route == "/hubs/metadata":
            return ("error", 404, "plexsim: not found")
        if route == "/hubs/continueWatching":
            return self.hub_container(
                Hub=[self.hub("home.continue", "Continue Watching",
                              [_listing_row(r) for r in self.continue_watching()])])
        if route == "/hubs/promoted":
            return self.hubs(query)
        if route.startswith("/hubs/home"):
            if route == "/hubs/home/recentlyAdded" and query.get("sectionID"):
                # The TV section's Recently Added shelf links here rather than to
                # the section listing, so it has to answer for a section.
                rows = sorted(catalog.section_items(query["sectionID"]),
                              key=lambda r: -(r.get("addedAt") or 0))
                return self.container(Metadata=[_listing_row(r) for r in rows[:12]])
            # Measured on a real PMS: with no identifiers named, this answers with
            # no Hub at all.
            return self.hub_container()
        if segments[:2] == ["hubs", "sections"] and len(segments) == 3:
            return self.section_hubs(segments[2])
        if (segments[:2] == ["hubs", "sections"] and len(segments) == 5
                and segments[3] == "continueWatching"):
            # The Continue Watching shelf's own "see all" list.
            rows = [_listing_row(r) for r in self.continue_watching()
                    if str(r.get("librarySectionID")) == str(segments[2])]
            return self.container(Metadata=rows)
        if route == "/hubs/search":
            return self.search(query)


        # -- playback ----------------------------------------------------
        if route == "/playQueues" and method == "POST":
            return self.create_play_queue(query)
        if segments[:1] == ["playQueues"] and len(segments) >= 2:
            return self.play_queue_route(method, segments, query)

        if route == "/video/:/transcode/universal/decision":
            return self.decision(query)
        if route.startswith("/video/:/transcode/universal/start"):
            # plexsim has no encoder. A 404 here is the honest answer and it is
            # also the signal that the client's own gate rejected direct play.
            logger.warning("TRANSCODE REQUESTED -- client refused direct play: %s", query.get("path"))
            return ("error", 404, "plexsim: no transcoder")
        if route.startswith("/video/:/transcode/universal/"):
            # Includes /subtitles and /session: all of it belongs to a transcode
            # session, and plexsim never opens one.
            return ("error", 404, "plexsim: no transcoder")
        if route == "/photo/:/transcode":
            # The client asks for artwork through the transcoder, passing the
            # real path in `url`. Resolve that to the row's TMDB image when
            # there is one; only fall back to a generated tile when there is not.
            resolved = self.resolve_artwork(query.get("url", ""))
            if resolved:
                return ("image", resolved)
            return ("png", query)
        if route.startswith("/:/resources/"):
            return ("png", query)
        # A real PMS serves the official Plex Web client from /web, and that
        # client talks to the server it was loaded FROM -- window.location's
        # origin -- rather than discovering servers through plex.tv. That
        # sidesteps the resource `accessToken` entirely, which is the one thing
        # blocking app.plex.tv from connecting to plexsim.
        if route == "/web" or route == "/web/":
            return ("redirect", "/web/index.html")
        if route.startswith("/web/"):
            return ("static", route[len("/web/"):])
        # The web app opens this as soon as it connects. A real PMS pushes
        # library/playback events down it; plexsim has none to push, but the
        # channel has to exist and stay open or the client keeps reconnecting.
        if route == "/:/eventsource/notifications":
            return ("sse",)
        # This MUST be a real RFC6455 upgrade. Refusing it -- even cleanly with
        # a 501 -- does not make the web client fall back to the eventsource
        # channel: it logs "Unexpectedly closed server event connection",
        # decides the SERVER is faulty ("Possible connection error ... retrying
        # connection tests"), and renders no library at all. That was the true
        # cause of both the empty grid and the earlier "currently unavailable".
        if route == "/:/websockets/notifications":
            return ("websocket",)

        # -- timeline / state --------------------------------------------
        if route in ("/:/timeline", "/:/progress"):
            return self.timeline(query)
        if route == "/:/scrobble":
            return self.set_watched(query, True)
        if route == "/:/unscrobble":
            return self.set_watched(query, False)
        if route == "/:/rate":
            return self.rate(query)
        if route == "/actions/removeFromContinueWatching":
            return self.remove_from_continue_watching(query)

        # -- live TV / DVR / sync: plexsim has no tuner and no encoder ----
        if segments[:1] == ["media"] and segments[1:2] in (["subscriptions"], ["grabbers"]):
            return self.bare_container(size=0)
        if segments[:1] == ["sync"]:
            return ("error", 404, "plexsim: no sync")

        with self.lock:
            self.unknown.append("%s %s" % (method, route))
        # A real PMS answers an unrouted path with 404 (measured), and an empty
        # MediaContainer in its place is the trap this project already learnt
        # once from the other side: a client reads a 200 as "this endpoint
        # exists and there is nothing in it". The rule that made the empty
        # container look safe -- refusing an endpoint cleanly is worse than not
        # having it -- is about endpoints the client NEEDS, and the fix for those
        # is to route them, which is what tests/test_ui_surface.py holds the line
        # on. So: log it loudly, and answer honestly.
        logger.warning("UNKNOWN %s %s", method, route)
        return ("error", 404, "plexsim: not found")

    # -- endpoint bodies -------------------------------------------------

    def do_claim(self, query):
        """`PUT /myplex/claim?token=claim-XXXX` -- redeem and become claimed."""
        token = query.get("token", "")
        if not token:
            return ("error", 400, "plexsim: missing claim token")
        if self.claim is None:
            return ("error", 500, "plexsim: claim support not configured")
        try:
            self.claim.exchange(token)
        except Exception as error:
            logger.error("claim exchange failed: %s", error)
            return ("error", 500, "plexsim: claim exchange failed: %s" % error)

        # A real PMS immediately reports its new state, and announces
        # connectivity -- which is the call that needs a SERVER registry record.
        status, body = self.claim.announce_connectivity()
        logger.info("post-claim connectivity PUT -> %s %s", status, body)

        return self.myplex_account()

    def myplex_account(self):
        """`/myplex/account` -- who this server is signed in as."""
        claimed = self._claimed()
        return {
            "MyPlex": {
                "authToken": (self.claim.token if claimed else None),
                # fun and zen both report the owner's EMAIL here.
                "username": self._owner() if claimed else None,
                "mappingState": "mapped",
                "mappingError": "",
                "signInState": "ok" if claimed else "none",
                "publicAddress": self.public_address or "",
                "publicPort": 0,
                "privateAddress": self.private_address or "",
                "privatePort": 32400,
                "subscriptionFeatures": "pass",
                "subscriptionActive": claimed,
                "subscriptionState": "Active" if claimed else "Unknown",
            }
        }

    def media_providers(self):
        """`/media/providers` -- the first call a modern client makes.

        The Feature list is a capability contract: a client will not offer
        search, playback state or a play queue for a provider that does not
        advertise the matching feature, so an omission here looks to the user
        like a missing feature rather than a protocol error.
        """
        directories = [{"hubKey": "/hubs", "title": "Home"}]
        for section in self.catalog.sections:
            key = section["key"]
            directories.append(
                {
                    "agent": ("tv.plex.agents.series" if section["type"] == "show"
                              else "tv.plex.agents.movie"),
                    "language": "en-US",
                    "refreshing": False,
                    "scanner": ("Plex TV Series" if section["type"] == "show"
                                else "Plex Movie"),
                    "uuid": section["uuid"],
                    # A string on a real PMS, not a number.
                    "id": str(key),
                    "key": "/library/sections/%s" % key,
                    "hubKey": "/hubs/sections/%s" % key,
                    "type": section["type"],
                    "title": section["title"],
                    "updatedAt": 1757500000,
                    "scannedAt": 1757500000,
                    "Pivot": [
                        {
                            "id": "recommended",
                            "key": "/hubs/sections/%s" % key,
                            "type": "hub",
                            "title": "Recommended",
                            "context": "content.discover",
                            "symbol": "star",
                        },
                        {
                            "id": "library",
                            "key": "/library/sections/%s/all?type=%d"
                                   % (key, 2 if section["type"] == "show" else 1),
                            "type": "list",
                            "title": "Library",
                            "context": "content.library",
                            "symbol": "library",
                        },
                        # The Categories tab. plexsim can answer it now that the
                        # secondary axes are real, and a real PMS advertises it
                        # as the third pivot on every content directory.
                        {
                            "id": "categories",
                            "key": "/library/sections/%s/categories" % key,
                            "type": "list",
                            "title": "Categories",
                            "context": "content.categories",
                            "symbol": "stack",
                        },
                    ],
                }
            )

        # Order is the reference server's, feature for feature. It is not
        # cosmetic for diffing: harness/scripts/replay.py compares the i-th
        # Feature against the i-th, so a set that matches in a different order
        # reads as a dozen value differences and hides the real ones.
        features = [
            {"key": "/library/sections", "type": "content", "Directory": directories},
            {"key": "/hubs/search", "type": "search"},
            {"key": "/library/matches", "type": "match"},
            {"key": "/library/metadata", "type": "metadata"},
            {"key": "/:/rate", "type": "rate"},
            {"key": "/photo/:/transcode", "type": "imagetranscoder"},
            {"key": "/hubs/promoted", "type": "promoted"},
            {"key": "/hubs/continueWatching", "type": "continuewatching"},
            {
                "key": "/actions",
                "type": "actions",
                "Action": [
                    {
                        "id": "removeFromContinueWatching",
                        "key": "/actions/removeFromContinueWatching",
                    }
                ],
            },
            {"flavor": "universal", "key": "/playlists", "type": "playlist"},
            {"flavor": "universal", "key": "/playQueues", "type": "playqueue"},
            {"key": "/library/collections", "type": "collection"},
            {
                "key": "/:/timeline",
                "type": "timeline",
                "scrobbleKey": "/:/scrobble",
                "unscrobbleKey": "/:/unscrobble",
            },
            {"type": "manage"},
            {"type": "queryParser"},
            {"flavor": "download", "type": "subscribe"},
            {"key": "/library/search", "type": "universalsearch"},
        ]

        # Attribute set taken from a real claimed PMS 1.43.4's own
        # /media/providers, which carries 30 attributes plexsim was missing --
        # including apiVersion and the myPlex* claim-state pair. THIS, not
        # /identity, is where a client reads whether the server is signed in;
        # the web app re-requested this endpoint 43 times while they were
        # absent, treating an incomplete provider as not-yet-ready.
        #
        # The transcoder capability flags stay false: plexsim has no encoder,
        # and advertising one invites a transcode request it cannot serve.
        return self.bare_container(
            size=1,
            machineIdentifier=self.catalog.machine,
            friendlyName=self.catalog.friendly,
            version=SERVER_VERSION,
            apiVersion="1.1.1",
            musicAnalysis=2,
            myPlex=True,
            myPlexSubscription=self._claimed(),
            myPlexUsername=(self._owner() if self._claimed() else None),
            myPlexMappingState="mapped",
            myPlexSigninState="ok" if self._claimed() else "none",
            certificate=True,
            platform="Linux",
            platformVersion="6.1",
            eventStream=True,
            backgroundProcessing=True,
            companionProxy=True,
            pluginHost=True,
            updater=True,
            voiceSearch=True,
            sync=True,
            readOnlyLibraries=False,
            # Present and false, not absent: the client's Sync & Download menu
            # entry reads `server.allowSync`, and plexsim has no encoder to
            # convert anything for a device.
            allowSync=False,
            pushNotifications=False,
            allowCameraUpload=False,
            allowChannelAccess=True,
            # The Delete entry in an item's context menu is drawn only when the
            # server reports this (client module 92273's `showDelete`). plexsim
            # owns no files, so its delete drops the ROW from the in-memory
            # library and leaves the debrid content alone -- which is the only
            # honest local meaning of "delete" here, and a restart undoes it.
            allowMediaDeletion=True,
            allowSharing=True,
            allowTuners=False,
            livetv=0,
            offlineTranscode=0,
            streamingBrainVersion=2,
            streamingBrainABRVersion=3,
            # A real PMS says "logs,databases,streaminglogs"; plexsim has no
            # database, and this string is exactly what the Troubleshooting page
            # reads to decide which Download buttons to draw.
            diagnostics="logs,streaminglogs",
            countryCode="deu",
            updatedAt=1789127123,
            ownerFeatures=OWNER_FEATURES,
            transcoderActiveVideoSessions=0,
            transcoderVideo=False,
            transcoderAudio=False,
            transcoderSubtitles=False,
            transcoderLyrics=False,
            MediaProvider=[
                {
                    "identifier": "com.plexapp.plugins.library",
                    "title": "Library",
                    "types": "video",
                    # `download` here is what draws the Download entry in the
                    # item context menu (client module 92273: `showDownload`
                    # reads `provider.protocols.download`). plexsim can serve
                    # it -- the same Part read with `download=1` -- so omitting
                    # it hid a feature that works.
                    "protocols": "stream,download",
                    "Feature": features,
                }
            ],
        )

    def library_section(self, segments, query, method="GET"):
        section_id = segments[2]
        section = self.catalog.section(section_id) or {}
        if len(segments) == 3:
            if method == "DELETE":
                # Manage > Libraries > Delete Library. Drops the section and
                # every row in it, in memory: catalog.json is untouched.
                if self.catalog.remove_section(section_id) is None:
                    return ("error", 404, "plexsim: no such section")
                logger.info("SECTION delete %s (%s)", section_id, section.get("title"))
                return ("empty", 200)
            if method == "PUT":
                # The Edit Library dialog. A real PMS refuses a partial edit
                # (measured: 400 for `?name=` alone), and the client always
                # sends the whole form, so a missing name is a client error.
                if not query.get("name"):
                    return ("error", 400, "plexsim: bad request")
                section["title"] = query["name"]
                logger.info("SECTION edit %s -> %s", section_id, query["name"])
                return ("empty", 200)
            return self.container(
                Directory=[
                    {"key": "all", "title": "All"},
                    {"key": "recentlyAdded", "title": "Recently Added"},
                ]
            )

        leaf = segments[3]

        # -- the write half of Manage > Libraries -------------------------
        if leaf == "refresh":
            # "Scan Library Files" / "Refresh All Metadata"; DELETE is the
            # Cancel Scan button. plexsim's rows come from the catalog, so a
            # scan re-versions artwork and reports an activity per item, which
            # is what the Libraries page and the Dashboard read.
            if method == "DELETE":
                self.refreshing.discard(str(section_id))
                self.library.clear_activities()
                logger.info("SECTION refresh cancelled %s", section_id)
                return ("empty", 200)
            self.refreshing.add(str(section_id))
            # Measured title, verbatim: it is what the header's activity spinner
            # shows while a scan runs.
            self.notify_status('Scanning the "%s" section' % (section.get("title") or ""))
            rows = self.catalog.section_items(section_id)
            for row in rows:
                if query.get("force") == "1":
                    self.library.refresh(row)
                else:
                    self.library.add_activity(row.get("title") or "")
            self.refreshing.discard(str(section_id))
            logger.info("SECTION refresh %s force=%s -> %d row(s)",
                        section_id, query.get("force") == "1", len(rows))
            return ("empty", 200)
        if leaf in ("emptyTrash", "analyze"):
            if method != "PUT":
                return ("error", 405, "plexsim: method not allowed")
            logger.info("SECTION %s %s -- nothing to %s, plexsim owns no files",
                        leaf, section_id, leaf)
            return ("empty", 200)
        if leaf == "indexes":
            # Delete Preview Thumbnails. plexsim generates none.
            if method != "DELETE":
                return ("error", 404, "plexsim: not found")
            return ("empty", 200)
        if leaf == "prefs":
            settings = SECTION_PREFS.get(section.get("type") or "movie") or []
            if method == "PUT":
                # Section settings have their own id space (`flattenSeasons` and
                # friends), so they cannot go through the server-prefs writer --
                # which filters against the server's own ids and would drop every
                # one of them silently.
                known = {s.get("id") for s in settings}
                for name, value in query.items():
                    if name.startswith("X-Plex-") or name not in known:
                        continue
                    self.section_pref_overrides[(str(section_id), name)] = value
                    logger.info("SECTION PREF %s %s = %s", section_id, name, value)
                return ("empty", 200)
            # A real PMS answers with a bare container and the full setting
            # list for the library's type.
            settings = [
                dict(s, value=self.section_pref_overrides[(str(section_id), s.get("id"))])
                if (str(section_id), s.get("id")) in self.section_pref_overrides else s
                for s in settings
            ]
            return self.bare_container(size=len(settings), Setting=settings)

        # -- listings ------------------------------------------------------
        if leaf == "all":
            return self.section_listing(section_id, section, query)
        if leaf in ("recentlyAdded", "newest", "recentlyViewed", "onDeck"):
            rows = self.catalog.section_items(section_id)
            if leaf == "onDeck":
                rows = [r for r in self.continue_watching()
                        if str(r.get("librarySectionID")) == str(section_id)]
            elif leaf == "recentlyViewed":
                rows = [r for r in rows if r.get("lastViewedAt")]
            else:
                rows = sorted(rows, key=lambda r: -(r.get("addedAt") or 0))[:12]
            return self.listing_container(
                section_id, section, [_listing_row(r) for r in rows])
        if leaf == "collections":
            # Measured: this listing keeps the SECTION's viewGroup and title2
            # ("movie" / "All Movies"), not "collection" -- the rows are
            # collections but the view is still the library's.
            rows = self.library.collections(section_id)
            return self.listing_container(section_id, section, rows, query=query)
        if leaf == "folder":
            # By Folder. plexsim's rows have one Part each, so the folder tree
            # is one level: the release directory each Part sits in.
            rows = []
            for index, row in enumerate(self.catalog.section_items(section_id), 1):
                title = row.get("title") or ""
                rows.append({"key": "/library/sections/%s/folder?parent=%d"
                                    % (section_id, index), "title": title})
            return self.secondary_container(
                section_id, section, "By Folder", Metadata=rows,
                _xml={"Metadata": "Directory"},
                librarySectionID=int(section_id) if str(section_id).isdigit() else section_id,
                librarySectionTitle=section.get("title"),
                librarySectionUUID=section.get("uuid"))

        # -- the filter and sort descriptors the legacy UI reads -----------
        if leaf == "sorts":
            sorts = [dict(s) for s in SECTION_SORTS]
            sorts[0]["firstCharacterKey"] = "/library/sections/%s/firstCharacter" % section_id
            return self.secondary_container(section_id, section, None,
                                            Directory=sorts)
        if leaf == "filters":
            filters = [
                {
                    "filter": name,
                    "filterType": kind,
                    "key": "/library/sections/%s/%s" % (section_id, name),
                    "title": title,
                    "type": "filter",
                }
                for name, title, kind in SECTION_FILTERS
            ]
            return self.secondary_container(section_id, section, None,
                                            Directory=filters)
        if leaf == "firstCharacter":
            counts = {}
            for row in self.catalog.section_items(section_id):
                title = str(row.get("titleSort") or row.get("title") or "")
                first = (title[:1] or "#").upper()
                if not first.isalpha():
                    first = "#"
                counts[first] = counts.get(first, 0) + 1
            entries = [{"size": counts[k], "key": k, "title": k} for k in sorted(counts)]
            return self.secondary_container(section_id, section, "By First Letter",
                                            Directory=entries)
        if leaf == "categories":
            # The Categories tab: one tile per genre, each opening the filtered
            # list. Measured shape: Directory rows of {thumb, type:"directory",
            # key, title} plus a Meta.Type block naming the active category. The
            # thumb is an art transcode of one of the genre's own items, which is
            # what a real PMS blends its tile colour from.
            tiles = []
            for entry in (self.secondary_directory(section_id, section, "genre")
                          ["MediaContainer"].get("Directory") or []):
                art = None
                for row in self.catalog.section_items(section_id):
                    if any(str(tag.get("id")) == entry["key"]
                           for tag in row.get("Genre") or []):
                        art = row.get("art")
                        break
                tile = {
                    "type": "directory",
                    "key": entry["fastKey"],
                    "title": entry["title"],
                }
                if art:
                    tile["thumb"] = ("/photo/:/transcode?width=2560&height=1440&url=%s"
                                     % urllib.parse.quote(art, safe=""))
                tiles.append(tile)
            return self.secondary_container(
                section_id, section, "", Directory=tiles,
                Meta={"Type": [{"key": "/library/sections/%s/categories" % section_id,
                                "type": "directory", "title": "Categories",
                                "active": True}]})

        if leaf in SECONDARY_TAGS or leaf in SECONDARY_FIELDS:
            return self.secondary_directory(section_id, section, leaf)

        if leaf in ("unwatched", "recentlyReleased"):
            # Listing shortcuts the hub shelves link to.
            rows = self.catalog.section_items(section_id)
            if leaf == "unwatched":
                rows = [r for r in rows if not r.get("viewCount")]
            else:
                rows = sorted(rows, key=lambda r: str(r.get("originallyAvailableAt") or ""),
                              reverse=True)
            return self.listing_container(
                section_id, section, [_listing_row(r) for r in rows])
        if leaf == "resolution":
            # A real axis, read off the Media rather than off a tag.
            seen = {}
            for row in self.catalog.section_items(section_id):
                for media in row.get("Media") or []:
                    value = media.get("videoResolution")
                    if value:
                        seen[str(value)] = True
            entries = [
                {"fastKey": "/library/sections/%s/all?resolution=%s" % (section_id, v),
                 "key": v, "title": v}
                for v in sorted(seen, key=lambda x: (not x.isdigit(), x))
            ]
            return self.secondary_container(section_id, section, "By Resolution",
                                            Directory=entries)
        if leaf == "search":
            # Measured: a real PMS answers 400 here; search is /hubs/search.
            return ("error", 400, "plexsim: bad request")
        if leaf == "composite":
            return ("png", query)

        # A leaf a real PMS does not have is a 404 there, not an empty container.
        logger.warning("UNKNOWN section leaf %s on section %s", leaf, section_id)
        return ("error", 404, "plexsim: not found")

    def section_listing(self, section_id, section, query):
        """`/library/sections/{id}/all` -- the grid, with the client's filters.

        Ignoring the filter params would make every secondary-filter Directory
        entry (By Genre, By Year, ...) open a list of the whole library, which
        looks like the filter is broken rather than unimplemented.
        """
        wanted = {"1": "movie", "2": "show", "3": "season", "4": "episode"}.get(
            query.get("type"))
        if wanted in ("season", "episode"):
            source = [r for r in self.catalog.items.values()
                      if r.get("type") == wanted
                      and str(r.get("librarySectionID")) == str(section_id)]
            source.sort(key=lambda r: (r.get("grandparentTitle") or r.get("parentTitle") or "",
                                       r.get("parentIndex") or 0, r.get("index") or 0))
        elif query.get("type") == "18":
            # Plex's collection type. The Collections pivot asks for it here
            # rather than at /library/sections/{id}/collections.
            rows = self.library.collections(section_id)
            return self.listing_container(section_id, section, rows, query=query)
        else:
            source = self.catalog.section_items(section_id)

        source = [r for r in source if self._matches_filters(r, query)]
        source = self._sorted(source, query.get("sort") or "")
        rows = [_listing_row(r) for r in source]
        # Measured: only the unpaged /all listing carries `nocache`; newest,
        # recentlyAdded, onDeck and collections carry neither it nor totalSize.
        extra = {}
        if not ("X-Plex-Container-Start" in query or "X-Plex-Container-Size" in query):
            extra["nocache"] = True
        if query.get("includeMeta") == "1":
            meta = self.section_meta(section_id, section.get("type") or "movie")
            if meta:
                extra["Meta"] = meta
        return self.listing_container(section_id, section, rows, query=query, **extra)

    # The sort keys the captured Meta advertises. A key the listing cannot honour
    # is a sort the UI offers and the server ignores, which reads as a broken
    # control rather than a missing one.
    TEXT_SORTS = ("titleSort", "title", "originallyAvailableAt")

    def _sorted(self, rows, spec):
        key = spec.split(":")[0]
        reverse = spec.endswith(":desc")
        if key == "random":
            # Deterministic per row so a re-request is stable, which is what a
            # client paging through a random sort needs.
            return sorted(rows, key=lambda r: zlib.crc32(str(r.get("ratingKey")).encode()))
        if key == "mediaHeight":
            return sorted(rows,
                          key=lambda r: max([m.get("height") or 0
                                             for m in r.get("Media") or []] or [0]),
                          reverse=reverse)
        if key in ("addedAt", "lastViewedAt", "year", "rating", "audienceRating",
                   "duration", "viewCount", "viewOffset", "index",
                   "leafCount", "unviewedLeafCount") + self.TEXT_SORTS:
            def value(row):
                if key not in self.TEXT_SORTS:
                    return (row.get(key) is None, row.get(key) or 0)
                # `titleSort` is the sort key the client sends, and a row that
                # has no titleSort of its own sorts by its title -- a playlist
                # row has only the latter, so keying on titleSort alone made
                # every playlist compare equal and the direction do nothing.
                text = row.get(key)
                if key == "titleSort" and not text:
                    text = row.get("title")
                return (text is None, str(text or "").lower())
            return sorted(rows, key=value, reverse=reverse)
        return rows

    def _matches_filters(self, row, query):
        """One row against the filter params the client sends."""
        for name, value in query.items():
            if name in SECONDARY_TAGS:
                key = SECONDARY_TAGS[name]
                tags = row.get(key) or []
                if not any(str(t.get("id")) == value or t.get("tag") == value
                           for t in tags):
                    return False
            elif name in SECONDARY_FIELDS and name != "decade":
                if str(row.get(SECONDARY_FIELDS[name]) or "") != urllib.parse.unquote(value):
                    return False
            elif name == "decade":
                year = row.get("year")
                if not year or not str(value).isdigit():
                    return False
                if int(year) // 10 * 10 != int(value):
                    return False
            elif name == "resolution":
                if not any(str(m.get("videoResolution")) == value
                           for m in row.get("Media") or []):
                    return False
            elif name == "unwatched" and value == "1":
                if row.get("viewCount"):
                    return False
            elif name == "inProgress" and value == "1":
                if not row.get("viewOffset"):
                    return False
            elif name == "unmatched" and value == "1":
                # An unmatched row is one with no agent identity, and a real PMS
                # writes that as `tv.plex.agents.none://<key>`; `local://` is the
                # legacy form. The client's own Match/Fix Match predicate tests
                # for exactly these two, so the filter has to agree with it.
                guid = str(row.get("guid") or "")
                if not (guid.startswith("local://") or "none://" in guid):
                    return False
        return True

    def listing_container(self, section_id, section, rows, view_group=None,
                          query=None, **extra):
        """The container a section listing returns, with the client's paging."""
        query = query or {}
        start = int(query.get("X-Plex-Container-Start", 0) or 0)
        size = int(query.get("X-Plex-Container-Size", len(rows)) or len(rows))
        page = rows[start:start + size]
        paged = "X-Plex-Container-Start" in query or "X-Plex-Container-Size" in query
        fields = {
            "librarySectionID": int(section_id) if str(section_id).isdigit() else section_id,
            "librarySectionTitle": section.get("title"),
            "librarySectionUUID": section.get("uuid"),
            "title1": section.get("title"),
            "title2": "All " + str(section.get("title") or ""),
            "viewGroup": view_group or section.get("type"),
            # Matched against a real PMS movie section; the web client's
            # grid reads these alongside Meta.
            "viewMode": 65592,
            "art": "/:/resources/show-fanart.jpg" if section.get("type") == "show"
                   else "/:/resources/movie-fanart.jpg",
            "thumb": "/:/resources/show.png" if section.get("type") == "show"
                     else "/:/resources/movie.png",
            "allowSync": False,
        }
        # Measured: a paged listing carries offset and totalSize, an unpaged one
        # carries `nocache` instead. The same rule child_container() follows --
        # and the missing totalSize is what left show pages with no Seasons
        # block for a week.
        if paged:
            fields["offset"] = start
            fields["totalSize"] = len(rows)
        fields.update(extra)
        return self.container(Metadata=page, **fields)

    def secondary_container(self, section_id, section, title2, **fields):
        """A secondary list (By Genre, By Year, sorts, filters).

        Measured: `content=secondary`, `viewGroup=secondary`, no
        librarySection* pair except on the folder view, and title2 naming the
        axis. The generic listing container's fields are wrong here.
        """
        body = {
            "allowSync": False,
            "art": "/:/resources/show-fanart.jpg" if section.get("type") == "show"
                   else "/:/resources/movie-fanart.jpg",
            "content": "secondary",
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": 1655122614,
            "thumb": "/:/resources/show.png" if section.get("type") == "show"
                     else "/:/resources/movie.png",
            "title1": section.get("title"),
            "viewGroup": "secondary",
            "viewMode": 65592,
        }
        if title2 is not None:
            # An empty title2 is sent, not omitted -- the Categories view's is "".
            body["title2"] = title2
        body.update(fields)
        for key in ("Directory", "Metadata"):
            if isinstance(body.get(key), list):
                body["size"] = len(body[key])
        body.setdefault("size", 0)
        return {"MediaContainer": self._prune(body)}

    def secondary_directory(self, section_id, section, leaf):
        """`/library/sections/{id}/{genre,year,director,...}`.

        A tag axis keys its entries by TAG ID and a field axis by the value
        itself -- measured: `?genre=24` but `?contentRating=PG`. Getting that
        backwards makes every entry open an empty list.
        """
        entries = []
        if leaf in SECONDARY_TAGS:
            key = SECONDARY_TAGS[leaf]
            seen = {}
            for row in self.catalog.section_items(section_id):
                for tag in row.get(key) or []:
                    name = tag.get("tag")
                    if not name or name in seen:
                        continue
                    seen[name] = tag.get("id")
            for name in sorted(seen):
                tag_id = seen[name]
                entries.append({
                    "fastKey": "/library/sections/%s/all?%s=%s" % (section_id, leaf, tag_id),
                    "key": str(tag_id),
                    "title": name,
                    "type": leaf,
                })
        else:
            field = SECONDARY_FIELDS[leaf]
            seen = set()
            for row in self.catalog.section_items(section_id):
                value = row.get(field)
                if value is None or value == "":
                    continue
                if leaf == "decade":
                    value = int(value) // 10 * 10
                seen.add(str(value))
            for value in sorted(seen, reverse=leaf in ("year", "decade")):
                entries.append({
                    "fastKey": "/library/sections/%s/all?%s=%s"
                               % (section_id, leaf, urllib.parse.quote(value)),
                    "key": urllib.parse.quote(value),
                    "title": (value + "s") if leaf == "decade" else value,
                })
        return self.secondary_container(section_id, section,
                                        SECONDARY_TITLES.get(leaf), Directory=entries)

    def library_metadata(self, segments, query, method="GET"):
        raw_key = segments[2]

        # A comma-joined key list is a real client pattern (plezy batches).
        if "," in raw_key:
            rows = []
            for piece in raw_key.split(","):
                if piece.isdigit() and int(piece) in self.catalog.items:
                    rows.append(_full(self.catalog.items[int(piece)]))
            return self.container(Metadata=rows)

        if not raw_key.isdigit():
            return self.container()

        rating_key = int(raw_key)
        row = self.catalog.items.get(rating_key)
        if row is None:
            return ("error", 404, "plexsim: no such item")

        if len(segments) == 3:
            if method == "PUT":
                # The Edit dialog, and "Add to Collection": the client PUTs
                # `<field>.value`, `<field>.locked` and `<tag>[N].tag.tag` to
                # the item's own key.
                self.library.edit(row, query)
                logger.info("EDIT %s %s", rating_key,
                            ",".join(k for k in query if not k.startswith("X-Plex-")))
                return ("empty", 200)
            if method == "DELETE":
                if query.get("proxy") == "1":
                    # Delete Version. plexsim has no optimized versions, and a
                    # real PMS answers 400 when there are none to delete.
                    return ("error", 400, "plexsim: no optimized versions")
                self.library.delete(rating_key)
                logger.info("DELETE %s (%s)", rating_key, row.get("title"))
                return ("empty", 200)
            section = self.catalog.section(row["librarySectionID"]) or {}
            detail = _full(row)
            if row.get("type") == "show":
                detail = self.show_detail(row, detail, query)
            # Re-measured 2026-09-12 against BOTH fun and the 1.43.4 reference,
            # with the client's own includeX param set: the detail container
            # DOES carry identifier and the media-tag pair. An earlier note here
            # claimed the opposite ("an item-detail container is BARE"); it did
            # not hold on either server, and the missing mediaTagPrefix is what
            # a client uses to resolve a tag into an image URL.
            return self.bare_container(
                size=1,
                allowSync=True,
                identifier="com.plexapp.plugins.library",
                mediaTagPrefix="/system/bundle/media/flags/",
                mediaTagVersion=1655122614,
                librarySectionID=row["librarySectionID"],
                librarySectionTitle=section.get("title"),
                librarySectionUUID=section.get("uuid"),
                Metadata=[detail],
            )

        leaf = segments[3]
        if leaf in ("children", "allLeaves"):
            if leaf == "children":
                rows = self.catalog.children(rating_key)
            else:
                rows = self.catalog.leaves(rating_key)
            return self.child_container(row, rows, leaf, query)

        # -- the context menu's write half --------------------------------
        if leaf in ("refresh", "analyze", "unmatch", "match", "split", "merge"):
            return self.item_action(row, leaf, method, query)
        if leaf == "matches":
            return self.matches(row, query)
        if leaf in ("posters", "arts"):
            if method == "PUT":
                return self.set_artwork(row, leaf, query)
            return self.artwork_list(row, leaf)
        if leaf == "tree":
            return self.metadata_tree(row)
        if leaf == "media" and len(segments) > 4:
            if method != "DELETE":
                return ("error", 405, "plexsim: method not allowed")
            if not self.library.delete_media(row, segments[4]):
                return ("error", 404, "plexsim: no such media")
            return ("empty", 200)

        if leaf == "prefs":
            # Per-item settings: a show's episode sorting, from its Edit dialog.
            # Measured: the GET is a 404 and only the PUT exists.
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            for name, value in query.items():
                if name.startswith("X-Plex-"):
                    continue
                row.setdefault("_prefs", {})[name] = value
                logger.info("ITEM PREF %s %s = %s", rating_key, name, value)
            return ("empty", 200)
        if leaf == "subtitles":
            # The Search Subtitles dialog. A real PMS answers with `Stream` rows
            # from its subtitle agent; plexsim has no subtitle source, so the
            # honest answer is that shape with nothing in it rather than a
            # different shape or a refusal the dialog reads as an error.
            return self.tag_container(size=0, Stream=[])
        if leaf in ("markers", "banner", "chapterImages", "indexes"):
            # All 404 on a real PMS for a row like plexsim's: no intro markers,
            # no banner artwork, no chapter thumbnails, no preview index.
            return ("error", 404, "plexsim: not found")
        if leaf == "extras":
            return self.tag_container(size=0)
        if leaf == "related":
            section = self.catalog.section(row["librarySectionID"]) or {}
            return self.bare_container(
                size=0, allowSync=False, identifier="com.plexapp.plugins.library",
                librarySectionID=row["librarySectionID"],
                librarySectionTitle=section.get("title"),
                librarySectionUUID=section.get("uuid"),
                mediaTagPrefix="/system/bundle/media/flags/",
                mediaTagVersion=1655122614)
        if leaf == "similar":
            # Measured: a real PMS 404s this for a season or an episode and
            # answers an empty container for a movie or a show.
            if row.get("type") in ("season", "episode"):
                return ("error", 404, "plexsim: not found")
            return self.tag_container(size=0)
        if leaf == "userState":
            # A real PMS 404s this (verified against fun). Answering with an
            # empty MediaContainer instead tells the client there IS state.
            return ("error", 404, "plexsim: no user state")
        if leaf == "theme":
            # No theme audio; answer empty rather than falling through to the
            # unknown-route handler.
            return ("error", 404, "plexsim: no theme")
        if leaf in ("thumb", "art", "poster", "background", "composite"):
            resolved = self.artwork_for(rating_key, leaf)
            if resolved:
                return ("image", resolved)
            return ("png", query)
        # Includes `/move`, which a real PMS 404s on an item: ordering is done
        # through the collection or playlist that holds it, not through the item.
        logger.warning("UNKNOWN item leaf %s on %s", leaf, rating_key)
        return ("error", 404, "plexsim: not found")

    def paged_container(self, rows, query, **fields):
        """A bare container that pages when the client asks, and not otherwise.

        Measured on /playlists/all and /playlists/{id}/items: a paged request
        gets `totalSize` and `offset` beside `size`, an unpaged one gets neither.
        The same rule as every other listing here -- the client pages playlists
        as soon as there are more than a screenful.
        """
        query = query or {}
        start = int(query.get("X-Plex-Container-Start", 0) or 0)
        size = int(query.get("X-Plex-Container-Size", len(rows)) or len(rows))
        page = rows[start:start + size]
        body = dict(fields)
        body["size"] = len(page)
        if "X-Plex-Container-Start" in query or "X-Plex-Container-Size" in query:
            body["totalSize"] = len(rows)
            body["offset"] = start
        body["Metadata"] = page
        return self.bare_container(**body)

    def hub_container(self, **fields):
        """A hub response. Measured on a real PMS: `size`, `allowSync` and
        `identifier` only -- no mediaTagPrefix or mediaTagVersion, which the
        generic library container adds."""
        body = {"size": 0, "allowSync": False, "identifier": "com.plexapp.plugins.library"}
        body.update(fields)
        if isinstance(fields.get("Hub"), list):
            body["size"] = len(fields["Hub"])
        return {"MediaContainer": self._prune(body)}

    def tag_container(self, **fields):
        """The container a per-item sub-resource uses: posters, arts, matches,
        tree, similar and extras all carry identifier plus the media-tag pair
        and nothing else."""
        body = {
            "size": 0,
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": 1655122614,
        }
        body.update(fields)
        return {"MediaContainer": self._prune(body)}

    def hub(self, identifier, title, rows, **extra):
        body = {
            "hubIdentifier": identifier,
            "title": title,
            "type": "movie",
            "hubKey": "/library/all",
            "size": len(rows),
            "more": False,
            "Metadata": rows,
        }
        body.update(extra)
        return body

    def hubs(self, query):
        """`/hubs` -- the Home screen's shelves.

        `identifier=` names the shelves the client wants; returning all of them
        regardless means Home draws rows it did not ask for, in the wrong places.
        """
        wanted = [i for i in (query.get("identifier") or "").split(",") if i]
        hubs = [self.hub("home.continue", "Continue Watching",
                         [_listing_row(r) for r in self.continue_watching()])]
        for section in self.catalog.sections:
            rows = [_listing_row(r) for r in self.catalog.section_items(section["key"])]
            if not rows:
                continue
            hubs.append(
                self.hub(
                    "movie.recentlyadded.%s" % section["key"],
                    "Recently Added in %s" % section["title"],
                    rows[:12],
                    hubKey="/library/sections/%s/recentlyAdded" % section["key"],
                    librarySectionID=int(section["key"]),
                    librarySectionTitle=section["title"],
                )
            )
        if wanted:
            hubs = [h for h in hubs if h["hubIdentifier"] in wanted
                    or h["hubIdentifier"].split(".")[0] in wanted]
        return self.hub_container(Hub=hubs)

    def section_hubs(self, section_id):
        """`/hubs/sections/{id}` -- the library's Recommended tab.

        Five shelves, measured hub for hub against the reference: identifier,
        title, type, style, context, the `promoted` flag on Recently Added and
        the `key` each shelf's "see all" link follows. plexsim answered with one
        shelf, so the tab existed and was nearly empty; every row here comes out
        of its own catalog.
        """
        section = self.catalog.section(section_id) or {}
        is_show = section.get("type") == "show"
        title = section.get("title") or ""
        items = self.catalog.section_items(section_id)
        episodes = [r for r in self.catalog.items.values()
                    if r.get("type") == "episode"
                    and str(r.get("librarySectionID")) == str(section_id)]
        in_progress = [r for r in self.continue_watching()
                       if str(r.get("librarySectionID")) == str(section_id)]
        watched = sorted([r for r in (episodes if is_show else items)
                          if r.get("lastViewedAt")],
                         key=lambda r: -(r.get("lastViewedAt") or 0))
        released = sorted(episodes if is_show else items,
                          key=lambda r: str(r.get("originallyAvailableAt") or ""),
                          reverse=True)
        added = sorted(items, key=lambda r: -(r.get("addedAt") or 0))

        def shelf(identifier, shelf_title, rows, kind, key, context, **extra):
            return self.hub(
                identifier, shelf_title, [_listing_row(r) for r in rows[:12]],
                type=kind, hubKey=key, key=key, context=context, style="shelf",
                more=False, size=len(rows[:12]),
                librarySectionID=int(section_id) if str(section_id).isdigit() else section_id,
                librarySectionTitle=title, **extra)

        prefix = "tv" if is_show else "movie"
        hubs = [
            shelf("%s.inprogress.%s" % (prefix, section_id), "Continue Watching",
                  in_progress, "episode" if is_show else "movie",
                  "/hubs/sections/%s/continueWatching/items" % section_id,
                  "hub.%s.inprogress" % prefix),
        ]
        if is_show:
            hubs += [
                shelf("tv.recentlyaired.%s" % section_id, "Recently Released Episodes",
                      released, "episode",
                      "/library/sections/%s/all?type=4&sort=originallyAvailableAt:desc"
                      % section_id, "hub.tv.recentlyaired"),
                shelf("tv.recentlyadded.%s" % section_id, "Recently Added in %s" % title,
                      added, "mixed",
                      "/hubs/home/recentlyAdded?type=2&sectionID=%s" % section_id,
                      "hub.tv.recentlyadded", promoted=True),
                shelf("tv.rediscover.%s" % section_id, "Rediscover", watched, "show",
                      "/library/sections/%s/all?type=2&unwatchedLeaves=1&viewCount>=1"
                      % section_id, "hub.tv.rediscover", random=True),
                shelf("tv.recentlyviewed.%s" % section_id, "Recently Watched Episodes",
                      watched, "episode",
                      "/library/sections/%s/all?sort=lastViewedAt:desc&unwatched=0"
                      "&viewOffset=0&type=4" % section_id, "hub.tv.recentlyviewed"),
            ]
        else:
            top_genre, top_rows = self._top_genre(items)
            hubs += [
                shelf("movie.recentlyreleased.%s" % section_id,
                      "Recently Released Movies", released, "movie",
                      "/library/sections/%s/all?sort=originallyAvailableAt:desc"
                      "&originallyAvailableAt>=-1y" % section_id,
                      "hub.movie.recentlyreleased"),
                shelf("movie.recentlyadded.%s" % section_id,
                      "Recently Added in %s" % title, added, "movie",
                      "/library/sections/%s/all?sort=addedAt:desc" % section_id,
                      "hub.movie.recentlyadded", promoted=True),
            ]
            if top_genre:
                hubs.append(shelf(
                    "movie.genre.%s.%s" % (section_id, top_genre["id"]),
                    "Top Movies in %s" % top_genre["tag"], top_rows, "movie",
                    "/library/sections/%s/all?unwatched=1&genre=%s&audienceRating>=7.0"
                    % (section_id, top_genre["id"]), "hub.movie.genre"))
            hubs.append(shelf(
                "movie.recentlyviewed.%s" % section_id, "Recently Watched Movies",
                watched, "movie",
                "/library/sections/%s/all?sort=lastViewedAt:desc&unwatched=0"
                "&viewOffset=0" % section_id, "hub.movie.recentlyviewed"))

        return self.hub_container(
            librarySectionID=int(section_id) if str(section_id).isdigit() else section_id,
            librarySectionTitle=title,
            librarySectionUUID=section.get("uuid"),
            Hub=hubs,
        )

    def _top_genre(self, rows):
        """The genre shelf a real PMS puts on a movie section's Recommended tab:
        the most common genre, unwatched and well rated."""
        counts = {}
        for row in rows:
            for tag in row.get("Genre") or []:
                if tag.get("tag"):
                    counts.setdefault(tag["tag"], (tag, []))[1].append(row)
        if not counts:
            return None, []
        best = max(counts.items(), key=lambda kv: len(kv[1][1]))
        tag, members = best[1]
        members = [r for r in members
                   if not r.get("viewCount") and (r.get("audienceRating") or 0) >= 7.0]
        return tag, members

    def search(self, query):
        rows = [_listing_row(r) for r in self.catalog.search(query.get("query", ""))]
        limit = int(query.get("limit", 30) or 30)
        hubs = []
        for kind, title in (("movie", "Movies"), ("show", "TV Shows"),
                            ("episode", "Episodes")):
            matched = [r for r in rows if r.get("type") == kind][:limit]
            if not matched:
                continue
            hubs.append(self.hub(kind, title, matched, type=kind, hubIdentifier=kind))
        return self.bare_container(size=len(hubs), Hub=hubs)

    def _items_for_uri(self, uri):
        """Resolve a playQueue `uri` to the rows it should enqueue.

        Two shapes reach here, and only the first was handled before:

          server://<machine>/com.plexapp.plugins.library/library/metadata/1001
          server://<machine>/com.plexapp.plugins.library/library/sections/1/all

        The second is what the library's "Play all" button sends. Returning an
        empty queue for it makes the web client show "An error occurred loading
        items to play" without ever calling this endpoint again.
        """
        path = urllib.parse.unquote(uri or "")
        # A library section: enqueue the whole section, in listing order.
        match = re.search(r"/library/sections/([^/]+)/all", path)
        if match:
            return self.catalog.section_items(match.group(1))
        # A single item.
        match = re.search(r"/library/metadata/(\d+)", path)
        if match:
            row = self.catalog.items.get(int(match.group(1)))
            return [row] if row else []
        # A bare trailing ratingKey, which some clients send.
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit() and int(tail) in self.catalog.items:
            return [self.catalog.items[int(tail)]]
        return []

    def create_play_queue(self, query):
        """`POST /playQueues` -- every client builds a queue before playing.

        `shuffle=1` is the Shuffle context-menu entry, and `playQueueSourceURI`
        is on a real PMS's response (measured): the client echoes it back when
        it later adds to the same queue.
        """
        uri = query.get("uri", "")
        rows = self._items_for_uri(uri)
        if query.get("shuffle") == "1":
            # Deterministic per queue rather than random, so a test can assert
            # the order changed without asserting which order.
            rows = list(reversed(rows))

        with self.lock:
            queue_id = self.next_play_queue
            self.next_play_queue += 1
            self.play_queues[queue_id] = [int(r["ratingKey"]) for r in rows]
            self.play_queue_sources[queue_id] = uri
            self.play_queue_shuffled[queue_id] = query.get("shuffle") == "1"

        logger.info("PLAYQUEUE %s -> %d item(s) from uri=%s",
                    queue_id, len(rows), uri[:80])
        return self.play_queue_container(queue_id)

    def get_play_queue(self, queue_id):
        return self.play_queue_container(
            int(queue_id) if str(queue_id).isdigit() else 0)

    def play_queue_container(self, queue_id):
        """One play-queue response, used by create, fetch and add-to.

        Every field here is on a real PMS's, including playQueueSourceURI --
        which plexsim omitted, so a client that re-read the queue after adding
        to it lost the source it was playing from.
        """
        with self.lock:
            keys = list(self.play_queues.get(queue_id) or [])
            source = self.play_queue_sources.get(queue_id, "")
            shuffled = bool(self.play_queue_shuffled.get(queue_id))
        metadata = []
        for position, rating_key in enumerate(keys):
            row = self.catalog.items.get(rating_key)
            if row is None:
                continue
            item = _full(row)
            item["playQueueItemID"] = queue_id * 1000 + position + 1
            metadata.append(item)
        fields = {
            "Metadata": metadata,
            "playQueueID": queue_id,
            "playQueueVersion": 1,
            "playQueueSelectedItemID": (metadata[0]["playQueueItemID"] if metadata else None),
            "playQueueSelectedItemOffset": 0,
            "playQueueSelectedMetadataItemID": (str(keys[0]) if keys else None),
            "playQueueShuffled": shuffled,
            "playQueueSourceURI": source,
            "playQueueTotalCount": len(metadata),
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
        }
        return self.container(**{k: v for k, v in fields.items() if v is not None})

    def decision(self, query):
        """`/video/:/transcode/universal/decision` -- always direct play.

        The codes are what plex-for-kodi's ServerDecision reads: it treats
        1000 <= mdeDecisionCode < 2000 as "server agrees".  The echoed
        Metadata/Media/Part carries `decision="directplay"` because the
        stricter clients (plx-native) read the verdict off `Part.decision`
        rather than the container codes.
        """
        path = query.get("path", "")
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        row = self.catalog.items.get(int(tail)) if tail.isdigit() else None

        rows = []
        if row is not None:
            item = _full(row)
            media = []
            for source in item.get("Media", []):
                copy = dict(source)
                copy["selected"] = True
                parts = []
                for part in copy.get("Part", []):
                    part_copy = dict(part)
                    part_copy["decision"] = "directplay"
                    part_copy["selected"] = True
                    parts.append(part_copy)
                copy["Part"] = parts
                media.append(copy)
            item["Media"] = media
            rows.append(item)

        logger.info("DECISION direct play for path=%s (%s)", path, "hit" if rows else "miss")

        return self.container(
            Metadata=rows,
            generalDecisionCode=1000,
            generalDecisionText="Direct play OK",
            directPlayDecisionCode=1000,
            directPlayDecisionText="Direct play OK",
            mdeDecisionCode=1000,
            mdeDecisionText="Direct play OK",
            allowSync=False,
            identifier="com.plexapp.plugins.library",
        )

    def timeline(self, query):
        """`/:/timeline` -- the player's progress report.

        A real PMS answers `{size, playbackState}`, not a bare container: the
        client reads `playbackState` back to reconcile what it just sent.
        """
        rating_key = query.get("ratingKey") or ""
        state = query.get("state") or "stopped"
        if rating_key.isdigit():
            offset = query.get("time")
            duration = query.get("duration")
            self.library.progress(
                int(rating_key),
                int(offset) if str(offset).isdigit() else None,
                state=state,
                duration=int(duration) if str(duration).isdigit() else None,
            )
            logger.info("TIMELINE %s state=%s t=%sms", rating_key, state, offset)
        return self.bare_container(size=0, playbackState=state)

    def set_watched(self, query, watched):
        """`/:/scrobble` and `/:/unscrobble` -- Mark as Played / Unplayed.

        Two things a container response gets wrong. A real PMS answers with an
        EMPTY 200, and marking a show or season has to walk down to the leaves:
        the client's own menu predicate reads `unviewedLeafCount` for those
        types, so a show whose episodes stayed unwatched keeps offering "Mark as
        Played" no matter what the show row says.
        """
        key = (query.get("key") or "").rsplit("/", 1)[-1]
        if key.isdigit():
            changed = self.library.scrobble(int(key), watched)
            logger.info("%s %s -> %d leaf row(s)",
                        "SCROBBLE" if watched else "UNSCROBBLE", key, len(changed))
        return ("empty", 200)

    def rate(self, query):
        """`/:/rate?key=&identifier=&rating=` -- the star rating. -1 clears."""
        key = (query.get("key") or "").rsplit("/", 1)[-1]
        rating = query.get("rating")
        if key.isdigit():
            try:
                value = float(rating)
            except (TypeError, ValueError):
                value = None
            self.library.rate(int(key), value)
            logger.info("RATE %s -> %s", key, rating)
        return ("empty", 200)

    def remove_from_continue_watching(self, query):
        """The context-menu entry of the same name: drop the resume point.

        Advertised as an Action on the `actions` provider feature; the client
        only draws the entry when that Action is present.
        """
        key = (query.get("ratingKey") or query.get("key") or "").rsplit("/", 1)[-1]
        if key.isdigit():
            self.library.remove_from_continue_watching(int(key))
        return ("empty", 200)

    def continue_watching(self):
        """Rows with a resume point, newest first -- the Continue Watching hub."""
        rows = [r for r in self.catalog.items.values()
                if r.get("viewOffset") and r.get("type") in ("movie", "episode")]
        return sorted(rows, key=lambda r: -(r.get("lastViewedAt") or 0))

    def select_stream(self, part_id, query):
        """`PUT /library/parts/{partID}?audioStreamID=&subtitleStreamID=`.

        Track selection from the player, and from Get Info. plexsim never
        repackages, so the choice only changes which Stream carries `selected` --
        which is what the client reads back.
        """
        wanted = {}
        for name, stream_type in (("audioStreamID", 2), ("subtitleStreamID", 3)):
            if name in query:
                wanted[stream_type] = query[name]
        if not wanted:
            return ("empty", 200)

        # `allParts=1` means every part of THIS item -- a multi-part movie keeps
        # one track choice across its parts. Applying it to every part in the
        # catalog would retrack the whole library from one dialog.
        owner = None
        for row in self.catalog.items.values():
            for media in row.get("Media", []):
                for part in media.get("Part", []):
                    if str(part.get("id")) == str(part_id):
                        owner = row
        if owner is None:
            return ("error", 404, "plexsim: no such part")
        for media in owner.get("Media", []):
            for part in media.get("Part", []):
                if str(part.get("id")) != str(part_id) and query.get("allParts") != "1":
                    continue
                for stream in part.get("Stream", []):
                    chosen = wanted.get(stream.get("streamType"))
                    if chosen is None:
                        continue
                    # `0` is the client's "None" pseudo-stream for subtitles, so
                    # it matches nothing and clears the selection.
                    if str(stream.get("id")) == str(chosen):
                        stream["selected"] = True
                    else:
                        stream.pop("selected", None)
        logger.info("STREAM SELECT part=%s item=%s %s", part_id, owner["ratingKey"], wanted)
        return ("empty", 200)

    # -- item actions ----------------------------------------------------

    def item_action(self, row, action, method, query):
        """The write half of the item context menu.

        Every one of these answers 200 with an EMPTY body on a real PMS
        (measured against 1.43.4). Returning a MediaContainer instead is not
        cosmetic: the client parses the response of `match` to decide whether
        the match took.
        """
        if method not in ("PUT", "POST"):
            return ("error", 405, "plexsim: method not allowed")

        if action == "refresh":
            self.library.refresh(row)
            # A real PMS pushes a TimelineEntry for the row it just refreshed;
            # without it the client keeps its stale poster until a reload.
            self.notify_timeline(row)
            logger.info("REFRESH %s (%s)", row["ratingKey"], row.get("title"))
        elif action == "analyze":
            # The catalog rows already carry ffprobe-derived Media attributes;
            # there is no second, better analysis to run.
            logger.info("ANALYZE %s -- catalog rows are already probe-derived",
                        row["ratingKey"])
        elif action == "unmatch":
            self.library.unmatch(row)
            logger.info("UNMATCH %s -> guid=%s", row["ratingKey"], row.get("guid"))
        elif action == "match":
            guid = query.get("guid") or ""
            if not guid:
                return ("error", 400, "plexsim: match needs a guid")
            self.library.apply_match(row, guid, query.get("name"), query.get("year"),
                                     identify=self._identify_guid)
            logger.info("MATCH %s -> %s", row["ratingKey"], guid)
        elif action == "split":
            created = self.library.split(row)
            logger.info("SPLIT %s -> %d new row(s)", row["ratingKey"], len(created))
        elif action == "merge":
            ids = [i for i in (query.get("ids") or "").split(",") if i]
            merged = self.library.merge(row, ids)
            logger.info("MERGE %s <- %s (%d row(s) absorbed)",
                        row["ratingKey"], ids, len(merged))
        else:
            return ("error", 404, "plexsim: unknown action")
        return ("empty", 200)

    def matches(self, row, query):
        """`GET /library/metadata/{id}/matches` -- Fix Match candidates.

        Shape measured on a real PMS: SearchResult rows of
        {guid, name, year, score, thumb, type, lifespanEnded, summary}. The
        guid each row carries is the one the client sends straight back to
        `PUT .../match`, so it has to be a guid plexsim can resolve again --
        hence the candidate map, which remembers which TMDB id minted it.
        """
        if self.tmdb is None:
            # No TMDB key: the honest answer is no candidates. The client says
            # "No matches were found using this agent", which is true.
            logger.info("MATCHES %s -- no TMDB key configured", row["ratingKey"])
            return self.tag_container(size=0)

        title = query.get("title") or row.get("title") or ""
        year = query.get("year") or row.get("year")
        # A show's episodes match against the SHOW, the way the agent does.
        kinds = ("tv",) if row.get("type") in ("show", "season", "episode") else ("movie", "tv")
        plex_kind = {"movie": "movie", "tv": "show"}
        results = []
        for kind in kinds:
            for found in self.tmdb.search(kind, title, year)[:20]:
                name = found.get("title") or found.get("name") or ""
                date = found.get("release_date") or found.get("first_air_date") or ""
                guid = metadata_module.plex_guid(plex_kind[kind], found.get("id"))
                with self.lock:
                    self.match_candidates[guid] = (kind, found.get("id"))
                entry = {
                    "guid": guid,
                    "name": name,
                    "type": plex_kind[kind],
                    "score": int(round(min(found.get("popularity") or 0, 100))),
                    "lifespanEnded": False,
                    "summary": found.get("overview") or "",
                }
                if date[:4].isdigit():
                    entry["year"] = int(date[:4])
                if found.get("poster_path"):
                    entry["thumb"] = metadata_module.IMAGE_BASE + "w342" + found["poster_path"]
                results.append(entry)
        logger.info("MATCHES %s %r -> %d candidate(s)", row["ratingKey"], title, len(results))
        return self.tag_container(size=len(results), SearchResult=results)

    def _identify_guid(self, row, guid):
        """Re-identify a row after a manual match.

        The guid came out of `matches()`, so it maps back to the TMDB id the
        user picked and the row is rebuilt from that -- not from a fresh search,
        which is the thing that got it wrong in the first place.
        """
        with self.lock:
            chosen = self.match_candidates.get(guid)
        if chosen is None or self.tmdb is None:
            return
        parsed = {"title": row.get("title") or "", "year": row.get("year"),
                  "season": row.get("parentIndex"), "episode": row.get("index")}
        try:
            metadata_module.enrich(row, self.tmdb, parsed, chosen=chosen)
        except Exception as error:  # a failed lookup must not fail the match
            logger.debug("re-identify failed for %s: %s", guid, error)

    def artwork_list(self, row, kind):
        """`/library/metadata/{id}/{posters,arts}` -- the Change Artwork picker.

        A real PMS lists every candidate the agent found, each with `selected`
        on the current one. plexsim has exactly what TMDB gave it plus its own
        generated placeholder, and says so rather than padding the list.
        """
        entries = []
        stored = row.get("_poster_url") if kind == "posters" else row.get("_art_url")
        current = row.get("thumb") if kind == "posters" else row.get("art")
        if stored:
            entries.append({
                "key": stored,
                "ratingKey": stored,
                "thumb": stored,
                "selected": bool(current),
                "provider": "tmdb",
            })
        placeholder = "/library/metadata/%s/%s/1" % (row["ratingKey"],
                                                     "thumb" if kind == "posters" else "art")
        entries.append({
            "key": placeholder,
            "ratingKey": placeholder,
            "thumb": placeholder,
            "selected": not current,
            "provider": "local",
        })
        # Measured: an artwork list renders as <Photo> in XML while its JSON
        # still says `Metadata`.
        return self.tag_container(size=len(entries), Metadata=entries,
                                  _xml={"Metadata": "Photo"})

    def set_artwork(self, row, kind, query):
        """`PUT /library/metadata/{id}/{posters,arts}?url=` -- pick one."""
        chosen = urllib.parse.unquote(query.get("url") or "")
        if not chosen:
            return ("error", 400, "plexsim: no url")
        field = "thumb" if kind == "posters" else "art"
        private = "_poster_url" if kind == "posters" else "_art_url"
        if chosen.startswith("http"):
            row[private] = chosen
            row[field] = "/library/metadata/%s/%s/%d" % (
                row["ratingKey"], field, int(time.time()))
        else:
            row.pop(private, None)
            row[field] = chosen
        logger.info("ARTWORK %s %s -> %s", row["ratingKey"], kind, chosen[:80])
        return ("empty", 200)

    def metadata_tree(self, row):
        """`/library/metadata/{id}/tree` -- the raw item/media/part tree.

        Measured shape: MetadataItem rows carrying MediaItem, each with
        MediaPart. It is what "Get Info" falls back to for an item whose
        Media the client has not already loaded.
        """
        media_items = []
        for media in row.get("Media", []):
            parts = []
            for part in media.get("Part", []):
                entry = {
                    "id": part.get("id"),
                    "file": part.get("file"),
                    "size": part.get("size"),
                    "duration": part.get("duration"),
                    "container": part.get("container"),
                }
                # A real MediaPart carries its streams here as MediaStream, with
                # `type` rather than `streamType`. Get Info reads this tree for
                # an item whose Media the client has not already loaded.
                streams = []
                for stream in part.get("Stream", []):
                    copy = {k: v for k, v in stream.items()
                            if k not in ("streamType", "selected", "default",
                                         "displayTitle", "extendedDisplayTitle")}
                    copy["type"] = stream.get("streamType")
                    streams.append(copy)
                if streams:
                    entry["MediaStream"] = streams
                parts.append(entry)
            media_items.append({
                "id": media.get("id"),
                "width": media.get("width"),
                "height": media.get("height"),
                "videoCodec": media.get("videoCodec"),
                "audioCodec": media.get("audioCodec"),
                "container": media.get("container"),
                "bitrate": media.get("bitrate"),
                "MediaPart": parts,
            })
        # `hash` and `openSubtitleHash` are on a real MediaPart here and are
        # deliberately absent: both are hashes OF THE FILE, and plexsim has no
        # file to hash.
        item = {
            "id": int(row["ratingKey"]),
            "title": row.get("title") or "",
            "guid": row.get("guid") or "",
            "parentTitle": row.get("parentTitle") or "",
            "instanceRatingKey": "",
            "index": row.get("index") or 1,
            "originallyAvailableAt": row.get("originallyAvailableAt") or "",
            "addedAt": row.get("addedAt") or 0,
            "refreshedAt": row.get("updatedAt") or 0,
            "MediaItem": media_items,
        }
        return self.tag_container(size=1, MetadataItem=[item])

    # -- collections -----------------------------------------------------

    def collections_route(self, method, segments, query):
        """`/library/collections` -- the collection half of the UI.

        A collection is a metadata item in its own right: it has a ratingKey, a
        context menu (Play, Play Next, Shuffle, Add to Playlist, Edit, Delete)
        and children. Its members also carry it as a `Collection` tag, which is
        the other way the UI writes one: an item edit with
        `collection[0].tag.tag=Name`.
        """
        if len(segments) == 2:
            if method == "POST":
                rows = self._items_for_uri(query.get("uri", ""))
                section = query.get("sectionId") or (
                    rows[0].get("librarySectionID") if rows else None)
                subtype = rows[0].get("type") if rows else "movie"
                collection = self.library.create_collection(
                    query.get("title") or "", section, rows, subtype=subtype,
                    smart=query.get("smart") == "1")
                logger.info("COLLECTION create %s (%d item(s))",
                            collection["title"], len(collection["items"]))
                return self.bare_container(
                    size=1, Metadata=[self.library._collection_row(collection)])
            # A real PMS 404s the bare GET: collections are listed per section.
            return ("error", 404, "plexsim: not found")

        collection = self.library.collection(segments[2])
        if collection is None:
            return ("error", 404, "plexsim: no such collection")

        if len(segments) == 3:
            if method == "DELETE":
                self.library.delete_collection(segments[2])
                return ("empty", 200)
            if method == "PUT":
                self.library.edit_collection(
                    collection, title=query.get("title.value") or query.get("title"),
                    summary=query.get("summary.value") or query.get("summary"))
                return ("empty", 200)
            section = self.catalog.section(collection["librarySectionID"]) or {}
            return self.bare_container(
                size=1,
                allowSync=False,
                librarySectionID=collection["librarySectionID"],
                librarySectionTitle=section.get("title"),
                librarySectionUUID=section.get("uuid"),
                Metadata=[self.library._collection_row(collection)],
            )

        leaf = segments[3]
        if leaf == "children":
            rows = [_listing_row(r) for r in self.library.collection_children(collection)]
            return self.bare_container(size=len(rows), Metadata=rows)
        if leaf == "items" and len(segments) == 6 and segments[5] == "move":
            if method != "PUT":
                return ("error", 405, "plexsim: method not allowed")
            if not self.library.move_collection_item(collection, segments[4],
                                                     query.get("after")):
                return ("error", 404, "plexsim: not in this collection")
            return ("empty", 200)
        if leaf == "items" and method == "PUT":
            rows = self._items_for_uri(query.get("uri", ""))
            added = self.library.add_collection_items(collection, rows)
            return self.bare_container(size=1, leafCountAdded=added,
                                       leafCountRequested=len(rows))
        if leaf == "composite":
            return ("png", query)
        return self.bare_container(size=0)

    # -- playlists -------------------------------------------------------

    def playlists_route(self, method, segments, query):
        """`/playlists` -- Add to Playlist, and the Playlists pivot.

        The client offers "Add to Playlist" purely because /media/providers
        advertises the `playlist` feature, so leaving these unimplemented shows
        the user a menu entry, a modal, and nothing happening.
        """
        if len(segments) == 1 or segments[1] in ("all", "sorts", "generators"):
            if len(segments) > 1 and segments[1] == "sorts":
                return self.bare_container(size=len(PLAYLIST_SORTS),
                                           Directory=PLAYLIST_SORTS)
            if len(segments) > 1 and segments[1] == "generators":
                # Optimized-version generators. plexsim has no encoder, so there
                # is nothing generating versions; a real PMS 404s this too.
                return ("error", 404, "plexsim: no version generators")
            if method == "POST":
                rows = self._items_for_uri(query.get("uri", ""))
                playlist = self.library.create_playlist(
                    query.get("title") or "", query.get("type") or "video", rows,
                    smart=query.get("smart") == "1")
                logger.info("PLAYLIST create %s (%d item(s))",
                            playlist["title"], len(playlist["items"]))
                return self.bare_container(
                    size=1, Metadata=[self.library._playlist_row(playlist)])
            rows = self.library.playlists(query.get("playlistType"))
            if query.get("smart") == "0":
                rows = [r for r in rows if not r.get("smart")]
            rows = self._sorted(rows, query.get("sort") or "")
            return self.paged_container(rows, query)

        playlist = self.library.playlist(segments[1])
        if playlist is None:
            return ("error", 404, "plexsim: no such playlist")

        if len(segments) == 2:
            if method == "DELETE":
                self.library.delete_playlist(segments[1])
                # Measured: a real PMS answers the playlist delete with 204,
                # not the 200 it uses for a collection delete.
                return ("empty", 204)
            if method == "PUT":
                self.library.edit_playlist(
                    playlist, title=query.get("title.value") or query.get("title"),
                    summary=query.get("summary.value") or query.get("summary"))
                return ("empty", 200)
            return self.bare_container(
                size=1, Metadata=[self.library._playlist_row(playlist)])

        leaf = segments[2]
        if leaf == "items":
            if len(segments) == 5 and segments[4] == "move" and method == "PUT":
                if not self.library.move_playlist_item(playlist, segments[3],
                                                       query.get("after")):
                    return ("error", 404, "plexsim: no such playlist item")
                return self.bare_container(
                    size=1, Metadata=[self.library._playlist_row(playlist)])
            if len(segments) == 4 and method == "DELETE":
                self.library.remove_playlist_item(playlist, segments[3])
                return self.bare_container(
                    size=1, Metadata=[self.library._playlist_row(playlist)])
            if method == "PUT":
                rows = self._items_for_uri(query.get("uri", ""))
                if not rows and query.get("playQueueID"):
                    keys = self.play_queues.get(int(query["playQueueID"]), [])
                    rows = [self.catalog.items[k] for k in keys if k in self.catalog.items]
                added = self.library.add_playlist_items(playlist, rows)
                logger.info("PLAYLIST %s += %d item(s)", playlist["ratingKey"], added)
                return self.bare_container(
                    size=1, leafCountAdded=added, leafCountRequested=len(rows),
                    Metadata=[self.library._playlist_row(playlist)])
            rows = []
            for row, item_id in self.library.playlist_items(playlist):
                entry = _listing_row(row)
                entry["playlistItemID"] = item_id
                rows.append(entry)
            summary = self.library._playlist_row(playlist)
            return self.paged_container(
                rows, query,
                composite=summary["composite"],
                duration=summary["duration"],
                leafCount=summary["leafCount"],
                playlistType=summary["playlistType"],
                ratingKey=summary["ratingKey"],
                smart=summary["smart"],
                title=summary["title"],
            )
        if leaf == "composite":
            return ("png", query)
        return self.bare_container(size=0)

    # -- play queue ------------------------------------------------------

    def play_queue_route(self, method, segments, query):
        """`/playQueues/{id}` -- Play Next and Add to Queue live here.

        The client builds both from the same call it uses to create a queue,
        with `next=1` for Play Next; without the PUT, both menu entries are
        drawn (the provider advertises `playqueue`) and neither does anything.
        """
        queue_id = int(segments[1]) if segments[1].isdigit() else 0
        if len(segments) == 4 and segments[2] == "items" and method == "DELETE":
            with self.lock:
                keys = self.play_queues.get(queue_id) or []
                index = int(segments[3]) - queue_id * 1000 - 1
                if 0 <= index < len(keys):
                    keys.pop(index)
            return self.play_queue_container(queue_id)
        if method == "PUT":
            rows = self._items_for_uri(query.get("uri", ""))
            with self.lock:
                keys = self.play_queues.setdefault(queue_id, [])
                new = [int(r["ratingKey"]) for r in rows]
                if query.get("next") == "1":
                    keys[1:1] = new
                else:
                    keys.extend(new)
            logger.info("PLAYQUEUE %s %s %d item(s)", queue_id,
                        "next" if query.get("next") == "1" else "append", len(rows))
            return self.play_queue_container(queue_id)
        if method == "DELETE":
            with self.lock:
                self.play_queues.pop(queue_id, None)
            return ("empty", 200)
        return self.play_queue_container(queue_id)

    # -- server settings -------------------------------------------------

    def server_prefs(self):
        """The setting list with any value the client has written applied."""
        if not self.pref_overrides:
            return SERVER_PREFS
        out = []
        for setting in SERVER_PREFS:
            if setting.get("id") in self.pref_overrides:
                setting = dict(setting, value=self.pref_overrides[setting["id"]])
            out.append(setting)
        return out

    def set_prefs(self, query):
        """`PUT /:/prefs?<id>=<value>` -- one or more settings at a time."""
        known = {s.get("id") for s in SERVER_PREFS}
        for name, value in query.items():
            if name.startswith("X-Plex-") or name not in known:
                continue
            self.pref_overrides[name] = value
            logger.info("PREF %s = %s", name, value)
        return ("empty", 200)

    def system_agents(self, query):
        """`/system/agents` -- the Agents (Legacy) page.

        Only the agents plexsim actually identifies with. A real PMS lists 17,
        most of them legacy plug-ins it still ships; claiming those would be
        advertising identification paths plexsim does not have.
        """
        wanted = query.get("mediaType")
        agents = [a for a in SYSTEM_AGENTS
                  if not wanted or str(a.get("mediaType")) == str(wanted)]
        agents = [{k: v for k, v in a.items() if k != "mediaType"} for a in agents]
        return self.bare_container(size=len(agents), Agent=agents)

    def system_scanners(self, section_id):
        """`/system/scanners/{sectionID}` -- what the Edit Library dialog lists."""
        section = self.catalog.section(section_id) or {}
        scanner = "Plex TV Series" if section.get("type") == "show" else "Plex Movie"
        return self.bare_container(
            size=1, noHistory=0, replaceParent=0,
            Directory=[{"key": scanner, "title": scanner}])

    def accounts(self, account_id):
        """`/accounts` -- the server's own user rows.

        Measured field set, and a bare container with only `identifier`: the
        generic library-container defaults do not belong here.
        """
        account = {
            "id": 1,
            "key": "/accounts/1",
            "name": self.owner_username,
            "defaultAudioLanguage": "en",
            "autoSelectAudio": True,
            "defaultSubtitleLanguage": "en",
            "subtitleMode": 2,
            "thumb": "",
        }
        rows = [account] if account_id in (None, "1") else []
        return self.bare_container(
            size=len(rows), identifier="com.plexapp.system.accounts", Account=rows)

    def devices(self):
        """`/devices` -- what the Manage > Devices list reads."""
        device = {
            "id": 1,
            "name": self.catalog.friendly,
            "platform": "Linux",
            "clientIdentifier": self.catalog.machine,
            "createdAt": 0,
        }
        return self.bare_container(
            size=1, identifier="com.plexapp.system.devices", Device=[device])

    def statistics(self, which):
        """`/statistics/{bandwidth,media,resources}` -- the Dashboard graphs.

        The Device and Account rows are real; the per-interval series is empty
        because plexsim keeps no history. An invented series would draw a
        plausible graph of traffic that never happened.
        """
        if which not in ("bandwidth", "media", "resources"):
            return ("error", 404, "plexsim: not found")
        # Measured on a production PMS with no rows for the window: the answer
        # is a container carrying nothing but `size`. The Device and Account
        # blocks only appear alongside an actual series, so emitting them with
        # an empty series would be a shape no real server sends.
        return self.bare_container(size=0)

    # Scheduled tasks. A real PMS lists 23; plexsim lists the ones it can
    # actually perform, because the page's Run button is a promise. The root
    # element is `ButlerTasks`, not MediaContainer -- one of the few endpoints
    # on the whole surface that is not a MediaContainer, which is exactly the
    # kind of thing that cannot be guessed.
    BUTLER_TASKS = [
        {"name": "RefreshLibraries", "interval": 3, "scheduleRandomized": False,
         "enabled": True, "title": "Update all libraries during maintenance",
         "description": "Re-read every row in every library"},
        {"name": "CleanOldBundles", "interval": 7, "scheduleRandomized": False,
         "enabled": True, "title": "Remove old bundles",
         "description": "Remove bundles no longer in use"},
    ]

    def butler(self, method, segments):
        """`/butler` and `/butler/{task}` -- the Scheduled Tasks page's Run button."""
        if len(segments) == 1:
            return {"ButlerTasks": {"size": len(self.BUTLER_TASKS),
                                    "ButlerTask": self.BUTLER_TASKS}}
        name = segments[1]
        if name not in {task["name"] for task in self.BUTLER_TASKS}:
            return ("error", 404, "plexsim: no such task")
        if method == "DELETE":
            # Cancelling a queued task.
            return ("empty", 200)
        if method != "POST":
            return ("error", 405, "plexsim: method not allowed")
        if name == "RefreshLibraries":
            for section in self.catalog.sections:
                for row in self.catalog.section_items(section["key"]):
                    self.library.add_activity(row.get("title") or "")
            # The message a real PMS pushes when this task finishes, verbatim.
            self.notify_progress("The butler refreshed all libraries.")
        logger.info("BUTLER ran %s", name)
        return ("empty", 200)

    def updater_status(self):
        """`/updater/status` -- plexsim never has an update to install."""
        return self.bare_container(size=0, canInstall=False,
                                   checkedAt=int(time.time()), status=0)

    def history(self, query):
        """`/status/sessions/history/all` -- the View Play History page.

        Rows are recorded as playback happens (library.record_view), with the
        field set measured on a production PMS: an episode carries
        key/ratingKey/parentKey/grandparent*/index/parentIndex, a movie carries
        title and originallyAvailableAt.
        """
        rows = self.library.history()
        start = int(query.get("X-Plex-Container-Start", 0) or 0)
        size = int(query.get("X-Plex-Container-Size", len(rows)) or len(rows))
        page = rows[start:start + size]
        fields = {"size": len(page), "Metadata": page}
        if "X-Plex-Container-Start" in query or "X-Plex-Container-Size" in query:
            fields["totalSize"] = len(rows)
            fields["offset"] = start
        return self.bare_container(**fields)

    def log_archive(self):
        """A zip of plexsim's own recent log, for Download Logs.

        The button exists because /media/providers advertises `diagnostics`
        containing "logs". plexsim keeps the lines it logged in this process
        rather than reading a file it does not own.
        """
        buffer = io.BytesIO()
        with self.lock:
            lines = list(self.log_lines)
            requests = ["%s %s" % (m, p) for m, p in self.requests[-2000:]]
            unknown = list(self.unknown)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("plexsim.log", "\n".join(lines) + "\n")
            archive.writestr("requests.log", "\n".join(requests) + "\n")
            archive.writestr("unrouted.log", "\n".join(unknown) + "\n")
        return buffer.getvalue()

    def create_section(self, query):
        """`POST /library/sections` -- Add Library.

        plexsim has no filesystem to scan, so the new section starts empty --
        which is also what a real PMS gives you for an empty folder, rather than
        an error. 201 with the created Directory is the measured answer.
        """
        title = query.get("name") or query.get("title") or "New Library"
        section_type = query.get("type") or "movie"
        seed = "%s-%s" % (title, section_type)
        section = self.catalog.add_section(
            title, section_type, "%032x" % (zlib.crc32(seed.encode()) & 0xFFFFFFFF))
        logger.info("SECTION create %s (%s)", title, section_type)
        return ("created", self.container(Directory=[dict(
            section,
            agent=("tv.plex.agents.series" if section_type == "show"
                   else "tv.plex.agents.movie"),
            scanner=("Plex TV Series" if section_type == "show" else "Plex Movie"),
            language=query.get("language") or "en-US",
            refreshing=False,
            content=True,
            directory=True,
            contentChangedAt=0,
            hidden=0,
            Location=[{"id": 1, "path": query.get("location") or "/plexsim"}],
        )]))

    # -- the stream ------------------------------------------------------

    def artwork_for(self, rating_key, kind):
        """Absolute image URL for a row's poster/background, if identified."""
        row = self.catalog.items.get(int(rating_key)) if str(rating_key).isdigit() else None
        if not row:
            return None
        if kind in ("thumb", "poster"):
            return row.get("_poster_url")
        return row.get("_art_url") or row.get("_poster_url")

    def resolve_artwork(self, url_param):
        """Map a `/photo/:/transcode?url=...` value to a real image URL."""
        if not url_param:
            return None
        target = urllib.parse.unquote(url_param)
        # Already absolute (a person thumb, or art we handed out earlier).
        if target.startswith("http://") or target.startswith("https://"):
            return target
        match = re.match(r"/library/metadata/(\d+)/(thumb|art|poster|background)", target)
        if match:
            return self.artwork_for(match.group(1), match.group(2))
        return None

    def resolve_part(self, part_id):
        """part id -> (restricted debrid link, container, file name).

        The file name is for the Download entry's `Content-Disposition`: a real
        PMS names the saved file after the media file, not after the URL's last
        segment (measured: `filename="Firefly (2002) - s01e01 - 1080p.mkv"`).
        """
        for row in self.catalog.items.values():
            for media in row.get("Media", []):
                for part in media.get("Part", []):
                    if str(part.get("id")) == str(part_id):
                        name = str(part.get("file") or "").rsplit("/", 1)[-1]
                        return row.get("_debrid"), part.get("container"), name
        return None, None, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PlexSim/0.1"

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt, *args):
        if self.server.verbose:
            logger.debug("%s", fmt % args)

    def _wants_xml(self):
        """Plex clients pick the format with Accept; TV and mobile want XML.

        pd_inject discovered this the hard way and hardcoded XML for mobile.
        Default is XML because that is what a real PMS returns without an
        explicit JSON Accept.
        """
        accept = (self.headers.get("Accept") or "").lower()
        if "application/json" in accept:
            return False
        if "xml" in accept:
            return True
        return True

    def _send(self, status, content_type, payload, extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Plex-Protocol", "1.0")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_container(self, container):
        # `_xml` names the element a container's rows render as where the rows
        # cannot name it themselves (an artwork list is <Photo>, a folder list
        # <Directory>). It is internal: it must never reach the JSON body.
        body_dict = next(iter(container.values()))
        elements = body_dict.pop("_xml", None) if isinstance(body_dict, dict) else None
        if self._wants_xml():
            self._send(200, "text/xml;charset=utf-8", to_xml(container, elements))
        else:
            self._send(200, "application/json", json.dumps(container).encode())

    # -- verbs -----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,HEAD,OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "X-Plex-Token,X-Plex-Client-Identifier,X-Plex-Product,X-Plex-Version,"
            "X-Plex-Platform,X-Plex-Device,X-Plex-Device-Name,X-Plex-Session-Identifier,"
            "Accept,Content-Type,Range",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method):
        sim = self.server.sim
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        try:
            result = sim.handle(method, self.path, body, self.headers)
        except Exception:
            logger.exception("handler failed for %s %s", method, self.path)
            self._send(500, "text/plain", b"plexsim: internal error")
            return

        with sim.lock:
            sim.requests.append((method, self.path))

        if isinstance(result, tuple):
            kind = result[0]
            if kind == "stream":
                self._serve_part(result[1], result[2], method)
                return
            if kind == "image":
                self._serve_image(result[1])
                return
            if kind == "png":
                query = result[1]
                payload = _png(
                    int(query.get("width", 342) or 342),
                    int(query.get("height", 513) or 513),
                    (32, 34, 38),
                )
                self._send(200, "image/png", payload, [("Cache-Control", "max-age=86400")])
                return
            if kind == "websocket":
                self._serve_websocket()
                return
            if kind == "redirect":
                self.send_response(302)
                self.send_header("Location", result[1])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if kind == "static":
                self._serve_static(result[1])
                return
            if kind == "sse":
                self._serve_eventsource()
                return
            if kind == "error":
                self._send(result[1], "text/plain", result[2].encode())
                return
            if kind == "empty":
                # Every mutating endpoint on a real PMS answers with an empty
                # body and a text/html content type -- measured against 1.43.4
                # for refresh, analyze, unmatch, match, split, merge, scrobble,
                # rate, the prefs write and the section tasks. A MediaContainer
                # here is a different shape from the one the client parses.
                self._send(result[1], "text/html", b"")
                return
            if kind == "created":
                # POST /library/sections answers 201 with the created Directory.
                payload = json.dumps(result[1]).encode()
                if self._wants_xml():
                    self._send(201, "text/xml;charset=utf-8", to_xml(result[1]))
                else:
                    self._send(201, "application/json", payload)
                return
            if kind == "zip":
                self._send(200, "application/zip", result[1],
                           [("Content-Disposition",
                             "attachment; filename=\"plexsim-logs.zip\"")])
                return

        self._send_container(result)

    STATIC_TYPES = {
        ".html": "text/html;charset=utf-8",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".gif": "image/gif",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".eot": "application/vnd.ms-fontobject",
        ".map": "application/json",
        ".txt": "text/plain",
        ".webmanifest": "application/manifest+json",
    }

    _image_cache = {}
    _image_lock = threading.Lock()

    def _serve_image(self, url):
        """Fetch and return a remote image, cached in memory.

        Artwork is requested once per tile per client, so without a cache a
        grid scroll becomes a burst of identical upstream fetches.
        """
        with self._image_lock:
            hit = self._image_cache.get(url)
        logger.info("ARTWORK %s %s", "cache" if hit else "fetch", url[:110])
        if hit is None:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": UPSTREAM_UA})
                with urllib.request.urlopen(request, timeout=20) as response:
                    hit = (response.read(), response.headers.get("Content-Type", "image/jpeg"))
            except Exception as error:
                logger.debug("artwork fetch failed for %s: %s", url[:80], error)
                self._send(404, "text/plain", b"plexsim: artwork unavailable")
                return
            with self._image_lock:
                # Bounded: a catalog of any size would otherwise grow this
                # without limit for the life of the process.
                if len(self._image_cache) > 512:
                    self._image_cache.clear()
                self._image_cache[url] = hit
        payload, content_type = hit
        self._send(200, content_type, payload, [("Cache-Control", "max-age=86400")])

    def _serve_static(self, relative):
        """Serve the bundled web client from --web-root."""
        root = self.server.sim.web_root
        if not root:
            self._send(404, "text/plain", b"plexsim: no web client bundled")
            return

        # Resolve and confine: a path that escapes the root is refused rather
        # than served, so a traversal cannot read the rest of the filesystem.
        target = os.path.realpath(os.path.join(root, relative))
        if not target.startswith(os.path.realpath(root) + os.sep):
            self._send(403, "text/plain", b"plexsim: outside the web root")
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            self._send(404, "text/plain", b"plexsim: not found")
            return

        with open(target, "rb") as handle:
            payload = handle.read()
        extension = os.path.splitext(target)[1].lower()
        self._send(
            200,
            self.STATIC_TYPES.get(extension, "application/octet-stream"),
            payload,
            [("Cache-Control", "max-age=3600")],
        )

    # RFC6455's fixed handshake GUID.
    WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    @staticmethod
    def _ws_frame(payload):
        """One unmasked text frame. Server frames are never masked (RFC6455)."""
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        return bytes(header) + payload

    def _serve_websocket(self):
        """Complete the RFC6455 handshake and push events down it.

        It needs a genuine 101 upgrade, because the web client reads a failed
        upgrade as a broken server. And it needs to carry real frames: a real
        PMS pushes `timeline`, `activity`, `status` and `progress` down this
        channel, and a client that receives none of them falls back to polling
        and never sees a change made anywhere else.
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, "text/plain", b"plexsim: not a websocket request")
            return

        import base64
        import hashlib

        accept = base64.b64encode(
            hashlib.sha1((key + self.WS_GUID).encode()).digest()
        ).decode()

        self.close_connection = True
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
            ).encode()
        )
        self.wfile.flush()
        logger.info("websocket event channel opened")

        sim = self.server.sim
        lock = threading.Lock()

        def sink(kind, element, rows):
            payload = json.dumps({"NotificationContainer": {
                "type": kind, "size": len(rows), element: rows}}).encode()
            # One writer at a time: two notifications interleaving mid-frame
            # would corrupt the stream for the life of the connection.
            with lock:
                self.wfile.write(self._ws_frame(payload))
                self.wfile.flush()

        sim.add_socket(sink)
        # Keep it alive with server-side pings. A silent socket is eventually
        # reaped by proxies, and Cloudflare sits in front of this one.
        deadline = time.time() + 3600
        try:
            while time.time() < deadline:
                time.sleep(20)
                with lock:
                    # opcode 0x9 = ping, FIN set, no payload.
                    self.wfile.write(struct.pack("!BB", 0x89, 0x00))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("websocket event channel closed by client")
        finally:
            sim.drop_socket(sink)

    def _serve_eventsource(self):
        """Server-Sent Events channel the web app subscribes to on connect.

        Held open with periodic comment frames: closing it immediately makes the
        client reconnect in a tight loop. It carries the same events as the
        websocket, in the SSE shape measured on the reference -- `event: <type>`
        and a `data:` line holding ONE row under its element name, with no
        NotificationContainer and no size around it.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        sim = self.server.sim
        lock = threading.Lock()

        def sink(kind, element, rows):
            frames = "".join(
                "event: %s\ndata: %s\n\n" % (kind, json.dumps({element: row}))
                for row in rows)
            with lock:
                self.wfile.write(frames.encode())
                self.wfile.flush()

        sim.add_socket(sink)
        deadline = time.time() + 300
        try:
            while time.time() < deadline:
                with lock:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sim.drop_socket(sink)

    # -- part streaming --------------------------------------------------

    def _serve_part(self, segments, query, method):
        """`/library/parts/{id}/{ts}/{file}` -- the media bytes.

        This is the whole experiment.  A real PMS answers a Range GET here with
        206, Accept-Ranges and a true total size, and a client seeks by issuing
        more Ranges; plexsim has to reproduce that against a debrid CDN it does
        not own.
        """
        sim = self.server.sim
        part_id = segments[2] if len(segments) > 2 else ""
        link, container, part_name = sim.resolve_part(part_id)
        if not link:
            self._send(404, "text/plain", b"plexsim: no such part")
            return

        # The Download context-menu entry is the same Part read with
        # `download=1`; the only difference a real PMS makes is the disposition
        # header, which is what turns a play into a save.
        filename = part_name or (segments[-1] if len(segments) > 3 else "part-%s" % part_id)
        download = query.get("download") in ("1", "true")

        try:
            upstream_url, _size = sim.resolver.resolve(link)
        except Exception as error:
            logger.error("unrestrict failed for part %s: %s", part_id, error)
            self._send(502, "text/plain", b"plexsim: debrid resolve failed")
            return

        if sim.part_mode == "redirect":
            logger.info("PART %s -> 302 CDN%s", part_id, " (download)" if download else "")
            self.send_response(302)
            self.send_header("Location", upstream_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._proxy(upstream_url, container, part_id, method,
                    download=filename if download else None)

    def _proxy(self, upstream_url, container, part_id, method, download=None):
        request_range = self.headers.get("Range")
        headers = {"User-Agent": UPSTREAM_UA}
        if request_range:
            headers["Range"] = request_range

        request = urllib.request.Request(upstream_url, headers=headers, method="GET")
        try:
            upstream = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            logger.error("PART %s upstream %s for range %s", part_id, error.code, request_range)
            self._send(error.code, "text/plain", b"plexsim: upstream refused")
            return
        except Exception as error:
            logger.error("PART %s upstream failed: %s", part_id, error)
            self._send(502, "text/plain", b"plexsim: upstream failed")
            return

        with upstream:
            status = upstream.status
            content_length = upstream.headers.get("Content-Length")
            content_range = upstream.headers.get("Content-Range")
            logger.info(
                "PART %s range=%s -> %s len=%s",
                part_id,
                request_range or "-",
                status,
                content_length,
            )

            self.send_response(status)
            # RD's CDN answers `application/force-download`, which is not a
            # media type; a client that sniffs it may refuse the stream, so the
            # container's real type is substituted here.
            self.send_header("Content-Type", CONTENT_TYPES.get(container, "video/mp4"))
            self.send_header("Accept-Ranges", "bytes")
            if download:
                # Both forms, the way a real PMS sends them: the plain one for
                # old clients and the RFC 5987 one for names that are not ASCII.
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                    % (download.replace('"', ""), urllib.parse.quote(download)))
            if content_length:
                self.send_header("Content-Length", content_length)
            if content_range:
                self.send_header("Content-Range", content_range)
            self.send_header("Access-Control-Allow-Origin", "*")
            # A media read must NOT keep the connection alive.  ffmpeg (so mpv,
            # and every client that embeds it) seeks by issuing an open-ended
            # `Range: bytes=N-`, reading a little, and then sending its next
            # request on the SAME connection.  Under keep-alive that request
            # lands behind the gigabytes of body still queued from the last
            # one, so ffmpeg reads media payload where it expects a status line
            # -- "Error reading HTTP response: End of file", then it gives up on
            # seeking entirely and reports the stream as unseekable
            # (AVERROR(ENOSYS) = -78).  Every CDN answers a media range with
            # `Connection: close` for exactly this reason, RD's included.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()

            if method == "HEAD":
                return

            try:
                while True:
                    chunk = upstream.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # A seek or a stop closes the socket mid-body. Normal.
                logger.debug("PART %s client closed", part_id)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, sim, verbose):
        self.sim = sim
        self.verbose = verbose
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="plexsim -- a PMS that owns no files")
    parser.add_argument("--catalog", default=os.path.join(os.path.dirname(__file__), "catalog.json"))
    parser.add_argument("--port", type=int, default=32400)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--part-mode",
        choices=("proxy", "redirect"),
        default="proxy",
        help="proxy streams bytes through plexsim; redirect 302s to the debrid CDN",
    )
    parser.add_argument("--public-url", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--assert-claimed",
        action="store_true",
        help="report claimed=1 without performing a real plex.tv claim exchange",
    )
    parser.add_argument(
        "--web-root",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "webclient"),
        help="directory of the extracted Plex Web client, served at /web",
    )
    parser.add_argument(
        "--owner-email",
        default="plexadmin@debridmediamanager.com",
        help="owner email, which is what a real PMS reports as myPlexUsername",
    )
    parser.add_argument(
        "--owner-username",
        default="plexa630",
        help="the Plex account that OWNS this server; shared users are not owners",
    )
    # Registration is opt-in: passing --announce-url WRITES to a plex.tv account.
    parser.add_argument(
        "--announce-url",
        default=None,
        help="publicly reachable https URL to register with plex.tv, and keep registered",
    )
    parser.add_argument(
        "--device-id",
        default=None,
        help="plex.tv NUMERIC device id to announce on (not a machineIdentifier)",
    )
    parser.add_argument("--heartbeat-interval", type=int, default=heartbeat_module.DEFAULT_INTERVAL)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    token = os.environ.get("RD_TOKEN")
    if not token:
        parser.error("RD_TOKEN must be set (the Real-Debrid API key)")

    catalog = Catalog(args.catalog)
    # Fix Match needs a live agent. The same key build_catalog.py uses; without
    # it the match dialog reports no candidates, which is the truth.
    tmdb = None
    tmdb_key = os.environ.get("TMDB_KEY")
    if tmdb_key:
        tmdb = metadata_module.Tmdb(
            tmdb_key,
            os.path.join(os.path.dirname(os.path.abspath(args.catalog)), "tmdb_cache.json"),
        )
    else:
        logger.info("no TMDB_KEY: Fix Match will report no candidates")
    claim = claim_module.Claim(
        catalog.machine,
        os.path.join(os.path.dirname(os.path.abspath(args.catalog)), "claim_state.json"),
        name=catalog.friendly,
        version=SERVER_VERSION,
    )
    sim = PlexSim(
        catalog,
        Resolver(token),
        part_mode=args.part_mode,
        public_url=args.public_url,
        owner_username=args.owner_username,
        claim=claim,
        web_root=args.web_root if os.path.isdir(args.web_root) else None,
        assert_claimed=args.assert_claimed,
        owner_email=args.owner_email,
        tmdb=tmdb,
    )

    # Keep the log in memory as well as on stderr, so Download Logs on the
    # Troubleshooting page has something real to hand over. The format is the
    # one the Console page's parser expects:
    # `MMM dd, yyyy HH:mm:ss.SSS [thread] LEVEL - message`.
    class _Capture(logging.Handler):
        def emit(self, record):
            try:
                line = "%s [%s] %s - %s" % (
                    time.strftime("%b %d, %Y %H:%M:%S", time.localtime(record.created))
                    + ".%03d" % (record.msecs,),
                    record.threadName,
                    record.levelname,
                    record.getMessage(),
                )
            except Exception:
                return
            with sim.lock:
                sim.log_lines.append(line)
                if len(sim.log_lines) > 5000:
                    del sim.log_lines[:1000]

    logger.addHandler(_Capture())

    server = Server((args.host, args.port), sim, args.verbose)
    logger.info("claim state: %s", "CLAIMED by %s" % claim.owner if claim.claimed else "unclaimed")
    logger.info(
        "plexsim on http://%s:%d  machine=%s  items=%d  parts=%s",
        socket.gethostname(),
        args.port,
        catalog.machine,
        len(catalog.items),
        args.part_mode,
    )

    if args.announce_url and args.part_mode == "proxy":
        # Measured behind a Cloudflare tunnel: a `Range: bytes=0-99` arrives here
        # with NO Range header at all, because Cloudflare collapses a prefix
        # range into a full origin fetch. plexsim then serves the whole file and
        # the CDN pulls all of it -- so every playback start drains the entire
        # title out of the debrid provider. Loud, because the cost is invisible
        # until the bandwidth bill.
        logger.warning(
            "part-mode=proxy behind a public URL: a CDN may strip Range and turn "
            "every play start into a FULL-FILE fetch from the provider. "
            "Use --part-mode redirect unless you have verified Range passthrough."
        )

    beat = None
    if args.announce_url:
        plex_token = os.environ.get("PLEX_TOKEN")
        if not plex_token:
            parser.error("--announce-url needs PLEX_TOKEN in the environment")
        if not args.device_id:
            parser.error(
                "--announce-url needs --device-id (plex.tv's numeric id). "
                "Look it up with heartbeat.resolve_device_id(); a machineIdentifier "
                "in that position silently overwrites a DIFFERENT device."
            )
        beat = heartbeat_module.Heartbeat(
            plex_token,
            args.device_id,
            args.announce_url,
            name=catalog.friendly,
            interval=args.heartbeat_interval,
        )
        beat.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("stopping")
    finally:
        if beat is not None:
            beat.stop()


if __name__ == "__main__":
    main()
