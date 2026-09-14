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
import hashlib
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

# The two library settings that change ROWS rather than only the setting list.
# Both are ALSO item settings, where `-1` means "inherit this library's value"
# -- measured on the copy in cycle 15, in all four combinations.
SHOW_ROW_PREFS = ("flattenSeasons", "episodeSort")

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
# Measured: `index` filters an episode or season listing (`type=4&index=2` is every
# episode 2, `season.index=2` every episode of season 2) although no captured Meta
# lists it as a field -- so plexsim ignored it and answered every row.
# Cycle 14: a MOVIE row has an index too, and it is 1 on every one of them --
# `index=1` and `index>>=0` answer all 5 on kids and all 3887 on zen, while `index=0`,
# `index=2` and `index>>=1` answer none. plexsim's movie rows carry no index at all,
# so all three answered zero rows where a real PMS answers the whole library.
FILTER_FIELD_TYPES.setdefault("index", "integer")
# And `id` filters every listing although no Meta advertises it: `/query/parse` echoes
# it as a Rule with an empty `fieldTitle`. See filters.py for the comparison rules.
FILTER_FIELD_TYPES.setdefault("id", "id")
# And the three leaf counts filter a show listing on both servers although no Meta
# advertises them: `viewedOrInProgressLeafCount=0` is 499 of zen's 507 shows and `=1`
# is 4, `viewedLeafCount>>0` is the 8 with a watched episode, `unviewedLeafCount>>0`
# is 505. `leafCount` and `childCount` are NOT fields -- both answer the whole library
# -- so they stay unknown, which is what plexsim already did with all five.
for _bare in ("viewedLeafCount", "viewedOrInProgressLeafCount", "unviewedLeafCount"):
    FILTER_FIELD_TYPES.setdefault(_bare, "integer")
# Likewise `titleSort`, `summary` and `tagline` filter on a real PMS (`titleSort=sintel`
# is one row, `summary=e` none) without appearing in a Meta. See filters.py for how.
for _bare in ("titleSort", "summary", "tagline"):
    FILTER_FIELD_TYPES.setdefault(_bare, "string")
# Cycle 14, measured field by field on kids (1.43.4) and zen (1.42.2), which agree:
# nine MORE columns filter without appearing in any Meta. Each is a real constraint
# and not a tolerated no-op -- `viewOffset>>=0` answers 0 rows on both where an
# unknown field answers every row, and on zen `updatedAt>>=1780000000` is 3861 of
# 3887, `mediaCount>>=1` is 214, `originalTitle>>=M` is 393 and `createdAt>>=1786000000`
# is 2437. `/query/parse` echoes each as a Rule with an EMPTY `fieldTitle`, the shape
# cycle 11 recorded for `id`. plexsim reported them as an unknown FIELD and answered
# the whole library, so each was a filter that silently did nothing.
for _bare in ("viewOffset", "skipCount", "ratingCount", "absoluteIndex"):
    FILTER_FIELD_TYPES.setdefault(_bare, "integer")
for _bare in ("updatedAt", "createdAt", "lastRatedAt"):
    FILTER_FIELD_TYPES.setdefault(_bare, "date")
FILTER_FIELD_TYPES.setdefault("mediaCount", "integer")
FILTER_FIELD_TYPES.setdefault("originalTitle", "string")
# Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: four MORE
# columns filter, and none of them appears in any Meta either. `parent=<ratingKey>`
# is the row's immediate `parentRatingKey` -- kids' `type=4` goes 6 -> 3 for a season
# and 6 -> 0 for the show above it, zen's 9287 -> 22 and -> 0, and it reaches
# `/library/all` as well. `rating=` and `audienceRating=` are the two agent ratings
# (`audienceRating>>=9` is 107 of zen's 3887 and `rating>>=9` is 142, `<<=1` is 0 for
# both). `file=` is a case-insensitive SUBSTRING of the Part's path: `unn` finds Big
# Buck Bunny, `.mkv` is 3811 of 3887, an empty value is every row and `zzz` is none --
# which is the `string` type's bare `=` exactly. plexsim ignored all four, and an
# ignored filter is worse than a refused one: it answered every row to
# `audienceRating>>=9` AND to `audienceRating<<=1`.
FILTER_FIELD_TYPES.setdefault("parent", "integer")
for _bare in ("rating", "audienceRating"):
    FILTER_FIELD_TYPES.setdefault(_bare, "integer")
FILTER_FIELD_TYPES.setdefault("file", "string")
# And `parentIndex` is a 500 on a listing on BOTH references -- on a movie library, on
# a show library and at type=4 alike -- where plexsim filtered on it happily. It is the
# one column of this family that refuses rather than filters.
REFUSED_FILTER_FIELDS = {"parentIndex"}

# Cycle 15, measured on kids (1.43.4) and zen (1.42.2): `/query/parse` knows fields a
# LISTING does not filter on. All eight of these come back as a `Rule` with an EMPTY
# `fieldTitle` -- the shape cycle 11 recorded for `id` -- where plexsim answered an
# `Error`, which is what the smart-list builder draws as "that field does not exist".
# `parentIndex` is a 500 on a listing and still a Rule here, and `guid` is the sharpest
# of them: plexsim's listing has filtered on `guid=` since cycle 8 while its own
# `/query/parse` called the field unknown, so the server contradicted itself.
PARSE_ONLY_FIELD_TYPES = {"parentIndex": "integer", "guid": "string",
                          "slug": "string", "extraType": "integer"}

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
# The three leaf-count filters that read a SHOW's counts even on a season listing.
# Measured on zen (48 seasons for `type=3&viewedLeafCount>>0`, every one of them with a
# count of 0 on its own row) and on the copy.
LEAF_COUNT_FILTERS = ("viewedLeafCount", "unviewedLeafCount", "viewedOrInProgressLeafCount")

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
# `/library/sections/{id}/userRating` -- a FIXED scale, identical on kids (1.43.4) and
# zen (1.42.2) and on a movie library and a show library alike, however the library's own
# ratings are distributed. The key is Plex's 0-10 rating and -1 is the unrated bucket.
USER_RATINGS = (("10", "5 stars"), ("8", "4 stars"), ("6", "3 stars"),
                ("4", "2 stars"), ("2", "1 star"), ("-1", "Not rated"))

# The axes of OTHER library types. A movie library answers each an EMPTY secondary
# container with its own title2, not a 404 -- measured on both references.
CROSS_TYPE_AXES = {"mood": "By Mood", "style": "By Style", "format": "By Format",
                   "subformat": "By Type", "source": "By Source",
                   "make": "By Camera Make", "model": "By Camera Model",
                   "place": "By Place", "tag": "By Tag"}

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
    # Cycle 14: measured on both references, and a LABEL rather than a value list.
    "userRating": "By Rating",
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

# `/library/sections/{id}/filters`, captured entry for entry from a real PMS.
# Re-measured in cycle 8, which corrects how this was keyed: the list belongs to the
# `?type=` asked for, not to the library. A movie library answers the 17 episode
# filters to `type=4` and a show library the 28 movie ones to `type=1`, identically,
# and only the DEFAULT follows the library. `type=3` and every type with no list are
# an empty 200, a non-number is a 400, and `includeAdvanced=1` appends the Duplicates
# filter to types 1 and 4 only. plexsim served the library's own list to every type
# and never sent Duplicates, so an episode view's filter bar offered show filters.
# The key suffixes (`?type=2` on most show keys, but not on `country`, `unmatched`
# or `editionTitle`) are measured and not derivable.
#
# Seven of these axes 404 on the reference itself (`hdr`, `dovi`, `hdr10plus`,
# `atmos`, `inProgress`, `unmatched`, `unwatchedLeaves`). They are advertised
# anyway, because that is what a real PMS advertises -- plexsim's unknown-leaf
# 404 reproduces the rest of it for free.
_SECTION_FILTERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "section_filters.json")
try:
    with open(_SECTION_FILTERS_PATH) as _handle:
        SECTION_FILTERS = json.load(_handle)
except OSError:
    SECTION_FILTERS = {"plain": {}, "advanced": {}}

# `/library/sections/{id}/sorts`, per requested TYPE, captured like the filters above.
_SECTION_SORTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "section_sorts.json")
try:
    with open(_SECTION_SORTS_PATH) as _handle:
        SECTION_SORTS = json.load(_handle)
except OSError:
    SECTION_SORTS = {"plain": {}, "advanced": {}}


def _begins_word(text, wanted):
    """True when `wanted` (lowercase) starts at the beginning of a word of `text`.

    Measured on both servers for `/library/sections/{id}/autocomplete`: the query is
    a PHRASE that must begin a whitespace-separated word -- `ste` finds Tears of
    Steel, `of s` finds it too, `tears steel` does not, and neither `gate` in
    Steins;Gate nor `burbs` in The 'Burbs matches, because only whitespace starts a
    word. This is not the listing's `title=` matcher, whose words may skip.
    """
    lowered = str(text).lower()
    return any((at == 0 or lowered[at - 1].isspace()) and lowered.startswith(wanted, at)
               for at in range(len(lowered)))


# `/playlists/sorts`, from a real PMS.
# Cycle 15, measured on kids (1.43.4) and zen (1.42.2), byte-identical on both:
# the `Meta` descriptor `/playlists?includeMeta=1` carries. Its sixth sort is
# `mediaCount` "Item Count" where `/playlists/sorts` below ends with `updatedAt`
# "Date Modified" -- the two lists really are different, and each was measured on
# its own path. The client reads `Meta.Type[].Sort` straight into its sort menu.
# Cycle 15, captured verbatim from plexscratch: the two settings a collection's
# `includePreferences=1` block carries. Which SECOND one appears depends on `smart` --
# a smart collection has `collectionFilterBasedOnUser` and no collection ORDER at all,
# which the write side agrees with (a `collectionSort` write to a smart collection is a
# 400 where `collectionMode` is a 200).
COLLECTION_PREFS = {
    "collectionMode": {
        "id": "collectionMode", "label": "Collection Mode",
        "summary": "How the items in the collection are displayed on the Library tab.",
        "type": "text", "default": "-1", "value": "-1", "hidden": False,
        "advanced": False, "group": "",
        "enumValues": "-1:Library default|1:Hide items in this collection"
                      "|2:Show this collection and its items"
                      "|0:Hide this collection but show its items"},
    "collectionSort": {
        "id": "collectionSort", "label": "Collection order",
        "summary": "How to sort the items in the collection.",
        "type": "text", "default": "0", "value": "0", "hidden": False,
        "advanced": False, "group": "",
        "enumValues": "0:Release date|1:Alphabetical"},
    "collectionFilterBasedOnUser": {
        "id": "collectionFilterBasedOnUser",
        "label": "Collection filtering based on",
        "summary": "Which user's activity (e.g. user rating, play count, or watched "
                   "status) is used for the collection filtering.",
        "type": "text", "default": "0", "value": "0", "hidden": False,
        "advanced": False, "group": "",
        "enumValues": "0:Always the server admin user"
                      "|1:User currently viewing the content"},
}


PLAYLIST_META = {"Type": [{
    "key": "/playlists", "type": "playlist", "title": "All", "active": True,
    "Sort": [
        {"active": True, "activeDirection": "asc", "default": "asc",
         "defaultDirection": "asc", "descKey": "titleSort:desc",
         "key": "titleSort", "title": "Name"},
        {"defaultDirection": "desc", "descKey": "viewCount:desc",
         "key": "viewCount", "title": "Plays"},
        {"defaultDirection": "desc", "descKey": "lastViewedAt:desc",
         "key": "lastViewedAt", "title": "Last Played"},
        {"defaultDirection": "desc", "descKey": "duration:desc",
         "key": "duration", "title": "Duration"},
        {"defaultDirection": "desc", "descKey": "addedAt:desc",
         "key": "addedAt", "title": "Date Added"},
        {"defaultDirection": "desc", "descKey": "mediaCount:desc",
         "key": "mediaCount", "title": "Item Count"},
    ]}]}

# And the one-row descriptor `/hubs/home/recentlyAdded?type=N&includeMeta=1` carries.
# `active` flips with the type and only these two types have a block at all -- 3, 4,
# 8, a non-number and no type answer no `Meta` on either reference.
HOME_RECENT_META = {
    "1": {"Type": [{"key": "/hubs/home/recentlyAdded?type=1", "type": "movie",
                    "title": "Recently Added", "active": True}]},
    "2": {"Type": [{"key": "/hubs/home/recentlyAdded?type=2", "type": "mixed",
                    "title": "Recently Added", "active": False}]},
}

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
        # (section id, setting id) -> the library-wide value of a show setting
        # a show inherits when its own is `-1`. Seeded from SECTION_PREFS at load.
        self.section_show_prefs = {}

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
        self._strip_show_fields()
        self._apply_section_defaults()
        # Row and media rules measured after most catalogs were built; see
        # hierarchy.normalize_rows and mediainfo.normalize_media for each rule.
        hierarchy_module.normalize_rows(list(self.items.values()))
        for row in self.items.values():
            for media in row.get("Media") or []:
                mediainfo_module.normalize_media(media)
        # Titles last: they are composed from the codec, language, resolution and flag
        # fields normalize_media has just corrected. Composed first, a stored `dts` track
        # kept a DTS title after its codec became `dca` with the `ma` profile.
        self._retitle_streams()
        # Measured: a show's LISTING row carries `duration` (a typical episode's) and
        # `originallyAvailableAt` (its first air date) exactly as its detail does;
        # plexsim computed them for the detail page only.
        for row in list(self.items.values()):
            if row.get("type") != "show":
                continue
            leaves = self.leaves(row["ratingKey"])
            durations = [leaf.get("duration") for leaf in leaves if leaf.get("duration")]
            if durations:
                row.setdefault("duration", int(sum(durations) / len(durations)))
            aired = sorted(leaf.get("originallyAvailableAt") for leaf in leaves
                           if leaf.get("originallyAvailableAt"))
            if aired:
                row.setdefault("originallyAvailableAt", aired[0])

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
                if setting.get("id") in SHOW_ROW_PREFS:
                    self.set_section_show_pref(section["key"], setting["id"],
                                               setting.get("value"))

    def set_section_show_pref(self, section_id, name, value):
        """Store a library-wide show setting and re-apply every show's rows.

        Cycle 15, measured on the copy: the LIBRARY-level `flattenSeasons` and
        `episodeSort` produce exactly the row effects the item path produces, and
        the show's OWN setting stays at `-1` while they do. So the item value is a
        tri-state and `-1` means INHERIT -- not "off", which is how plexsim read it.
        """
        self.section_show_prefs[(str(section_id), name)] = str(value)
        for row in self.section_items(section_id):
            if row.get("type") == "show":
                self.apply_show_prefs(row)

    def show_pref(self, show, name):
        """The EFFECTIVE value of a show setting: the item's unless it inherits.

        Measured on the copy across all four combinations, for `flattenSeasons` and
        `episodeSort` alike:

            section 1, item -1 -> the effect is ON   (the item inherits)
            section 1, item  0 -> the effect is OFF  (the item overrides, downwards)
            section 0, item  1 -> the effect is ON   (the item overrides, upwards)
            section 0, item -1 -> the effect is OFF

        plexsim read only the item's `_prefs`, defaulting to `-1`, so a library-wide
        write -- which is where the Plex UI actually puts these two -- moved the
        setting list and nothing else. `ITEM_PREFS` supplies the value an item that
        has never been written advertises.
        """
        stored = (show.get("_prefs") or {}).get(name)
        if stored is None:
            for setting in ITEM_PREFS.get(show.get("type")) or []:
                if setting.get("id") == name:
                    stored = setting.get("value")
                    break
        if stored is not None and str(stored) != "-1":
            return str(stored)
        section = self.section_show_prefs.get((str(show.get("librarySectionID")), name))
        if section is not None:
            return section
        for setting in SECTION_PREFS.get("show") or []:
            if setting.get("id") == name:
                return str(setting.get("value"))
        return "-1"

    def restamp_children(self, row):
        """Move the borrowed artwork stamp onto the children, as an artwork pick does.

        Cycle 14 measured the cascade for a PICK; a clear is the same write with the
        field removed, so the children that borrow the field follow it.
        """
        for child in self.children(row.get("ratingKey") or ""):
            for borrowed, own in (("parentThumb", "thumb"), ("parentArt", "art")):
                if borrowed in child:
                    if row.get(own) is None:
                        child.pop(borrowed, None)
                    else:
                        child[borrowed] = row[own]
            for leaf in self.leaves(child.get("ratingKey") or ""):
                for borrowed, own in (("grandparentThumb", "thumb"),
                                      ("grandparentArt", "art")):
                    if borrowed in leaf:
                        if row.get(own) is None:
                            leaf.pop(borrowed, None)
                        else:
                            leaf[borrowed] = row[own]

    def apply_show_prefs(self, show):
        """The row effects of a show's effective `flattenSeasons`, measured on the copy.

        `flattenSeasons=1`: the show gains `skipChildren: true` and every episode
        under it gains `skipParent: true` and **loses `parentRatingKey` and
        `parentKey`**, keeping `parentIndex` and `parentTitle`. `=0` and `=-1` put all
        of it back. `/children` still answers the SEASONS either way -- the client
        decides to skip the season level on the strength of those fields.

        `=2` is "hide for single-season series" at the section level; the item enum is
        the same three values, and only `1` was exercised on a two-season show, so the
        `2` branch follows the section rule rather than a measurement of its own.

        The value is the EFFECTIVE one -- see `show_pref`. Cycle 14 measured this on
        the ITEM path only and cycle 15 found the section path produced half of it.
        """
        value = self.show_pref(show, "flattenSeasons")
        seasons = self.children(show["ratingKey"])
        skip = value == "1" or (value == "2" and len(seasons) <= 1)
        if skip:
            show["skipChildren"] = True
        else:
            show.pop("skipChildren", None)
        for leaf in self.leaves(show["ratingKey"]):
            if skip:
                # The season key is kept privately so `=0` can put it back: the
                # child index still knows the parent, but the ROW must not say it.
                leaf.setdefault("_parentRatingKey", leaf.get("parentRatingKey"))
                leaf["skipParent"] = True
                leaf.pop("parentRatingKey", None)
                leaf.pop("parentKey", None)
            else:
                leaf.pop("skipParent", None)
                season = leaf.pop("_parentRatingKey", None)
                if season is not None:
                    leaf["parentRatingKey"] = season
                    leaf["parentKey"] = "/library/metadata/%s" % season

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

    def tag_id(self, field, name):
        """The id the tag table gives one tag name, without needing a row to hang
        it on. Same seed as `number_tags`, so a collection's `index` and its
        members' `Collection` tag agree."""
        return zlib.crc32(("%s:%s" % (field, name)).encode()) % 900000 + 1000

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
        # Measured on a writable copy of the reference: a split's new row takes a key
        # NEVER used before (67 after 57), not the one a merge just freed. plexsim reused
        # the highest free key, so a split after a merge put the new row on the absorbed
        # row's old key -- one a client's cache still holds for the absorbed row.
        retired = [k for k in getattr(self, "_retired", ()) if base <= k < base + 10000]
        return (max(used + retired) + 1) if used or retired else base + 1

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
        self.__dict__.setdefault("_retired", set()).add(rating_key)
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
        # Cycle 14, on zen: the word matcher folds combining marks on BOTH sides.
        # `Amelie` finds "Amelie" and `Bogota` finds "Bogota: City of the Lost" --
        # and so does the accented spelling of either, on /hubs/search and on
        # /library/search alike (same 0.93095 score for both spellings). The
        # section's substring box does NOT: there `Amelie` finds nothing and only
        # the accented spelling hits, so that one is left alone below.
        #
        # plexsim split on [^0-9a-z], which makes an accented letter a SEPARATOR:
        # "Amelie" became the two words "am" and "lie", so the quick search never
        # found the row by its own name and did find it under a fragment of one.
        needle = hierarchy_module.fold_marks(query).lower().strip()
        if len(needle) < 2:
            return []

        def hit(text):
            words = re.split(r"[^0-9a-z]+",
                             hierarchy_module.fold_marks(str(text or "")).lower())
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
    if out.get("type") == "episode" and out.get("grandparentThumb"):
        # Measured on both servers, detail and listing alike: an episode's
        # coverPoster is its SHOW's poster and its own 16:9 still is `snapshot`.
        # plexsim labelled the still as the poster.
        images.append({"alt": title, "type": "coverPoster", "url": out["grandparentThumb"]})
        if out.get("thumb"):
            images.append({"alt": title, "type": "snapshot", "url": out["thumb"]})
    elif out.get("thumb"):
        images.append({"alt": title, "type": "coverPoster", "url": out["thumb"]})
    if out.get("art"):
        images.append({"alt": title, "type": "background", "url": out["art"]})
    if images:
        out["Image"] = images
    return out


# Tag lists a grid or hub row carries, each tag reduced to its text -- and CAPPED.
# Measured on kids (1.43.4) and zen (1.42.2) against a real library, which is the only
# place the cap is visible: a light row carries at most 2 Genre, 2 Writer, 2 Country and
# 3 Role, always the first of the detail row's own order, where the DETAIL row carries
# the lot (one zen movie has 155 roles on its detail row and 3 on every listing). A
# five-clip library cannot show this -- no item has enough tags to be truncated -- which
# is why eleven cycles of row-shape work missed it. `Director` is not capped in any
# sample available (the richest row in 240 zen movies has two), so it is left alone
# rather than guessed at; `Collection` and `Label` had no sample either.
GRID_TAG_CAPS = {"Genre": 2, "Writer": 2, "Country": 2, "Role": 3, "Director": 3}
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


def _with_client_titles(node, product):
    """A response with every Stream's displayTitle as `product` is sent it.

    Measured on both servers: Plex Web and twelve other products get the codec in a video's
    and a subtitle's displayTitle (mediainfo.CODEC_TITLE_PRODUCTS). Only the dicts and lists
    on the way to a changed Stream are copied: responses hand back the catalog's own
    Media/Part/Stream dicts, and a title written into one would reach every other client.
    """
    if isinstance(node, list):
        items = [_with_client_titles(item, product) for item in node]
        return items if any(new is not old for new, old in zip(items, node)) else node
    if not isinstance(node, dict):
        return node
    copy = None
    if node.get("streamType") in (1, 3) and node.get("displayTitle"):
        title = mediainfo_module.client_display_title(node, product)
        if title != node["displayTitle"]:
            copy = dict(node, displayTitle=title)
    for key, value in node.items():
        if isinstance(value, (dict, list)):
            changed = _with_client_titles(value, product)
            if changed is not value:
                copy = dict(node) if copy is None else copy
                copy[key] = changed
    return node if copy is None else copy


def _image_type(body):
    """The content type of an uploaded image, from its magic bytes."""
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _exclude_fields(container, spec):
    """`excludeFields=` applied to a whole response, as a real PMS applies it.

    Measured: the named fields go from EVERY row -- section listings, `/children`,
    `/allLeaves`, hubs, search -- and from the container itself (a show's
    `/children?excludeFields=summary` loses the container's summary too). plexsim
    applied it to detail reads only, and the client sends `excludeFields=summary` on
    every library hubs request. Rows are copied, never trimmed in place.
    """
    fields = {name for name in spec.split(",") if name}
    body = container.get("MediaContainer")
    if not fields or not isinstance(body, dict):
        return container

    def trim(rows):
        return [{k: v for k, v in row.items() if k not in fields}
                if isinstance(row, dict) else row for row in rows]

    body = {k: v for k, v in body.items() if k not in fields}
    if isinstance(body.get("Metadata"), list):
        body["Metadata"] = trim(body["Metadata"])
    if isinstance(body.get("Hub"), list):
        body["Hub"] = [dict(hub, Metadata=trim(hub["Metadata"]))
                       if isinstance(hub.get("Metadata"), list) else hub
                       for hub in body["Hub"]]
    return dict(container, MediaContainer=body)


# `/hubs/sections/{id}/manage`, measured per library type: (identifier, title,
# promoted to home). Every row is visible in Recommended and promoted there.
MANAGED_HUBS = {
    "movie": [
        ("movie.recentlyreleased", "Recently Released Movies", False),
        ("movie.recentlyadded", "Recently Added Movies", True),
        ("recent.library.playlists", "Library Playlists", False),
        ("movie.genre", "Top Movies in (Genre)", False),
        ("movie.by.actor.or.director", "Top Movies by (Actor or Director)", False),
        ("movie.topunwatched", "Top Unwatched Movies", False),
        ("movie.curated", "Seasonal Movies", True),
        ("movie.recentlyviewed", "Recently Watched Movies", False),
    ],
    "show": [
        ("tv.recentlyaired", "Recently Released Episodes", False),
        ("tv.recentlyadded", "Recently Added TV", True),
        ("recent.library.playlists", "Library Playlists", False),
        ("tv.startwatching", "Start Watching", False),
        ("tv.rediscover", "Rediscover", False),
        ("tv.morefromnetwork", "More from (Network)", False),
        ("tv.moreingenre", "More in (Genre)", False),
        ("tv.toprated", "Top Rated TV", False),
        ("tv.recentlyviewed", "Recently Watched Episodes", False),
    ],
}


def _grid_row(row, keep_section=True, keep_guid=False, keep_location=False):
    """A row as a GRID or a HUB shelf carries it -- lighter than the detail row.

    Also measured: no `Guid` on a hub or search row (a listing decides that by
    `includeGuids`, and passes `keep_guid`), and a SHOW in search keeps its
    `Location`.

    Measured in the client's own corpus against the reference, and on fun: a grid
    or shelf row's tags are `{tag}` and nothing else (no id, filter, role or thumb),
    a Part carries no `Stream` list, and there is no `Rating`, `Location`, `Field`
    or `Producer`. A SECTION listing's rows also leave the librarySection fields to
    the container, where a hub's rows keep them. plexsim sent the full detail row
    everywhere. The catalog's own lists are shared, so this copies what it trims.
    """
    out = {k: v for k, v in row.items()
           if k not in ("Rating", "Field", "Producer")
           and (k != "Location" or keep_location)
           and (k != "Guid" or keep_guid)}
    if not keep_section:
        for key in ("librarySectionID", "librarySectionTitle", "librarySectionKey"):
            out.pop(key, None)
    for key in GRID_TAGS:
        if isinstance(out.get(key), list):
            out[key] = [{"tag": tag["tag"]} for tag in out[key]
                        if isinstance(tag, dict) and tag.get("tag")][:GRID_TAG_CAPS.get(key)]
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
        # Sidecar subtitles the Upload modal has sent, by stream id: the bytes are kept
        # so a DELETE of one can tell an uploaded stream from an embedded track.
        self.uploaded_subtitles = {}
        # ratingKeys a decision has been made for. The timeline's `Bandwidths`
        # block appears only after one, measured -- not merely while playing.
        self.decided = set()
        # Images uploaded through `POST /library/metadata/{id}/{posters,arts}`, keyed by the
        # `upload://` id a real PMS mints from the bytes.
        self.uploads = {}
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
        # section id -> {ratingKey: the last finished/removed timeline state}.
        self.section_timeline = {}
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

    def notify_activity_run(self, kind, title, steps):
        """One activity from `started` to `ended`, with the measured `updated` steps --
        (progress, subtitle) pairs -- between. The end keeps the last subtitle."""
        activity = {"uuid": str(uuid.uuid4()), "type": kind, "cancellable": False,
                    "userID": 1, "title": title, "subtitle": "", "progress": 0}
        self.notify_activity(activity, "started")
        for progress, subtitle in steps:
            activity.update(progress=progress, subtitle=subtitle)
            self.notify_activity(activity, "updated")
        activity["progress"] = 100
        self.notify_activity(activity, "ended")

    def notify_status(self, title, name="LIBRARY_UPDATE", description=""):
        self.notify("status", "StatusNotification",
                    [{"title": title, "description": description, "notificationName": name}])

    def notify_progress(self, message):
        self.notify("progress", "ProgressNotification", [{"message": message}])

    def notify_section_scan(self, section_id, section):
        """The frames a library scan pushes, measured on a writable copy of the reference.

        One `library.update.section` activity starts as `Scanning`, a status names the
        section, the activity is retitled `Scanning <library>` and steps through the
        items -- a `progress` frame and an `updated` frame each, the subtitle the item's
        `title (year)`, the percentage `round(n * 99 / count)` -- to 100, then it ends
        and a status says the scan is complete. plexsim started and ended a metadata
        activity per row instead, which no scan on a real server does.
        """
        title = section.get("title") or ""
        activity = {"uuid": str(uuid.uuid4()), "type": "library.update.section",
                    "cancellable": False, "userID": 1, "title": "Scanning", "subtitle": "",
                    "progress": 0}
        self.notify_activity(activity, "started")
        self.notify_status('Scanning the "%s" section' % title)
        activity["title"] = "Scanning %s" % title
        self.notify_activity(activity, "updated")
        # Measured on a writable copy of the reference: a MOVIE library steps per movie,
        # and a SHOW library per SEASON -- the progress line is `Scanning <show>/Season 02`
        # and the activity's subtitle `<show> - Season 02`, deepest season first. plexsim
        # stepped a show library by show, so a scan of a library of long shows reported one
        # step per series.
        steps = []
        for row in self.catalog.section_items(section_id):
            label = ("%s (%s)" % (row.get("title") or "", row["year"]) if row.get("year")
                     else row.get("title") or "")
            if row.get("type") == "movie":
                steps.append((label, label))
            elif row.get("type") == "show":
                seasons = sorted(self.catalog.children(row["ratingKey"]),
                                 key=lambda s: -int(s.get("index") or 0))
                for season in seasons:
                    name = "Season %02d" % int(season.get("index") or 0)
                    steps.append(("%s/%s" % (label, name), "%s - %s" % (label, name)))
                if not seasons:
                    steps.append((label, label))
        for number, (message, subtitle) in enumerate(steps, 1):
            self.notify_progress("Scanning %s" % message)
            # The Context names the library the activity belongs to -- measured on every
            # `updated` frame of a scan, and the client keys the Libraries page's progress
            # bar on it. plexsim sent none, so a scan showed no progress against a library.
            activity.update(subtitle=subtitle,
                            progress=int(round(number * 99.0 / len(steps))),
                            Context={"librarySectionID": str(section_id)})
            self.notify_activity(activity, "updated")
        activity["progress"] = 100
        self.notify_activity(activity, "updated")
        # Measured on the copy, in this exact interleaving: a scan ends inside a NESTED
        # pair of `provider.subscriptions.process` activities. The outer one starts
        # between the section activity's last `updated` and its `ended`; the inner one
        # runs after the completion status; then the inner ends and the outer ends. Four
        # frames plexsim pushed none of, and the Activity pane draws a row for each.
        outer = {"uuid": str(uuid.uuid4()), "type": "provider.subscriptions.process",
                 "cancellable": False, "userID": 1, "title": "Processing subscriptions",
                 "subtitle": "", "progress": 0}
        self.notify_activity(outer, "started")
        self.notify_activity(activity, "ended")
        self.notify_status("Library scan complete",
                           description="Extra information may still be downloading from the Internet")
        inner = dict(outer, uuid=str(uuid.uuid4()))
        self.notify_activity(inner, "started")
        inner["progress"] = 100
        self.notify_activity(inner, "ended")
        outer["progress"] = 100
        self.notify_activity(outer, "ended")

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

    def notify_timeline(self, row, state=3, metadata_state="queued", media_state=None,
                        type_number=None):
        """A TimelineEntry for one row, with the field set measured off a real
        refresh: identifier, sectionID and itemID as STRINGS, the numeric type,
        the title, `state` and `metadataState`.

        `metadata_state=None` omits the field, which is what state 5 does.
        """
        title = row.get("title") or ""
        kind = row.get("type")
        if kind in ("movie", "show") and row.get("year"):
            # Measured: a movie's and a show's frames title them with the year,
            # `Elephants Dream (2006)`, `Firefly (2002)` -- an emptied title still gets
            # it, ` (2010)` -- which the Activity pane prints.
            title = "%s (%s)" % (title, row["year"])
        elif kind in ("season", "episode"):
            # Measured on a writable copy of the reference: a season's frames say
            # `Firefly S01` and an episode's `Firefly S01 E07`, from the SHOW's title and
            # the current indexes, where plexsim sent the row's own title.
            show_key = str(row.get("parentRatingKey" if kind == "season" else "grandparentRatingKey") or "")
            show = self.catalog.items.get(int(show_key)) if show_key.isdigit() else None
            season_index = row.get("index") if kind == "season" else row.get("parentIndex")
            title = "%s S%02d" % ((show or {}).get("title") or "", int(season_index or 0))
            if kind == "episode":
                title += " E%02d" % int(row.get("index") or 0)
        entry = {
            "identifier": "com.plexapp.plugins.library",
            "sectionID": str(row.get("librarySectionID", "")),
            "itemID": str(row.get("ratingKey", "")),
            "type": (type_number if type_number is not None
                     else self.TIMELINE_TYPES.get(row.get("type"), 1)),
            "title": title,
            "state": state,
            "updatedAt": int(time.time()),
        }
        if metadata_state is not None:
            entry["metadataState"] = metadata_state
        if media_state:
            entry["mediaState"] = media_state
        section_key = str(row.get("librarySectionID", ""))
        if state in (self.TIMELINE_DONE, self.TIMELINE_DELETED) and section_key.isdigit():
            # What a library's /timeline lists: the last finished or removed state
            # of each item, keyed by ratingKey.
            record = {"state": state, "at": entry["updatedAt"]}
            if state == self.TIMELINE_DELETED:
                stub = {"ratingKey": str(row.get("ratingKey", "")),
                        "key": "/library/metadata/%s" % row.get("ratingKey", ""),
                        "guid": "", "type": row.get("subtype") or row.get("type"),
                        "title": "", "summary": "", "state": state,
                        "metadataState": "deleted"}
                if row.get("type") == "show":
                    for count in ("childCount", "index", "leafCount", "viewedLeafCount"):
                        stub[count] = row.get(count) or 0
                record["stub"] = stub
            self.section_timeline.setdefault(section_key, {})[str(row.get("ratingKey"))] = record
        self.notify("timeline", "TimelineEntry", [entry])

    @staticmethod
    def _frame_title(row):
        """A row's name as an activity subtitle prints it: `title (year)`.

        Measured on the copy: the refresh activity's subtitle is `Sintel (2010)`, the
        same form the scan activity uses for each item it steps through.
        """
        title = row.get("title") or ""
        return "%s (%s)" % (title, row["year"]) if row.get("year") else title

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
        # Measured on the copy, and what the docstring above already said: the `updated`
        # frame comes SECOND, before `processing`. plexsim emitted it inside
        # finish_activity, after `loading` -- the rule was recorded right and implemented
        # as something else. The subtitle is the row's `title (year)`, as the scan
        # activity's is; plexsim sent the bare title.
        activity = self.library.add_activity(self._frame_title(row), progress=-1)
        activity["progress"] = 49
        self.library.notify_activity(activity, "updated")
        self.notify_timeline(row, self.TIMELINE_QUEUED, "processing")
        self.notify_timeline(row, self.TIMELINE_LOADING, "loading")
        self.library.finish_activity(activity)
        self.notify_timeline(row, self.TIMELINE_DONE, None)

    def notify_show_cascade(self, show):
        """The frames a show-wide re-read pushes, measured on a writable copy of the reference.

        The show goes queued -> processing -> loading inside one `Updating Metadata`
        activity; then each SEASON in turn (state 5 `processing`, then 4 `loading`) with its
        EPISODES inside it (5 `processing`, 4 `loading` twice, then 5 with no metadataState);
        each episode is then announced again with `mediaState` analyzing and thumbnailing and
        a bare 5; the season closes at 5, the activity steps per season, and the SHOW's own
        state 5 ends the cascade. Forty frames for a two-season show, and the shape is what
        clears every `!!metadataState` gate the client latches on.
        """
        activity = self.library.add_activity(show.get("title") or "", progress=-1)
        self.notify_timeline(show, self.TIMELINE_QUEUED, "queued")
        self.notify_timeline(show, self.TIMELINE_QUEUED, "processing")
        self.notify_timeline(show, self.TIMELINE_LOADING, "loading")
        seasons = self.catalog.children(show["ratingKey"])
        for number, season in enumerate(seasons, 1):
            # The same string a season's own frames carry: `<show> S01`.
            activity["subtitle"] = "%s S%02d" % (show.get("title") or "",
                                                 int(season.get("index") or 0))
            activity["progress"] = int(round(number * 89.0 / max(len(seasons), 1)))
            self.notify_activity(activity, "updated")
            self.notify_timeline(season, self.TIMELINE_DONE, "processing")
            self.notify_timeline(season, self.TIMELINE_LOADING, "loading")
            episodes = self.catalog.children(season["ratingKey"])
            for episode in episodes:
                self.notify_timeline(episode, self.TIMELINE_DONE, "processing")
                self.notify_timeline(episode, self.TIMELINE_LOADING, "loading")
                self.notify_timeline(episode, self.TIMELINE_LOADING, "loading")
                self.notify_timeline(episode, self.TIMELINE_DONE, None)
            for episode in episodes:
                # The media pass, measured after the metadata one: the same rows again,
                # analyzing then thumbnailing, then bare.
                self.notify_timeline(episode, self.TIMELINE_DONE, None, media_state="analyzing")
                self.notify_timeline(episode, self.TIMELINE_DONE, None, media_state="thumbnailing")
                self.notify_timeline(episode, self.TIMELINE_DONE, None)
            self.notify_timeline(season, self.TIMELINE_DONE, None)
        self.library.finish_activity(activity)
        self.notify_timeline(show, self.TIMELINE_DONE, None)

    def notify_queue_regenerated(self):
        """The `backgroundProcessingQueue` frame that follows a queue-touching write.

        Cycle 15, measured on the copy: it is neither create-specific nor periodic.
        It arrived +7.1 s after an analyze and +63.1 s after a library create here,
        and cycle 14 saw it +2.4 s after a season split and +1.2 s after a collection
        add. plexsim never sent it, so a client watching the queue never learned it
        had been rebuilt. The DELAY is not reproduced -- plexsim pushes its frames
        synchronously -- and that is recorded rather than guessed at.
        """
        self.notify("backgroundProcessingQueue",
                    "BackgroundProcessingQueueEventNotification",
                    [{"queueID": 1, "event": "queueRegenerated"}])

    def notify_season_cascade(self, season):
        """The eighteen frames a SEASON refresh pushes. Cycle 15, measured.

        Season queued, the activity started with the season's own frame title,
        season processing, season loading; then each episode's FOUR -- state 5
        `processing`, state 4 `loading` twice, state 5 with no `metadataState` --
        and finally the activity ended and the season's own state 5. There is no
        `activity updated` here, unlike the episode form, which has one.
        """
        activity = self.library.add_activity(self._frame_title(season), progress=-1)
        self.notify_timeline(season, self.TIMELINE_QUEUED, "queued")
        self.notify_timeline(season, self.TIMELINE_QUEUED, "processing")
        self.notify_timeline(season, self.TIMELINE_LOADING, "loading")
        for episode in self.catalog.children(season["ratingKey"]):
            self.notify_timeline(episode, self.TIMELINE_DONE, "processing")
            self.notify_timeline(episode, self.TIMELINE_LOADING, "loading")
            self.notify_timeline(episode, self.TIMELINE_LOADING, "loading")
            self.notify_timeline(episode, self.TIMELINE_DONE, None)
        self.library.finish_activity(activity)
        self.notify_timeline(season, self.TIMELINE_DONE, None)

    def notify_edited(self, row, metadata_state="loading"):
        """The two frames an EDIT pushes: state 3, then done. plexsim pushed none,
        so an edit made here was invisible to every other open client. Measured:
        the first frame says `loading` for an edit to the item's own path and
        `queued` for one saved through `/library/sections/{id}/all`."""
        self.notify_timeline(row, self.TIMELINE_QUEUED, metadata_state)
        self.notify_timeline(row, self.TIMELINE_DONE, None)

    def notify_list_edited(self, target, kind):
        """A playlist's or a collection's own edit pair. Measured on a writable copy of
        the reference: a rename or a summary edit -- through `/playlists/{id}`,
        `/library/collections/{id}` or `/library/metadata/{id}` -- pushes state 3
        `loading` then 5 for the list itself, type 15 with sectionID -1 for a playlist
        and type 18 with its library for a collection. plexsim pushed nothing, so a
        list renamed in one client kept its old name in every other."""
        self.notify_edited({"ratingKey": target["ratingKey"], "type": kind,
                            "title": target.get("title") or "",
                            "librarySectionID": target.get("librarySectionID", -1)}, "loading")

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
        if query.get("includeChildren") == "1" and row.get("type") == "show":
            # A show gains its seasons INLINE, as a `Children` block with its own
            # size -- not a second request to /children.
            #
            # Cycle 15, measured on kids (1.43.4) and zen (1.42.2): a SHOW and
            # nothing else. Neither reference inlines a season's episodes, and
            # neither is silent for want of them -- kids' season 7 holds three and
            # zen's season 17201 holds twenty-two. plexsim inlined them, so a season
            # page was handed an episode list it had not asked for and then fetched
            # the same list again from /children.
            children = self.catalog.children(row["ratingKey"])
            if children:
                detail["Children"] = {"size": len(children),
                                      "Metadata": [_full(c) for c in children]}
        if query.get("includeRelated") == "1":
            # Measured on the reference: the item's related hubs as
            # `{"Hub": [...]}`, and an empty OBJECT -- not an absent block -- for an
            # item with none. plexsim always sent the empty object, even where the
            # hubs endpoint had a shelf for the same item.
            # Cycle 15, measured on zen (kids holds no related items at all, so it
            # cannot witness this): the INLINE block caps at FIVE rows per hub and
            # says `more: true`, where the standalone `/hubs/metadata/{id}/related`
            # answers six for the same row. `includeRelatedCount=N` sets that cap --
            # `=1` gives one and `=3` gives three -- and does nothing on its own.
            # plexsim honoured `count=` on the standalone path and ignored this
            # spelling, so a real catalog's preplay page carried every related row.
            hubs = self.related_hubs(row, query)
            cap = query.get("includeRelatedCount")
            cap = int(cap) if str(cap or "").isdigit() and int(cap) > 0 else 5
            trimmed = []
            for hub in hubs:
                rows = (hub.get("Metadata") or [])[:cap]
                trimmed.append(dict(hub, Metadata=rows, size=len(rows),
                                    more=len(hub.get("Metadata") or []) > len(rows)
                                    or bool(hub.get("more"))))
            detail["Related"] = {"Hub": trimmed} if trimmed else {}
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
        `viewMode` and, for an episode list, the whole `grandparent*` set. No
        `theme`: plexsim has no theme audio, and a real show without one carries no
        `theme`, `parentTheme` or `grandparentTheme` anywhere (measured on fun) --
        advertising a URL that 404s made every page with theme music on ask for it. The desktop client rendered an empty season list
        while these were missing, even though the rows themselves were correct.
        """
        section = self.catalog.section(parent.get("librarySectionID")) or {}
        is_episode_list = ((bool(rows) and rows[0].get("type") == "episode")
                           or (leaf == "allLeaves" and parent.get("type") in ("season", "episode")))
        # Measured on kids (1.43.4) and zen (1.42.2), 20 rows across five shows: a
        # SHOW's `/allLeaves` -- and only that list -- gives every episode row the
        # SHOW's `studio`. The episode's own detail row, the season's `/children` and
        # the SECTION's allLeaves all answer none on both servers, so it is a property
        # of this list rather than of the row, and it is stamped on the projection
        # rather than written into the catalog.
        carry_studio = (parent.get("studio")
                        if leaf == "allLeaves" and parent.get("type") == "show" else None)

        fields = {
            # Measured on both servers: a child row is the light projection -- text
            # tags, no Stream list -- and leaves the librarySection fields to the
            # container.
            "Metadata": [_grid_row(r, keep_section=False, keep_guid=True)
                         for r in self._guid_gate([_full(r) for r in rows], query)],
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
            # Measured on both servers: every child list says it can sync -- a show's
            # seasons, a season's episodes, allLeaves -- where plexsim said it could not.
            "allowSync": True,
        }

        if parent.get("type") == "season":
            # An episode list is presented as the SHOW's, with the season as
            # the parent -- hence grandparent* on the container. Measured on both
            # servers: `title1` is the SHOW's title, not the library's;
            # `grandparentRatingKey` is a number; and there is no `grandparentYear`.
            # `parentYear` is the SEASON's own year when it has one -- re-measured in
            # cycle 8 on both servers; the first reading was taken on seasons without a
            # year. The same trap twice more, measured on a writable copy in cycle 9:
            # `parentTitle` is "" only while the season keeps its generated `Season N`
            # title, and `summary` is absent only while the season has none.
            show_key = parent.get("parentRatingKey")
            show = self.catalog.items.get(int(show_key)) if show_key else None
            if not parent.get("summary"):
                fields.pop("summary", None)
            fields.update(
                {
                    "title1": show.get("title") if show else parent.get("parentTitle"),
                    "title2": parent.get("title"),
                    "parentIndex": parent.get("index"),
                    "parentTitle": ("" if parent.get("title") in ("", "Season %s" % parent.get("index"))
                                    else parent.get("title")),
                    "parentYear": parent.get("year"),
                    "thumb": parent.get("thumb"),
                    "art": parent.get("art"),
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
                }
            )
        if leaf == "allLeaves" and parent.get("type") == "season":
            # Measured on both servers: a season's (empty) allLeaves names the show only
            # by title -- none of the grandparent key, thumb, studio or rating its
            # /children carries.
            for name in ("grandparentRatingKey", "grandparentThumb", "grandparentStudio",
                         "grandparentContentRating"):
                fields.pop(name, None)
        if leaf == "allLeaves" and parent.get("type") == "episode":
            # Measured on both servers: an episode's allLeaves is presented as its
            # SEASON's -- title1 the season, title2 the episode, an empty
            # grandparentTitle, and the episode layout -- where plexsim sent the library
            # title and the season layout.
            fields.update(title1=parent.get("parentTitle"), grandparentTitle="")
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
            # Measured: a PAGED row is the same light projection as an unpaged one.
            # plexsim served the full row here, so every row of the page the client
            # actually asks for carried the librarySection fields a child row never
            # does.
            fields["Metadata"] = [_grid_row(r, keep_section=False, keep_guid=True)
                                  for r in self._guid_gate([_full(r) for r in page], query)]
            fields["offset"] = start
            fields["totalSize"] = total
        excluded = [e for e in ((query or {}).get("excludeElements") or "").split(",") if e]
        if "Media" in excluded:
            # Measured on both servers: `excludeElements=Media` on a child list gives
            # every row the two counts the detail read gets -- `mediaCount` 0 on a
            # season, which has no Media of its own, and `mediaCountOptimized` 0.
            fields["Metadata"] = [dict({k: v for k, v in r.items() if k != "Media"},
                                       mediaCount=len(r.get("Media") or []),
                                       mediaCountOptimized=0)
                                  for r in fields["Metadata"]]
        if carry_studio:
            fields["Metadata"] = [dict(r, studio=carry_studio)
                                  for r in fields.get("Metadata") or []]
        fields.update(extra)
        if extra.get("Directory"):
            # Measured: the container's `size` counts the "All episodes" Directory
            # row as well as the seasons, which is what `excludeAllLeaves=1` takes
            # back off it; `totalSize` does not. In XML the row comes FIRST, before
            # the seasons, and plexsim sent it last.
            fields["size"] = len(fields.get("Metadata") or []) + len(extra["Directory"])
            fields = dict([("Directory", fields.pop("Directory"))] + list(fields.items()))

        return self.container(**{k: v for k, v in fields.items() if v is not None})

    def grandchildren(self, row, query):
        """`/library/metadata/{id}/grandchildren` -- the items two levels down.

        Measured on both servers: a show's are its episodes, a season's its
        episodes, an episode's itself, and a movie's an empty 200. The client builds
        this path itself -- a hub row with `skipChildren` has its `/children` key
        rewritten to `/grandchildren` -- and plexsim 404'd it. The container is not
        the allLeaves one: no parent identity, no librarySection fields and no
        allowSync, just the library's title and art and a `viewGroup` naming the
        LIBRARY's type, so an episode list says `show`.
        """
        kind = row.get("type")
        if kind == "show":
            rows = self.catalog.leaves(row["ratingKey"])
        elif kind == "season":
            rows = self.catalog.children(row["ratingKey"])
        elif kind == "episode":
            rows = [row]
        else:
            rows = []
        section = self.catalog.section(row.get("librarySectionID")) or {}
        is_show = section.get("type") == "show"
        page, paged, start, total = self.page_window(rows, query)
        fields = {
            "art": "/:/resources/show-fanart.jpg" if is_show else "/:/resources/movie-fanart.jpg",
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": 1655122614,
            "title1": section.get("title"),
            "viewGroup": "show" if is_show else "movie",
            "viewMode": 65592,
            "Metadata": [_grid_row(r, keep_section=False, keep_guid=True)
                         for r in self._guid_gate([_full(r) for r in page], query)],
        }
        if is_show:
            fields["nocache"] = True
        if paged:
            fields.update(offset=start, totalSize=total)
        return self.bare_container(size=len(page), **fields)

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
            "language": section.get("language") or "en-US",
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
        # The platform too: Plex Web sends it in the query string and the native apps as
        # a header, and the one response that depends on it -- iOS types a show's "All
        # episodes" row -- has to see either.
        for name in ("X-Plex-Container-Start", "X-Plex-Container-Size", "X-Plex-Platform"):
            # Cycle 16, measured on kids and zen: when BOTH are sent the
            # HEADER wins, per parameter -- query `Start=0&Size=2` with
            # header `Start=1&Size=1` answers offset 1 size 1, a header
            # naming only the size takes the size from the header and the
            # offset from the query, and a header `Start=abc` beside a
            # valid query is a 500. plexsim only read a header the query
            # had not named, so a client sending both got the query's page.
            if headers.get(name) is not None:
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
            result = self._route(method, route, segments, query, body, headers, address)
            if isinstance(result, dict) and query.get("excludeFields"):
                result = _exclude_fields(result, query["excludeFields"])
            product = query.get("X-Plex-Product") or (headers or {}).get("X-Plex-Product")
            if isinstance(result, dict) and product in mediainfo_module.CODEC_TITLE_PRODUCTS:
                result = _with_client_titles(result, product)
            return result
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
        if route == "/:/prefs/get" and method in ("GET", "HEAD"):
            # One setting by id -- how Home reads MergedRecentlyAdded. Measured on both
            # servers: a one-row container whose Setting has an EMPTY label and summary
            # and no enumValues; no `id` is a 400, and an unknown, empty or
            # comma-joined id a 404. plexsim 404'd the path.
            if "id" not in query:
                return ("error", 400, "plexsim: bad request")
            setting = next((s for s in self.server_prefs() if s.get("id") == query["id"]),
                           None)
            if setting is None:
                return ("error", 404, "plexsim: not found")
            setting = {k: v for k, v in setting.items() if k != "enumValues"}
            setting.update(label="", summary="")
            return self.bare_container(size=1, Setting=[setting])
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
        if segments[:2] == ["system", "agents"] and len(segments) == 3:
            # Measured on both servers: one agent by identifier -- a real one or
            # `nosuch.agent` -- is a 404 with an empty body. plexsim answered the whole
            # agent list to it, which reads as "this identifier names six agents".
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
        if segments[:1] == ["devices"]:
            if method == "DELETE":
                return ("empty", 200)
            if method == "POST" and len(segments) == 1:
                # Measured: the legacy plug-in dispatcher answers this one.
                return self.bare_container(size=0, content="plugins")
            if method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            if len(segments) == 2:
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
            if segments[3].isdigit() and method in ("GET", "HEAD"):
                # Measured on both servers: one history row by its key -- the same row
                # history/all lists, alone in a container carrying only `size`. plexsim
                # listed `/status/sessions/history/1` in history/all and 404'd that key.
                wanted = "/status/sessions/history/%s" % int(segments[3])
                found = [self._history_row(r) for r in self.library.history()
                         if r.get("historyKey") == wanted]
                if found:
                    return self.bare_container(size=1, Metadata=found[:1])
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
        if route == "/channels/recentlyViewed":
            # Measured on both servers: the plug-in dispatcher's empty answer, the
            # same as /channels/all. plexsim 404'd it.
            return self.bare_container(size=0, content="plugins")
        if route == "/neighborhood/devices" and method in ("GET", "HEAD"):
            # The DLNA devices this server can see. Measured: an empty container on
            # the reference, and fun lists the renderers on its LAN. plexsim sees
            # none, and 404'd the Settings page's read.
            return self.bare_container(size=0)
        if route == "/services/defaultLibraries" and method in ("GET", "HEAD"):
            # Measured on both servers: a 400 with and without a language, which the
            # first-run setup reads as "no suggested libraries".
            return ("error", 400, "plexsim: bad request")
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
            if segments[1] == "subscriptions" and segments[2] == "template" \
                    and len(segments) == 3 and method in ("GET", "HEAD"):
                # Measured on both servers: a 400 for any guid, where plexsim 404'd.
                return ("error", 400, "plexsim: bad request")
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
        if route == "/livetv/epg/channels":
            # Measured on both servers with no tuner: a 400, like the lineup reads.
            return ("error", 400, "plexsim: bad request")
        if (route in ("/livetv/epg", "/livetv/epg/guide", "/livetv/epg/search",
                      "/livetv/recordings")
                or (segments[:2] in (["livetv", "dvrs"], ["livetv", "sessions"])
                    and len(segments) > 2)):
            # Measured on both servers: the guide, EPG search, recordings, a named DVR
            # and a named live session (and everything under them) are 404s. plexsim's
            # blanket empty container told a client each of these existed and was
            # empty; the two collection roots below are the real empty ones.
            return ("error", 404, "plexsim: not found")
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
        if route == "/library/sections/all" and method in ("GET", "HEAD"):
            # Measured on both servers: the same document as /library/sections, row for
            # row. The community OpenAPI description names this path `getSections`, so
            # every SDK generated from it asks here -- and plexsim 404'd it.
            route = "/library/sections"
        if route == "/library/sections":
            if method in ("PUT", "DELETE"):
                # Measured: 404 for both, where a POST with a bad form is a 400.
                return ("error", 404, "plexsim: not found")
            if method == "POST":
                return self.create_section(query)
            rows = [self.section_directory(s) for s in catalog.sections]
            if query.get("includePreferences") == "1":
                # Measured on both servers: every row gains its library's settings,
                # the same list /library/sections/{id}/prefs answers -- except that a
                # show library's `episodeSort` is the per-SHOW variant, with "Library
                # default" in its enum. The setup flow reads `Preferences.Setting` off
                # every row to find hidden libraries, and plexsim ignored the flag, so
                # that read threw and the flow treated no library as hidden.
                for section, row in zip(catalog.sections, rows):
                    settings = self.section_settings(section["key"], section)
                    if section.get("type") == "show":
                        own = {s.get("id"): s for s in ITEM_PREFS.get("show") or []}
                        settings = [dict(s, summary=own[s["id"]].get("summary"),
                                         enumValues=own[s["id"]].get("enumValues"))
                                    if s.get("id") == "episodeSort" and "episodeSort" in own
                                    else s for s in settings]
                    row["Preferences"] = {"Setting": settings}
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
            # Measured on the copy, frame for frame: Optimize Database runs a
            # `general.db.optimize` activity -- started at 0 with an EMPTY subtitle, then
            # updated at 0, 10, 40 and 60 with the subtitle `Optimizing Database`, then
            # ended at 100. plexsim answered the empty 200 and pushed nothing, so the
            # Settings page's progress bar never moved and the Activity pane stayed blank.
            self.notify_activity_run("general.db.optimize", "Optimizing database",
                                     [(0, "Optimizing Database"), (10, "Optimizing Database"),
                                      (40, "Optimizing Database"), (60, "Optimizing Database")])
            logger.info("OPTIMIZE database -- plexsim has none, the activity is the answer")
            return ("empty", 200)
        if route == "/library/clean/bundles":
            if method != "PUT":
                return ("error", 404, "plexsim: not found")
            # Measured on a writable copy of the reference, frame for frame. plexsim has
            # no bundles, so both counts are 0.
            self.notify_activity_run("general.bundle.clean", "Cleaning bundles", [
                (0, "Analyzing metadata bundles"), (25, "Deleting 0 metadata bundles"),
                (50, "Deleting 0 metadata bundles"), (50, "Analyzing media bundles"),
                (75, "Deleting 0 media bundles"), (100, "Deleting 0 media bundles")])
            return ("empty", 200)

        if route == "/library/tags":
            return self.library_tags(query)
        if route == "/library/randomArtwork" and method in ("GET", "HEAD"):
            return self.random_artwork(query)
        if route == "/library/matches":
            return self.library_matches(query)
        if route == "/library/people":
            return ("error", 404, "plexsim: not found")
        if (segments[:2] == ["library", "people"] and method in ("GET", "HEAD")
                and (len(segments) == 3 or (len(segments) == 4 and segments[3] == "media"))):
            return self.person(segments[2], len(segments) == 4, query)

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
                if segments[2:] == ["all", "refresh"]:
                    return self.library_section(segments, query, method)
                # Measured: any other non-numeric section id -- `abc`, `-1`, `1.5` --
                # is a 404, not the 400 plexsim answered.
                return ("error", 404, "plexsim: not found")
            return self.library_section(segments, query, method)

        if segments[:2] == ["library", "metadata"] and len(segments) >= 3:
            return self.library_metadata(segments, query, method, body)

        if route == "/library/onDeck":
            # Measured: this is the server-wide Continue Watching list -- every row
            # carries a `viewOffset` and a `lastViewedAt`, and it is EMPTY on a
            # server with nothing in progress. plexsim answered the first twelve
            # catalog rows with no view state at all, which is what every non-web
            # client's On Deck reads (plexapi, Kodi/PKC, the legacy apps).
            rows = []
            for row in self.continue_watching():
                # Measured on fun, the reference having nothing in progress: the light
                # projection, naming its library by id, title and UUID and not by key.
                entry = _grid_row(_listing_row(row))
                entry.pop("librarySectionKey", None)
                entry["librarySectionUUID"] = (catalog.section(row.get("librarySectionID"))
                                               or {}).get("uuid")
                rows.append(entry)
            # Measured on both lists: `mixedParents`, because the rows come from
            # more than one library -- and on both servers both lists PAGE, with the
            # headers or the query parameters, where plexsim answered every row.
            page, paged, start, total = self.page_window(rows, query)
            fields = {"offset": start, "totalSize": total} if paged else {}
            return self.container(Metadata=page, mixedParents=True, **fields)
        if route == "/library/recentlyAdded":
            # Measured on both servers: a new episode is listed as its SEASON, never on
            # its own, and the season row's `addedAt` is its EARLIEST episode's -- the
            # reference's Season 1 says 1789187076, its first episode's, where the
            # season's own row says 1789187257. plexsim listed each episode beside its
            # season, dated by the season. The list stops at 50 (fun's totalSize) and
            # pages.
            seasons = {}
            for row in catalog.items.values():
                parent_key = str(row.get("parentRatingKey") or "")
                if row.get("type") == "episode" and parent_key.isdigit() \
                        and int(parent_key) in catalog.items:
                    added = row.get("addedAt") or 0
                    seasons[int(parent_key)] = min(seasons.get(int(parent_key), added), added)
            dated = [(r.get("addedAt") or 0, r) for r in catalog.items.values()
                     if r.get("type") == "movie"]
            dated += [(added, catalog.items[key]) for key, added in seasons.items()]
            dated.sort(key=lambda pair: (-pair[0], str(pair[1].get("titleSort")
                                                       or pair[1].get("title") or "").lower()))
            # Measured on both servers: these rows are the light projection (text
            # tags, no Guid, no Rating), and each names its section by ID, title and
            # UUID -- not by key -- and carries `allowSync`.
            rows = []
            for added, row in dated[:50]:
                entry = _grid_row(_listing_row(row))
                if row.get("type") == "season":
                    # Measured on both servers: a SEASON row here -- and nowhere else on
                    # the surface -- carries `parentSummary`, the show's own summary. An
                    # empty one is omitted like every other empty string (4 zen rows
                    # whose show has none carry no field at all). The client prints it
                    # under the season tile, where plexsim left it blank.
                    parent = self.catalog.items.get(int(row["parentRatingKey"])) \
                        if str(row.get("parentRatingKey") or "").isdigit() else None
                    if parent and parent.get("summary"):
                        entry["parentSummary"] = parent["summary"]
                entry.pop("librarySectionKey", None)
                entry["librarySectionUUID"] = (catalog.section(row.get("librarySectionID"))
                                               or {}).get("uuid")
                entry["allowSync"] = False
                entry["addedAt"] = added
                rows.append(entry)
            page, paged, start, total = self.page_window(rows, query)
            fields = {"offset": start, "totalSize": total} if paged else {}
            return self.container(Metadata=page, mixedParents=True, **fields)

        if route == "/library/all" and method not in ("GET", "HEAD"):
            # Measured: `/library/all` is read-only -- a PUT there is a 404, where
            # plexsim answered the listing to what looks like an edit.
            return ("error", 404, "plexsim: not found")
        if route == "/library/all":
            # Measured: a real query endpoint, not a dump. `type=` selects the rows,
            # filters and `sort=` apply as on a section, the default order is the
            # ratingKey, and a row carries its Media only when a `type` was given.
            # plexsim ignored every parameter and answered all its rows every time,
            # which is what python-plexapi's `library.search(libtype=)` read.
            # Measured on both servers: `/library/all` refuses an unparseable
            # `type=` with 400 where a section listing answers 500, and both answer
            # 500 to an empty one.
            self._parsed_type(query, status=400)
            if str(query.get("type")) == "18":
                # Cycle 14, measured on the copy: `/library/all?type=18` lists every
                # collection the SERVER holds, across libraries, in title order -- the
                # same rows the section form answers, each carrying its tag id as
                # `index` and its own `librarySectionID`. plexsim answered `size 0`,
                # because its collections are not rows in `catalog.items` and this
                # handler only ever looked there.
                rows = []
                for section in catalog.sections:
                    rows += self.library.collections(section["key"])
                rows, _ = self._sorted(rows, query.get("sort") or "titleSort")
                page, paged, start, total = self.page_window(rows, query)
                fields = {"offset": start, "totalSize": total} if paged else {}
                return self.container(Metadata=[_listing_row(r) for r in page], **fields)
            source = sorted(catalog.items.values(), key=lambda r: int(r["ratingKey"]))
            kinds = {value for name, value in query.get("_pairs") or [] if name == "type"}
            if kinds:
                wanted = ({"1": "movie", "2": "show", "3": "season", "4": "episode"}
                          .get(next(iter(kinds))) if len(kinds) == 1 else None)
                source = [r for r in source if wanted and r.get("type") == wanted]
            source, _ = self.apply_filters(source, query)
            source = self._guid_prefix(source, query)
            if query.get("sort"):
                source, unsupported = self._sorted(source, query["sort"])
                if unsupported:
                    return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
            rows = []
            for row in self._limited(source, query):
                entry = _grid_row(_listing_row(row), keep_guid=query.get("includeGuids") == "1")
                if not kinds:
                    entry.pop("Media", None)
                rows.append(entry)
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
            # Measured on both servers: `/library/streams/{id}.srt` and `.vtt` -- the
            # sidecar extraction by extension -- are the same 501 as the bare stream for
            # a stream the server has, and a 404 for one it does not. plexsim read the
            # extension as part of the id and 404'd every one.
            if stream_id.endswith((".srt", ".vtt")) and len(segments) == 3:
                stream_id = stream_id.rsplit(".", 1)[0]
            known = stream_id.isdigit() and any(
                str(stream.get("id")) == stream_id
                for row in catalog.items.values() for media in row.get("Media") or []
                for part in media.get("Part") or [] for stream in part.get("Stream") or [])
            if not known:
                # Measured on the copy: a PUT to an id it does not have is a 400, and so
                # is a DELETE of a NON-NUMERIC one -- `DELETE /library/streams/abc` is a
                # 400 where `DELETE /library/streams/999999` is a 404. plexsim answered
                # 404 to both, so a malformed id and a missing one were indistinguishable.
                if len(segments) == 3 and (method == "PUT"
                                           or (method == "DELETE" and not stream_id.isdigit())):
                    return ("error", 400, "plexsim: bad request")
                return ("error", 404, "plexsim: not found")
            if segments[3:] in (["levels"], ["loudness"]):
                # Measured on both servers: the loudness levels of a stream the server
                # has not analysed are a 404, where the stream itself is the 501.
                return ("error", 404, "plexsim: not found")
            if method == "PUT" and len(segments) == 3:
                # Measured on a writable copy of the reference: a PUT to a stream it HAS
                # is 200 with an empty body -- and stores nothing, the track's title is
                # unchanged afterwards, with or without parameters. plexsim answered the
                # 501 the GET answers, so python-plexapi's stream edit read a refusal
                # where a real PMS reads an acceptance.
                return ("empty", 200)
            if method == "POST" and len(segments) == 3:
                # Measured on the copy: a POST to a stream it HAS is a 404 -- there is no
                # such verb on the path -- where plexsim answered its 501.
                return ("error", 404, "plexsim: not found")
            if method == "DELETE" and len(segments) == 3:
                # And a DELETE removes an uploaded SIDECAR (200) while refusing an
                # embedded track with 403 -- it is part of the file. plexsim 501'd both.
                if int(stream_id) not in self.uploaded_subtitles:
                    return ("error", 403, "plexsim: the stream is embedded in the part")
                self.library.delete_stream(int(stream_id))
                self.uploaded_subtitles.pop(int(stream_id), None)
                return ("empty", 200)
            return ("error", 501, "plexsim: streams are embedded in the part")

        if segments[:2] == ["library", "parts"] and method in ("PUT", "POST"):
            # Audio/subtitle track selection from the player or the Get Info
            # dialog: PUT /library/parts/{partID}?subtitleStreamID=&allParts=1.
            # Measured on a writable copy of the reference: the same endpoint answers a
            # POST identically, where plexsim 404'd it.
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
        if segments[:2] == ["hubs", "metadata"] and len(segments) == 3:
            return self.item_music_hubs(segments[2])
        if segments[:2] == ["hubs", "metadata"] and len(segments) == 4:
            return self.item_hubs(segments[2], segments[3], query)
        if route == "/hubs/items":
            return self.hub_items(query)
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
                # Measured on fun: the light projection, like the shelf it expands.
                rows = [_grid_row(_listing_row(r)) for r in self.continue_watching()]
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
                meta = (HOME_RECENT_META.get(str(query.get("type") or ""))
                        if query.get("includeMeta") == "1" else None)
                if rows is None:
                    answer = self.hub_container(size=0)
                    if meta:
                        answer["MediaContainer"]["Meta"] = meta
                    return answer
                # Measured: a movie or episode row here is the light projection
                # (no Guid, text-only tags); a show row is the full one.
                page = [_listing_row(r) if r.get("type") == "show"
                        else _grid_row(_listing_row(r)) for r in rows[:self.HUB_COUNT]]
                answer = self.hub_container(size=len(page), Metadata=page)
                if meta:
                    # Cycle 15, measured on both references: one `Type` row whose
                    # `active` flips with the type -- true for `movie` at type=1,
                    # false for `mixed` at type=2 -- and NO block at all for any
                    # other type.
                    answer["MediaContainer"]["Meta"] = meta
                return answer
            # Measured: a home hub a real PMS does not have is a 404, not the
            # empty container plexsim answered -- which a client reads as "this
            # shelf exists and is empty".
            return ("error", 404, "plexsim: not found")
        if (segments[:2] == ["hubs", "sections"] and len(segments) == 4
                and segments[3] == "manage"):
            # Manage Recommendations: which of the library's hubs exist and where
            # each is promoted. Measured: a bare container of `Hub` rows, eight for a
            # movie library and nine for a show library, the same on both servers.
            # plexsim 404'd it, which python-plexapi's `managedHubs()` raises on.
            section = catalog.section(segments[2])
            if section is None or method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            if "metadataItemId" in query:
                # Measured on both servers: naming an item -- even with an empty id --
                # is a 400 on a read.
                return ("error", 400, "plexsim: bad request")
            kind = "show" if section.get("type") == "show" else "movie"
            rows = [{"identifier": identifier, "title": title,
                     "recommendationsVisibility": "all",
                     "homeVisibility": "all" if home else "none",
                     "promotedToRecommended": True, "promotedToOwnHome": home,
                     "promotedToSharedHome": home}
                    for identifier, title, home in MANAGED_HUBS[kind]]
            return self.bare_container(size=len(rows), Hub=rows)
        if segments[:2] == ["hubs", "sections"] and len(segments) == 4 and segments[3] == "extras":
            # Measured on both servers: an empty hub list -- `size 0`, `allowSync` and the
            # identifier, nothing else -- for a movie library, a show library and a section id
            # the server does not have alike. plexsim 404'd it.
            if method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            return self.bare_container(size=0, allowSync=True,
                                       identifier="com.plexapp.plugins.library")
        if segments[:2] == ["hubs", "sections"] and len(segments) == 3:
            return self.section_hubs(segments[2], query.get("count"),
                                     query.get("includeLibraryPlaylists") == "1")
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
            return self.create_play_queue(query, headers)
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
            return self.remove_from_continue_watching(query, method)

        # -- live TV / DVR / sync: plexsim has no tuner and no encoder ----
        if segments[:1] == ["media"] and segments[1:2] in (["subscriptions"], ["grabbers"]):
            return self.bare_container(size=0)
        if segments[:1] == ["sync"]:
            return ("error", 404, "plexsim: no sync")

        if len(segments) == 1:
            # Measured on both reference servers: an unknown SINGLE-segment path
            # falls into the legacy plug-in dispatcher and answers a 200 with
            # `content="plugins"`, where a multi-segment one 404s. `/prefs`,
            # `/settings`, `/login` and `/logout` -- all four named in the client
            # bundle -- are 200 there and were 404 here.
            # Cycle 15, measured on the copy: it answers EVERY method, not just
            # GET -- `PUT /prefs`, `POST /setting`, `DELETE /users` and `/devices.xml`
            # are all 200 with `content="plugins"`. plexsim served the GET alone.
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
                    "language": section.get("language") or "en-US",
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
        if section_id == "all" and list(segments[3:]) == ["refresh"] and method in ("GET", "POST"):
            # Measured on a writable copy of the reference: this scans EVERY library, one
            # after another. plexsim 404'd it as an unknown section.
            for directory in list(self.catalog.sections):
                self.library_section(["library", "sections", str(directory["key"]), "refresh"],
                                     query, method)
            return ("empty", 200)
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
                # Measured on a writable copy of the reference, frame for frame.
                self.notify_activity_run("library.delete", "Deleting Library", [
                    (0, "Deleting section %s" % section_id), (50, "Optimizing Database"),
                    (0, "Optimizing Database"), (10, "Optimizing Database"),
                    (40, "Optimizing Database"), (60, "Optimizing Database")])
                logger.info("SECTION delete %s (%s)", section_id, section.get("title"))
                return ("empty", 200)
            if method == "PUT":
                # The Edit Library dialog sends the WHOLE form, and a real PMS
                # validates it: measured 400 for a `name` alone, for a form with no
                # `agent` or no `scanner`, and for an unknown agent, scanner or
                # language. plexsim accepted every one of those with a 200, so a
                # typo in the dialog looked like it had been saved.
                # Re-measured on the copy, correcting the reading above: what an edit
                # needs is an `agent`, not a `name` and not a `scanner`. The client's own
                # library-UPGRADE form sends `agent` and `scanner` and nothing else, and
                # `agent` alone is a 200 too; `name` alone, `scanner` alone, `language`
                # alone and an empty form are all 400. plexsim demanded `name`, so the
                # upgrade form -- the one whose failure the bundle reports as "There was
                # a problem upgrading the library" -- was refused every time.
                if not query.get("agent"):
                    return ("error", 400, "plexsim: bad request")
                if query["agent"] not in KNOWN_AGENTS:
                    return ("error", 400, "plexsim: bad request")
                if query.get("scanner") and query["scanner"] not in KNOWN_SCANNERS:
                    return ("error", 400, "plexsim: bad request")
                if query.get("language") and query["language"] not in KNOWN_LANGUAGES:
                    return ("error", 400, "plexsim: bad request")
                if query.get("name"):
                    section["title"] = query["name"]
                # Measured on a writable copy of the reference: the edit APPLIES the rest
                # of the form -- `language=de-DE` reads back on the section and
                # `prefs[enableBIFGeneration]=0` in its settings -- and runs a scan.
                # plexsim renamed the library and dropped the rest.
                if query.get("language"):
                    section["language"] = query["language"]
                prefs = {match.group(1): value for name, value in query.items()
                         for match in [re.match(r"^prefs\[(\w+)\]$", name)] if match}
                if prefs:
                    self.library_section(list(segments) + ["prefs"], prefs, "PUT")
                self.notify_section_scan(section_id, section)
                logger.info("SECTION edit %s -> %s", section_id, section.get("title"))
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

        # Cycle 16, swept across kids and zen, both library types: a leaf's EXTRA path
        # segment is never the leaf again. On a leaf with no bucket it is a flat 404
        # -- /all/zzz, /onDeck/zzz, /sorts/zzz, /studio/<a real studio key>,
        # /audioLanguage/en, /location/1 and fourteen more -- and on a leaf WITH one a
        # second extra segment (`/genre/24/more`) is a 404 too. plexsim routed on
        # `segments[3]` and dropped the rest of the path, so all of them answered the
        # leaf: the A-Z rail, the genre list and the sort list came back where the
        # client had asked for a page of films, which is fix 4's trap one level down --
        # a 200 reads as "this is the thing I asked for".
        #
        # `query/parse`, `indexes/thumbnails`, `extras/all` (cycle 10) and the
        # VERSIONED `composite/{v}` are real two-segment leaves, and `firstCharacter`
        # has its own branch below; each of those 404s its own unknown second segment. A trailing slash is already
        # stripped, so `/year/` is the axis and not an empty bucket -- measured.
        if len(segments) > 4 and leaf not in ("query", "indexes", "firstCharacter",
                                             "extras", "composite"):
            if len(segments) > 5:
                return ("error", 404, "plexsim: not found")
            return self.axis_bucket(section_id, section, leaf, segments[4], query)

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
            rows = self.catalog.section_items(section_id)
            if query.get("force") == "1":
                # Measured on the copy: "Refresh All Metadata" runs the WHOLE scan
                # sequence first -- the same `library.update.section` activity, its
                # per-item progress, the subscriptions pair and the completion status --
                # and only then refreshes each item. plexsim pushed a bare status frame
                # and went straight to the per-item half, so the Libraries page showed no
                # scan and no progress for the one operation that takes longest.
                self.notify_section_scan(section_id, section)
                for row in rows:
                    self.library.refresh(row)
                    self.notify_refreshed(row)
            else:
                # Measured on a writable copy of the reference: a scan is ONE
                # `library.update.section` activity stepping through the items.
                self.notify_section_scan(section_id, section)
            self.refreshing.discard(str(section_id))
            logger.info("SECTION refresh %s force=%s -> %d row(s)",
                        section_id, query.get("force") == "1", len(rows))
            return ("empty", 200)
        if leaf in ("emptyTrash", "analyze"):
            if method != "PUT":
                # Measured: a real PMS 404s this rather than answering 405, which is
                # a status it never sends.
                return ("error", 404, "plexsim: not found")
            if leaf == "analyze":
                # Measured on a writable copy of the reference: an analyze pushes, for
                # each movie in title order, a state-5 frame with `mediaState:
                # analyzing`, one with `thumbnailing`, and a plain state 5. plexsim
                # pushed nothing.
                movies = [r for r in self.catalog.section_items(section_id) if r.get("type") == "movie"]
                for row in sorted(movies, key=self._sort_key("titleSort")):
                    for media_state in ("analyzing", "thumbnailing", None):
                        self.notify_timeline(row, self.TIMELINE_DONE, None, media_state=media_state)
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
                    if name in SHOW_ROW_PREFS:
                        # These two change the ROWS, not just the setting list,
                        # and a show whose own value is `-1` inherits them.
                        self.catalog.set_section_show_pref(section_id, name, value)
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
            settings = self.section_settings(section_id, section, settings)
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
                for name, _ in query.get("_pairs") or []:
                    if name.startswith(("X-Plex-", "include", "exclude")) \
                            or name in ("type", "id") or ".tag.tag" in name \
                            or name.endswith((".value", ".locked")):
                        continue
                    # Measured: a TYPE-SCOPED filter (`show.title=`, `movie.year>>=`,
                    # even the asked type's own) is a 400 on this path, where an
                    # unscoped one scopes the edit. plexsim applied it and edited.
                    try:
                        field, _ = filters_module.split_key(name)
                    except filters_module.FilterError as refused:
                        return ("error", refused.status, "plexsim: %s" % refused.detail)
                    if filters_module.split_scope(field)[0]:
                        return ("error", 400, "plexsim: bad request")
                if str(query.get("type")) == "18":
                    # Measured on a writable copy of the reference: `type=18` edits the
                    # COLLECTIONS `id=` names. plexsim found no row of that type and
                    # edited nothing.
                    edits = {k: v for k, v in query.items() if k not in ("type", "id")}
                    edits["_pairs"] = [(k, v) for k, v in query.get("_pairs") or []
                                       if k not in ("type", "id")]
                    for key in (query.get("id") or "").split(","):
                        if key.strip().isdigit() and self.library.collection(key.strip()):
                            # Cycle 15, measured on the copy: the SECTION path renames
                            # the members' `Collection` TAG as well, where the item
                            # path (cycle 9) leaves it alone. The Edit Collection
                            # dialog saves through here, so on plexsim every member's
                            # page kept the old name for ever.
                            self.library.rename_collection_tag(
                                self.library.collection(key.strip()),
                                edits.get("title.value", edits.get("title")))
                            self.library_list_item(int(key), ["library", "metadata", key.strip()],
                                                   edits, "PUT")
                    return ("empty", 200)
                wanted = {"1": "movie", "2": "show", "3": "season",
                          "4": "episode"}.get(str(query.get("type")))
                keys = [int(k) for k in (query.get("id") or "").split(",")
                        if k.strip().isdigit()]
                if keys:
                    # Measured: `id=` names rows of the asked TYPE in THIS library. A
                    # movie id under `type=2`, or on another library's path, is edited
                    # nowhere -- still a 200 -- and `id=<movie>,<show>` with `type=1`
                    # edits only the movie. plexsim edited every id it had.
                    rows = [self.catalog.items[k] for k in keys
                            if k in self.catalog.items
                            and self.catalog.items[k].get("type") == wanted
                            and str(self.catalog.items[k].get("librarySectionID"))
                            == str(section_id)]
                else:
                    rows = self.typed_rows(section_id, query)
                    rows, _ = self.apply_filters(rows, query, section)
                return self.apply_edit(rows, query, section=True,
                                       metadata_state="queued")
            return self.section_listing(section_id, section, query)
        if leaf == "timeline":
            return self.section_timeline_container(section_id, section)
        if leaf == "common":
            if method != "GET":
                return ("error", 404, "plexsim: not found")
            return self.common_fields(section_id, section, query)
        if leaf in self.SUBSET_TITLES:
            return self.section_subset(section_id, section, leaf, query)
        if leaf == "autocomplete":
            return self.autocomplete(section_id, section, query)
        if leaf in ("allLeaves", "albums", "cluster", "moment", "computePath", "nearest"):
            return self.section_leaf(section_id, section, leaf, query)
        if leaf == "arts":
            return self.section_arts(section_id, section)
        if leaf == "extras":
            return self.section_extras(section, segments[4:])
        if leaf == "collections":
            # Measured: this listing keeps the SECTION's viewGroup and title2
            # ("movie" / "All Movies"), not "collection" -- the rows are
            # collections but the view is still the library's.
            rows = self.library.collections(section_id)
            # Measured on both servers: a SHOW library's list carries `nocache`, paged
            # or not, as its other listings do; a movie library's does not.
            extra = {"nocache": True} if section.get("type") == "show" else {}
            return self.listing_container(section_id, section, rows, query=query, **extra)
        if leaf == "folder":
            return self.folder_view(section_id, section, query)

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
            # Re-measured in cycle 9, which corrects how this was keyed: like /filters,
            # the list belongs to the `?type=` asked for, not to the library. A movie
            # library answers the 7 episode sorts to `type=4` and the artist sorts to
            # `type=8`; a show library the 9 movie sorts to `type=1`; types 9, 10, 13, 18
            # and 20 have lists of their own, every other number is empty and a
            # non-number a 400. plexsim read the library's own Meta, so a movie library
            # had no sorts for any type but its own and answered the movie list to
            # `type=8`. The lists are `section_sorts.json`, captured from the reference.
            raw = query.get("type")
            if raw in (None, ""):
                wanted = "2" if section.get("type") == "show" else "1"
            elif raw.lstrip("-").isdigit():
                wanted = str(int(raw))
            else:
                return ("error", 400, "plexsim: bad request")
            variant = "advanced" if query.get("includeAdvanced") == "1" else "plain"
            sorts = json.loads(json.dumps(SECTION_SORTS[variant].get(wanted) or [])
                               .replace("{section}", str(section_id)))
            return self.secondary_container(section_id, section, None,
                                            Directory=sorts)
        if leaf == "filters":
            raw = query.get("type")
            if raw in (None, ""):
                wanted = "2" if section.get("type") == "show" else "1"
            elif raw.lstrip("-").isdigit():
                wanted = str(int(raw))
            else:
                return ("error", 400, "plexsim: bad request")
            entries = list(SECTION_FILTERS["plain"].get(wanted) or [])
            if query.get("includeAdvanced") == "1":
                entries += SECTION_FILTERS["advanced"].get(wanted) or []
            filters = json.loads(json.dumps(entries).replace("{section}", str(section_id)))
            return self.secondary_container(section_id, section, None,
                                            Directory=filters)
        if leaf == "firstCharacter" and len(segments) == 5:
            # Cycle 16, measured on kids and zen: the bucket behind a letter is a real
            # LISTING, and it is NOT the axis. `title2` reads "Start with '<key>'" with
            # the key as the client sent it (`%23` decodes, `BB` and a lowercase `b`
            # are echoed verbatim and match nothing), the rows are the light grid
            # projection, and the listing grammar applies on top -- `year=2020` on
            # zen's `B` bucket answers 6 of its 161 rows and `sort=titleSort:desc`
            # reverses them. The match is on the ROW's own sort title, which is why a
            # season list's `A` bucket says 64 in the axis and answers 0 rows here.
            #
            # plexsim routed on `segments[3]` alone, so the extra segment was dropped
            # and every bucket answered the AXIS again: clicking a letter in the A-Z
            # rail redrew the rail.
            wanted = urllib.parse.unquote(segments[4])
            self._parsed_type(query, 500)
            rows = [r for r in self.typed_rows(section_id, query)
                    if self._first_character_bucket(r) == wanted]
            rows, _ = self.apply_filters(rows, query, section)
            rows, unsupported = self._sorted(rows, query.get("sort") or "titleSort")
            if unsupported:
                return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
            # `limit=` is parsed but does nothing here, the way it does nothing on
            # /children: zen's `B` bucket answers all 161 rows to `limit=2`.
            extra = {"nocache": True} if section.get("type") == "show" else {}
            return self.listing_container(
                section_id, section, [_listing_row(r) for r in rows], query=query,
                title2="Start with '%s'" % wanted, **extra)
        if leaf == "firstCharacter":
            # Measured on zen's show library: this axis IS scoped by `?type=`, and its
            # counts follow -- 13 `#` and 40 `A` for the shows at type 2, 13 and 64 for
            # the seasons at type 3, 185 and 207 for the episodes at type 4, and empty
            # for a type the library holds no rows of. plexsim counted the whole
            # section whatever the type said, so a show library's letter bar reported
            # show counts on an episode grid.
            self._parsed_type(query, 500)
            counts = {}
            for row in self.typed_rows(section_id, query):
                # Cycle 16, measured on zen's real show library (507 shows, 741
                # seasons, 9287 episodes) and confirmed on kids: the AXIS counts a
                # row below the library's own top-level type by its SHOW's sort
                # title. Bucketing zen's seasons and episodes by their show
                # reproduces the axis on 28 of 28 buckets at type=3 AND at type=4;
                # bucketing them by their own titles reproduces 0 of 28. kids says
                # `F:2` for its two "Season 1"/"Season 2" rows and `F:6` for six
                # episodes with real titles of their own. plexsim counted each row's
                # own title, so a season grid's A-Z rail read "S" and an episode
                # grid's indexed episode titles -- neither of which is what the
                # client's letter bar is for, and neither of which agrees with the
                # rail's own links.
                title = str(self._first_character_basis(row).get("titleSort")
                            or self._first_character_basis(row).get("title") or "")
                first = (title[:1] or "#").upper()
                # Cycle 14, reconstructed from zen's 3887 rows against its own axis:
                # `#` is the DIGITS bucket, not the everything-else one. The 29 rows
                # in `#` are exactly the 29 whose sort title starts with 0-9, and the
                # one row starting with a bracket gets a bucket of its OWN, listed
                # after Z. plexsim folded every non-letter into `#`, so a library of
                # CJK or punctuation-led titles collapsed to one bucket.
                if first.isdigit():
                    first = "#"
                counts[first] = counts.get(first, 0) + 1
            # ...and the bucket KEY is percent-encoded, which `#` needs most of all --
            # raw, it is a URL fragment and the client's request loses everything after
            # it. Corroborated on kids, whose other axes encode the same way
            # (`contentRating` key `Not%20Rated`).
            entries = [{"size": counts[k], "key": urllib.parse.quote(k), "title": k}
                       for k in sorted(counts)]
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
            # Measured on both servers: an axis over the rows refuses a `type=` it
            # cannot parse, exactly as a listing does -- 500 everywhere, and 400 on
            # `year` alone, which is the same split `/library/all` has against a
            # section listing. plexsim answered 200 to all of them.
            refusal = 400 if leaf == "year" else 500
            self._parsed_type(query, refusal, refusal)
            return self.secondary_directory(section_id, section, leaf, query)
        if leaf in SECONDARY_MEDIA:
            # And the MEDIA axes ignore `type=` completely -- the same list for every
            # value including `abc`, on a movie library and a show library alike --
            # so they neither validate it nor scope by it.
            return self.media_axis(section_id, section, leaf)
        if leaf == "location":
            return self.location_axis(section_id, section)
        if leaf == "userRating":
            # Cycle 14: `/userRating` is a real axis and a FIXED one. kids and zen
            # answer the same six rows, on a movie library and a show library alike,
            # whatever ratings the library actually holds -- so it is a scale and not
            # a value list. Like a media axis it carries no `fastKey` and its title is
            # a LABEL; the key is the rating on Plex's 0-10 scale, and `-1` is the
            # "Not rated" bucket. plexsim 404'd it, so the Ratings entry in the filter
            # bar opened nothing.
            return self.secondary_container(
                section_id, section, SECONDARY_TITLES["userRating"],
                Directory=[{"key": key, "title": title} for key, title in USER_RATINGS])
        if leaf in CROSS_TYPE_AXES:
            # ...and the axes belonging to OTHER library types answer an EMPTY
            # secondary container rather than a 404 -- measured on both references, on
            # a movie library: `/mood`, `/style`, `/format`, `/subformat`, `/source`
            # (music) and `/make`, `/model`, `/place`, `/tag` (photo), each with its
            # own title2 and `size` 0. A 404 there tells the client the endpoint does
            # not exist, which is the opposite of what a real PMS says.
            #
            # They are TAG/FIELD axes for the purposes of cycle 13's type rule, not
            # media axes: `?type=abc` and an empty `?type=` are 500s on both
            # references, exactly as `/genre` is, while `?type=2` is a 200. The two
            # axes added beside them split the other way -- see `userRating` above,
            # which ignores `type=` completely the way `/resolution` does.
            self._parsed_type(query, 500, 500)
            return self.secondary_container(section_id, section,
                                            CROSS_TYPE_AXES[leaf], Directory=[])

        if leaf == "unwatched":
            # Measured: `/unwatched` is a LISTING with title2 "Unplayed", not a
            # secondary axis -- the `/filters` entry beside it advertises a boolean
            # the grid applies.
            #
            # `/recentlyReleased` used to be answered here beside it. Cycle 14 found
            # it is a **404** on kids and zen alike, on both library types and with
            # every `type=`, and that it appears nowhere in docs/findings.md, in
            # CLAUDE.md or in either suite: it was never measured, only assumed. A
            # 200 there is fix 4's trap from the inside -- the client reads it as
            # "this endpoint exists and is empty" -- so it falls through to the 404.
            rows = [r for r in self.typed_rows(section_id, query)
                    if not r.get("viewCount")]
            # Cycle 16, measured on kids and zen: like every other container a SHOW
            # library answers, this one carries `nocache`; a movie library's does not.
            # plexsim already had the rule on /all and the five subsets and had missed
            # it here alone.
            extra = {"nocache": True} if section.get("type") == "show" else {}
            return self.listing_container(
                section_id, section, [_listing_row(r) for r in rows],
                title2=SECONDARY_TITLES["unwatched"], **extra)
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
            # Cycle 15, measured on kids and zen: this row is the LIGHT grid
            # projection, like every other listing's -- no `Guid`, no `Rating`, no
            # tag ids and no `Stream`. Both references answer a search row that is
            # identical to their grid row for the same item. plexsim sent the detail
            # row here and only here, so the one listing a user reaches by typing
            # was the fattest response the server had.
            found = self.secondary_container(
                section_id, section, "Search for '%s'" % text,
                Metadata=[_grid_row(_listing_row(r)) for r in rows],
                viewGroup=section.get("type"))
            # Measured: this one is NOT a secondary view -- it carries no
            # `content`, and its viewGroup is the library's type.
            found["MediaContainer"].pop("content", None)
            return found
        if leaf == "composite":
            # Cycle 16, measured on kids and zen: the BARE path is a 404 on both, with
            # and without `?width&height`, on a movie library and a show library alike
            # -- only `/composite/{version}` answers, and no `/library/sections` row
            # carries a `composite` attribute for a client to build the bare form
            # from. plexsim answered image bytes to either, inventing a path the way
            # `/recentlyReleased` was invented.
            if len(segments) < 5:
                return ("error", 404, "plexsim: not found")
            art = self.composite_for("section", section_id)
            if art:
                return ("image", self._at_width(art, query.get("width")))
            return ("png", query)

        # A leaf a real PMS does not have is a 404 there, not an empty container.
        logger.warning("UNKNOWN section leaf %s on section %s", leaf, section_id)
        return ("error", 404, "plexsim: not found")

    def _owns_artwork(self, row, leaf):
        """Does this row OWN the image behind `/library/metadata/{id}/{leaf}`?

        Measured cycle 16 on kids and zen: only the owner answers 200. A row that
        borrows -- every episode's background, and a season's when the show supplies
        it -- carries the OWNER's path in its own `art`/`thumb`, so the stored path's
        ratingKey is the whole test. A row with no artwork at all owns nothing.
        """
        stored = str(row.get("art" if leaf == "art" else "thumb") or "")
        if not stored:
            return False
        parts = stored.split("/")
        if len(parts) > 3 and parts[1] == "library" and parts[2] == "metadata":
            return parts[3] == str(row.get("ratingKey"))
        return True

    # The axes that have a bucket sub-path at all, and how each reads its key.
    # Measured on kids and zen over 29 leaves x 2 library types: `studio`,
    # `videoCodec`, `audioCodec`, `audioLayout`, `audioLanguage`, `subtitleLanguage`
    # and `location` answer 404 even for a key their own axis just listed, so they are
    # deliberately absent here. `collection` and `label` are tag axes by construction
    # but neither reference holds one, so their real-key behaviour is INFERRED from the
    # six tag axes that could be measured (`network` among them, which answers rows).
    AXIS_BUCKETS = {"genre": "tag", "director": "tag", "writer": "tag",
                    "producer": "tag", "actor": "tag", "country": "tag",
                    "network": "tag", "collection": "tag", "label": "tag",
                    "year": "year", "decade": "decade",
                    "contentRating": "contentRating", "resolution": "resolution",
                    "userRating": "userRating"}

    def axis_bucket(self, section_id, section, leaf, key, query):
        """`/library/sections/{id}/{axis}/{key}` -- the rows behind one axis entry.

        Measured cycle 16. A tag axis keys on the TAG ID and 404s an id the library
        does not hold; `year` and `decade` take a non-negative integer and 404 a word
        or a negative number; `contentRating` and `resolution` take anything;
        `userRating` takes a non-negative integer and 404s `-1`, which is the key of
        its own "Not rated" bucket. `title2` is the tag's own title, the bare value
        for year/decade/resolution, `Rated <value>` for contentRating and
        `<n> Star` for userRating -- none of which is the axis's "By ..." heading.
        """
        kind = self.AXIS_BUCKETS.get(leaf)
        if kind is None:
            return ("error", 404, "plexsim: not found")
        wanted = urllib.parse.unquote(key)
        rows = self.typed_rows(section_id, query)
        if kind == "tag":
            column = SECONDARY_TAGS[leaf]
            if leaf == "label":
                rows = list(rows) + list(self.library.collections(section_id))
            title = next((tag.get("tag") for row in rows
                          for tag in row.get(column) or []
                          if str(tag.get("id")) == wanted), None)
            if title is None:
                return ("error", 404, "plexsim: not found")
            rows = [r for r in rows
                    if any(str(t.get("id")) == wanted for t in r.get(column) or [])]
            title2 = title
        elif kind in ("year", "decade", "userRating"):
            if not wanted.isdigit():
                return ("error", 404, "plexsim: not found")
            number = int(wanted)
            if kind == "year":
                rows = [r for r in rows if str(r.get("year") or "") == wanted]
                title2 = wanted
            elif kind == "decade":
                # Measured on zen: `decade/1995` answers 245 rows where `decade/1990`
                # answers 108, so the key opens a ten-year window from itself rather
                # than naming a calendar decade.
                rows = [r for r in rows if r.get("year")
                        and number <= int(r["year"]) < number + 10]
                title2 = wanted
            else:
                rows = [r for r in rows
                        if int(float(r.get("userRating") or 0)) == number]
                title2 = "%s Star" % wanted
        elif kind == "contentRating":
            if wanted == "None":
                # the empty synonym, the same one the axis's own trailing bucket uses
                rows = [r for r in rows if not r.get("contentRating")]
            else:
                rows = [r for r in rows if str(r.get("contentRating") or "") == wanted]
            title2 = "Rated %s" % wanted
        else:
            known = {"4k", "1080", "720", "576", "480", "sd"}
            if wanted in known:
                rows = [r for r in rows
                        if any(str(m.get("videoResolution") or "") == wanted
                               for m in r.get("Media") or [])]
            else:
                # Measured on both references: a value outside the enum is not refused
                # here the way it is as a filter -- it matches every row that has a
                # resolution at all (3867 of zen's 3887).
                rows = [r for r in rows
                        if any(m.get("videoResolution") for m in r.get("Media") or [])]
            title2 = wanted
        rows, _ = self.apply_filters(rows, query, section)
        rows, unsupported = self._sorted(rows, query.get("sort") or "titleSort")
        if unsupported:
            return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
        extra = {"nocache": True} if section.get("type") == "show" else {}
        return self.listing_container(section_id, section,
                                      [_listing_row(r) for r in rows], query=query,
                                      title2=title2, **extra)

    def _first_character_bucket(self, row):
        """The bucket a row falls in when the LISTING filters on it.

        The row's OWN sort title, first character upper-cased, with every digit
        sharing `#` -- measured on kids, where "The Train Job" (titleSort "Train Job")
        answers to `T` and not to `M`, so it really is the sort title. The axis above
        it counts on a different basis; see `_first_character_basis`.
        """
        title = str(row.get("titleSort") or row.get("title") or "")
        first = (title[:1] or "#").upper()
        return "#" if first.isdigit() else first

    def _first_character_basis(self, row):
        """The row whose sort title the `firstCharacter` AXIS counts this row under.

        Measured cycle 16 on zen and kids: a SEASON and an EPISODE are counted under
        their SHOW, not under themselves. The LISTING behind each bucket is the other
        way round -- it filters on the row's own sort title -- so the two halves of
        the same axis genuinely disagree on a real PMS, and a season list's `A`
        bucket says 64 and answers 0 rows.
        """
        if row.get("type") == "season":
            key = row.get("parentRatingKey")
        elif row.get("type") == "episode":
            key = row.get("grandparentRatingKey")
        else:
            return row
        show = self.catalog.items.get(int(key)) if str(key or "").isdigit() else None
        return show or row

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
            if query.get("type"):
                # Measured on both servers: a type NUMBER the library holds no rows
                # of -- 8, 9, 10, 12, 13, 19, 99, -1 -- answers an empty listing, not
                # the library. plexsim fell back to every row in the section, so a
                # client asking a movie library for its tracks was handed its movies.
                return []
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

    @staticmethod
    def _parsed_type(query, status=500, empty_status=None):
        """`type=` has to PARSE as a whole number, or the listing is refused.

        Measured on kids (1.43.4) and zen (1.42.2), identically: a section listing
        answers 500 to `type=abc`, `type=1.5` and an empty `type=`, while `type=-1`
        and `type=99` are 200 with no rows -- an unknown number selects nothing, a
        non-number is a refusal. `/library/all` refuses the same values with 400 and
        keeps 500 for the empty one, which is why the status is a parameter here.
        plexsim answered the whole unfiltered grid to all three.
        """
        raw = query.get("type")
        if raw is None:
            return None
        if str(raw).strip() == "":
            # A listing answers 500 to an empty `type=` whatever its refusal status for
            # a non-number is -- but the `year` AXIS answers 400 to both, measured on
            # kids and zen alike, so the empty case takes its own status here.
            raise filters_module.FilterError(empty_status or 500, "bad type %r" % raw)
        if not str(raw).lstrip("-").isdigit():
            raise filters_module.FilterError(status, "bad type %r" % raw)
        return int(raw)

    def section_listing(self, section_id, section, query):
        """`/library/sections/{id}/all` -- the grid, with the client's filters.

        Ignoring the filter params would make every secondary-filter Directory
        entry (By Genre, By Year, ...) open a list of the whole library, which
        looks like the filter is broken rather than unimplemented.
        """
        if query.get("type") == "18":
            # Plex's collection type. The Collections pivot asks for it here
            # rather than at /library/sections/{id}/collections.
            #
            # Cycle 14, measured on the copy with two collections: this is a real
            # LISTING and takes the grammar -- `type=18&title=aard` answers one row of
            # the two, `id=` one, `sort=addedAt:desc` reorders, `duplicate=1` none --
            # and the default order is `titleSort` like every other listing
            # (`Aardvark` before `C14 Trio`). plexsim answered the whole collection
            # list to every one of them.
            rows = self.library.collections(section_id)
            rows, _ = self.apply_filters(rows, query, section)
            rows, unsupported = self._sorted(rows, query.get("sort") or "titleSort")
            if unsupported:
                return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
            rows = self._limited(rows, query)
            return self.listing_container(section_id, section, rows, query=query)
        self._parsed_type(query)
        source = self.typed_rows(section_id, query)

        source, _ = self.apply_filters(source, query, section)
        source = self._guid_prefix(source, query)
        # Measured on both servers: the DEFAULT order of a listing is `titleSort`
        # ascending (falling back to the title where no sort title is stored), not
        # the catalog's own order -- fun's show grid opens on `【OSHI NO KO】, 13
        # Reasons Why, 30 Rock, 86 EIGHTY-SIX, The 100` because The 100's titleSort
        # is "100". plexsim answered catalog order, so an edited Sort Title never
        # moved a row and the grid's own default disagreed with its sort control.
        # Measured on both references: `parentIndex` is a 500 on a listing rather than a
        # filter, whatever operator key follows it. The operator lives in the KEY, so
        # the field is what is left once the operator characters come off the end.
        refused = next((key for key in query
                        if key.rstrip("<>=!") in REFUSED_FILTER_FIELDS), None)
        if refused:
            return ("error", 500, "plexsim: cannot filter on %s" % refused)
        source, unsupported = self._sorted(source, query.get("sort") or "titleSort")
        if unsupported:
            # Measured: a real PMS answers an unknown sort key with a 500 rather
            # than ignoring it. Silently returning the list unsorted is the worse
            # answer -- the caller cannot tell its sort did nothing.
            logger.warning("UNSUPPORTED sort key %r", unsupported)
            return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
        if "group" in query:
            source = self.grouped(source, query.get("group"))
        source = self._limited(source, query)
        rows = self._excluded_elements([_listing_row(r) for r in source], query)
        if query.get("includeStorage") == "1" and str(query.get("type") or "") in (
                "", str(TYPE_NUMBERS.get(section.get("type") or "movie", 1))):
            # Measured: the pair is on the library's OWN top-level type only -- a show
            # library's `?type=3` and `?type=4` listings carry neither.
            rows = [self._with_storage(row) for row in rows]
        if query.get("type") == "3":
            # Measured on both servers: a season in a LISTING carries no leaf
            # counts, where its /children row does.
            rows = [{k: v for k, v in r.items() if k not in ("leafCount", "viewedLeafCount")}
                    for r in rows]
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

    # The subsets a library's pivot bar opens, and the title2 each one carries.
    SUBSET_TITLES = {"onDeck": "On Deck", "recentlyAdded": "Recently Added",
                     "newest": "Recently Released", "recentlyViewed": "Recently Viewed",
                     "recentlyViewedShows": "Recently Viewed Shows"}

    def section_subset(self, section_id, section, leaf, query):
        """`/library/sections/{id}/{onDeck,recentlyAdded,newest,recentlyViewed,...}`.

        Measured on both servers, and plexsim had all of it wrong: it answered each
        subset as the library's "All" grid of top-level rows, and 404'd
        recentlyViewedShows, which the pivot bar offers.

        - `title2` names the subset; a show library's Recently Viewed is "Recently
          Viewed Episodes".
        - On a SHOW library the first four are EPISODE lists -- viewGroup `episode`,
          `mixedParents`, `nocache` -- whatever `type=` asks for (the client itself
          rewrites a show or season type to episode for them). A movie library's
          stay movies.
        - recentlyViewedShows is every top-level row, the viewed ones first by when
          and the rest by sort title. It is the one with `allowSync` false, and it
          carries offset and totalSize even unpaged.
        - Recently Added and Recently Viewed stop at 100 rows: fun's totalSize is
          exactly 100 on both of its libraries, and 6 or 60 on the others.
        """
        is_show = section.get("type") == "show"
        extra = {"title2": self.SUBSET_TITLES[leaf], "allowSync": True}
        if is_show:
            extra["mixedParents"] = True
            extra["nocache"] = True
            if leaf == "recentlyViewed":
                extra["title2"] = "Recently Viewed Episodes"
        view_group = "episode" if is_show else "movie"
        if leaf == "recentlyViewedShows":
            rows, _ = self._sorted(self.typed_rows(section_id, {"type": "2" if is_show else "1"}),
                                   "titleSort")
            rows.sort(key=lambda r: -self._last_viewed(r))
            extra["allowSync"] = False
            view_group = "show" if is_show else "movie"
        else:
            kind = "episode" if is_show else "movie"
            rows, _ = self._sorted(self.typed_rows(section_id, {"type": "4" if is_show else "1"}),
                                   "titleSort")
            if leaf == "onDeck":
                rows = [r for r in self.continue_watching()
                        if str(r.get("librarySectionID")) == str(section_id)
                        and r.get("type") == kind]
            elif leaf == "recentlyViewed":
                rows = sorted([r for r in rows if r.get("lastViewedAt")],
                              key=lambda r: -r["lastViewedAt"])[:100]
            elif leaf == "recentlyAdded":
                rows = sorted(rows, key=lambda r: -(r.get("addedAt") or 0))[:100]
            else:
                rows = sorted(rows, key=lambda r: str(r.get("originallyAvailableAt") or ""),
                              reverse=True)
        if leaf == "recentlyViewedShows" and not self.page_window(rows, query)[1]:
            extra["offset"] = 0
            extra["totalSize"] = len(rows)
        return self.listing_container(section_id, section, [_listing_row(r) for r in rows],
                                      view_group=view_group, query=query, **extra)

    @staticmethod
    def _artwork_row(row):
        """One backdrop as the screensaver and the library's art list carry it: the
        measured row, with every field but title and key blanked or -1."""
        return {"title": row.get("title"), "type": "image", "key": row.get("art"),
                "thumb": "", "label": "", "mediaType": "", "summary": "", "rating": -1,
                "addDate": -1, "modDate": -1, "metaModDate": -1}

    def random_artwork(self, query):
        """`/library/randomArtwork?sections=` -- what a screensaver cycles through.

        Measured on both servers: one `<Photo>` row per movie or show with a backdrop in
        the named libraries, in a different order on every request, at most 100, in a
        container carrying only `size`; no `sections` is `size 0`. plexsim 404'd it.
        """
        wanted = [s for s in (query.get("sections") or "").split(",") if s]
        rows = [r for section_id in wanted for r in self.catalog.section_items(section_id)
                if r.get("type") in ("movie", "show") and r.get("art")]
        rows = sorted(rows, key=lambda _row: uuid.uuid4().hex)[:100]
        return self.bare_container(size=len(rows), Metadata=[self._artwork_row(r) for r in rows],
                                   _xml={"Metadata": "Photo"})

    def section_arts(self, section_id, section):
        """`/library/sections/{id}/arts` -- the library's backdrops.

        Measured on both servers: the same `<Photo>` rows as randomArtwork, one per movie
        or show with a backdrop, in ratingKey order, in the library's container (with
        `nocache` and viewGroup `episode` on a show library). plexsim 404'd it.
        """
        is_show = section.get("type") == "show"
        rows = sorted([r for r in self.catalog.section_items(section_id)
                       if r.get("type") in ("movie", "show") and r.get("art")],
                      key=lambda r: int(r["ratingKey"]))
        fields = {
            "allowSync": False,
            "art": "/:/resources/show-fanart.jpg" if is_show else "/:/resources/movie-fanart.jpg",
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": 1655122614,
            "thumb": "/:/resources/show.png" if is_show else "/:/resources/movie.png",
            "title1": section.get("title"),
            "viewGroup": "episode" if is_show else "movie",
            "viewMode": 65592,
        }
        if is_show:
            fields["nocache"] = True
        return self.bare_container(size=len(rows), Metadata=[self._artwork_row(r) for r in rows],
                                   _xml={"Metadata": "Photo"}, **fields)

    def section_extras(self, section, rest):
        """`/library/sections/{id}/extras` and its one view, `/extras/all`.

        Measured on both servers: `/extras` is the library's container naming a single `all`
        view ("All Movies", "All TV Shows"), and `/extras/all` lists the library's trailers
        and other clips in the same container -- `size 0` and no rows for a library with none,
        which is every plexsim library. Anything deeper is a 404, and a show library's
        container carries `nocache`. plexsim 404'd both.
        """
        if rest not in ([], ["all"]):
            return ("error", 404, "plexsim: not found")
        is_show = section.get("type") == "show"
        fields = {
            "art": "/:/resources/show-fanart.jpg" if is_show else "/:/resources/movie-fanart.jpg",
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": 1655122614,
            "thumb": "/:/resources/show.png" if is_show else "/:/resources/movie.png",
            "title1": section.get("title"),
        }
        if is_show:
            fields["nocache"] = True
        if not rest:
            return self.bare_container(size=1, Directory=[
                {"key": "all", "title": "All %s" % (section.get("title") or "")}], **fields)
        return self.bare_container(size=0, **fields)

    def section_leaf(self, section_id, section, leaf, query):
        """Six section leaves the community OpenAPI description names, all 404 on plexsim.

        Measured on both servers, on a movie and a show library alike:

        - `allLeaves` is every leaf the library holds -- its movies in sort-title order,
          or its episodes in broadcast order -- as listing rows, in a container with no
          librarySection fields and no title2, whose viewGroup names the LIBRARY's type.
        - `albums` is an empty album list titled "By Album", with `mixedParents`.
        - `cluster` and `moment` are the same empty "By Moment" secondary container with
          `clusterZoomLevel` 1, `clusteringActive` false and a key onto `/all`.
        - `computePath` needs `startID` and `endID` (400 without), 404s an id the server
          does not have, and otherwise answers an empty path; `nearest` is a 400 however
          it is asked, here and on the reference alike.
        """
        is_show = section.get("type") == "show"
        fields = {
            "allowSync": False,
            "art": "/:/resources/show-fanart.jpg" if is_show else "/:/resources/movie-fanart.jpg",
            "identifier": "com.plexapp.plugins.library",
            "mediaTagPrefix": "/system/bundle/media/flags/",
            "mediaTagVersion": 1655122614,
            "thumb": "/:/resources/show.png" if is_show else "/:/resources/movie.png",
            "title1": section.get("title"),
        }
        if leaf == "nearest":
            return ("error", 400, "plexsim: bad request")
        if leaf == "computePath":
            start, end = query.get("startID") or "", query.get("endID") or ""
            if not start or not end:
                return ("error", 400, "plexsim: bad request")
            if not (start.isdigit() and end.isdigit() and int(start) in self.catalog.items
                    and int(end) in self.catalog.items):
                return ("error", 404, "plexsim: not found")
            fields.pop("allowSync")
            return self.bare_container(size=0, **fields)
        if is_show:
            fields["nocache"] = True
        fields["viewMode"] = 65592
        if leaf == "allLeaves":
            # Measured on kids (1.43.4) and zen (1.42.2): a section's `allLeaves` is a
            # full QUERY surface, exactly like `/all` -- filters, `sort=`, `limit=` and
            # `type=` all apply, an unknown sort key and an unparseable type are 500s,
            # and a type the leaves are not selects nothing. plexsim answered the whole
            # leaf list to every one of them, so the filter bar and the sort menu over
            # an "All episodes" view silently did nothing: a 200 that looks like it
            # worked, which is fix 4's failure mode from the inside.
            self._parsed_type(query)
            rows = self.typed_rows(section_id, {"type": "4" if is_show else "1"})
            asked = str(query.get("type") or "")
            if asked:
                rows = [r for r in rows
                        if r.get("type") == {"1": "movie", "2": "show", "3": "season",
                                             "4": "episode"}.get(asked)]
            rows, _ = self.apply_filters(rows, query, section)
            if query.get("sort"):
                rows, unsupported = self._sorted(rows, query["sort"])
                if unsupported:
                    return ("error", 500, "plexsim: unsupported sort key %s" % unsupported)
            elif not is_show:
                rows, _ = self._sorted(rows, "titleSort")
            if "group" in query:
                # Cycle 14: `group=` is the last part of the grammar this surface was
                # missing. `/library/sections/2/allLeaves?group=index` is 3 of kids'
                # 6 episodes and 1144 of zen's 9287 -- the same number as
                # `type=4&group=index` on the grid. plexsim ignored it and answered
                # every leaf. (An ITEM's `/allLeaves?group=` is ignored on both
                # references and on plexsim, so only the SECTION form takes it.)
                rows = self.grouped(rows, query.get("group"))
            rows = self._limited(rows, query)
            shown = self._excluded_elements(
                [_grid_row(r, keep_section=False, keep_guid=True)
                 for r in self._guid_gate([_listing_row(r) for r in rows], query)], query)
            return self.bare_container(size=len(shown), viewGroup="show" if is_show else "movie",
                                       Metadata=shown, **fields)
        if leaf == "albums":
            return self.bare_container(size=0, mixedParents=True, title2="By Album",
                                       viewGroup="album", **fields)
        return self.bare_container(size=0, clusterZoomLevel=1, clusteringActive=False,
                                   content="secondary",
                                   key="/library/sections/%s/all?clusterZoomLevel=1" % section_id,
                                   title2="By Moment", viewGroup="secondary", **fields)

    def _last_viewed(self, row):
        """When a row was last watched -- for a show, its most recent episode."""
        stamps = [row.get("lastViewedAt") or 0]
        if row.get("type") == "show":
            stamps += [leaf.get("lastViewedAt") or 0
                       for leaf in self.catalog.leaves(row["ratingKey"])]
        return max(stamps)

    # `/library/sections/{id}/autocomplete`, field by field on both servers: a tag
    # field answers its tags, a text or number field answers the rows it matches,
    # the fields below answer 500, and any other name is unknown and a 400.
    AUTOCOMPLETE_TAGS = {"genre": ("Genre", 1), "collection": ("Collection", 2),
                         "director": ("Director", 4), "writer": ("Writer", 5),
                         "actor": ("Role", 6), "producer": ("Producer", 7),
                         "country": ("Country", 8), "label": ("Label", 11),
                         "network": ("Network", 319)}
    AUTOCOMPLETE_TEXT = ("title", "titleSort", "originalTitle", "summary", "tagline",
                         "studio", "contentRating", "editionTitle", "year", "index")
    AUTOCOMPLETE_EMPTY = ("userRating", "duration", "addedAt", "originallyAvailableAt",
                          "guid", "similar", "mood", "style")
    AUTOCOMPLETE_REFUSED = ("decade", "resolution", "unwatched", "inProgress",
                            "audioLanguage", "location", "viewCount", "lastViewedAt",
                            "unwatchedLeaves")

    def autocomplete(self, section_id, section, query):
        """The Edit dialog's suggestions: `?type=N&<field>.query=<text>&limit=10`.

        Measured on both servers; plexsim 404'd every form of it.

        - `type` is required, exactly ONE field may be asked (a repeat of the same
          field keeps its last value, two different fields are a 400), and an empty
          value is a 400. `limit` must be an integer: 0 answers one row and a
          negative one every row, as does no limit at all.
        - A TAG field answers `Directory` rows of id, `filter`, tag and tagType (plus
          tagKey and thumb for a person), alphabetical, drawn from the rows of that
          type in this library.
        - A text or number field answers the matching rows as the listing carries
          them, in sort-title order, each led by `<field>.queryRange`: the inclusive
          span of the FIRST case-insensitive occurrence of the query. That is not
          always the word that matched -- `e` matches 300: Rise of an Empire through
          "Empire" and reports `8,8`, the end of "Rise". `index` has no range, and a
          movie's index is 1.
        - A season list answers nothing, and neither does a type the library does not
          hold.
        """
        if not query.get("type"):
            return ("error", 400, "plexsim: bad request")
        asked = [(name[:-len(".query")], value)
                 for name, value in query.get("_pairs") or [] if name.endswith(".query")]
        if len({field for field, _ in asked}) != 1:
            return ("error", 400, "plexsim: bad request")
        field, wanted = asked[-1]
        if not wanted:
            return ("error", 400, "plexsim: bad request")
        limit = None
        if "limit" in query:
            try:
                limit = int(query["limit"])
            except ValueError:
                return ("error", 400, "plexsim: bad request")
            limit = None if limit < 0 else max(limit, 1)
        if field in self.AUTOCOMPLETE_REFUSED:
            return ("error", 500, "plexsim: internal error")
        if field not in self.AUTOCOMPLETE_TAGS and field not in self.AUTOCOMPLETE_TEXT \
                and field not in self.AUTOCOMPLETE_EMPTY:
            return ("error", 400, "plexsim: bad request")
        kind = {"1": "movie", "2": "show", "4": "episode"}.get(query["type"])
        rows = self.typed_rows(section_id, {"type": query["type"]}) if kind else []
        wanted = wanted.lower()
        if field in self.AUTOCOMPLETE_TAGS:
            element, tag_type = self.AUTOCOMPLETE_TAGS[field]
            seen = {}
            for row in rows:
                for tag in row.get(element) or []:
                    if isinstance(tag, dict) and tag.get("tag") \
                            and _begins_word(tag["tag"], wanted):
                        seen.setdefault(tag["tag"].lower(), tag)
            entries = []
            for _, tag in sorted(seen.items()):
                entry = {"id": tag.get("id"), "filter": "%s=%s" % (field, tag.get("id")),
                         "tag": tag["tag"], "tagType": tag_type}
                for extra in ("tagKey", "thumb"):
                    if tag.get(extra):
                        entry[extra] = tag[extra]
                entries.append(entry)
            return self.secondary_container(section_id, section, None,
                                            Directory=entries[:limit])
        found = []
        if field in self.AUTOCOMPLETE_TEXT:
            for row in rows:
                if field == "titleSort":
                    value = row.get("titleSort") or row.get("title")
                elif field == "index":
                    value = row.get("index") or (1 if row.get("type") == "movie" else None)
                else:
                    value = row.get(field)
                if value is None or value == "" or not _begins_word(value, wanted):
                    continue
                shown = _grid_row(_listing_row(row), keep_section=False)
                if field != "index":
                    at = str(value).lower().find(wanted)
                    shown = dict([("%s.queryRange" % field,
                                   "%d,%d" % (at, at + len(wanted) - 1))] + list(shown.items()))
                found.append((row, shown))
        order = self._sort_key("titleSort")
        found.sort(key=lambda pair: order(pair[0]))
        return self.secondary_container(section_id, section, None,
                                        Metadata=[shown for _, shown in found][:limit])

    def folder_view(self, section_id, section, query):
        """By Folder: the library's directory tree, one level per request.

        Measured on both servers: the top level lists the directories under the
        library's Location as `{key: .../folder?parent=<id>, title}` rows, and
        `?parent=<id>` lists THAT directory -- its subdirectories the same way and its
        files as the items they hold, full listing rows (an episode's without its
        Director, Writer and Role). An unknown or empty parent is an empty list and a
        non-numeric one the top level. A directory's items come in FILE order --
        Firefly's episodes by their s01eNN names, not their titles. The container is
        the secondary one plus the librarySection triple and `allowSync`, and it pages.

        plexsim listed one directory per ITEM, titled after the item, and ignored
        `parent`, so every folder opened the same list again and the view never
        reached an item. Its catalogs keep every file flat under `/plexsim/<n>`, so
        the tree is built from each Part's path relative to the library's Location --
        a file outside it sits at the top -- with directory ids numbered in path order.
        """
        is_show = section.get("type") == "show"
        root = "/plexsim/%s/" % section_id
        placed = []
        for row in self.typed_rows(section_id, {"type": "4" if is_show else "1"}):
            files = [part.get("file") for media in row.get("Media") or []
                     for part in media.get("Part") or [] if part.get("file")]
            if not files:
                continue
            relative = (files[0][len(root):] if files[0].startswith(root)
                        else files[0].rsplit("/", 1)[-1])
            parts = tuple(p for p in relative.split("/") if p) or ("",)
            placed.append((parts[:-1], parts[-1], row))
        directories = {folders[:depth] for folders, _, _ in placed
                       for depth in range(1, len(folders) + 1)}
        ids = {folders: number for number, folders in enumerate(sorted(directories), 1)}
        level = ()
        parent = query.get("parent")
        if parent == "":
            level = None
        elif parent is not None and parent.isdigit():
            level = next((f for f, number in ids.items() if number == int(parent)), None)
        rows = []
        if level is not None:
            for folders in sorted((f for f in directories if f[:-1] == level),
                                  key=lambda f: f[-1].lower()):
                rows.append({"key": "/library/sections/%s/folder?parent=%d"
                                    % (section_id, ids[folders]),
                             "title": folders[-1]})
            for _, _, row in sorted((p for p in placed if p[0] == level),
                                    key=lambda p: (p[1].lower(), int(p[2]["ratingKey"]))):
                shown = _grid_row(_listing_row(row), keep_section=False)
                if row.get("type") == "episode":
                    for tag in ("Director", "Writer", "Role"):
                        shown.pop(tag, None)
                rows.append(shown)
        page, paged, start, total = self.page_window(rows, query)
        fields = {}
        if paged:
            fields.update(offset=start, totalSize=total)
        if page and not any("type" in r for r in page):
            fields["_xml"] = {"Metadata": "Directory"}
        return self.secondary_container(
            section_id, section, "By Folder", Metadata=page, allowSync=True,
            librarySectionID=int(section_id) if str(section_id).isdigit() else section_id,
            librarySectionTitle=section.get("title"),
            librarySectionUUID=section.get("uuid"), **fields)

    # The sort keys the captured Meta advertises. A key the listing cannot honour
    # is a sort the UI offers and the server ignores, which reads as a broken
    # control rather than a missing one.
    TEXT_SORTS = ("titleSort", "title", "originallyAvailableAt", "contentRating",
                  # Cycle 14: seven more text columns sort on both references.
                  "guid", "originalTitle", "studio", "type", "tagline", "summary",
                  "editionTitle")

    NUMERIC_SORTS = ("addedAt", "lastViewedAt", "year", "rating", "audienceRating",
                     "duration", "viewCount", "viewOffset", "index",
                     "unviewedLeafCount", "originallyAvailableAt", "mediaHeight",
                     "updatedAt", "lastRatedAt", "ratingCount",
                     # The keys `includeAdvanced=1` advertises. Three of them --
                     # userRating, contentRating, mediaBitrate -- answered 500 on
                     # plexsim while its own Meta was about to offer them, which is
                     # an advertised control that breaks the grid when used.
                     "userRating", "mediaBitrate",
                     # Cycle 14, measured key by key on kids (1.43.4) and zen (1.42.2),
                     # which agree exactly: thirteen MORE keys sort although
                     # `/sorts?includeAdvanced=1` advertises only fifteen, and none of
                     # these is among them. They are real orderings, checked against
                     # the rows zen returns -- `sort=createdAt` comes back in addedAt
                     # order, `sort=mediaSize` smallest file first. plexsim answered
                     # 500, which is a blank page for python-plexapi and for any
                     # stored smart-list URI naming one.
                     "contentRatingAge", "skipCount", "absoluteIndex", "mediaSize",
                     "createdAt", "viewedLeafCount")

    # ...and `leafCount` is the other direction: BOTH references answer 500 to it,
    # along with `childCount`, `slug`, `key` and any unknown key, while plexsim sorted
    # by it happily. A sort a real PMS refuses has to be refused, so it is gone from
    # the list above.

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
                # Measured on both servers across eleven keys and four row types: a
                # row with NO value sorts BELOW every row that has one -- first
                # ascending, last descending -- so the "missing" flag has to sort
                # FIRST, not last. `(value is None, ...)` put it above every value,
                # and twelve of the fifteen advertised sorts default to `desc`.
                return (value is not None, str(value or "").lower())
            return text
        if key == "mediaSize":
            # The Part's own size, the way the `mediaSize` FILTER reads it.
            return lambda r: (True, max(
                [p.get("size") or 0 for m in r.get("Media") or []
                 for p in m.get("Part") or []] or [0]))
        if key == "createdAt":
            # Measured on zen: `sort=createdAt` comes back in addedAt order.
            return lambda r: (r.get("addedAt") is not None, r.get("addedAt") or 0)
        if key in self.NUMERIC_SORTS:
            # Same rule as the text keys above: absent sorts below every value.
            return lambda r: (r.get(key) is not None, r.get(key) or 0)
        return None

    def _scoped_sort_key(self, key):
        """A sort key with the TYPE prefix python-plexapi and the Meta both send.

        Measured: `movie.titleSort:desc`, `show.titleSort`, `season.index` and
        `episode.id` all sort, and the prefix is a scope -- on an episode list
        `show.titleSort` orders by the SHOW's sort title and `season.index` by the
        season's number. An unknown prefix (`bogus.titleSort`), the row's own type,
        or a scope below the row reads the row itself; an unknown FIELD after any
        prefix is still a 500. `id` is the ratingKey. plexsim 500'd every prefixed
        key -- including the episode sort its own Meta advertises -- so every
        python-plexapi `all(sort=)`, `search(sort=)` and `recentlyAdded()` raised.
        """
        if key == "_natural":
            return self._natural_order
        scope, _, bare = key.rpartition(".")
        if bare == "id":
            def base(row):
                return (False, int(row.get("ratingKey") or 0))
        else:
            base = self._sort_key(bare)
        if base is None:
            return None
        if not scope:
            return base

        def resolve(row):
            kind = row.get("type")
            if (scope == "show" and kind in ("season", "episode")) \
                    or (scope == "season" and kind == "episode"):
                found = self.scope_rows(scope, row)
                return found[0] if found else row
            return row
        return lambda row: base(resolve(row))

    def _natural_order(self, row):
        """Show title, season number, episode number: a season or episode list's
        own order, which is what its ties fall back to."""
        show = row
        if row.get("type") in ("season", "episode"):
            show = (self.scope_rows("show", row) or [row])[0]
        title = str(show.get("titleSort") or show.get("title") or "").lower()
        if row.get("type") == "episode":
            return (title, int(row.get("parentIndex") or 0), int(row.get("index") or 0))
        return (title, int(row.get("index") or 0), 0)

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
                and not any(key.rpartition(".")[2] == "random" for key, _d, _s in terms):
            # ...except on a season or episode list, where a tie falls back to the
            # show, season and episode order: `sort=episode.index:desc` puts S1E3
            # before S2E3, and `sort=show.titleSort` on one show keeps broadcast
            # order (measured).
            if rows and all(r.get("type") in ("season", "episode") for r in rows):
                terms = list(terms) + [("_natural", False, "")]
            else:
                terms = list(terms) + [("titleSort", False, "")]
        ordered = list(rows)
        # Right to left, relying on the sort being stable: the leftmost key ends
        # up dominant, which is the order a real PMS applies them in.
        for key, descending, seed in reversed(terms):
            # Measured on both servers: `movie.random` and `show.random` (with a
            # direction or a seed) sort like `random`; python-plexapi always sends the
            # prefixed form, and plexsim 500'd it.
            if key.rpartition(".")[2] == "random":
                ordered = sorted(ordered, key=lambda r: zlib.crc32(
                    ("%s/%s" % (seed or "0", r.get("ratingKey"))).encode()))
                if descending:
                    ordered.reverse()
                continue
            getter = self._scoped_sort_key(key)
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
    # Re-measured on kids (1.43.4) and zen (1.42.2) across all 27 candidates: `type`,
    # `guid` and `id` group too, and plexsim answered 500 to all three. They are the
    # row's own identity columns, which is consistent with the rest of the set; the
    # earlier probe simply did not try them. `id` became a FILTER field in cycle 11 and
    # was still not a group field.
    GROUPABLE = {"title", "titleSort", "studio", "contentRating", "year", "decade",
                 "addedAt", "updatedAt", "originallyAvailableAt", "audienceRating",
                 "rating", "editionTitle", "index", "summary", "tagline",
                 "contentRatingAge", "type", "guid", "id"}

    def grouped(self, rows, field):
        """`group=<field>` -- one row per distinct value of that field.

        Measured: `?group=studio` on five movies that share a studio answers ONE
        row, and an empty, unknown or non-scalar field is a 500.

        WHICH row survives was recorded as an unknowable SQL artifact and
        implemented as "the first in the order the sort just produced". Cycle 14
        measured it: it is the group's **minimum `addedAt`**, and it is chosen
        BEFORE the sort, so the surviving SET is the same under every `sort=` and
        only the order changes. On kids `group=type` answers ratingKey 3 (the
        lowest addedAt) and not 1 (the lowest ratingKey), under `sort=titleSort`,
        `titleSort:desc` and `addedAt:desc` alike. On zen the survivor is the
        group's minimum addedAt in 88/88 year groups, 1074/1074 studio groups,
        1238/1238 originallyAvailableAt groups, 3450/3450 title groups and
        3887/3887 guid groups, with groups up to 64 members and the survivor's
        index in sort order ranging 0-7. (min addedAt and min ratingKey coincide
        on zen, which is why kids is the discriminating witness.)

        It matters because Group and Sort are independent controls in the client:
        picking the survivor after the sort makes changing the sort change WHICH
        rows the grid shows, and a smart list storing both has contents that
        depend on its own sort.

        A tie on `addedAt` inside one group has no witness -- kids' two equal rows
        never form a group of their own -- so the lowest ratingKey breaks it here,
        which at least keeps the answer independent of the sort. See HANDOVER.md 3.3.
        """
        bare = self.group_field(field)
        if not bare or bare not in self.GROUPABLE:
            raise filters_module.FilterError(500, "cannot group by %r" % field)
        best = {}
        for row in rows:
            # The prefix is a SCOPE, exactly as it is on a filter and on a sort
            # key: `filter_value` resolves it against the row the prefix names.
            marker = str(self.filter_value(field, row))
            rank = (row.get("addedAt") or 0, int(row.get("ratingKey") or 0))
            if marker not in best or rank < best[marker][0]:
                best[marker] = (rank, id(row))
        chosen = {entry[1] for entry in best.values()}
        # The survivors come back in the order the sort produced, which is what a
        # real PMS does: same set, re-ordered.
        return [row for row in rows if id(row) in chosen]

    def group_field(self, field):
        """The column a `group=` key names, with its type prefix stripped."""
        return field.split(".", 1)[1] if field and "." in field else field

    def _sortable(self, key):
        """Whether a `sort=` key is one this server can order by.

        `random` is not in the key tables because it is not read off a row, but
        it IS a sort a real PMS accepts -- and /query/parse validates the key
        before it echoes the Order block, answering 500 for one it does not know.
        """
        return key.rpartition(".")[2] == "random" or self._scoped_sort_key(key) is not None

    # How each advertised filter field is read off a row. The keys here are the
    # `Field` entries of the captured Meta, so plexsim filters on exactly what it
    # tells the client it can filter on -- and a field it cannot answer from its
    # own rows is honestly absent rather than guessed at.
    TAG_FILTER_FIELDS = {"genre": "Genre", "collection": "Collection",
                         "director": "Director", "writer": "Writer",
                         "producer": "Producer", "country": "Country",
                         "actor": "Role", "label": "Label",
                         "contentRating": "contentRating", "studio": "studio"}

    SCOPE_TYPES = ("movie", "show", "season", "episode")

    def scope_rows(self, scope, row):
        """The rows a SCOPED filter field is read from, for one listing row.

        Measured: the prefix names WHOSE field is compared. `type=4&show.title=Fire`
        is every episode of the show titled Firefly, `type=4&season.index=2` every
        episode of season 2, and a scope BELOW the row matches when any descendant
        does -- `type=2&episode.title=Train` is the show with that episode,
        `type=3&episode.title=Train` its season. An unknown scope, or the row's own
        type, is the row itself. plexsim read the row's OWN field whatever the
        prefix said, so every Show-scoped filter on an episode grid tested the
        episode.
        """
        kind = row.get("type")
        if scope not in self.SCOPE_TYPES or scope == kind:
            return [row]
        items = self.catalog.items

        def lookup(key):
            found = items.get(int(key)) if str(key or "").isdigit() else None
            return [found] if found is not None else []

        if kind == "episode" and scope == "season":
            return lookup(row.get("parentRatingKey"))
        if kind == "episode" and scope == "show":
            return lookup(row.get("grandparentRatingKey"))
        if kind == "season" and scope == "show":
            return lookup(row.get("parentRatingKey"))
        if kind == "show" and scope == "season":
            return list(self.catalog.children(row["ratingKey"]))
        if kind in ("show", "season") and scope == "episode":
            return list(self.catalog.leaves(row["ratingKey"]))
        return [row]

    def filter_value(self, field, row):
        """One row's value for an advertised filter field.

        Cycle 14: the type prefix is a SCOPE here too, not decoration to strip.
        `type=4&group=show.title` collapses kids' six episodes to 1 and zen's 9287
        to 505 (its show count) where `group=title` gives 4 and 4443;
        `group=season.index` gives 2 and 58 where `group=index` gives 3 and 1144.
        plexsim stripped the prefix and read the field off the episode, so every
        prefixed group and every prefixed value read was the unprefixed one.
        """
        scope, _, field = field.rpartition(".")
        if scope and scope in self.SCOPE_TYPES and row.get("type") != scope:
            found = self.scope_rows(scope, row)
            if found:
                row = found[0]
        if field == "id":
            # The row's own key: `id=` is the client's "is there another copy of this?"
            # probe, sent beside `guid=`.
            return row.get("ratingKey")
        if field == "parent":
            # Cycle 15: the row's IMMEDIATE parent, so `type=4&parent=<season>` is that
            # season's episodes and `parent=<show>` is none of them -- measured 6 -> 3
            # and 6 -> 0 on kids, 9287 -> 22 and -> 0 on zen.
            return row.get("parentRatingKey")
        if field == "file":
            # Cycle 15: a case-insensitive substring of the Part path, which is what
            # the `string` type's bare `=` already does -- the value just has to be
            # the path.
            #
            # It matches ANY Part, measured on zen: movie 10513 has two versions whose
            # paths differ only in a trailing `{083223}`, and a fragment that occurs in
            # the SECOND path alone still finds the row. The paths are joined on a
            # newline rather than returned as a list, because a LIST routes through the
            # tag-style set membership and would compare whole paths for equality; a
            # newline cannot occur inside a path, so no match can span the join.
            return chr(10).join(part.get("file") or ""
                              for media in row.get("Media") or []
                              for part in media.get("Part") or [])
        if field in self.TAG_FILTER_FIELDS:
            return row.get(self.TAG_FILTER_FIELDS[field])
        if field == "titleSort":
            # A row omits titleSort when it equals the title; the value is still
            # there to compare (`titleSort=sintel` finds Sintel, measured).
            return row.get("titleSort") or row.get("title")
        if field == "index" and row.get("type") == "movie" and "index" not in row:
            # Measured: neither reference emits `index` on a movie row, and `index=1`
            # still matches every movie on both while `index=0` and `index=2` match
            # none. The column is there; the attribute is not.
            return 1
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
        if field in LEAF_COUNT_FILTERS and row.get("type") == "season":
            # Cycle 14, measured on zen: on a SEASON listing these three read the
            # SHOW's counts, not the season's. `type=3&viewedLeafCount>>0` answers 48
            # seasons -- every season of the eight shows that have a watched leaf --
            # and ALL 48 carry `viewedLeafCount` 0 of their own, so the filter cannot
            # be reading the row it returns. `type=3&unviewedLeafCount=0` is 2 there.
            # The copy agrees on the two-season tree: with one episode of season 1
            # watched, `type=3&viewedLeafCount=1` answers season 1 AND season 2.
            #
            # It is the same shape as the scoped filters: the value is read from the
            # row the FIELD belongs to, and these three belong to a show.
            parent = self.scope_rows("show", row)
            if parent:
                row = parent[0]
        if field == "unwatchedLeaves":
            leaves = (row.get("leafCount") or 0) - (row.get("viewedLeafCount") or 0)
            return 1 if leaves > 0 else 0
        if field == "unviewedLeafCount":
            return (row.get("leafCount") or 0) - (row.get("viewedLeafCount") or 0)
        if field == "viewedLeafCount":
            # Measured on zen: `viewedLeafCount>>0` on its show library returns exactly
            # the eight shows whose row carries a non-zero count, and `=0` the other
            # 499. plexsim had no such field, so both answered the whole library.
            return row.get("viewedLeafCount") or 0
        if field == "viewedOrInProgressLeafCount":
            # The key of a real PMS's own "Start Watching" shelf. Measured on zen it
            # partitions the library exactly as `viewedLeafCount` does -- but zen and
            # kids between them hold NO leaf that is in progress and unwatched, so the
            # "or in progress" half of the name could not be separated from the viewed
            # half on either reference. It is read here as the name says; see
            # HANDOVER.md 3.3.
            leaves = self.catalog.leaves(row.get("ratingKey")) \
                if row.get("type") in ("show", "season") else []
            return sum(1 for leaf in leaves
                       if leaf.get("viewCount") or leaf.get("viewOffset"))
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
        if field == "duplicate":
            # Measured on zen, which has 214 duplicated movies of 3887 and 504 episodes
            # of 9287: the Duplicates filter is "more than one Media on the row", and for
            # a SHOW or SEASON -- neither of which has Media of its own -- it is "a leaf
            # under it has more than one" (44 shows and 74 seasons there, each sampled
            # one holding a multi-Media episode). plexsim returned 0 here, so a filter
            # its own `includeAdvanced` Meta advertises could never select anything.
            if len(row.get("Media") or []) > 1:
                return 1
            if row.get("type") in ("show", "season"):
                return 1 if any(len(leaf.get("Media") or []) > 1
                                for leaf in self.catalog.leaves(row.get("ratingKey"))) else 0
            return 0
        if field in ("hdr", "dovi", "hdr10plus", "atmos", "trash"):
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
                if filters_module.evaluate(tree, r, self.filter_value, self.scope_rows)]
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

        def parse_field_type(name):
            # Cycle 15: the parse surface is WIDER than the filter surface. See
            # PARSE_ONLY_FIELD_TYPES -- eight fields echo as Rules here that a
            # listing either ignores, refuses or (for `guid`) filters by another path.
            return FILTER_FIELD_TYPES.get(name) or PARSE_ONLY_FIELD_TYPES.get(name)

        tree = filters_module.parse(query.get("_pairs") or [],
                                    parse_field_type, errors=errors)
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
    def _guid_prefix(rows, query):
        """`guid=` on a listing: a case-sensitive PREFIX of the row's guid.

        Measured on both servers, and not a field the filter grammar advertises:
        `/library/all?type=1&guid=<guid>` is that one movie, `guid=plex` every matched
        one, `guid=PLEX` none and an empty value every row. It is how the client asks
        whether this server holds an item it found elsewhere -- `type=N&guid=`, from
        its availability lookup -- and plexsim ignored it, so every movie guid
        "found" the whole library. `guid==` and `guid!=` do something else again and
        the client sends neither; they are left to the grammar.
        """
        wanted = [value for name, value in query.get("_pairs") or [] if name == "guid"]
        if not wanted:
            return rows
        return [r for r in rows
                if all(str(r.get("guid") or "").startswith(value) for value in wanted)]

    def _with_storage(self, row):
        """`includeStorage=1` adds `durationTotal` and `storageTotal` to a grid row.

        Measured on kids (1.43.4) and zen (1.42.2): the pair appears ONLY on
        `/library/sections/{id}/all` and only at the library's own top-level type -- a
        show carries the sum over its episodes (Firefly: 60186 ms and 37683488 bytes,
        exactly its six episodes' durations and part sizes), a movie its own, and
        `?type=3` or `?type=4` on the same library carries neither, as do allLeaves,
        `/library/all`, `/newest`, `/children` and a detail read. It is what the
        library's storage column prints, and plexsim sent neither, so the column was
        empty on a server that has both numbers on the row already.
        """
        leaves = [row] if row.get("Media") else self.catalog.leaves(row.get("ratingKey"))
        duration = sum(int(leaf.get("duration") or 0) for leaf in leaves)
        storage = sum(int(part.get("size") or 0) for leaf in leaves
                      for media in leaf.get("Media") or [] for part in media.get("Part") or [])
        return dict(row, durationTotal=duration, storageTotal=storage)

    @staticmethod
    def _excluded_elements(rows, query):
        """`excludeElements=` on a list of rows, measured on both servers.

        A LISTING drops the named child elements as a detail read does, and dropping
        Media substitutes the counts -- `mediaCount` (0 on a show) and
        `mediaCountOptimized` 0. Re-measured in cycle 11 on kids (1.43.4) and zen
        (1.42.2): it applies to the section `allLeaves`, to `/library/people/{id}/media`
        and to `/hubs/items` as well, which plexsim answered with their Media intact --
        so a client that asked for the cheap list to lay a page out was handed the
        expensive one and no count to draw the badge from.
        """
        excluded = [e for e in ((query or {}).get("excludeElements") or "").split(",") if e]
        if not excluded:
            return rows
        trimmed = []
        for row in rows:
            out = {k: v for k, v in row.items() if k not in excluded}
            if "Media" in excluded:
                # Cycle 15: this now runs as a post-handler pass as well, so a row a
                # handler has ALREADY trimmed arrives with no `Media` and its count
                # in place. Recomputing from an absent `Media` would zero it.
                out["mediaCount"] = (len(row["Media"]) if "Media" in row
                                     else row.get("mediaCount", 0))
                out["mediaCountOptimized"] = 0
            trimmed.append(out)
        return trimmed

    @staticmethod
    def _limited(rows, query):
        """`?limit=N` on a listing -- measured on kids (1.43.4) and zen (1.42.2).

        Not paging: it caps the rows the QUERY returns, before the container's own
        Start/Size window, and the two edges are the ones autocomplete's limit has --
        `limit=0` answers ONE row and a negative one every row. `limit=abc` is a 500,
        the refusal a bad filter value gets. Measured as ignored on `/newest`,
        `/recentlyAdded` and `/children`, so it belongs to the query grammar rather
        than to every listing: plexsim read it only in `/query/parse`, so a capped
        smart list's own preview -- the listing the client builds from the stored
        query -- was the whole section.
        """
        if not query or "limit" not in query:
            return rows
        count = int(filters_module.as_number(query["limit"]))
        if count < 0:
            return rows
        return rows[:max(count, 1)]

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
        page = [_grid_row(r, keep_section=False, keep_guid=True) for r in page]
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
            # Measured on kids (1.43.4) and zen (1.42.2): the section GRID says
            # `allowSync: true`, and so do the hub containers. plexsim said false here
            # while already saying true on /newest, /recentlyAdded, /onDeck, /folder,
            # /children and a detail row -- so the single most-requested container on
            # the surface disagreed with every other one, and it is an attribute
            # python-plexapi reads off every section listing.
            "allowSync": True,
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

    def section_timeline_container(self, section_id, section):
        """`/library/sections/{id}/timeline` -- what changed in a library lately.

        Measured on the reference: a secondary container with `latestEntryTime` and
        `updateQueueSize`, whose rows are the items the server recently finished
        (state 5: the row without its tags, plus `state`) and removed (state 9: a
        stub with an empty title and summary). fun, with nothing recent, answers
        `size 0` and `latestEntryTime 0`. plexsim 404'd it, which python-plexapi's
        `section.timeline()` raises on.
        """
        rows = []
        latest = 0
        entries = self.section_timeline.get(str(section_id), {})
        for key, entry in sorted(entries.items(), key=lambda kv: -kv[1]["at"]):
            latest = max(latest, entry["at"])
            if entry["state"] == self.TIMELINE_DONE:
                row = self.catalog.items.get(int(key)) if str(key).isdigit() else None
                if row is None:
                    continue
                shown = _grid_row(_listing_row(row), keep_section=False)
                for tag in GRID_TAGS:
                    shown.pop(tag, None)
                shown["state"] = entry["state"]
            else:
                shown = dict(entry["stub"])
            rows.append(shown)
        found = self.secondary_container(section_id, section, None, Metadata=rows,
                                         latestEntryTime=latest, updateQueueSize=0)
        found["MediaContainer"]["size"] = len(rows)
        return found

    def section_settings(self, section_id, section, settings=None):
        """A library's setting list with the values written to it."""
        if settings is None:
            settings = SECTION_PREFS.get(section.get("type") or "movie") or []
        return [
            dict(s, value=self.section_pref_overrides[(str(section_id), s.get("id"))])
            if (str(section_id), s.get("id")) in self.section_pref_overrides else s
            for s in settings
        ]

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

    # Measured across all 26 secondary axes on kids (1.43.4) and zen (1.42.2): every
    # axis carries `content: "secondary"` EXCEPT `actor`, which carries `viewGroup`
    # without it. No rule explains it and both servers agree, so it is emulated as
    # measured -- a field invented is as wrong as one missing.
    CONTENTLESS_AXES = ("actor",)

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
        if leaf == "label":
            # Cycle 15, measured on the copy: a label written onto a COLLECTION is in
            # this axis too, under a bare call and under every `type=`. plexsim built
            # the axis from library ITEMS alone, so a label used to organise
            # collections could not be filtered on at all.
            rows = list(rows) + list(self.library.collections(section_id))
        entries = []
        if leaf in SECONDARY_TAGS:
            key = SECONDARY_TAGS[leaf]
            seen = {}
            for row in rows:
                # Cycle 14, measured on kids and zen: the ACTOR axis is the union of
                # each item's FIRST THREE roles, not every Role tag in the library.
                # kids' five movies contribute 0+2+2+2+3 = 9 and the axis totalSize is
                # exactly 9 -- Tears of Steel has eight roles and only its first three
                # are there. On zen the axis is 3413 where `/library/tags?type=6` is
                # 59030, and all 15 sampled movies with three or more roles contribute
                # exactly indices 0, 1 and 2. `?actor=<id>` still matches a role the
                # axis omits, so the cap is on the AXIS and not on the tag.
                #
                # It is actor-only: director, writer, producer, genre and country
                # carry every tag (7 producers, 5 writers, 7 genres, all present).
                # The axis's own title2 says as much -- "By Starring Actor".
                tags = row.get(key) or []
                for tag in (tags[:3] if leaf == "actor" else tags):
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
        if leaf == "contentRating" and any(not row.get("contentRating") for row in rows):
            # Measured on both servers: the contentRating axis -- and only that axis --
            # ends with a `None` bucket, after the alphabetical entries, whose fastKey
            # is `?contentRating=None`. It selects exactly the rows with no rating, and
            # plexsim offered no way to reach them from the filter bar at all.
            entries.append({"fastKey": "/library/sections/%s/all?contentRating=None" % section_id,
                            "key": "None", "title": "None"})
        container = self.secondary_container(section_id, section,
                                             SECONDARY_TITLES.get(leaf), Directory=entries)
        if leaf in self.CONTENTLESS_AXES:
            container["MediaContainer"].pop("content", None)
        return container

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
            if collection.get("smart") and "collectionSort" in query:
                # Cycle 15, measured on the copy: a SMART collection refuses
                # `collectionSort` with a 400 -- for `0` as well as `1` -- and the row
                # never gains the field, while `collectionMode` is a 200. It has no
                # collection ORDER at all: its Preferences block carries
                # `collectionFilterBasedOnUser` in that slot instead.
                return ("error", 400, "plexsim: bad request")
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
        if len(segments) == 4 and segments[3] in ("posters", "arts"):
            # Measured on a writable copy of the reference: a playlist's and a collection's
            # artwork list lives under /library/metadata/{ratingKey} like an item's, and
            # holds one `default://` row -- the composite, at the poster size the dialog
            # draws. plexsim 404'd it, so the Poster pane of a list's Edit dialog was empty.
            if method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            target = collection if collection is not None else playlist
            row = (self.library._collection_row(collection) if collection is not None
                   else self.library._playlist_row(target))
            composite = (row.get("composite") or row.get("thumb") or "").split("?")[0]
            entries = []
            if segments[3] == "posters":
                # Cycle 15, measured on the copy: `/arts` is EMPTY on a collection --
                # plexsim handed back the POSTER there, so Change Background offered
                # the poster. And the `default://` row carries no `provider`: the
                # agent rows do, so the omission belongs to this row alone.
                entries = [{"key": "%s?width=400&height=600" % composite,
                            "ratingKey": "default://",
                            "thumb": "%s?width=200&height=300" % composite,
                            "selected": True}]
            return self.tag_container(
                size=len(entries), Metadata=entries, _xml={"Metadata": "Photo"})
        if len(segments) == 4 and segments[3] == "children" and collection is not None:
            # Cycle 14, measured on the copy: a collection answers `/children` on the
            # METADATA path with a full secondary container -- `key` (the bare id),
            # `parentIndex` (the collection's TAG id), `parentTitle` and `title2` (its
            # title), `title1` (the library), the librarySection triple, `nocache`,
            # `viewGroup` the member type and `viewMode` 65592 -- while
            # `/library/collections/{id}/children` beside it answers the BARE
            # `{size: N}` container. plexsim 404'd the metadata form, which is the one
            # the client builds from a collection row's own `key`, so a collection page
            # opened empty.
            if method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            rows = self.library.collection_children(collection)
            section = self.catalog.section(collection.get("librarySectionID")) or {}
            body = self.secondary_container(
                collection.get("librarySectionID"), section, collection.get("title"),
                Metadata=[_listing_row(r) for r in rows],
                viewGroup=collection.get("subtype") or section.get("type"))
            container = body["MediaContainer"]
            container.pop("content", None)
            container["key"] = str(collection["ratingKey"])
            container["nocache"] = True
            container["parentIndex"] = self.library._collection_row(collection).get("index")
            container["parentTitle"] = collection.get("title")
            return body
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
                if query.get("titleSort.locked") is not None:
                    self.library._set_lock(collection, "titleSort",
                                           query["titleSort.locked"] == "1")
                labels = sorted(((name, value) for name, value in query.get("_pairs") or []
                                 if re.match(r"^label\[\d+\]\.tag\.tag$", name)),
                                key=lambda pair: int(pair[0][6:].split("]", 1)[0]))
                if labels:
                    # Measured on a writable copy of the reference: a collection takes a
                    # Label like any item. plexsim ignored the tag.
                    self.library._set_tags(collection, "label", [value for _, value in labels if value])
            else:
                self.library.edit_playlist(playlist, title=title, summary=summary)
            if any(name in query for name in ("title", "title.value", "titleSort.value",
                                              "titleSort.locked")):
                # Measured: a list's Sort Title follows an item's rule.
                self.library._derive_title_sort(target)
            self.notify_list_edited(target, "collection" if collection is not None else "playlist")
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
                Metadata=[self.collection_detail(collection, query)])
        row = self.library._playlist_row(playlist, content=True)
        return self.bare_container(size=1, Metadata=[row])

    def collection_detail(self, collection, query):
        """A collection's detail row, with its Preferences when asked for. Cycle 15."""
        row = self.library._collection_row(collection)
        if (query or {}).get("includePreferences") == "1":
            second = ("collectionFilterBasedOnUser" if collection.get("smart")
                      else "collectionSort")
            stored = collection
            settings = []
            for name in ("collectionMode", second):
                setting = dict(COLLECTION_PREFS[name])
                if name in stored:
                    setting["value"] = str(stored[name])
                settings.append(setting)
            row["Preferences"] = {"Setting": settings}
        return row

    def apply_edit(self, rows, query, section=False, metadata_state="loading"):
        """One edit against one or more rows, with the refusals a real PMS makes.

        The client's Edit dialog saves through `PUT /library/sections/{id}/all`,
        and a batch edit through a comma-joined key list, so the same parameter
        handling has to serve three paths. Every one of them answered the LISTING
        on plexsim, with a 200 -- so the dialog reported success and applied
        nothing, which is the quietest failure on the whole surface.
        """
        try:
            if section:
                for row in rows:
                    self.library.edit(row, query, pairs=query.get("_pairs"), section=True)
                # Measured: a save through the section path frames its rows in LISTING
                # order -- the sort title, whatever order `id=` named them in -- and
                # sends every row's state 3 before any row's state 5. A comma-joined key
                # list keeps the per-row pair, in the order it was given.
                ordered = sorted(rows, key=self._sort_key("titleSort"))
                for row in ordered:
                    self.notify_timeline(row, self.TIMELINE_QUEUED, metadata_state)
                for row in ordered:
                    self.notify_timeline(row, self.TIMELINE_DONE, None)
            else:
                for row in rows:
                    self.library.edit(row, query, pairs=query.get("_pairs"))
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
        if wanted == "season":
            # Re-measured on kids (1.43.4) and zen (1.42.2): a SEASON's title is never
            # compared or sent -- but an EPISODE's is. The earlier reading said both, from
            # a lab whose episodes are titled by the agent identically enough that the
            # difference did not show; on both servers today, several differently titled
            # episodes list `title` in `mixedFields` and a single one sends its title.
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
        if "titleSort" in mixed:
            # Re-measured: only a MIXED titleSort is reported as "". A single season
            # answers its own sort title (`Season 1`) and a single episode the sort form
            # of its title (`The Train Job` -> `Train Job`), derived where the row stores
            # none. plexsim hardcoded "" for every season and episode, so the Edit
            # dialog's Sort Title box came up blank on both and a save could overwrite it.
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

    def library_metadata(self, segments, query, method="GET", body=b""):
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
                return self.library_metadata(segments[:2] + [numeric[0]] + segments[3:],  # noqa: E501
                                             query, method)
            keys = [int(p) for p in raw_key.split(",")
                    if p.isdigit() and int(p) in self.catalog.items]
            if method == "PUT":
                # Measured: a batch EDIT. plexsim answered the listing here, so a
                # multi-select edit applied to nothing and reported success.
                return self.apply_edit([self.catalog.items[k] for k in keys], query)
            fields = {}
            if keys:
                # Measured on both servers: the container says `allowSync` and names
                # the FIRST row's library, even when the list spans two.
                first = self.catalog.items[keys[0]]
                first_section = self.catalog.section(first.get("librarySectionID")) or {}
                fields = {"allowSync": True,
                          "librarySectionID": first.get("librarySectionID"),
                          "librarySectionTitle": first_section.get("title"),
                          "librarySectionUUID": first_section.get("uuid")}
            return self.container(Metadata=[_full(self.catalog.items[k]) for k in keys],
                                  **fields)
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
            if method not in ("GET", "HEAD"):
                # Measured on a writable copy of the reference: a POST to an item is a
                # 404 -- edits are PUTs -- where plexsim answered the detail row.
                return ("error", 404, "plexsim: not found")
            section = self.catalog.section(row["librarySectionID"]) or {}
            detail = _full(row)
            if row.get("type") in ("show", "season"):
                detail = (self.show_detail(row, detail, query)
                          if row["type"] == "show"
                          else self.parent_detail(row, detail, query))
            elif query.get("includeExtras") == "1":
                # Measured: an episode and a movie carry the Extras block too.
                detail["Extras"] = {"size": 0}
            if row.get("type") == "movie":
                # Measured on a writable copy of the reference: a movie's written setting
                # shows on its row as a show's does -- `useOriginalTitle: "1"`.
                detail.update(row.get("_prefs") or {})
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
        if leaf == "file" and method in ("GET", "HEAD") and not query.get("url"):
            # Measured on both servers: the item's file endpoint without its `url` is a
            # 400. (With a `url` the two servers disagree -- 400 on one, 404 on the
            # other -- so that form stays the unrouted 404.)
            return ("error", 400, "plexsim: bad request")
        if leaf == "grandchildren":
            if method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            return self.grandchildren(row, query)
        if leaf in ("children", "allLeaves"):
            if leaf == "children":
                rows = self.catalog.children(rating_key)
                if row.get("type") == "season":
                    # Cycle 14, measured on the copy: the SHOW's `episodeSort=1` puts a
                    # season's `/children` NEWEST FIRST (`10, 8` where the default is
                    # `8, 10`), and `0`/`-1` put it back. `/allLeaves` and the section
                    # listing keep broadcast order either way, so this is the season
                    # page's own order and not a catalog-wide sort. plexsim stored the
                    # setting and never read it.
                    #
                    # Cycle 15: the value is the EFFECTIVE one. Written at the
                    # LIBRARY level -- which is where the Edit Library dialog puts
                    # it -- the show's own setting stays `-1` and inherits, so
                    # reading the item's alone left a library-wide Newest First
                    # ordering nothing at all.
                    parent = self.catalog.items.get(int(row["parentRatingKey"])) \
                        if str(row.get("parentRatingKey") or "").isdigit() else None
                    if parent and self.catalog.show_pref(parent, "episodeSort") == "1":
                        rows = list(reversed(rows))
            elif row.get("type") in ("season", "episode"):
                # Measured on both servers: `/allLeaves` below the SHOW is empty -- a
                # season's is `size 0` with its episode-list container, not the season's
                # episodes, and an episode's is empty too. plexsim returned the season's
                # episodes, so a client walking allLeaves counted them twice.
                rows = []
            else:
                rows = self.catalog.leaves(rating_key)
            extra = {}
            page, paged, start, _ = self.page_window(rows, query)
            # Re-measured on zen against a 24-season show, which is the first show this
            # project has had that can tell the two readings apart: the "All episodes"
            # row is present exactly when the PAGE holds more than one season row --
            # Start=10 Size=10 carries it, Start=0 Size=1 does not -- so the gate is the
            # page's own length and has nothing to do with the offset. Cycle 8 read
            # kids as dropping it past Start=0, but kids has two seasons, so every page
            # past the first there IS a one-row page.
            short_page = paged and len(page) < 2
            if (leaf == "children" and row.get("type") == "show"
                    and query.get("excludeAllLeaves") != "1" and rows
                    and not short_page):
                # Measured: a SHOW's /children carries an extra `Directory` row
                # linking to /allLeaves, titled "All episodes", with the show's leaf
                # counts and poster, and the container's size counts it -- which is what
                # `excludeAllLeaves=1` removes. Re-measured in cycle 8 on both
                # servers, correcting "a paged request drops it": the client's own
                # Start=0 page KEEPS it, outside the page (Size=2 answers it plus two
                # seasons), and only a one-row page drops it. At Start>0 the two
                # servers disagree; this follows the reference and drops it.
                leaves = self.catalog.leaves(rating_key)
                all_episodes = {"leafCount": len(leaves), "thumb": row.get("thumb")}
                if query.get("X-Plex-Platform") == "iOS":
                    # Measured on both servers, in XML and JSON, and the one
                    # client-profile rule a cycle-8 sweep of four client families found:
                    # the iOS PLATFORM -- not the product, not tvOS -- types this row as
                    # a season.
                    all_episodes["type"] = "season"
                all_episodes.update(viewedLeafCount=sum(1 for leaf_row in leaves
                                                        if leaf_row.get("viewCount")),
                                    key="/library/metadata/%s/allLeaves" % rating_key,
                                    title="All episodes")
                extra["Directory"] = [all_episodes]
            return self.child_container(row, rows, leaf, query, **extra)

        # -- the context menu's write half --------------------------------
        if leaf in ("refresh", "analyze", "unmatch", "match", "split", "merge"):
            return self.item_action(row, leaf, method, query)
        if leaf == "matches":
            return self.matches(row, query)
        if leaf in ("art", "poster", "thumb") and not self._owns_artwork(row, leaf):
            # Cycle 16: cycle 15's episode-only rule is the special case of a general
            # one. Measured on kids and zen, an artwork path is a 200 only on the row
            # that OWNS the image -- kids' season 7 borrows its show's background and
            # 404s where zen's season 11114, which has one of its own, answers 200,
            # and every episode borrows. The row's own stored path names its owner, so
            # comparing the two IS the rule, and it needs no per-type list.
            return ("error", 404, "plexsim: not found")
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
            if method == "DELETE":
                # Cycle 15, measured on plexscratch: the DELETE is real. 200 with an
                # EMPTY body, the field gone from the row, whatever artwork is left
                # re-stamped onto a new version, `updatedAt` moved, ONE `state 5`
                # frame with no `metadataState` (the artwork-pick shape) and nothing
                # `selected` in the candidate list afterwards. The bare GET of the
                # deleted path is then a 404.
                #
                # plexsim answered the image BYTES to the DELETE and changed nothing:
                # Remove Poster did nothing, and since Change Poster assigns the
                # response BODY onto the row, clearing the artwork set the tile's img
                # src to a blob of JPEG.
                return self.clear_artwork(row, leaf)
            return ("image-key", row, leaf)
        if leaf in ("posters", "arts"):
            if method == "PUT":
                # Measured: the plural path is a 404 for a write.
                return ("error", 404, "plexsim: not found")
            if method == "POST":
                # Cycle 15, measured on the copy: an empty body is a 406, not the
                # 400 plexsim answered.
                if not body:
                    return ("error", 406, "plexsim: not acceptable")
                return self.upload_artwork(row, leaf, body)
            return self.artwork_list(row, leaf)
        if leaf in ("thumbs", "banners", "themes", "clearLogos") and method == "POST":
            # Cycle 15, measured on the copy: every artwork candidate list answers a
            # POST the same way an upload with no bytes does -- a 406. plexsim
            # answered 404 for two of them and the candidate LISTING for the other
            # two, and a 200 listing reads to a client as an accepted upload.
            if not body:
                return ("error", 406, "plexsim: not acceptable")
            return self.upload_artwork(row, leaf, body)
        if leaf in ("banners", "thumbs"):
            # Cycle 14: two more artwork candidate lists, 200 on kids (1.43.4) AND zen
            # (1.42.2) where plexsim answered 404. `banners` is empty on both servers;
            # `thumbs` lists the video thumbnails out of the media BUNDLE (3 on kids, 1
            # on zen), which plexsim has none of -- so an empty list of the same shape
            # is the honest answer, the way `arts` already answers for a season.
            # `/squareArts` is NOT here on purpose: it is 200 on kids and 404 on zen,
            # so it is 1.43.4-only and plexsim reports 1.42.2.
            if method not in ("GET", "HEAD"):
                return ("error", 404, "plexsim: not found")
            return self.tag_container(size=0, _xml={"Metadata": "Photo"})
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
            if writes and row.get("type") == "show":
                # Cycle 14, re-measured on the copy with a socket open, one setting at a
                # time: the cascade belongs to `showOrdering` ALONE. `showOrdering=dvd`
                # pushes 45 frames -- the show's own refresh sequence and then every
                # season and episode -- because it changes how the episodes are NUMBERED
                # and the server re-reads the tree. `flattenSeasons`, `episodeSort`,
                # `useOriginalTitle` and `subtitleMode` each push **zero** frames in
                # eight seconds on the same socket, which answered a title edit beside
                # them with the usual `state 3` / `state 5` pair.
                #
                # plexsim pushed the cascade for EVERY prefs write -- telling every
                # client the whole tree had been re-read whenever a display flag moved --
                # and changed no row at all. The first version of this fix then swung
                # the other way and silenced `showOrdering` too; the suite's own
                # cycle-13 check caught it, which is what it is there for.
                if any(name == "showOrdering" for name, _ in writes):
                    self.notify_show_cascade(row)
                self.catalog.apply_show_prefs(row)
            return ("empty", 200)
        if leaf == "subtitles":
            if method == "POST":
                return self.upload_subtitle(row, query, body)
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
            if len(segments) > 4:
                # Measured on both servers: `/extras/{n}` is a 404, even for an extra the
                # item has. plexsim answered the extras container at any depth.
                return ("error", 404, "plexsim: not found")
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
                # ...and this one is built by hand rather than by `hub_container`, so
                # it needs the same `allowSync: true` said explicitly. Both references
                # carry true here, as they do on the `/hubs/` form beside it.
                size=len(hubs), Hub=hubs,
                allowSync=True, identifier="com.plexapp.plugins.library",
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
        if leaf in ("clearLogos", "themes"):
            # Measured: both are candidate lists in the tag container -- `Photo`
            # rows for clear logos, `Track` rows for theme music -- and EMPTY for an
            # item with none (every season and episode, a movie with no theme).
            # plexsim has neither, so both are empty; it 404'd them, which
            # python-plexapi's `logos()` and `themes()` raise on.
            return self.tag_container(
                size=0, _xml={"Metadata": "Photo" if leaf == "clearLogos" else "Track"})
        if leaf == "theme":
            # No theme audio; answer empty rather than falling through to the
            # unknown-route handler.
            return ("error", 404, "plexsim: no theme")
        if leaf in ("thumb", "art", "poster"):
            # Cycle 14: `background` and `composite` used to be served here too, and
            # both are a **404** on kids and zen alike, on a movie, a show, a season
            # and an episode, bare and versioned -- while `thumb`, `art` and `poster`
            # are the 200s they always were. A `composite` is real on a PLAYLIST and a
            # COLLECTION, which is where plexsim's own rows point and which is handled
            # elsewhere; on a library ITEM there is no such thing, and `background` is
            # not a path at all -- it is the `type` of an `Image` element.
            #
            # The same sweep that found `/recentlyReleased`: fetch every leaf from both
            # references and from plexsim, and report only where the references agree
            # and plexsim does not.
            # Cycle 15: once the field has been DELETED the path is a 404 on the
            # copy, not a placeholder -- and a 404 lets the client draw its own
            # placeholder where a grey 200 tile gets cached.
            owner = self.catalog.items.get(int(rating_key)) \
                if str(rating_key).isdigit() else None
            # Cycle 16, the same ownership rule on the VERSIONED path: a borrower's
            # own key is a 404 there too, and the stamp in the URL belongs to the
            # owner's path rather than to this row.
            if owner is not None and not self._owns_artwork(owner, leaf):
                return ("error", 404, "plexsim: not found")
            if owner is not None and owner.get("thumb" if leaf in ("thumb", "poster")
                                               else "art") is None:
                return ("error", 404, "plexsim: not found")
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
        generic library container adds.

        Cycle 14: `allowSync` is TRUE here, on kids (1.43.4) and zen (1.42.2) alike.
        Checked across every hub path: `/hubs`, `/hubs/sections/{id}`,
        `/hubs/promoted`, `/hubs/home/recentlyAdded`, `/hubs/home/onDeck`,
        `/hubs/continueWatching`, `/hubs/metadata/{id}/related` and
        `/library/metadata/{id}/related` all say true. The ONE exception is
        `/hubs/metadata/{id}/postplay`, which says false -- so the default flips and
        postplay passes it explicitly. (`/hubs/search` and `/hubs/items` carry no
        `allowSync` at all and are built by a different container, which is why they
        are unaffected.) plexsim defaulted to false and had three call sites putting
        it back, so eight of the ten hub paths said the opposite of a real PMS.
        """
        body = {"size": 0, "allowSync": True, "identifier": "com.plexapp.plugins.library"}
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

    # A shelf's default row cap. Re-measured on zen, the 1.42.2 build plexsim
    # reports, against a 3887-movie and 507-show library: with no `count=` a SHELF
    # carries at most SIX rows and says `more: true` -- on the library tab, on /hubs
    # and on the related shelf alike -- and an explicit `count=` is honoured exactly,
    # so six is the default rather than a ceiling. The recorded 12 was never
    # witnessed: kids holds five movies and one show, so no shelf there reaches six.
    #
    # It is a separate number from HUB_COUNT because the other two users of that
    # constant are not shelves and are not capped at six: `/hubs/home/recentlyAdded`
    # answered all 3887 rows for `type=1` on zen, and the similar-row pool is the
    # candidate list a shelf is then cut from.
    SHELF_COUNT = 6

    # The cap on the two lists that are not shelves; see SHELF_COUNT above.
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
        limit = self.SHELF_COUNT
        if count is not None and str(count).strip().lstrip("-").isdigit():
            limit = max(int(count), 0)
        # Measured: a SHOW on a `mixed` shelf -- Recently Added TV, on the home
        # screen, the library tab and /hubs/promoted -- is the FULL row, Guid,
        # Location, Rating and tag ids included, where every other row on every
        # shelf is the light one.
        full_shows = extra.get("type") == "mixed"
        full_rows = extra.pop("full", False)
        page = [r if full_rows or (full_shows and r.get("type") == "show") else _grid_row(r)
                for r in rows[:limit]]
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
            identifier = "%s.recentlyadded.%s" % ("tv" if is_show else "movie",
                                                  section["key"])
            shelf_title = "Recently Added in %s" % (section.get("title") or "")
            if query.get("pinnedContentDirectoryID"):
                # Measured on zen, the 1.42.2 build plexsim reports: when the client
                # sends `pinnedContentDirectoryID` beside `contentDirectoryID` -- and
                # Plex Web always does, its Promoted fetch builds both -- this shelf
                # comes back under a different identifier, title and key entirely:
                # `home.movies.recent.1` / "Recently Added Movies" keyed on
                # `/hubs/home/recentlyAdded?type=1&pinnedContentDirectoryID=1`, and
                # `home.television.recent.2` / "Recently Added TV" for a show library.
                # kids (1.43.4) ignores the parameter, so this follows zen. plexsim
                # answered the bare shape to both.
                identifier = "home.%s.recent.%s" % ("television" if is_show else "movies",
                                                    section["key"])
                shelf_title = "Recently Added %s" % ("TV" if is_show else "Movies")
                key = ("/hubs/home/recentlyAdded?type=%s&pinnedContentDirectoryID=%s"
                       % (2 if is_show else 1, section["key"]))
            hubs.append(self.home_hub(
                identifier, shelf_title, key,
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

    # `/hubs/metadata/{id}`'s four shelves: (key template, title, type, identifier).
    ITEM_MUSIC_HUBS = (
        ("/library/sections/%s/all?artist.id=%s&type=10&sort=viewCount:desc&viewCount>=10",
         "Most Played", "track", "artist.mostplayedtracks"),
        ("/library/sections/%s/all?artist.id=%s&type=10&sort=ratingCount:desc&group=title",
         "Most Popular Tracks", "track", "artist.mostpopulartracks"),
        ("/library/sections/%s/all?artist.id=%s&type=9&sort=year:desc",
         "Albums", "album", "artist.albums"),
        ("/library/sections/%s/extras/all?extraType=4&type=8&artist.id=%s",
         "Music Videos", "clip", "music.videos.popular.new"),
    )

    def item_music_hubs(self, rating_key):
        """`/hubs/metadata/{id}` -- an item's own hub list.

        Measured on both servers, for a movie, a show and an episode alike: the four
        ARTIST shelves (Most Played, Most Popular Tracks, Albums, Music Videos), every
        one empty, keyed to this item in its own library as if it were an artist. An
        unknown id is a 404. plexsim 404'd the path, which the community OpenAPI
        description names `getMetadataHubs`.
        """
        row = self.catalog.items.get(int(rating_key)) if str(rating_key).isdigit() else None
        if row is None:
            return ("error", 404, "plexsim: no such item")
        hubs = [{"key": template % (row.get("librarySectionID"), row.get("ratingKey")),
                 "title": title, "type": kind, "hubIdentifier": identifier,
                 "context": "hub.%s" % identifier, "size": 0, "more": False, "style": "shelf"}
                for template, title, kind, identifier in self.ITEM_MUSIC_HUBS]
        return self.hub_container(Hub=hubs, allowSync=True)

    def hub_items(self, query):
        """`/hubs/items?identifier=` -- one shelf's rows, uncapped.

        Measured on both servers: the shelf a library or Home hub list names by
        `hubIdentifier` (`movie.recentlyadded.1`, `home.ondeck`), with all its rows --
        `count` caps them -- in a container of `size`, `allowSync` true and identifier;
        the rows name their library as the shelf's do. A missing or unknown identifier
        is a 404, and so, measured, are `tv.inprogress.{id}` and `home.playlists`, which
        the hub lists do carry. plexsim 404'd every identifier.
        """
        identifier = query.get("identifier") or ""
        count = query.get("count") or "100000"
        if not identifier or identifier.startswith("tv.inprogress.") or identifier == "home.playlists":
            return ("error", 404, "plexsim: not found")
        built = None
        head, _, tail = identifier.rpartition(".")
        if identifier.startswith(("movie.", "tv.")) and tail.isdigit() \
                and self.catalog.section(tail) is not None:
            built = self.section_hubs(tail, count)
        elif identifier.startswith("home."):
            built = self.hubs({"identifier": identifier, "count": count})
        hubs = ((built or {}).get("MediaContainer") or {}).get("Hub") or [] \
            if isinstance(built, dict) else []
        hub = next((h for h in hubs if h.get("hubIdentifier") == identifier), None)
        if hub is None:
            return ("error", 404, "plexsim: not found")
        rows = self._excluded_elements(hub.get("Metadata") or [], query)
        return self.hub_container(size=len(rows), allowSync=True, Metadata=rows)

    def item_hubs(self, rating_key, leaf, query):
        """`/hubs/metadata/{id}/{related,postplay}` -- the preplay page's shelves.

        The client rewrites `/library/metadata/{id}` to `/hubs/metadata/{id}` and
        appends the leaf, so these are not the same endpoints as
        `/library/metadata/{id}/related`. plexsim routed neither, and a 404 is
        what the preplay page reads as "no related items" -- silently.

        Measured: `related` carries the librarySection triple, `postplay` carries
        none of it, and the hub inside them is `movie.similar` keyed on the item's
        own `/similar`.

        Cycle 14 corrects the `allowSync` half of that sentence: postplay DOES carry
        `allowSync`, as **false**, on both references -- its container is
        `allowSync`, `identifier`, `size`. It is the only hub container that is not
        true, which is why it is the one that has to say so here.
        """
        if leaf not in ("related", "postplay"):
            return ("error", 404, "plexsim: not found")
        row = self.catalog.items.get(int(rating_key)) if str(rating_key).isdigit() \
            else None
        if row is None:
            return ("error", 404, "plexsim: no such item")
        hubs = self.related_hubs(row, query)
        if leaf == "postplay":
            return self.hub_container(Hub=hubs, allowSync=False)
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
        # Measured on both servers, and overturning cycle 7's "a movie or episode is
        # light on every shelf": a Related shelf -- on both related endpoints, on
        # postplay and inside includeRelated=1 -- carries the FULL row: Guid, Rating,
        # Field, tag ids and roles, the Part's Streams and the librarySection triple.
        # python-plexapi built those items with no guids, ratings or streams.
        return [self.home_hub(
            "%s.similar" % kind, "Related %s" % ("Shows" if kind == "show" else "Movies"),
            "/library/metadata/%s/similar" % row["ratingKey"], rows, query, kind=kind,
            full=True)]

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

    def section_hubs(self, section_id, count=None, library_playlists=False):
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
        # Each of the three shelves added below is built from its own key, which is
        # the listing its "see all" opens: Top Unwatched is unwatched films rated 7.0
        # or better, best first; Start Watching is the shows with no episode watched or
        # in progress; Top Rated TV is the shows with an unwatched episode, rated 7.0
        # or better. All three keys were read verbatim off zen.
        def _rated(rows_in):
            return sorted([r for r in rows_in if (r.get("audienceRating") or 0) >= 7.0],
                          key=lambda r: -(r.get("audienceRating") or 0))
        top_unwatched = _rated([r for r in items if not r.get("viewCount")])
        shows_here = [r for r in items if r.get("type") == "show"]
        unstarted = [r for r in shows_here
                     if not any(leaf.get("viewCount") or leaf.get("viewOffset")
                                for leaf in self.catalog.leaves(r.get("ratingKey")))]
        top_rated = _rated([r for r in shows_here
                            if (r.get("leafCount") or 0) > (r.get("viewedLeafCount") or 0)])
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
                # Measured on zen, which is the 1.42.2 build plexsim reports: a show
                # library carries "Start Watching" here -- the shows with no episode
                # watched or in progress -- and "Top Rated TV" after Rediscover. kids
                # (1.43.4) builds neither although its own library qualifies for both,
                # so they are a version difference and not an absence.
                shelf("tv.startwatching.%s" % section_id, "Start Watching",
                      unstarted, "show",
                      "/library/sections/%s/all?type=2&viewedOrInProgressLeafCount=0"
                      % section_id, "hub.tv.startwatching", random=True),
                shelf("tv.rediscover.%s" % section_id, "Rediscover", rediscover, "show",
                      "/library/sections/%s/all?type=2&lastViewedAt<=-6w"
                      "&lastViewedAt>=-5y&unwatchedLeaves=1&viewCount>=1"
                      % section_id, "hub.tv.rediscover", random=True),
                shelf("tv.toprated.%s" % section_id, "Top Rated TV", top_rated, "show",
                      "/library/sections/%s/all?type=2&unwatchedLeaves=1"
                      "&audienceRating>=7.0&sort=audienceRating:desc" % section_id,
                      "hub.tv.toprated", random=True),
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
                "movie.topunwatched.%s" % section_id, "Top Unwatched Movies",
                top_unwatched, "movie",
                "/library/sections/%s/all?unwatched=1&audienceRating>=7.0"
                "&sort=audienceRating:desc" % section_id,
                "hub.movie.topunwatched", random=True))
            hubs.append(shelf(
                "movie.recentlyviewed.%s" % section_id, "Recently Watched Movies",
                watched, "movie",
                "/library/sections/%s/all?sort=lastViewedAt:desc&unwatched=0"
                "&viewOffset=0" % section_id, "hub.movie.recentlyviewed"))

        if library_playlists:
            # Measured: `includeLibraryPlaylists=1` -- which the client's own hub
            # request always sends -- adds a Recent Playlists shelf right after the
            # library's Recently Added. plexsim ignored the parameter, so the tab
            # could never show one. Only its empty form was measurable (no playlists
            # on either server); what fills it is not recorded, so it stays empty.
            position = next((index + 1 for index, hub in enumerate(hubs)
                             if ".recentlyadded." in hub["hubIdentifier"]), len(hubs))
            hubs.insert(position, shelf(
                "recent.library.playlists.%s" % section_id, "Recent Playlists", [],
                "playlist",
                "/playlists/all?type=15&playlistType=video&sort=lastViewedAt:desc"
                "&sectionID=%s" % section_id, "hub.recent.library.playlists"))
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
    # Re-measured on kids (1.43.4) and zen (1.42.2) with four queries that match
    # NOTHING -- the only way to see the base order, because a query with fuzzy hits
    # promotes the hubs that scored. Both servers give this order exactly. The two
    # plexsim had wrong were the first one: `movie` is fifth, not first, and
    # `collection` is fourteenth, between genre and playlist, not seventh.
    SEARCH_HUBS = [
        ("show", "Shows"), ("artist", "Artists"), ("album", "Albums"),
        ("episode", "Episodes"), ("movie", "Movies"), ("track", "Tracks"),
        ("photoalbum", "Photo Albums"), ("autotag", "Automatic Tags"),
        ("photo", "Photos"), ("tag", "Tags"), ("actor", "Actors"),
        ("director", "Directors"), ("genre", "Genres"),
        ("collection", "Collections"), ("playlist", "Playlists"),
        ("shared", "Shared"), ("place", "Places"),
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

    def search_matches(self, text, limit, tagged_items=False):
        """(typed row lists, tag hits) for one query, both ranked.

        `tagged_items` adds the ITEMS a matched tag is on to the typed lists.
        Measured on both servers: `/hubs/search` carries them -- kids `colin`
        answers Firefly reasoned on its actor and Sintel on its director, and 53 of
        81 item rows across ten zen queries carried a `reason` -- while
        `/library/search` carries the tag rows and the title hits only. So it is
        the hubs endpoint that asks for them, not the matcher that always finds
        them.
        """
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
        if tagged_items:
            seen_keys = {str(r.get("ratingKey")) for r in rows}
            for row in self.catalog.items.values():
                if str(row.get("ratingKey")) in seen_keys:
                    continue
                for field in self.SEARCH_TAG_HUBS.values():
                    if any(any(w.startswith(needle)
                               for w in re.split(r"[^0-9a-z]+", str(tag.get("tag") or "").lower())
                               if w)
                           for tag in row.get(field) or []) and len(needle) >= 2:
                        typed.setdefault(row.get("type"), []).append(row)
                        seen_keys.add(str(row.get("ratingKey")))
                        break
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
        # Cycle 15, measured on kids (1.43.4) and zen (1.42.2): `contentDirectoryID`
        # SCOPES this endpoint to one library and VALIDATES its value -- `abc`, `99`,
        # `1.5` and `-1` are all 404s, an EMPTY value is a 200 that scopes nothing,
        # and a real section id keeps that library's hubs alone. It belongs to
        # `/hubs/search` only: `/hubs`, `/hubs/promoted` and `/hubs/sections/{id}`
        # ignore it on both references. plexsim ignored it everywhere, so "search in
        # this library" answered the whole server and a bad id read as "no results".
        wanted_section = query.get("contentDirectoryID")
        if wanted_section:
            if self.catalog.section(wanted_section) is None:
                return ("error", 404, "plexsim: not found")
        else:
            wanted_section = None
        # Measured on kids (1.43.4) and zen (1.42.2): the DEFAULT cap is 3 PER HUB, not
        # 30. A zen search answering 6 movies, 26 directors, 11 episodes and 50 actors
        # under `limit=50` answers three of each with no limit at all, and `more` stays
        # false either way. plexsim's 30 handed the type-ahead path -- and every client
        # that omits `limit` -- a results page many times the size a real PMS sends,
        # which changes how the search page lays its shelves out.
        # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), identical on both:
        # this endpoint refuses in TWO ways. A value it cannot parse -- `abc`, an
        # empty one, `1.5` -- is a **400**; one that parses but is not a positive
        # count -- `-1`, `0` -- is a **500**; `01` is fine. plexsim had the two
        # statuses the wrong way round and let an empty value through.
        if "limit" in query:
            raw_limit = str(query.get("limit") or "")
            if not raw_limit.lstrip("-").isdigit():
                return ("error", 400, "plexsim: bad request")
            if int(raw_limit) <= 0:
                return ("error", 500, "plexsim: internal error")
        limit = int(query.get("limit", 3) or 3)
        typed, tags = self.search_matches(text, limit, tagged_items=True)
        if wanted_section is not None:
            typed = {kind: [r for r in rows
                            if str(r.get("librarySectionID")) == str(wanted_section)]
                     for kind, rows in typed.items()}
            tags = {hub: [t for t in rows
                          if str(t.get("librarySectionID")) == str(wanted_section)]
                    for hub, rows in tags.items()}
        needle = text.strip().lower()

        hubs = []
        for kind, title in self.SEARCH_HUBS:
            rows = []
            for row in typed.get(kind, []):
                # Measured: a search row is the light projection, and a show keeps
                # its Location in it.
                entry = _grid_row(_listing_row(row), keep_location=row.get("type") == "show")
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
            if kind == "collection":
                # Cycle 15, measured on the copy: a collection is found HERE, as a
                # `tag` row with `tagType: 2` and the collection's own guid, and it is
                # NOT found by `/library/search` -- which refutes cycle 14's note.
                # plexsim's collection hub was empty for every query, so the search
                # box never found a collection at all.
                found = []
                for section in self.catalog.sections:
                    # `collections()` already answers ROWS, not the stored dicts.
                    for row in self.library.collections(section["key"]):
                        words = re.split(r"[^0-9a-z]+",
                                         str(row.get("title") or "").lower())
                        if not any(w.startswith(needle) for w in words if w):
                            continue
                        found.append(self._collection_tag_result(row, section))
                if found:
                    hub["Directory"] = found
                    hub["size"] = len(found)
            elif kind in tags:
                hub["Directory"] = [self._tag_result(entry, kind)
                                    for entry in tags[kind]]
                hub["size"] = len(hub["Directory"])
            hubs.append(hub)
        return self.bare_container(size=len(hubs), Hub=hubs)

    def _collection_tag_result(self, row, section):
        """A collection as a search `tag` row. Cycle 15, measured on the copy."""
        section_id = row["librarySectionID"]
        return {
            "key": "/library/sections/%s/all?collection=%s" % (section_id, row["index"]),
            "librarySectionID": int(section_id) if str(section_id).isdigit() else section_id,
            "librarySectionKey": "/library/sections/%s" % section_id,
            "librarySectionTitle": section.get("title"),
            "librarySectionType": 2 if section.get("type") == "show" else 1,
            "reason": "section",
            "reasonID": int(section_id) if str(section_id).isdigit() else section_id,
            "reasonTitle": section.get("title"),
            "score": self._score(0.52),
            "type": "tag",
            "id": row["index"],
            "filter": "collection=%s" % row["index"],
            "tag": row.get("title"),
            # Measured: a COLLECTION tag row is tagType 2, beside the actor's 6 and
            # the director's 4.
            "tagType": 2,
            "count": row.get("childCount") or 0,
            "guid": row.get("guid"),
        }

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
            # Plex's own tagType codes, re-measured on kids (1.43.4) and zen (1.42.2):
            # an ACTOR row is 6 and a DIRECTOR row is 4 -- they are different tag types,
            # not one "person" type. plexsim called both 4, while `/library/people/{id}`
            # already answered 6 for the same person, so the server contradicted itself
            # and a client keying artwork or a filter off the search row's tagType built
            # the wrong link.
            "tagType": {"actor": 6, "director": 4}.get(hub, 1),
            "count": len(entry["rows"]),
        }
        if tag.get("thumb"):
            result["thumb"] = tag["thumb"]
        return result

    @staticmethod
    def _search_result_row(row):
        """A `/library/search` hit.

        Measured on both servers: the light projection -- text tags, no Guid, Rating
        or Streams -- naming its library by TITLE only, except a show, which keeps the
        id and key beside the title, and its Location. plexsim sent the full row with
        the id and key on every hit.
        """
        shown = _grid_row(_listing_row(row), keep_location=row.get("type") == "show")
        if row.get("type") != "show":
            shown.pop("librarySectionID", None)
            shown.pop("librarySectionKey", None)
        return shown

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
        limit = int(query.get("limit", 10) or 10)
        typed, tags = self.search_matches(text, limit)
        needle = text.strip().lower()
        results = []
        for kind, _title in self.SEARCH_HUBS:
            for row in typed.get(kind, []):
                score = 0.95 if str(row.get("title") or "").lower() == needle \
                    else 0.73 if str(row.get("title") or "").lower().startswith(needle) \
                    else 0.33
                results.append({"score": float(self._score(score)),
                                "Metadata": self._search_result_row(row)})
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
        # Measured on both servers: `type=N` keeps only that type's hub (`type=4` is the
        # episode hub alone, and a type with no hub -- 5, 8, 18 -- answers none), and a
        # hub's identifier is `results.search.<type number>`, not its position. plexsim
        # ignored `type` and numbered hubs 1, 2, ... so a show hub came out as the movie
        # identifier.
        wanted_type = query.get("type")
        hubs = []
        for kind, _title in self.SEARCH_HUBS:
            rows = typed.get(kind)
            if not rows:
                continue
            number = TYPE_NUMBERS.get(kind)
            if wanted_type is not None and str(number) != wanted_type:
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
                "results.search.%s" % number, kind, None,
                entries, query, kind=kind, distance=distance))
            # Measured: a voice hub carries `hubKey` and NO `key`, and its context
            # is the shared `hub.results.search` rather than one per hub.
            hubs[-1].pop("key", None)
            hubs[-1]["context"] = "hub.results.search"
        return self.bare_container(size=len(hubs), Hub=hubs)

    def _foreign_uri(self, uri):
        """Whether a `server://` uri names a machine that is not this one.

        Measured on the copy: `server://<other machine>/com.plexapp.plugins.library/...`
        adds nothing at all, where a uri with the same machine and a different PROVIDER
        segment is added normally. plexsim read the ratingKey out of any uri shaped like
        one and added the item whoever it belonged to.
        """
        if not str(uri).startswith("server://"):
            return False
        machine = str(uri)[len("server://"):].split("/", 1)[0]
        return bool(machine) and machine != self.catalog.machine

    def _items_for_uri(self, uri, continuous=False, expand=True):
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
            # `limit` is part of the query a smart list stores, measured on a writable copy
            # of the reference: a list saved with `sort=titleSort:desc&limit=2` reads back
            # two rows and its `leafCount` follows. plexsim applied the filter and the sort
            # and ignored the limit, so every capped smart list was the whole section.
            if str(inner.get("limit") or "").isdigit() and int(inner["limit"]) > 0:
                rows = rows[:int(inner["limit"])]
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
            # `expand=False` is a collection's add: measured, a show collection holds the
            # SHOW, not its episodes.
            return self._playable(rows) if expand else rows
        # A bare trailing ratingKey, which some clients send.
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit() and int(tail) in self.catalog.items:
            rows = [self.catalog.items[int(tail)]]
            return self._playable(rows) if expand else rows
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

    def create_play_queue(self, query, headers=None):
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
        named = re.match(r"server://([^/]+)/", urllib.parse.unquote(uri or ""))
        foreign = bool(named) and named.group(1) != self.catalog.machine
        if foreign and playlist is None:
            # Measured on a writable copy of the reference: a uri naming ANOTHER server
            # builds an EMPTY queue that echoes the uri verbatim. plexsim resolved the
            # path against its own library and queued its own item.
            rows = []
        elif playlist is not None:
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

        client = (query.get("X-Plex-Client-Identifier")
                  or (headers or {}).get("X-Plex-Client-Identifier") or "")
        with self.lock:
            if client:
                # Measured on a writable copy of the reference: a client's new queue
                # RETIRES its older ones -- reading one afterwards is a 404 -- while a
                # queue from another client leaves them alone. plexsim kept every queue.
                for old in [key for key, known in self.play_queues.items()
                            if known.get("client") == client]:
                    del self.play_queues[old]
            queue_id = self.next_play_queue
            self.next_play_queue += 1
            items = self._queue_items(rows)
            selected = items[0]["id"] if items else None
            wanted = str(query.get("key") or "").rsplit("/", 1)[-1]
            if wanted.isdigit():
                # Measured on a writable copy of the reference: `key=` names the item the
                # queue starts ON -- a queue over a whole library with
                # `key=/library/metadata/3` selects that movie, where plexsim selected
                # the first row and the player started the wrong film.
                selected = next((e["id"] for e in items if str(e["ratingKey"]) == wanted), selected)
            self.play_queues[queue_id] = {
                "items": items,
                "source": ("" if playlist is not None
                           else uri if foreign else self.source_uri(uri, rows)),
                "shuffled": query.get("shuffle") == "1",
                "client": client,
                "version": 1,
                "selected": selected,
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
            # Measured on a writable copy of the reference: `includeBefore` and
            # `includeAfter` (both on by default) choose the sides of the window, and the
            # selected item itself comes back only when both are on -- after-only answers
            # just the rows after it. plexsim ignored both flags.
            before = query.get("includeBefore") != "0"
            after = query.get("includeAfter") != "0"
            metadata = ((metadata[low:offset] if before else [])
                        + (metadata[offset:offset + 1] if before and after else [])
                        + (metadata[offset + 1:offset + span + 1] if after else []))
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
        if (offset is not None and not str(offset).isdigit()) \
                or (duration is not None and not str(duration).isdigit()) \
                or (offset is not None and duration is not None and int(duration)
                    and int(offset) > int(duration)):
            # Measured on a writable copy of the reference: a negative or unparseable
            # `time` or `duration`, and a `time` past the `duration` sent with it, are
            # each a 400. plexsim answered 200 -- and a time past the duration
            # completed the play and wrote a history row.
            return ("error", 400, "plexsim: bad request")
        offset_ms = int(offset) if str(offset).isdigit() else 0
        # Measured: `duration=0` is a 200 that stores nothing -- a real PMS reads it as
        # "no usable duration", the same as sending none, where plexsim's `time > duration`
        # refusal caught it as a 400.
        total_ms = int(duration) if str(duration).isdigit() and int(duration) else None
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
            # A completed play answers `complete` to every beat past 90% until a
            # `stopped` beat has closed it, and `ignore` to every beat after that.
            already = (row.get("_completed") == (playing_session or True)
                       and row.get("_completionStopped"))
            self.library.progress(int(rating_key), offset_ms, state=state,
                                  duration=total_ms, device_id=device,
                                  session=playing_session)
            if state == "stopped" and row.get("_completed") == (playing_session or True):
                row["_completionStopped"] = True
            entry = self.sessions.touch(row, query, headers, address, state,
                                        offset_ms, total_ms)
            self.notify_playing(entry, row, state, offset_ms,
                                self.playing_queue_fields(query))
            # Measured thresholds: nothing is reported back at or under a minute, on
            # ANY state, and past 90% the play is `complete` on any state too.
            # `viewCount` is the item's own count -- 0 on a first play in progress,
            # measured -- where plexsim sent 1 for anything past a minute.
            # The beat's own duration, as the library uses: with none, nothing is stored
            # and nothing completes, and the answer carries the item's stored offset.
            total = total_ms or 0
            if offset_ms > 60000 and total:
                playback_state = "progress"
                if offset_ms >= total * 0.9:
                    # Past 90% the answer is never `progress`: it is `complete` for the beat
                    # that completed the play and every one after it until a `stopped` closes
                    # it, and `ignore` for a beat that completed nothing -- one that did not
                    # cross into the last tenth inside this session. plexsim called every
                    # near-end beat `complete`, and the crossing rule alone would have left
                    # the refused ones saying `progress`.
                    finished = row.get("_completed") == (playing_session or True)
                    playback_state = "complete" if finished and not already else "ignore"
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

    def playing_queue_fields(self, query):
        """The play-queue pair a `playing` frame carries when the session has one.

        Measured on a writable copy of the reference: every frame of a session driven from a
        play queue -- playing, paused, a seek and the stop -- carries `playQueueItemID` and
        `playQueueID`, and a session with no queue carries neither. plexsim sent neither
        ever, and the Now Playing panel and companion clients tie a frame to the queue item
        they hold by exactly these two.
        """
        wanted = query.get("playQueueItemID")
        if not str(wanted or "").isdigit():
            return {}
        for queue_id, queue in list(self.play_queues.items()):
            if any(item["id"] == int(wanted) for item in queue.get("items") or []):
                return {"playQueueItemID": int(wanted), "playQueueID": queue_id}
        return {}

    def notify_playing(self, entry, row, state, offset_ms, extra=None):
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
            **(extra or {}),
        }])

    def session_rows(self):
        """`/status/sessions` -- the item row plus its session blocks."""
        rows = []
        for entry in self.sessions.live():
            row = entry.get("row") or {}
            item = _full(row)
            for name in self.sessions.SESSION_ROW_DROPS:
                item.pop(name, None)
            item["sessionKey"] = entry["sessionKey"]
            item["viewOffset"] = entry["viewOffset"]
            # The selected flags a real session row carries on its own media -- set on
            # COPIES. `_full` copies only the row's top level, so writing them into its
            # Media wrote them into the catalog: after one play every detail, listing
            # and search row of the item said `selected` and `directplay`, which no read
            # outside a session carries on either real server.
            media_rows = []
            for media in item.get("Media") or []:
                parts = []
                for part in media.get("Part") or []:
                    played = dict(part, decision="directplay", selected=True)
                    if part.get("Stream"):
                        played["Stream"] = [dict(stream, location="direct")
                                            for stream in part["Stream"]]
                    parts.append(played)
                media_rows.append(dict(media, selected=True, Part=parts))
            if media_rows:
                item["Media"] = media_rows
            item["User"] = entry["User"]
            item["Player"] = entry["Player"]
            item["Session"] = entry["Session"]
            rows.append(item)
        return rows

    def _keyed_row(self, query, validate=None):
        """The row a `key=` parameter names, or the refusal a real PMS answers.

        Measured on every one of these state writes: a MISSING key is a 400 and a
        key the server does not have is a 404. plexsim answered an empty 200 to
        both, which tells the client the write landed. Re-measured on a writable copy
        of the reference with the `identifier` the client always sends taken away:
        the checks run in a FIXED order -- a missing key is a 400, then a missing
        identifier is a 400 (even for a key the server does not have), then the
        write's own parameter (`validate`), and only then is a foreign identifier or
        an unknown key a 404. plexsim never read the identifier.
        """
        raw = query.get("key")
        if raw is None or raw == "":
            return None, ("error", 400, "plexsim: bad request")
        if not query.get("identifier"):
            return None, ("error", 400, "plexsim: bad request")
        refusal = validate() if validate is not None else None
        if refusal:
            return None, refusal
        key = str(raw).rsplit("/", 1)[-1]
        row = self.catalog.items.get(int(key)) if key.isdigit() else None
        if row is None or query.get("identifier") != "com.plexapp.plugins.library":
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
        offset = query.get("time")
        # Measured on a writable copy of the reference: a missing or unparseable `time`
        # is a 500 here, where plexsim stored 0 and answered 200.
        row, refusal = self._keyed_row(query, validate=lambda: None if str(offset or "").isdigit()
                                       else ("error", 500, "plexsim: bad time"))
        if refusal:
            return refusal
        self.library.progress(int(row["ratingKey"]), int(offset),
                              state=query.get("state") or "stopped", completes=False)
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
        rating = query.get("rating")
        try:
            value = float(rating)
        except (TypeError, ValueError):
            value = None
        # Measured: an unparseable rating is a 400, where plexsim silently cleared the
        # rating instead -- and it is refused before a foreign identifier or an unknown
        # key would be.
        row, refusal = self._keyed_row(query, validate=lambda: None if value is not None
                                       else ("error", 400, "plexsim: bad request"))
        if refusal:
            return refusal
        self.library.rate(int(row["ratingKey"]), value)
        logger.info("RATE %s -> %s", row["ratingKey"], rating)
        return ("empty", 200)

    def remove_from_continue_watching(self, query, method="PUT"):
        """The context-menu entry of the same name: drop the resume point.

        Advertised as an Action on the `actions` provider feature; the client
        only draws the entry when that Action is present.
        """
        if method == "GET":
            # Measured on a writable copy of the reference: the action is a WRITE -- a
            # GET is a 404 -- and a PUT that names no ratingKey is a 400. plexsim hid
            # the row on a GET and 404'd the bare PUT.
            return ("error", 404, "plexsim: not found")
        if not (query.get("ratingKey") or query.get("key")):
            return ("error", 400, "plexsim: bad request")
        key = (query.get("ratingKey") or query.get("key") or "").rsplit("/", 1)[-1]
        if not key.isdigit() or self.library.remove_from_continue_watching(
                int(key)) is None:
            # Measured: an unknown ratingKey is a 404 here, where plexsim answered
            # an empty 200 for anything at all.
            return ("error", 404, "plexsim: no such item")
        return self.bare_container(size=0)

    def continue_watching(self):
        """Rows for Continue Watching and On Deck, newest first.

        A movie is there while it has a resume point. A SHOW contributes one row --
        its in-progress episode, or with none in progress the episode after the
        highest-numbered one watched. Removing any of its episodes from Continue
        Watching hides the show's row, and a timeline on any of them brings it back.
        plexsim listed every episode with a resume point, and never the next one.

        WHICH in-progress episode was recorded in cycle 9 as "the LOWEST season and
        episode number (not the one played last)". Cycle 14 re-measured it on the copy
        in `long` mode and it is the other way round: the show contributes its **most
        recently viewed** in-progress episode. A beat on episode 8 at 40% puts 8 on
        deck; a `/:/progress` on episode 10 -- a LATER one, still unwatched -- moves
        the row to 10; a fresh beat on 8 moves it back to 8. Three transitions, and
        `/library/onDeck`, `/library/sections/{id}/onDeck`, `/hubs/home/onDeck` and
        `/hubs/continueWatching` all agree each time.

        Cycle 9 measured this on the copy's TEN-SECOND clips, where every beat crosses
        90% at once and an episode can barely be left in progress -- the handover's own
        "re-run anything about sessions, completion or resume from cycles 9 or earlier
        on `long`" entry, paying off.
        """
        rows = [r for r in self.catalog.items.values()
                if r.get("viewOffset") and r.get("type") == "movie"
                and not r.get("_hiddenFromContinueWatching")]
        shows = {}
        for row in self.catalog.items.values():
            if row.get("type") == "episode":
                shows.setdefault(str(row.get("grandparentRatingKey")), []).append(row)
        stamps = {}
        for episodes in shows.values():
            if any(e.get("_hiddenFromContinueWatching") for e in episodes):
                continue
            episodes.sort(key=lambda e: (e.get("parentIndex") or 0, e.get("index") or 0))
            # The most recently viewed of the in-progress ones, not the lowest-numbered.
            # `max` keeps the FIRST maximal element and `episodes` is already in
            # (season, episode) order, so two beats inside one second -- `lastViewedAt`
            # has one-second resolution -- fall back to the lowest-numbered. That
            # tie-break is a choice, not a measurement.
            in_progress = [e for e in episodes if e.get("viewOffset")]
            chosen = max(in_progress, key=lambda e: e.get("lastViewedAt") or 0) \
                if in_progress else None
            if chosen is None:
                watched = [n for n, e in enumerate(episodes) if e.get("viewCount")]
                chosen = next((e for e in episodes[watched[-1] + 1:] if not e.get("viewCount")),
                              None) if watched else None
            if chosen is not None:
                rows.append(chosen)
                stamps[id(chosen)] = max(e.get("lastViewedAt") or 0 for e in episodes)
        return sorted(rows, key=lambda r: -stamps.get(id(r), r.get("lastViewedAt") or 0))

    def save_smart_query(self, target, query, kind):
        """`PUT {list}/items?uri=<listing>` on a smart list -- the Edit dialog's Save.

        Measured on a writable copy of the reference, for a smart playlist and a smart
        collection alike: the stored query is REPLACED by the uri and the list re-evaluated
        on the next read, so its `leafCount` and its items follow the new `sort` and `limit`
        at once. The response is the list's own row. A uri naming an ITEM, and no uri at all,
        are 400s. Only the uri is stored: both row builders derive `content` from it -- a
        playlist rewriting it into `library://x/directory/...` and a collection keeping it
        verbatim -- and re-evaluate the query on every read.
        """
        uri = query.get("uri") or ""
        if not uri or "/library/metadata/" in uri:
            return ("error", 400, "plexsim: bad request")
        target["uri"] = uri
        target["updatedAt"] = int(time.time())
        logger.info("%s %s query <- %s", kind.upper(), target["ratingKey"], uri[:120])
        if kind == "playlist":
            return self.bare_container(size=1, Metadata=[self.library._playlist_row(target)])
        return self.bare_container(size=1, Metadata=[self.library._collection_row(target)])

    # What the Upload Subtitle modal is allowed to send, measured one extension at a
    # time on a writable copy of the reference: these five are accepted and `vtt`,
    # `sub`, `idx`, `sup`, `pgs`, an unknown extension and an empty one are 400s.
    # Re-measured one extension at a time on the copy, extending cycle 11's set:
    # `psb` is accepted too, and the check is CASE-SENSITIVE -- `format=SRT` and a
    # `title=x.SRT` are both 400 where the lowercase forms are 200. plexsim lowercased
    # the value, so it accepted uploads a real PMS refuses. The client's own allowed
    # list is wider than the server's (`aqt jss rt utf utf-8 utf8 sub idx vtt` are
    # offered by the modal and refused by the server), which is the client's business.
    SUBTITLE_UPLOAD_FORMATS = ("srt", "ass", "ssa", "smi", "txt", "psb")

    def upload_subtitle(self, row, query, body):
        """`POST /library/metadata/{id}/subtitles?title=&format=` -- the Upload modal.

        The client (bundle module 90487) sends the file's name as `title`, its extension
        as `format` and the bytes as the body. Measured on a writable copy of the
        reference: a well-formed upload answers 200 with an EMPTY body and leaves a new
        subtitle `Stream` on the item -- streamType 3, the format as both `codec` and
        `format`, the name with its extension stripped as `title`, a `key` of
        `/library/streams/{id}`, `transient: "0"` -- and that new track becomes the
        SELECTED subtitle, deselecting the one before it. The refusals are their own
        grammar: no `title` at all, and a title with no extension, are 500s; a format
        the server does not take is a 400; an empty body is a 406; an unknown item is a
        404. plexsim fell through to the subtitle SEARCH branch and answered 400 to
        every one of them, so the modal always reported "Unable to upload subtitle file"
        -- and cycle 10 recorded the endpoint as unmeasurable on the copy, from a
        request shape the client does not send.
        """
        # Cycle 15, measured on the copy: the EMPTY-BODY check runs FIRST. With
        # neither a title nor bytes the answer is 406, not the 500 a missing title
        # gets on its own -- plexsim had the two the other way round.
        if not body:
            return ("error", 406, "plexsim: not acceptable")
        title = str(query.get("title") or "")
        if not title or "." not in title:
            return ("error", 500, "plexsim: internal error")
        stem, _, extension = title.rpartition(".")
        # Measured on the copy: the format is compared AS SENT -- `SRT` is a 400.
        fmt = str(query.get("format") or extension)
        if fmt not in self.SUBTITLE_UPLOAD_FORMATS:
            return ("error", 400, "plexsim: bad request")
        if not body:
            return ("error", 406, "plexsim: not acceptable")
        with self.lock:
            used = [int(stream.get("id") or 0)
                    for item in self.catalog.items.values()
                    for media in item.get("Media") or []
                    for part in media.get("Part") or []
                    for stream in part.get("Stream") or []]
            stream_id = max(used or [0]) + 1
            parts = [part for media in row.get("Media") or []
                     for part in media.get("Part") or []]
            if not parts:
                return ("error", 404, "plexsim: not found")
            stream = {"id": stream_id, "streamType": 3, "codec": fmt, "format": fmt,
                      "title": stem, "key": "/library/streams/%d" % stream_id,
                      "transient": "0", "selected": True}
            mediainfo_module.stream_titles(stream)
            for part in parts:
                for other in part.get("Stream") or []:
                    if other.get("streamType") == 3:
                        other.pop("selected", None)
            parts[0].setdefault("Stream", []).append(stream)
            self.uploaded_subtitles[stream_id] = body
        logger.info("SUBTITLE upload %s -> stream %d (%s)", row.get("ratingKey"), stream_id, fmt)
        # Measured on the copy: an upload pushes NO frames at all -- 0 in a 15-second
        # watch and 0 in a 60-second one, `/activities` stays empty and the item's
        # `updatedAt` does not move. The client refetches the row itself afterwards with
        # `?skipRefresh=1`. Cycle 11 added an edit pair here without measuring it, which
        # is this project's own class-C mistake: every other client on the socket was
        # told the item had been edited when nothing about it had changed.
        return ("empty", 200)

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
            if row.get("type") in ("show", "season"):
                # Cycle 15, re-measured on the copy with a control write on the same
                # socket first: a refresh CASCADES. The episode form is the seven
                # frames below; a SEASON runs its episodes inside its own activity
                # (18 frames on a three-episode season) and a SHOW nests a season's
                # whole block under each season (49 on the two-season Firefly tree).
                # plexsim sent seven at every level, so Refresh Metadata on a show
                # told the client one row had changed and left the tree stale.
                if row["type"] == "show":
                    self.notify_show_cascade(row)
                else:
                    self.notify_season_cascade(row)
                self.catalog.restamp_children(row)
                logger.info("REFRESH %s (%s) -- cascaded", row["ratingKey"],
                            row.get("title"))
                return ("empty", 200)
            # The FULL seven-frame sequence a real PMS pushes. One timeline(3) is
            # not enough: without state 5 the client never refetches the row it just
            # refreshed, and without the final frame clearing `metadataState` its
            # spinner never stops.
            self.notify_refreshed(row)
            logger.info("REFRESH %s (%s)", row["ratingKey"], row.get("title"))
        elif action == "analyze":
            # The catalog rows already carry ffprobe-derived Media attributes; there
            # is no second, better analysis to run -- but the FRAMES are what a client
            # watches for, and plexsim pushed none.
            #
            # Cycle 15, re-measured on the copy with a control write first: per LEAF
            # and in tree order, three `state 5` frames carrying `mediaState`
            # `analyzing`, `thumbnailing` and none, then ONE
            # `media.generate.chapter.thumbs` activity whose subtitle is the TARGET's
            # frame title. 5 frames for an episode or a movie, 11 for a
            # three-episode season, 21 for the two-season show. Cycle 13's
            # "per-LEVEL triple" was wrong: a season and a show row are never framed
            # themselves, only their leaves.
            leaves = (self.catalog.leaves(row["ratingKey"])
                      if row.get("type") in ("show", "season") else [row])
            for leaf in leaves:
                for media_state in ("analyzing", "thumbnailing", None):
                    self.notify_timeline(leaf, self.TIMELINE_DONE, None,
                                         media_state=media_state)
            activity = self.library.add_activity(
                self._frame_title(row), title="Generating video chapter thumbnails",
                kind="media.generate.chapter.thumbs", progress=-1)
            # started then ENDED, with no `updated` between them -- measured. The
            # refresh sequence's `finish_activity` inserts one, so this ends the
            # activity itself rather than borrowing that helper.
            activity["progress"] = 100
            self.library.end_activity_quietly(activity)
            self.notify_queue_regenerated()
            logger.info("ANALYZE %s -- %d leaf frame set(s)",
                        row["ratingKey"], len(leaves))
        elif action == "unmatch":
            self.library.unmatch(row)
            if row.get("type") in ("episode", "season"):
                # Measured: an unmatch pushes `matching` (state 2) and then the refresh it
                # causes -- an episode straight to state 5, a season through the full
                # refresh sequence.
                self.notify_timeline(row, 2, "matching")
                if row["type"] == "season":
                    self.notify_refreshed(row)
                else:
                    self.notify_timeline(row, self.TIMELINE_DONE, None)
            logger.info("UNMATCH %s -> guid=%s", row["ratingKey"], row.get("guid"))
        elif action == "match":
            guid = query.get("guid") or ""
            if query.get("name") is None:
                # Measured on a writable copy of the reference: the NAME is what this
                # endpoint requires -- a guid with no name is a 400, and an empty guid with
                # a name is a 200 that renames the row and refreshes it. plexsim had the
                # test the other way round.
                return ("error", 400, "plexsim: match needs a name")
            # Matching to a guid another row in the same library already carries MERGES this
            # row into that one: measured, the matched row is deleted and the survivor keeps
            # both Media. plexsim left two rows claiming one identity.
            existing = next((other for other in list(self.catalog.items.values())
                             if guid and other is not row and other.get("guid") == guid
                             and str(other.get("librarySectionID")) == str(row.get("librarySectionID"))),
                            None)
            self.notify_timeline(row, self.TIMELINE_QUEUED, None)
            if existing is not None:
                merged = self.library.merge(existing, [row["ratingKey"]])
                self.drop_from_queues(int(row["ratingKey"]))
                for other in merged:
                    self.notify_timeline(dict(other, title="", year=None),
                                         self.TIMELINE_DELETED, "deleted")
                self.notify_timeline(existing, self.TIMELINE_QUEUED, None, type_number=-1)
                self.notify_timeline(existing, self.TIMELINE_DONE, None, type_number=-1)
                self.library.refresh(existing)
                self.notify_refreshed(existing)
                logger.info("MATCH %s merged into %s (%s)", row["ratingKey"],
                            existing["ratingKey"], guid)
            else:
                self.library.apply_match(row, guid, query.get("name"), query.get("year"),
                                         identify=self._identify_guid if guid else None)
                self.library.refresh(row)
                # Every match runs the refresh sequence, activity and all, and it ENDS.
                self.notify_refreshed(row)
                logger.info("MATCH %s -> %s", row["ratingKey"], guid or "(name only)")
        elif action == "split":
            created = self.library.split(row)
            # Measured: each new row is created -- state 0 untitled, state 0 titled, state
            # 1 -- and the source then refreshed, state 3 then 5, with no metadataState.
            for clone in created:
                self.notify_timeline(dict(clone, title="", year=None), self.TIMELINE_CREATED, "created")
                self.notify_timeline(clone, self.TIMELINE_CREATED, "created")
                self.notify_timeline(clone, 1, "created")
            if created:
                self.notify_timeline(row, self.TIMELINE_QUEUED, None)
                self.notify_timeline(row, self.TIMELINE_DONE, None)
            logger.info("SPLIT %s -> %d new row(s)", row["ratingKey"], len(created))
        elif action == "merge":
            ids = [i for i in (query.get("ids") or "").split(",") if i]
            others = [self.catalog.items.get(int(i)) if i.isdigit() else None for i in ids]
            # Measured on a writable copy of the reference: a merge with no `ids` is a
            # 400, an id the server does not have a 404, and a row of another type -- a
            # movie into a show -- a 400. plexsim answered 200 to all three, and moved
            # the movie's media into the show.
            if not ids:
                return ("error", 400, "plexsim: bad request")
            if any(other is None for other in others):
                return ("error", 404, "plexsim: no such item")
            if any(other.get("type") != row.get("type") for other in others):
                return ("error", 400, "plexsim: bad request")
            merged = self.library.merge(row, ids)
            # Measured on a writable copy of the reference: the absorbed row is deleted
            # (state 9, untitled) and the survivor refreshed under type -1, state 3 then
            # 5. plexsim pushed nothing.
            for other in merged:
                self.notify_timeline(dict(other, title="", year=None), self.TIMELINE_DELETED, "deleted")
            if merged:
                self.notify_timeline(row, self.TIMELINE_QUEUED, None, type_number=-1)
                self.notify_timeline(row, self.TIMELINE_DONE, None, type_number=-1)
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

    def clear_artwork(self, row, leaf):
        """Remove one artwork field, re-stamp the rest, and frame it. Cycle 15."""
        field = "thumb" if leaf in ("poster", "thumb") else leaf
        stamp = int(time.time())
        had = row.pop(field, None)
        row.pop("_poster_url" if field == "thumb" else "_art_url", None)
        for other in ("thumb", "art"):
            # Measured: the artwork that is LEFT moves to a new version stamp, the
            # way an artwork PICK re-versions every field it keeps.
            if other != field and row.get(other):
                row[other] = "/library/metadata/%s/%s/%s" % (row["ratingKey"], other, stamp)
        if had is not None:
            row["updatedAt"] = stamp
            self.catalog.restamp_children(row)
            self.notify_timeline(row, self.TIMELINE_DONE, None)
        return ("empty", 200)

    def artwork_list(self, row, kind):
        """`/library/metadata/{id}/{posters,arts}` -- the Change Artwork picker.

        A real PMS lists every candidate the agent found, each with `selected`
        on the current one. plexsim has exactly what TMDB gave it plus its own
        generated placeholder, and says so rather than padding the list.
        """
        if kind == "arts" and row.get("type") in ("season", "episode"):
            # Measured: a season's and an episode's background candidates are their
            # OWN (none on the lab, fanart.tv's on fun) -- never the show's. plexsim
            # offered the show's backdrop as the item's selected choice.
            return self.tag_container(size=0, _xml={"Metadata": "Photo"})
        entries = []
        field = "thumb" if kind == "posters" else "art"
        stored = row.get("_poster_url") if kind == "posters" else row.get("_art_url")
        # An uploaded image is a candidate of its own, and stays one after something else
        # is picked -- measured: the copy's list keeps every upload it has been given.
        uploads = [key for key in self.uploads
                   if key.startswith("upload://posters" if kind == "posters" else "upload://art")
                   and (row.get("type") not in ("season", "episode") or str(row.get("index")) in key)]
        for key in uploads:
            entries.append({"key": key, "ratingKey": key, "thumb": key,
                            "selected": stored == key, "provider": "local"})
        if stored in uploads:
            stored = None
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

    def cascade_artwork(self, row):
        """Copy a row's re-versioned artwork onto the children that point at it.

        Measured on the copy after a poster pick on a show: the season's
        `parentThumb` and `art` and the episode's `grandparentThumb`,
        `grandparentArt` and `grandparentTheme` all move to the show's new stamp,
        while the season's own `thumb` does not. The same shape one level down for a
        pick on a SEASON: its episodes' `parentThumb` follows and the show is
        untouched.
        """
        kind = row.get("type")
        if kind not in ("show", "season"):
            return
        for child in self.catalog.children(row["ratingKey"]):
            if kind == "show":
                for name in ("thumb", "art", "theme"):
                    target = "parent" + name.capitalize() if name == "thumb" else name
                    if row.get(name) and child.get(target) is not None:
                        child[target] = row[name]
            else:
                if row.get("thumb") and child.get("parentThumb") is not None:
                    child["parentThumb"] = row["thumb"]
            for leaf in self.catalog.leaves(child["ratingKey"]) if kind == "show" else []:
                for name in ("thumb", "art", "theme"):
                    target = "grandparent" + name.capitalize()
                    if row.get(name) and leaf.get(target) is not None:
                        leaf[target] = row[name]

    def upload_artwork(self, row, kind, body):
        """`POST /library/metadata/{id}/{posters,arts}` with the image as the body.

        Measured on a writable copy of the reference: the answer is an EMPTY 200, the image
        becomes an `upload://posters/<sha1 of the bytes>` candidate (an episode's and a
        season's carry their place in the path) and is selected at once, the row's thumb AND
        art both re-version, and one timeline frame -- state 5, no metadataState -- is
        pushed, with no state 3 before it. plexsim answered the candidate LIST and stored
        nothing, so an upload looked like it worked and changed no artwork.
        """
        if not body:
            return ("error", 400, "plexsim: bad request")
        prefix = "upload://posters" if kind == "posters" else "upload://art"
        if row.get("type") == "season":
            prefix += "/seasons/%s" % row.get("index")
        elif row.get("type") == "episode":
            prefix += "/seasons/%s/episodes/%s" % (row.get("parentIndex"), row.get("index"))
        key = "%s/%s" % (prefix, hashlib.sha1(body).hexdigest())
        self.uploads[key] = (body, _image_type(body))
        field = "thumb" if kind == "posters" else "art"
        row["_poster_url" if kind == "posters" else "_art_url"] = key
        stamp = int(time.time())
        for name in ("thumb", "art"):
            if row.get(name):
                row[name] = "/library/metadata/%s/%s/%d" % (row["ratingKey"], name, stamp)
        row.setdefault(field, "/library/metadata/%s/%s/%d" % (row["ratingKey"], field, stamp))
        logger.info("ARTWORK upload %s %s %d bytes -> %s", row["ratingKey"], kind, len(body), key)
        self.notify_timeline(row, self.TIMELINE_DONE, None)
        return ("empty", 200)

    def set_artwork(self, row, kind, query):
        """`PUT /library/metadata/{id}/{posters,arts}?url=` -- pick one."""
        chosen = urllib.parse.unquote(query.get("url") or "")
        if not chosen:
            return ("error", 400, "plexsim: no url")
        field = "thumb" if kind == "posters" else "art"
        private = "_poster_url" if kind == "posters" else "_art_url"
        # Measured on a writable copy of the reference: whatever is picked -- a remote
        # candidate, an `upload://` one, even one the server does not have -- the row's
        # artwork becomes a NEW versioned path of its own and the response body is that
        # path. plexsim stored the url ITSELF for anything but an http one, so picking an
        # upload set the row's `thumb` to `upload://posters/...` and the client pointed the
        # tile's img src at a string no server serves.
        placeholder = "/library/metadata/%s/%s/1" % (row["ratingKey"], field)
        if chosen == placeholder:
            row.pop(private, None)
        else:
            row[private] = chosen
        # Cycle 14, re-measured on the copy with a socket open: a pick re-versions
        # `thumb`, `art` AND `theme` together onto ONE new stamp, and moves `updatedAt`
        # with them, even though only the poster was chosen. The new stamp then
        # CASCADES: the season's `parentThumb` and `art` and the episode's
        # `grandparentThumb`, `grandparentArt` and `grandparentTheme` all follow, while
        # the season's OWN `thumb` is untouched. The picked field is LOCKED
        # (`Field: [{locked: true, name: thumb}]`), and exactly one frame goes out --
        # `timeline type=2 state=5`, no `metadataState`.
        #
        # plexsim re-versioned only the field that was picked, left the children
        # pointing at the old stamp -- which is how a show wore its old poster on every
        # season and episode tile after a change -- locked nothing and pushed nothing.
        stamp = int(time.time())
        for name in ("thumb", "art", "theme"):
            if row.get(name):
                row[name] = "/library/metadata/%s/%s/%d" % (row["ratingKey"], name, stamp)
        row.setdefault(field, "/library/metadata/%s/%s/%d" % (row["ratingKey"], field, stamp))
        row["updatedAt"] = stamp
        fields = [f for f in row.get("Field") or [] if f.get("name") != field]
        row["Field"] = fields + [{"locked": True, "name": field}]
        self.cascade_artwork(row)
        logger.info("ARTWORK %s %s -> %s", row["ratingKey"], kind, chosen[:80])
        self.notify_timeline(row, self.TIMELINE_DONE, None)
        # Measured: the body is the NEW artwork path, bare -- `/library/metadata/1/
        # thumb/1789241428` -- because the client assigns the response straight onto
        # the model's `thumb`. An empty 200 there blanks the tile.
        return ("text", 200, row[field])

    # The show settings the tree carries, measured: the show's eleven minus the three
    # a show shares with a movie.
    TREE_SHOW_SETTINGS = ("episodeSort", "autoDeletionItemPolicyUnwatchedLibrary",
                          "autoDeletionItemPolicyWatchedLibrary", "flattenSeasons",
                          "showOrdering", "audioLanguage", "subtitleLanguage", "subtitleMode")

    def metadata_tree(self, row, root=None):
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
                if row.get("type") == "episode":
                    # Measured on both servers: an episode's part names its item's
                    # type number first; a movie's part does not.
                    entry = dict([("metadataType", 4)] + list(entry.items()))
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
        # Measured on both servers: a season's title is always EMPTY here, and every
        # node's parentTitle is the ROOT's -- the show's title throughout a season's
        # tree, and empty throughout a show's (which is where an episode's tree is
        # rooted). plexsim sent each row's own titles.
        root = root or row
        item = {
            "id": int(row["ratingKey"]),
            "title": "" if row.get("type") == "season" else (row.get("title") or ""),
            "guid": row.get("guid") or "",
            "parentTitle": (root.get("parentTitle") or "") if root.get("type") == "season" else "",
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
        children = [self._tree_item(child, root)
                    for child in self.catalog.children(row["ratingKey"])]
        if children:
            # Measured: the tree NESTS -- a show holds its seasons, each season
            # its episodes, each episode its MediaItem. plexsim answered one flat
            # row with an empty MediaItem list, so Get Info on a show or a season
            # had nothing under it.
            item["MetadataItem"] = children
        if row.get("type") == "show":
            # Measured on both servers: a show's node ends with eight of its settings,
            # after its seasons, every value and default a STRING.
            stored = row.get("_prefs") or {}
            known = {s.get("id"): s for s in ITEM_PREFS.get("show") or []}
            settings = []
            for name in self.TREE_SHOW_SETTINGS:
                if name not in known:
                    continue
                setting = dict(known[name])
                if name in stored:
                    setting["value"] = stored[name]
                for field in ("default", "value"):
                    value = setting.get(field)
                    setting[field] = "" if value is None else (
                        ("1" if value else "0") if isinstance(value, bool) else str(value))
                settings.append(setting)
            if settings:
                item["Setting"] = settings
        return self.tag_container(size=1, MetadataItem=[item])

    def _tree_item(self, row, root=None):
        """One node of the tree, with its own children beneath it."""
        inner = self.metadata_tree(row, root)["MediaContainer"]["MetadataItem"][0]
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
                if query.get("title") is None or not query.get("sectionId"):
                    # Measured on a writable copy of the reference: a collection create
                    # needs a `title` and a `sectionId` -- each missing one is a 400 --
                    # where plexsim made an "Untitled" collection in the first row's
                    # library. A missing `uri` is a 200 on both servers.
                    return ("error", 400, "plexsim: bad request")
                # Measured on a writable copy of the reference: a collection over a show
                # holds the SHOW (`subtype: show`, one child) and one over a season holds
                # the season, where plexsim expanded both into episodes.
                rows = self._items_for_uri(query.get("uri", ""), expand=False)
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
                                     "subtype": collection.get("subtype"),
                                     "librarySectionID": collection["librarySectionID"],
                                     "title": ""})
                return ("empty", 200)
            if method == "PUT":
                # `title.value=` is an empty title too, which `or` turned into None.
                title = query.get("title.value")
                if title is None:
                    title = query.get("title")
                summary = query.get("summary.value")
                if summary is None:
                    summary = query.get("summary")
                self.library.edit_collection(collection, title=title, summary=summary)
                self.notify_list_edited(collection, "collection")
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
                Metadata=[self.collection_detail(collection, query)],
            )

        leaf = segments[3]
        if leaf == "prefs":
            # Cycle 15, measured on the copy: a 404 on a smart collection and on a
            # plain one alike. plexsim answered an empty 200, which the client reads
            # as "this endpoint exists and is empty" -- so it never looks for the
            # settings, which ride in the `includePreferences=1` block instead.
            return ("error", 404, "plexsim: not found")
        if leaf in ("posters", "arts") and method in ("GET", "HEAD"):
            # Cycle 15, measured on the copy: both carry the container's identifying
            # triple, and `/posters` lists exactly one candidate -- the collection's
            # own composite, keyed `default://`, `selected`, and with NO `provider`
            # (the agent rows do carry one, so its absence is specific to this row).
            # `/arts` is empty. plexsim answered a bare `{size: 0}` to both, so
            # Change Poster on a collection page offered nothing at all.
            entries = []
            if leaf == "posters":
                composite = ("/library/collections/%s/composite/%d"
                             % (collection["ratingKey"], collection["updatedAt"]))
                entries = [{"key": composite + "?width=400&height=600",
                            "ratingKey": "default://",
                            "thumb": composite + "?width=200&height=300",
                            "selected": True}]
            return self.bare_container(
                size=len(entries),
                identifier="com.plexapp.plugins.library",
                mediaTagPrefix="/system/bundle/media/flags/",
                mediaTagVersion=1655122614,
                Metadata=entries, _xml={"Metadata": "Photo"})
        if leaf in ("children", "items") and method == "GET":
            # Measured: `/items` answers the same members as `/children` -- plexsim
            # answered an empty container to it, so the Edit Collection dialog's
            # member list was blank.
            # Measured on a writable copy of the reference: a collection's members come
            # back as the LIGHT grid row -- no Guid, no Rating, no Field, a Part with no
            # Stream list and text-only tags -- while the same item's detail read is
            # full. plexsim sent the detail row here and on a playlist's items, the two
            # list endpoints the grid projection was never applied to, so every Edit
            # Collection dialog and every playlist page carried the whole stream table.
            rows = [_grid_row(_listing_row(r))
                    for r in self.library.collection_children(collection)]
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
            # Measured: the move answers the collection ROW, where plexsim sent nothing.
            return self.bare_container(
                size=1, Metadata=[self.library._collection_row(collection)])
        if leaf == "items" and method == "PUT" and collection.get("smart"):
            # The Edit-smart-collection Save, the same as a playlist's above.
            return self.save_smart_query(collection, query, "collection")
        if leaf == "items" and method == "PUT":
            rows = self._items_for_uri(query.get("uri", ""), expand=False)
            if not rows or any(
                    r.get("type") != collection.get("subtype") for r in rows):
                # Measured on a writable copy of the reference: a smart collection's
                # members are its query, and an add with no uri, an unknown key, or a row
                # of another type -- an episode into a movie collection -- is a 400.
                # plexsim added whatever the uri named.
                return ("error", 400, "plexsim: bad request")
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
                # Measured on a writable copy of the reference: a create needs a
                # `title`, a `type` and a `uri` (each missing one is a 400), a type
                # outside video/audio/photo is a 500, and a type that does not fit what
                # the uri names -- `audio` or `photo` over a movie -- is a 400. plexsim
                # created an "Untitled" list of whatever it was sent.
                if any(query.get(name) is None for name in ("title", "type", "uri")):
                    return ("error", 400, "plexsim: bad request")
                if query["type"] not in ("video", "audio", "photo"):
                    return ("error", 500, "plexsim: bad playlist type")
                rows = self._items_for_uri(query.get("uri", ""))
                if query["type"] != "video" and rows:
                    return ("error", 400, "plexsim: bad request")
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
            answer = self.paged_container(rows, query)
            if query.get("includeMeta") == "1":
                # Measured on both references, byte-identical: the playlist page's
                # own sort menu comes from here, and plexsim sent no Meta at all.
                answer["MediaContainer"]["Meta"] = PLAYLIST_META
            return answer

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
                self.notify_list_edited(playlist, "playlist")
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
            if playlist.get("smart") and len(segments) == 3 and method == "PUT":
                # Measured on a writable copy of the reference: this is the Edit-smart-
                # playlist Save. A LISTING uri REPLACES the stored query and answers the
                # playlist row, re-evaluated; an ITEM uri and no uri are 400s, because a
                # smart playlist's items are its query. plexsim refused every uri, so Save
                # answered "There was a problem updating this item."
                return self.save_smart_query(playlist, query, "playlist")
            if playlist.get("smart") and ((len(segments) == 3 and method == "DELETE")
                                          or (len(segments) == 4 and method == "DELETE")):
                # A smart playlist's items cannot be removed or cleared either.
                return ("error", 400, "plexsim: bad request")
            if len(segments) == 4 and method == "DELETE":
                if not self.library.remove_playlist_item(playlist, segments[3]):
                    # Measured: an item id the playlist does not hold is a 404 -- and a
                    # ratingKey is not an item id -- where plexsim answered 200.
                    return ("error", 404, "plexsim: no such playlist item")
                return self.bare_container(
                    size=1, Metadata=[self.library._playlist_row(playlist)])
            if len(segments) == 3 and method == "DELETE":
                # Measured: DELETE on the items CLEARS the playlist. plexsim answered the
                # item listing and removed nothing.
                self.library.clear_playlist(playlist)
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
                # The light grid row, as measured on the copy -- see the collection's
                # children above; a playlist item keeps its librarySection fields.
                entry = _grid_row(_listing_row(row))
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

        if len(segments) == 3 and segments[2] == "items" and method == "DELETE":
            # Measured on a writable copy of the reference: DELETE on the items clears
            # the queue down to its SELECTED item -- the one playing -- and steps the
            # version. plexsim answered 404.
            with self.lock:
                queue["items"] = [e for e in queue["items"] if e["id"] == queue["selected"]]
                queue["version"] += 1
            return self.play_queue_container(queue_id, query)

        if len(segments) == 4 and segments[2] == "items" and method == "DELETE":
            # Measured on a writable copy of the reference: deleting the SELECTED item
            # answers the queue as it stood -- the old version, selection and total --
            # with no rows, and the next read has the selection on the item that
            # followed it. plexsim left the selection on the deleted id.
            stale = None
            if str(queue.get("selected")) == str(segments[3]):
                stale = self.play_queue_container(queue_id, query)
                if isinstance(stale, dict):
                    stale["MediaContainer"].pop("Metadata", None)
                    stale["MediaContainer"]["size"] = 0
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
                if stale is not None:
                    following = items[index] if index < len(items) else (items[-1] if items else None)
                    queue["selected"] = following["id"] if following else None
            return stale if stale is not None else self.play_queue_container(queue_id, query)

        if len(segments) == 2 and method == "PUT":
            if not query.get("uri") and not query.get("playlistID"):
                # Measured: a bare refresh PUT with no uri is a 400.
                return ("error", 400, "plexsim: bad request")
            rows = ([] if self._foreign_uri(query.get("uri", ""))
                    else self._items_for_uri(query.get("uri", ""),
                                             continuous=query.get("continuous") == "1"))
            if not query.get("uri") and query.get("playlistID"):
                # Measured on a writable copy of the reference: `playlistID=` adds that
                # playlist's items as a plain add. plexsim stepped the version and added
                # nothing.
                playlist = self.library.playlist(query["playlistID"])
                rows = [row for row, _item in self.library.playlist_items(playlist)] if playlist else []
            if not rows:
                # Measured on the copy: an add that adds NOTHING leaves the queue exactly
                # as it was -- `playQueueVersion` does not move, and neither does
                # `playQueueLastAddedItemID`. plexsim stepped the version on every no-op,
                # and the client DISCARDS a queue response whose version it does not
                # expect, so a no-op add invalidated the queue the player was holding.
                # The return sits OUTSIDE the lock: play_queue_container takes the same
                # one and it is not reentrant.
                return self.play_queue_container(queue_id, query)
            with self.lock:
                items = queue["items"]
                new = self._queue_items(rows)
                current = next((i for i, e in enumerate(items)
                                if e["id"] == queue["selected"]), 0)
                if query.get("next") == "1":
                    at = current + 1
                else:
                    # Skip past the items ALREADY ADDED behind the current one -- the Up
                    # Next block. Re-measured on a writable copy of the reference: a
                    # create of [A, B, C] with A playing, then a plain add of D, answers
                    # [A, D, B, C]; a second plain add of E answers [A, D, E, B, C], and a
                    # `next=1` add goes in front of that block. plexsim tested
                    # `id > selected` as a proxy for "added", but every ORIGINAL member
                    # also has an id above the first item's, so it skipped the whole queue
                    # and appended at the very end -- Add to Queue from a grid dropped the
                    # row at the bottom instead of into Up Next. The flag is internal:
                    # play_queue_container copies only `playQueueItemID` onto a row.
                    at = current + 1
                    while at < len(items) and items[at].get("added"):
                        at += 1
                for item in new:
                    item["added"] = True
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
                # Measured on a writable copy of the reference: an `after` the queue does
                # not hold -- `0`, an unknown id -- moves the item to the FRONT, as `-1`
                # does. plexsim answered 404 and left it where it was.
                items.insert(0 if target is None else target + 1, moving)
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
        if "FriendlyName" in dict(typed):
            # Measured on a writable copy of the reference: the name written here is the
            # server's name on `/`, `/media/providers` and `/servers` at once, and an EMPTY
            # one is kept as the preference's value while the displayed name falls back to
            # the host name. plexsim stored the preference and kept its old name everywhere,
            # so renaming the server in Settings changed nothing the client draws.
            self.catalog.friendly = dict(typed)["FriendlyName"] or socket.gethostname()
        # One `preference` frame per written id, in ASCII order of the id, whether or not
        # the value changed -- the shape `/:/prefs/get` answers, label and summary emptied.
        # The client's alerts list and the Console consume these; plexsim pushed none.
        current = {s.get("id"): s for s in self.server_prefs()}
        for name, _value in sorted(typed):
            setting = current.get(name)
            if setting is None:
                continue
            setting = {k: v for k, v in setting.items() if k != "enumValues"}
            setting.update(label="", summary="")
            self.notify("preference", "Setting", [setting])
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
        with `includeFiles` present (any value; `=1` adds the files) -- without it,
        and for anything that is not base64, it is a 400 -- and a path that does
        not exist is an empty 200. plexsim
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
        # Measured: `includeFiles` must be PRESENT, whatever its value -- `=0`
        # answers the directories and `=1` adds `File` rows. Only its absence is a
        # 400.
        if "includeFiles" not in query or not path.startswith("/"):
            return ("error", 400, "plexsim: bad request")
        wanted = path.rstrip("/") or "/"
        children = sorted(tree.get(wanted, ()),
                          key=lambda child: child.rsplit("/", 1)[-1].lower())
        rows = [entry(child) for child in children]
        files = []
        if query.get("includeFiles") == "1":
            files = sorted({str(part.get("file")) for row in self.catalog.items.values()
                            for media in row.get("Media") or []
                            for part in media.get("Part") or []
                            if part.get("file")
                            and str(part.get("file")).rsplit("/", 1)[0] == wanted},
                           key=lambda name: name.rsplit("/", 1)[-1].lower())
        file_rows = [entry(name) for name in files]
        return self.bare_container(size=len(rows) + len(file_rows), Path=rows,
                                   File=file_rows)

    # `/library/tags?type=N`: the tag table for one tag type. Measured types; every
    # other number is an empty container, and no type or a non-number is a 400.
    TAG_TYPES = {"1": ("Genre", "genre"), "2": ("Collection", "collection"),
                 "4": ("Director", "director"), "5": ("Writer", "writer"),
                 "6": ("Role", "actor"), "7": ("Producer", "producer"),
                 "8": ("Country", "country"), "319": ("Network", "network")}

    # A person's tag element, the filter field it answers with, and its tagType.
    PERSON_TAGS = (("Role", "actor", 6), ("Director", "director", 4),
                   ("Writer", "writer", 5), ("Producer", "producer", 7))

    def person(self, person_id, media, query=None):
        """`/library/people/{id}` and `/library/people/{id}/media`.

        Measured on both servers. The person is one `Directory` row -- id, a `filter`
        naming the field the id came from (`actor=47`, `director=213`), tag, tagType,
        and tagKey and thumb where it has them -- in a container of identifier and the
        media-tag pair. `/media` lists the items that person is on, as listing rows that
        name their library by id, key and title. A non-numeric or unknown id is a 404.
        plexsim 404'd both, although every Role it serves carries the id to ask with.

        Re-measured in cycle 11 on kids (1.43.4) and zen (1.42.2), correcting the line
        above: `viewGroup`/`viewMode` belong to an ACTOR's media list only. Measured
        across eight people on the two servers, a Role (tagType 6) carries both -- even
        when its rows are shows, which is where the earlier reading came from -- and a
        Director, Writer or Producer list carries neither. And the list is a QUERY: the
        library filters apply to it, so `?year=` narrows it where plexsim answered every
        row the person is on.
        """
        if not str(person_id).isdigit():
            return ("error", 404, "plexsim: not found")
        wanted = str(int(person_id))
        found, rows, seen = None, [], set()
        for row in self.catalog.items.values():
            for element, field, tag_type in self.PERSON_TAGS:
                for tag in row.get(element) or []:
                    if not isinstance(tag, dict) or str(tag.get("id")) != wanted:
                        continue
                    if found is None:
                        found = {"id": tag.get("id"), "filter": "%s=%s" % (field, tag.get("id")),
                                 "tag": tag.get("tag"), "tagType": tag_type}
                        for extra in ("tagKey", "thumb"):
                            if tag.get(extra):
                                found[extra] = tag[extra]
                    if row.get("ratingKey") not in seen:
                        seen.add(row.get("ratingKey"))
                        rows.append(row)
        if found is None:
            return ("error", 404, "plexsim: not found")
        common = {"identifier": "com.plexapp.plugins.library",
                  "mediaTagPrefix": "/system/bundle/media/flags/", "mediaTagVersion": 1655122614}
        if not media:
            return self.bare_container(size=1, Directory=[found], **common)
        rows, _ = self.apply_filters(rows, query or {})
        rows, _ = self._sorted(rows, "titleSort")
        shown = self._excluded_elements([_grid_row(_listing_row(r)) for r in rows], query)
        if found.get("tagType") != 6:
            return self.bare_container(size=len(shown), Metadata=shown, **common)
        return self.bare_container(size=len(shown), viewGroup="movie", viewMode=65592,
                                   Metadata=shown, **common)

    def library_matches(self, query):
        """`/library/matches` -- does this library already hold an item?

        plexsim advertises this key in /media/providers (`type: match`) and refused every
        request to it. Measured on both servers:

        - `type` is required and numeric (none, or `abc`, is a 400);
        - `title` matches a movie or show title EXACTLY, ignoring case -- a partial title
          matches nothing -- narrowed by `year`, and each row leads with `score` 100;
        - `guid` matches the whole guid (a prefix matches nothing), and an episode
          matches by `grandparentTitle` + `parentIndex` + `index`; neither carries a score;
        - `type` alone, a season title and an episode title all answer `size 0`.

        Rows are the listing's own; the container is nothing but `size`.
        """
        raw = query.get("type") or ""
        if not raw.lstrip("-").isdigit():
            return ("error", 400, "plexsim: bad request")
        wanted = {"1": "movie", "2": "show", "3": "season", "4": "episode"}.get(str(int(raw)))
        rows = [r for r in self.catalog.items.values() if wanted and r.get("type") == wanted]
        scored = False
        if query.get("guid"):
            rows = [r for r in rows if r.get("guid") == query["guid"]]
        elif wanted == "episode" and query.get("grandparentTitle"):
            rows = [r for r in rows
                    if str(r.get("grandparentTitle") or "").lower() == query["grandparentTitle"].lower()
                    and str(r.get("parentIndex")) == str(query.get("parentIndex"))
                    and str(r.get("index")) == str(query.get("index"))]
        elif query.get("title") and wanted in ("movie", "show"):
            title = query["title"].lower()
            rows = [r for r in rows if str(r.get("title") or "").lower() == title
                    and (not query.get("year") or str(r.get("year")) == str(query["year"]))]
            scored = True
        else:
            rows = []
        rows, _ = self._sorted(rows, "titleSort")
        shown = []
        for r in rows:
            row = _grid_row(_listing_row(r), keep_section=False)
            shown.append(dict([("score", 100)] + list(row.items())) if scored else row)
        return self.bare_container(size=len(shown), Metadata=shown)

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
        if str(int(kind)) == "318":
            # Measured on both servers: studios are a tag table too, whose filter is a
            # bare `=<id>`, ordered by id. plexsim keeps a studio as text on the row, so
            # it mints the id from the name the way it mints every other tag's.
            names = {row.get("studio") for row in self.catalog.items.values()
                     if row.get("studio")}
            entries = sorted(({"id": zlib.crc32(("Studio:%s" % name).encode()) % 900000 + 1000,
                               "tag": name} for name in names), key=lambda entry: entry["id"])
            rows = [{"id": entry["id"], "filter": "=%d" % entry["id"], "tag": entry["tag"],
                     "tagType": 318} for entry in entries]
            return self.tag_container(size=len(rows), Directory=rows)
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
        if "timespan" in (query or {}) and not str((query or {})["timespan"]).lstrip("-").isdigit():
            # Measured on kids (1.43.4) and zen (1.42.2), on all three endpoints: the
            # rollup window has to parse as a whole number. `abc`, `1.5`, an empty value
            # and one with a leading space are 400s, where `0`, `-1` and `06` are 200s.
            # plexsim ran an unguarded int() on it: a 500 on two of the three endpoints
            # and, on /statistics/resources, a silently ignored parameter.
            return ("error", 400, "plexsim: bad request")
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
            # Measured on both servers: the Dashboard's per-account and per-device
            # graphs filter the series -- `accountID` and `deviceID` keep matching rows
            # (an id with none answers `size 0`), `lan=0|1` keeps remote or local
            # traffic -- and an empty or non-numeric id, or any other `lan`, is a 400.
            # plexsim ignored all three, so every filtered graph drew the whole server.
            for name, field in (("accountID", "accountID"), ("deviceID", "deviceID")):
                if name in query:
                    if not str(query[name]).isdigit():
                        return ("error", 400, "plexsim: bad request")
                    rows = [r for r in rows if str(r.get(field)) == str(int(query[name]))]
            if "lan" in query:
                if query["lan"] not in ("0", "1"):
                    return ("error", 400, "plexsim: bad request")
                rows = [r for r in rows if bool(r.get("lan")) == (query["lan"] == "1")]
        else:
            rows = self.sessions.media_statistics(timespan)
            element = "StatisticsMedia"
            # Measured on both servers: this one filters like /statistics/bandwidth, which
            # cycle 8 implemented and left this out -- an `accountID` or `deviceID` the
            # server does not have answers `size 0` with no Device and no Account row, and
            # an empty or non-numeric `accountID` is a 400. plexsim answered the whole
            # server to every one of them, so the Dashboard's per-user and per-device views
            # showed everyone's plays.
            for name, field in (("accountID", "accountID"), ("deviceID", "deviceID")):
                if name in query:
                    if not str(query[name]).isdigit():
                        return ("error", 400, "plexsim: bad request")
                    rows = [r for r in rows if str(r.get(field)) == str(int(query[name]))]
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

    # What each butler task pushes, measured task by task on a writable copy of the
    # reference: the message it ends with, and the activity it runs inside (a task with no
    # activity of its own reports only its message). Every run sits inside ONE `butler`
    # activity called "Butler tasks", which is what the Dashboard draws while it runs.
    BUTLER_MESSAGES = {
        "BackupDatabase": ("The butler backed up your database.",
                           ("general.db.backup", "Database", "Backing up database")),
        "OptimizeDatabase": ("The butler optimized your database.",
                             ("general.db.optimize", "Database", "Optimizing database")),
        "CleanOldBundles": ("The butler cleaned up your old bundles.",
                            ("general.bundle.clean", "Bundles", "Cleaning old bundles")),
        "CleanOldCacheFiles": ("The butler deleted 0 old cache files.", None),
        "GarbageCollectBlobs": ("The butler collected 0 old blobs.", None),
        "RefreshLocalMedia": ("The butler refreshed 0 movies.", None),
        "RefreshPeriodicMetadata": ("The butler refreshed 0 shows.", None),
        "UpgradeMediaAnalysis": ("The butler upgraded 0 items.", None),
        "LoudnessAnalysis": ("The butler analyzed the loudness of 0 tracks.", None),
        "GenerateChapterThumbs": ("The butler generated 0 chapter thumbnails.", None),
        "DeepMediaAnalysis": ("The butler analyzed 0 items.", None),
        "GenerateMediaIndexFiles": ("The butler generated 0 index files.", None),
        "RefreshLibraries": ("The butler refreshed all libraries.", None),
    }

    def butler(self, method, segments):
        """`/butler` and `/butler/{task}` -- the Scheduled Tasks page's Run button."""
        if len(segments) == 1:
            if method in ("POST", "DELETE"):
                # Measured on a writable copy of the reference: running every enabled task,
                # and cancelling the queue, each answer an EMPTY body. plexsim answered the
                # task list, which a client reads as a listing rather than an acceptance.
                if method == "POST":
                    self.run_butler([task["name"] for task in self.BUTLER_TASKS
                                     if task.get("enabled")])
                return ("empty", 200)
            if method != "GET" and method != "HEAD":
                return ("error", 404, "plexsim: not found")
            # Measured: the ButlerTasks root carries NO size attribute, unlike
            # every MediaContainer on the surface.
            return {"ButlerTasks": {"ButlerTask": self.BUTLER_TASKS}}
        name = segments[1]
        if name not in {task["name"] for task in self.BUTLER_TASKS}:
            return ("error", 404, "plexsim: no such task")
        if method != "POST":
            # Measured: every verb but POST is a 404 on a task a real PMS HAS -- the DELETE
            # that cancels is on `/butler` itself, and plexsim answered 200 to a task DELETE,
            # which tells the page a queued task was cancelled when none was.
            return ("error", 404, "plexsim: not found")
        self.run_butler([name])
        return ("empty", 200)

    def run_butler(self, names):
        """Run named butler tasks, with the frames a real PMS pushes.

        Measured on a writable copy of the reference: one `butler` activity ("Butler tasks",
        started at -1) wraps the whole run; each task with an activity of its own starts,
        steps and ends inside it; and each task ends with its own `progress` message. plexsim
        pushed nothing, so the Dashboard showed no task running and no result.
        """
        wrapper = {"uuid": str(uuid.uuid4()), "type": "butler", "cancellable": False,
                   "userID": 1, "title": "Butler tasks", "subtitle": "", "progress": -1}
        self.notify_activity(wrapper, "started")
        for name in names:
            message, inner = self.BUTLER_MESSAGES.get(name, ("", None))
            if inner:
                kind, title, subtitle = inner
                # The task's own activity, measured: started at -1 with its subtitle already
                # set, stepping to 100, then ended.
                activity = {"uuid": str(uuid.uuid4()), "type": kind, "cancellable": False,
                            "userID": 1, "title": title, "subtitle": subtitle, "progress": -1}
                self.notify_activity(activity, "started")
                for step in (10, 53, 100):
                    activity["progress"] = step
                    self.notify_activity(activity, "updated")
                self.notify_activity(activity, "ended")
            if name == "RefreshLibraries":
                for section in self.catalog.sections:
                    for row in self.catalog.section_items(section["key"]):
                        self.library.finish_activity(
                            self.library.add_activity(row.get("title") or "", progress=-1))
            if message:
                self.notify_progress(message)
            logger.info("BUTLER ran %s", name)
        wrapper["progress"] = 100
        self.notify_activity(wrapper, "updated")
        self.notify_activity(wrapper, "ended")

    def updater_status(self):
        """`/updater/status` -- plexsim never has an update to install."""
        return self.bare_container(size=0, canInstall=False,
                                   checkedAt=int(time.time()), status=0)

    # The attribute set a SINGLE history read answers, in order -- measured on kids
    # (1.43.4) and zen (1.42.2) and identical on both, against every readable row on
    # each. It is not the listing's row: there is no `key`, no `ratingKey` and no
    # artwork, `viewType` is always 1, and an episode adds the show's title and the two
    # indices. plexsim answered its stored listing row, so a client reading one history
    # entry was handed links the endpoint does not advertise.
    HISTORY_ROW_FIELDS = ("historyKey", "librarySectionID", "title", "grandparentTitle",
                          "type", "index", "parentIndex", "originallyAvailableAt",
                          "viewedAt", "accountID", "deviceID")

    @classmethod
    def _history_row(cls, entry):
        """One history row as the single read projects it."""
        row = {name: entry[name] for name in cls.HISTORY_ROW_FIELDS if entry.get(name) is not None}
        row["viewType"] = 1
        return row

    def history(self, query):
        """`/status/sessions/history/all` -- the View Play History page.

        Rows are recorded as playback happens (library.record_view), with the
        field set measured on a production PMS: an episode carries
        key/ratingKey/parentKey/grandparent*/index/parentIndex, a movie carries
        title and originallyAvailableAt.
        """
        # Cycle 15: every listing row carries `viewType: 1`, which is also the
        # column `type=` matches -- see the gate below. plexsim put `viewType` on
        # the single read only, so the column `type=` filters on was not on the row.
        rows = [dict(row, viewType=1) for row in self.library.history()]
        # Measured: this page filters and sorts, and plexsim ignored all of it --
        # so Top Users' "View Play History" link (`accountID=&viewedAt=`) and the
        # per-item history link (`metadataItemID=`) both showed the whole server's
        # history in insertion order, and the Played column's sort did nothing.
        #
        # Note the operators: `viewedAt>=` here is a plain `>=`, NOT the library
        # grammar's `>>`. Two query languages on one server.
        # Cycle 14, measured case by case on kids (1.43.4) and zen (1.42.2), which
        # agree on all nineteen: this page VALIDATES its ids. `accountID`,
        # `librarySectionID` and `metadataItemID` are 400 for anything that is not a
        # whole number -- `abc`, empty and `1.5` alike -- while `1`, `-1` and `01` are
        # fine; `metadataItemID` is additionally 404 for a whole number the server
        # holds no row for, `0` and `99999999` included. An unknown `librarySectionID`
        # is NOT 404, it is an empty 200. `deviceID` is not validated at all: `abc`
        # and empty are both 200. `viewedAt>=abc` and an empty one are 400s.
        #
        # plexsim answered 200 with `size 0` to every one of them, so a client sending
        # a malformed accountID read "this user has no history" instead of an error,
        # and the per-item History link on a row the server does not hold read "never
        # played" rather than saying so.
        for name in ("accountID", "librarySectionID", "metadataItemID"):
            if name not in query:
                continue
            value = str(query.get(name) or "")
            if not (value.lstrip("-").isdigit() and value.lstrip("-")):
                return ("error", 400, "plexsim: bad request")
        if "metadataItemID" in query:
            wanted = str(query["metadataItemID"])
            if not any(str(r.get("ratingKey")) == wanted for r in rows) \
                    and self.catalog.items.get(int(wanted)) is None:
                return ("error", 404, "plexsim: not found")
        for key in ("viewedAt>", "viewedAt>=", "viewedAt<", "viewedAt<="):
            if key in query and not str(query.get(key) or "").lstrip("-").isdigit():
                return ("error", 400, "plexsim: bad request")
        # Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: this page
        # lists NOTHING without a `type=`. Both answer `size 0`/`totalSize 0` to the
        # plain call -- zen with 73 rows in it -- and that is the whole of the `size 0`
        # cycle 11 recorded as unexplained and three cycles carried in the register as
        # "find the parameter or account state that makes a real PMS list history".
        #
        # The `type` is the row's `viewType`, NOT its metadata type: kids' history is
        # five MOVIES and zen's is 73 EPISODES and both answer to `type=1`, while
        # `type=0`, `2`, `3`, `4` and `5` answer nothing on either. A comma list is an
        # OR, as everywhere else. `type=abc` and an empty `type=` are 400s -- the same
        # grammar the ids take, and validated after them.
        #
        # plexsim ignored `type=` completely, so it answered its whole history where a
        # real PMS answers none, and answered every row to `type=2` as well.
        wanted_types = set()
        if "type" in query:
            parts = str(query.get("type") or "").split(",")
            if not all(p.lstrip("-").isdigit() and p.lstrip("-") for p in parts):
                return ("error", 400, "plexsim: bad request")
            wanted_types = {int(p) for p in parts}
        rows = [r for r in rows if int(r.get("viewType") or 1) in wanted_types]
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
        prefs = {match.group(1): value for name, value in query.items()
                 for match in [re.match(r"^prefs\[(\w+)\]$", name)] if match}
        if prefs:
            # Measured on a writable copy of the reference: the create applies its
            # `prefs[...]` -- `flattenSeasons` and `episodeSort` read back as sent -- where
            # plexsim dropped them.
            self.library_section(["library", "sections", str(section["key"]), "prefs"], prefs, "PUT")
        # Cycle 15, re-measured on the copy: a create ends in ONE
        # `backgroundProcessingQueue` `queueRegenerated` frame (+63.1 s there). The
        # 75-frame scan that precedes it is over REAL files; a library plexsim
        # creates is empty by construction, so those frames have nothing to name.
        self.notify_queue_regenerated()
        # Measured: the create answers a BARE container, with none of the library
        # container's identifier, media-tag pair or allowSync.
        return ("created", self.bare_container(size=1, Directory=[dict(
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
        )]), "/library/sections/%s" % section["key"])

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
        """The image behind a row's poster/background: an uploaded key, or a remote URL."""
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
    # Cycle 16, measured on kids and zen: an idle kept-alive connection is closed by
    # the server after exactly 20 seconds, which is the number it advertises in
    # `Keep-Alive: timeout=20`. plexsim never closed one, so an abandoned socket held
    # a ThreadingHTTPServer thread for the life of the process.
    timeout = 20

    def send_response(self, code, message=None):
        """A real PMS sends no `Server:` banner -- measured on kids and zen.

        Fifteen paths, JSON and XML, 200/400/404/500 alike: both references answer
        `X-Plex-Protocol, Content-Type, Content-Length, Connection, Cache-Control,
        Date` and nothing else. BaseHTTPRequestHandler's own `send_response` adds a
        `Server:` from `server_version`, so plexsim named itself on every response it
        has ever sent. Blanking `version_string()` does not help -- `send_header`
        still puts an empty header on the wire -- so the banner has to be dropped by
        not sending it. `Date` stays: both references send one.
        """
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

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
        # Cycle 16, measured on kids and zen over every status and both formats: a
        # real PMS names its connection state on EVERY response -- `Connection:
        # Keep-Alive` plus `Keep-Alive: timeout=20` when the connection is being
        # kept, `Connection: close` when it is not. plexsim sent neither, so a
        # client reading the advertised timeout to schedule reuse got nothing.
        if "connection" not in names:
            if self.close_connection:
                self.send_header("Connection", "close")
            else:
                self.send_header("Connection", "Keep-Alive")
                self.send_header("Keep-Alive", "timeout=20")
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
        if "content-encoding" in names:
            return payload
        accepted = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in accepted:
            return payload
        if not any(content_type.startswith(kind) for kind in self.GZIP_TYPES):
            return payload
        import gzip as gzip_module
        squeezed = gzip_module.compress(payload, 6)
        # Re-measured on kids (1.43.4) and zen (1.42.2): the gate is the client's offer
        # and NOTHING else. Both compress an 83-byte body into 90 bytes and send it
        # gzipped, and both send it plain for `Accept-Encoding: identity`. The earlier
        # 860-byte floor was inferred from the smallest compressed answer that happened
        # to be seen, and the "only if it shrinks" test was invented: together they left
        # seventeen of the endpoints cycles 9 and 10 added answering plain where a real
        # PMS answers gzip, without the length pair a client reads off them.
        self.send_header("Content-Encoding", "gzip")
        self.send_header("X-Plex-Content-Original-Length", str(len(payload)))
        self.send_header("X-Plex-Content-Compressed-Length", str(len(squeezed)))
        return squeezed

    def _paging_headers(self, body):
        """The window a paged container answers in its response HEADERS.

        Cycle 16, measured on kids and zen: every container that pages carries
        `X-Plex-Container-Start` and `X-Plex-Container-Total-Size` beside the body's
        own `offset`/`totalSize` -- `/identity` answers Total-Size 0 -- and an unpaged
        one carries neither. plexsim sent no such header at all.
        """
        if not isinstance(body, dict) or "offset" not in body or "totalSize" not in body:
            return ()
        return (("X-Plex-Container-Start", str(body["offset"])),
                ("X-Plex-Container-Total-Size", str(body["totalSize"])))

    def _send_container(self, container):
        # `_xml` names the element a container's rows render as where the rows
        # cannot name it themselves (an artwork list is <Photo>, a folder list
        # <Directory>). It is internal: it must never reach the JSON body.
        body_dict = next(iter(container.values()))
        elements = body_dict.pop("_xml", None) if isinstance(body_dict, dict) else None
        window = self._paging_headers(body_dict)
        if self._wants_xml():
            self._send(200, "text/xml;charset=utf-8", to_xml(container, elements), window)
        else:
            self._send(200, "application/json", json_body(container), window)

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

    def do_PATCH(self):
        # Cycle 15, measured on the copy across nine paths: `PATCH` is ROUTED, not
        # refused -- `/identity`, `/hubs` and `/playlists` answer 200 to it (the
        # recorded "these answer any verb" rule) and every other path 404s it. The
        # hunt that reported this read it as a blanket 404; nine paths say otherwise.
        self._dispatch("PATCH")

    # Python's BaseHTTPRequestHandler answers 501 with its own error page and a
    # `Server:` banner for any method it has no `do_` for. A real PMS answers a
    # **500** with its own four-line html body -- measured on the copy for `FROB`,
    # `TRACE` and a wrong-case `Get`, on every path. HANDOVER already records that a
    # real PMS never answers 405; it never answers 501 either.
    UNKNOWN_METHOD_BODY = (b"<html><head><title>Internal Server Error</title></head>"
                           b"<body><h1>500 Internal Server Error</h1></body></html>")

    def __getattr__(self, name):
        # Only reached when normal lookup fails, so every real `do_*` above still wins.
        if name.startswith("do_"):
            return self._unknown_method
        raise AttributeError(name)

    def _unknown_method(self):
        self._send(500, "text/html", self.UNKNOWN_METHOD_BODY)

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
        if not self.headers.get("Access-Control-Request-Method"):
            # Cycle 15, measured on the copy: a BARE OPTIONS is routed like any other
            # verb -- 200 on `/identity`, `/hubs` and `/playlists`, 404 everywhere
            # else. Only a genuine preflight gets the CORS answer below. plexsim
            # answered every OPTIONS with the preflight, so a probe of any path read
            # as "this endpoint exists".
            self._dispatch("OPTIONS")
            return
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
        # Measured on the copy: the preflight response carries a Content-Type, and it
        # is `text/plain`. plexsim sent none.
        self.send_header("Content-Type", "text/plain")
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

    # Cycle 15, swept by METHOD across 33 paths on the disposable copy -- the sweep
    # nobody could run before, because it needs writes. A READ-ONLY route answers
    # **404** to every verb but GET and HEAD; plexsim answered the GET body with a
    # 200, which reads to a client (and to python-plexapi's capability probes, which
    # are exactly this shape) as "this endpoint exists and accepts writes".
    #
    # Listed rather than inferred: each of these was measured, and the routes that do
    # take a write -- `/library/sections`, `/playlists`, `/library/collections`,
    # `/:/prefs`, every item and section leaf that mutates -- are deliberately absent.
    READ_ONLY_ROUTES = (
        "/", "/library/recentlyAdded", "/library/onDeck", "/library/tags",
        "/library/matches", "/playlists/all", "/hubs/promoted",
        "/hubs/continueWatching", "/hubs/search", "/status/sessions/history/all",
        "/:/plugins", "/channels/all", "/services/browse", "/system",
        "/system/agents", "/updater/status",
    )
    READ_ONLY_PREFIXES = ("/system/scanners/",)
    # Item leaves that are pure reads. `extras`, `themes`, `clearLogos`, `posters`
    # and `arts` are NOT here: their POST is the upload path (400 or 406, measured
    # separately), only their PUT and DELETE are refused.
    READ_ONLY_ITEM_LEAVES = ("allLeaves", "tree", "related", "similar", "matches",
                             "grandchildren")

    # Cycle 15, measured on kids (1.43.4) and zen (1.42.2): `limit=` and `parent=` are
    # PARSED wherever a listing consumes them, and a value that is not a whole number is
    # a 500 on every one of these -- `abc`, an empty value and `1.5` alike. A GOOD limit
    # still does nothing on `/children` (cycle 11), so ignoring the VALUE is not the same
    # as not parsing the string. `/newest`, `/recentlyAdded`, `/library/onDeck`, the hub
    # paths, `/sorts`, `/filters` and `/library/sections` ignore both entirely and are
    # deliberately absent.
    PARSES_LIMIT = ("all", "allLeaves", "search", "common", "genre", "year", "decade",
                    "contentRating", "studio", "director", "writer", "producer",
                    "country", "actor", "label", "collection", "resolution",
                    "firstCharacter", "userRating", "subtitleLanguage", "audioLanguage",
                    "hdr", "unwatched", "duplicate", "editionTitle", "unmatched")

    def _query_grammar_refusal(self, method, path):
        """The status a malformed `limit=`/`parent=` gets, or 0. Cycle 15."""
        if method not in ("GET", "HEAD"):
            return 0
        route, _, raw = path.partition("?")
        route = route.rstrip("/") or "/"
        query = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
        bad = [name for name in ("limit", "parent")
               if name in query and not str(query[name]).lstrip("-").isdigit()]
        if not bad:
            return 0
        if route == "/status/sessions/history/all":
            # Measured: this page answers 400 where a listing answers 500 -- the same
            # split it already has for an unknown sort key. `parent` is not validated
            # here at all.
            return 400 if "limit" in bad else 0
        parts = [p for p in route.split("/") if p]
        if parts[:2] == ["library", "sections"] and len(parts) == 4 \
                and parts[3] in self.PARSES_LIMIT:
            return 500
        if route == "/library/all" or route == "/playlists":
            return 500
        if parts[:2] == ["library", "metadata"] and len(parts) == 4 \
                and parts[3] in ("children", "grandchildren", "allLeaves"):
            return 500
        return 0

    def _read_only_refusal(self, method, path):
        """404 for a write to a route that only reads. Cycle 15."""
        if method in ("GET", "HEAD"):
            return False
        route = path.split("?")[0].rstrip("/") or "/"
        if route in self.READ_ONLY_ROUTES:
            return True
        if any(route.startswith(prefix) for prefix in self.READ_ONLY_PREFIXES):
            return True
        parts = [p for p in route.split("/") if p]
        if parts[:2] == ["library", "metadata"] and len(parts) == 4 \
                and parts[3] in self.READ_ONLY_ITEM_LEAVES:
            return True
        if parts[:2] == ["library", "metadata"] and len(parts) == 4 \
                and parts[3] in ("extras", "themes", "clearLogos", "posters", "arts") \
                and method != "POST":
            # Cycle 15: everything but the POST -- which is the upload path, and a
            # 400 or a 406 of its own. A bare OPTIONS lands here too, and the copy
            # 404s it: only a genuine preflight is answered.
            return True
        return False

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

        refusal = self._query_grammar_refusal(method, self.path)
        if refusal:
            self._send(refusal, "text/plain", b"plexsim: bad request")
            return

        if self._read_only_refusal(method, self.path):
            logger.info("404 %s %s -- read-only route", method, self.path.split("?")[0])
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
                # POST /library/sections answers 201 with the created Directory -- and,
                # measured on the copy, a `Location` header naming the new library
                # (`/library/sections/6`). plexsim sent the body alone, so a client that
                # follows the header rather than parsing the row had nowhere to go.
                payload = json_body(result[1])
                created = [("Location", result[2])] if len(result) > 2 and result[2] else []
                if self._wants_xml():
                    self._send(201, "text/xml;charset=utf-8", to_xml(result[1]), created)
                else:
                    self._send(201, "application/json", payload, created)
                return
            if kind == "zip":
                self._send(200, "application/zip", result[1],
                           [("Content-Disposition",
                             "attachment; filename=\"plexsim-logs.zip\"")])
                return

        if method in ("GET", "HEAD"):
            try:
                result = self._exclude_container(result)
                result = self._page_container(result)
            except filters_module.FilterError as refused:
                self._send(refused.status, "text/plain",
                           ("plexsim: %s" % refused.detail).encode())
                return
        self._send_container(result)

    def _exclude_container(self, result):
        """Apply `excludeElements=` to any MediaContainer that carries rows.

        Cycle 15, measured on kids (1.43.4) and zen (1.42.2), which agree: this is a
        property of the RESPONSE, the way paging is -- `/library/all`,
        `/library/recentlyAdded`, a section's `recentlyAdded`, `newest`, `search` and
        `common`, an item's `/grandchildren` and EVERY hub path all honour it. Cycle
        11 wired it into four surfaces by hand and the rest were missed; doing it
        here instead is the shape the measurement actually has.

        The Home screen is the one that costs a client something: `excludeElements=
        Media` is how it asks for a cheap shelf, and plexsim sent the whole
        Media/Part/Stream tree for every shelf row on every render.
        """
        if not isinstance(result, dict) or list(result) != ["MediaContainer"]:
            return result
        body = result["MediaContainer"]
        if not isinstance(body, dict):
            return result
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query,
                                            keep_blank_values=True))
        if not [e for e in (query.get("excludeElements") or "").split(",") if e]:
            return result
        trim = self.server.sim._excluded_elements

        def walk(container):
            out = dict(container)
            if isinstance(out.get("Metadata"), list):
                out["Metadata"] = trim(out["Metadata"], query)
            if isinstance(out.get("Hub"), list):
                out["Hub"] = [walk(hub) for hub in out["Hub"]]
            return out

        return {"MediaContainer": walk(body)}

    def _page_container(self, result):
        """Apply the client's container paging to any MediaContainer not yet paged.

        Measured on both servers: EVERY MediaContainer pages when both
        `X-Plex-Container-Start` and `X-Plex-Container-Size` are present, as headers or
        query parameters -- `/`, `/library`, `/:/prefs` (all 174 Settings), `/devices`,
        a detail row, a tree, every hub list, the tag table -- and even an empty one
        gains `totalSize: 0` and `offset: 0` (`/identity`). A container holding several
        lists (`/statistics/bandwidth`: Device, Account, StatisticsBandwidth) slices
        each, counts `size` over all of them and takes `totalSize` from the longest.
        plexsim paged only the handful of listings that called page_window themselves,
        so a client asking for a page got every row and no total to stop at. The
        exceptions are measured too: a plug-in framework container (`noHistory`,
        `/system/scanners/{type}`) ignores paging, and a root that is not a
        MediaContainer (`ButlerTasks`, `MyPlex`) has nothing to page.
        """
        if not isinstance(result, dict) or list(result) != ["MediaContainer"]:
            return result
        body = result["MediaContainer"]
        if not isinstance(body, dict) or "totalSize" in body or "noHistory" in body:
            return result
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query,
                                            keep_blank_values=True))
        for name in ("X-Plex-Container-Start", "X-Plex-Container-Size"):
            # Cycle 16, measured on kids and zen: when BOTH are sent the
            # HEADER wins, per parameter -- query `Start=0&Size=2` with
            # header `Start=1&Size=1` answers offset 1 size 1, a header
            # naming only the size takes the size from the header and the
            # offset from the query, and a header `Start=abc` beside a
            # valid query is a 500. plexsim only read a header the query
            # had not named, so a client sending both got the query's page.
            if self.headers.get(name) is not None:
                query[name] = self.headers.get(name)
        lists = [key for key, value in body.items() if isinstance(value, list)]
        # Measured on kids (1.43.4) and zen (1.42.2): a statistics container does NOT
        # slice its Device and Account lists with the page. They are a LOOKUP for the
        # rows -- the page carries exactly the devices and accounts its own rows name,
        # by content, so a window past the end carries neither and a window over two
        # devices carries both. plexsim sliced all three lists with the same offset, so
        # the second page of a graph named a device it had not sent and lost the
        # Account row entirely: the Dashboard's per-device and per-account series
        # arrive unlabelable.
        series = [key for key in lists if key.startswith("Statistics")]
        lookups = {"Device": "deviceID", "Account": "accountID"}
        if series and any(key in lists for key in lookups):
            lists = series
        total = max([len(body[key]) for key in lists] or [int(body.get("size") or 0)])
        _, paged, start, _ = self.server.sim.page_window(range(total), query)
        if not paged:
            return result
        size = int(query["X-Plex-Container-Size"])
        sliced = {key: value[start:start + size] for key, value in body.items()
                  if key in lists}
        if series and lists == series:
            named = {field: {str(row.get(field)) for key in series
                             for row in sliced.get(key) or []}
                     for field in lookups.values()}
            for key, field in lookups.items():
                if isinstance(body.get(key), list):
                    sliced[key] = [row for row in body[key]
                                   if str(row.get("id")) in named[field]]
        count = sum(len(rows) for rows in sliced.values()) if lists else int(body.get("size") or 0)
        out = {"size": count, "totalSize": total, "offset": start}
        out.update({key: sliced.get(key, value) for key, value in body.items() if key != "size"})
        return {"MediaContainer": out}

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
        """Serve an uploaded image, or fetch a remote one, cached in memory.

        Artwork is requested once per tile per client, so without a cache a
        grid scroll becomes a burst of identical upstream fetches.
        """
        uploaded = self.server.sim.uploads.get(url)
        if uploaded is not None:
            payload, content_type = uploaded
            self._send(200, content_type, payload,
                       [("Cache-Control", "no-cache"), ("Accept-Ranges", "bytes")])
            return
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
