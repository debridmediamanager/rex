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
import base64
import locale
import io
import json
import logging
import os
import re
import resource
import socket
import struct
import sys
import threading
import uuid
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
import filters as filters_module  # noqa: E402
import mediainfo as mediainfo_module  # noqa: E402
import hierarchy as hierarchy_module  # noqa: E402
import sessions as sessions_module  # noqa: E402
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

# The Add Library dialog's form, per library type and agent. See the file's own
# provenance note: a real PMS shapes the list by BOTH, and plexsim 400'd it.
_SECTION_PREFS_NEW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "section_prefs_new.json")
try:
    with open(_SECTION_PREFS_NEW_PATH) as _handle:
        SECTION_PREFS_NEW = json.load(_handle)
except (OSError, ValueError):
    SECTION_PREFS_NEW = {"settings": {}, "lists": {}}

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

# What `includeAdvanced=1` ADDS to a section's Meta, captured type by type off the
# reference by diffing the plain and advanced documents. The client sends the flag on
# the grid listing and on its FilterCollection, and reads the Sort list straight into
# the sort menu -- so ignoring the flag cost a movie library six sort options and a
# show library's episode view eight, plus the Duplicates filter on both. plexsim's
# Meta was identical with and without it.
# The ORDER of a type's sorts once the advanced ones are in, captured from the
# reference's Meta with includeAdvanced=1. They are interleaved, not appended.
ADVANCED_SORT_ORDER = {
    "movie": ["titleSort", "year", "originallyAvailableAt", "rating", "audienceRating",
              "userRating", "contentRating", "duration", "viewOffset", "viewCount",
              "addedAt", "lastViewedAt", "mediaHeight", "mediaBitrate", "random"],
    "show": ["titleSort", "year", "originallyAvailableAt", "rating", "audienceRating",
             "userRating", "contentRating", "unviewedLeafCount", "episode.addedAt",
             "addedAt", "lastViewedAt", "random"],
    "season": ["season.index,season.titleSort", "show.titleSort,index", "userRating",
               "addedAt", "random"],
    "episode": ["titleSort",
                "show.titleSort,season.index:nullsLast,episode.index:nullsLast,"
                "episode.originallyAvailableAt:nullsLast,episode.titleSort,episode.id",
                "year", "originallyAvailableAt", "rating", "audienceRating", "userRating",
                "duration", "viewOffset", "viewCount", "addedAt", "lastViewedAt",
                "mediaHeight", "mediaBitrate", "random"],
}

ADVANCED_META = {
    ("movie", "movie"): {
        "Sort": [
            {"defaultDirection": "desc", "descKey": "year:desc", "key": "year",
             "title": "Year"},
            {"defaultDirection": "desc", "descKey": "userRating:desc",
             "key": "userRating", "title": "Rating"},
            {"defaultDirection": "desc", "descKey": "contentRating:desc",
             "key": "contentRating", "title": "Content Rating"},
            {"defaultDirection": "desc", "descKey": "viewOffset:desc",
             "key": "viewOffset", "title": "Progress"},
            {"defaultDirection": "desc", "descKey": "viewCount:desc",
             "key": "viewCount", "title": "Plays"},
            {"defaultDirection": "desc", "descKey": "mediaBitrate:desc",
             "key": "mediaBitrate", "title": "Bitrate"},
        ],
        "Filter": [{"filter": "duplicate", "filterType": "boolean",
                    "key": "/library/sections/%s/duplicate", "title": "Duplicates",
                    "type": "filter", "advanced": True}],
    },
    ("show", "show"): {
        "Sort": [
            {"defaultDirection": "desc", "descKey": "year:desc", "key": "year",
             "title": "Year"},
            {"defaultDirection": "desc", "descKey": "userRating:desc",
             "key": "userRating", "title": "Rating"},
            {"defaultDirection": "desc", "descKey": "contentRating:desc",
             "key": "contentRating", "title": "Content Rating"},
            {"defaultDirection": "desc", "descKey": "addedAt:desc", "key": "addedAt",
             "title": "Date Added"},
        ],
    },
    ("show", "season"): {
        "Sort": [
            {"defaultDirection": "asc",
             "descKey": "season.index:desc,season.titleSort",
             "key": "season.index,season.titleSort", "title": "Season"},
            {"defaultDirection": "desc", "descKey": "userRating:desc",
             "key": "userRating", "title": "Rating"},
            {"defaultDirection": "desc", "descKey": "addedAt:desc", "key": "addedAt",
             "title": "Date Added"},
        ],
    },
    ("show", "episode"): {
        "Sort": [
            {"defaultDirection": "desc", "descKey": "titleSort:desc",
             "firstCharacterKey": "/library/sections/%s/firstCharacter?type=4",
             "key": "titleSort", "title": "Title"},
            {"defaultDirection": "desc", "descKey": "year:desc", "key": "year",
             "title": "Year"},
            {"defaultDirection": "desc", "descKey": "userRating:desc",
             "key": "userRating", "title": "Rating"},
            {"defaultDirection": "desc", "descKey": "duration:desc", "key": "duration",
             "title": "Duration"},
            {"defaultDirection": "desc", "descKey": "viewOffset:desc",
             "key": "viewOffset", "title": "Progress"},
            {"defaultDirection": "desc", "descKey": "viewCount:desc",
             "key": "viewCount", "title": "Plays"},
            {"defaultDirection": "desc", "descKey": "mediaHeight:desc",
             "key": "mediaHeight", "title": "Resolution"},
            {"defaultDirection": "desc", "descKey": "mediaBitrate:desc",
             "key": "mediaBitrate", "title": "Bitrate"},
        ],
        "Filter": [{"filter": "duplicate", "filterType": "boolean",
                    "key": "/library/sections/%s/duplicate?type=4",
                    "title": "Duplicates", "type": "filter", "advanced": True}],
    },
}

# Plex's numeric type codes, and the field-name PREFIX each one uses. A show
# library advertises `show.genre` and `episode.title`; a movie library advertises
# the bare name. /query/parse echoes the prefix back as the rule's own `type`,
# and an unprefixed field is read against the prefix the requested `type` implies
# -- measured: `?type=2&title=x` answers `fieldTitle: "Show Title"`.
TYPE_NUMBERS = {"movie": 1, "show": 2, "season": 3, "episode": 4,
                "artist": 8, "album": 9, "track": 10, "photo": 13,
                "collection": 18}
IMPLICIT_SCOPE = {1: "", 2: "show", 3: "season", 4: "episode"}

# Every field any library advertises, keyed by its BARE name, plus the title each
# (scope, field) pair carries. Two measurements force both shapes: the filter
# lookup is NOT scoped to the section (`show.title` parses and filters against a
# movie library too), and a show library's advertised keys arrive back prefixed,
# so a map keyed by the prefixed name can never match -- which is why no filter
# on a show grid ever did anything.
FILTER_FIELD_TYPES = {}
FIELD_TITLES = {}
for _meta in SECTION_META.values():
    for _type in _meta.get("Type") or []:
        for _field in _type.get("Field") or []:
            _key = _field.get("key")
            if not _key:
                continue
            _scope, _, _bare = _key.rpartition(".")
            FILTER_FIELD_TYPES.setdefault(_bare, _field.get("type") or "string")
            FIELD_TITLES.setdefault((_scope, _bare), _field.get("title") or "")

# Per-item Preferences: the Setting list an item's own settings tab reads,
# captured from a real PMS. A show has eleven and a season has three, and they
# are not the same three -- plexsim invented a single `episodeSort` row.
_ITEM_PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "item_prefs.json")
try:
    with open(_ITEM_PREFS_PATH) as _handle:
        ITEM_PREFS = json.load(_handle)
except (OSError, ValueError):
    ITEM_PREFS = {}

_OWNER_FEATURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner_features.txt")
try:
    with open(_OWNER_FEATURES_PATH) as _handle:
        OWNER_FEATURES = _handle.read().strip()
except OSError:
    OWNER_FEATURES = "pass,sync,hwtranscode"

# `/media/grabbers`, verbatim off the reference. plexsim answered an empty
# container, which tells the Live TV setup page the server supports no tuner
# protocols at all rather than that it has no tuners.
# What the Edit Library dialog is allowed to name. Measured: a real PMS answers
# 400 for an agent, scanner or language it does not have, where plexsim accepted
# anything with a 200 -- so a typo in the dialog looked saved and silently was not.
# The `/:/resources/*` names plexsim's own containers reference. A real PMS 404s a
# name it does not have; plexsim generated a tile for anything.
KNOWN_RESOURCES = {"movie.png", "show.png", "artist.png", "photo.png",
                   "movie-fanart.jpg", "show-fanart.jpg", "artist-fanart.jpg",
                   "library-art.png", "playlist.png"}

# Which verbs each section leaf accepts. Everything absent here is a 404 for POST,
# PUT and DELETE -- measured leaf by leaf, and a real PMS never answers 405: that
# status does not appear anywhere on its surface.
SECTION_LEAF_VERBS = {
    "refresh": {"GET", "HEAD", "POST", "DELETE"},
    "emptyTrash": {"PUT"},
    "analyze": {"PUT"},
    "indexes": {"DELETE"},
    "prefs": {"GET", "HEAD", "PUT"},
    "all": {"GET", "HEAD", "PUT"},
    "common": {"GET", "HEAD"},
}

# The NFO agents are in the list because the Add/Edit Library dialog offers them --
# /system/agents lists them for every video type -- and a library the dialog can
# pick must be one the create accepts.
KNOWN_AGENTS = {"tv.plex.agents.movie", "tv.plex.agents.series", "tv.plex.agents.none",
                "tv.plex.agents.nfo.movie", "tv.plex.agents.nfo.series",
                "com.plexapp.agents.none"}
KNOWN_SCANNERS = {"Plex Movie", "Plex Movie Scanner", "Plex Video Files Scanner",
                  "Plex Video Files", "Plex TV Series", "Plex Series Scanner"}
KNOWN_LANGUAGES = {"en-US", "en-GB", "en-AU", "en-CA", "de-DE", "fr-FR", "es-ES",
                   "es-MX", "it-IT", "nl-NL", "pt-BR", "pt-PT", "sv-SE", "da-DK",
                   "nb-NO", "fi-FI", "pl-PL", "cs-CZ", "hu-HU", "ro-RO", "tr-TR",
                   "el-GR", "ru-RU", "he-IL", "ar-SA", "ja-JP", "ko-KR", "zh-CN",
                   "zh-TW", "zh-HK", "hi-IN", "id-ID", "vi-VN", "th-TH", "uk-UA",
                   "ca-ES", "et-EE", "lv-LV", "lt-LT", "sk-SK", "hr-HR", "is-IS",
                   "fr-CA"}

MEDIA_GRABBERS = [
    {"title": "HDHomerun", "identifier": "tv.plex.grabbers.hdhomerun",
     "protocol": "livetv"},
    {"title": "Stream", "identifier": "tv.plex.grabbers.stream", "protocol": "stream"},
    {"title": "Download", "identifier": "tv.plex.grabbers.download",
     "protocol": "download"},
    {"title": "Remote", "identifier": "tv.plex.grabbers.tunerservice",
     "protocol": "livetv"},
]

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
    "network": "Network",
}
SECONDARY_FIELDS = {
    "year": "year",
    "decade": "year",
    "contentRating": "contentRating",
    "studio": "studio",
    "editionTitle": "editionTitle",
}
# The axes read off the Media and its Streams rather than off a tag or a field.
# Their entries carry only `key` and `title` -- measured, there is no `fastKey`
# here, so the client builds the URL itself.
SECONDARY_MEDIA = ("resolution", "videoCodec", "audioCodec", "subtitleCodec",
                   "audioLayout", "audioLanguage", "subtitleLanguage")
SECONDARY_TITLES = {
    "genre": "By Genre",
    "year": "By Year",
    "decade": "By Decade",
    "contentRating": "By Content Rating",
    "collection": "By Collection",
    "director": "By Director",
    "writer": "By Writer",
    "producer": "By Producer",
    # Measured: "By Starring Actor", not "By Actor".
    "actor": "By Starring Actor",
    "country": "By Country",
    "studio": "By Studio",
    "label": "By Label",
    "network": "By Network",
    "editionTitle": "By Edition",
    "resolution": "By Resolution",
    "videoCodec": "By Video Codec",
    "audioCodec": "By Audio Codec",
    "subtitleCodec": "By Subtitle Codec",
    "audioLayout": "By Audio Layout",
    "audioLanguage": "By Audio Language",
    "subtitleLanguage": "By Subtitle Language",
    "location": "By Folder Location",
    # `/unwatched` is a LISTING, not an axis, and this is its title2.
    "unwatched": "Unplayed",
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

# `/library/sections/{id}/filters`, captured entry for entry from a real PMS --
# 28 on a movie library and a DIFFERENT 15 on a show one, which is the same
# lesson as the two `Meta` sidecars: one list served for both types gives a show
# library a filter bar that can only talk about movies. plexsim served a
# hand-trimmed 15 for both.
#
# The tuple is (filter, filterType, title, key suffix). The suffix is measured
# and not derivable: a show library's keys carry `?type=2` -- except `country`,
# `unmatched` and `editionTitle`, which do not.
#
# Seven of these axes 404 on the reference itself (`hdr`, `dovi`, `hdr10plus`,
# `atmos`, `inProgress`, `unmatched`, `unwatchedLeaves`). They are advertised
# anyway, because that is what a real PMS advertises -- plexsim's unknown-leaf
# 404 reproduces the rest of it for free.
SECTION_FILTERS = {
    "movie": [
        ("genre", "string", "Genre", ""),
        ("year", "integer", "Year", ""),
        ("decade", "integer", "Decade", ""),
        ("contentRating", "string", "Content Rating", ""),
        ("collection", "string", "Collection", ""),
        ("director", "string", "Director", ""),
        ("actor", "string", "Actor", ""),
        ("writer", "string", "Writer", ""),
        ("producer", "string", "Producer", ""),
        ("country", "string", "Country", ""),
        ("studio", "string", "Studio", ""),
        ("resolution", "string", "Resolution", ""),
        ("hdr", "boolean", "HDR", ""),
        ("dovi", "boolean", "DOVI", ""),
        ("hdr10plus", "boolean", "HDR10+", ""),
        ("atmos", "boolean", "Atmos", ""),
        ("unwatched", "boolean", "Unwatched", ""),
        ("inProgress", "boolean", "In Progress", ""),
        ("unmatched", "boolean", "Unmatched", ""),
        ("videoCodec", "string", "Video Codec", ""),
        ("audioCodec", "string", "Audio Codec", ""),
        ("subtitleCodec", "string", "Subtitle Codec", ""),
        ("audioLayout", "string", "Audio Layout", ""),
        ("audioLanguage", "string", "Audio Language", ""),
        ("subtitleLanguage", "string", "Subtitle Language", ""),
        ("editionTitle", "string", "Edition", ""),
        ("label", "string", "Labels", ""),
        ("location", "string", "Folder Location", ""),
    ],
    "show": [
        ("genre", "string", "Genre", "?type=2"),
        ("year", "integer", "Year", "?type=2"),
        ("contentRating", "string", "Content Rating", "?type=2"),
        ("studio", "string", "Studio", "?type=2"),
        ("network", "string", "Network", "?type=2"),
        ("country", "string", "Country", ""),
        ("collection", "string", "Collection", "?type=2"),
        ("director", "string", "Director", "?type=2"),
        ("actor", "string", "Actor", "?type=2"),
        ("writer", "string", "Writer", "?type=2"),
        ("producer", "string", "Producer", "?type=2"),
        ("unwatchedLeaves", "boolean", "Unwatched", "?type=2"),
        ("unmatched", "boolean", "Unmatched", ""),
        ("editionTitle", "string", "Edition", ""),
        ("label", "string", "Labels", "?type=2"),
    ],
}

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

# `/system/agents` -- the modern agents a real PMS lists, each with the Language
# list the Edit Library dialog fills its dropdown from and the `default` flag it
# preselects by. plexsim sent one `en-US` row with no `default`, so the dropdown
# had one language and nothing selected. Which agents each library type offers is
# measured too. See system_agents.json for what is left out and why.
_SYSTEM_AGENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "system_agents.json")
try:
    with open(_SYSTEM_AGENTS_PATH) as _handle:
        _SYSTEM_AGENTS_DOC = json.load(_handle)
except (OSError, ValueError):
    _SYSTEM_AGENTS_DOC = {"agents": [], "mediaTypes": {}}
SYSTEM_AGENTS = _SYSTEM_AGENTS_DOC["agents"]
AGENT_MEDIA_TYPES = _SYSTEM_AGENTS_DOC["mediaTypes"]


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
        self._strip_show_fields()
        self._apply_section_defaults()
        # Row and media rules measured after most catalogs were built; see
        # hierarchy.normalize_rows and mediainfo.normalize_media for each rule.
        hierarchy_module.normalize_rows(list(self.items.values()))
        for row in self.items.values():
            for media in row.get("Media") or []:
                mediainfo_module.normalize_media(media)

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

    # Fields that belong to a SHOW and not to its episodes. Measured on both the
    # 1.43.4 reference and production fun: a real episode row carries no Genre,
    # no studio, no tagline and no Country. plexsim's episodes inherited all of
    # them from the enriched file row, so an episode page advertised its show's
    # genres where a real one shows none.
    SHOW_ONLY_FIELDS = ("Genre", "Country", "studio", "tagline", "originalTitle",
                        "rating", "Label")

    def _apply_section_defaults(self):
        """Apply the library-type preferences that change a ROW, at load."""
        for section in self.sections:
            settings = SECTION_PREFS.get(section.get("type") or "movie") or []
            for setting in settings:
                if setting.get("id") == "flattenSeasons":
                    self.apply_flatten_seasons(section["key"], setting.get("value"))

    def apply_flatten_seasons(self, section_id, value):
        """`flattenSeasons` does NOT change what /children returns.

        Measured: with the pref set to Hide, a real PMS still answers /children
        with the SEASONS -- what changes is a single field on the show row,
        `skipChildren`, and the CLIENT decides to skip the season level on the
        strength of it. plexsim served the preference in its section prefs and
        then ignored it, so a flatten-seasons library still drew every season.

        Enum, measured: `0` Show, `1` Hide, `2` Hide for single-season series.
        """
        for row in self.section_items(section_id):
            if row.get("type") != "show":
                continue
            seasons = len(self.children(row["ratingKey"]))
            skip = str(value) == "1" or (str(value) == "2" and seasons <= 1)
            if skip:
                row["skipChildren"] = True
            else:
                row.pop("skipChildren", None)

    def _strip_show_fields(self):
        for row in self.items.values():
            if row.get("type") != "episode":
                continue
            for field in self.SHOW_ONLY_FIELDS:
                row.pop(field, None)

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
        """Rows whose title matches, by WORD PREFIX rather than by substring.

        Measured against the reference, query by query: `te`, `tea`, `tear`,
        `tears`, `of` and `steel` all find "Tears of Steel" and `uck` does NOT
        find "Big Buck Bunny" -- so a match is a prefix of some WORD in the
        title, not a substring of it. A single character matches nothing at all,
        whatever it is (`t` answers seventeen empty hubs). plexsim used a plain
        substring test, so `uck` matched and `t` returned the whole library.

        The reference is additionally fuzzy -- `eel` finds "Firefly" and an actor
        called "Halina Reijn" -- which is a trigram score plexsim does not try to
        reproduce, and says so rather than approximating it badly.
        """
        needle = query.lower().strip()
        if len(needle) < 2:
            return []

        def hit(text):
            words = re.split(r"[^0-9a-z]+", str(text or "").lower())
            return any(word.startswith(needle) for word in words if word) \
                or " ".join(w for w in words if w).startswith(needle)

        return [row for row in self.items.values()
                if row.get("type") in ("movie", "show", "season", "episode")
                and (hit(row.get("title")) or hit(row.get("originalTitle")))]


# --------------------------------------------------------------------------
# metadata projection
# --------------------------------------------------------------------------


def accept_wants_xml(header):
    """Whether an Accept header selects XML, the way a real PMS reads it.

    Measured, header by header: only an EXACT, case-sensitive `application/json`
    or `application/xml` range counts; a `q` parameter ranks the two and a tie goes
    to the one listed first; ANY other parameter disqualifies the range, so
    `application/json; charset=utf-8` is answered in XML, as is `Application/JSON`.
    With neither, the answer is XML. plexsim answered JSON whenever the substring
    appeared, so `application/xml, application/json` came back as JSON.
    """
    best = None
    for position, token in enumerate((header or "").split(",")):
        parts = [part.strip() for part in token.split(";")]
        if parts[0] not in ("application/json", "application/xml"):
            continue
        weight = 1.0
        usable = True
        for parameter in parts[1:]:
            name, _, value = parameter.partition("=")
            if name.strip() != "q":
                usable = False
                break
            try:
                weight = float(value)
            except ValueError:
                usable = False
                break
        if not usable or weight <= 0:
            continue
        rank = (weight, -position)
        if best is None or rank > best[0]:
            best = (rank, parts[0] == "application/xml")
    return True if best is None else best[1]


def _drop_empty(value):
    if isinstance(value, dict):
        return {key: _drop_empty(item) for key, item in value.items()
                if item is not None and item != []}
    if isinstance(value, list):
        return [_drop_empty(item) for item in value]
    return value


def json_body(container):
    """A container as JSON with every `null` and empty list left out.

    Measured across ~110 endpoints of the reference: not one `null` and not one
    `[]`. The XML writer already dropped `None`, so XML clients never saw the
    difference, but plexsim's JSON carried `audioProfile: null`, `refFrames: null`,
    `Label: []` and a `Metadata: []` on every empty hub -- the shape a typed client
    decoding a missing key happily chokes on as a null. An empty OBJECT is kept:
    `Related: {}` is exactly what the reference sends for an item with no hubs.
    """
    return json.dumps(_drop_empty(container)).encode()


# The tree's MediaStream is its own projection, measured: a fixed field set per
# stream type, and every number in it is a STRING (`"bitDepth": "8"`,
# `"frameRate": "24.000"`, `"samplingRate": "44100"`) except `id`, `index`, `type`
# and an audio stream's `channels`. The MediaPart beside it has no `container`.
# plexsim copied the detail Stream across, numbers and detail-only fields included.
TREE_STREAM_FIELDS = {
    1: ("id", "index", "type", "codec", "language", "bitDepth", "chromaLocation",
        "chromaSubsampling", "codedHeight", "codedWidth", "frameRate",
        "hasScalingMatrix", "height", "level", "profile", "refFrames", "scanType",
        "width"),
    2: ("id", "index", "type", "channels", "codec", "language", "samplingRate"),
}
TREE_STREAM_NUMBERS = ("id", "index", "type", "channels")


def _tree_stream(stream):
    kind = stream.get("streamType")
    out = {}
    for name in TREE_STREAM_FIELDS.get(kind, ("id", "index", "type", "codec", "language")):
        if name == "type":
            value = kind
        elif name == "language":
            # The tree's language is the short TAG (`en`), not the detail's label.
            value = stream.get("languageTag") or stream.get("language")
        else:
            value = stream.get(name)
        if value is None:
            continue
        if name not in TREE_STREAM_NUMBERS:
            if name == "frameRate" and isinstance(value, (int, float)):
                value = "%.3f" % value
            elif isinstance(value, bool):
                value = "1" if value else "0"
            elif isinstance(value, (int, float)):
                value = str(value)
        out[name] = value
    return out


def _pref_parses(setting, value):
    if setting.get("type") == "int":
        try:
            int(value)
        except (TypeError, ValueError):
            return False
    return True


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


# Tag lists a grid or hub row carries, each tag reduced to its text.
GRID_TAGS = ("Genre", "Country", "Director", "Writer", "Role", "Collection", "Label")


# The client profiles a real PMS ships -- the file names of its Resources/Profiles
# directory, read off the reference. A decision is refused (400) unless
# X-Plex-Client-Profile-Name, X-Plex-Platform or X-Plex-Device names one of them
# exactly: `Chrome`, `Windows` and `Microsoft Edge` are accepted, where `Edge`,
# `Darwin`, `Kodi`, `webOS`, `chrome` and `Chrome ` are all 400s.
CLIENT_PROFILES = frozenset([
    "Android", "Asus Transformer Prime TF201", "Chrome", "Chromecast", "Firefox",
    "Generic", "HTML TV App", "Internet Explorer", "Konvergo", "Microsoft Edge",
    "Mystery 3", "Mystery 4", "Mystery 5", "Opera TV App", "Opera",
    "Panasonic Viera 3D TV", "PlayStation 3 App", "PlayStation 3", "PlayStation 4 App",
    "Plex Desktop", "Plex Home Theater-RaspberryPi", "Plex Home Theater", "Plexamp",
    "Popcorn Hour", "Roku-3.1", "Roku-5.x", "Roku-5000X-6.0", "Roku-6.x", "Roku-7.x",
    "Roku", "RokuDLNA-3.1", "RokuDLNA-4400X-7.x", "RokuDLNA-5.1", "RokuDLNA-5000X-6.x",
    "RokuDLNA-5000X-7.x", "RokuDLNA-6.x", "RokuDLNA-6000X-7.x", "Safari",
    "Samsung SmartTV", "Samsung Tizen", "Samsung", "Sonos", "Sony Blu-ray 2013",
    "Sony Blu-ray", "Sony Bravia 2010", "Sony Bravia 2011", "Sony Bravia 2012",
    "Sony Bravia 2013", "Sony Streaming Player", "Universal Mobile",
    "Universal Recording", "Universal TV", "Unknown Browser", "Vivaldi", "Web",
    "Western Digital TV Live", "Wii U", "Windows 10 Desktop", "Windows 10 Mobile",
    "Windows Phone", "Windows", "Xbox 360 App", "Xbox 360", "Xbox One", "ZP",
    "iOS-iPad-1,1", "iOS-iPhone-1,1", "iOS-iPhone-1,2", "iOS-iPhone-2,1",
    "iOS-iPhone-3,1", "iOS-iPhone-3,2", "iOS-iPhone-3,3", "iOS-iPod-1,1",
    "iOS-iPod-2,1", "iOS-iPod-3,1", "iOS-iPod-4,1", "iOS", "tvOS",
])

# The two-letter languages a subtitle search accepts. The rule is measured (`en`,
# `de`, `en-US` search; `xx`, `zz`, `eng` are refused); the table is the system's
# own list of language codes rather than a hand-typed one.
SUBTITLE_LANGUAGES = frozenset(
    code for code in {alias.split("_")[0].split(".")[0].split("@")[0]
                      for alias in locale.locale_alias}
    if len(code) == 2 and code.isalpha())


def _grid_row(row, keep_section=True):
    """A row as a GRID or a HUB shelf carries it -- lighter than the detail row.

    Measured in the client's own corpus against the reference, and on fun: a grid
    or shelf row's tags are `{tag}` and nothing else (no id, filter, role or thumb),
    a Part carries no `Stream` list, and there is no `Rating`, `Location`, `Field`
    or `Producer`. A SECTION listing's rows also leave the librarySection fields to
    the container, where a hub's rows keep them. plexsim sent the full detail row
    everywhere. The catalog's own lists are shared, so this copies what it trims.
    """
    out = {k: v for k, v in row.items()
           if k not in ("Location", "Rating", "Field", "Producer")}
    if not keep_section:
        for key in ("librarySectionID", "librarySectionTitle", "librarySectionKey"):
            out.pop(key, None)
    for key in GRID_TAGS:
        if isinstance(out.get(key), list):
            out[key] = [{"tag": tag["tag"]} for tag in out[key]
                        if isinstance(tag, dict) and tag.get("tag")]
    if out.get("Media"):
        out["Media"] = [dict(media, Part=[{k: v for k, v in part.items() if k != "Stream"}
                                          for part in media.get("Part") or []])
                        for media in out["Media"]]
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
                 assert_claimed=False, owner_email=None, tmdb=None, token=None):
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
        # The token every request must carry, or None to serve anyone. A real PMS
        # always has one and 401s everything but /identity and /web/* without it --
        # measured on both reference servers, for a missing AND a wrong token alike.
        self.token = token
        # Verified against fun and zen: both report the owner's email here.
        self.owner_email = owner_email or owner_username
        # Reported on /myplex/account the way fun and zen do.
        self.public_address = ""
        self.private_address = ""
        self.unknown = []
        self.requests = []
        self.lock = threading.Lock()
        # Playback bookkeeping a client expects to survive between calls.
        # A play queue is a record, not a bare key list. Measured: its item ids
        # are GLOBALLY increasing and stable across inserts, moves and deletes,
        # its version increments on every mutation, and the client throws the
        # whole response away when the selected id is not the one it holds.
        self.play_queues = {}
        self.next_play_queue = 1
        self.next_play_queue_item = 1
        # ratingKeys a decision has been made for. The timeline's `Bandwidths`
        # block appears only after one, measured -- not merely while playing.
        self.decided = set()
        self.sessions = {}
        # Everything the UI can change: watch state, edits, playlists,
        # collections, play history. In memory only -- catalog.json is input.
        self.library = library_module.Library(catalog,
                                             notify=self.notify_activity_event,
                                             resolve=self._items_for_uri)
        # Preferences the client has written back through PUT /:/prefs. A real
        # PMS persists these; plexsim keeps them for the life of the process so
        # a settings page round-trips instead of silently reverting.
        self.pref_overrides = {}
        # The resource graph's series, and the last (wall clock, cpu seconds) pair
        # a process-CPU percentage is measured against.
        self._resource_series = []
        self._resource_sample = None
        # Per-section settings, keyed (section id, setting id): `flattenSeasons`
        # and the rest of the library-type set live in their own id space.
        self.section_pref_overrides = {}
        # Download queues by client identifier: `POST /downloadQueue` answers the
        # SAME queue to the same client, measured.
        self.download_queues = {}
        # Section ids with a scan in flight, which /library/sections reports as
        # `refreshing` and the Libraries page reads to swap its Scan button.
        self.refreshing = set()
        # Recent log lines, for /diagnostics/logs -- the Troubleshooting page's
        # Download Logs button, which is only drawn when the server advertises
        # `diagnostics` containing "logs".
        self.log_lines = []
        # Channels that opened with `filters=log`. Kept apart from `sockets` because
        # a log frame must reach ONLY those: measured, a socket with no filters gets
        # everything EXCEPT log, and one with `filters=log` gets nothing else.
        self.log_sinks = []
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
        # Live playback sessions and the byte counters behind the Dashboard. A
        # session is created by /:/timeline, not by anyone reading bytes.
        self.sessions = sessions_module.Sessions(owner=owner_username)

    # -- notifications ---------------------------------------------------

    def log_sink(self, sink):
        """Register a channel that asked for `filters=log`."""
        with self.lock:
            self.log_sinks.append(sink)

    def drop_log_sink(self, sink):
        with self.lock:
            self.log_sinks = [s for s in self.log_sinks if s is not sink]

    def push_log(self, record):
        """One `LogSentNotification`, in the shape the reference sends.

        Measured with `?filters=log`, which is what the client's own
        MediaServerLogEventConnection passes: `{"level": 3, "thread": "...",
        "time": "Sep 12, 2026 18:19:15.428", "message": "..."}`. An earlier finding
        said this channel carries no log frames on a real server -- that measurement
        was taken without the parameter, which is the exact trap this project's one
        rule is about.
        """
        with self.lock:
            sinks = list(self.log_sinks)
        if not sinks:
            return
        try:
            row = {
                "level": {"DEBUG": 4, "INFO": 3, "WARNING": 2,
                          "ERROR": 1, "CRITICAL": 0}.get(record.levelname, 3),
                "thread": str(record.thread),
                "time": time.strftime("%b %d, %Y %H:%M:%S",
                                      time.localtime(record.created))
                        + ".%03d" % (record.msecs,),
                "message": record.getMessage(),
            }
        except Exception:
            return
        for sink in sinks:
            try:
                sink("log", "LogSentNotification", [row])
            except Exception:
                self.drop_log_sink(sink)

    def add_socket(self, sink, filters=None):
        """Register a channel. `filters` is the client's `filters=` parameter.

        Measured by running two sockets side by side: `filters=` is an exact
        whitelist of type names, and `log` is OPT-IN ONLY -- a socket with no
        filters receives everything EXCEPT log frames, and `?filters=log` receives
        386 log frames and nothing else. plexsim fanned every event to every sink,
        so a socket opened for the Console's log pane got playing and activity
        frames and never a log line.
        """
        with self.lock:
            self.sockets.append((sink, frozenset(filters) if filters else None))

    def drop_socket(self, sink):
        with self.lock:
            self.sockets = [(s, f) for s, f in self.sockets if s is not sink]

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
        for sink, filters in sinks:
            if filters is None:
                # No `filters=`: everything except `log`, which is opt-in only.
                if kind == "log":
                    continue
            elif kind not in filters:
                continue
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
    # A playlist is 15 and a collection 18 on the wire -- measured -- and the client
    # builds the URL of the row it updates from this number (`/playlists/`,
    # `/library/collections/`, else `/library/metadata/`). With 1 there, a list
    # deleted anywhere dispatched a delete for a metadata row nobody holds.
    TIMELINE_TYPES = {"movie": 1, "show": 2, "season": 3, "episode": 4,
                      "playlist": 15, "collection": 18}

    # The `state` values /:/timeline accepts. Measured: anything else is a 400.
    TIMELINE_STATES = {"playing", "paused", "buffering", "stopped"}

    # The timeline's state alphabet, read off the reference and pinned against the
    # client's own reducer (`main-*.js` module 35623, constants `lm = 5`, `cm = 9`):
    #
    #   0  created        3  queued / processing / loading an edit
    #   4  loading        5  DONE -- and it carries NO metadataState
    #   9  deleted
    #
    # `5` is what dispatches `metadataProcessed`, which is what makes the client
    # REFETCH the row. And the reducer deletes its entry only when `metadataState` is
    # absent, so a state that always carries one latches every `!!metadataState`
    # gate: the spinner, and the Refresh/Match menu entries.
    TIMELINE_CREATED = 0
    TIMELINE_QUEUED = 3
    TIMELINE_LOADING = 4
    TIMELINE_DONE = 5
    TIMELINE_DELETED = 9

    def notify_timeline(self, row, state=3, metadata_state="queued"):
        """A TimelineEntry for one row, with the field set measured off a real
        refresh: identifier, sectionID and itemID as STRINGS, the numeric type,
        the title, `state` and `metadataState`.

        `metadata_state=None` omits the field, which is what state 5 does.
        """
        title = row.get("title") or ""
        if row.get("type") == "movie" and title and row.get("year"):
            # Measured: a movie's frames title it with its year, `Elephants Dream
            # (2006)`, which the Activity pane prints.
            title = "%s (%s)" % (title, row["year"])
        entry = {
            "identifier": "com.plexapp.plugins.library",
            "sectionID": str(row.get("librarySectionID", "")),
            "itemID": str(row.get("ratingKey", "")),
            "type": self.TIMELINE_TYPES.get(row.get("type"), 1),
            "title": title,
            "state": state,
            "updatedAt": int(time.time()),
        }
        if metadata_state is not None:
            entry["metadataState"] = metadata_state
        self.notify("timeline", "TimelineEntry", [entry])

    def notify_refreshed(self, row):
        """The seven-frame sequence a real PMS pushes for a metadata refresh.

        Measured verbatim off the reference's eventsource: timeline(3, queued),
        activity started, activity updated, timeline(3, processing),
        timeline(4, loading), activity ended, timeline(5, no metadataState).

        plexsim pushed the first activity frame and ONE timeline(3, queued) and
        stopped. So state 5 never arrived, the client never dispatched
        `metadataProcessed` and never refetched the row it had just refreshed --
        Refresh Metadata did nothing on screen until a reload -- and because
        `metadataState` was never cleared, the reducer entry was never deleted and
        every `!!metadataState` gate stayed latched.
        """
        self.notify_timeline(row, self.TIMELINE_QUEUED, "queued")
        activity = self.library.add_activity(row.get("title") or "", progress=-1)
        self.notify_timeline(row, self.TIMELINE_QUEUED, "processing")
        self.notify_timeline(row, self.TIMELINE_LOADING, "loading")
        self.library.finish_activity(activity)
        self.notify_timeline(row, self.TIMELINE_DONE, None)

    def notify_edited(self, row, metadata_state="loading"):
        """The two frames an EDIT pushes: state 3, then done. plexsim pushed none,
        so an edit made here was invisible to every other open client. Measured:
        the first frame says `loading` for an edit to the item's own path and
        `queued` for one saved through `/library/sections/{id}/all`."""
        self.notify_timeline(row, self.TIMELINE_QUEUED, metadata_state)
        self.notify_timeline(row, self.TIMELINE_DONE, None)

    def notify_created(self, row):
        self.notify_timeline(row, self.TIMELINE_CREATED, "created")

    def notify_deleted(self, row):
        """state 9, which is what dispatches the client's own `deleted` action --
        without it a row deleted through the API stays on screen everywhere else."""
        self.notify_timeline(row, self.TIMELINE_DELETED, "deleted")

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
        if "size" not in fields:
            # An explicit size wins: a show's /children counts its "All episodes"
            # Directory row alongside the seasons, so the count is not simply the
            # length of the first list.
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

        return self.parent_detail(row, detail, query)

    def parent_detail(self, row, detail, query):
        """The blocks a show OR a season carries when the client asks for them.

        Measured: a SEASON detail carries `OnDeck`, `Extras` and `Preferences`
        just as a show does -- plexsim answered them for shows only, so a season
        page had no episode to offer and an empty settings tab. And the
        Preferences lists differ: eleven settings on a show, three on a season
        (audio language, subtitle language, subtitle mode), against the one
        `episodeSort` row plexsim used to invent.
        """
        leaves = self.catalog.leaves(row["ratingKey"])

        if query.get("includeExtras") == "1":
            detail["Extras"] = {"size": 0}

        if query.get("includeOnDeck") == "1":
            # First episode with no view offset, else the first episode.
            unwatched = [l for l in leaves if not l.get("viewOffset") and not l.get("viewCount")]
            pick = (unwatched or leaves or [None])[0]
            if pick is not None:
                detail["OnDeck"] = {"Metadata": _full(pick)}

        # A setting the client has written shows up on the row itself as well,
        # the way `episodeSort` does on a real show.
        for name, value in (row.get("_prefs") or {}).items():
            detail[name] = value
        return detail

    def apply_detail_options(self, row, detail, query):
        """The `include*` / `exclude*` / `checkFiles` effects, measured one by one.

        Each of these is a parameter the official client actually sends, and each
        was silently ignored -- which is the quiet kind of wrong: the response is
        a valid detail document that simply lacks the block the client asked for,
        so the page renders with a section missing rather than with an error.
        """
        if query.get("includeChildren") == "1":
            # A show gains its seasons INLINE, as a `Children` block with its own
            # size -- not a second request to /children.
            children = self.catalog.children(row["ratingKey"])
            if children:
                detail["Children"] = {"size": len(children),
                                      "Metadata": [_full(c) for c in children]}
        if query.get("includeRelated") == "1":
            # Measured on the reference: the item's related hubs as
            # `{"Hub": [...]}`, and an empty OBJECT -- not an absent block -- for an
            # item with none. plexsim always sent the empty object, even where the
            # hubs endpoint had a shelf for the same item.
            hubs = self.related_hubs(row, query)
            detail["Related"] = {"Hub": hubs} if hubs else {}
        for field in (query.get("excludeFields") or "").split(","):
            if field and field in detail:
                detail.pop(field)
        excluded = [e for e in (query.get("excludeElements") or "").split(",") if e]
        for element in excluded:
            detail.pop(element, None)
        if "Media" in excluded:
            # Measured: dropping Media substitutes two COUNTS, which is what a
            # grid needs to decide whether to draw the version picker at all.
            detail["mediaCount"] = len(row.get("Media") or [])
            detail["mediaCountOptimized"] = 0
        # `checkFiles=1` is the one option here with nothing to do: measured, it
        # adds `accessible` and `exists` to every Part, and plexsim already sends
        # both on every Part unconditionally -- deliberately, because plezy
        # re-fetches metadata in a loop until it sees them. See mediainfo.py.
        if query.get("includePreferences") == "1":
            # The item's own settings tab. A movie has three, a show eleven and a
            # season three, all captured -- and the list is per TYPE, so serving
            # the show list for a movie offers settings the item does not have.
            settings = ITEM_PREFS.get(row.get("type")) or []
            stored = row.get("_prefs") or {}
            if settings:
                # An EPISODE has no settings of its own, and the reference omits
                # the block entirely rather than sending an empty one -- an empty
                # Preferences draws a settings tab with nothing in it.
                # Measured: the ROW attribute carries whatever was stored, but the
                # setting reports it only when it parses as the setting's type --
                # after `flattenSeasons=abc` the row says `abc` and the setting still
                # says `-1`.
                detail["Preferences"] = {"Setting": [
                    dict(s, value=stored[s["id"]])
                    if s.get("id") in stored and _pref_parses(s, stored[s["id"]]) else s
                    for s in settings
                ]}
        return detail

    def child_container(self, parent, rows, leaf, query=None, **extra):
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
            "Metadata": self._guid_gate([_full(r) for r in rows], query),
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
            # the parent -- hence grandparent* on the container. Measured on both
            # servers: `title1` is the SHOW's title, not the library's;
            # `parentTitle` is the EMPTY string; `grandparentRatingKey` is a
            # number; and there is no `summary`, `parentYear` or `grandparentYear`.
            show_key = parent.get("parentRatingKey")
            show = self.catalog.items.get(int(show_key)) if show_key else None
            fields.pop("summary", None)
            fields.update(
                {
                    "title1": show.get("title") if show else parent.get("parentTitle"),
                    "title2": parent.get("title"),
                    "parentIndex": parent.get("index"),
                    "parentTitle": "",
                    "thumb": parent.get("thumb"),
                    "art": parent.get("art"),
                    "theme": "/library/metadata/%s/theme/1" % show_key if show_key else None,
                }
            )
            if show:
                show_rating_key = show.get("ratingKey")
                fields.update(
                    {
                        "grandparentRatingKey": (int(show_rating_key)
                                                 if str(show_rating_key).isdigit()
                                                 else show_rating_key),
                        "grandparentTitle": show.get("title"),
                        "grandparentThumb": show.get("thumb"),
                        "grandparentStudio": show.get("studio"),
                        "grandparentContentRating": show.get("contentRating"),
                        "grandparentTheme": "/library/metadata/%s/theme/1" % show.get("ratingKey"),
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

        # A real PMS emits offset/totalSize only when the client pages -- and only
        # when BOTH container parameters are present; see page_window().
        page, paged, start, total = self.page_window(rows, query or {})
        if paged:
            fields["Metadata"] = self._guid_gate([_full(r) for r in page], query)
            fields["offset"] = start
            fields["totalSize"] = total
        fields.update(extra)
        if extra.get("Directory"):
            # Measured: the container's `size` counts the "All episodes" Directory
            # row as well as the seasons, which is what `excludeAllLeaves=1` takes
            # back off it -- and a PAGED request drops the row entirely, because it
            # is not part of the page. plexsim appended it to every page and counted
            # it there, so a virtualised list mis-measured and every page grew a
            # spurious tile. In XML the row also comes FIRST, before the seasons.
            fields["size"] = len(fields.get("Metadata") or []) + len(extra["Directory"])

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
        # The id is the section's own key, because "Folder Location" filters by
        # this id (`?location=1`) and a shared id would make every section's
        # folder filter answer for section 1.
        row["Location"] = [{"id": int(key) if str(key).isdigit() else 1,
                            "path": "/plexsim/%s" % key}]
        if shared:
            # Measured: the shared view carries empty art/thumb.
            row["art"] = ""
            row["thumb"] = ""
        # And no `composite` on either view: neither real server sends one here.
        return row

    def section_meta(self, section_id, section_type, advanced=False):
        """Per-section filter/sort descriptors, retargeted from the template.

        Every key in the captured blob names the section it came from, in Type
        keys, Sort firstCharacterKeys and Filter keys alike, so the retarget is
        a rewrite of the whole document rather than of the three fields that
        happen to be at the top.

        `advanced` is the client's `includeAdvanced=1`, which plexsim ignored: a real
        PMS then adds six Sort entries to a movie library, four to a show, three to a
        season and eight to an episode view, plus the `Duplicates` filter. The client
        reads that Sort list straight into its sort menu, so ignoring the flag cost
        the menu most of its options.
        """
        kind = "show" if section_type == "show" else "movie"
        template = SECTION_META.get(kind)
        if not template:
            return None
        blob = json.dumps(template)
        blob = re.sub(r"/library/sections/\d+/", "/library/sections/%s/" % section_id, blob)
        document = json.loads(blob)
        if advanced:
            for entry in document.get("Type") or []:
                extra = ADVANCED_META.get((kind, entry.get("type")))
                if not extra:
                    continue
                for name, rows in extra.items():
                    for row in rows:
                        copy = dict(row)
                        if "%s" in str(copy.get("key", "")):
                            copy["key"] = copy["key"] % section_id
                        if "%s" in str(copy.get("firstCharacterKey", "")):
                            copy["firstCharacterKey"] = \
                                copy["firstCharacterKey"] % section_id
                        entry.setdefault(name, []).append(copy)
                order = ADVANCED_SORT_ORDER.get(entry.get("type"))
                if order and entry.get("Sort"):
                    # Measured: the advanced sorts are INTERLEAVED with the plain
                    # ones -- Year is second on a movie library, not after Randomly
                    # -- and the client reads the list straight into its sort menu.
                    entry["Sort"].sort(key=lambda s: order.index(s["key"])
                                       if s.get("key") in order else len(order))
        return document

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

    def handle(self, method, path, body, headers, address=None):
        if path.startswith("//"):
            # A leading empty segment is a 404 like any other (`//identity`,
            # measured) -- and it has to be caught before urlparse, which reads
            # `//identity` as a HOST named identity with an empty path.
            return ("error", 404, "plexsim: not found")
        parsed = urllib.parse.urlparse(path)
        route = parsed.path
        if not route.startswith("/web"):
            # Measured: an EMPTY interior segment (`//identity`, `/library//sections`)
            # is a 404, where plexsim dropped it and served the page; one TRAILING
            # slash is harmless (`/library/sections/` is the listing, which plexsim
            # 404'd).
            if "//" in route:
                return ("error", 404, "plexsim: not found")
            if len(route) > 1 and route.endswith("/"):
                route = route[:-1]
            # Measured: a query parameter with no `=` at all -- `?includeGuids`,
            # `?foo`, even `?&` -- is a 400 on every path, `/identity` included.
            # plexsim read a bare `includeGuids` as true.
            if parsed.query and any("=" not in piece for piece in parsed.query.split("&")):
                return ("error", 400, "plexsim: bad request")
        # Both shapes are needed. The dict is what every endpoint reads, but a
        # filter query is ORDERED and may repeat a key -- `?genre=6&genre=23` is
        # an intersection and `push`/`pop` are positional -- so the pairs are
        # carried alongside rather than collapsed away.
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = {k: v for k, v in pairs}
        query["_pairs"] = pairs
        # Container paging arrives as query parameters from the Backbone paths and
        # as HTTP HEADERS from the React ones (`fetchRange` sets
        # `headers:{"X-Plex-Container-Start":…,"X-Plex-Container-Size":…}`). A real
        # PMS honours either, and a mix of the two. plexsim read only the query
        # string, so every windowed grid request got the whole library and no
        # `totalSize` -- the same shape of bug as the missing Seasons block, one
        # transport over. Normalised here so every listing sees one form.
        for name in ("X-Plex-Container-Start", "X-Plex-Container-Size"):
            if name not in query and headers.get(name) is not None:
                query[name] = headers.get(name)
        segments = [s for s in route.split("/") if s]

        # Measured: an id of 2^31 or more is a 400 on the families that parse theirs
        # as a 32-bit integer, where metadata and sections answer 404; and a play
        # queue id that is not a number at all is a 400.
        for prefix in (["playlists"], ["playQueues"], ["library", "collections"],
                       ["devices"], ["status", "sessions", "history"],
                       ["library", "parts"], ["library", "streams"]):
            if segments[:len(prefix)] == prefix and len(segments) > len(prefix):
                ident = segments[len(prefix)]
                if ident.isdigit() and int(ident) >= 2 ** 31:
                    return ("error", 400, "plexsim: bad request")
        if segments[:1] == ["playQueues"] and len(segments) > 1 and not segments[1].isdigit():
            return ("error", 400, "plexsim: bad request")

        # A Part read is not a container response; it returns a streaming
        # sentinel the handler turns into a body. Only for the READ verbs: the
        # same prefix with PUT is track selection, which is a normal endpoint.
        if route.startswith("/library/parts/") and method in ("GET", "HEAD"):
            # Measured: only `/library/parts/{id}/{version}/{file}` is the media. A
            # non-numeric version, anything deeper, and the BIF thumbnail index
            # (`/indexes/sd/{offset}`) are 404s -- plexsim handed all of them to the
            # part reader, so a client asking for preview thumbnails was sent the
            # whole video. A wrong numeric version or file name still serves, and so
            # do the bare part and a part with no file name.
            if len(segments) > 5 or (len(segments) == 5 and not segments[3].isdigit()):
                return ("error", 404, "plexsim: not found")
            return ("stream", segments, query)

        try:
            return self._route(method, route, segments, query, body, headers, address)
        except filters_module.FilterError as refused:
            # A query a real PMS refuses, with the status IT answers: 400 for a
            # malformed key, 500 for a value it cannot parse. Answering 200 with
            # the library unfiltered is the trap -- the caller cannot tell its
            # filter did nothing.
            logger.warning("REFUSED %s: %s", route, refused.detail)
            return ("error", refused.status, "plexsim: %s" % refused.detail)

    def _route(self, method, route, segments, query, body, headers, address=None):
        catalog = self.catalog

        # -- identity / capability ---------------------------------------
        if route == "/":
            return self.server_root()
        if route == "/identity":
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
            # Measured: a real PMS routes per (method, path) and NOT uniformly --
            # /identity and /hubs answer any verb, while these three refuse, each
            # with its own status. plexsim answered the GET body to every verb.
            if method == "POST":
                return ("error", 400, "plexsim: bad request")
            if method in ("PUT", "DELETE"):
                return ("error", 404, "plexsim: not found")
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
            if method != "GET":
                # Measured: a POST here is a 404 -- plexsim answered the whole
                # Setting list to any verb, so a client that posted by mistake got
                # a success and the settings page never showed an error.
                return ("error", 404, "plexsim: not found")
            settings = self.server_prefs()
            return self.bare_container(size=len(settings), Setting=settings)

        # -- system ------------------------------------------------------
        if (segments[:2] == ["system", "agents"] and len(segments) == 5
                and segments[3] == "config"):
            # `/system/agents/{identifier}/config/{mediaType}` -- the contributor
            # chain the Agents page shows under a primary agent. Measured on BOTH
            # real servers: for a modern agent (and for an unknown one) it is an
            # EMPTY `com.plexapp.system` container -- only the legacy plug-ins have
            # a chain. A mediaType no agent serves is a 404 and a non-number a 500.
            if method == "PUT":
                rows = [{"name": agent.get("name"), "enabled": True, "hasPrefs": False,
                         "identifier": agent.get("identifier")}
                        for agent in SYSTEM_AGENTS
                        if agent.get("identifier") in AGENT_MEDIA_TYPES.get(segments[4], [])]
                return self.bare_container(size=len(rows), noHistory=0, replaceParent=0,
                                           identifier="com.plexapp.system", Agent=rows)
            if not segments[4].lstrip("-").isdigit():
                return ("error", 500, "plexsim: internal error")
            if str(int(segments[4])) not in AGENT_MEDIA_TYPES:
                return ("error", 404, "plexsim: not found")
            return self.bare_container(noHistory=0, replaceParent=0, size=0,
                                       identifier="com.plexapp.system")
        if route == "/system/agents/contributors":
            # Same measurement: empty for a modern primary agent, and a 500 when the
            # two parameters the plug-in framework needs are missing.
            if not query.get("mediaType") or not query.get("primaryAgent"):
                return ("error", 500, "plexsim: internal error")
            return self.bare_container(noHistory=0, replaceParent=0, size=0,
                                       identifier="com.plexapp.system")
        if route in ("/system/agents/attribution", "/system/agents/attribution/image"):
            # Measured: a modern agent has no attribution, text or image, and says so
            # with a 404; with no identifier at all it is a 500. plexsim answered the
            # JSON agent list to both, which the Agents page fed to an <img>.
            if not query.get("identifier"):
                return ("error", 500, "plexsim: internal error")
            return ("error", 404, "plexsim: not found")
        if route.startswith("/system/agents"):
            return self.system_agents(query)
        if segments[:2] == ["system", "scanners"]:
            if len(segments) == 2:
                # Measured: the bare collection is a 404 -- scanners are listed
                # per section. plexsim answered the movie list to it.
                return ("error", 404, "plexsim: not found")
            return self.system_scanners(segments[2])
        if segments[:3] == ["system", "library", "sections"]:
            # The system view of the libraries, which the Agents page reads to
            # know which sections an agent can be configured for. Measured: a
            # `com.plexapp.system` container of Directory rows.
            rows = [{"key": section["key"], "type": section.get("type"),
                     "title": section.get("title"),
                     "agent": ("tv.plex.agents.series"
                               if section.get("type") == "show"
                               else "tv.plex.agents.movie")}
                    for section in catalog.sections]
            return self.bare_container(size=len(rows),
                                       identifier="com.plexapp.system",
                                       Directory=rows)
        if route.startswith("/system/appstore"):
            # Measured: a real PMS 404s the appstore paths -- the channel store
            # is gone, and the client's Plugins page copes with that.
            return ("error", 404, "plexsim: not found")
        if route.startswith("/:/plugins"):
            # The Plugins page. A real PMS lists its built-in agents here;
            # plexsim has no plug-in host, so the honest answer is none.
            if route in ("/:/plugins", "/:/plugins/"):
                return self.bare_container(size=0, Plugin=[])
            # Measured: `/:/plugins/<id>/prefs` is a 404 for a modern agent and for
            # an unknown identifier alike (only a hosted legacy plug-in has one),
            # and so is `/:/plugins/<id>`. plexsim answered an empty Plugin list to
            # every path under it -- the Edit Library dialog asks unconditionally.
            return ("error", 404, "plexsim: not found")
        if route == "/system/services/url/lookup":
            return self.bare_container(size=0)

        if segments[:1] == ["accounts"]:
            return self.accounts(segments[1] if len(segments) > 1 else None)
        if segments[:1] == ["devices"] or route == "/devices.xml":
            if method == "DELETE":
                return ("empty", 200)
            if method == "POST" and len(segments) == 1:
                # Measured: the legacy plug-in dispatcher answers this one.
                return self.bare_container(size=0, content="plugins")
            if method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            if len(segments) == 2 and route != "/devices.xml":
                # `/devices/{id}`: one row, and a 404 for an id the server never
                # assigned -- including a non-number.
                device = self.sessions.device(segments[1])
                if device is None:
                    return ("error", 404, "plexsim: not found")
                return self.bare_container(size=1, identifier="com.plexapp.system.devices",
                                           Device=[device])
            return self.devices()
        if route == "/clients" or route.startswith("/clients/profiles"):
            # No Plex client is ever *hosted* by plexsim, so this is empty for
            # the same reason a headless PMS's is.
            return self.bare_container(size=0)

        if segments[:1] == ["statistics"]:
            return self.statistics(segments[1] if len(segments) > 1 else "", query)

        if route == "/updater/status":
            return self.updater_status()
        if route in ("/updater/check", "/updater/apply"):
            # Measured: only the PUT of `check` exists. A GET of either is a 404,
            # and a PUT of `apply` with nothing downloaded is a 400 -- plexsim
            # answered an empty 200 to all four, which reads as "the update is
            # being installed".
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            if route == "/updater/apply":
                logger.warning("UPDATER apply requested -- nothing downloaded")
                return ("error", 400, "plexsim: bad request")
            return ("empty", 200)

        if segments[:1] == ["activities"]:
            if method == "DELETE" and len(segments) > 1:
                # Measured: cancelling an activity that is not running is a 404.
                if not self.library.cancel_activity(segments[1]):
                    return ("error", 404, "plexsim: no such activity")
                return ("empty", 200)
            if len(segments) > 1:
                # Measured: an activity has no read of its own -- `GET /activities/{id}`
                # is a 404, for a live uuid on fun as well. plexsim answered the list.
                return ("error", 404, "plexsim: not found")
            activities = self.library.activities()
            return self.bare_container(size=len(activities), Activity=activities)

        if segments == ["status", "sessions", "history"]:
            # Measured: only `/all` and a historyKey exist; the bare path is a 404.
            return ("error", 404, "plexsim: not found")
        if segments[:3] == ["status", "sessions", "history"] and len(segments) == 4:
            if method == "DELETE":
                return self.delete_history(segments[3])
            if segments[3] == "all":
                return self.history(query)
            # Measured: only `all` and a numeric historyKey exist here.
            return ("error", 404, "plexsim: not found")
        if route == "/status/sessions/terminate":
            # Measured: keyed on `Session.id` (not sessionKey), 400 without one,
            # 404 for an id that is not live, and the kill itself is delivered
            # through that session's NEXT timeline rather than in this response.
            session_id = query.get("sessionId")
            if not session_id:
                return ("error", 400, "plexsim: bad request")
            if not self.sessions.terminate(session_id, query.get("reason")):
                return ("error", 404, "plexsim: no such session")
            logger.info("TERMINATE %s: %s", session_id, query.get("reason"))
            return ("empty", 200)
        if route == "/status/sessions/background":
            return self.bare_container(size=0)
        if route.startswith("/status/sessions"):
            if method in ("POST", "PUT", "DELETE"):
                # Measured: all three are 404 here. plexsim answered the session
                # listing to any verb.
                return ("error", 404, "plexsim: not found")
            rows = self.session_rows()
            page, paged, start, total = self.page_window(rows, query)
            fields = {"size": len(page)}
            if paged:
                fields["totalSize"] = total
                fields["offset"] = start
            if page:
                fields["Metadata"] = page
            return self.bare_container(**fields)
        if segments[:2] == ["transcode", "sessions"]:
            if len(segments) > 2:
                # Measured: an id that is not a live transcode session is a 404,
                # for GET and DELETE alike. plexsim never opens one, so that is
                # every id.
                return ("error", 404, "plexsim: no such transcode session")
            if method not in ("GET", "HEAD"):
                # Measured: DELETE and POST on the bare collection are 404s. plexsim
                # answered 200 to both.
                return ("error", 404, "plexsim: not found")
            return self.bare_container(size=0)

        if route == "/security/token":
            # The client asks for a delegation token before handing a URL to
            # another device. plexsim has no second party to delegate to, but the
            # shape is what the caller reads. Measured: a request with no `type`
            # and no `scope` is a 400, and each call answers a DIFFERENT token --
            # plexsim answered a constant, so two devices shared one credential.
            if not query.get("type") or not query.get("scope"):
                return ("error", 400, "plexsim: bad request")
            if self.token:
                given = (headers or {}).get("X-Plex-Token") or query.get("X-Plex-Token")
                if given != self.token:
                    # Measured: 403 here, not the 401 every other path answers.
                    return ("error", 403, "plexsim: forbidden")
            return self.bare_container(size=0, token="transient-" + str(uuid.uuid4()))
        if segments[:1] == ["downloadQueue"]:
            return self.download_queue(method, segments, query, headers)
        if route == "/channels":
            # Measured 200 on both servers with exactly these two rows. The channel
            # APPSTORE paths do 404 (which CLAUDE.md already records) -- bare
            # /channels does not, and plexsim 404'd it.
            return self.bare_container(size=2, Directory=[
                {"key": "all", "title": "All"},
                {"key": "recentlyViewed", "title": "Recently Viewed"}])
        if route == "/channels/all":
            # Measured 200 on the reference with nothing but `size` and
            # `content="plugins"`; plexsim 404'd it.
            return self.bare_container(size=0, content="plugins")
        if route == "/services/browse" or route.startswith("/services/browse/"):
            return self.browse(route, query)
        if route == "/media/grabbers":
            # Measured: four grabbers, verbatim. plexsim answered an empty
            # container, which tells the Live TV setup page the server has no
            # tuner protocols at all rather than no tuners.
            return self.bare_container(size=len(MEDIA_GRABBERS),
                                       MediaGrabber=MEDIA_GRABBERS)
        if (segments[:1] == ["media"] and len(segments) > 1
                and segments[1] in ("grabbers", "subscriptions")
                and len(segments) > 2):
            # Measured: every child of these two is a 404 -- a named grabber, a
            # grabber's devices, one subscription. plexsim answered empty
            # containers, so the Live TV pages drew controls for things that do
            # not exist. The two collection roots themselves are real.
            # `/media/grabbers/devices` is the tuner-device list and is real (an
            # empty container); a NAMED grabber, and that grabber's own /devices,
            # are both 404.
            if segments[1] == "grabbers" and segments[2] == "devices" \
                    and len(segments) == 3:
                return self.bare_container(size=0)
            if not (segments[1] == "subscriptions" and segments[2] == "scheduled"):
                return ("error", 404, "plexsim: not found")
        if route == "/security/resources":
            return ("error", 400, "plexsim: bad request")

        if route in ("/logs", "/diagnostics"):
            # Not the zip -- measured: a container with a `content` string, and
            # `/diagnostics` answers the same. plexsim 404'd the latter.
            return self.bare_container(size=0, content="plugins")
        if route == "/log/networked":
            # The networked-logging endpoint a companion client posts to. Measured
            # 200 with an empty body on both verbs; plexsim 404'd it.
            return ("empty", 200)
        if route == "/resources":
            # Measured: an empty container, not a 404. It is the device-resource
            # list a companion client reads, and plexsim has no devices of its own.
            return self.bare_container(size=0)
        if route == "/log":
            # The web client posts its OWN errors here. Answering with a
            # container is wrong (a real PMS answers empty), and dropping them
            # means the client's logging silently goes nowhere -- so they land in
            # plexsim's log, where Download Logs will hand them back.
            # Measured: `level` must be an integer AND `message` must be present,
            # on every verb -- either alone, or a non-numeric level, is a 400.
            level = query.get("level") or ""
            if not level.lstrip("-").isdigit() or "message" not in query:
                return ("error", 400, "plexsim: bad request")
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
        if route in ("/livetv/epg/lineup", "/livetv/epg/channelmap",
                     "/livetv/epg/lineupchannels"):
            # Measured on two servers with NO tuner: these three are 400, not the
            # blanket empty container plexsim answered for the whole /livetv tree.
            # (`/livetv/epg/countries` and `/languages` are real there -- 97 and 594
            # rows -- and plexsim answers them empty, which is honest for a server
            # with no EPG provider and is recorded as a deviation rather than faked.)
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
            if method in ("PUT", "DELETE"):
                # Measured: 404 for both, where a POST with a bad form is a 400.
                return ("error", 404, "plexsim: not found")
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
            return self.library_tags(query)
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
                if segments[2] == "prefs" and len(segments) == 3 \
                        and method in ("GET", "HEAD"):
                    return self.new_section_prefs(query)
                if segments[2] == "prefs":
                    # `/library/sections/prefs?type=movie` and friends: a 400.
                    return ("error", 400, "plexsim: bad request")
                # Measured: any other non-numeric section id -- `abc`, `-1`, `1.5` --
                # is a 404, not the 400 plexsim answered.
                return ("error", 404, "plexsim: not found")
            return self.library_section(segments, query, method)

        if segments[:2] == ["library", "metadata"] and len(segments) >= 3:
            return self.library_metadata(segments, query, method)

        if route == "/library/onDeck":
            # Measured: this is the server-wide Continue Watching list -- every row
            # carries a `viewOffset` and a `lastViewedAt`, and it is EMPTY on a
            # server with nothing in progress. plexsim answered the first twelve
            # catalog rows with no view state at all, which is what every non-web
            # client's On Deck reads (plexapi, Kodi/PKC, the legacy apps).
            rows = [_listing_row(r) for r in self.continue_watching()]
            # Measured on both lists: `mixedParents`, because the rows come from
            # more than one library.
            return self.container(Metadata=rows, mixedParents=True)
        if route == "/library/recentlyAdded":
            rows = sorted(catalog.items.values(), key=lambda r: -(r.get("addedAt") or 0))
            rows = [r for r in rows if r.get("type") in ("movie", "season", "episode")]
            return self.container(
                Metadata=[_listing_row(r) for r in rows[:self.HUB_COUNT]],
                mixedParents=True)

        if route == "/library/all":
            rows = [_listing_row(r) for r in catalog.items.values()]
            return self.container(Metadata=rows)

        if route == "/library/all/top":
            # Most played first -- what the Home screen's "Top" shelf reads.
            # Measured: `type=` is REQUIRED (a real PMS answers 400 without it),
            # and the counts come out of the play HISTORY rather than off the row.
            # Big Buck Bunny reported `globalViewCount: 1` with no `viewCount` at
            # all, because it had been played and then marked unplayed: the row's
            # counter is current state, this shelf is the history.
            wanted = {"1": "movie", "2": "show", "4": "episode"}.get(query.get("type"))
            if wanted is None:
                return ("error", 400, "plexsim: bad request")
            plays, viewers = {}, {}
            for entry in self.library.history():
                key = entry.get("ratingKey")
                row = catalog.items.get(int(key)) if str(key).isdigit() else None
                if row is None or row.get("type") != wanted:
                    continue
                plays[int(key)] = plays.get(int(key), 0) + 1
                viewers.setdefault(int(key), set()).add(entry.get("accountID") or 1)
            ordered = sorted(plays, key=lambda k: (-plays[k], k))
            limit = int(query.get("limit", 12) or 12)
            rows = []
            for key in ordered[:limit]:
                row = _listing_row(catalog.items[key])
                row["globalViewCount"] = plays[key]
                row["userCount"] = len(viewers[key])
                # Who watched it, which is what the Home shelf's avatar reads.
                row["User"] = [{"id": account} for account in sorted(viewers[key])]
                rows.append(row)
            return self.container(Metadata=rows)

        if segments[:2] == ["library", "streams"]:
            # Sidecar subtitle extraction. plexsim never repackages, so every
            # subtitle stays embedded in the part and there is nothing to serve.
            # Measured: a real PMS answers 501 here for a stream it cannot
            # extract, which is what "not implemented" honestly means -- but only
            # for a stream it HAS. An unknown or non-numeric id, and the bare path,
            # are 404s, and a PUT to an unknown one is a 400. plexsim 501'd them all.
            stream_id = segments[2] if len(segments) > 2 else ""
            known = stream_id.isdigit() and any(
                str(stream.get("id")) == stream_id
                for row in catalog.items.values() for media in row.get("Media") or []
                for part in media.get("Part") or [] for stream in part.get("Stream") or [])
            if not known:
                if method == "PUT" and len(segments) == 3:
                    return ("error", 400, "plexsim: bad request")
                return ("error", 404, "plexsim: not found")
            return ("error", 501, "plexsim: streams are embedded in the part")

        if segments[:2] == ["library", "parts"] and method == "PUT":
            # Audio/subtitle track selection from the player or the Get Info
            # dialog: PUT /library/parts/{partID}?subtitleStreamID=&allParts=1.
            return self.select_stream(segments[2] if len(segments) > 2 else "", query)

        if route == "/library/search":
            # `universalsearch`, which plexsim advertised in /media/providers and
            # answered with an empty container -- on the note that "a real PMS
            # answers the bare query with an empty container". Measured: a real PMS
            # answers a bare query with a 400 and a real one with SearchResult rows.
            return self.library_search(query)
        if route == "/search":
            # The legacy search endpoint, which also lists the search PROVIDERS.
            return self.legacy_search(query)

        # -- hubs --------------------------------------------------------
        if route == "/hubs":
            return self.hubs(query)
        if route == "/hubs/metadata":
            return ("error", 404, "plexsim: not found")
        if segments[:2] == ["hubs", "metadata"] and len(segments) == 4:
            return self.item_hubs(segments[2], segments[3], query)
        if route == "/hubs/continueWatching":
            # Measured: this hub's own identifier is `continueWatching` (not the
            # home screen's `home.continue`), its key is its OWN items endpoint,
            # and its style is `hero`. plexsim answered a bare hub whose key was
            # /library/all, so the shelf had nowhere to expand to.
            rows = [_listing_row(r) for r in self.continue_watching()]
            return self.hub_container(Hub=[self.home_hub(
                "continueWatching", "Continue Watching", "/hubs/continueWatching/items",
                rows, query, kind="mixed", style="hero")])
        if route == "/hubs/continueWatching/items":
            rows = [_listing_row(r) for r in self.continue_watching()]
            return self.hub_container(size=len(rows), Metadata=rows)
        if route == "/hubs/promoted":
            return self.promoted_hubs(query)
        if route.startswith("/hubs/home"):
            if route == "/hubs/home/continueWatching":
                rows = [_listing_row(r) for r in self.continue_watching()]
                return self.hub_container(size=len(rows), Metadata=rows)
            if route == "/hubs/home/onDeck":
                rows = [_listing_row(r) for r in self.continue_watching()
                        if r.get("type") == "episode"]
                return self.hub_container(size=len(rows), Metadata=rows)
            if route == "/hubs/home/recentlyAdded":
                # Measured: this endpoint keys on `type`, NOT on `sectionID` --
                # `?sectionID=1` alone is empty and `?type=2` alone answers for
                # every show library. plexsim had it the other way round, so the
                # TV shelf's "see all" opened an empty page.
                rows = self.recently_added_rows(query.get("type"), query.get("sectionID"))
                if rows is None:
                    return self.hub_container(size=0)
                page = [_listing_row(r) for r in rows[:self.HUB_COUNT]]
                return self.hub_container(size=len(page), Metadata=page)
            # Measured: a home hub a real PMS does not have is a 404, not the
            # empty container plexsim answered -- which a client reads as "this
            # shelf exists and is empty".
            return ("error", 404, "plexsim: not found")
        if segments[:2] == ["hubs", "sections"] and len(segments) == 3:
            return self.section_hubs(segments[2], query.get("count"))
        if (segments[:2] == ["hubs", "sections"] and len(segments) == 5
                and segments[3] == "continueWatching"):
            # The Continue Watching shelf's own "see all" list.
            rows = [_listing_row(r) for r in self.continue_watching()
                    if str(r.get("librarySectionID")) == str(segments[2])]
            return self.container(Metadata=rows)
        if route == "/hubs/search":
            return self.search(query)
        if route == "/hubs/search/voice":
            return self.voice_search(query)


        # -- playback ----------------------------------------------------
        if route == "/playQueues" and method == "POST":
            return self.create_play_queue(query)
        if route == "/playQueues":
            # Re-measured, and it splits on ONE parameter: a bare GET (or one with
            # any parameter but `type`) is an empty 200, and `?type=` is a 400 --
            # naming a type without creating anything is the malformed request. The
            # earlier finding saw only the `?type=1` half.
            if query.get("type"):
                return ("error", 400, "plexsim: bad request")
            return self.bare_container(size=0)
        if segments[:1] == ["playQueues"] and len(segments) == 2 and method == "GET":
            return self.get_play_queue(segments[1], query)
        if segments[:1] == ["playQueues"] and len(segments) >= 2:
            return self.play_queue_route(method, segments, query)

        if route == "/video/:/transcode/universal/decision":
            return self.decision(query, headers)
        if route.startswith("/video/:/transcode/universal/start"):
            # plexsim has no encoder. A 404 here is the honest answer and it is
            # also the signal that the client's own gate rejected direct play --
            # but only for a request that names something playable. Measured: the
            # start family validates exactly as the decision does, and a request
            # with no parameters, an unknown path or a bad mediaIndex is a 400.
            _row, refusal = self._decision_target(query, headers)
            if refusal:
                return refusal
            logger.warning("TRANSCODE REQUESTED -- client refused direct play: %s", query.get("path"))
            return ("error", 404, "plexsim: no transcoder")
        if route == "/subtitles/:/transcode/universal/start":
            # The URL Plex Web uses for a <track> src when it CAN direct play --
            # which is plexsim's only path. Measured 400 on both reference servers,
            # including for a real embedded SRT; plexsim let it fall into the
            # catch-all 404, which is a different answer for the same condition.
            return ("error", 400, "plexsim: bad request")
        if route == "/video/:/transcode/universal/subtitles":
            # Measured: a real PMS answers 400 here for an unknown session, where
            # the rest of the universal/* family answers 404.
            return ("error", 400, "plexsim: bad request")
        if route in ("/video/:/transcode/universal/ping",
                     "/video/:/transcode/universal/stop") and not query.get("session"):
            # Measured: with no `session` these are 400s; an unknown one is a 404.
            return ("error", 400, "plexsim: bad request")
        if route.startswith("/video/:/transcode/universal/"):
            # /ping, /stop, /session: all of it belongs to a transcode session,
            # and plexsim never opens one.
            return ("error", 404, "plexsim: no transcoder")
        if route == "/photo/:/transcode":
            # Measured: both dimensions are MANDATORY and a real PMS 400s the request
            # without them, 400s a non-numeric one, and 404s a `url` naming a row it
            # does not have. plexsim answered a 200 grey tile to all four, so a client
            # that would have drawn its own title-card placeholder drew -- and could
            # cache -- plexsim's instead.
            if not query.get("url"):
                return ("error", 400, "plexsim: bad request")
            for name in ("width", "height"):
                if not query.get(name):
                    return ("error", 400, "plexsim: bad request")
                if not str(query[name]).lstrip("-").isdigit():
                    return ("error", 400, "plexsim: bad request")
            inner = urllib.parse.urlparse(urllib.parse.unquote(query["url"])).path
            owner = re.match(r"^/library/metadata/(\d+)/", inner)
            if owner and int(owner.group(1)) not in catalog.items:
                return ("error", 404, "plexsim: no such item")
            # The client asks for artwork through the transcoder, passing the
            # real path in `url`. Resolve that to the row's TMDB image when
            # there is one; only fall back to a generated tile when there is not.
            resolved = self.resolve_artwork(query.get("url", ""))
            if resolved:
                return ("image", self._at_width(resolved, query.get("width")))
            return ("png", query)
        if route.startswith("/:/resources/"):
            # Measured: a real PMS 404s a resource name it does not have -- plexsim
            # generated a tile for ANY name, so a typo'd art path silently drew.
            if route.split("/")[-1] not in KNOWN_RESOURCES:
                return ("error", 404, "plexsim: not found")
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
        if route == "/:/timeline":
            return self.timeline(query, headers, address)
        if route == "/:/progress":
            return self.progress(query)
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

        if len(segments) == 1 and method in ("GET", "HEAD"):
            # Measured on both reference servers: an unknown SINGLE-segment path
            # falls into the legacy plug-in dispatcher and answers a 200 with
            # `content="plugins"`, where a multi-segment one 404s. `/prefs`,
            # `/settings`, `/login` and `/logout` -- all four named in the client
            # bundle -- are 200 there and were 404 here.
            logger.info("PLUGIN PATH %s %s -- legacy dispatcher", method, route)
            return self.bare_container(size=0, content="plugins")

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
                # No `authToken` here: measured against the reference, a real PMS
                # does NOT put its account token in this response, and plexsim did.
                # A page that reads /myplex/account is a page that could log the
                # token, and the field buys nothing -- the client already holds one.
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
        return self.bare_container(size=1,
                                   MediaProvider=self.media_provider_rows(),
                                   **self.server_info())

        return self.bare_container(size=1, MediaProvider=self.media_provider_rows(),
                                   **self.server_info())

    # The 25 top-level Directory rows `/` lists, in the reference's order. The row
    # shape is `{count, key, title}` and the count is 1 on every one.
    ROOT_DIRECTORIES = [
        "actions", "activities", "butler", "channels", "clients", "devices",
        "diagnostics", "downloadQueue", "hubs", "library", "livetv", "media",
        "neighborhood", "playQueues", "playlists", "resources", "search",
        "server", "servers", "service", "statistics", "system", "transcode",
        "updater", "user",
    ]

    def server_root(self):
        """`GET /` -- the server-info document, which is NOT `/identity`.

        plexsim answered `/` with the five-field identity document. Measured, a
        real PMS answers the whole capability blob -- the same ~50 scalars as
        /media/providers -- plus 25 `Directory` rows naming its top-level
        endpoints. python-plexapi's `PlexServer()` loads every attribute it has
        from here, so an identity document leaves it with almost none of them.
        """
        rows = [{"count": 1, "key": name, "title": name}
                for name in self.ROOT_DIRECTORIES]
        info = self.server_info()
        # Measured on BOTH real servers: `/` carries five capability flags that
        # /media/providers does not, and the client's server schema reads all five
        # (`shared && !multiuser` is what disables rating for a shared user).
        info.update(hubSearch=True, itemClusters=True, mediaProviders=True,
                    multiuser=True, transcoderPhoto=True)
        return self.bare_container(size=len(rows), Directory=rows, **info)

    def server_info(self):
        """The capability scalars `/` and `/media/providers` both carry.

        Taken from a real claimed PMS 1.43.4's own /media/providers, which carries
        30 attributes plexsim was missing -- including apiVersion and the myPlex*
        claim-state pair. THIS, not /identity, is where a client reads whether the
        server is signed in; the web app re-requested that endpoint 43 times while
        they were absent, treating an incomplete provider as not-yet-ready. And
        measured since: `GET /` carries the same set, which is where
        python-plexapi reads every attribute it has.

        The transcoder capability flags stay false: plexsim has no encoder, and
        advertising one invites a transcode request it cannot serve.
        """
        return dict(
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
        )

    def media_provider_rows(self):
        """The MediaProvider row, whose Feature list is the capability contract.

        A client will not offer search, playback state or a play queue for a
        provider that does not advertise the matching feature, so an omission here
        looks to the user like a missing feature rather than a protocol error.
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

        return [
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
        ]

    def library_section(self, segments, query, method="GET"):
        section_id = segments[2]
        section = self.catalog.section(section_id)
        if section is None:
            # Measured: every leaf of a section the server does not have is a 404.
            # plexsim answered a complete, valid 200 -- an empty library with a
            # working 28-row filter bar and a 19-row settings pane -- so a deleted
            # library or a stale bookmark rendered as a real one. Fix 4's trap from
            # the inside.
            return ("error", 404, "plexsim: no such section")
        if len(segments) == 3:
            if method not in ("GET", "HEAD", "PUT", "DELETE"):
                # Measured: a POST to a section is a 404; only the edit (PUT) and the
                # delete exist beside the read.
                return ("error", 404, "plexsim: not found")
            if method == "DELETE":
                # Manage > Libraries > Delete Library. Drops the section and
                # every row in it, in memory: catalog.json is untouched.
                if self.catalog.remove_section(section_id) is None:
                    return ("error", 404, "plexsim: no such section")
                logger.info("SECTION delete %s (%s)", section_id, section.get("title"))
                return ("empty", 200)
            if method == "PUT":
                # The Edit Library dialog sends the WHOLE form, and a real PMS
                # validates it: measured 400 for a `name` alone, for a form with no
                # `agent` or no `scanner`, and for an unknown agent, scanner or
                # language. plexsim accepted every one of those with a 200, so a
                # typo in the dialog looked like it had been saved.
                if not query.get("name"):
                    return ("error", 400, "plexsim: bad request")
                if not query.get("agent") or not query.get("scanner"):
                    return ("error", 400, "plexsim: bad request")
                if query["agent"] not in KNOWN_AGENTS:
                    return ("error", 400, "plexsim: bad request")
                if query["scanner"] not in KNOWN_SCANNERS:
                    return ("error", 400, "plexsim: bad request")
                if query.get("language") and query["language"] not in KNOWN_LANGUAGES:
                    return ("error", 400, "plexsim: bad request")
                section["title"] = query["name"]
                logger.info("SECTION edit %s -> %s", section_id, query["name"])
                return ("empty", 200)
            return self.section_pivots(section_id, section, query)

        leaf = segments[3]

        # Measured across 21 section leaves: a real PMS 404s every verb a leaf does
        # not implement, and it never answers 405 anywhere. plexsim answered the
        # GET body to any verb -- so `DELETE /library/sections/1/all` came back 200
        # with the grid, which tells a client the delete was accepted.
        allowed = SECTION_LEAF_VERBS.get(leaf, {"GET", "HEAD"})
        if method not in allowed:
            return ("error", 404, "plexsim: not found")

        # -- the write half of Manage > Libraries -------------------------
        if leaf == "refresh":
            if method == "PUT":
                # Measured: a PUT here is a 404 -- the scan is a GET (or a POST),
                # and the cancel is a DELETE.
                return ("error", 404, "plexsim: not found")
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
                    self.notify_refreshed(row)
                else:
                    self.library.finish_activity(
                        self.library.add_activity(row.get("title") or "", progress=-1))
            self.refreshing.discard(str(section_id))
            logger.info("SECTION refresh %s force=%s -> %d row(s)",
                        section_id, query.get("force") == "1", len(rows))
            return ("empty", 200)
        if leaf in ("emptyTrash", "analyze"):
            if method != "PUT":
                # Measured: a real PMS 404s this rather than answering 405, which is
                # a status it never sends.
                return ("error", 404, "plexsim: not found")
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
                known = {s.get("id"): s for s in settings}
                writes = [(name, value) for name, value in query.items()
                          if not name.startswith("X-Plex-") and name != "_pairs"]
                # Measured: an unknown setting is a 400 and NOTHING in the request
                # is applied, even the known settings beside it. plexsim skipped the
                # unknown one and answered 200.
                if any(name not in known for name, _ in writes):
                    return ("error", 400, "plexsim: bad request")
                for name, value in writes:
                    kind = known[name].get("type")
                    if kind == "int":
                        # Measured: a value that is not an integer -- `abc`, `4.5`,
                        # empty -- is IGNORED with a 200. `-1` and an out-of-enum `7`
                        # are integers and are stored.
                        try:
                            value = str(int(value))
                        except ValueError:
                            continue
                    elif kind == "bool":
                        # Measured: exactly `1` and `true` store "true"; `TRUE`,
                        # `yes`, `0` and an empty value all store "false".
                        value = "true" if value in ("1", "true") else "false"
                    self.section_pref_overrides[(str(section_id), name)] = value
                    logger.info("SECTION PREF %s %s = %s", section_id, name, value)
                    if name == "flattenSeasons":
                        # This one changes the ROWS, not just the setting list.
                        self.catalog.apply_flatten_seasons(section_id, value)
                return ("empty", 200)
            if query.get("agent"):
                # Measured: `?agent=` RESHAPES an existing library's list exactly as
                # it shapes a new one's -- `tv.plex.agents.none` gives a movie
                # library 12 settings, a legacy agent 7 -- keeping the library's own
                # values for the settings it has. The Edit Library dialog re-reads
                # the form with it whenever the agent dropdown changes.
                type_id = "2" if section.get("type") == "show" else "1"
                current = {s.get("id"): s for s in settings}
                settings = self.section_pref_rows(type_id, query["agent"], current)
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
            if method == "PUT":
                # THE Edit dialog's save path (`MultiMetadataModel.saveUrl`), and
                # plexsim answered the listing to it -- so every Save applied
                # nothing and the dialog reported success, because the status was
                # 200. Measured: `id=` scopes it to those rows, and with no `id`
                # the whole FILTERED set is edited; no `type` is a 400, and the
                # container paging parameters are ignored on a write.
                if not query.get("type"):
                    return ("error", 400, "plexsim: bad request")
                keys = [int(k) for k in (query.get("id") or "").split(",")
                        if k.strip().isdigit()]
                if keys:
                    rows = [self.catalog.items[k] for k in keys
                            if k in self.catalog.items]
                else:
                    rows = self.typed_rows(section_id, query)
                    rows, _ = self.apply_filters(rows, query, section)
                return self.apply_edit(rows, query, bare_fields=False,
                                       metadata_state="queued")
            return self.section_listing(section_id, section, query)
        if leaf == "common":
            if method != "GET":
                return ("error", 404, "plexsim: not found")
            return self.common_fields(section_id, section, query)
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

        if leaf == "query":
            # Measured: only the `parse` child exists -- `/query` alone is a 404,
            # a POST to `/query/parse` is a 404, and so is any section id the
            # server does not have.
            if len(segments) != 5 or segments[4] != "parse" or method != "GET":
                return ("error", 404, "plexsim: not found")
            if not section:
                return ("error", 404, "plexsim: not found")
            return self.query_parse(section, query)

        # -- the filter and sort descriptors the legacy UI reads -----------
        if leaf == "sorts":
            # Measured: the list is the TYPE's -- `?type=3` on a show library is the
            # two season sorts, `?type=4` the episode list -- and `includeAdvanced=1`
            # adds the advanced ones in their interleaved places, exactly as the
            # section's Meta carries them minus its `active` markers. plexsim
            # answered the movie list to every library and ignored both parameters.
            meta = self.section_meta(section_id, section.get("type"),
                                     advanced=query.get("includeAdvanced") == "1") or {}
            names = {"1": "movie", "2": "show", "3": "season", "4": "episode"}
            wanted = names.get(query.get("type") or "",
                               "show" if section.get("type") == "show" else "movie")
            sorts = []
            for entry in meta.get("Type") or []:
                if entry.get("type") == wanted:
                    sorts = [{k: v for k, v in s.items()
                              if k not in ("active", "activeDirection")}
                             for s in entry.get("Sort") or []]
            return self.secondary_container(section_id, section, None,
                                            Directory=sorts)
        if leaf == "filters":
            entries = (SECTION_FILTERS.get(section.get("type"))
                       or SECTION_FILTERS["movie"])
            filters = [
                {
                    "filter": name,
                    "filterType": kind,
                    "key": "/library/sections/%s/%s%s" % (section_id, name, suffix),
                    "title": title,
                    "type": "filter",
                }
                for name, kind, title, suffix in entries
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
            return self.secondary_directory(section_id, section, leaf, query)
        if leaf in SECONDARY_MEDIA:
            return self.media_axis(section_id, section, leaf, query)
        if leaf == "location":
            return self.location_axis(section_id, section)

        if leaf in ("unwatched", "recentlyReleased"):
            # Listing shortcuts the hub shelves link to. Measured: `/unwatched` is
            # a LISTING with title2 "Unplayed", not a secondary axis -- the
            # `/filters` entry beside it advertises a boolean the grid applies.
            rows = self.typed_rows(section_id, query)
            extra = {}
            if leaf == "unwatched":
                rows = [r for r in rows if not r.get("viewCount")]
                extra["title2"] = SECONDARY_TITLES["unwatched"]
            else:
                rows = sorted(rows, key=lambda r: str(r.get("originallyAvailableAt") or ""),
                              reverse=True)
            return self.listing_container(
                section_id, section, [_listing_row(r) for r in rows], **extra)
        if leaf == "search":
            # Measured again, more carefully than the first time: only the form
            # with NO `type` is a 400. With a type this is a real listing, whose
            # title2 is "Search for '<query>'" -- it is the in-library search box,
            # and plexsim refused every form of it.
            if not query.get("type"):
                return ("error", 400, "plexsim: bad request")
            rows = self.typed_rows(section_id, query)
            text = (query.get("query") or "").strip().lower()
            if text:
                # Measured: this is a SUBSTRING match on the title, not the word-
                # prefix matcher /hubs/search uses, and ONE character is enough --
                # `?query=ear` finds "Tears of Steel" and `?query=a` finds three
                # rows. plexsim routed it through the hub matcher, so the in-library
                # search box found nothing that did not start a word.
                rows = [r for r in rows
                        if text in str(r.get("title") or "").lower()
                        or text in str(r.get("originalTitle") or "").lower()]
            found = self.secondary_container(
                section_id, section, "Search for '%s'" % text,
                Metadata=[_listing_row(r) for r in rows],
                viewGroup=section.get("type"))
            # Measured: this one is NOT a secondary view -- it carries no
            # `content`, and its viewGroup is the library's type.
            found["MediaContainer"].pop("content", None)
            return found
        if leaf == "composite":
            art = self.composite_for("section", section_id)
            if art:
                return ("image", self._at_width(art, query.get("width")))
            return ("png", query)

        # A leaf a real PMS does not have is a 404 there, not an empty container.
        logger.warning("UNKNOWN section leaf %s on section %s", leaf, section_id)
        return ("error", 404, "plexsim: not found")

    def typed_rows(self, section_id, query):
        """The rows a listing or a secondary axis is over, scoped by `?type=`.

        Measured: `/library/sections/1/genre?type=2` is an EMPTY axis on a movie
        library, so the type selects rows rather than being ignored -- and a show
        library's filter keys all carry `?type=2`, so an axis that ignored it
        would list the wrong type's tags on every show grid.
        """
        if len({value for name, value in query.get("_pairs") or [] if name == "type"}) > 1:
            # Measured: repeated parameters AND, and a row has one type, so
            # `?type=2&type=1` selects nothing. plexsim let the last one win.
            return []
        wanted = {"1": "movie", "2": "show", "3": "season",
                  "4": "episode"}.get(str(query.get("type") or ""))
        if wanted is None:
            return self.catalog.section_items(section_id)
        rows = [r for r in self.catalog.items.values()
                if r.get("type") == wanted
                and str(r.get("librarySectionID")) == str(section_id)]
        if wanted in ("season", "episode"):
            rows.sort(key=lambda r: (r.get("grandparentTitle") or r.get("parentTitle") or "",
                                     r.get("parentIndex") or 0, r.get("index") or 0))
        return rows

    # `GET /library/sections/{id}` -- the PIVOT list, captured per library type.
    # plexsim answered two rows ("All", "Recently Added"); a real PMS answers 20 for
    # a movie library and 16 for a show one, each with the title the sidebar prints,
    # `secondary` on the axes and `search`/`prompt` on the search entries.
    SECTION_PIVOTS = {
        "movie": [
            {"key": "all", "title": "All Movies"},
            {"key": "unwatched", "title": "Unwatched"},
            {"key": "newest", "title": "Recently Released"},
            {"key": "recentlyAdded", "title": "Recently Added"},
            {"key": "recentlyViewed", "title": "Recently Viewed"},
            {"key": "onDeck", "title": "Continue Watching"},
            {"secondary": True, "key": "collection", "title": "By Collection"},
            {"secondary": True, "key": "edition", "title": "By Edition"},
            {"secondary": True, "key": "genre", "title": "By Genre"},
            {"secondary": True, "key": "year", "title": "By Year"},
            {"secondary": True, "key": "decade", "title": "By Decade"},
            {"secondary": True, "key": "director", "title": "By Director"},
            {"secondary": True, "key": "actor", "title": "By Starring Actor"},
            {"secondary": True, "key": "country", "title": "By Country"},
            {"secondary": True, "key": "contentRating", "title": "By Content Rating"},
            {"secondary": True, "key": "rating", "title": "By Rating"},
            {"secondary": True, "key": "resolution", "title": "By Resolution"},
            {"secondary": True, "key": "firstCharacter", "title": "By First Letter"},
            {"key": "folder", "title": "By Folder"},
            {"prompt": "Search Movies", "search": True, "key": "search?type=1",
             "title": "Search..."},
        ],
        "show": [
            {"key": "all", "title": "All Shows"},
            {"key": "unwatched", "title": "Unwatched"},
            {"key": "newest", "title": "Recently Released"},
            {"key": "recentlyAdded", "title": "Recently Added"},
            {"key": "recentlyViewed", "title": "Recently Viewed Episodes"},
            {"key": "recentlyViewedShows", "title": "Recently Viewed Shows"},
            {"key": "onDeck", "title": "Continue Watching"},
            {"secondary": True, "key": "collection", "title": "By Collection"},
            {"secondary": True, "key": "edition", "title": "By Edition"},
            {"secondary": True, "key": "firstCharacter", "title": "By First Letter"},
            {"secondary": True, "key": "genre", "title": "By Genre"},
            {"secondary": True, "key": "year", "title": "By Year"},
            {"secondary": True, "key": "contentRating", "title": "By Content Rating"},
            {"key": "folder", "title": "By Folder"},
            {"prompt": "Search for Shows", "search": True, "key": "search?type=2",
             "title": "Search Shows..."},
            {"prompt": "Search for Episodes", "search": True, "key": "search?type=4",
             "title": "Search Episodes..."},
        ],
    }

    def section_pivots(self, section_id, section, query):
        """`GET /library/sections/{id}` -- the sidebar's pivot list, and the
        Edit dialog's source of EDITABLE TAG FIELDS.

        `includeDetails=1` adds the `Type` blocks and the `FieldType` operator
        lists, and the client reads `Type[].Field[type=="tag"]` out of it to decide
        which tag fields the Edit dialog offers. plexsim answered two Directory rows
        and no Type at all, so the dialog's Tags pane was empty -- no genre, no
        collection, no label, no people.
        """
        pivots = (self.SECTION_PIVOTS.get(section.get("type"))
                  or self.SECTION_PIVOTS["movie"])
        fields = {"librarySectionID": int(section_id) if str(section_id).isdigit()
                  else section_id,
                  "Directory": [dict(p) for p in pivots]}
        size = len(pivots)
        if query.get("includeDetails") == "1":
            meta = self.section_meta(section_id, section.get("type") or "movie",
                                     advanced=True) or {}
            if meta.get("Type"):
                fields["Type"] = meta["Type"]
                size += len(meta["Type"])
            if meta.get("FieldType"):
                fields["FieldType"] = meta["FieldType"]
                size += len(meta["FieldType"])
        # Measured: `size` counts the Directory rows AND the Type and FieldType
        # blocks -- 20 + 1 + 8 = 29 on the reference's movie library.
        body = self.secondary_container(section_id, section, None, **fields)
        body["MediaContainer"]["size"] = size
        body["MediaContainer"].pop("title2", None)
        return body

    def section_listing(self, section_id, section, query):
        """`/library/sections/{id}/all` -- the grid, with the client's filters.

        Ignoring the filter params would make every secondary-filter Directory
        entry (By Genre, By Year, ...) open a list of the whole library, which
        looks like the filter is broken rather than unimplemented.
        """
        if query.get("type") == "18":
            # Plex's collection type. The Collections pivot asks for it here
            # rather than at /library/sections/{id}/collections.
            rows = self.library.collections(section_id)
            return self.listing_container(section_id, section, rows, query=query)
        source = self.typed_rows(section_id, query)

        source, _ = self.apply_filters(source, query, section)
        # Measured on both servers: the DEFAULT order of a listing is `titleSort`
        # ascending (falling back to the title where no sort title is stored), not
        # the catalog's own order -- fun's show grid opens on `【OSHI NO KO】, 13
        # Reasons Why, 30 Rock, 86 EIGHTY-SIX, The 100` because The 100's titleSort
        # is "100". plexsim answered catalog order, so an edited Sort Title never
        # moved a row and the grid's own default disagreed with its sort control.
        source, unsupported = self._sorted(source, query.get("sort") or "titleSort")
        if unsupported:
            # Measured: a real PMS answers an unknown sort key with a 500 rather
            # than ignoring it. Silently returning the list unsorted is the worse
            # answer -- the caller cannot tell its sort did nothing.
            logger.warning("UNSUPPORTED sort key %r", unsupported)
            return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
        if "group" in query:
            source = self.grouped(source, query.get("group"))
        rows = [_listing_row(r) for r in source]
        # Measured: only the unpaged /all listing carries `nocache`; newest,
        # recentlyAdded, onDeck and collections carry neither it nor totalSize.
        extra = {}
        if section.get("type") == "show":
            # Re-measured on both servers: `nocache` on a section listing tracks the
            # library TYPE, exactly as it does on the secondary containers -- a show
            # library carries it paged AND unpaged, a movie library never does. The
            # earlier reading (unpaged only) had it the wrong way round, so a show
            # grid -- which the client always pages -- never carried it.
            extra["nocache"] = True
        if query.get("includeMeta") == "1":
            meta = self.section_meta(section_id, section.get("type") or "movie",
                                     advanced=query.get("includeAdvanced") == "1")
            if meta:
                extra["Meta"] = meta
        return self.listing_container(section_id, section, rows, query=query, **extra)

    # The sort keys the captured Meta advertises. A key the listing cannot honour
    # is a sort the UI offers and the server ignores, which reads as a broken
    # control rather than a missing one.
    TEXT_SORTS = ("titleSort", "title", "originallyAvailableAt", "contentRating")

    NUMERIC_SORTS = ("addedAt", "lastViewedAt", "year", "rating", "audienceRating",
                     "duration", "viewCount", "viewOffset", "index", "leafCount",
                     "unviewedLeafCount", "originallyAvailableAt", "mediaHeight",
                     "updatedAt", "lastRatedAt", "ratingCount",
                     # The keys `includeAdvanced=1` advertises. Three of them --
                     # userRating, contentRating, mediaBitrate -- answered 500 on
                     # plexsim while its own Meta was about to offer them, which is
                     # an advertised control that breaks the grid when used.
                     "userRating", "mediaBitrate")

    def _sort_key(self, key):
        """One row -> comparable value for a sort key, or None if unsupported."""
        if key == "mediaHeight":
            return lambda r: max([m.get("height") or 0
                                  for m in r.get("Media") or []] or [0])
        if key == "mediaBitrate":
            return lambda r: max([m.get("bitrate") or 0
                                  for m in r.get("Media") or []] or [0])
        if key in self.TEXT_SORTS:
            def text(row):
                value = row.get(key)
                # A row with no titleSort of its own sorts by its title -- a
                # playlist row has only the latter, so keying on titleSort alone
                # made every playlist compare equal and the direction do nothing.
                if key == "titleSort" and not value:
                    value = row.get("title")
                return (value is None, str(value or "").lower())
            return text
        if key in self.NUMERIC_SORTS:
            return lambda r: (r.get(key) is None, r.get(key) or 0)
        return None

    def _sorted(self, rows, spec):
        """Apply a `sort=` spec, which is a COMMA-SEPARATED list of keys.

        Measured on the reference: `sort=year:desc,titleSort:asc` orders by year
        and breaks ties on title, and `sort=random:<seed>` gives a stable order
        that DIFFERS per seed -- which is how a client pages through a shuffled
        library without repeating or skipping rows. plexsim honoured neither: it
        split the whole spec on the first colon, so a secondary key was silently
        dropped and `titleSort:asc,year:desc` even came out descending, because
        the spec as a whole ended in `:desc`.

        A term's tail is a LIST too: `titleSort:desc:nocollate` is descending,
        and reading the direction off the first colon made it ascending. The
        modifiers and the direction may come in either order.

        Returns (rows, unsupported_key). A real PMS answers an unknown sort key
        with a 500 -- but an unknown MODIFIER (`titleSort:bogus`) it ignores.
        """
        terms = filters_module.sort_terms(spec)
        if not terms:
            return rows, None
        # Measured: the secondary key is ALWAYS titleSort ascending, whatever the
        # primary is and whichever direction it runs -- fun's `sort=year` breaks its
        # 1915 ties alphabetically, and `sort=rating:desc` with no ratings comes out
        # in title order rather than in catalog order. plexsim left ties in whatever
        # order the rows arrived in, so a tied page was unstable between requests.
        if [key for key, _desc, _seed in terms] != ["titleSort"] \
                and not any(key == "random" for key, _d, _s in terms):
            terms = list(terms) + [("titleSort", False, "")]
        ordered = list(rows)
        # Right to left, relying on the sort being stable: the leftmost key ends
        # up dominant, which is the order a real PMS applies them in.
        for key, descending, seed in reversed(terms):
            if key == "random":
                ordered = sorted(ordered, key=lambda r: zlib.crc32(
                    ("%s/%s" % (seed or "0", r.get("ratingKey"))).encode()))
                if descending:
                    ordered.reverse()
                continue
            getter = self._sort_key(key)
            if getter is None:
                return rows, key
            ordered = sorted(ordered, key=getter, reverse=descending)
        return ordered, None

    # The fields `group=` accepts, probed one at a time against the reference.
    # They are the scalar columns of the metadata row itself; everything that
    # lives somewhere else -- a tag join (`genre`, `director`), the media
    # (`resolution`, `audioCodec`, `mediaSize`), or per-user view state
    # (`viewCount`, `lastViewedAt`, `unwatched`) -- is a 500. `duration` and
    # `userRating` are 500 too, which no rule about the schema predicts, so the
    # set is the measurement rather than a rule.
    GROUPABLE = {"title", "titleSort", "studio", "contentRating", "year", "decade",
                 "addedAt", "updatedAt", "originallyAvailableAt", "audienceRating",
                 "rating", "editionTitle", "index", "summary", "tagline",
                 "contentRatingAge"}

    def grouped(self, rows, field):
        """`group=<field>` -- one row per distinct value of that field.

        Measured: `?group=studio` on five movies that share a studio answers ONE
        row, and an empty, unknown or non-scalar field is a 500. Which row of a
        group survives is a SQL artifact on the reference (it is not the first in
        sort order, nor the lowest id); plexsim keeps the first in the order the
        sort just produced, which is at least deterministic.
        """
        bare = field.split(".", 1)[1] if field and "." in field else field
        if not bare or bare not in self.GROUPABLE:
            raise filters_module.FilterError(500, "cannot group by %r" % field)
        seen, kept = set(), []
        for row in rows:
            value = self.filter_value(bare, row)
            marker = str(value)
            if marker in seen:
                continue
            seen.add(marker)
            kept.append(row)
        return kept

    def _sortable(self, key):
        """Whether a `sort=` key is one this server can order by.

        `random` is not in the key tables because it is not read off a row, but
        it IS a sort a real PMS accepts -- and /query/parse validates the key
        before it echoes the Order block, answering 500 for one it does not know.
        """
        return key == "random" or self._sort_key(key) is not None

    # How each advertised filter field is read off a row. The keys here are the
    # `Field` entries of the captured Meta, so plexsim filters on exactly what it
    # tells the client it can filter on -- and a field it cannot answer from its
    # own rows is honestly absent rather than guessed at.
    TAG_FILTER_FIELDS = {"genre": "Genre", "collection": "Collection",
                         "director": "Director", "writer": "Writer",
                         "producer": "Producer", "country": "Country",
                         "actor": "Role", "label": "Label",
                         "contentRating": "contentRating", "studio": "studio"}

    def filter_value(self, field, row):
        """One row's value for an advertised filter field."""
        # `episode.unwatched` and friends: the type prefix is the scope, and the
        # field behind it is the same field.
        if "." in field:
            field = field.split(".", 1)[1]
        if field in self.TAG_FILTER_FIELDS:
            return row.get(self.TAG_FILTER_FIELDS[field])
        if field == "decade":
            year = row.get("year")
            return (int(year) // 10 * 10) if year else None
        if field == "unwatched":
            return 0 if row.get("viewCount") else 1
        if field == "inProgress":
            return 1 if row.get("viewOffset") else 0
        if field == "unmatched":
            guid = str(row.get("guid") or "")
            return 1 if (guid.startswith("local://") or "none://" in guid) else 0
        if field == "unwatchedLeaves":
            leaves = (row.get("leafCount") or 0) - (row.get("viewedLeafCount") or 0)
            return 1 if leaves > 0 else 0
        if field == "unviewedLeafCount":
            return (row.get("leafCount") or 0) - (row.get("viewedLeafCount") or 0)
        media = (row.get("Media") or [{}])[0]
        parts = media.get("Part") or [{}]
        if field == "resolution":
            return media.get("videoResolution")
        if field in ("videoCodec", "audioCodec", "container"):
            return media.get(field)
        if field == "audioLayout":
            return media.get("audioChannelLayout")
        if field == "mediaBitrate":
            return media.get("bitrate")
        if field == "mediaSize":
            return parts[0].get("size")
        if field == "location":
            # "Folder Location" compares a section LOCATION ID, not a path:
            # measured, `?location=1` returns the whole of section 1 and
            # `?location=/data/movies` is a 500. Every plexsim section has one
            # location whose id is the section's own key.
            return row.get("librarySectionID")
        if field in ("subtitleCodec",):
            return [s.get("codec") for part in (media.get("Part") or [])
                    for s in part.get("Stream") or [] if s.get("streamType") == 3]
        if field in ("audioLanguage", "subtitleLanguage"):
            # Both forms: the axis keys by the 2-letter tag (`en`) and that is
            # what the client sends back, but a hand-built `?audioLanguage=eng`
            # should not silently miss.
            wanted = 2 if field == "audioLanguage" else 3
            return [code
                    for part in (media.get("Part") or [])
                    for s in part.get("Stream") or []
                    if s.get("streamType") == wanted
                    for code in (s.get("languageTag"), s.get("languageCode"))
                    if code]
        if field in ("hdr", "dovi", "hdr10plus", "atmos", "duplicate", "trash"):
            # plexsim's probe data carries no HDR or Atmos flag and it keeps no
            # trash, so these are honestly false rather than guessed.
            return 0
        return row.get(field)

    def apply_filters(self, rows, query, section=None):
        """Filter rows by the query, or raise the status a real PMS answers with.

        The advertised field map is global rather than per-section: measured, a
        real PMS parses `show.title` against a MOVIE library, and it is the show
        library's own prefixed keys (`show.genre`) that the client sends back --
        which plexsim looked up after stripping the prefix, in a map keyed by the
        prefixed names, so no filter on a show grid ever constrained anything.
        """
        tree = filters_module.parse(query.get("_pairs") or [],
                                    FILTER_FIELD_TYPES.get)
        if tree is None:
            return rows, None
        kept = [r for r in rows
                if filters_module.evaluate(tree, r, self.filter_value)]
        return kept, tree

    def tag_title(self, field, value):
        """A tag id's name, for /query/parse's `valueTitle`.

        Measured: the lookup is by ID in the server's own tag table and is NOT
        scoped to the field that asked -- `?label=5` came back titled "Art", the
        name of tag 5 whichever field names it. A non-numeric value and an id
        that does not resolve both get no title at all rather than an empty one.
        """
        if not str(value).isdigit():
            return None
        for row in self.catalog.items.values():
            for key in ("Genre", "Collection", "Director", "Writer", "Producer",
                        "Role", "Country", "Label", "Similar", "Network"):
                for tag in row.get(key) or []:
                    if str(tag.get("id")) == str(value):
                        return tag.get("tag")
        return None

    def query_parse(self, section, query):
        """`/library/sections/{id}/query/parse` -- the grammar, read back.

        This is how the Edit Filters dialog reopens a filter that was saved: it
        hands the server the query string it stored and reads the `Rule` tree out
        of the answer. Without it the dialog can only ever draw an empty filter,
        whatever the smart playlist behind it actually holds -- and the failure is
        silent, because an unrouted path is a 404 the dialog treats as "no rules".

        The root is `Query`, not a MediaContainer. Errors are REPORTED rather than
        raised: an unknown field comes back as an `Error` entry beside whatever
        did parse, where the same field on a listing is simply ignored.
        """
        raw = query.get("type")
        if raw in (None, ""):
            # Measured: the default is the SECTION's own type, not movie.
            type_number = TYPE_NUMBERS.get(section.get("type") or "movie", 1)
        elif str(raw).lstrip("-").isdigit():
            # Measured: an unknown NUMBER is echoed back untouched (`type=99`).
            type_number = int(raw)
        else:
            return ("error", 400, "plexsim: bad request")
        implicit = IMPLICIT_SCOPE.get(type_number, "")

        errors = []
        tree = filters_module.parse(query.get("_pairs") or [],
                                    FILTER_FIELD_TYPES.get, errors=errors)
        order = filters_module.orders(query.get("sort") or "", self._sortable)
        limit = None
        if query.get("limit"):
            # Measured: `?limit=abc` is a 500, the same refusal a bad filter
            # value gets, and the block carries the value as a NUMBER.
            limit = int(filters_module.as_number(query["limit"]))
        group = [{"field": query["group"]}] if query.get("group") else None
        return filters_module.document(
            type_number, tree, order, limit, group, errors,
            lambda field, scope: FIELD_TITLES.get((scope or implicit, field), ""),
            self.tag_title)

    @staticmethod
    def _guid_gate(rows, query):
        """Strip `Guid` from listing rows unless the client asked for it.

        Measured: a DETAIL read always carries the `Guid` list, and a LISTING
        carries it only under `includeGuids=1`. plexsim sent it everywhere, which
        is a superset rather than a break -- but it is not what a real PMS sends,
        and the grid payload is the one place the difference is measurable.
        """
        if (query or {}).get("includeGuids") == "1":
            return rows
        return [{k: v for k, v in row.items() if k != "Guid"} for row in rows]

    def listing_container(self, section_id, section, rows, view_group=None,
                          query=None, **extra):
        """The container a section listing returns, with the client's paging."""
        rows = self._guid_gate(rows, query)
        page, paged, start, total = self.page_window(rows, query)
        page = [_grid_row(r, keep_section=False) for r in page]
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
            fields["totalSize"] = total
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
        if section.get("type") == "show":
            # Measured across two servers and five libraries: every secondary
            # container of a SHOW library carries `nocache` (filters, sorts, genre,
            # year alike) and no movie library's does. The cause is not visible
            # from outside -- it tracks the library's TYPE, not its cache state,
            # which is what three movie libraries and two show ones rule out.
            body["nocache"] = True
        if title2 is not None:
            # An empty title2 is sent, not omitted -- the Categories view's is "".
            body["title2"] = title2
        body.update(fields)
        for key in ("Directory", "Metadata"):
            if isinstance(body.get(key), list):
                body["size"] = len(body[key])
        body.setdefault("size", 0)
        return {"MediaContainer": self._prune(body)}

    def secondary_directory(self, section_id, section, leaf, query=None):
        """`/library/sections/{id}/{genre,year,director,...}`.

        A tag axis keys its entries by TAG ID and a field axis by the value
        itself -- measured: `?genre=24` but `?contentRating=PG`. Getting that
        backwards makes every entry open an empty list.

        Three smaller measurements, each of which plexsim had wrong: only a
        `genre` entry carries a `type`, a person entry carries the tag's `thumb`,
        and the two field axes encode differently -- a `contentRating` entry
        single-encodes its key and its fastKey alike, while a `studio` entry
        leaves the fastKey RAW and DOUBLE-encodes the key. That inconsistency is
        the reference's own; it is reproduced so the replay diff stays clean.
        """
        rows = self.typed_rows(section_id, query or {})
        entries = []
        if leaf in SECONDARY_TAGS:
            key = SECONDARY_TAGS[leaf]
            seen = {}
            for row in rows:
                for tag in row.get(key) or []:
                    name = tag.get("tag")
                    if not name or name in seen:
                        continue
                    seen[name] = tag
            for name in sorted(seen):
                tag = seen[name]
                entry = {"fastKey": "/library/sections/%s/all?%s=%s"
                                    % (section_id, leaf, tag.get("id"))}
                if tag.get("thumb"):
                    entry["thumb"] = tag["thumb"]
                entry["key"] = str(tag.get("id"))
                entry["title"] = name
                if leaf == "genre":
                    entry["type"] = leaf
                entries.append(entry)
        else:
            field = SECONDARY_FIELDS[leaf]
            seen = set()
            for row in rows:
                value = row.get(field)
                if value is None or value == "":
                    continue
                if leaf == "decade":
                    value = int(value) // 10 * 10
                seen.add(str(value))
            for value in sorted(seen, reverse=leaf in ("year", "decade")):
                once = urllib.parse.quote(value)
                entries.append({
                    "fastKey": "/library/sections/%s/all?%s=%s"
                               % (section_id, leaf,
                                  value if leaf == "studio" else once),
                    "key": urllib.parse.quote(once) if leaf == "studio" else once,
                    "title": (value + "s") if leaf == "decade" else value,
                })
        return self.secondary_container(section_id, section,
                                        SECONDARY_TITLES.get(leaf), Directory=entries)

    def media_axis(self, section_id, section, leaf, query=None):
        """`/library/sections/{id}/{resolution,videoCodec,audioLanguage,...}`.

        The axes read off the Media and its Streams. Measured: their entries
        carry `key` and `title` and NO `fastKey` -- the client builds the URL --
        and the title is a LABEL rather than the value: `1080` prints as `1080p`
        and a language code prints as the language's own name.

        These four (`videoCodec`, `audioCodec`, `subtitleCodec`, `audioLayout`)
        404 on production's older PMS and answer on the lab's 1.43.4. plexsim
        follows the lab, which is the designated reference.
        """
        seen = {}
        for row in self.typed_rows(section_id, query or {}):
            for media in row.get("Media") or []:
                if leaf == "resolution":
                    found = [(media.get("videoResolution"), None)]
                elif leaf == "audioLayout":
                    found = [(media.get("audioChannelLayout"), None)]
                elif leaf in ("videoCodec", "audioCodec"):
                    found = [(media.get(leaf), None)]
                else:
                    wanted = 2 if leaf == "audioLanguage" else 3
                    found = []
                    for part in media.get("Part") or []:
                        for stream in part.get("Stream") or []:
                            if stream.get("streamType") != wanted:
                                continue
                            if leaf == "subtitleCodec":
                                found.append((stream.get("codec"), None))
                            else:
                                found.append((stream.get("languageTag"),
                                              stream.get("language")))
                for value, label in found:
                    if value in (None, ""):
                        continue
                    seen.setdefault(str(value), label)
        if leaf == "resolution":
            # Best first, which is the order the reference's own axis opened with.
            order = list(filters_module.RESOLUTION_TITLES)
            keys = sorted(seen, key=lambda v: -order.index(v) if v in order else 1)
            entries = [{"key": v,
                        "title": filters_module.RESOLUTION_TITLES.get(v, v)}
                       for v in keys]
        else:
            entries = [{"key": v, "title": seen[v] or v} for v in sorted(seen)]
        return self.secondary_container(section_id, section,
                                        SECONDARY_TITLES.get(leaf), Directory=entries)

    def location_axis(self, section_id, section):
        """`/library/sections/{id}/location` -- By Folder Location.

        The one secondary axis whose container is NOT the secondary one: measured,
        it carries `size` and `title2` and nothing else, where every other axis
        carries the full art/thumb/identifier/viewGroup set. The key is the
        section's Location ID, which is also what the matching filter compares.
        """
        entries = [{"fastKey": "/library/sections/%s/all?location=%s"
                               % (section_id, location.get("id")),
                    "key": str(location.get("id")),
                    "title": location.get("path")}
                   for location in self.section_directory(section).get("Location") or []]
        return {"MediaContainer": {"size": len(entries),
                                   "title2": SECONDARY_TITLES["location"],
                                   "Directory": entries}}


    def library_list_item(self, rating_key, segments, query, method):
        """`/library/metadata/{key}` where the key is a COLLECTION or a PLAYLIST."""
        collection = self.library.collection(rating_key)
        playlist = self.library.playlist(rating_key)
        if collection is None and playlist is None:
            return ("error", 404, "plexsim: no such item")
        if len(segments) == 4 and segments[3] == "prefs":
            # Measured: the per-item prefs are WRITE-only here (the GET is a 404),
            # and `collectionSort`/`collectionMode` land on the collection row as
            # STRING attributes. `collectionSort.value=` on this path is a 400 --
            # prefs are not fields.
            if method != "PUT" or collection is None:
                return ("error", 404, "plexsim: not found")
            for name, value in query.items():
                if name.startswith("X-Plex-") or name == "_pairs":
                    continue
                if name not in ("collectionSort", "collectionMode"):
                    return ("error", 400, "plexsim: bad request")
                if value == "":
                    collection.pop(name, None)
                else:
                    collection[name] = str(value)
            logger.info("COLLECTION PREF %s %s", rating_key,
                        ",".join(k for k in query if not k.startswith("X-Plex-")))
            return ("empty", 200)
        if len(segments) != 3:
            return ("error", 404, "plexsim: not found")
        if method == "PUT":
            target = collection if collection is not None else playlist
            title = query.get("title.value")
            if title is None:
                title = query.get("title")
            summary = query.get("summary.value")
            if summary is None:
                summary = query.get("summary")
            if collection is not None:
                self.library.edit_collection(collection, title=title, summary=summary)
                for name in ("titleSort", "contentRating"):
                    if query.get("%s.value" % name) is not None:
                        collection[name] = query["%s.value" % name]
            else:
                self.library.edit_playlist(playlist, title=title, summary=summary)
            logger.info("EDIT list %s <- %s", rating_key,
                        ",".join(k for k in query if not k.startswith("X-Plex-")
                                 and k != "_pairs"))
            return ("empty", 200)
        if collection is not None:
            # Measured on fun: a collection read through /library/metadata carries
            # the library container any item's detail does -- allowSync, the
            # identifier, the section triple and the media-tag pair. plexsim sent a
            # bare one, so a client could resolve neither the section nor a tag.
            section = self.catalog.section(collection["librarySectionID"]) or {}
            section_key = collection["librarySectionID"]
            return self.bare_container(
                size=1, allowSync=True, identifier="com.plexapp.plugins.library",
                librarySectionID=int(section_key) if str(section_key).isdigit()
                else section_key,
                librarySectionTitle=section.get("title"),
                librarySectionUUID=section.get("uuid"),
                mediaTagPrefix="/system/bundle/media/flags/", mediaTagVersion=1655122614,
                Metadata=[self.library._collection_row(collection)])
        row = self.library._playlist_row(playlist, content=True)
        return self.bare_container(size=1, Metadata=[row])

    def apply_edit(self, rows, query, bare_fields=True, metadata_state="loading"):
        """One edit against one or more rows, with the refusals a real PMS makes.

        The client's Edit dialog saves through `PUT /library/sections/{id}/all`,
        and a batch edit through a comma-joined key list, so the same parameter
        handling has to serve three paths. Every one of them answered the LISTING
        on plexsim, with a 200 -- so the dialog reported success and applied
        nothing, which is the quietest failure on the whole surface.
        """
        try:
            for row in rows:
                self.library.edit(row, query, pairs=query.get("_pairs"),
                                  bare_fields=bare_fields)
                self.notify_edited(row, metadata_state)
        except library_module.EditError as refused:
            logger.warning("EDIT REFUSED: %s", refused.detail)
            return ("error", refused.status, "plexsim: %s" % refused.detail)
        logger.info("EDIT %s <- %s",
                    ",".join(str(r.get("ratingKey")) for r in rows),
                    ",".join(k for k in query if not k.startswith("X-Plex-")
                             and k != "_pairs"))
        return ("empty", 200)

    # The scalar fields a Common compares, in the order `mixedFields` lists them --
    # the one order consistent with every capture across the four types.
    COMMON_FIELDS = ["title", "ratingKey", "parentRatingKey", "titleSort", "tagline",
                     "index", "summary", "year", "studio", "originallyAvailableAt",
                     "contentRating", "userRating"]

    def common_fields(self, section_id, section, query):
        """`GET /library/sections/{id}/common` -- what the Edit dialog READS.

        plexsim 404'd it, and the dialog's populate() rejects on that, so Edit
        answered "There was an error editing this item" before the user could type.

        Measured: a `Common` row (JSON `Metadata` with `type: "common"`) holding the
        fields the selected items SHARE, in a secondary container titled "Common".
        With several ids, the fields that differ are dropped and named in
        `mixedFields` instead -- which is how the dialog knows to show a field as
        "(mixed)" rather than overwrite it.
        """
        if not query.get("type"):
            return ("error", 400, "plexsim: bad request")
        wanted = {"1": "movie", "2": "show", "3": "season", "4": "episode"}.get(query["type"])
        if "id" in query:
            keys = [int(k) for k in (query.get("id") or "").split(",")
                    if k.strip().isdigit()]
            # Measured: an id of the wrong TYPE is simply not part of the set.
            rows = [self.catalog.items[k] for k in keys if k in self.catalog.items
                    and self.catalog.items[k].get("type") == wanted]
        else:
            # Measured: with no `id` the dialog is editing EVERYTHING the filter
            # selects -- the select-all edit, whose save already edits the whole
            # filtered set -- and the common fields are computed over all of it.
            # plexsim computed them over no rows and answered an empty Common.
            rows = self.typed_rows(section_id, query)
            rows, _ = self.apply_filters(rows, query, section)
            rows = [r for r in rows if r.get("type") == wanted]

        def value(row, field):
            if field == "titleSort":
                return row.get("titleSort") or row.get("title")
            if field == "index" and row.get("type") == "movie":
                return row.get("index") or 1
            return row.get(field)

        fields = self.COMMON_FIELDS
        if wanted in ("season", "episode"):
            # Measured on the lab's seasons and episodes: their titles are never
            # compared (two differently titled episodes do not list `title` mixed).
            fields = [f for f in fields if f != "title"]
        common = {"mixedFields": ""}
        mixed = []
        for field in fields:
            if len({json.dumps(value(row, field)) for row in rows}) > 1:
                mixed.append(field)
            elif rows and value(rows[0], field) is not None:
                common[field] = value(rows[0], field)
        if len(rows) == 1:
            common["key"] = "/library/metadata/%s/children" % rows[0]["ratingKey"]
        common["type"] = "common"
        # Measured: `guid` is ALWAYS the empty string, even for a single item; a
        # MIXED titleSort is present as "" rather than absent; and a season's or an
        # episode's titleSort is reported only ever as "".
        common["guid"] = ""
        if "titleSort" in mixed or wanted in ("season", "episode"):
            common["titleSort"] = ""
        if wanted == "episode" and rows:
            # Measured: an episode Common names its show, and `parentTitle` is "".
            if len({str(r.get("grandparentRatingKey")) for r in rows}) == 1:
                common["grandparentRatingKey"] = str(rows[0].get("grandparentRatingKey"))
                common["grandparentTitle"] = rows[0].get("grandparentTitle")
            common["parentTitle"] = ""
        # Measured: `mixedFields` is a comma-joined list in a FIXED field order, and
        # is the EMPTY string when nothing differs, not absent.
        common["mixedFields"] = ",".join(mixed)
        if len(rows) == 1:
            for key in ("Guid", "Rating", "Field"):
                if rows[0].get(key):
                    common[key] = rows[0][key]
        elif rows:
            # Measured: one Rating per rating IMAGE that every selected item has --
            # the full entry when all agree, an EMPTY element when the values
            # differ. Movies 1 and 3 share imdb and themoviedb but not their Rotten
            # Tomatoes images, and answer two empty Ratings.
            shared = None
            for row in rows:
                images = {r.get("image") for r in row.get("Rating") or []}
                shared = images if shared is None else shared & images
            ratings = []
            for image in [r.get("image") for r in rows[0].get("Rating") or []]:
                if image not in (shared or set()):
                    continue
                entries = [next(r for r in row.get("Rating") if r.get("image") == image)
                           for row in rows]
                ratings.append(entries[0] if all(e == entries[0] for e in entries) else {})
            if ratings:
                common["Rating"] = ratings
        for key in ("Genre", "Country", "Director", "Writer", "Producer", "Role",
                    "Collection", "Label"):
            shared = None
            for row in rows:
                names = {str(t.get("tag") or t.get("id")) for t in row.get(key) or []}
                shared = names if shared is None else (shared & names)
            if shared:
                common[key] = [t for t in (rows[0].get(key) or [])
                               if str(t.get("tag") or t.get("id")) in shared]
        found = self.secondary_container(section_id, section, "Common",
                                         Metadata=[common],
                                         _xml={"Metadata": "Common"})
        return found

    def library_metadata(self, segments, query, method="GET"):
        raw_key = segments[2]

        # A comma-joined key list is a real client pattern (plezy batches, and the
        # web client's own per-grid userState probe). Measured: a LEAF on a
        # comma-joined list is a 404 -- plexsim returned before it looked at the
        # leaf at all, so `/library/metadata/a,b/userState` answered full metadata
        # rows with no UserState in them, which the client reads as state it has.
        if "," in raw_key:
            if len(segments) > 3:
                # Measured: a leaf on a list with ONE numeric member is that item's
                # leaf (`1,abc/posters` answers item 1's posters); on a list of two or
                # more it is a 404.
                numeric = [part for part in raw_key.split(",") if part.isdigit()]
                if len(numeric) != 1:
                    return ("error", 404, "plexsim: not found")
                return self.library_metadata(segments[:2] + [numeric[0]] + segments[3:],
                                             query, method)
            keys = [int(p) for p in raw_key.split(",")
                    if p.isdigit() and int(p) in self.catalog.items]
            if method == "PUT":
                # Measured: a batch EDIT. plexsim answered the listing here, so a
                # multi-select edit applied to nothing and reported success.
                return self.apply_edit([self.catalog.items[k] for k in keys], query)
            return self.container(Metadata=[_full(self.catalog.items[k])
                                            for k in keys])
        if not raw_key.isdigit():
            # Measured: a non-numeric ratingKey is a 404, not an empty container.
            return ("error", 404, "plexsim: not found")

        if not raw_key.isdigit():
            return self.container()

        rating_key = int(raw_key)
        row = self.catalog.items.get(rating_key)
        if row is None:
            # A collection and a playlist are metadata items in their own right, and
            # the client edits both through this path -- its own saveUrl falls back
            # to `/library/metadata/{key}` for a playlist on a server without the
            # true-playlists feature. plexsim kept them outside the catalog, so both
            # answered 404 and neither could be renamed from its own page.
            return self.library_list_item(rating_key, segments, query, method)

        if len(segments) == 3:
            if method == "PUT":
                # The Edit dialog, and "Add to Collection": the client PUTs
                # `<field>.value`, `<field>.locked` and `<tag>[N].tag.tag` to
                # the item's own key.
                return self.apply_edit([row], query)
            if method == "DELETE":
                if query.get("proxy") == "1":
                    # Delete Version. plexsim has no optimized versions, and a
                    # real PMS answers 400 when there are none to delete.
                    return ("error", 400, "plexsim: no optimized versions")
                self.library.delete(rating_key)
                self.drop_from_queues(rating_key)
                self.notify_deleted(row)
                logger.info("DELETE %s (%s)", rating_key, row.get("title"))
                return ("empty", 200)
            section = self.catalog.section(row["librarySectionID"]) or {}
            detail = _full(row)
            if row.get("type") in ("show", "season"):
                detail = (self.show_detail(row, detail, query)
                          if row["type"] == "show"
                          else self.parent_detail(row, detail, query))
            elif query.get("includeExtras") == "1":
                # Measured: an episode and a movie carry the Extras block too.
                detail["Extras"] = {"size": 0}
            detail = self.apply_detail_options(row, detail, query)
            # Re-measured 2026-09-12 against BOTH fun and the 1.43.4 reference,
            # with the client's own includeX param set: the detail container
            # DOES carry identifier and the media-tag pair. An earlier note here
            # claimed the opposite ("an item-detail container is BARE"); it did
            # not hold on either server, and the missing mediaTagPrefix is what
            # a client uses to resolve a tag into an image URL.
            fields = {
                "size": 1,
                "allowSync": True,
                "identifier": "com.plexapp.plugins.library",
                "mediaTagPrefix": "/system/bundle/media/flags/",
                "mediaTagVersion": 1655122614,
                "librarySectionID": row["librarySectionID"],
                "librarySectionTitle": section.get("title"),
                "librarySectionUUID": section.get("uuid"),
            }
            if query.get("asyncCheckFiles") == "1" \
                    or query.get("asyncRefreshLocalMediaAgent") == "1":
                # Measured, and not guessable: either `async*` flag makes the
                # container DROP identifier and the media-tag pair. The response
                # is the same row, described by a container that can no longer
                # resolve a tag into an image URL -- which is presumably why the
                # client only sends these on a refresh it does not draw from.
                for name in ("identifier", "mediaTagPrefix", "mediaTagVersion"):
                    fields.pop(name)
            return self.bare_container(Metadata=[detail], **fields)

        leaf = segments[3]
        if leaf == "children" and row.get("type") in ("movie", "episode", "clip", "track"):
            # Measured: `/children` of an item that cannot have any -- a movie, an
            # episode -- is a 400, where its `/allLeaves` is still a 200.
            return ("error", 400, "plexsim: bad request")
        if leaf in ("children", "allLeaves"):
            if leaf == "children":
                rows = self.catalog.children(rating_key)
            else:
                rows = self.catalog.leaves(rating_key)
            extra = {}
            paging = ("X-Plex-Container-Start" in query
                      and "X-Plex-Container-Size" in query)
            if (leaf == "children" and row.get("type") == "show"
                    and query.get("excludeAllLeaves") != "1" and rows
                    and not paging):
                # Measured: a SHOW's /children carries an extra `Directory` row
                # linking to /allLeaves, titled "All episodes", and the container's
                # size counts it -- which is what `excludeAllLeaves=1` removes.
                # plexsim never emitted it, so the season list had no "All
                # episodes" entry and the count was one short of the reference's.
                extra["Directory"] = [{"key": "/library/metadata/%s/allLeaves"
                                              % rating_key,
                                       "title": "All episodes"}]
            return self.child_container(row, rows, leaf, query, **extra)

        # -- the context menu's write half --------------------------------
        if leaf in ("refresh", "analyze", "unmatch", "match", "split", "merge"):
            return self.item_action(row, leaf, method, query)
        if leaf == "matches":
            return self.matches(row, query)
        if leaf in ("poster", "art"):
            # The SINGULAR path is the write, and plexsim had it as the image read.
            # The client's own Change Poster / Change Background builds
            # `/library/metadata/{id}/` + ("poster"|"art") with type PUT, and then
            # does `model.set("thumb", response)` -- so the response BODY is the new
            # artwork path, not a container and not empty. plexsim answered the
            # PLURAL path for the write and served image bytes from the singular one,
            # which meant Change Poster did nothing AND set the tile's img src to a
            # blob of PNG.
            if method == "PUT":
                return self.set_artwork(row, leaf + "s", query)
            return ("image-key", row, leaf)
        if leaf in ("posters", "arts"):
            if method == "PUT":
                # Measured: the plural path is a 404 for a write.
                return ("error", 404, "plexsim: not found")
            return self.artwork_list(row, leaf)
        if leaf == "tree":
            # Measured: an EPISODE's tree is rooted at its SHOW, and a season's at
            # the season. The dialog reads the whole branch, not just the leaf.
            top = row
            if row.get("type") == "episode" and row.get("grandparentRatingKey"):
                top = self.catalog.items.get(int(row["grandparentRatingKey"])) or row
            return self.metadata_tree(top)
        if leaf == "media" and len(segments) > 4:
            if method != "DELETE":
                # Measured: a 404. A real PMS never answers 405 anywhere.
                return ("error", 404, "plexsim: not found")
            if not self.library.delete_media(row, segments[4]):
                return ("error", 404, "plexsim: no such media")
            return ("empty", 200)

        if leaf == "prefs":
            # Per-item settings: a show's episode sorting, from its Edit dialog.
            # Measured: the GET is a 404 and only the PUT exists.
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            settings = {s.get("id"): s for s in ITEM_PREFS.get(row.get("type")) or []}
            writes = [(name, value) for name, value in query.items()
                      if not name.startswith("X-Plex-") and name != "_pairs"]
            # Measured: a setting the item's TYPE does not have is a 400 and nothing
            # in the request is applied -- an episode has none, so any write to one
            # is refused. plexsim stored every key it was sent, including its own
            # parser's `_pairs` list, and both then rendered onto the metadata row.
            if any(name not in settings for name, _ in writes):
                return ("error", 400, "plexsim: bad request")
            stored = row.setdefault("_prefs", {})
            for name, value in writes:
                # Measured: an EMPTY value clears the setting (the row attribute
                # goes and the setting reads its default again); anything else is
                # stored as sent, `abc` on an int setting included.
                if value == "":
                    stored.pop(name, None)
                else:
                    stored[name] = value
                logger.info("ITEM PREF %s %s = %s", rating_key, name, value)
            return ("empty", 200)
        if leaf == "subtitles":
            # The Search Subtitles dialog. A real PMS answers with `Stream` rows
            # from its subtitle agent; plexsim has no subtitle source, so the
            # honest answer is that shape with nothing in it rather than a
            # different shape or a refusal the dialog reads as an error.
            # Measured: `language` is required (400 without it) and must be a real
            # two-letter language, any case and with or without a region -- `en`,
            # `EN`, `en-US` and `de` search, where `xx`, `eng` and an empty value are
            # 500s. plexsim answered the empty container to all of them.
            language = query.get("language")
            if language is None:
                return ("error", 400, "plexsim: bad request")
            if language.split("-")[0].lower() not in SUBTITLE_LANGUAGES:
                return ("error", 500, "plexsim: internal error")
            return self.tag_container(size=0, Stream=[])
        if leaf in ("markers", "banner", "chapterImages", "indexes"):
            # All 404 on a real PMS for a row like plexsim's: no intro markers,
            # no banner artwork, no chapter thumbnails, no preview index.
            return ("error", 404, "plexsim: not found")
        if leaf == "extras":
            return self.tag_container(size=0)
        if leaf == "related":
            # NOTE: this is NOT /hubs/metadata/{id}/related -- the client rewrites
            # the path for that one. Re-measured, overturning the note that stood
            # here: this one carries the SAME hub as the hubs form, plus the
            # media-tag pair the hubs form drops. The earlier recording covered only
            # the four lab items that have no related titles, so it saw nothing.
            section = self.catalog.section(row["librarySectionID"]) or {}
            hubs = self.related_hubs(row, query)
            return self.bare_container(
                size=len(hubs), Hub=hubs,
                allowSync=False, identifier="com.plexapp.plugins.library",
                librarySectionID=row["librarySectionID"],
                librarySectionTitle=section.get("title"),
                librarySectionUUID=section.get("uuid"),
                mediaTagPrefix="/system/bundle/media/flags/",
                mediaTagVersion=1655122614)
        if leaf == "similar":
            # Measured: a real PMS 404s this for a season or an episode, and for a
            # movie or a show it answers a tag container whose rows are the agent's
            # similar titles that are IN the library -- 2 to 4 rows on the lab, and
            # zero for one of the five movies, so an empty answer is correct too.
            # plexsim answered an unconditional empty, which made the Related shelf
            # never appear; the rows come from `similar_rows()` now.
            if row.get("type") in ("season", "episode"):
                return ("error", 404, "plexsim: not found")
            rows = [_listing_row(r) for r in self.similar_rows(row)]
            return self.tag_container(size=len(rows), Metadata=rows)
        if leaf == "nearest":
            # Measured: an empty library container for every item the reference
            # holds -- movie, show, season and episode alike, whatever `sort` says --
            # and a 404 only for an id it does not. plexsim 404'd it for all of them.
            return self.tag_container(size=0)
        if leaf == "users" and len(segments) == 5 and segments[4] == "top":
            # "Top users" of an item: one Account row per account that has
            # completed it, with a `globalViewCount`. Measured: rows only for an item
            # with views (0 of them is `size: 0`, not a zero row), and a SEASON is a
            # 500 on the reference.
            if row.get("type") == "season":
                return ("error", 500, "plexsim: internal error")
            views = self.library.global_views(row)
            accounts = [{"globalViewCount": views, "id": 1}] if views else []
            return self.tag_container(size=len(accounts), Account=accounts)
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
                return ("image", self._at_width(resolved, query.get("width")))
            return ("png", query)
        # Includes `/move`, which a real PMS 404s on an item: ordering is done
        # through the collection or playlist that holds it, not through the item.
        logger.warning("UNKNOWN item leaf %s on %s", leaf, rating_key)
        return ("error", 404, "plexsim: not found")

    @staticmethod
    def page_window(rows, query):
        """(page, paged, start, total) for a listing, by the measured rule.

        A real PMS pages only when `X-Plex-Container-Start` **and**
        `X-Plex-Container-Size` are both present: one alone is ignored entirely,
        with no slice, no `offset` and no `totalSize`. plexsim tested them with
        OR, so a lone `Size=1` returned one row and claimed a total, and a lone
        `Start=1` silently dropped the first row. A negative size drops paging
        altogether on a real server -- in plexsim it was a Python slice, so
        `Size=-1` quietly served every row but the last.
        """
        query = query or {}
        start_raw = query.get("X-Plex-Container-Start")
        size_raw = query.get("X-Plex-Container-Size")
        if size_raw == "":
            # Measured: an EMPTY size is a 500 even with no start beside it.
            raise filters_module.FilterError(500, "bad container parameter")
        if start_raw is None or size_raw is None:
            return list(rows), False, 0, len(rows)
        try:
            start = int(start_raw)
            size = int(size_raw)
        except (TypeError, ValueError):
            raise filters_module.FilterError(500, "bad container parameter")
        if size < 0 or start < 0:
            return list(rows), False, 0, len(rows)
        return list(rows)[start:start + size], True, start, len(rows)

    def paged_container(self, rows, query, **fields):
        """A bare container that pages when the client asks, and not otherwise.

        Measured on /playlists/all and /playlists/{id}/items: a paged request
        gets `totalSize` and `offset` beside `size`, an unpaged one gets neither.
        The same rule as every other listing here -- the client pages playlists
        as soon as there are more than a screenful.
        """
        page, paged, start, total = self.page_window(rows, query)
        body = dict(fields)
        body["size"] = len(page)
        if paged:
            body["totalSize"] = total
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

    # A hub's default row cap, measured: with no `count=` a shelf carries up to 12.
    HUB_COUNT = 12

    def hub(self, identifier, title, rows, count=None, **extra):
        """One shelf, capped at `count` with `more` saying whether it was cut.

        Measured on the reference: `?count=2` caps every shelf at two rows AND
        sets `more: true` on the ones that had more, which is how the client knows
        to draw the "see all" chevron. plexsim hard-coded `more: false` and ignored
        `count` entirely, so a shelf was either complete or looked complete. The
        client asks for `count+1` for exactly this reason -- it wants to know
        whether the row after the last visible one exists.
        """
        limit = self.HUB_COUNT
        if count is not None and str(count).strip().lstrip("-").isdigit():
            limit = max(int(count), 0)
        page = [_grid_row(r) for r in rows[:limit]]
        body = {
            "hubIdentifier": identifier,
            "title": title,
            "type": "movie",
            "hubKey": "/library/all",
            "size": len(page),
            "more": len(rows) > len(page),
            "Metadata": page,
        }
        body.update(extra)
        return body

    def home_hub(self, identifier, title, key, rows, query, kind="mixed",
                 style="shelf", **extra):
        """One Home-screen shelf, with the five fields plexsim never sent.

        Measured on the reference: every shelf carries `key` (where "see all"
        goes), `context` (`hub.<identifier>`, which is what the client keys its
        layout off), `style` (`hero` for Continue Watching, `shelf` for the rest)
        and, on /hubs/promoted, `promoted`. And `hubKey` is not a path to a
        listing: it is `/library/metadata/<comma-joined ratingKeys>` naming the
        rows THIS shelf holds, present only when there are any.

        plexsim sent `hubIdentifier`, `title`, `type`, `size`, `more` and a
        `hubKey` of `/library/all` for everything, so every shelf expanded to the
        whole library and none of them had a context to lay out by.
        """
        # The context is the identifier with any trailing SECTION ID stripped:
        # `movie.recentlyadded.1` has context `hub.movie.recentlyadded`, measured.
        # It names the KIND of shelf, which is why it is shared across libraries.
        parts = identifier.split(".")
        stem = ".".join(parts[:-1]) if parts[-1].isdigit() else identifier
        built = self.hub(identifier, title, rows, count=query.get("count"),
                         type=kind, key=key, context="hub.%s" % stem,
                         style=style, **extra)
        keys = [str(r.get("ratingKey")) for r in built.get("Metadata") or []]
        if keys:
            built["hubKey"] = "/library/metadata/" + ",".join(keys)
        else:
            built.pop("hubKey", None)
        return built

    def promoted_hubs(self, query):
        """`/hubs/promoted` -- the Home screen, one call per pinned library.

        Measured: `contentDirectoryID=N` selects ONE library's Recently Added
        shelf, and the call is repeated per pinned library rather than made once.
        plexsim ignored it and answered every library's shelf every time, so Home
        drew each library's rows under every library's heading.

        `excludeContinueWatching=1` drops that shelf, and the bare call (no
        contentDirectoryID) adds `home.playlists`.
        """
        hubs = []
        if query.get("excludeContinueWatching") != "1":
            hubs.append(self.home_hub(
                "home.continue", "Continue Watching", "/hubs/home/continueWatching",
                [_listing_row(r) for r in self.continue_watching()], query,
                style="hero", promoted=True))
        hubs.append(self.home_hub(
            "home.ondeck", "On Deck", "/hubs/home/onDeck",
            [_listing_row(r) for r in self.continue_watching()
             if r.get("type") == "episode"], query, kind="episode", promoted=True))
        wanted = query.get("contentDirectoryID")
        for section in self.catalog.sections:
            if wanted and str(section["key"]) != str(wanted):
                continue
            is_show = section.get("type") == "show"
            rows = sorted(self.catalog.section_items(section["key"]),
                          key=lambda r: -(r.get("addedAt") or 0))
            # Measured: a SHOW library's Recently Added shelf is typed `mixed` and
            # its key goes to /hubs/home/recentlyAdded; a movie library's is typed
            # `movie` and goes straight to the section listing.
            key = ("/hubs/home/recentlyAdded?type=2&sectionID=%s" % section["key"]
                   if is_show else
                   "/library/sections/%s/all?sort=addedAt:desc" % section["key"])
            hubs.append(self.home_hub(
                "%s.recentlyadded.%s" % ("tv" if is_show else "movie", section["key"]),
                "Recently Added in %s" % (section.get("title") or ""), key,
                [_listing_row(r) for r in rows], query,
                kind="mixed" if is_show else "movie", promoted=True))
        if not wanted:
            hubs.append(self.home_hub(
                "home.playlists", "Recent Playlists",
                "/playlists/all?type=15&sort=lastViewedAt:desc&playlistType=video,audio",
                [], query, kind="playlist", promoted=True))
        return self.hub_container(Hub=hubs)

    def similar_rows(self, row):
        """Rows a client should offer as "Related".

        The reference fills this from its metadata agent, which plexsim has no
        equivalent of -- and the reference's own list is EMPTY for some items
        (`/library/metadata/1/similar` is `size 0` on the lab while its
        neighbours answer 2 to 4 rows), so an empty answer is a correct answer.
        Rather than leave it always empty, plexsim computes its own: other rows in
        the same library that share a genre. The SHAPE is measured; the source is
        plexsim's own and says so.
        """
        tags = {str(t.get("id")) for t in row.get("Genre") or [] if t.get("id")}
        if not tags:
            return []
        scored = []
        for other in self.catalog.section_items(row.get("librarySectionID")):
            if str(other.get("ratingKey")) == str(row.get("ratingKey")):
                continue
            shared = len(tags & {str(t.get("id")) for t in other.get("Genre") or []})
            if shared:
                scored.append((-shared, -(other.get("addedAt") or 0), other))
        scored.sort(key=lambda entry: entry[:2])
        return [entry[2] for entry in scored[:self.HUB_COUNT]]

    def item_hubs(self, rating_key, leaf, query):
        """`/hubs/metadata/{id}/{related,postplay}` -- the preplay page's shelves.

        The client rewrites `/library/metadata/{id}` to `/hubs/metadata/{id}` and
        appends the leaf, so these are not the same endpoints as
        `/library/metadata/{id}/related`. plexsim routed neither, and a 404 is
        what the preplay page reads as "no related items" -- silently.

        Measured: `related` carries the librarySection triple and `allowSync`,
        `postplay` carries NEITHER, and the hub inside them is `movie.similar`
        keyed on the item's own `/similar`.
        """
        if leaf not in ("related", "postplay"):
            return ("error", 404, "plexsim: not found")
        row = self.catalog.items.get(int(rating_key)) if str(rating_key).isdigit() \
            else None
        if row is None:
            return ("error", 404, "plexsim: no such item")
        hubs = self.related_hubs(row, query)
        if leaf == "postplay":
            return self.hub_container(Hub=hubs)
        section = self.catalog.section(row["librarySectionID"]) or {}
        return self.hub_container(
            Hub=hubs, allowSync=True,
            librarySectionID=row["librarySectionID"],
            librarySectionTitle=section.get("title"),
            librarySectionUUID=section.get("uuid"))

    def related_hubs(self, row, query):
        """An item's related shelves, shared by every endpoint that carries them.

        Measured: `/hubs/metadata/{id}/related`, `/library/metadata/{id}/related`
        and a detail read's `includeRelated=1` all carry the SAME `movie.similar`
        hub; the latter two carried nothing on plexsim.
        """
        rows = [_listing_row(r) for r in self.similar_rows(row)]
        if not rows:
            return []
        kind = row.get("type") if row.get("type") in ("movie", "show") else "movie"
        return [self.home_hub(
            "%s.similar" % kind, "Related %s" % ("Shows" if kind == "show" else "Movies"),
            "/library/metadata/%s/similar" % row["ratingKey"], rows, query, kind=kind)]

    def recently_added_rows(self, type_id, section_id=None):
        """Newest first, by TYPE (1 movies, 2 shows, 4 episodes); None for others."""
        wanted = {"1": "movie", "2": "show", "4": "episode"}.get(type_id)
        if wanted is None:
            return None
        rows = [r for r in self.catalog.items.values() if r.get("type") == wanted]
        if section_id:
            rows = [r for r in rows if str(r.get("librarySectionID")) == str(section_id)]
        rows.sort(key=lambda r: -(r.get("addedAt") or 0))
        return rows

    # The global hub list, measured identically on both real servers: eight shelves
    # in this order, all promoted. (identifier, title, key, kind, style)
    GLOBAL_HUBS = [
        ("home.continue", "Continue Watching", "/hubs/home/continueWatching", "mixed", "hero"),
        ("home.ondeck", "On Deck", "/hubs/home/onDeck", "episode", "shelf"),
        ("home.movies.recent", "Recently Added Movies", "/hubs/home/recentlyAdded?type=1",
         "movie", "shelf"),
        ("home.television.recent", "Recently Added TV", "/hubs/home/recentlyAdded?type=2",
         "mixed", "shelf"),
        ("home.music.recent", "Recently Added Music", "/hubs/home/recentlyAdded?type=8",
         "album", "shelf"),
        ("home.photos.recent", "Recently Added Photos", "/hubs/home/recentlyAdded?type=13",
         "photo", "shelf"),
        ("home.videos.recent", "Recently Added Videos",
         "/hubs/home/recentlyAdded?type=1&personal=1", "clip", "shelf"),
        ("home.playlists", "Recent Playlists",
         "/playlists/all?type=15&sort=lastViewedAt:desc&playlistType=video,audio",
         "playlist", "shelf"),
    ]

    def hubs(self, query):
        """`/hubs` -- the global shelves, which python-plexapi's `library.hubs()`
        reads and the web client asks for by identifier.

        plexsim answered three shelves of its own devising: a Continue Watching
        typed `movie` whose hubKey was /library/all, and one "Recently Added in
        <library>" per section, typed `movie` even for the TV library -- none with
        a key, context, style or promoted flag. `identifier=` names the shelves the
        client wants; returning all of them regardless draws rows it did not ask
        for. There are no music, photo or personal-video libraries here, and no
        playlist history, so those four shelves are the reference's own empty ones.
        """
        wanted = [i for i in (query.get("identifier") or "").split(",") if i]
        watching = [_listing_row(r) for r in self.continue_watching()]
        rows = {
            "home.continue": watching,
            "home.ondeck": [r for r in watching if r.get("type") == "episode"],
            "home.movies.recent": [_listing_row(r) for r in self.recently_added_rows("1")],
            "home.television.recent": [_listing_row(r)
                                       for r in self.recently_added_rows("2")],
        }
        hubs = [self.home_hub(identifier, title, key, rows.get(identifier, []), query,
                              kind=kind, style=style, promoted=True)
                for identifier, title, key, kind, style in self.GLOBAL_HUBS
                if not wanted or identifier in wanted]
        return self.hub_container(Hub=hubs)

    def section_hubs(self, section_id, count=None):
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
        # Measured: the movie shelf's own key filters `originallyAvailableAt>=-1y`
        # and the shelf honours it -- the reference's is EMPTY for films from 2006
        # to 2015, where plexsim listed all of them. The TV shelf's key has no
        # window and neither does its shelf.
        year_ago = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 365 * 86400))
        released = sorted(episodes if is_show else
                          [r for r in items
                           if str(r.get("originallyAvailableAt") or "") >= year_ago],
                          key=lambda r: str(r.get("originallyAvailableAt") or ""),
                          reverse=True)
        added = sorted(items, key=lambda r: -(r.get("addedAt") or 0))
        # Rediscover, by its measured key: SHOWS watched between five years and six
        # weeks ago that still have unwatched episodes. plexsim filled this show
        # shelf with recently watched EPISODE rows.
        now = time.time()
        rediscover = [r for r in items
                      if r.get("type") == "show" and (r.get("viewCount") or 0) >= 1
                      and (r.get("leafCount") or 0) > (r.get("viewedLeafCount") or 0)
                      and now - 5 * 365 * 86400 <= (r.get("lastViewedAt") or 0)
                      <= now - 42 * 86400]

        def shelf(identifier, shelf_title, rows, kind, key, context, **extra):
            # Measured: a library shelf carries no librarySection fields of its own
            # (the container has them), and `hubKey` names the shelf's own rows as
            # `/library/metadata/<keys>` -- absent on an empty shelf. plexsim sent
            # the section pair on every shelf and made `hubKey` a copy of `key`.
            built = self.hub(identifier, shelf_title, [_listing_row(r) for r in rows],
                             count=count, type=kind, key=key, context=context,
                             style="shelf", **extra)
            keys = [str(r.get("ratingKey")) for r in built.get("Metadata") or []]
            if keys:
                built["hubKey"] = "/library/metadata/" + ",".join(keys)
            else:
                built.pop("hubKey", None)
            return built

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
                shelf("tv.rediscover.%s" % section_id, "Rediscover", rediscover, "show",
                      "/library/sections/%s/all?type=2&lastViewedAt<=-6w"
                      "&lastViewedAt>=-5y&unwatchedLeaves=1&viewCount>=1"
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

    # The seventeen typed hubs /hubs/search answers with, in the reference's order
    # and with its titles. Every one is present on every search, EMPTY ones
    # included -- the client lays the results page out from this list, so a server
    # that omits the empty hubs is handing it a different page. Note "Shows", not
    # "TV Shows", which is what plexsim called it.
    SEARCH_HUBS = [
        ("movie", "Movies"), ("show", "Shows"), ("artist", "Artists"),
        ("album", "Albums"), ("episode", "Episodes"), ("track", "Tracks"),
        ("collection", "Collections"), ("photoalbum", "Photo Albums"),
        ("autotag", "Automatic Tags"), ("photo", "Photos"), ("tag", "Tags"),
        ("actor", "Actors"), ("director", "Directors"), ("genre", "Genres"),
        ("playlist", "Playlists"), ("shared", "Shared"), ("place", "Places"),
    ]

    # The tag hubs, and which of a row's tag lists each one searches.
    SEARCH_TAG_HUBS = {"actor": "Role", "director": "Director", "genre": "Genre"}

    def _score(self, rank):
        """A relevance score in the shape the reference prints it.

        Measured: `/hubs/search` sends it as a STRING with five decimals
        ("0.73055") and `/library/search` sends the same number as a FLOAT. The
        value itself is Plex's own relevance, which plexsim cannot reproduce, so it
        ranks by match quality and prints it the same way.
        """
        return "%.5f" % max(0.0, min(1.0, rank))

    def search_matches(self, text, limit):
        """(typed row lists, tag hits) for one query, both ranked."""
        rows = self.catalog.search(text)
        needle = (text or "").strip().lower()

        def rank(row):
            title = str(row.get("title") or "").lower()
            # An exact title is the best match, a title that starts with the query
            # next, a later word last -- which is the ordering the reference's own
            # scores produce even though its numbers differ.
            if title == needle:
                return 0.95
            if title.startswith(needle):
                return 0.73
            return 0.31

        typed = {}
        for row in rows:
            typed.setdefault(row.get("type"), []).append(row)
        for kind in typed:
            typed[kind].sort(key=lambda r: (-rank(r), str(r.get("title") or "")))
            typed[kind] = typed[kind][:limit]

        tags = {}
        for hub, field in self.SEARCH_TAG_HUBS.items():
            seen = {}
            for row in self.catalog.items.values():
                for tag in row.get(field) or []:
                    name = str(tag.get("tag") or "")
                    words = re.split(r"[^0-9a-z]+", name.lower())
                    if len(needle) < 2 or not any(w.startswith(needle)
                                                  for w in words if w):
                        continue
                    entry = seen.setdefault(name, {"tag": tag, "rows": [], "row": row})
                    entry["rows"].append(row)
            if seen:
                tags[hub] = [seen[name] for name in sorted(seen)][:limit]
        return typed, tags

    def search(self, query):
        """`/hubs/search` -- the search page, which is seventeen hubs every time.

        plexsim answered only the hubs that had rows, so the results page was laid
        out from a list that changed per query. It also matched by substring and
        sent no `score`, which is what the client sorts by.
        """
        text = query.get("query")
        if not text:
            # Measured: a missing or empty query is a 400, not an empty result.
            return ("error", 400, "plexsim: bad request")
        limit = int(query.get("limit", 30) or 30)
        typed, tags = self.search_matches(text, limit)
        needle = text.strip().lower()

        hubs = []
        for kind, title in self.SEARCH_HUBS:
            rows = []
            for row in typed.get(kind, []):
                entry = _listing_row(row)
                title_words = re.split(r"[^0-9a-z]+",
                                       str(row.get("title") or "").lower())
                matched_title = any(w.startswith(needle) for w in title_words if w)
                entry["score"] = self._score(
                    0.95 if str(row.get("title") or "").lower() == needle
                    else 0.73 if matched_title else 0.33)
                if not matched_title:
                    # Measured: a row that matched through a TAG rather than its
                    # own title names the tag it matched on -- the client prints
                    # "because Ian Hubert" under the row.
                    for hub, field in self.SEARCH_TAG_HUBS.items():
                        for tag in row.get(field) or []:
                            words = re.split(r"[^0-9a-z]+",
                                             str(tag.get("tag") or "").lower())
                            if any(w.startswith(needle) for w in words if w):
                                entry["reason"] = hub
                                entry["reasonID"] = tag.get("id")
                                entry["reasonTitle"] = tag.get("tag")
                                break
                        if "reason" in entry:
                            break
                rows.append(entry)
            hub = {
                "title": title,
                "type": kind,
                "hubIdentifier": kind,
                # Measured: the search hubs carry an EMPTY context, and no hubKey.
                "context": "",
                "size": len(rows),
                "more": False,
                "style": "shelf",
            }
            if rows:
                hub["Metadata"] = rows
            if kind in tags:
                hub["Directory"] = [self._tag_result(entry, kind)
                                    for entry in tags[kind]]
                hub["size"] = len(hub["Directory"])
            hubs.append(hub)
        return self.bare_container(size=len(hubs), Hub=hubs)

    def _tag_result(self, entry, hub):
        """One tag row of a search result, field for field off the reference."""
        tag, row = entry["tag"], entry["row"]
        section_id = row.get("librarySectionID")
        section = self.catalog.section(section_id) or {}
        field = "actor" if hub == "actor" else hub
        result = {
            "key": "/library/sections/%s/all?%s=%s" % (section_id, field, tag.get("id")),
            "librarySectionID": section_id,
            "librarySectionKey": "/library/sections/%s" % section_id,
            "librarySectionTitle": section.get("title"),
            "librarySectionType": 2 if section.get("type") == "show" else 1,
            # A tag row matched because its SECTION did, which is what the
            # reference reports rather than naming the tag again.
            "reason": "section",
            "reasonID": int(section_id) if str(section_id).isdigit() else section_id,
            "reasonTitle": section.get("title"),
            "score": self._score(0.32),
            "type": "tag",
            "id": tag.get("id"),
            "filter": "%s=%s" % (field, tag.get("id")),
            "tag": tag.get("tag"),
            # Plex's own tagType codes: 4 is a person, 1 a genre.
            "tagType": 4 if hub in ("actor", "director") else 1,
            "count": len(entry["rows"]),
        }
        if tag.get("thumb"):
            result["thumb"] = tag["thumb"]
        return result

    def library_search(self, query):
        """`/library/search` -- the same matches as a flat `SearchResult` list.

        Advertised by plexsim's own /media/providers as `universalsearch` and
        answered with an empty container, which is the exact failure this project
        is about. Measured: one `SearchResult` per hit, each holding a `score` (a
        FLOAT here, where /hubs/search sends the same number as a string) and
        either a `Metadata` row or a `Directory` tag row.
        """
        text = query.get("query")
        if not text:
            return ("error", 400, "plexsim: bad request")
        limit = int(query.get("limit", 30) or 30)
        typed, tags = self.search_matches(text, limit)
        needle = text.strip().lower()
        results = []
        for kind, _title in self.SEARCH_HUBS:
            for row in typed.get(kind, []):
                score = 0.95 if str(row.get("title") or "").lower() == needle \
                    else 0.73 if str(row.get("title") or "").lower().startswith(needle) \
                    else 0.33
                results.append({"score": float(self._score(score)),
                                "Metadata": _listing_row(row)})
            for entry in tags.get(kind, []):
                results.append({"score": 0.32,
                                "Directory": self._tag_result(entry, kind)})
        return self.bare_container(size=len(results), SearchResult=results)

    # The search providers /search lists beside its results, measured verbatim.
    # The `m_` prefix is the reference's, not a typo.
    SEARCH_PROVIDERS = [
        {"key": "/system/search", "m_title": "Local Network", "m_type": "mixed"},
        {"key": "/search?type=10", "m_title": "Tracks", "m_type": "tracks"},
        {"key": "/search/actor", "m_title": "Actors", "m_type": "actors"},
        {"key": "/search/director", "m_title": "Directors", "m_type": "directors"},
    ]

    def legacy_search(self, query):
        """`/search` -- the old flat search, which also lists the PROVIDERS.

        Measured, and both refusals are different: no `query` parameter at all is
        a 400 and an EMPTY one is a 404. The container's `size` counts the Provider
        rows as well as the results (1 row + 4 providers = 5), each row carries
        `personal` and `sourceTitle` naming this server, and plexsim 404'd the
        whole endpoint.
        """
        if "query" not in query:
            return ("error", 400, "plexsim: bad request")
        if not query.get("query"):
            return ("error", 404, "plexsim: not found")
        limit = int(query.get("limit", 30) or 30)
        typed, _tags = self.search_matches(query["query"], limit)
        rows = []
        for kind, _title in self.SEARCH_HUBS:
            for row in typed.get(kind, []):
                entry = _listing_row(row)
                entry["personal"] = True
                entry["sourceTitle"] = self.catalog.friendly
                rows.append(entry)
        # The container is the tag one (identifier plus the media-tag pair, no
        # allowSync) and its `size` counts the Provider rows as well as the
        # results -- measured: 1 result + 4 providers answered size 5.
        body = self.tag_container(Metadata=rows, Provider=self.SEARCH_PROVIDERS)
        body["MediaContainer"]["size"] = len(rows) + len(self.SEARCH_PROVIDERS)
        return body

    def voice_search(self, query):
        """`/hubs/search/voice` -- the same matches, keyed by edit distance.

        Measured: one hub per TYPE, titled with the bare type name, identified
        `results.search.<n>` with context `hub.results.search`, and both the hub
        and every row carry a `distance`. plexsim 404'd it.
        """
        text = query.get("query")
        if not text:
            return ("error", 400, "plexsim: bad request")
        limit = int(query.get("limit", 30) or 30)
        typed, _tags = self.search_matches(text, limit)
        hubs = []
        for kind, _title in self.SEARCH_HUBS:
            rows = typed.get(kind)
            if not rows:
                continue
            # The reference's distance is its own fuzzy-match cost; plexsim reports
            # the query's length, which is what an exact word-prefix hit costs it.
            distance = len(text.strip())
            entries = []
            for row in rows:
                entry = _listing_row(row)
                entry["distance"] = distance
                entries.append(entry)
            hubs.append(self.home_hub(
                "results.search.%d" % (len(hubs) + 1), kind, None,
                entries, query, kind=kind, distance=distance))
            # Measured: a voice hub carries `hubKey` and NO `key`, and its context
            # is the shared `hub.results.search` rather than one per hub.
            hubs[-1].pop("key", None)
            hubs[-1]["context"] = "hub.results.search"
        return self.bare_container(size=len(hubs), Hub=hubs)

    def _items_for_uri(self, uri, continuous=False):
        """Resolve a playQueue `uri` to the rows it should enqueue.

        Two shapes reach here, and only the first was handled before:

          server://<machine>/com.plexapp.plugins.library/library/metadata/1001
          server://<machine>/com.plexapp.plugins.library/library/sections/1/all

        The second is what the library's "Play all" button sends. Returning an
        empty queue for it makes the web client show "An error occurred loading
        items to play" without ever calling this endpoint again.
        """
        path = urllib.parse.unquote(uri or "")
        # A library section, with whatever filter and sort the URI carries. A
        # smart playlist or collection IS that query -- dropping it would make
        # every smart list the whole section, which looks like a working list.
        match = re.search(r"/library/sections/([^/]+)/all", path)
        if match:
            section_id = match.group(1)
            section = self.catalog.section(section_id) or {}
            pairs = urllib.parse.parse_qsl(path.partition("?")[2],
                                           keep_blank_values=True)
            inner = {k: v for k, v in pairs}
            inner["_pairs"] = pairs
            rows = self.typed_rows(section_id, inner)
            rows, _ = self.apply_filters(rows, inner, section)
            rows, unsupported = self._sorted(rows, inner.get("sort") or "")
            if unsupported:
                raise filters_module.FilterError(
                    500, "unsupported sort key %s" % unsupported)
            return self._playable(rows)
        # A playlist's items, which is what Play on a playlist sends.
        match = re.search(r"/playlists/(\d+)/items", path)
        if match:
            playlist = self.library.playlist(match.group(1))
            if playlist is None:
                return []
            return [row for row, _item in self.library.playlist_items(playlist)]
        # A collection's children.
        match = re.search(r"/library/collections/(\d+)/(?:children|items)", path)
        if match:
            collection = self.library.collection(match.group(1))
            if collection is None:
                return []
            return self._playable(self.library.collection_children(collection))
        # One or MORE items: a multi-select Play sends a comma-joined key list, and
        # a regex that stops at the first key silently plays one of them.
        match = re.search(r"/library/metadata/([\d,]+)", path)
        if match:
            rows = [self.catalog.items[int(k)] for k in match.group(1).split(",")
                    if k.isdigit() and int(k) in self.catalog.items]
            if continuous and len(rows) == 1 and rows[0].get("type") == "episode":
                # Measured: `continuous=1` on an episode appends the episodes that
                # FOLLOW it within its own season -- not the rest of the show.
                episode = rows[0]
                siblings = self.catalog.children(episode.get("parentRatingKey"))
                index = next((i for i, r in enumerate(siblings)
                              if str(r.get("ratingKey")) == str(episode["ratingKey"])),
                             None)
                if index is not None:
                    return siblings[index:]
            return self._playable(rows)
        # A bare trailing ratingKey, which some clients send.
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit() and int(tail) in self.catalog.items:
            return self._playable([self.catalog.items[int(tail)]])
        return []

    def _playable(self, rows):
        """Expand any show or season into the episodes a player can actually play.

        Measured: a queue over a show holds its SIX EPISODES, a queue over a season
        holds that season's three, and a queue over a show library's section holds
        every leaf -- never the parent row. plexsim enqueued the parent, which has
        no Media at all, so Play, Play All and Shuffle on every show page, season
        page and TV library handed the player a row it could not play and the web
        client said "An error occurred loading items to play".
        """
        out = []
        for row in rows:
            if row is None:
                continue
            if row.get("type") in ("show", "season"):
                out.extend(self.catalog.leaves(row["ratingKey"]))
            else:
                out.append(row)
        return out

    @staticmethod
    def source_uri(uri, rows):
        """The `playQueueSourceURI` a real PMS reports, which is NOT what it was sent.

        Measured: `server://<machine>/com.plexapp.plugins.library/library/metadata/1`
        comes back as `library://x/item/%2Flibrary%2Fmetadata%2F1`, and anything
        that names a LIST -- a section, a collection's children, a comma-joined key
        set -- comes back as `library://x/directory/<the same encoding>`. plexsim
        echoed the raw `server://` URI, so a client that re-read the queue got a
        source it had not sent and could not match.
        """
        path = urllib.parse.unquote(uri or "")
        marker = "/com.plexapp.plugins.library"
        if marker not in path:
            # An unresolvable but well-formed uri is echoed verbatim, measured.
            return uri or ""
        path = path.split(marker, 1)[1]
        single = re.fullmatch(r"/library/metadata/\d+", path) is not None
        kind = "item" if single else "directory"
        return "library://x/%s/%s" % (kind, urllib.parse.quote(path, safe=""))

    def _queue_items(self, rows):
        """Wrap rows as queue items with globally increasing, stable ids."""
        items = []
        for row in rows:
            self.next_play_queue_item += 1
            items.append({"id": self.next_play_queue_item,
                          "ratingKey": int(row["ratingKey"])})
        return items

    def create_play_queue(self, query):
        """`POST /playQueues` -- every client builds a queue before playing.

        `shuffle=1` is the Shuffle context-menu entry. Measured refusals: a POST
        with no `uri` AND no `playlistID`, or with no `type`, is a 400 -- plexsim
        answered 200 with an empty queue, which the client plays as nothing.
        """
        uri = query.get("uri", "")
        playlist_id = query.get("playlistID")
        if not query.get("type"):
            return ("error", 400, "plexsim: bad request")
        if not uri and not playlist_id:
            return ("error", 400, "plexsim: bad request")
        playlist = self.library.playlist(playlist_id) if playlist_id else None
        if playlist is not None:
            # Measured: `playlistID=` ALONE, with no uri at all, builds the same
            # queue -- and the response then names the playlist instead of
            # carrying a source URI.
            rows = [row for row, _item_id in self.library.playlist_items(playlist)]
        else:
            rows = self._items_for_uri(uri, continuous=query.get("continuous") == "1")
        if query.get("shuffle") == "1":
            # Deterministic per queue rather than random, so a test can assert
            # the order changed without asserting which order.
            rows = list(reversed(rows))

        with self.lock:
            queue_id = self.next_play_queue
            self.next_play_queue += 1
            items = self._queue_items(rows)
            self.play_queues[queue_id] = {
                "items": items,
                "source": "" if playlist is not None else self.source_uri(uri, rows),
                "shuffled": query.get("shuffle") == "1",
                "version": 1,
                "selected": items[0]["id"] if items else None,
                "last_added": None,
                "playlist": (int(playlist["ratingKey"]), playlist["title"])
                            if playlist is not None else None,
            }
        logger.info("PLAYQUEUE %s -> %d item(s) from uri=%s",
                    queue_id, len(rows), (uri or playlist_id or "")[:80])
        return self.play_queue_container(queue_id, query)

    def get_play_queue(self, queue_id, query=None):
        if not str(queue_id).isdigit() or int(queue_id) not in self.play_queues:
            # Measured: an unknown queue is a 404, where plexsim invented an
            # empty one -- which a client reads as "the queue is still there".
            return ("error", 404, "plexsim: no such play queue")
        return self.play_queue_container(int(queue_id), query)

    def play_queue_container(self, queue_id, query=None):
        """One play-queue response, used by create, fetch and add-to.

        Two things here decide whether the client keeps the answer at all. Its
        play-queue fetch DISCARDS the whole response when the returned
        `playQueueSelectedItemID` is not the one it already holds, and it refreshes
        only when `playQueueVersion` changed. plexsim recomputed the item ids from
        their POSITION and hardcoded the version at 1, so every id moved whenever
        anything was inserted and the version never moved at all: once the player
        was past the first item, every refresh, Add to Queue and Play Next response
        was silently thrown away.

        `playQueueLastAddedItemID` is the third: the client disables the Up Next
        shuffle button once it is present, and it tracks PLAIN adds only.
        """
        query = query or {}
        with self.lock:
            queue = self.play_queues.get(queue_id)
            if queue is None:
                return ("error", 404, "plexsim: no such play queue")
            items = list(queue["items"])
            source = queue["source"]
            shuffled = bool(queue["shuffled"])
            version = queue["version"]
            selected = queue["selected"]
            last_added = queue["last_added"]
            playlist = queue["playlist"]
        metadata = []
        for entry in items:
            row = self.catalog.items.get(entry["ratingKey"])
            if row is None:
                continue
            item = _full(row)
            item["playQueueItemID"] = entry["id"]
            metadata.append(item)
        offset = next((i for i, m in enumerate(metadata)
                       if m["playQueueItemID"] == selected), 0)
        total = len(metadata)
        # `window`/`center`: the client asks for a slice AROUND the current item
        # rather than the whole queue, and `playQueueTotalCount` still reports the
        # whole thing. plexsim ignored both and sent every row every time.
        window = query.get("window")
        centre = query.get("center")
        if str(centre).isdigit():
            offset = next((i for i, m in enumerate(metadata)
                           if m["playQueueItemID"] == int(centre)), offset)
        if str(window).isdigit():
            span = int(window)
            low = max(0, offset - span)
            metadata = metadata[low:offset + span + 1]
            offset = offset - low
        fields = {
            "Metadata": metadata,
            "playQueueID": queue_id,
            "playQueueVersion": version,
            "playQueueShuffled": shuffled,
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
        }
        if playlist is not None:
            # Measured: a queue built from a playlist names it and carries NO
            # source URI at all.
            fields["playQueuePlaylistID"] = playlist[0]
            fields["playQueuePlaylistTitle"] = playlist[1]
        elif source:
            fields["playQueueSourceURI"] = source
        if metadata:
            fields["playQueueSelectedItemID"] = selected if selected is not None \
                else metadata[0]["playQueueItemID"]
            fields["playQueueSelectedItemOffset"] = offset
            fields["playQueueSelectedMetadataItemID"] = str(
                items[min(offset, len(items) - 1)]["ratingKey"])
            fields["playQueueTotalCount"] = total
        if last_added is not None:
            fields["playQueueLastAddedItemID"] = last_added
        body = self.container(**fields)
        # Measured: a play-queue container carries no allowSync, which the generic
        # library container adds.
        body["MediaContainer"].pop("allowSync", None)
        return body

    def decision(self, query, headers=None):
        """`/video/:/transcode/universal/decision` -- always direct play.

        The codes are what plex-for-kodi's ServerDecision reads: it treats
        1000 <= mdeDecisionCode < 2000 as "server agrees".  The echoed
        Metadata/Media/Part carries `decision="directplay"` because the
        stricter clients (plx-native) read the verdict off `Part.decision`
        rather than the container codes.

        Which codes appear is measured, and it is not all of them at once: on a
        direct play a real PMS sends ONLY the `mde*` pair, and the general,
        directPlay and transcode triples appear only when direct play is REFUSED
        (`directPlay=0` answers `directPlayDecisionCode: 3000` with
        `generalDecisionCode`/`transcodeDecisionCode: 1001`). plexsim sent all
        three 1000s on every request, which tells a client that a transcode
        decision was made when none was.

        The container's own fields are measured too: `allowSync` is the string
        "1", and `librarySectionID` and `mediaTagVersion` are strings here where
        every other endpoint sends them as numbers. `resourceSession` is the
        client's X-Plex-Session-Identifier, falling back to its client identifier
        and to an EMPTY string -- re-measured with a session identifier present,
        correcting the earlier note that it is always the client identifier.
        """
        def param(name):
            return query.get(name) or (headers or {}).get(name)

        row, refusal = self._decision_target(query, headers)
        if refusal:
            return refusal
        section = self.catalog.section(row["librarySectionID"]) or {}
        fields = {
            "allowSync": "1",
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": "1655122614",
            "librarySectionID": str(row["librarySectionID"]),
            "librarySectionTitle": section.get("title"),
            "librarySectionUUID": section.get("uuid"),
        }
        path = query.get("path", "")
        if query.get("directPlay") == "0" or query.get("hasMDE") != "1":
            # Direct play refused -- and plexsim cannot convert, so this is the
            # measured "neither" answer, not the "Conversion OK" a PMS with an
            # encoder gives. A real PMS answers exactly this for a client whose
            # profile has no conversion target (`Generic`, `Sonos`): general 2000,
            # transcode 4005 naming the protocol, NO Metadata and no
            # resourceSession. plexsim answered 1001 "Conversion OK.", so the client
            # went on to request a transcode that 404'd and showed a generic error;
            # a code of 2000 or more is what it shows its own message for.
            # Without hasMDE the direct-play text always names protocol `http`,
            # whatever protocol was asked for (measured).
            media = (row.get("Media") or [{}])[0]
            reason = ("Direct play is disabled." if query.get("directPlay") == "0"
                      else "No direct play video profile exists for protocol http, "
                           "with container %s, and video codec %s."
                           % (media.get("container") or "", media.get("videoCodec") or ""))
            fields.update(
                directPlayDecisionCode=3000,
                directPlayDecisionText="App cannot direct play this item. " + reason,
                generalDecisionCode=2000,
                generalDecisionText="Neither direct play nor conversion is available.",
                transcodeDecisionCode=4005,
                transcodeDecisionText="Cannot convert this item. No conversion profile "
                                      "found for protocol %s."
                                      % (query.get("protocol") or "http"),
            )
            logger.info("DECISION refused for path=%s", path)
            return self.container(size=0, **fields)

        # The timeline's Bandwidths block appears only once a decision has been
        # made for the item, so the decision is what arms it.
        self.decided.add(int(row["ratingKey"]))
        fields["resourceSession"] = (param("X-Plex-Session-Identifier")
                                     or param("X-Plex-Client-Identifier") or "")
        fields.update(mdeDecisionCode=1000, mdeDecisionText="Direct play OK.")
        logger.info("DECISION direct play for path=%s", path)
        return self.container(Metadata=[self._decision_row(row, query)], **fields)

    def _decision_target(self, query, headers=None):
        """The row a decision or a transcode start is FOR, or the 400 refusing it.

        Measured, every one a 400 with nothing in the body: no parameters; a path
        that is not `/library/metadata/{id}` of a playable row (an unknown id, a
        show, a season, `/children`, a section); a missing, non-numeric or
        out-of-range `mediaIndex`; an out-of-range `partIndex` (a MISSING one is
        fine); and a request whose profile, platform and device all name no client
        profile. plexsim answered "Direct play OK." to every one of them, for things
        it cannot play and for nothing at all.
        """
        def param(name):
            return query.get(name) or (headers or {}).get(name)

        if not any(param(name) in CLIENT_PROFILES for name in
                   ("X-Plex-Client-Profile-Name", "X-Plex-Platform", "X-Plex-Device")):
            return None, ("error", 400, "plexsim: bad request")
        match = re.search(r"/library/metadata/(\d+)/?$", query.get("path") or "")
        row = self.catalog.items.get(int(match.group(1))) if match else None
        media = (row or {}).get("Media") or []
        if not media:
            return None, ("error", 400, "plexsim: bad request")
        index = query.get("mediaIndex") or ""
        if not index.isdigit() or int(index) >= len(media):
            return None, ("error", 400, "plexsim: bad request")
        part = query.get("partIndex")
        if part is not None and (not part.isdigit()
                                 or int(part) >= len(media[int(index)].get("Part") or [])):
            return None, ("error", 400, "plexsim: bad request")
        return row, None

    def _decision_row(self, row, query):
        """The row a direct-play decision echoes, measured against the detail row.

        It is the detail row without `Guid` and `Image` (and a movie's `summary`),
        with `selected` on the chosen Media, `decision`/`selected` on its Parts and
        `location: "direct"` on every Stream -- and in JSON the Media, Part and
        Stream ids, `aspectRatio` and `hasVoiceActivity` are STRINGS here, where the
        detail endpoint sends numbers and a boolean.
        """
        item = _full(row)
        item.pop("Guid", None)
        item.pop("Image", None)
        if row.get("type") == "movie":
            item.pop("summary", None)
        chosen = int(query.get("mediaIndex") or 0)
        media = []
        for position, source in enumerate(item.get("Media") or []):
            copy = {k: v for k, v in source.items() if k != "Part"}
            copy["id"] = str(copy.get("id"))
            if copy.get("aspectRatio") is not None:
                copy["aspectRatio"] = str(copy["aspectRatio"])
            copy["hasVoiceActivity"] = "1" if copy.get("hasVoiceActivity") else "0"
            if position == chosen:
                copy["selected"] = True
            parts = []
            for part in source.get("Part") or []:
                entry = {k: v for k, v in part.items()
                         if k not in ("Stream", "exists", "accessible")}
                entry["id"] = str(entry.get("id"))
                entry["decision"] = "directplay"
                entry["selected"] = True
                streams = [dict(stream, id=str(stream.get("id")), location="direct")
                           for stream in part.get("Stream") or []]
                if streams:
                    entry["Stream"] = streams
                parts.append(entry)
            copy["Part"] = parts
            media.append(copy)
        item["Media"] = media
        return item

    def download_queue(self, method, segments, query, headers=None):
        """`/downloadQueue` -- the mobile apps' download queue, which `GET /` names.

        Measured: the bare GET is the plug-in dispatcher's `{size 0, content
        plugins}`; `POST /downloadQueue` answers a `DownloadQueue` row for the
        calling client -- the SAME queue on a second POST from that client -- and
        `/downloadQueue/{id}` reads it back, with `/items` an empty container. An
        unknown id is a 404, and so is a DELETE of a queue that exists. plexsim
        404'd all of it while listing `downloadQueue` among its root endpoints.
        What adding an item does was not measured, so it is not answered.
        """
        if len(segments) == 1:
            if method == "POST":
                client = (query.get("X-Plex-Client-Identifier")
                          or (headers or {}).get("X-Plex-Client-Identifier") or "")
                queue = self.download_queues.get(client)
                if queue is None:
                    queue = {"id": len(self.download_queues) + 1, "owner": 1,
                             "clientIdentifier": client, "itemCount": 0, "status": "done"}
                    self.download_queues[client] = queue
                return self.bare_container(size=1, DownloadQueue=[dict(queue)])
            return self.bare_container(size=0, content="plugins")
        queue = next((q for q in self.download_queues.values()
                      if str(q["id"]) == segments[1]), None)
        if queue is None or method not in ("GET", "HEAD"):
            return ("error", 404, "plexsim: not found")
        if len(segments) == 2:
            return self.bare_container(size=1, DownloadQueue=[dict(queue)])
        if len(segments) == 3 and segments[2] == "items":
            return self.bare_container(size=0)
        return ("error", 404, "plexsim: not found")

    def timeline(self, query, headers=None, address=None):
        """`/:/timeline` -- the player's progress report, and what CREATES a session.

        Two things measured here that plexsim had wrong. First, this endpoint is
        what puts a row in /status/sessions: a bare timeline with no play queue,
        no decision and no byte read is enough, and `state=stopped` removes the
        row. plexsim only stored a viewOffset, so Now Playing was always empty.

        Second, the response is NOT an echo of the state sent. A real PMS answers
        `{size, playbackState, skipCount, viewCount, viewOffset}` where
        `playbackState` is `ignore` until the client passes 60 s, then `progress`,
        and `complete` once it stops at the end -- and `viewOffset` on the
        response is `-1`, not the offset. An earlier note in docs/findings.md
        recorded `{size, playbackState}` with the state echoed; that was wrong.
        """
        rating_key = query.get("ratingKey") or ""
        if not query.get("state"):
            # Measured: `state` is the ONE required parameter -- a timeline without
            # it is a 400 even with a ratingKey and a key, and `state` alone with
            # nothing else is a 200. plexsim defaulted it to `stopped`.
            return ("error", 400, "plexsim: bad request")
        state = query.get("state")
        if state not in self.TIMELINE_STATES:
            # Measured: `state` is a CLOSED set and anything else is a 400.
            # plexsim treated an unknown state as live and opened a session.
            return ("error", 400, "plexsim: bad request")
        offset = query.get("time")
        duration = query.get("duration")
        offset_ms = int(offset) if str(offset).isdigit() else 0
        total_ms = int(duration) if str(duration).isdigit() else None
        row = self.catalog.items.get(int(rating_key)) if rating_key.isdigit() else None
        if rating_key and row is None:
            # Measured: a ratingKey the server does not have is a 404, not a 200
            # with an empty container.
            return ("error", 404, "plexsim: no such item")

        # The player reports which queue item it is on, and a real PMS MOVES the
        # queue's selection to it. The client's play-queue fetch then DISCARDS any
        # response whose selected id is not the one it holds, so a server that
        # never moves the selection has every queue refresh thrown away once
        # playback is past the first item.
        if str(query.get("playQueueItemID") or "").isdigit():
            wanted = int(query["playQueueItemID"])
            with self.lock:
                for queue in self.play_queues.values():
                    if any(e["id"] == wanted for e in queue["items"]):
                        queue["selected"] = wanted
                        break

        playback_state = "ignore"
        view_count = 0
        fields = {}
        if row is not None:
            client = (query.get("X-Plex-Client-Identifier")
                      or (headers.get("X-Plex-Client-Identifier") if headers else None))
            device = self.sessions.device_id(client)
            playing_session = (query.get("X-Plex-Session-Identifier")
                               or (headers.get("X-Plex-Session-Identifier") if headers else None)
                               or client or "")
            self.library.progress(int(rating_key), offset_ms, state=state,
                                  duration=total_ms, device_id=device,
                                  session=playing_session)
            entry = self.sessions.touch(row, query, headers, address, state,
                                        offset_ms, total_ms)
            self.notify_playing(entry, row, state, offset_ms)
            # Measured thresholds: nothing is reported back at or under a minute, on
            # ANY state, and past 90% the play is `complete` on any state too.
            # `viewCount` is the item's own count -- 0 on a first play in progress,
            # measured -- where plexsim sent 1 for anything past a minute.
            total = total_ms or row.get("duration") or 0
            if offset_ms > 60000:
                playback_state = "progress"
                if total and offset_ms >= total * 0.9:
                    playback_state = "complete"
            view_count = int(row.get("viewCount") or 0)
            if state == "stopped":
                self.sessions.record_play(row, offset_ms / 1000 if offset_ms else 0,
                                          device_id=device)
            media = (row.get("Media") or [{}])[0]
            bitrate = media.get("bitrate") or 0
            # Re-measured: `Bandwidths` appears once a DECISION has been made for
            # this item and then on every timeline including `stopped`, and it
            # disappears again after the stopping one. plexsim keyed it on the
            # state alone, so it was present before any decision and absent on the
            # stop -- exactly inverted at both ends. The resolution here is the
            # LABEL (`1080p`), not the raw `videoResolution`.
            if int(rating_key) in self.decided and bitrate:
                resolution = media.get("videoResolution") or ""
                fields["Bandwidths"] = [{"Bandwidth": [{
                    "time": 0, "bandwidth": int(round(bitrate * 2.1)),
                    "resolution": filters_module.RESOLUTION_TITLES.get(
                        str(resolution), str(resolution))}]}]
            if state == "stopped":
                self.decided.discard(int(rating_key))
            # A terminate is delivered HERE, not in the terminate response.
            session_id = entry.get("id")
            killed = self.sessions.termination(session_id)
            if killed:
                fields["terminationCode"], fields["terminationText"] = killed
            logger.info("TIMELINE %s state=%s t=%sms session=%s",
                        rating_key, state, offset, entry.get("sessionKey"))
        if row is None:
            # Measured: with no ratingKey at all the answer is just the two fields.
            return self.bare_container(size=0, playbackState=playback_state)
        # Re-measured, correcting an earlier finding: `viewOffset` is the item's
        # STORED offset and is -1 only when it has none. plexsim always sent -1, so
        # a client reading its resume point off the timeline never found one.
        stored = row.get("viewOffset")
        # Measured: `size` counts the Bandwidths block and nothing else -- 0 before a
        # decision, 1 after. plexsim sent 1 on every response.
        return self.bare_container(size=1 if fields.get("Bandwidths") else 0,
                                   playbackState=playback_state,
                                   skipCount=0, viewCount=view_count,
                                   viewOffset=int(stored) if stored else -1,
                                   **fields)

    def notify_playing(self, entry, row, state, offset_ms):
        """The `playing` notification -- a FIFTH type, and the one Now Playing
        depends on. Measured: one frame per timeline, and the client drops the
        row from its own state on `stopped` or refetches /status/sessions when it
        does not already know the sessionKey."""
        self.notify("playing", "PlaySessionStateNotification", [{
            "sessionKey": entry.get("sessionKey"),
            "clientIdentifier": (entry.get("Player") or {}).get("machineIdentifier", ""),
            "guid": "",
            "ratingKey": str(row.get("ratingKey")),
            "url": "",
            "key": "/library/metadata/%s" % row.get("ratingKey"),
            "viewOffset": int(offset_ms or 0),
            "state": state or "playing",
        }])

    def session_rows(self):
        """`/status/sessions` -- the item row plus its session blocks."""
        rows = []
        for entry in self.sessions.live():
            row = entry.get("row") or {}
            item = _full(row)
            item["sessionKey"] = entry["sessionKey"]
            item["viewOffset"] = entry["viewOffset"]
            # The selected flags a real session row carries on its own media.
            for media in item.get("Media") or []:
                media["selected"] = True
                for part in media.get("Part") or []:
                    part["decision"] = "directplay"
                    part["selected"] = True
                    for stream in part.get("Stream") or []:
                        stream["location"] = "direct"
            item["User"] = entry["User"]
            item["Player"] = entry["Player"]
            item["Session"] = entry["Session"]
            rows.append(item)
        return rows

    def _keyed_row(self, query):
        """The row a `key=` parameter names, or the refusal a real PMS answers.

        Measured on every one of these state writes: a MISSING key is a 400 and a
        key the server does not have is a 404. plexsim answered an empty 200 to
        both, which tells the client the write landed.
        """
        raw = query.get("key")
        if raw is None or raw == "":
            return None, ("error", 400, "plexsim: bad request")
        key = str(raw).rsplit("/", 1)[-1]
        row = self.catalog.items.get(int(key)) if key.isdigit() else None
        if row is None:
            return None, ("error", 404, "plexsim: no such item")
        return row, None

    def progress(self, query):
        """`/:/progress?key=&time=&state=` -- the OTHER way a client reports resume.

        Not an alias of `/:/timeline`, which plexsim had it routed as: this one keys
        on **`key`**, not `ratingKey`, and it answers an EMPTY body rather than a
        MediaContainer. Routed at the timeline it read no key at all, so every
        client that reports progress this way -- plex_debrid, Kodi/PKC, Infuse,
        the older Plex apps -- lost its resume point silently.
        """
        row, refusal = self._keyed_row(query)
        if refusal:
            return refusal
        offset = query.get("time")
        offset_ms = int(offset) if str(offset).isdigit() else 0
        self.library.progress(int(row["ratingKey"]), offset_ms,
                              state=query.get("state") or "stopped")
        logger.info("PROGRESS %s t=%sms state=%s", row["ratingKey"], offset,
                    query.get("state"))
        return ("empty", 200)

    def set_watched(self, query, watched):
        """`/:/scrobble` and `/:/unscrobble` -- Mark as Played / Unplayed.

        Two things a container response gets wrong. A real PMS answers with an
        EMPTY 200, and marking a show or season has to walk down to the leaves:
        the client's own menu predicate reads `unviewedLeafCount` for those
        types, so a show whose episodes stayed unwatched keeps offering "Mark as
        Played" no matter what the show row says.
        """
        row, refusal = self._keyed_row(query)
        if refusal:
            return refusal
        changed = self.library.scrobble(int(row["ratingKey"]), watched)
        logger.info("%s %s -> %d leaf row(s)",
                    "SCROBBLE" if watched else "UNSCROBBLE", row["ratingKey"],
                    len(changed))
        return ("empty", 200)

    def rate(self, query):
        """`/:/rate?key=&identifier=&rating=` -- the star rating. -1 clears."""
        row, refusal = self._keyed_row(query)
        if refusal:
            return refusal
        rating = query.get("rating")
        try:
            value = float(rating)
        except (TypeError, ValueError):
            # Measured: an unparseable rating is a 400, where plexsim silently
            # cleared the rating instead.
            return ("error", 400, "plexsim: bad request")
        self.library.rate(int(row["ratingKey"]), value)
        logger.info("RATE %s -> %s", row["ratingKey"], rating)
        return ("empty", 200)

    def remove_from_continue_watching(self, query):
        """The context-menu entry of the same name: drop the resume point.

        Advertised as an Action on the `actions` provider feature; the client
        only draws the entry when that Action is present.
        """
        key = (query.get("ratingKey") or query.get("key") or "").rsplit("/", 1)[-1]
        if not key.isdigit() or self.library.remove_from_continue_watching(
                int(key)) is None:
            # Measured: an unknown ratingKey is a 404 here, where plexsim answered
            # an empty 200 for anything at all.
            return ("error", 404, "plexsim: no such item")
        return self.bare_container(size=0)

    def continue_watching(self):
        """Rows with a resume point, newest first -- the Continue Watching hub."""
        rows = [r for r in self.catalog.items.values()
                if r.get("viewOffset") and r.get("type") in ("movie", "episode")
                and not r.get("_hiddenFromContinueWatching")]
        return sorted(rows, key=lambda r: -(r.get("lastViewedAt") or 0))

    def select_stream(self, part_id, query):
        """`PUT /library/parts/{partID}?audioStreamID=&subtitleStreamID=`.

        Track selection from the player, and from Get Info. plexsim never
        repackages, so the choice only changes which Stream carries `selected` --
        which is what the client reads back.
        """
        wanted = {}
        for name, stream_type in (("audioStreamID", 2), ("subtitleStreamID", 3)):
            # Measured: a value that is not a number -- empty, or `abc` -- is
            # IGNORED with a 200 and the selection left alone. Only a numeric id of
            # the wrong type is refused. plexsim 400'd the malformed ones.
            if name in query and str(query[name]).isdigit():
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
        # Measured: a stream id that is not a stream of the right TYPE on this item
        # is a 400, and the selection is left untouched. plexsim accepted anything
        # with a 200 and then cleared every selection, because nothing matched --
        # so a rejected request left the item with no selected track at all and the
        # track picker went blank. `0` is the client's "None" pseudo-stream and is
        # valid for subtitles only.
        available = {stream_type: {str(s.get("id"))
                                  for media in owner.get("Media", [])
                                  for part in media.get("Part", [])
                                  for s in part.get("Stream", [])
                                  if s.get("streamType") == stream_type}
                     for stream_type in (2, 3)}
        for stream_type, chosen in wanted.items():
            if str(chosen) == "0" and stream_type == 3:
                continue
            if str(chosen) not in available.get(stream_type, set()):
                logger.warning("STREAM SELECT refused: %s is not a type-%d stream of %s",
                               chosen, stream_type, owner["ratingKey"])
                return ("error", 400, "plexsim: bad request")
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
            # Measured: `GET /library/metadata/{id}/refresh` and `/analyze` are 404s.
            return ("error", 404, "plexsim: not found")

        if action == "refresh":
            self.library.refresh(row)
            # The FULL seven-frame sequence a real PMS pushes. One timeline(3) is
            # not enough: without state 5 the client never refetches the row it just
            # refreshed, and without the final frame clearing `metadataState` its
            # spinner never stops.
            self.notify_refreshed(row)
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
        field = "thumb" if kind == "posters" else "art"
        stored = row.get("_poster_url") if kind == "posters" else row.get("_art_url")
        current = row.get(field)
        # `selected` is whichever candidate the row's artwork actually POINTS AT: a
        # versioned `/library/metadata/{id}/thumb/{ver}` means the remote one, a bare
        # `/thumb/1` the generated placeholder. plexsim keyed it on "is there any
        # artwork at all", so after picking the placeholder NOTHING came back selected
        # and the picker showed no current choice.
        chose_remote = bool(stored)
        placeholder = "/library/metadata/%s/%s/1" % (row["ratingKey"], field)
        if stored:
            entries.append({
                "key": stored,
                "ratingKey": stored,
                "thumb": stored,
                "selected": chose_remote,
                "provider": "tmdb",
            })
        entries.append({
            "key": placeholder,
            "ratingKey": placeholder,
            "thumb": placeholder,
            "selected": not chose_remote and bool(current),
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
        # Measured: the body is the NEW artwork path, bare -- `/library/metadata/1/
        # thumb/1789241428` -- because the client assigns the response straight onto
        # the model's `thumb`. An empty 200 there blanks the tile.
        return ("text", 200, row[field])

    def metadata_tree(self, row):
        """`/library/metadata/{id}/tree` -- the raw item/media/part tree.

        Measured shape: MetadataItem rows carrying MediaItem, each with MediaPart.
        It is what "Get Info" falls back to for an item whose Media the client has
        not already loaded.

        It NESTS, which plexsim did not do: a show's tree holds its seasons, each
        season its episodes, and each episode its MediaItem -- and an EPISODE's own
        tree is rooted at the SHOW. plexsim answered one flat row with
        `MediaItem: []`, which is both empty-list-shaped and missing the hierarchy
        the dialog reads.
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
                }
                # A real MediaPart carries its streams here as MediaStream, with
                # `type` rather than `streamType`. Get Info reads this tree for
                # an item whose Media the client has not already loaded.
                streams = [_tree_stream(stream) for stream in part.get("Stream", [])]
                if streams:
                    entry["MediaStream"] = streams
                parts.append(entry)
            # Measured: a MediaItem here carries ONLY its id -- the width,
            # height, codecs, container and bitrate plexsim added belong on the
            # Media of a normal detail response, not on this tree.
            media_items.append({"id": media.get("id"), "MediaPart": parts})
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
        }
        if media_items:
            # Never `MediaItem: []`: a parent row has no media at all, and an
            # empty list is a shape no real PMS sends.
            item["MediaItem"] = media_items
        children = [self._tree_item(child)
                    for child in self.catalog.children(row["ratingKey"])]
        if children:
            # Measured: the tree NESTS -- a show holds its seasons, each season
            # its episodes, each episode its MediaItem. plexsim answered one flat
            # row with an empty MediaItem list, so Get Info on a show or a season
            # had nothing under it.
            item["MetadataItem"] = children
        return self.tag_container(size=1, MetadataItem=[item])

    def _tree_item(self, row):
        """One node of the tree, with its own children beneath it."""
        inner = self.metadata_tree(row)["MediaContainer"]["MetadataItem"][0]
        return inner

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
                    smart=query.get("smart") == "1", uri=query.get("uri"))
                logger.info("COLLECTION create %s smart=%s (%d item(s))",
                            collection["title"], collection["smart"], len(rows))
                # Measured: a create pushes one frame -- type 18, state 0 "created",
                # titled -- which plexsim never sent, and the create RESPONSE carries
                # no librarySection fields where the ADD response does.
                self.notify_created({"ratingKey": collection["ratingKey"],
                                     "type": "collection", "title": collection["title"],
                                     "librarySectionID": collection["librarySectionID"]})
                created = {k: v for k, v in self.library._collection_row(collection).items()
                           if k not in ("librarySectionID", "librarySectionKey",
                                        "librarySectionTitle")}
                return self.bare_container(size=1, Metadata=[created])
            # A real PMS 404s the bare GET: collections are listed per section.
            return ("error", 404, "plexsim: not found")

        collection = self.library.collection(segments[2])
        if collection is None:
            return ("error", 404, "plexsim: no such collection")

        if len(segments) == 3:
            if method == "DELETE":
                # Measured order: every MEMBER is queued (state 3), then every member
                # is done (state 5) -- which is what makes another client refetch the
                # rows and drop the collection's tag from them -- and only then the
                # collection's own state 9, whose title is EMPTY. plexsim pushed the
                # state 9 alone. A smart collection tags nobody, so it has no members
                # to refetch.
                members = ([] if collection.get("smart")
                           else list(self.library.collection_children(collection)))
                self.library.delete_collection(segments[2])
                for member in members:
                    self.notify_timeline(member, self.TIMELINE_QUEUED, "queued")
                for member in members:
                    self.notify_timeline(member, self.TIMELINE_DONE, None)
                self.notify_deleted({"ratingKey": segments[2], "type": "collection",
                                     "librarySectionID": collection["librarySectionID"],
                                     "title": ""})
                return ("empty", 200)
            if method == "PUT":
                self.library.edit_collection(
                    collection, title=query.get("title.value") or query.get("title"),
                    summary=query.get("summary.value") or query.get("summary"))
                return ("empty", 200)
            section = self.catalog.section(collection["librarySectionID"]) or {}
            # Measured: this container carries `allowSync` TRUE, the identifier and
            # the media-tag pair, and its librarySectionID is a NUMBER -- plexsim
            # sent a string and omitted the other three, so a client could not
            # resolve a tag into an image URL from a collection page.
            return self.bare_container(
                size=1,
                allowSync=True,
                identifier="com.plexapp.plugins.library",
                mediaTagPrefix="/system/bundle/media/flags/",
                mediaTagVersion=1655122614,
                librarySectionID=int(collection["librarySectionID"])
                if str(collection["librarySectionID"]).isdigit()
                else collection["librarySectionID"],
                librarySectionTitle=section.get("title"),
                librarySectionUUID=section.get("uuid"),
                Metadata=[self.library._collection_row(collection)],
            )

        leaf = segments[3]
        if leaf in ("children", "items") and method == "GET":
            # Measured: `/items` answers the same members as `/children` -- plexsim
            # answered an empty container to it, so the Edit Collection dialog's
            # member list was blank.
            rows = [_listing_row(r) for r in self.library.collection_children(collection)]
            return self.bare_container(size=len(rows), Metadata=rows)
        # Measured: `/children/{ratingKey}` removes a member exactly as `/items/` does;
        # plexsim answered it with an empty 200 and removed nothing.
        if leaf in ("items", "children") and len(segments) == 5 and method == "DELETE":
            # Remove-from-collection, which the client builds as
            # `collection.url + "/items/" + ratingKey` with type DELETE and then
            # assigns the RESPONSE onto its own model. plexsim had no DELETE branch
            # at all, so it fell to an empty container: the tile disappeared
            # optimistically and came back on the next fetch. Measured: 200 with the
            # collection row, and it is idempotent.
            row = self.catalog.items.get(int(segments[4])) \
                if segments[4].isdigit() else None
            if row is not None:
                self.library.remove_collection_item(collection, row)
            return self.bare_container(
                size=1, Metadata=[self.library._collection_row(collection)])
        if leaf == "items" and len(segments) == 6 and segments[5] == "move":
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            if not self.library.move_collection_item(collection, segments[4],
                                                     query.get("after")):
                return ("error", 404, "plexsim: not in this collection")
            return ("empty", 200)
        if leaf == "items" and method == "PUT":
            rows = self._items_for_uri(query.get("uri", ""))
            self.library.add_collection_items(collection, rows)
            # Measured: a collection add answers the collection ROW -- with its new
            # childCount and year span -- and not the leafCountAdded/Requested pair a
            # PLAYLIST add answers. plexsim sent the playlist shape here, so the
            # collection page had nothing to update itself from.
            return self.bare_container(
                size=1, Metadata=[self.library._collection_row(collection)])
        if leaf == "composite":
            art = self.composite_for("collection", segments[2])
            if art:
                return ("image", self._at_width(art, query.get("width")))
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
                    smart=query.get("smart") == "1", uri=query.get("uri"))
                logger.info("PLAYLIST create %s smart=%s (%d item(s))",
                            playlist["title"], playlist["smart"], len(rows))
                self.notify_created({"ratingKey": playlist["ratingKey"],
                                     "librarySectionID": -1, "type": "playlist",
                                     "title": ""})
                # Measured: the CREATE response omits `content` the same way the
                # list does; only /playlists/{id} carries it.
                return self.bare_container(
                    size=1, Metadata=[self.library._playlist_row(playlist)])
            rows = self.library.playlists(query.get("playlistType"))
            if query.get("smart") == "0":
                rows = [r for r in rows if not r.get("smart")]
            rows, unsupported = self._sorted(rows, query.get("sort") or "")
            if unsupported:
                return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
            return self.paged_container(rows, query)

        playlist = self.library.playlist(segments[1])
        if playlist is None:
            return ("error", 404, "plexsim: no such playlist")

        if len(segments) == 2:
            if method == "DELETE":
                title = playlist.get("title") or ""
                self.library.delete_playlist(segments[1])
                # state 9 is what dispatches the client's own `deleted` action --
                # without it the row stays on screen in every other open client.
                self.notify_deleted({"ratingKey": segments[1], "librarySectionID": -1,
                                     "type": "playlist", "title": title})
                # Measured: a real PMS answers the playlist delete with 204,
                # not the 200 it uses for a collection delete.
                return ("empty", 204)
            if method == "PUT":
                # Measured: a PUT that names nothing it can write is a 400 -- an
                # unknown parameter, `smart=1`, or no parameters at all. plexsim
                # answered 200 to all three.
                writable = {"title", "summary", "titleSort", "title.value",
                            "summary.value", "titleSort.value", "type"}
                named = [k for k in query
                         if not k.startswith("X-Plex-") and k != "_pairs"]
                if not named or any(k not in writable for k in named):
                    return ("error", 400, "plexsim: bad request")
                title = query.get("title.value")
                if title is None:
                    title = query.get("title")
                summary = query.get("summary.value")
                if summary is None:
                    summary = query.get("summary")
                self.library.edit_playlist(playlist, title=title, summary=summary)
                return ("empty", 200)
            return self.bare_container(
                size=1,
                Metadata=[self.library._playlist_row(playlist, content=True)])

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
                if not rows and str(query.get("playQueueID") or "").isdigit():
                    queue = self.play_queues.get(int(query["playQueueID"])) or {}
                    rows = [self.catalog.items[e["ratingKey"]]
                            for e in queue.get("items") or []
                            if e["ratingKey"] in self.catalog.items]
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
                composite=summary.get("composite"),
                # Measured, and documented in findings.md before it was
                # implemented: the CONTAINER's duration is in SECONDS where the
                # playlist ROW's is in milliseconds -- the same number, two units,
                # one endpoint apart. Rounded to whole seconds, as the reference
                # rounds it (2 x 10023 ms answered 20000 on the row and 20 here).
                duration=(int(round(summary["duration"] / 1000.0))
                          if "duration" in summary else None),
                leafCount=summary["leafCount"],
                playlistType=summary["playlistType"],
                ratingKey=summary["ratingKey"],
                smart=summary["smart"],
                title=summary["title"],
            )
        if leaf == "composite":
            art = self.composite_for("playlist", segments[1])
            if art:
                return ("image", self._at_width(art, query.get("width")))
            return ("png", query)
        return self.bare_container(size=0)

    # -- play queue ------------------------------------------------------

    def play_queue_route(self, method, segments, query):
        """`/playQueues/{id}` -- Play Next, Add to Queue, move and delete.

        The client builds Play Next and Add to Queue from the same call it uses to
        create a queue, with `next=1` for Play Next; without the PUT, both menu
        entries are drawn (the provider advertises `playqueue`) and neither does
        anything.

        WHERE they insert is measured, and plexsim had both wrong. A plain add goes
        after the block of already-added items following the current one; a
        `next=1` add goes IMMEDIATELY after the current item, ahead of that block.
        plexsim appended to the very end of the queue and inserted at index 1
        regardless of where the player actually was.
        """
        if not segments[1].isdigit():
            return ("error", 404, "plexsim: no such play queue")
        queue_id = int(segments[1])
        with self.lock:
            queue = self.play_queues.get(queue_id)
        if queue is None:
            return ("error", 404, "plexsim: no such play queue")

        # Measured: the player's shuffle button PUTs here and a real PMS answers
        # 404 -- it has no such endpoint. The client reverts its optimistic
        # `playQueueShuffled` on a failure, so plexsim's 200 left the button stuck
        # "on" over a queue that was never shuffled.
        if len(segments) == 3 and segments[2] in ("shuffle", "unshuffle"):
            return ("error", 404, "plexsim: not found")

        if (len(segments) == 5 and segments[2] == "items"
                and segments[4] == "move" and method == "PUT"):
            return self.move_play_queue_item(queue_id, segments[3],
                                            query.get("after"), query)

        if len(segments) == 4 and segments[2] == "items" and method == "DELETE":
            with self.lock:
                items = queue["items"]
                index = next((i for i, e in enumerate(items)
                              if str(e["id"]) == str(segments[3])), None)
                if index is None:
                    # Measured: an unknown item id is a 404. plexsim derived the
                    # index from the id arithmetically, so an id the client cached
                    # before an insert deleted a DIFFERENT row.
                    return ("error", 404, "plexsim: no such play queue item")
                items.pop(index)
                queue["version"] += 1
            return self.play_queue_container(queue_id, query)

        if len(segments) == 2 and method == "PUT":
            if not query.get("uri") and not query.get("playlistID"):
                # Measured: a bare refresh PUT with no uri is a 400.
                return ("error", 400, "plexsim: bad request")
            rows = self._items_for_uri(query.get("uri", ""),
                                      continuous=query.get("continuous") == "1")
            with self.lock:
                items = queue["items"]
                new = self._queue_items(rows)
                current = next((i for i, e in enumerate(items)
                                if e["id"] == queue["selected"]), 0)
                if query.get("next") == "1":
                    at = current + 1
                else:
                    # Skip past the items already queued up behind the current one.
                    at = current + 1
                    while at < len(items) and items[at]["id"] > queue["selected"]:
                        at += 1
                items[at:at] = new
                queue["version"] += 1
                if new and query.get("next") != "1":
                    # Measured: this tracks PLAIN adds only -- a next=1 add leaves
                    # it where it was. The client disables the Up Next shuffle
                    # button once it is present.
                    queue["last_added"] = new[-1]["id"]
            logger.info("PLAYQUEUE %s %s %d item(s)", queue_id,
                        "next" if query.get("next") == "1" else "append", len(rows))
            return self.play_queue_container(queue_id, query)

        if len(segments) == 2 and method in ("POST", "DELETE"):
            # Measured: both are 404 on a real PMS. plexsim's DELETE really dropped
            # the queue, so a client that tried one lost the queue it was playing.
            return ("error", 404, "plexsim: not found")

        if len(segments) == 2:
            return self.play_queue_container(queue_id, query)
        return ("error", 404, "plexsim: not found")

    def drop_from_queues(self, rating_key):
        """Take a removed row out of every play queue, moving the selection with it.

        Measured on the reference: the queue shrinks, `playQueueTotalCount` drops and
        the VERSION steps -- and the selection moves to a row that still exists.
        plexsim left the removed row's item id as `playQueueSelectedItemID` and never
        moved the version, which per its own findings is the response the client
        throws away: the player would have been holding a selection that no longer
        existed on a queue it could not refresh.
        """
        with self.lock:
            for queue in self.play_queues.values():
                keeping = [e for e in queue["items"]
                           if int(e["ratingKey"]) != int(rating_key)]
                if len(keeping) == len(queue["items"]):
                    continue
                queue["items"] = keeping
                queue["version"] += 1
                if not any(e["id"] == queue["selected"] for e in keeping):
                    queue["selected"] = keeping[0]["id"] if keeping else None

    def move_play_queue_item(self, queue_id, item_id, after, query):
        """`PUT /playQueues/{id}/items/{itemID}/move?after=` -- drag in Up Next.

        Measured: `after=-1` moves the item to the FRONT and the version steps.
        plexsim did not route this at all -- the request fell through to its
        add-to-queue branch and answered 200 with the queue unchanged, so the
        handle moved and the order snapped back on the next read.
        """
        with self.lock:
            queue = self.play_queues[queue_id]
            items = queue["items"]
            index = next((i for i, e in enumerate(items)
                          if str(e["id"]) == str(item_id)), None)
            if index is None:
                return ("error", 404, "plexsim: no such play queue item")
            moving = items.pop(index)
            if after is None or str(after) in ("-1", ""):
                items.insert(0, moving)
            else:
                target = next((i for i, e in enumerate(items)
                               if str(e["id"]) == str(after)), None)
                if target is None:
                    items.insert(index, moving)
                    return ("error", 404, "plexsim: no such play queue item")
                items.insert(target + 1, moving)
            queue["version"] += 1
        return self.play_queue_container(queue_id, query)

    # -- server settings -------------------------------------------------

    def _identity_prefs(self):
        """The preference rows whose value IS this server's identity.

        `FriendlyName` is the one that matters: it is `hidden: false, group:
        "general"` and the client looks it up BY ID to draw Settings > General's
        server-name field and to set its own `friendlyName`. plexsim's list was
        missing it, so the field did not render and a write to it was refused as an
        unknown preference.
        """
        return {"FriendlyName": self.catalog.friendly,
                "MachineIdentifier": self.catalog.machine,
                "PlexOnlineMail": self._owner() if self._claimed() else ""}

    def server_prefs(self):
        """The setting list, with this server's identity and any written value."""
        identity = self._identity_prefs()
        out = []
        for setting in SERVER_PREFS:
            name = setting.get("id")
            if name in self.pref_overrides:
                setting = dict(setting, value=self.pref_overrides[name])
            elif name in identity:
                setting = dict(setting, value=identity[name])
            out.append(setting)
        return out

    def set_prefs(self, query):
        """`PUT /:/prefs?<id>=<value>` -- one or more settings at a time.

        Validated the way a real PMS validates it, all of it measured:
        * an unknown preference is a 400, message and all, and NOTHING in the
          request is applied -- plexsim applied the known ones beside it;
        * an `int` or `double` that does not parse (`abc`, `4.5` for an int) is a
          500 and an EMPTY one is ignored;
        * a `bool` is true for exactly `1` and `true`, and false for anything else,
          an empty value included;
        * the stored value keeps its TYPE, so the next read still says `40` and
          `false` rather than the strings `"40"` and `"0"` plexsim echoed back.
        """
        settings = {s.get("id"): s for s in SERVER_PREFS}
        writes = [(name, value) for name, value in query.items()
                  if not name.startswith("X-Plex-") and name != "_pairs"]
        for name, _value in writes:
            if name not in settings:
                return ("error", 400,
                        "cannot set preference value for unknown preference " + name)
        typed = []
        for name, value in writes:
            kind = settings[name].get("type")
            if kind in ("int", "double"):
                if value == "":
                    continue
                try:
                    typed.append((name, int(value) if kind == "int" else float(value)))
                except ValueError:
                    return ("error", 500, "plexsim: internal error")
            elif kind == "bool":
                typed.append((name, value in ("1", "true")))
            else:
                typed.append((name, value))
        for name, value in typed:
            self.pref_overrides[name] = value
            logger.info("PREF %s = %s", name, value)
        return ("empty", 200)

    def new_section_prefs(self, query):
        """`GET /library/sections/prefs?type=N&agent=ID` -- the Add Library form.

        The dialog asks for this before the library exists, and joins it with three
        other reads, so a refusal stops the whole dialog from validating. plexsim
        400'd every call. Measured: `type` is required and must be a number; the
        list depends on the type AND the agent; a legacy or unknown agent gives the
        type's base list and an unknown type gives `hidden` alone.
        """
        kind = query.get("type") or ""
        if not kind.lstrip("-").isdigit():
            return ("error", 400, "plexsim: bad request")
        rows = self.section_pref_rows(str(int(kind)), query.get("agent"))
        return self.bare_container(size=len(rows), Setting=rows)

    @staticmethod
    def section_pref_rows(type_id, agent, current=None):
        """The measured setting list for a library type and agent."""
        lists = SECTION_PREFS_NEW.get("lists") or {}
        known = SECTION_PREFS_NEW.get("settings") or {}
        ids = lists.get("%s|%s" % (type_id, agent)) or lists.get(type_id) or ["hidden"]
        current = current or {}
        return [dict(current.get(name) or known[name]) for name in ids
                if name in current or name in known]

    def browse(self, route, query):
        """`/services/browse` -- the Add Folder picker walking the filesystem.

        Measured: the bare path lists `/` and the server's mount points; a
        sub-path is its base64 in the URL and answers its directories, but ONLY
        with `includeFiles=1` -- without it, and for anything that is not base64,
        it is a 400 -- and a path that does not exist is an empty 200. plexsim
        404'd the lot, so Add Folder opened on nothing. plexsim owns no disk: its
        filesystem is the `/plexsim` tree its sections and parts are addressed by.
        """
        def entry(path):
            title = "/" if path == "/" else path.rstrip("/").rsplit("/", 1)[-1]
            return {"key": "/services/browse/" + base64.b64encode(path.encode()).decode(),
                    "title": title, "path": path}
        tree = {}
        for section in self.catalog.sections:
            path = "/plexsim/%s" % section["key"]
            tree.setdefault("/", set()).add("/plexsim")
            tree.setdefault("/plexsim", set()).add(path)
        if route.rstrip("/") == "/services/browse":
            rows = [entry("/"), entry("/plexsim")]
            return self.bare_container(size=len(rows), Path=rows)
        encoded = route[len("/services/browse/"):]
        try:
            path = base64.b64decode(encoded, validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return ("error", 400, "plexsim: bad request")
        if query.get("includeFiles") != "1" or not path.startswith("/"):
            return ("error", 400, "plexsim: bad request")
        children = sorted(tree.get(path.rstrip("/") or "/", ()),
                          key=lambda child: child.rsplit("/", 1)[-1].lower())
        rows = [entry(child) for child in children]
        return self.bare_container(size=len(rows), Path=rows)

    # `/library/tags?type=N`: the tag table for one tag type. Measured types; every
    # other number is an empty container, and no type or a non-number is a 400.
    TAG_TYPES = {"1": ("Genre", "genre"), "2": ("Collection", "collection"),
                 "4": ("Director", "director"), "5": ("Writer", "writer"),
                 "6": ("Role", "actor"), "7": ("Producer", "producer"),
                 "8": ("Country", "country")}

    def library_tags(self, query):
        """Every tag of one type the server knows, ordered by id.

        Measured: `Directory` rows of id, filter, tag and tagType, plus tagKey and
        thumb for a person. It is the TABLE, not what is currently on a row -- the
        reference still lists a collection tag no item carries -- so plexsim reads
        its own tag table as well as the rows. plexsim answered `size: 0` for every
        type.
        """
        kind = query.get("type") or ""
        if not kind.lstrip("-").isdigit():
            return ("error", 400, "plexsim: bad request")
        field = self.TAG_TYPES.get(str(int(kind)))
        if field is None:
            return self.tag_container(size=0)
        element, name = field
        found = {}
        for row in self.catalog.items.values():
            for tag in row.get(element) or []:
                if isinstance(tag, dict) and tag.get("id") is not None and tag.get("tag"):
                    found.setdefault(tag["id"], tag)
        for (table, _lowered), tag in list(self.library._tags.items()):
            if table == element and tag.get("id") is not None and tag.get("tag"):
                found.setdefault(tag["id"], tag)
        rows = []
        for tag_id in sorted(found, key=lambda value: int(value) if str(value).isdigit() else 0):
            tag = found[tag_id]
            entry = {"id": tag_id, "filter": tag.get("filter") or "%s=%s" % (name, tag_id),
                     "tag": tag["tag"], "tagType": int(kind)}
            for extra in ("tagKey", "thumb"):
                if tag.get(extra):
                    entry[extra] = tag[extra]
            rows.append(entry)
        return self.tag_container(size=len(rows), Directory=rows)

    def system_agents(self, query):
        """`/system/agents` -- the Agents page and the library dialogs' dropdown.

        The modern agents only. A real PMS lists 17, most of them legacy plug-ins
        it still ships; claiming those would be advertising identification paths
        plexsim does not have. Measured: `mediaType=N` lists exactly the agents that
        type offers (a movie library: Plex Movie, Personal Media, NFO Movie), a type
        no agent serves is `size: 0`, and a non-number is a 400.
        """
        wanted = query.get("mediaType")
        if not wanted:
            return self.bare_container(size=len(SYSTEM_AGENTS), Agent=SYSTEM_AGENTS)
        if not wanted.lstrip("-").isdigit():
            return ("error", 400, "plexsim: bad request")
        by_id = {agent["identifier"]: agent for agent in SYSTEM_AGENTS}
        agents = [by_id[name] for name in AGENT_MEDIA_TYPES.get(str(int(wanted)), [])
                  if name in by_id]
        return self.bare_container(size=len(agents), Agent=agents)

    # The scanner names a real PMS offers per library type, captured verbatim.
    # The element is `Scanner` with nothing but a `name`, and the container's
    # identifier is `com.plexapp.system` rather than the library's -- plexsim
    # answered `Directory` rows with a key and a title, which the Edit Library
    # dialog's scanner dropdown does not read.
    SCANNERS = {
        "movie": ["Plex Movie", "Plex Movie Scanner",
                  "Plex Video Files Scanner", "Plex Video Files"],
        "show": ["Plex TV Series", "Plex Series Scanner"],
    }

    # `/system/scanners/{typeID}`, measured per library TYPE. The music types answer
    # `size="3"` with two Scanner rows -- sent as measured.
    SCANNERS_BY_TYPE = {
        "1": (4, ["Plex Movie", "Plex Movie Scanner", "Plex Video Files Scanner",
                  "Plex Video Files"]),
        "2": (2, ["Plex TV Series", "Plex Series Scanner"]),
        "8": (3, ["Plex Music", "Plex Music Scanner"]),
        "9": (3, ["Plex Music", "Plex Music Scanner"]),
        "13": (1, ["Plex Photo Scanner"]),
    }

    def system_scanners(self, type_id):
        """`/system/scanners/{typeID}` -- what the Edit Library dialog lists.

        The number is the library TYPE (`ScannerCollection` builds the URL from
        `sectionTypeID`), not a section id: `/system/scanners/13` is the photo
        scanner on a server with no photo library. plexsim looked it up as a
        section and answered the movie list for anything it did not have; a real
        PMS answers 500 for a type with no scanner and for a non-number.
        """
        found = self.SCANNERS_BY_TYPE.get(str(type_id))
        if found is None:
            return ("error", 500, "plexsim: internal error")
        size, names = found
        return {"MediaContainer": {
            "size": size,
            "identifier": "com.plexapp.system",
            "noHistory": 0,
            "replaceParent": 0,
            "Scanner": [{"name": name} for name in names],
        }}

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
        if account_id not in (None, "1"):
            # Measured: an account the server does not have is a 404, not an empty
            # container -- which a client reads as "the account exists, with no
            # settings".
            return ("error", 404, "plexsim: no such account")
        return self.bare_container(
            size=1, identifier="com.plexapp.system.accounts", Account=[account])

    def devices(self):
        """`/devices` -- every client the server has seen, as the reference lists."""
        rows = self.sessions.devices()
        return self.bare_container(
            size=len(rows), identifier="com.plexapp.system.devices", Device=rows)

    def statistics(self, which, query=None):
        """`/statistics/{bandwidth,media,resources}` -- the Dashboard graphs.

        The Device and Account rows are real; the per-interval series is empty
        because plexsim keeps no history. An invented series would draw a
        plausible graph of traffic that never happened.
        """
        if which not in ("bandwidth", "media", "resources"):
            return ("error", 404, "plexsim: not found")
        if which == "resources":
            return self.resource_statistics(query or {})

        query = query or {}
        # Measured: with NO `timespan` a real PMS answers every rollup at once.
        timespan = query.get("timespan") or None
        since = query.get("at>") or query.get("at>=")
        try:
            since = int(since) if since else None
        except ValueError:
            since = None
        if which == "bandwidth":
            rows = self.sessions.bandwidth(timespan, since)
            element = "StatisticsBandwidth"
        else:
            rows = self.sessions.media_statistics(timespan)
            element = "StatisticsMedia"
        if not rows:
            # Measured on a real server with no rows for the window: nothing but
            # `size`. The Device and Account blocks only appear with a series.
            return self.bare_container(size=0)
        account = {"id": 1, "key": "/accounts/1", "name": self.owner_username,
                   "defaultAudioLanguage": "en", "autoSelectAudio": True,
                   "defaultSubtitleLanguage": "en", "subtitleMode": 2, "thumb": ""}
        devices = self.sessions.devices()
        return self.bare_container(size=len(rows) + len(devices) + 1,
                                   Device=devices, Account=[account],
                                   **{element: rows})

    # Scheduled tasks. A real PMS lists 23; plexsim lists the ones it can
    # actually perform, because the page's Run button is a promise. The root
    # element is `ButlerTasks`, not MediaContainer -- one of the few endpoints
    # on the whole surface that is not a MediaContainer, which is exactly the
    # kind of thing that cannot be guessed.
    # The Scheduled Tasks page, task for task off the reference: 23 of them, and
    # only BackupDatabase carries a `title` and a `description` -- the client
    # humanises the rest from the name itself. plexsim listed two with invented
    # titles, so the page showed two tasks that do not exist on a real server and
    # hid twenty-one that do.
    BUTLER_TASKS = [
        {"name": "AutomaticUpdates", "interval": 1, "scheduleRandomized": False,
         "enabled": False},
        {"name": "BackupDatabase", "interval": 3, "scheduleRandomized": False,
         "enabled": True, "title": "Backup Database",
         "description": "Create a backup copy of the server's database in the "
                        "configured backup directory"},
        {"name": "ButlerTaskCleanSupplementalLogFiles", "interval": 1,
         "scheduleRandomized": False, "enabled": False},
        {"name": "ButlerTaskGenerateAdMarkers", "interval": 1,
         "scheduleRandomized": False, "enabled": True},
        {"name": "ButlerTaskGenerateCreditsMarkers", "interval": 1,
         "scheduleRandomized": True, "enabled": True},
        {"name": "ButlerTaskGenerateIntroMarkers", "interval": 1,
         "scheduleRandomized": False, "enabled": True},
        {"name": "ButlerTaskGenerateVoiceActivity", "interval": 1,
         "scheduleRandomized": True, "enabled": False},
        {"name": "CleanOldBundles", "interval": 7, "scheduleRandomized": False,
         "enabled": True},
        {"name": "CleanOldCacheFiles", "interval": 7, "scheduleRandomized": False,
         "enabled": True},
        {"name": "DeepMediaAnalysis", "interval": 1, "scheduleRandomized": False,
         "enabled": True},
        {"name": "GarbageCollectBlobs", "interval": 7, "scheduleRandomized": False,
         "enabled": True},
        {"name": "GarbageCollectLibraryMedia", "interval": 1,
         "scheduleRandomized": False, "enabled": True},
        {"name": "GenerateChapterThumbs", "interval": 1, "scheduleRandomized": False,
         "enabled": True},
        {"name": "GenerateMediaIndexFiles", "interval": 1,
         "scheduleRandomized": False, "enabled": False},
        {"name": "LoudnessAnalysis", "interval": 1, "scheduleRandomized": False,
         "enabled": True},
        {"name": "MusicAnalysis", "interval": 1, "scheduleRandomized": False,
         "enabled": True},
        {"name": "OptimizeDatabase", "interval": 7, "scheduleRandomized": False,
         "enabled": True},
        {"name": "ProcessAssets", "interval": 1, "scheduleRandomized": False,
         "enabled": True},
        {"name": "RefreshEpgGuides", "interval": 1, "scheduleRandomized": True,
         "enabled": True},
        {"name": "RefreshLibraries", "interval": 1, "scheduleRandomized": False,
         "enabled": False},
        {"name": "RefreshLocalMedia", "interval": 3, "scheduleRandomized": False,
         "enabled": True},
        {"name": "RefreshPeriodicMetadata", "interval": 1, "scheduleRandomized": True,
         "enabled": True},
        {"name": "UpgradeMediaAnalysis", "interval": 1, "scheduleRandomized": False,
         "enabled": True},
    ]

    def resource_statistics(self, query):
        """`/statistics/resources` -- the Dashboard's CPU and memory graph.

        plexsim used to 404 this, reasoning that a refusal reads as "unsupported"
        and hides the graph. The measurement says otherwise: BOTH reference
        servers answer 200 with a `StatisticsResources` series, and only
        `timespan=6` has samples -- every coarser bucket is an empty 200.

        The samples are taken when the endpoint is ASKED, which is the only time
        they are wanted: the Dashboard polls while it is open, so the graph fills
        in the way it does on a real server, and plexsim runs no sampler thread
        for a page nobody is looking at. Two of the four numbers are real -- host
        CPU from the load average over the core count, process CPU from rusage
        over wall time -- and the two memory ones are honestly 0.0, because the
        stdlib cannot see host memory and inventing a percentage would draw a
        plausible graph of nothing.
        """
        if not query.get("timespan"):
            # Measured: no timespan is an empty 200, not an error.
            return self.bare_container(size=0)
        timespan = str(query["timespan"])
        if timespan != "6":
            # Measured on both servers: only the finest bucket carries samples.
            return self.bare_container(size=0)
        now = int(time.time())
        with self.lock:
            previous = self._resource_sample
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu_seconds = usage.ru_utime + usage.ru_stime
            process = 0.0
            if previous and now > previous[0]:
                process = max(0.0, (cpu_seconds - previous[1]) / (now - previous[0]) * 100)
            self._resource_sample = (now, cpu_seconds)
            try:
                host = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
            except OSError:
                host = 0.0
            if not self._resource_series or self._resource_series[-1]["at"] < now:
                self._resource_series.append({
                    "timespan": 6,
                    "at": now,
                    "hostCpuUtilization": round(host, 3),
                    "processCpuUtilization": round(process, 3),
                    "hostMemoryUtilization": 0.0,
                    "processMemoryUtilization": 0.0,
                })
                # The reference kept about two and a half minutes at this bucket.
                del self._resource_series[:-180]
            series = list(self._resource_series)
        since = query.get("at>") or query.get("at>=")
        if str(since).isdigit():
            series = [s for s in series if s["at"] > int(since)]
        return self.bare_container(size=len(series), StatisticsResources=series)

    def butler(self, method, segments):
        """`/butler` and `/butler/{task}` -- the Scheduled Tasks page's Run button."""
        if len(segments) == 1:
            # Measured: the ButlerTasks root carries NO size attribute, unlike
            # every MediaContainer on the surface.
            return {"ButlerTasks": {"ButlerTask": self.BUTLER_TASKS}}
        name = segments[1]
        if name not in {task["name"] for task in self.BUTLER_TASKS}:
            return ("error", 404, "plexsim: no such task")
        if method == "DELETE":
            # Cancelling a queued task.
            return ("empty", 200)
        if method != "POST":
            # Measured: a PUT to a task a real PMS HAS is a 404, not the 405
            # plexsim answered -- only POST (run now) and DELETE (cancel) exist.
            return ("error", 404, "plexsim: not found")
        if name == "RefreshLibraries":
            for section in self.catalog.sections:
                for row in self.catalog.section_items(section["key"]):
                    self.library.finish_activity(
                        self.library.add_activity(row.get("title") or "", progress=-1))
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
        # Measured: this page filters and sorts, and plexsim ignored all of it --
        # so Top Users' "View Play History" link (`accountID=&viewedAt=`) and the
        # per-item history link (`metadataItemID=`) both showed the whole server's
        # history in insertion order, and the Played column's sort did nothing.
        #
        # Note the operators: `viewedAt>=` here is a plain `>=`, NOT the library
        # grammar's `>>`. Two query languages on one server.
        for name, field in (("accountID", "accountID"),
                            ("deviceID", "deviceID"),
                            ("librarySectionID", "librarySectionID"),
                            ("metadataItemID", "ratingKey")):
            if query.get(name):
                rows = [r for r in rows
                        if str(r.get(field)) == str(query[name])]
        for key, test in (("viewedAt>", lambda a, b: a > b),
                          ("viewedAt>=", lambda a, b: a >= b),
                          ("viewedAt<", lambda a, b: a < b),
                          ("viewedAt<=", lambda a, b: a <= b)):
            if str(query.get(key) or "").isdigit():
                moment = int(query[key])
                rows = [r for r in rows if test(int(r.get("viewedAt") or 0), moment)]
        # Default order is viewedAt ASCENDING, and an unknown sort key is a 400 --
        # not the 500 a library listing answers, which is its own small surprise.
        spec = query.get("sort") or "viewedAt"
        for field, descending, _seed in reversed(filters_module.sort_terms(spec)):
            if field not in ("viewedAt", "historyKey", "ratingKey", "accountID",
                             "deviceID", "title"):
                return ("error", 400, "plexsim: bad request")
            rows = sorted(rows, key=lambda r: (r.get(field) is None, r.get(field) or 0),
                          reverse=descending)
        page, paged, start, total = self.page_window(rows, query)
        fields = {"size": len(page), "Metadata": page}
        if paged:
            fields["totalSize"] = total
            fields["offset"] = start
        return self.bare_container(**fields)

    def delete_history(self, key):
        """`DELETE /status/sessions/history/{historyKey}` -- one row off the page.

        plexsim routed the history path on the path alone, so a DELETE was answered
        with the LISTING, and a numeric key fell through to the live-sessions
        handler and answered `{size: 0}` -- which is exactly the "it worked" signal
        for a delete that did nothing. Measured: 200 with an empty container when
        the row goes, 404 when there is no such row.
        """
        if not self.library.delete_history(key):
            return ("error", 404, "plexsim: no such history row")
        logger.info("HISTORY delete %s", key)
        return self.bare_container(size=0)

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
        # Measured: a real PMS validates this form and answers a PLAIN-TEXT 400
        # naming what is wrong -- with no Content-Type header at all. plexsim
        # accepted every malformed form with a 201 and created a library with
        # `type="bogus"` or titled "New Library", which then sat in the sidebar.
        # A nonexistent location and a duplicate name ARE both accepted (201).
        if "location" not in query or not query.get("location"):
            return ("error", 400, "'location' is missing or invalid. ")
        if query.get("type") not in ("movie", "show", "photo", "artist"):
            return ("error", 400,
                    "'type' is missing or invalid (must be one of 'movie', 'show', "
                    "'photo' or 'artist') 'agent' is invalid for the selected "
                    "library type ")
        if not query.get("agent") or query["agent"] not in KNOWN_AGENTS:
            return ("error", 400,
                    "'agent' is missing or invalid new scanner needs to be paired "
                    "with new agent ")
        if not query.get("scanner") or query["scanner"] not in KNOWN_SCANNERS:
            return ("error", 400, "new scanner needs to be paired with new agent ")
        if not query.get("name"):
            return ("error", 400, "'name' is missing or invalid ")
        title = query.get("name") or query.get("title") or "New Library"
        section_type = query["type"]
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

    # TMDB serves fixed-width variants, and the row stores one of them. Asking
    # upstream for the variant nearest the size the client wanted is the closest
    # plexsim can get to a real PMS's resize, having no imaging library: the
    # served image is still a fixed width rather than exactly the one requested,
    # which is a deliberate difference.
    TMDB_WIDTHS = (92, 154, 185, 342, 500, 780, 1280)

    def _at_width(self, url, width):
        if not url or "image.tmdb.org" not in url or not width:
            return url
        try:
            wanted = int(width)
        except (TypeError, ValueError):
            return url
        variant = next((w for w in self.TMDB_WIDTHS if w >= wanted), None)
        target = "w%d" % variant if variant else "original"
        return re.sub(r"/t/p/(w\d+|original)/", "/t/p/%s/" % target, url)

    def composite_for(self, kind, key):
        """The poster behind a collection's, playlist's or section's tile.

        A real PMS builds a MOSAIC of its members' posters, 400x600 (measured).
        plexsim has no imaging library and will not fabricate one, so it serves
        one member's own poster -- which is honestly one of the images that
        mosaic would be made of, and is what the tile is for. A flat generated
        tile was the alternative, and it is what the client showed before.
        """
        rows = []
        if kind == "collection":
            collection = self.library.collection(key)
            if collection:
                rows = self.library.collection_children(collection)
        elif kind == "playlist":
            playlist = self.library.playlist(key)
            if playlist:
                rows = [row for row, _ in self.library.playlist_items(playlist)]
        else:
            rows = self.catalog.section_items(key)
        for row in rows:
            art = row.get("_poster_url") or row.get("_art_url")
            if art:
                return art
        return None

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
        """part id -> (restricted debrid link, container, file name, the Part).

        The file name is for the Download entry's `Content-Disposition`: a real
        PMS names the saved file after the media file, not after the URL's last
        segment (measured: `filename="Firefly (2002) - s01e01 - 1080p.mkv"`).
        """
        for row in self.catalog.items.values():
            for media in row.get("Media", []):
                for part in media.get("Part", []):
                    if str(part.get("id")) == str(part_id):
                        name = str(part.get("file") or "").rsplit("/", 1)[-1]
                        # The part itself comes back too, for the ETag and
                        # Last-Modified a real PMS puts on every byte response.
                        return (row.get("_debrid"), part.get("container"), name,
                                dict(part, addedAt=row.get("addedAt")))
        return None, None, None, None


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
        return accept_wants_xml(self.headers.get("Accept"))

    def _count_bytes(self, count):
        """Every response counts towards the bandwidth statistics.

        Measured: a real PMS's `StatisticsBandwidth` rows include API replies,
        not only media -- a 4 KB `/status/sessions` answer showed up as its own
        per-second row. plexsim serves every byte it reports, so these are real
        numbers rather than a drawn graph.
        """
        try:
            sim = self.server.sim
            identifier = (self.headers.get("X-Plex-Client-Identifier")
                          or urllib.parse.parse_qs(
                              urllib.parse.urlparse(self.path).query
                          ).get("X-Plex-Client-Identifier", [""])[0])
            address = self.client_address[0] if self.client_address else None
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            def plex(name):
                return self.headers.get(name) or params.get(name, [""])[0]
            sim.sessions.record_bytes(identifier, sessions_module._private(address), count,
                                      name=plex("X-Plex-Device-Name"),
                                      platform=plex("X-Plex-Platform"))
        except Exception:
            pass

    def _cors_headers(self):
        """The CORS headers a real PMS sends, measured either side of an Origin.

        With an Origin it ECHOES that origin, and adds `Access-Control-Expose-
        Headers: Location, Date` and `Vary: Origin, X-Plex-Token`. With no Origin
        it sends no CORS headers at all. plexsim answered `*` unconditionally,
        which is not interchangeable: a wildcard is invalid on a credentialed
        request, so a client sending cookies or an Authorization header has its
        response rejected by the browser rather than by the server.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return []
        return [("Access-Control-Allow-Origin", origin),
                ("Access-Control-Expose-Headers", "Location, Date"),
                ("Vary", "Origin, X-Plex-Token")]

    def _send(self, status, content_type, payload, extra_headers=()):
        self.send_response(status)
        names = {name.lower() for name, _ in extra_headers}
        # Measured across eight endpoints on both reference servers: a real PMS
        # sends `Cache-Control: no-cache` on EVERYTHING. plexsim sent none at all,
        # except on artwork where it sent a day of caching that the reference does
        # not -- so a re-match or a Change Artwork stayed invisible to the browser.
        if "cache-control" not in names:
            self.send_header("Cache-Control", "no-cache")
        payload = self._maybe_gzip(content_type, payload, names)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Plex-Protocol", "1.0")
        for key, value in self._cors_headers():
            self.send_header(key, value)
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self._count_bytes(len(payload))

    # Compressible types. Measured: a real PMS gzips its XML and JSON (3.4x on a
    # five-row grid) and names both sizes in two Plex-specific headers, which
    # plexsim never sent -- so every container went out uncompressed.
    GZIP_TYPES = ("text/xml", "application/json", "text/html", "text/plain")

    def _maybe_gzip(self, content_type, payload, names):
        if "content-encoding" in names or len(payload) < 860:
            # The reference leaves very small bodies alone; 860 is the smallest
            # compressed answer measured off it.
            return payload
        accepted = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in accepted:
            return payload
        if not any(content_type.startswith(kind) for kind in self.GZIP_TYPES):
            return payload
        import gzip as gzip_module
        squeezed = gzip_module.compress(payload, 6)
        if len(squeezed) >= len(payload):
            return payload
        self.send_header("Content-Encoding", "gzip")
        self.send_header("X-Plex-Content-Original-Length", str(len(payload)))
        self.send_header("X-Plex-Content-Compressed-Length", str(len(squeezed)))
        return squeezed

    def _send_container(self, container):
        # `_xml` names the element a container's rows render as where the rows
        # cannot name it themselves (an artwork list is <Photo>, a folder list
        # <Directory>). It is internal: it must never reach the JSON body.
        body_dict = next(iter(container.values()))
        elements = body_dict.pop("_xml", None) if isinstance(body_dict, dict) else None
        if self._wants_xml():
            self._send(200, "text/xml;charset=utf-8", to_xml(container, elements))
        else:
            self._send(200, "application/json", json_body(container))

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

    # What a real PMS allows on a preflight, measured. It ECHOES the requested
    # header list rather than publishing a fixed one, which is the only way to
    # be sure of not blocking a header a client legitimately sends: plexsim's
    # fixed list already omitted `X-Plex-Session-Id`, `X-Plex-Language`,
    # `X-Plex-Features` and half a dozen others the web client uses, and a
    # preflight that omits one makes the browser refuse the request with no
    # server-side trace at all. Plex Web itself escapes this by passing identity
    # in the QUERY STRING, which is why it never showed up.
    CORS_FALLBACK_HEADERS = ("x-plex-token,x-plex-client-identifier,x-plex-product,"
                             "x-plex-version,x-plex-platform,x-plex-device,"
                             "x-plex-device-name,x-plex-session-id,"
                             "x-plex-session-identifier,x-plex-language,accept,"
                             "content-type,range")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("X-Plex-Protocol", "1.0")
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin, X-Plex-Token")
        self.send_header("Access-Control-Allow-Methods",
                         "POST, GET, OPTIONS, DELETE, PUT, HEAD")
        self.send_header("Access-Control-Allow-Headers",
                         self.headers.get("Access-Control-Request-Headers")
                         or self.CORS_FALLBACK_HEADERS)
        self.send_header("Access-Control-Max-Age", "1209600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # The only two paths a real PMS serves WITHOUT a token: `/identity`, so a client
    # can discover the server at all, and the web client itself. Measured on both
    # reference servers -- everything else, artwork and media bytes included, is a
    # 401 with no token AND with a bad one; there is no distinction and no
    # WWW-Authenticate header.
    OPEN_PATHS = ("/identity", "/web")

    def _authorised(self, path):
        """Whether this request carries the server's token, if one is required."""
        wanted = self.server.sim.token
        if not wanted:
            return True
        route = urllib.parse.urlparse(path)
        if route.path == "/identity" or route.path.startswith("/web"):
            return True
        if route.path == "/security/token":
            # Measured: this one is NOT a 401 -- it answers its own 400 with no
            # parameters and 403 with a token that is not good enough, which is how
            # a client tells "you did not ask properly" from "you may not have one".
            return True
        given = self.headers.get("X-Plex-Token") or urllib.parse.parse_qs(
            route.query).get("X-Plex-Token", [""])[0]
        return given == wanted

    def _dispatch(self, method):
        sim = self.server.sim
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        target = (self.requestline.split(" ") + ["", ""])[1]
        if target.startswith("//"):
            # Measured: a leading empty segment is a 404 like any other. The stdlib's
            # request parser collapses a leading `//` to `/` before this handler ever
            # sees `self.path` (an open-redirect guard), so it is read off the request
            # line -- `//identity` otherwise answered the identity document.
            self._send(404, "text/plain", b"plexsim: not found")
            return

        if not self._authorised(self.path):
            # Byte-for-byte the reference's refusal: the HTML shell, text/html,
            # no-cache, and `Connection: close`.
            logger.warning("401 %s %s -- no valid token", method, self.path.split("?")[0])
            self._send(401, "text/html",
                       b"<html><head><title>Unauthorized</title></head><body>"
                       b"<h1>401 Unauthorized</h1></body></html>",
                       extra_headers=[("Connection", "close")])
            return

        try:
            result = sim.handle(method, self.path, body, self.headers,
                                self.client_address[0] if self.client_address else None)
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
                self._send(200, "image/png", payload,
                           [("Cache-Control", "max-age=259200"
                             if self.path.startswith("/photo/:/transcode")
                             else "no-cache"),
                            ("Accept-Ranges", "bytes")])
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
            if kind == "text":
                # A body that IS the answer: the artwork write returns the new
                # artwork path, which the client assigns straight onto the row.
                self._send(result[1], "text/html", str(result[2]).encode())
                return
            if kind == "image-key":
                # `/library/metadata/{id}/{poster,art}` on a GET is the image the
                # row currently points at.
                row, which = result[1], result[2]
                target = row.get("thumb" if which == "poster" else "art")
                resolved = self.server.sim.resolve_artwork(target or "")
                if resolved:
                    self._serve_image(resolved)
                    return
                payload = _png(342, 513, (32, 34, 38))
                self._send(200, "image/png", payload,
                           [("Cache-Control", "no-cache"), ("Accept-Ranges", "bytes")])
                return
            if kind == "created":
                # POST /library/sections answers 201 with the created Directory.
                payload = json_body(result[1])
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
        # Measured: a real PMS sends `no-cache` on a VERSIONED direct asset and
        # `max-age=259200` only on transcoder output, plus `X-Plex-Local-Asset: 1`
        # and `Accept-Ranges: bytes`. plexsim cached the direct asset for a day, so
        # a Change Artwork or a re-match stayed invisible to the browser even though
        # the version in the URL had changed.
        transcoded = self.path.startswith("/photo/:/transcode")
        headers = [("Cache-Control", "max-age=259200" if transcoded else "no-cache"),
                   ("Accept-Ranges", "bytes")]
        if not transcoded:
            headers.append(("X-Plex-Local-Asset", "1"))
        self._send(200, content_type, payload, headers)

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
        # Measured: the reference's 101 carries the protocol version, the CORS pair
        # and `Cache-Control` alongside the handshake. plexsim sent the three
        # handshake headers alone.
        upgrade = ["HTTP/1.1 101 Switching Protocols",
                   "X-Plex-Protocol: 1.0",
                   "Upgrade: websocket",
                   "Connection: Upgrade",
                   "Sec-WebSocket-Accept: %s" % accept,
                   "Cache-Control: no-cache"]
        for name, value in self._cors_headers():
            upgrade.append("%s: %s" % (name, value))
        self.wfile.write(("\r\n".join(upgrade) + "\r\n\r\n").encode())
        self.wfile.flush()
        filters = [f for f in (urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).get("filters", [""])[0]
        ).split(",") if f]
        # Measured: `filters=` with an EMPTY value is not "no filter" -- it delivers
        # everything INCLUDING the log stream, which a socket with no `filters` at all
        # does not get.
        everything = not filters and "filters" in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True)
        logger.info("websocket event channel opened filters=%s", filters or "all")

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

        sim.add_socket(sink, filters)
        if "log" in filters or everything:
            # The Console's log pane. An earlier finding recorded that this channel
            # carries no log frames on a real server either -- which was a
            # measurement taken WITHOUT the `filters=log` the client actually sends.
            # With it, the reference pushes log lines continuously (386 in 14 s).
            sim.log_sink(sink)
        # Keep it alive with server-side pings. A silent socket is eventually
        # reaped by proxies, and Cloudflare sits in front of this one. Measured: the
        # reference pings every 10 s with a ONE-byte payload, not every 20 empty.
        deadline = time.time() + 3600
        try:
            while time.time() < deadline:
                # A client CLOSE has to be answered, or the browser sits in CLOSING
                # until its own timeout and the sink is never reaped. Poll for it
                # between pings rather than blocking on the socket.
                if self._client_went_away():
                    break
                with lock:
                    self.wfile.write(struct.pack("!BB", 0x89, 0x01) + b"?")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("websocket event channel closed by client")
        finally:
            sim.drop_socket(sink)
            sim.drop_log_sink(sink)

    def _client_went_away(self, seconds=10):
        """Wait out one ping interval, answering a CLOSE frame if one arrives.

        plexsim never read the socket at all, so a browser's `ws.close()` hung in
        CLOSING until its own timeout, plexsim never learnt the peer had gone, and
        the sink stayed registered with its thread alive to the one-hour deadline --
        never-reaped sinks accumulating for the life of the process.
        """
        import select
        ready, _, _ = select.select([self.connection], [], [], seconds)
        if not ready:
            return False
        try:
            head = self.connection.recv(2)
        except OSError:
            return True
        if not head:
            return True
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        masked = bool(head[1] & 0x80)
        # Drain the rest of the frame so the next read starts on a boundary.
        try:
            self.connection.recv(length + (4 if masked else 0))
        except OSError:
            return True
        if opcode == 0x8:
            # Echo the close with status 1000, which is what the reference sends.
            try:
                self.connection.sendall(struct.pack("!BBH", 0x88, 0x02, 1000))
            except OSError:
                pass
            logger.info("websocket closed by client")
            return True
        return False

    def _serve_eventsource(self):
        """Server-Sent Events channel the web app subscribes to on connect.

        Held open with periodic comment frames: closing it immediately makes the
        client reconnect in a tight loop. It carries the same events as the
        websocket, in the SSE shape measured on the reference -- `event: <type>`
        and a `data:` line holding ONE row under its element name, with no
        NotificationContainer and no size around it.
        """
        self.send_response(200)
        self.send_header("X-Plex-Protocol", "1.0")
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # This handler bypassed _cors_headers() and hardcoded `*` -- the exact bug
        # this project already found and fixed for _send(). A wildcard is invalid on
        # a credentialed request, and the reference sends NO CORS headers at all
        # without an Origin and the echoed origin with one.
        for name, value in self._cors_headers():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        sim = self.server.sim
        lock = threading.Lock()
        filters = [f for f in (urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).get("filters", [""])[0]
        ).split(",") if f]
        # Measured: `filters=` with an EMPTY value is not "no filter" -- it delivers
        # everything INCLUDING the log stream, which a socket with no `filters` at all
        # does not get.
        everything = not filters and "filters" in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True)

        def sink(kind, element, rows):
            frames = "".join(
                "event: %s\ndata: %s\n\n" % (kind, json.dumps({element: row}))
                for row in rows)
            with lock:
                self.wfile.write(frames.encode())
                self.wfile.flush()

        sim.add_socket(sink, filters)
        if "log" in filters or everything:
            sim.log_sink(sink)
        deadline = time.time() + 300
        try:
            while time.time() < deadline:
                # Measured: the FIRST ping arrives ten seconds in, not on connect.
                time.sleep(10)
                with lock:
                    # Measured: a real named SSE event every 10 s, not a comment
                    # every 15 -- a comment is invisible to an EventSource listener.
                    self.wfile.write(b"event: ping\ndata: {}\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sim.drop_socket(sink)
            sim.drop_log_sink(sink)

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
        link, container, part_name, part_row = sim.resolve_part(part_id)
        if not link:
            self._send(404, "text/plain", b"plexsim: no such part")
            return

        # A terminated session's next part read is a 503 on a real PMS -- the
        # terminate call itself only arms it, and this is where the client
        # actually stops getting bytes.
        session_id = query.get("X-Plex-Session-Identifier") or ""
        if session_id and sim.sessions.is_terminated(session_id):
            logger.info("PART %s refused: session %s terminated", part_id, session_id)
            self._send(503, "text/plain", b"plexsim: session terminated")
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
                    download=filename if download else None, part=part_row)

    def _proxy(self, upstream_url, container, part_id, method, download=None,
               part=None):
        import hashlib
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
            # Measured: every byte response from a real PMS carries BOTH validators,
            # on the 200 and on every 206 -- its `ETag` is the part's own file hash
            # and `Last-Modified` is the file's mtime. plexsim sent neither, so a
            # client had nothing to revalidate against. plexsim owns no file to hash,
            # so the ETag is a digest of what identifies these bytes (part id,
            # version, size) -- an opaque validator is exactly what the header is.
            if part is not None:
                stamp = part.get("addedAt") or part.get("updatedAt") or 0
                seed = "%s/%s/%s" % (part_id, stamp, part.get("size") or 0)
                self.send_header("ETag", '"%s"' % hashlib.sha1(seed.encode()).hexdigest())
                if stamp:
                    self.send_header(
                        "Last-Modified",
                        time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(stamp)))
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
            for key, value in self._cors_headers():
                self.send_header(key, value)
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
            finally:
                if content_length:
                    self._count_bytes(int(content_length))


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
    parser.add_argument(
        "--token", default=os.environ.get("PLEXSIM_TOKEN"),
        help="require this X-Plex-Token on every request except /identity and "
             "/web/* -- which is what a real PMS does. Default OFF, because the "
             "test suites and probes talk to plexsim without one; a real server "
             "always has a token, so leaving it off is a deliberate deviation.")
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
        token=args.token,
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
            # Push it to any channel that opened with `filters=log` -- the Console's
            # log pane, which is the only subscriber a real PMS feeds these to.
            sim.push_log(record)

    logger.addHandler(_Capture())

    server = Server((args.host, args.port), sim, args.verbose)
    logger.info("claim state: %s", "CLAIMED by %s" % claim.owner if claim.claimed else "unclaimed")
    # Said out loud either way: a real PMS 401s everything but /identity and /web/*
    # without its token, and a plexsim with no token is readable by any peer that
    # can reach the port -- library, preferences, sessions and media bytes alike.
    logger.info("token gate: %s", "ENFORCED" if args.token else
                "OFF -- every peer that can reach this port can read the library")
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
