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
import json
import logging
import os
import re
import socket
import struct
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim as claim_module  # noqa: E402
import heartbeat as heartbeat_module  # noqa: E402
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

_SECTION_META_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "section_meta.json")
try:
    with open(_SECTION_META_PATH) as _handle:
        SECTION_META = json.load(_handle)
except (OSError, ValueError):
    SECTION_META = None

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
                 assert_claimed=False, owner_email=None):
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
        self.next_play_queue = 1
        self.sessions = {}

    # -- helpers ---------------------------------------------------------

    def bare_container(self, **fields):
        """A MediaContainer with no library-container defaults.

        /media/providers on a real PMS has no `identifier`, `mediaTagPrefix`,
        `mediaTagVersion` or `allowSync`, and no `claimed` either -- claim state
        lives on /identity and /myplex/account. Sending extras is not harmless:
        it is how plexsim ended up asserting `claimed` in a place a real server
        never does.
        """
        return {"MediaContainer": dict(fields)}

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
        return {"MediaContainer": body}

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

    def section_meta(self, section_id, section_type):
        """Per-section filter/sort descriptors, retargeted from the template."""
        if not SECTION_META:
            return None
        meta = json.loads(json.dumps(SECTION_META))  # deep copy per request
        plex_type = 2 if section_type == "show" else 1
        for entry in meta.get("Type") or []:
            entry["key"] = "/library/sections/%s/all?type=%d" % (section_id, plex_type)
            entry["type"] = section_type
            entry["title"] = "TV Shows" if section_type == "show" else "Movies"
            for sort in entry.get("Sort") or []:
                if sort.get("firstCharacterKey"):
                    sort["firstCharacterKey"] = "/library/sections/%s/firstCharacter" % section_id
        return meta

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
        # sentinel the handler turns into a body.
        if route.startswith("/library/parts/"):
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

        if route in ("/:/prefs", "/:/prefs/"):
            return self.bare_container(size=len(SERVER_PREFS), Setting=SERVER_PREFS)

        if route.startswith("/system/agents"):
            return self.container(Agent=[])

        if route.startswith("/accounts"):
            return self.container(
                Account=[{"id": 1, "key": "/accounts/1", "name": catalog.friendly}]
            )

        # -- library -----------------------------------------------------
        if route == "/library/sections":
            return self.container(
                Directory=[
                    dict(
                        section,
                        agent=("tv.plex.agents.series" if section.get("type") == "show"
                               else "tv.plex.agents.movie"),
                        scanner=("Plex TV Series" if section.get("type") == "show"
                                 else "Plex Movie"),
                        language="en-US",
                        refreshing=False,
                        allowSync=False,
                        content=True,
                        directory=True,
                        hidden=0,
                        composite="/library/sections/%s/composite/1" % section["key"],
                        Location=[{"id": 1, "path": "/plexsim/%s" % section["key"]}],
                    )
                    for section in catalog.sections
                ]
            )

        if segments[:2] == ["library", "sections"] and len(segments) >= 3:
            return self.library_section(segments, query)

        if segments[:2] == ["library", "metadata"] and len(segments) >= 3:
            return self.library_metadata(segments, query)

        if route in ("/library/onDeck", "/library/recentlyAdded"):
            rows = [_listing_row(r) for r in list(catalog.items.values())[:12]]
            return self.container(Metadata=rows)

        if route == "/library/all":
            rows = [_listing_row(r) for r in catalog.items.values()]
            return self.container(Metadata=rows)

        if segments[:2] == ["library", "streams"]:
            # Sidecar subtitle extraction. plexsim never repackages, so every
            # subtitle stays embedded in the part and there is nothing to serve.
            return self.container()

        # -- hubs --------------------------------------------------------
        if route == "/hubs":
            return self.hubs(query)
        if route == "/hubs/continueWatching":
            return self.container(Hub=[self.hub("home.continue", "Continue Watching", [])])
        if route == "/hubs/promoted":
            return self.hubs(query)
        if segments[:2] == ["hubs", "sections"] and len(segments) == 3:
            return self.section_hubs(segments[2])
        if route == "/hubs/search":
            return self.search(query)
        if segments[:2] == ["hubs", "metadata"]:
            return self.container(Hub=[])

        # -- playback ----------------------------------------------------
        if route == "/playQueues" and method == "POST":
            return self.create_play_queue(query)
        if segments[:1] == ["playQueues"] and len(segments) == 2:
            return self.get_play_queue(segments[1])

        if route == "/video/:/transcode/universal/decision":
            return self.decision(query)
        if route.startswith("/video/:/transcode/universal/start"):
            # plexsim has no encoder. A 404 here is the honest answer and it is
            # also the signal that the client's own gate rejected direct play.
            logger.warning("TRANSCODE REQUESTED -- client refused direct play: %s", query.get("path"))
            return ("error", 404, "plexsim: no transcoder")
        if route.startswith("/video/:/transcode/universal/"):
            return self.container()
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
            return self.container()
        if route.startswith("/status/sessions"):
            return self.container(Metadata=[])
        if route == "/actions/removeFromContinueWatching":
            return self.container()

        with self.lock:
            self.unknown.append("%s %s" % (method, route))
        logger.warning("UNKNOWN %s %s", method, route)
        return self.container()

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
                    "id": int(key),
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
                    ],
                }
            )

        features = [
            {"key": "/library/sections", "type": "content", "Directory": directories},
            {"key": "/hubs/search", "type": "search"},
            {"key": "/library/metadata", "type": "metadata"},
            {"key": "/hubs/promoted", "type": "promoted"},
            {"key": "/hubs/continueWatching", "type": "continuewatching"},
            {"key": "/photo/:/transcode", "type": "imagetranscoder"},
            {"flavor": "universal", "key": "/playQueues", "type": "playqueue"},
            {
                "key": "/:/timeline",
                "type": "timeline",
                "scrobbleKey": "/:/scrobble",
                "unscrobbleKey": "/:/unscrobble",
            },
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
            {"key": "/library/matches", "type": "match"},
            {"type": "queryParser"},
            # Feature types a real PMS advertises that plexsim was missing. A
            # client will not offer the corresponding UI without them, and the
            # web app re-requested /media/providers 43 times while they were
            # absent -- it treats an incomplete provider as not-yet-ready.
            {"key": "/library/collections", "type": "collection"},
            {"flavor": "universal", "key": "/playlists", "type": "playlist"},
            {"key": "/:/rate", "type": "rate"},
            {"type": "manage"},
            {"key": "/library/search", "type": "universalsearch"},
            {"flavor": "download", "type": "subscribe"},
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
            pushNotifications=False,
            allowCameraUpload=False,
            allowChannelAccess=True,
            allowMediaDeletion=False,
            allowSharing=True,
            allowTuners=False,
            livetv=0,
            offlineTranscode=0,
            streamingBrainVersion=2,
            streamingBrainABRVersion=3,
            diagnostics="logs,databases,streaminglogs",
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
                    "protocols": "stream",
                    "Feature": features,
                }
            ],
        )

    def library_section(self, segments, query):
        section_id = segments[2]
        if len(segments) == 3:
            return self.container(
                Directory=[
                    {"key": "all", "title": "All"},
                    {"key": "recentlyAdded", "title": "Recently Added"},
                ]
            )

        leaf = segments[3]
        section = self.catalog.section(section_id) or {}
        if leaf == "all":
            # Plex's numeric type filter. Ignoring it meant a client asking a
            # show section for EPISODES (type=4) was handed shows instead.
            wanted = {"1": "movie", "2": "show", "3": "season", "4": "episode"}.get(
                query.get("type"))
            if wanted in ("season", "episode"):
                source = [r for r in self.catalog.items.values()
                          if r.get("type") == wanted
                          and str(r.get("librarySectionID")) == str(section_id)]
                source.sort(key=lambda r: (r.get("grandparentTitle") or r.get("parentTitle") or "",
                                           r.get("parentIndex") or 0, r.get("index") or 0))
            else:
                source = self.catalog.section_items(section_id)
            rows = [_listing_row(r) for r in source]
            start = int(query.get("X-Plex-Container-Start", 0) or 0)
            size = int(query.get("X-Plex-Container-Size", len(rows)) or len(rows))
            page = rows[start : start + size]
            extra = {
                "totalSize": len(rows),
                "offset": start,
                "librarySectionID": int(section_id),
                "librarySectionTitle": section.get("title"),
                "librarySectionUUID": section.get("uuid"),
                "title1": section.get("title"),
                "title2": "All " + str(section.get("title") or ""),
                "viewGroup": section.get("type"),
                # Matched against a real PMS movie section; the web client's
                # grid reads these alongside Meta.
                "viewMode": 65592,
                "art": "/:/resources/show-fanart.jpg" if section.get("type") == "show"
                       else "/:/resources/movie-fanart.jpg",
                "thumb": "/:/resources/show.png" if section.get("type") == "show"
                         else "/:/resources/movie.png",
                "allowSync": False,
                "nocache": True,
            }
            if query.get("includeMeta") == "1":
                meta = self.section_meta(section_id, section.get("type") or "movie")
                if meta:
                    extra["Meta"] = meta
            return self.container(Metadata=page, **extra)

        if leaf == "recentlyAdded":
            rows = [_listing_row(r) for r in self.catalog.section_items(section_id)]
            return self.container(Metadata=rows[:12])

        if leaf == "sorts":
            return self.container(
                Sort=[
                    {"key": "titleSort", "title": "Title", "default": "asc"},
                    {"key": "addedAt", "title": "Date Added", "defaultDirection": "desc"},
                ]
            )
        if leaf == "filters":
            return self.container(Filter=[])
        if leaf == "firstCharacter":
            return self.container(Directory=[])
        if leaf == "prefs":
            # A real PMS answers with a bare container and the full setting
            # list for the library's type.
            settings = SECTION_PREFS.get(section.get("type") or "movie") or []
            return self.bare_container(size=len(settings), Setting=settings)
        if leaf == "composite":
            return ("png", query)

        return self.container()

    def library_metadata(self, segments, query):
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
            return self.container()

        if len(segments) == 3:
            section = self.catalog.section(row["librarySectionID"]) or {}
            detail = _full(row)
            if row.get("type") == "show":
                detail = self.show_detail(row, detail, query)
            # Verified against fun: an item-detail container is BARE -- no
            # identifier, mediaTagPrefix or mediaTagVersion. Those belong on
            # listing containers (/children does carry them). The generic
            # container() helper was injecting them here.
            return self.bare_container(
                size=1,
                allowSync=True,
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

        if leaf in ("extras", "related", "similar"):
            return self.container(Metadata=[], Hub=[])
        if leaf == "userState":
            # A real PMS 404s this (verified against fun). Answering with an
            # empty MediaContainer instead tells the client there IS state.
            return ("error", 404, "plexsim: no user state")
        if leaf == "theme":
            # No theme audio; answer empty rather than falling through to the
            # unknown-route handler.
            return ("error", 404, "plexsim: no theme")
        if leaf in ("thumb", "art", "poster", "background"):
            resolved = self.artwork_for(rating_key, leaf)
            if resolved:
                return ("image", resolved)
            return ("png", query)
        return self.container()

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
        hubs = [self.hub("home.continue", "Continue Watching", [])]
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
        return self.container(Hub=hubs)

    def section_hubs(self, section_id):
        rows = [_listing_row(r) for r in self.catalog.section_items(section_id)]
        section = self.catalog.section(section_id) or {}
        return self.container(
            Hub=[
                self.hub(
                    "movie.recentlyadded.%s" % section_id,
                    "Recently Added",
                    rows[:12],
                    librarySectionID=int(section_id),
                    librarySectionTitle=section.get("title"),
                )
            ]
        )

    def search(self, query):
        rows = [_listing_row(r) for r in self.catalog.search(query.get("query", ""))]
        limit = int(query.get("limit", 30) or 30)
        return self.container(
            Hub=[
                self.hub(
                    "movie",
                    "Movies",
                    rows[:limit],
                    type="movie",
                    hubIdentifier="movie",
                )
            ]
        )

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
        """`POST /playQueues` -- every client builds a queue before playing."""
        uri = query.get("uri", "")
        rows = self._items_for_uri(uri)

        with self.lock:
            queue_id = self.next_play_queue
            self.next_play_queue += 1
            self.play_queues[queue_id] = [int(r["ratingKey"]) for r in rows]

        metadata = []
        for position, row in enumerate(rows):
            item = _full(row)
            item["playQueueItemID"] = queue_id * 1000 + position + 1
            metadata.append(item)

        selected = metadata[0]["playQueueItemID"] if metadata else None
        logger.info(
            "PLAYQUEUE %s -> %d item(s) from uri=%s", queue_id, len(metadata), uri[:80]
        )
        return self.container(
            Metadata=metadata,
            playQueueID=queue_id,
            playQueueVersion=1,
            playQueueSelectedItemID=selected,
            playQueueSelectedItemOffset=0,
            playQueueSelectedMetadataItemID=(
                str(rows[0]["ratingKey"]) if rows else None
            ),
            playQueueTotalCount=len(metadata),
            playQueueShuffled=False,
            identifier="com.plexapp.plugins.library",
            mediaTagPrefix="/system/bundle/media/flags/",
        )

    def get_play_queue(self, queue_id):
        key = int(queue_id) if str(queue_id).isdigit() else 0
        keys = self.play_queues.get(key) or []
        metadata = []
        for position, rating_key in enumerate(keys):
            row = self.catalog.items.get(rating_key)
            if row is None:
                continue
            item = _full(row)
            item["playQueueItemID"] = key * 1000 + position + 1
            metadata.append(item)
        return self.container(
            Metadata=metadata,
            playQueueID=key,
            playQueueVersion=1,
            playQueueSelectedItemID=(metadata[0]["playQueueItemID"] if metadata else None),
            playQueueSelectedItemOffset=0,
            playQueueTotalCount=len(metadata),
            identifier="com.plexapp.plugins.library",
        )

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
        rating_key = query.get("ratingKey") or ""
        if rating_key.isdigit():
            row = self.catalog.items.get(int(rating_key))
            offset = query.get("time")
            if row is not None and offset and str(offset).isdigit():
                row["viewOffset"] = int(offset)
                logger.info(
                    "TIMELINE %s state=%s t=%sms", rating_key, query.get("state"), offset
                )
        return self.container()

    def set_watched(self, query, watched):
        key = (query.get("key") or "").rsplit("/", 1)[-1]
        if key.isdigit():
            row = self.catalog.items.get(int(key))
            if row is not None:
                if watched:
                    row["viewCount"] = int(row.get("viewCount", 0)) + 1
                    row.pop("viewOffset", None)
                else:
                    row["viewCount"] = 0
        return self.container()

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
        """part id -> (restricted debrid link, container) for a catalog row."""
        for row in self.catalog.items.values():
            for media in row.get("Media", []):
                for part in media.get("Part", []):
                    if str(part.get("id")) == str(part_id):
                        return row.get("_debrid"), part.get("container")
        return None, None


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
        if self._wants_xml():
            self._send(200, "text/xml;charset=utf-8", to_xml(container))
        else:
            body = json.dumps(container).encode()
            self._send(200, "application/json", body)

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

    def _serve_websocket(self):
        """Complete the RFC6455 handshake and hold the event channel open.

        plexsim has no events to push, so the socket only needs to exist and
        stay healthy -- but it does need a genuine 101 upgrade, because the web
        client reads a failed upgrade as a broken server.
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

        # Keep it alive with server-side pings. A silent socket is eventually
        # reaped by proxies, and Cloudflare sits in front of this one.
        import struct as _struct
        import time as _time

        deadline = _time.time() + 3600
        try:
            while _time.time() < deadline:
                _time.sleep(20)
                # opcode 0x9 = ping, FIN set, no payload.
                self.wfile.write(_struct.pack("!BB", 0x89, 0x00))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("websocket event channel closed by client")

    def _serve_eventsource(self):
        """Server-Sent Events channel the web app subscribes to on connect.

        Held open with periodic comment frames: closing it immediately makes the
        client reconnect in a tight loop. plexsim emits no real events, so this
        is a keep-alive only.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        import time as _time

        deadline = _time.time() + 300
        try:
            while _time.time() < deadline:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                _time.sleep(15)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        link, container = sim.resolve_part(part_id)
        if not link:
            self._send(404, "text/plain", b"plexsim: no such part")
            return

        try:
            upstream_url, _size = sim.resolver.resolve(link)
        except Exception as error:
            logger.error("unrestrict failed for part %s: %s", part_id, error)
            self._send(502, "text/plain", b"plexsim: debrid resolve failed")
            return

        if sim.part_mode == "redirect":
            logger.info("PART %s -> 302 CDN", part_id)
            self.send_response(302)
            self.send_header("Location", upstream_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._proxy(upstream_url, container, part_id, method)

    def _proxy(self, upstream_url, container, part_id, method):
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
    )

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
