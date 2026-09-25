#!/usr/bin/env python3
"""The mutable half of plexsim's library: everything the UI can change.

`catalog.json` is read-only input. The Plex UI is not a read-only client: every
entry in an item's context menu is a write, and the web client decides which
entries to *draw* from what the server advertises, not from whether the write
would succeed. Measured in the client's own bundle (`main-*.js` module 92273,
one flag list per item type plus the predicate gating each entry):

    Mark as Played   provider timeline feature carries scrobbleKey, and for a
                     show or season, `unviewedLeafCount > 0` -- so a show row
                     with no `viewedLeafCount` never offers it, however correct
                     the rest of the row is.
    Edit/Refresh/    provider advertises the `manage` feature
    Analyze
    Fix Match        guid present and not `local://` or `none://`; an item with
    Match            NO guid at all offers neither
    Delete           server reports `allowMediaDeletion`
    Download         provider `protocols` contains `download`
    Split Apart      more than one Media (or Location for a show)
    Optimize         server reports transcoderVideo AND backgroundProcessing

Everything here is in memory. catalog.json is never rewritten, which makes a
restart the undo button -- deliberate, because a Delete in the Plex UI is
phrased "delete from your filesystem" and plexsim owns no files: the honest
local meaning is "drop the row from the library", and the debrid content behind
it is untouched.

Key spaces stay disjoint, the way the catalog's already are (leaves 1000+,
shows 20000+, seasons 30000+), so a ratingKey identifies its own kind on sight:
playlists 40000+, collections 50000+.
"""

import datetime
import logging
import re
import time
import urllib.parse
import uuid
import zlib

from hierarchy import (_GENERATED_TITLE as GENERATED_SEASON_TITLE,
                       PERSON_TAG_ELEMENTS, person_tag_key, sort_title)

logger = logging.getLogger("plexsim.library")

# Fields a row carries as a number. The client sends every edit as a string.
# The columns whose VALUE the ITEM edit path really reads. Cycle 20, measured one at a
# time on plexscratch with `12` and with `ZZQ1`: exactly three answer 500 to the word,
# and every other numeric column answers 200 to both -- accepted and discarded. The
# SECTION path reads far more of them; see SECTION_INTEGER_FIELDS.
INTEGER_FIELDS = {"year", "index", "absoluteIndex", "parentIndex", "duration", "viewOffset",
                  "viewCount", "leafCount", "childCount"}

DATE_FIELDS = {"originallyAvailableAt"}

# Fields the Edit dialog may write. Measured: anything else is a 400, including
# `sortTitle` (the field is `titleSort`) -- plexsim stored whatever it was sent,
# so a client typo became a row attribute and a `Field` lock for a field that
# does not exist.
# Cycle 53, measured on plexscratch (1.43.4) and plexscratch42 (1.42.2), identical on
# both: the ten columns cycle 53 read out of the database template are editable on the
# ITEM path -- `<column>.value=` is a 200 and `.locked=1` records the lock -- where
# plexsim answered 400 to nine of the ten. Only `isAdult` is READ BACK: exactly the
# literal `1` stores it and every other value clears it, and the row then carries
# `isAdult: true` between `originalTitle` and `summary` on every projection, the light
# grid row included. The other nine store nothing visible, which is the `LOCK_ONLY_FIELDS`
# shape.
DATABASE_COLUMN_FIELDS = ("remote", "isAdult", "resourcesChangedAt", "userFields",
                          "lastSkippedAt", "mediaAnalysisVersion", "beginsAt", "endsAt",
                          "agent", "source")

EDITABLE_FIELDS = {
    "title", "titleSort", "originalTitle", "studio", "tagline", "summary",
    # Cycle 53: the one database column that reads back.
    "isAdult",
    "contentRating", "originallyAvailableAt", "year", "index", "editionTitle",
    "userRating", "audienceRating", "rating", "duration",
    # Cycle 20: eleven more columns are edits too -- each a 200 on the item path with
    # its lock recorded, where an unknown name is a 400. Ten of them are discarded
    # there (IGNORED_VALUE_FIELDS) and read on the section path.
    #
    # Cycle 31: `absoluteIndex` is the ELEVENTH and it is read on NEITHER. Re-measured
    # on plexscratch42 (1.42.2) and plexscratch (1.43.4), which agree: `.value=` is a
    # 200 that stores nothing on the item path and on the section path, on a movie, an
    # episode, a season and a show -- twelve measurements -- while `.locked=1` really
    # does record the lock (`Field` gains the name on both). Cycle 20 recorded it as
    # "read on both" and plexsim stored the number, so an edited row carried an
    # attribute no real PMS puts there and `sort=absoluteIndex` ordered on it.
    "updatedAt", "createdAt", "skipCount", "viewOffset", "ratingCount", "type",
    "viewedLeafCount", "contentRatingAge", "mediaCount", "extraType", "absoluteIndex",
}

# Accepted, locked, and DISCARDED on the ITEM path: the star rating is written
# through /:/rate there. The section path stores it (see `_set_field`).
IGNORED_EDIT_FIELDS = {"userRating"}

# Accepted with a 200 and DISCARDED as values. Measured on a writable copy of the
# reference: `addedAt.value=`, `guid.value=`, `lastViewedAt.value=`, `viewCount.value=`
# and an episode's `parentIndex.value=` change nothing, where plexsim refused each as an
# unknown field. Play state is written through /:/scrobble.
#
# Cycle 31 moved `thumb` and `art` OUT of this set: they are artwork COLUMNS that take a
# value, and cycle 20 read them as discarded because the rows it wrote to already owned a
# poster. See ARTWORK_VALUE_FIELDS.
IGNORED_VALUE_FIELDS = {"addedAt", "guid", "lastViewedAt", "viewCount",
                        # Cycle 53: nine of the ten database columns take the write on the
                        # ITEM path and store nothing; only `isAdult` reads back.
                        "remote", "resourcesChangedAt", "userFields", "lastSkippedAt",
                        "mediaAnalysisVersion", "beginsAt", "endsAt", "agent", "source",
                        "parentIndex",
                        # Cycle 20: ten more are accepted and discarded on the ITEM
                        # path -- 200 for a number and 200 for a word alike, and
                        # nothing stored. The section path reads every one of them.
                        "updatedAt", "createdAt", "skipCount", "ratingCount", "type",
                        "viewedLeafCount", "contentRatingAge", "mediaCount",
                        "extraType", "viewOffset", "duration"}

# Cycle 31: PARSED and then dropped, which is a third thing from "stored" and "ignored".
# Re-measured on plexscratch42 (1.42.2) and plexscratch (1.43.4), which agree:
# `absoluteIndex.value=7` is a 200 that stores NOTHING, while `=ZZQ1` and `=1.5` are 500s
# -- so the value is read as an integer and then discarded, the way `duration` and
# `parentIndex` are on the section path. Cycle 20 recorded it as read on both paths and
# plexsim stored the number; putting it in IGNORED_VALUE_FIELDS skipped the parse and made
# a word a 200, which is the opposite error.
PARSED_NOT_STORED = {"absoluteIndex"}

# A tag type accepted and discarded: a show's `network[0].tag.tag=` is a 200 that stores
# nothing visible, where plexsim 400'd it.
# Cycle 49 added `mood`: on plexscratch42 (1.42.2) a `mood[0].tag.tag=` write to a MOVIE
# is a 200 that stores nothing -- the row carries no `Mood` afterwards -- where the `style`
# and `similar` beside it really store. plexsim stored it, and then carried a `Mood`
# element no real PMS emits on a movie into every projection that reads the row.
# (`mood` is a music-library field; plexsim holds no music library, so "not stored on the
# types plexsim holds" is the whole of what is measured here.)
IGNORED_TAG_TYPES = {"network", "mood"}

# Cycle 31, re-measured on plexscratch42 (1.42.2) and plexscratch (1.43.4), which agree:
# `hdr10plus` is accepted on BOTH halves of the grammar and stores NEITHER -- `.locked=1`
# and `.value=1` are each a 200 and the row's `Field` list is unchanged (1.42.2's stays
# `collection, hdr, dovi`, 1.43.4's stays its seven). It is the only name on the surface in
# that position: every other accepted name either stores a value or records a lock.
#
# Cycle 21 recorded it correctly as "a 200 that stores nothing" and cycle 30, having
# confirmed the 200 on the new 1.42.2 copy, put it in LOCK_ONLY_FIELDS -- which records a
# lock. HANDOVER 3.1 class C inside the cycle that re-measured it: the status was checked
# and the row was never read back. The Edit dialog draws its lock icons from `Field`, so
# the regression drew a lock on a field it cannot show.
NO_OP_FIELDS = {"hdr10plus"}

# Cycle 20, measured one column at a time on plexscratch: the SECTION save path reads
# every numeric column, where the item path reads three. These seven parse -- a number
# is a 200 and a word a 500 -- and the eight below are refused outright, 500 even for a
# valid number. Of the seven, `updatedAt`, `addedAt`, `ratingCount` and
# `contentRatingAge` really store (and an episode's `index`); `duration` and
# `parentIndex` parse and change nothing, which is why they are read and then dropped.
SECTION_INTEGER_FIELDS = {"addedAt", "duration", "parentIndex", "ratingCount", "updatedAt",
                          "createdAt", "contentRatingAge"}
SECTION_STORED_FIELDS = {"addedAt", "ratingCount", "updatedAt", "contentRatingAge"}
SECTION_REFUSED_FIELDS = {"viewCount", "lastViewedAt", "skipCount", "viewOffset", "type",
                          # Cycle 53: six of the ten database columns are a 500 on the
                          # SECTION path where the item path takes them -- the same split
                          # `lastRatedAt` already has. `remote`, `isAdult`,
                          # `resourcesChangedAt` and `userFields` are 200s there.
                          "lastSkippedAt", "mediaAnalysisVersion", "beginsAt", "endsAt",
                          "agent", "source",
                          "viewedLeafCount", "mediaCount", "extraType",
                          # Cycle 31, re-measured on both writable copies over five values
                          # (`5`, `0`, `abc`, empty, a real epoch): `lastRatedAt.value=` is
                          # a **500 on the section path on both versions**, every time,
                          # where the ITEM path takes it with a 200. It is the ninth member
                          # of this set and the one plexsim answered with the unknown-field
                          # 400 -- a refusal either way, so nothing renders differently;
                          # it is the status a client logs and retries on.
                          "lastRatedAt"}

# Stored as NUMBERS. Measured: `audienceRating.value=5.5` reads back 5.5, not "5.5".
FLOAT_FIELDS = {"audienceRating", "rating", "userRating"}

# The only two fields an empty value STORES as "" rather than removing. Measured
# one field at a time; everything else falls back to what the agent supplies.
KEEP_EMPTY_FIELDS = {"title", "summary"}

# The order a real PMS lists locks in -- a fixed column order, not the order the
# client happened to write them. plexsim appended, so re-locking a field moved it
# to the end and the dialog's checkbox order shuffled. Measured with every lockable
# field locked in reverse, on a movie and on an episode.
FIELD_ORDER = ["title", "titleSort", "originalTitle", "studio", "rating", "tagline",
               "summary", "contentRating", "thumb", "art", "banner", "theme",
               "originallyAvailableAt", "year", "genre", "collection", "director",
               "writer", "actor", "duration", "producer", "country", "index", "label",
               "audienceRating", "guid", "editionTitle", "slug", "addedAt", "network",
               "viewCount", "userRating", "parentIndex"]

# Columns that take a LOCK without being editable fields. Measured: each is a 200 and
# recorded in `Field`, where plexsim answered 400 "unknown field"; `key`, `ratingImage`
# and `audienceRatingImage` stay a 400.
# Cycle 20, measured on plexscratch: `theme` and `banner` are ARTWORK columns on the
# edit path. The URL written is DISCARDED and the column becomes a versioned path of the
# row's own, minted at its current `updatedAt` -- the same shape a poster pick produces.
# Cycle 31, re-measured on plexscratch42 (1.42.2) and plexscratch (1.43.4): `thumb` and
# `art` belong here too. The column has to be EMPTY for the rule to be visible, which is
# why cycle 20 concluded they were not artwork columns -- on a row that already owns a
# poster the write looks like a no-op. Cleared first (1.43.4 through `DELETE .../thumb`,
# 1.42.2 on a show that owns none, since that DELETE does not exist there):
#
#   1.42.2  show 15, no thumb   PUT ?thumb.value=http://example.com/x.jpg
#                               -> 200, thumb = /library/metadata/15/thumb/1789514219
#   1.43.4  movie 1, cleared    PUT ?thumb.value=...
#                               -> 200, thumb = /library/metadata/1/thumb/1789554952
#
# The url is DISCARDED and the column becomes the row's own versioned path, exactly as
# `theme` and `banner` already did. There is no image behind it either way -- a GET of the
# new path is a 404 on both references -- so it is a column write and nothing more.
# plexsim discarded the value AND the column, so the Edit dialog's artwork fields wrote
# nothing and the save read as a silent no-op.
ARTWORK_VALUE_FIELDS = {"theme", "banner", "thumb", "art"}

LOCK_ONLY_FIELDS = {"thumb", "art", "theme", "banner", "guid", "slug", "addedAt",
                    # Cycle 53: nine of the ten database columns take the write and
                    # store nothing; `isAdult` is the one that reads back, so it is an
                    # EDITABLE field rather than a lock-only one.
                    "remote", "resourcesChangedAt", "userFields", "lastSkippedAt",
                    "mediaAnalysisVersion", "beginsAt", "endsAt", "agent",
                    "viewCount", "network", "parentIndex",
                    # Cycle 20: `lastViewedAt` records a lock as well -- plexsim
                    # accepted the write and left the `Field` list alone.
                    "lastViewedAt",
                    # Cycle 21, swept on plexscratch over 41 candidate names drawn
                    # from both references' `Meta` keys and every column findings.md
                    # records: each of these is a 200 that stores a lock and a `Field`
                    # row, and stores no VALUE, where plexsim answered 400 "unknown
                    # field". Eleven names (`album`, `artist`, `chapterSource`,
                    # `childCount`, `hasChapterVideoStream`, `leafCount`, `logo`,
                    # `poster`, `role`, `sourceType`, `track`) are a 400 on both and
                    # stay refused.
                    #
                    # Cycle 30 settled the two questions cycle 21 had to leave open, on
                    # **plexscratch42** -- a fresh writable 1.42.2 copy built for exactly
                    # this (HANDOVER 4). On 1.42.2 the five media-codec names (`atmos`,
                    # `audioCodec`, `audioLayout`, `subtitleCodec`, `videoCodec`) are each
                    # a **400** where 1.43.4 answers 200, so plexsim's refusal was right
                    # and that hold-out is CLOSED rather than implemented. `hdr10plus` is
                    # the opposite: a **200** on 1.42.2 as well as on 1.43.4 -- two
                    # witnesses, both versions. Cycle 31 read the `Field` list BACK and it
                    # does not belong here: the 200 stores nothing at all, not even a lock,
                    # so the name lives in `NO_OP_FIELDS` below.
                    "audioLanguage", "dovi", "duplicate", "file", "format", "hdr",
                    "id", "lastRatedAt", "make", "mediaBitrate", "mediaSize", "model",
                    "parent", "place", "resolution", "source", "subformat",
                    "subtitleLanguage", "tag", "trash", "unmatched",
                    "unviewedLeafCount", "unwatched", "viewedOrInProgressLeafCount"}


class EditError(Exception):
    """An edit a real PMS refuses. `status` is the code it answers with."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail

def parse_date(value):
    """An `originallyAvailableAt` as a real PMS reads it, or None when it cannot.

    Measured value by value on a writable copy of the reference: `2007-2-3` is
    `2007-02-03`, a day past the month's end ROLLS OVER (`2007-02-30` is
    `2007-03-02`) while a day outside 1-31 or a month outside 1-12 is refused, a
    trailing time is dropped (`2003-04-05T23:30:00Z` is `2003-04-05`, no zone shift),
    a two-digit year is that year (`99-01-02` is `0099-01-02`), and a bare INTEGER is
    epoch seconds (`20070203` is `1970-08-21`, `-5` is `1969-12-31`). `2001/02/03`,
    `2001-02`, a leading space and trailing text are refused.
    """
    text = str(value)
    if re.fullmatch(r"-?\d+", text):
        try:
            return (datetime.date(1970, 1, 1)
                    + datetime.timedelta(seconds=int(text))).isoformat()
        except OverflowError:
            return None
    match = re.fullmatch(r"(\d{1,4})-(\d{1,2})-(\d{1,2})"
                         r"(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?",
                         text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    if year < 1 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    try:
        return (datetime.date(year, month, 1) + datetime.timedelta(days=day - 1)).isoformat()
    except OverflowError:
        return None


PLAYLIST_BASE = 40000
COLLECTION_BASE = 50000

# Tag-type parameter name -> the row key that holds it. The web client builds
# edit params from the LOWERCASED tag type (`getTagSaveParams`: `collection[0]
# .tag.tag=`), and `actor` writes `Role` rather than `Actor`.
TAG_KEYS = {
    "collection": "Collection",
    "genre": "Genre",
    "director": "Director",
    "writer": "Writer",
    "producer": "Producer",
    "actor": "Role",
    "country": "Country",
    "label": "Label",
    "similar": "Similar",
    "mood": "Mood",
    "style": "Style",
}


def _now():
    return int(time.time())


# The Plex type numbers a collection row's `ratingCount` is built from.
# Measured in cycle 15; see _collection_row.
TYPE_NUMBERS = {"movie": 1, "show": 2, "season": 3, "episode": 4}


class Library:
    """Watch state, edits, playlists, collections and play history."""

    def __init__(self, catalog, notify=None, resolve=None):
        self.catalog = catalog
        # Called with a stored `server://` URI to re-evaluate a SMART playlist or
        # collection. It has to be a callback rather than a method here because
        # the filter engine lives on the server, and a smart list is evaluated
        # LIVE: measured on the reference, scrobbling one movie drops it out of a
        # smart playlist built on `?unwatched=1` and unscrobbling puts it back.
        # A stored row list would be a snapshot, and the list would go stale in a
        # way nothing on the surface reveals.
        self._resolve = resolve
        # Called with (event, activity) when an activity starts or ends, so the
        # server can push it down the notification socket. A client watches that
        # channel for the Activity indicator rather than polling /activities.
        self._notify = notify
        self._playlists = {}
        self._collections = {}
        self._next_playlist = PLAYLIST_BASE
        self._next_collection = COLLECTION_BASE
        self._next_tag_id = 90000
        # The tag TABLE: every tag this server has ever minted, by (row key,
        # lowercased name). A real PMS keeps tags independently of membership, so a
        # tag removed from the last row and re-added comes back with its original
        # id and spelling -- measured. plexsim looked only at the rows, so clearing
        # a tag lost its identity.
        self._tags = {}
        # Play history, newest last -- the order /status/sessions/history/all
        # returns (measured on fun: the newest rows are at the highest offset).
        self._history = []
        self._next_history = 0
        # Completed views per ratingKey, for /users/top. Measured: the reference's
        # `globalViewCount` survives a history delete and an unscrobble (movie 1
        # reports 2 with no history rows and no viewCount), so it is its own count
        # and not something to derive from either.
        self._views = {}
        self._activities = []

    # -- watch state -----------------------------------------------------

    def import_account_state(self, state):
        """Apply one measured scrobbles.plex.tv state row by its Plex GUID.

        This is an import, not a local scrobble: it must not mint a history row,
        increment an already-complete item, or replace the cloud timestamps with
        the current wall clock.
        """
        guid = state.get("guid")
        row = next((item for item in self.catalog.items.values()
                    if item.get("guid") == guid), None)
        if row is None:
            return False
        viewed_at = state.get("viewedAt")
        if not isinstance(viewed_at, int):
            return False
        view_state = state.get("viewState")
        if view_state == "complete":
            row["viewCount"] = max(1, int(row.get("viewCount") or 0))
            row["lastViewedAt"] = viewed_at // 1000
            row.pop("viewOffset", None)
        elif view_state == "unplayed":
            row.pop("viewCount", None)
            row.pop("lastViewedAt", None)
            row.pop("viewOffset", None)
        else:
            logger.warning("unknown cloud viewState %r for %s", view_state, guid)
            return False

        # In all 39 measured item observations rating and ratedAt were paired.
        # Twenty explicit `rating: -1` rows are the clear operation; the ten rows
        # with neither field are view-only changes and must not erase local stars.
        if "rating" in state and "ratedAt" in state:
            rating = state["rating"]
            rated_at = state["ratedAt"]
            if not isinstance(rating, int) or not isinstance(rated_at, int):
                return False
            if rating < 0:
                row.pop("userRating", None)
                row.pop("lastRatedAt", None)
            else:
                row["userRating"] = float(rating)
                row["lastRatedAt"] = rated_at // 1000
        elif "rating" in state or "ratedAt" in state:
            return False
        self.recount(row)
        return True

    def _stamp_view(self, *rows):
        """Cycle 54: `viewUpdatedAt`, the time a row's per-user view state was written.

        Measured on plexscratch42 (1.42.2), one write at a time with every row's value
        bisected through `viewUpdatedAt>>=`: a rating (any value, -1 included), a
        scrobble, an unscrobble -- even of a row with nothing to clear -- a `/:/progress`
        that stores a position and a Remove from Continue Watching each stamp it, and an
        edit or a sub-minute progress does not. It is stamped on exactly the rows whose
        stored view state the write touches: a rating on the rated row alone, a scrobble
        of a season or a show on it and everything beneath it, and an EPISODE scrobble or
        progress on its season and show as well, whose own counter and `lastViewedAt` it
        writes. The stamp outlives the state it recorded.
        """
        now = _now()
        for row in rows:
            key = str((row or {}).get("ratingKey") or "")
            # Catalog rows only: a list's state row is minted per read.
            if key.isdigit() and self.catalog.items.get(int(key)) is row:
                row["_viewUpdatedAt"] = now

    def _descendants(self, row):
        out = []
        for child in self.catalog.children(row.get("ratingKey")):
            out.append(child)
            out.extend(self._descendants(child))
        return out

    def scrobble(self, rating_key, watched, account_id=1, device_id=1):
        """Mark played/unplayed. A show or season marks every episode beneath it.

        The client's own menu predicate reads `unviewedLeafCount` for a show, so
        marking a show played without updating its leaves leaves the menu
        offering "Mark as Played" forever.
        """
        row = self.state_row(rating_key)
        if row is None:
            return []
        targets = self._leaves_or_self(row)
        if watched and self._collections.get(int(row["ratingKey"])) is row:
            # Cycle 21, measured on a writable copy of the reference: a SCROBBLE is
            # where the two list types part. A collection's is a 200 that changes
            # NOTHING -- it never carries `viewCount`, and it does not even clear a
            # `viewOffset` a progress write left -- while its UNSCROBBLE really
            # clears the view state, so only the watched half is the no-op. A
            # playlist counts both halves, up from the 1 it is born with (1 -> 2 ->
            # 3) and back to nothing. Neither reaches a member. The test is identity,
            # because a STORED list row carries no `type` of its own: the projection
            # mints it.
            targets = []
        for leaf in targets:
            if watched:
                leaf["viewCount"] = int(leaf.get("viewCount", 0)) + 1
                leaf["lastViewedAt"] = _now()
                leaf.pop("viewOffset", None)
                # Cycle 25, re-measured on plexscratch and OVERTURNING cycle 9's rule
                # above: a scrobble writes one history row per LEAF it marks. A movie
                # scrobbled twice adds two rows; a SEASON adds one per episode (9 rows
                # -> 12 on a three-episode season) and a SHOW one per leaf (12 -> 18);
                # an UNSCROBBLE removes none of them. Cycle 9 measured the copy's
                # history "staying at one row through three scrobbles"; driven again,
                # one scrobble at a time with the count read between each, it moves
                # every time. The row that does NOT appear is a timeline's, which is the
                # copy's already-recorded session-bound limit (HANDOVER section 8,
                # cycle 9) -- so only the additive half is asserted, the half where the
                # instrument produced a row rather than failed to.
                self.record_view(leaf, account_id, device_id, view_type=1)
            else:
                leaf.pop("viewCount", None)
                leaf.pop("viewOffset", None)
                leaf.pop("lastViewedAt", None)
        # A show or season keeps its OWN play counter, independent of its leaves:
        # measured, scrobbling one episode twice took the show to `viewCount 2`,
        # unscrobbling that episode left it at 2 (it increments, it does not
        # recompute), and unscrobbling the SHOW removed the field entirely. plexsim
        # emitted neither field on a parent at all, so a TV grid's "Last Played" sort
        # and its viewCount filter had nothing to read -- and then implemented that note
        # the wrong way round. Re-measured one scrobble at a time on a writable copy of
        # the reference: it is the EPISODE scrobble that adds one to its season's and
        # its show's counter (every time, even for an episode already watched), while
        # scrobbling the season or the show itself adds one to each episode and leaves
        # its own counter alone; unscrobbling a season or a show removes its own.
        if watched and row.get("type") == "episode":
            for key in ("parentRatingKey", "grandparentRatingKey"):
                parent = self.catalog.items.get(int(row[key])) \
                    if str(row.get(key) or "").isdigit() else None
                if parent is not None:
                    parent["viewCount"] = int(parent.get("viewCount", 0)) + 1
                    parent["lastViewedAt"] = _now()
        elif not watched and row.get("type") in ("show", "season"):
            row.pop("viewCount", None)
            row.pop("lastViewedAt", None)
        if targets:
            self._stamp_view(row, *self._descendants(row))
            if watched and row.get("type") == "episode":
                self._stamp_view(*self._parents(row))
        self.recount(row)
        return targets

    def state_row(self, rating_key):
        """The row a `/:/` state write lands on -- an item, a collection or a playlist.

        Cycle 21, measured on a writable copy of the reference with a fresh collection
        and a fresh playlist: both take EVERY write in this family, and `rate`,
        `progress` and `unscrobble` store on them exactly as they do on an item -- an 8
        is `userRating` plus `lastRatedAt`, and it survives the unscrobble that drops
        the view state. plexsim resolved the catalog alone, so all ten writes were a
        404 and the star rating on a collection page and on a playlist page errored.
        """
        row = self.catalog.items.get(rating_key)
        if row is None:
            row = self.collection(rating_key) or self.playlist(rating_key)
        return row

    def _parents(self, row):
        # A flattened show's episodes keep their season only privately (see
        # `apply_flatten`): the ROW must not name it, but the season is still the parent
        # a write stamps -- the suite reads it at the end, after a flatten.
        keys = [row.get("parentRatingKey") or row.get("_parentRatingKey"),
                row.get("grandparentRatingKey")]
        return [self.catalog.items.get(int(key)) for key in keys if str(key or "").isdigit()]

    def rate(self, rating_key, rating):
        """`/:/rate?key=&rating=` -- the star rating. -1 clears it."""
        row = self.state_row(rating_key)
        if row is None:
            return None
        self._stamp_view(row)
        if rating is None or rating == -1:
            row.pop("userRating", None)
            row.pop("lastRatedAt", None)
        elif rating < 0:
            # Measured on a writable copy of the reference: any OTHER negative rating
            # clears the stars; only -1 clears both. Cycle 54 re-measured it on both
            # copies from a row with NO rating: `-5` STAMPS `lastRatedAt` with the
            # current time. "Keeps" was a row rated seconds before, where the two
            # stamps cannot be told apart.
            row.pop("userRating", None)
            row["lastRatedAt"] = _now()
        else:
            row["userRating"] = float(rating)
            row["lastRatedAt"] = _now()
        return row

    # What each playback session last reported for an item, keyed (session, ratingKey).
    # A completion is a CROSSING, so the previous position is the whole state it needs.
    _reported = {}
    # The (session, ratingKey) pairs whose session has reported a position past the minute:
    # only those delete the resume point with a later sub-minute beat.
    _progressed = set()

    def progress(self, rating_key, offset, state=None, duration=None, device_id=1,
                 session="", completes=True):
        """A timeline update. `stopped` past 90% counts as a view, as PMS does.

        `completes=False` is `/:/progress`, which never completes a play: measured on
        a writable copy of the reference, `time=999999999` on a ten-second item is
        stored as the resume point, with no view and no history row.

        `device_id` is the client that played: the history row names it, and a
        real PMS lists every such id in the Dashboard's Device block.
        """
        row = self.state_row(rating_key)
        if row is None:
            return None
        # The duration the BEAT carries, and nothing else: measured on a writable copy of
        # the reference with half-hour media, a beat with no `duration` stores nothing and
        # completes nothing, however far along it says it is -- 95% of the item's own length
        # included. plexsim fell back to the row's duration, so a player that omits it (and
        # `/:/progress` never sends one) drove the whole watched-state machine.
        total = int(duration) if duration else 0
        key = (session or "", int(rating_key))
        # Cycle 58: a beat at EXACTLY the stored position writes nothing -- measured on both
        # copies in every state and from a new session and the same one alike:
        # `lastViewedAt` stands still and a row removed from Continue Watching stays
        # removed, where 1 ms either side stamps it and brings it back. It is what a
        # client sends for "resume, then stop before the position moves", and plexsim
        # re-sorted Continue Watching and un-hid the row the user had removed.
        unmoved = (offset is not None and row.get("viewOffset") is not None
                   and int(offset) == int(row["viewOffset"]))
        if offset is not None and not unmoved:
            # A timeline puts the row back in Continue Watching, whatever the menu
            # entry did before -- measured on the reference. For an episode that row is
            # its SHOW's, which any of the show's episodes hides and brings back.
            row.pop("_hiddenFromContinueWatching", None)
            if row.get("type") == "episode":
                for other in self.catalog.items.values():
                    if other.get("grandparentRatingKey") == row.get("grandparentRatingKey"):
                        other.pop("_hiddenFromContinueWatching", None)
            # Measured: a resume point under a MINUTE is not stored at all -- the
            # reference answers `playbackState: ignore` AND leaves the row untouched
            # at 3 000, 20 000 and 59 000 ms, and only starts recording at 61 000.
            # plexsim answered `ignore` and wrote the offset anyway, so a title
            # glanced at for two seconds appeared in Continue Watching.
            # Pinned since: EXACTLY 60 000 is not stored, and the rule holds for
            # EVERY state -- a `stopped` beat under a minute stores nothing either,
            # measured at 30 000 and at 59 500 of a 60 000 ms item. plexsim exempted
            # `stopped`, so stopping a trailer at ten seconds left a resume point on
            # it, and stopping one near its end marked it watched.
            # A position inside the last 10% is a completion, not a resume point:
            # the reference answers `viewOffset: -1` from there on.
            near_end = completes and total and int(offset) >= total * 0.9
            # Nothing is stored for a TIMELINE beat whose own `duration` is missing or zero:
            # measured, the row is untouched at 130 s and at 95% of its real length alike.
            # `/:/progress` (`completes=False`) is the exception, and never sends a duration:
            # cycle 9 measured it storing any position past a minute.
            if (total or not completes) and int(offset) > 60000 and not near_end:
                row["viewOffset"] = int(offset)
                row["lastViewedAt"] = _now()
                self._stamp_view(row, *self._parents(row))
                # Measured: a progress beat stamps `lastViewedAt` on the SEASON and the SHOW
                # as well, before anything is watched -- plexsim touched the parents only on
                # a completion, so a show part-way through an episode sorted as never played.
                for name in ("parentRatingKey", "grandparentRatingKey"):
                    parent = self.catalog.items.get(int(row[name])) \
                        if str(row.get(name) or "").isdigit() else None
                    if parent is not None:
                        parent["lastViewedAt"] = row["lastViewedAt"]
            elif total and int(offset) <= 60000 and key in self._progressed:
                # Measured: a beat at or under a minute DELETES the stored resume point,
                # rather than being ignored -- but only inside a playback SESSION that has
                # already reported a position past the minute (cycle 58, both copies). A
                # NEW session's sub-minute beats leave the stored offset alone and answer
                # `ignore` with it, which is "Play from beginning, stop within a minute":
                # a real PMS keeps the old resume point in Continue Watching and plexsim
                # deleted it. `lastViewedAt` is left alone either way.
                row.pop("viewOffset", None)
        # A play COMPLETES past 90% on any state -- measured: a `playing` beat at 95%
        # answers `complete` and sets viewCount, and the `stopped` beat after it
        # counts nothing again. Nothing under a minute ever completes. `_completed`
        # names the playback session that completed it, so one play does not count
        # twice while another client's play still counts; a beat back under 90% is a
        # new play and clears it.
        position = int(offset) if offset is not None else int(row.get("viewOffset") or 0)
        # The crossing rule, measured on half-hour media: a play is watched only when THIS
        # session's previous position for the item was under 90% of the duration this beat
        # carries. A cold item beaten once at 95% records nothing, and five climbing beats
        # from 95% record nothing either; another client's earlier position does not count,
        # and neither does an offset stored by `/:/progress`. plexsim completed on the
        # position alone.
        seen = self._reported.get(key)
        if offset is not None and total:
            self._reported[key] = position
            if position > 60000:
                self._progressed.add(key)
        crossed = seen is not None and total and seen < total * 0.9
        # Cycle 45 REFUTED the `position > 60000` gate that used to stand here. The
        # minute rule is real for the RESUME POINT -- a beat at or under 60 000 ms
        # deletes the stored offset, measured and still applied above -- and NOT for
        # completion. Measured on plexscratch42 (1.42.2) and plexscratch (1.43.4):
        # `duration=66667` beaten 30000 -> 60000, `duration=3000` beaten 900 -> 3000 and
        # `duration=10030` beaten 6000 -> 10000 each COMPLETE, and a real Plex Web client
        # completing kids' 10 030 ms row is the third witness. With the gate, nothing
        # whose 90% point falls under ~66.7 s could ever be marked watched.
        #
        # And the threshold itself is INTEGER arithmetic, `(duration * 9) // 10`, not a
        # float `* 0.9`. Bisected on plexscratch42: at `duration=66667` the boundary is
        # exactly 60 000 (59 999 ignores, 60 000 completes) where `66667 * 0.9` is
        # 60 000.3 and a float comparison would need 60 001. At `duration=100000` both
        # forms agree at 90 000, which is why no earlier cycle could see the difference.
        if completes and crossed and total and position >= (total * 9) // 10:
            if row.get("_completed") != (session or True):
                row["_completed"] = session or True
                row.pop("_completionStopped", None)
                row["viewCount"] = int(row.get("viewCount", 0)) + 1
                # Cycle 57: the view came from a PLAYBACK SESSION -- a section's own
                # Continue Watching shelf lists a show's next episode only once one of
                # these exists. Cycle 58: it is HISTORY, not view state -- a later
                # scrobble or unscrobble of the same leaf leaves it standing.
                row["_viewedInSession"] = True
                row.pop("viewOffset", None)
                # Cycle 45: the completion stamps the ROW's own `lastViewedAt`, which
                # plexsim set only when a `progress` beat happened to run first --
                # measured on both copies, where a single crossing pair leaves it set. A
                # watched row without it sorts last under `sort=lastViewedAt`, never
                # reaches `/recentlyViewed` and cannot win On Deck's most-recently-viewed
                # rule.
                row["lastViewedAt"] = _now()
                self._stamp_view(row, *self._parents(row))
                self.record_view(row, device_id=device_id)
                # A completed play bumps the parents' own counters too.
                for key in ("parentRatingKey", "grandparentRatingKey"):
                    parent = self.catalog.items.get(int(row[key])) \
                        if str(row.get(key) or "").isdigit() else None
                    if parent is not None:
                        parent["viewCount"] = int(parent.get("viewCount", 0)) + 1
                        parent["lastViewedAt"] = _now()
                self.recount(row)
        elif offset is not None and total and int(offset) < total * 0.9:
            row.pop("_completed", None)
            row.pop("_completionStopped", None)
        return row

    def remove_from_continue_watching(self, rating_key):
        """The menu entry of the same name: HIDE the row, keep the resume point.

        Measured: the reference answers 200, the row KEEPS its `viewOffset` (91 000
        was still there afterwards), and it simply stops appearing in
        `/library/onDeck` and `/hubs/continueWatching` -- until the next timeline
        brings it back. plexsim deleted the offset, so "Remove from Continue
        Watching" also threw away the resume position and the next Play started the
        item over.
        """
        row = self.catalog.items.get(rating_key)
        if row is None:
            return None
        row["_hiddenFromContinueWatching"] = True
        # Cycle 54: the write lands on the Continue Watching ROW, which for an episode is
        # its SHOW's -- measured, the show's `viewUpdatedAt` moves and the episode's does
        # not.
        if row.get("type") == "episode" and str(row.get("grandparentRatingKey") or "").isdigit():
            self._stamp_view(self.catalog.items.get(int(row["grandparentRatingKey"])))
        else:
            self._stamp_view(row)
        return row

    def recount(self, row):
        """Recompute viewedLeafCount up the tree from a changed leaf."""
        parents = []
        if row.get("type") == "episode":
            parents = [row.get("parentRatingKey"), row.get("grandparentRatingKey")]
        elif row.get("type") == "season":
            parents = [row.get("ratingKey"), row.get("parentRatingKey")]
        elif row.get("type") == "show":
            parents = [row.get("ratingKey")]
            parents += [s.get("ratingKey") for s in self.catalog.children(row["ratingKey"])]
        for key in [p for p in parents if p]:
            parent = self.catalog.items.get(int(key))
            if parent is None:
                continue
            leaves = self.catalog.leaves(parent["ratingKey"])
            parent["leafCount"] = len(leaves)
            parent["viewedLeafCount"] = sum(1 for l in leaves if l.get("viewCount"))
            if parent.get("type") == "show":
                parent["childCount"] = len(self.catalog.children(parent["ratingKey"]))

    # -- play history ----------------------------------------------------

    def record_view(self, row, account_id=1, device_id=1, view_type=None):
        """One row of `/status/sessions/history/all`.

        Field set measured on fun (a production PMS with 3879 rows): an episode
        carries key/ratingKey/parentKey/grandparent*/index/parentIndex, a movie
        carries title and originallyAvailableAt, and both carry historyKey,
        librarySectionID as a STRING, viewedAt, accountID and deviceID.
        """
        # Measured: the key is a MONOTONIC counter, never reused -- the reference went
        # 34, 35, delete 35, then 36. plexsim derived it from the list length, so a
        # delete made the next play reuse the deleted key and a client keyed on
        # historyKey got two different plays under one key.
        self._next_history += 1
        key = str(row["ratingKey"])
        self._views[key] = self._views.get(key, 0) + 1
        entry = {
            "historyKey": "/status/sessions/history/%d" % self._next_history,
            "key": "/library/metadata/%s" % row["ratingKey"],
            "ratingKey": str(row["ratingKey"]),
            "librarySectionID": str(row.get("librarySectionID", "")),
            "type": row.get("type"),
            "viewedAt": _now(),
            "accountID": account_id,
            "deviceID": device_id,
        }
        # Cycle 25, measured on zen (1.42.2, 73 episode rows) and on plexscratch
        # (1.43.4): an EPISODE history row carries `title` and `originallyAvailableAt`
        # too, so it is not the movie's fields against the episode's -- every row of
        # both types carries both. The comment above says the field set was measured on
        # fun; both current references disagree with it, and the History page listed the
        # episode with no episode name at all.
        # Cycle 70, measured on plexscratch42 (1.42.2) and kids (1.43.4) with Plex for
        # Android's own beats: a SCROBBLE writes a row with `viewType: 1`, and a play a
        # timeline completes writes one with no `viewType` at all. The two never overlap
        # -- 191 and 129 rows on the copy -- and the kind decides which listing shows it.
        if view_type is not None:
            entry["viewType"] = view_type
        entry["_history"] = True
        if row.get("type") == "episode":
            for source, target in (("parentRatingKey", "parentKey"),
                                   ("grandparentRatingKey", "grandparentKey")):
                if row.get(source):
                    entry[target] = "/library/metadata/%s" % row[source]
            for key in ("grandparentTitle", "index", "parentIndex", "thumb",
                        "parentThumb", "grandparentThumb", "grandparentArt"):
                if row.get(key) is not None:
                    entry[key] = row[key]
        else:
            if row.get("thumb") is not None:
                entry["thumb"] = row["thumb"]
        for key in ("title", "originallyAvailableAt"):
            if row.get(key) is not None:
                entry[key] = row[key]
        self._history.append(entry)
        return entry

    def delete_stream(self, stream_id):
        """Remove one uploaded sidecar stream from wherever it sits."""
        for row in self.catalog.items.values():
            for media in row.get("Media") or []:
                for part in media.get("Part") or []:
                    streams = part.get("Stream") or []
                    kept = [s for s in streams if int(s.get("id") or 0) != int(stream_id)]
                    if len(kept) != len(streams):
                        part["Stream"] = kept
                        return True
        return False

    def _forget_item(self, rating_key):
        """Strip a removed row's keys from its history rows, as a real PMS does.

        Measured: after the scanner removed an item its history row SURVIVED as
        `{historyKey, title, viewedAt}` -- `ratingKey` and `key` were stripped.
        plexsim left both, so the History page linked a 404.
        """
        for entry in self._history:
            if str(entry.get("ratingKey")) == str(rating_key):
                entry.pop("ratingKey", None)
                entry.pop("key", None)

    def history(self):
        return list(self._history)

    def delete_history(self, key):
        """Drop one history row by its `historyKey`. True if there was one."""
        tail = str(key).rsplit("/", 1)[-1]
        for index, entry in enumerate(self._history):
            if str(entry.get("historyKey") or "").rsplit("/", 1)[-1] == tail:
                del self._history[index]
                return True
        return False

    # -- activities ------------------------------------------------------

    def add_activity(self, subtitle, title="Updating Metadata",
                     kind="library.update.item.metadata", progress=0):
        """Shape measured on fun mid-refresh: uuid, type, cancellable, userID,
        title, subtitle, progress."""
        activity = {
            "uuid": str(uuid.uuid4()),
            "type": kind,
            "cancellable": False,
            "userID": 1,
            "title": title,
            "subtitle": subtitle,
            "progress": progress,
        }
        self._activities.append(activity)
        if self._notify:
            self._notify("started", activity)
        return activity

    def notify_activity(self, activity, event):
        """Push one activity frame for an activity that is already open."""
        if self._notify:
            self._notify(event, activity)

    def end_activity_quietly(self, activity):
        """End an activity with started -> ended and NO `updated` between.

        Cycle 15: the chapter-thumbs activity an analyze pushes has exactly two
        frames on the copy, where `finish_activity` inserts an `updated` for the
        refresh sequence that needs one.
        """
        self._activities = [a for a in self._activities
                            if a["uuid"] != activity["uuid"]]
        if self._notify:
            self._notify("ended", activity)

    def finish_activity(self, activity):
        """End an activity the way a real refresh does: an `updated` frame, then
        `ended` at 100, and the row leaves /activities.

        plexsim only ever fired `started`, and only `cancel_activity` ever fired
        `ended` -- so an activity that nothing cancelled stayed in /activities for
        the life of the process and the header's spinner never stopped. Measured on
        the reference: the started frame's progress is **-1**, not 0, and it reaches
        `ended` with 100.
        """
        if activity not in self._activities:
            return False
        if activity.get("progress") != 49:
            # The refresh sequence pushes its own `updated 49` before the processing
            # timeline (measured on the copy); every other caller still gets one here.
            activity["progress"] = 49
            if self._notify:
                self._notify("updated", activity)
        activity["progress"] = 100
        self._activities = [a for a in self._activities
                            if a["uuid"] != activity["uuid"]]
        if self._notify:
            self._notify("ended", activity)
        return True

    def activities(self):
        return list(self._activities)

    def cancel_activity(self, activity_uuid):
        ending = [a for a in self._activities if a["uuid"] == activity_uuid]
        self._activities = [a for a in self._activities if a["uuid"] != activity_uuid]
        for activity in ending:
            if self._notify:
                self._notify("ended", activity)
        return bool(ending)

    def clear_activities(self):
        self._activities = []

    # -- metadata edits --------------------------------------------------

    def edit(self, row, params, pairs=None, section=False):
        if row.get("type") == "episode":
            _written = [value for name, value in (params or {}).items()
                        if name.split(".")[0] == "contentRating"
                        and not name.endswith(".locked")]
            if any(str(value).strip() for value in _written):
                # an explicit write makes the rating the EPISODE's own, and a
                # later show write leaves it alone -- measured cycle 16
                row["_own_content_rating"] = True
            elif _written:
                # Cycle 20: an EMPTY write gives the row back to the show; see
                # the re-inherit at the end of `_edit`.
                row.pop("_own_content_rating", None)
        if section and "titleSort.value" in (params or {}):
            # marks this write as the section path's, which keeps what was
            # typed until the next title write -- measured cycle 16
            row["_section_title_sort"] = True
        """`PUT /library/metadata/{id}?title.value=X&title.locked=1`.

        Four param shapes, all from the client's own `getSaveParams`:
          `<field>.value=`         set a field
          `<field>=`               the same, without the suffix -- also honoured
          `<field>.locked=0|1`     lock it against a future agent refresh
          `<tag>[N].tag.tag=`      set tag N of a tag type
          `<tag>[].tag.tag-=a,b`   remove those tags

        Raises EditError with the status a real PMS answers. Measured, parameter by
        parameter against the reference, and plexsim had all of this wrong:

          * an UNKNOWN field is a 400, not a silently stored attribute;
          * `year.value=abc` is a 500, and `year.value=-5` REMOVES the year;
          * an unparseable date removes the field rather than storing the text;
          * `userRating` is accepted and IGNORED on the item path (the star rating
            is /:/rate) and STORED on the section path;
          * on the item path only `.locked=1` locks -- `true`, `false` and an empty
            value all UNLOCK, where plexsim locked on anything non-empty; the
            section path 400s anything but an integer 0 or 1;
          * on the ITEM path an indexed tag list REPLACES the whole list rather
            than appending, and a `tag-` removal with any value CLEARS it; on the
            SECTION path (`section=True`, the Edit dialog's own save) the list
            MERGES and a removal removes only what it names;
          * a tag matches case-insensitively and keeps its canonical spelling;
          * an unknown TAG type is a 400.
        """
        # The ORDERED pairs matter: `genre[0]`, `genre[1]` build one desired list,
        # and a dict collapses them. The caller passes them when it has them.
        items = list(pairs) if pairs is not None else list(params.items())
        items = [(k, v) for k, v in items
                 if not k.startswith(("X-Plex-", "include", "exclude"))
                 and k not in ("_pairs", "id")
                 # Cycle 22, measured on plexscratch: `type` is the SECTION path's
                 # scoping parameter and nothing else there, but on the ITEM path a
                 # bare `type=1` -- or `type=4`, or `type=abc` -- is a 200 that reads
                 # no value and changes no row, exactly like every other name cycle 20
                 # added to the lockable set. plexsim dropped it on BOTH paths, so a
                 # form naming only `type` looked EMPTY on the item path and got the
                 # 400 an empty form answers.
                 and not (section and k == "type")]
        if section:
            # On the SECTION path a bare `field=value` is a FILTER that scopes the
            # edit, not a field to write -- measured: `?type=1&title=Sintel` edits
            # only Sintel. On the ITEM path the same shape IS an edit (a bare
            # `studio=` is applied). So which it means depends on the path.
            items = [(k, v) for k, v in items
                     if k.endswith((".value", ".locked")) or ".tag.tag" in k]
        if section and not items:
            # Cycle 17, measured on plexscratch: on the SECTION path a form that names
            # nothing to write is a 200 that edits nothing -- `?type=2&id={id}` alone,
            # with a bare unknown name, with `includeGuids=1` or with an item-prefs
            # option are all 200s -- because a bare `field=value` is a FILTER there and
            # a filter with nothing to write is a no-op. The unknown `.value` form
            # stays a 400 on this path, which is where the two paths really differ.
            return False
        if not items or (not section and not self._recognised(items)):
            # Measured: a PUT with no parameters at all is a 400.
            #
            # Cycle 17, measured on plexscratch: on the ITEM path the 400 means "this
            # request edits NOTHING", not "an unknown field". With a real edit beside
            # it the reference ignores every name it does not recognise -- an unknown
            # `.value`, a bare unknown name, an unknown TAG type and an option like
            # `flattenSeasons=1` all answer 200 and the edit lands -- while each of
            # them ALONE is a 400. The recorded rule was measured with the unknown
            # name alone, which is failure class A: a client appends its own options
            # to the save, and plexsim refused 34 of the 71 names swept.
            raise EditError(400, "bad request")

        # Cycle 17, measured on plexscratch: a save a real PMS REFUSES writes
        # NOTHING. `title.value=PARTIAL&year.value=abc` is a 500 there and the title
        # is untouched, on the item path and the section path alike. plexsim applied
        # each parameter as it validated it, so everything before the bad one landed
        # and everything after it did not -- the client is told the save failed, keeps
        # its old model, and the row on disk has moved, with nothing to tell the user.
        # A shallow snapshot is enough: every writer below REPLACES a value rather
        # than mutating one, so the row's nested Media/Part/Stream dicts keep their
        # identity across a restore.
        snapshot = dict(row)
        try:
            return self._edit(row, items, section)
        except EditError:
            row.clear()
            row.update(snapshot)
            raise

    def _edit(self, row, items, section):
        old_title = row.get("title")
        # A title write, a sort-title write and a sort-title unlock each re-derive an
        # unlocked Sort Title (`_derive_title_sort`).
        sort_touched = any(name in ("title", "title.value", "titleSort", "titleSort.value",
                                    "titleSort.locked") for name, _ in items)
        changed = False
        adding = {}     # tag type -> (index, position, value) per indexed entry
        removing = {}   # tag type -> the names a `tag-` removal lists
        for position, (name, value) in enumerate(items):
            if ".tag.tag-" in name:
                tag_type = name.split("[", 1)[0].lower()
                if tag_type not in TAG_KEYS:
                    if not section:
                        continue
                    raise EditError(400, "unknown tag field %r" % tag_type)
                # The client double-encodes the list: each name is quoted, then the
                # comma-joined list is quoted again (`Label%2520A%2CLabel%2520B`).
                removing.setdefault(tag_type, []).extend(
                    urllib.parse.unquote(part) for part in str(value).split(",") if part)
                changed = True
            elif name.split("[", 1)[0].lower() in IGNORED_TAG_TYPES \
                    or re.match(r"^actor\[\d*\]\.tagRole$", name):
                # Measured: accepted and discarded -- a role written as
                # `actor[0].tagRole=` is not stored, and the actor carries `role: ""`.
                changed = True
            elif ".tag.tag" in name:
                tag_type = name.split("[", 1)[0].lower()
                if tag_type not in TAG_KEYS:
                    if not section:
                        continue
                    raise EditError(400, "unknown tag field %r" % tag_type)
                # Measured: the INDEX orders the list, not the parameter's position --
                # `genre[1]=Second&genre[0]=First` is [First, Second].
                index = name.split("[", 1)[1].split("]", 1)[0] if "[" in name else ""
                adding.setdefault(tag_type, []).append(
                    (int(index) if index.isdigit() else position, position, value))
                changed = True
            elif name.endswith(".locked"):
                field = name[: -len(".locked")]
                if field not in EDITABLE_FIELDS and field not in TAG_KEYS \
                        and field not in LOCK_ONLY_FIELDS and field not in NO_OP_FIELDS:
                    if not section:
                        continue
                    raise EditError(400, "unknown field %r" % field)
                if field in NO_OP_FIELDS:
                    # Cycle 31: accepted, and recorded NOWHERE -- see NO_OP_FIELDS.
                    changed = True
                    continue
                if section:
                    # Measured: the section path PARSES the lock as an integer that must
                    # be 0 or 1 -- `01` and `001` lock, `00` unlocks, and `true`, `2`,
                    # `-1`, ` 1` and an empty value are all a 400.
                    if not re.fullmatch(r"[0-9]+", str(value)) or int(value) > 1:
                        raise EditError(400, "bad lock %r" % value)
                    self._set_lock(row, field, int(value) == 1)
                else:
                    # Measured: on the item path `1` is the only value that locks, and
                    # `01`, `true` and an empty value unlock.
                    self._set_lock(row, field, str(value) == "1")
                changed = True
            else:
                field = name[: -len(".value")] if name.endswith(".value") else name
                if section and field in SECTION_REFUSED_FIELDS:
                    # Cycle 20: eight columns the item path accepts are a 500 on the
                    # section path for ANY value, a valid number included.
                    raise EditError(500, "refused %s on the section path" % field)
                if section and field in SECTION_INTEGER_FIELDS:
                    # ...and seven more are really parsed there. `_set_field` writes the
                    # ones that store and drops the two that do not.
                    self._set_field(row, field, value, section)
                    changed = True
                    continue
                if field in IGNORED_VALUE_FIELDS or field in NO_OP_FIELDS \
                        or (field in ("duration", "index") and row.get("type") == "movie") \
                        or (field in TAG_KEYS and not name.endswith(".value")):
                    # Measured: each is a 200 that changes nothing -- the value-less
                    # columns above, a movie's `duration` and `index`, and a bare tag
                    # name (`genre=Drama`) on the item path, where plexsim stored the
                    # first two and 400'd the rest.
                    changed = True
                    continue
                if field in ARTWORK_VALUE_FIELDS or (section and field == "slug"):
                    # Cycle 20: the artwork columns take a value on both paths -- the
                    # url is discarded and the column becomes the row's own versioned
                    # path -- and `slug` is accepted on the section path where plexsim
                    # answered 400.
                    if field in ARTWORK_VALUE_FIELDS:
                        if str(value) == "":
                            # Cycle 31, measured on plexscratch42 (1.42.2) and plexscratch
                            # (1.43.4), which agree exactly: an EMPTY artwork value is a 200
                            # that REMOVES the column, and the bare `/thumb` GET becomes a
                            # 404 -- the same shape `DELETE .../thumb` produces on 1.43.4.
                            #
                            # This matters beyond the grammar. On 1.42.2 that DELETE does
                            # not exist at all (it is a 404 on all five artwork leaves), so
                            # the empty-value edit is the ONLY way a client on the version
                            # plexsim reports can clear a row's artwork -- which is how the
                            # cleared-artwork state cycle 29's derivation rules are all
                            # about is reached there.
                            row.pop(field, None)
                        else:
                            row[field] = "/library/metadata/%s/%s/%s" % (
                                row["ratingKey"], field, row.get("updatedAt") or _now())
                    changed = True
                    continue
                if field not in EDITABLE_FIELDS:
                    # Cycle 17: on the ITEM path an unrecognised name is IGNORED when
                    # the form edits something else. See `_recognised` for the whole
                    # rule and what the recorded one was measured without.
                    if not section:
                        continue
                    raise EditError(400, "unknown field %r" % field)
                self._set_field(row, field, value, section)
                changed = True

        written = {name[: -len(".value")] if name.endswith(".value") else name
                   for name, _ in items}
        if section and "originallyAvailableAt" in written and "year" not in written:
            # Measured: on the SECTION path the release date sets the YEAR -- and
            # clearing it removes both -- unless the same save writes a year, which
            # wins in either order. The item path leaves the year alone.
            if row.get("originallyAvailableAt"):
                row["year"] = int(row["originallyAvailableAt"][:4])
            else:
                row.pop("year", None)
        for tag_type in set(adding) | set(removing):
            wanted = [value for _, _, value in sorted(adding.get(tag_type, []),
                                                      key=lambda entry: entry[:2]) if value]
            if section:
                self._merge_tags(row, tag_type, wanted, removing.get(tag_type, []))
            else:
                # Measured on the item path: ANY `tag-` removal clears the whole list,
                # whatever it names, and the indexed list in the same request rebuilds
                # it -- which is how the client's "remove one tag" works there. An
                # exact repeat is one tag; `Qx&qx` is the first spelling twice, because
                # the tag lookup ignores case and the dedupe does not.
                self._set_tags(row, tag_type, list(dict.fromkeys(wanted)))
        if "title" in written:
            row.pop("_generatedTitle", None)
        if "title" in written and row.get("title") == "":
            self._empty_title(row, old_title)
        elif sort_touched:
            self._derive_title_sort(row)
        if "index" in written and row.get("type") == "season" and "title" not in written \
                and GENERATED_SEASON_TITLE.match(str(old_title or "")):
            # Cycle 55, measured on plexscratch42: a season's title is GENERATED unless
            # the user wrote one (cycle 31 read `title = ''` for every season out of the
            # database), so it follows an index edit -- `index.value=0` is `Specials` and
            # putting the index back is `Season N` again. plexsim re-titled only rows it
            # had itself generated, so a catalog season kept `Season 20` at index 0.
            index = row.get("index")
            row["title"] = ("Specials" if str(index) == "0"
                            else "Season %s" % index if index is not None
                            else "[Unknown Season]")
            written = written | {"title"}
        elif "index" in written and row.get("_generatedTitle"):
            # Measured: a generated title follows the index -- `Episode 2` is
            # `Episode 7` after `index.value=7`.
            row["title"] = "%s %s" % ("Season" if row.get("type") == "season" else "Episode",
                                      row.get("index"))
            written = written | {"title"}
        if written & {"title", "studio", "index", "contentRating"}:
            self._follow_parent(row, written)
        if row.get("type") == "episode" and "contentRating" in written \
                and not row.get("contentRating"):
            # Cycle 20, measured on plexscratch: the inheritance is a READ-TIME
            # fallback, not a copy made when the show is written. Clearing an
            # episode's own rating (`contentRating.value=`) makes it read the show's
            # AT ONCE, and a later show write moves it again. plexsim copied the
            # show's value onto the episode at load and on a show edit, so a cleared
            # rating left the episode with none at all and its page drew a blank
            # badge for the life of the process. Re-inheriting here reproduces every
            # step of the measured sequence; the read-time hook itself would have to
            # reach the catalog from the row writer, so this is the write-side proxy
            # -- see HANDOVER.md 3.3.
            show = self.catalog.items.get(int(row["grandparentRatingKey"])) \
                if str(row.get("grandparentRatingKey") or "").isdigit() else None
            if (show or {}).get("contentRating"):
                row["contentRating"] = show["contentRating"]
        if row.get("type") == "season" and "year" in written:
            # Cycle 16, measured on plexscratch: a SEASON with no year of its
            # own carries the show's as `parentYear`, and one that LOSES its
            # year gains it in the same read. The rule is recorded and already
            # right at catalog LOAD; plexsim derived it only there, so a season
            # edited at runtime carried neither year and its page printed none.
            parent = self.catalog.items.get(int(row["parentRatingKey"])) \
                if str(row.get("parentRatingKey") or "").isdigit() else None
            if not row.get("year") and (parent or {}).get("year"):
                row["parentYear"] = parent["year"]
            else:
                row.pop("parentYear", None)
        # Cycle 20, measured on plexscratch behind a control write: an edit does NOT
        # move `updatedAt`. Five fields written to one movie over half a minute left
        # the column where it was, on both paths, while a poster write in the same
        # session moved it -- so the column follows the ARTWORK and the row's own
        # identity, not its text. It is the cursor python-plexapi and every sync client
        # polls, and plexsim stamped it on every edit, which made a title fix look like
        # a new row. The artwork writers stamp it themselves.
        return changed

    def _recognised(self, items):
        """Does this form name anything the ITEM edit path can write?

        Cycle 17: the item path's 400 is "nothing here is an edit". A name counts
        when it is an editable field (with or without `.value`), a lock on one, a
        known tag type, or one of the names the path accepts and discards.
        """
        for name, _ in items:
            if ".tag.tag" in name or re.match(r"^actor\[\d*\]\.tagRole$", name):
                if name.split("[", 1)[0].lower() in TAG_KEYS \
                        or name.split("[", 1)[0].lower() in IGNORED_TAG_TYPES:
                    return True
                continue
            field = name[: -len(".locked")] if name.endswith(".locked") else name
            field = field[: -len(".value")] if field.endswith(".value") else field
            if field in EDITABLE_FIELDS or field in TAG_KEYS \
                    or field in LOCK_ONLY_FIELDS or field in IGNORED_VALUE_FIELDS \
                    or field in NO_OP_FIELDS:
                return True
        return False

    def _set_field(self, row, field, value, section=False):
        if field == "isAdult":
            if str(value) == "1":
                row["isAdult"] = True
            else:
                row.pop("isAdult", None)
            return
        """One scalar field, with the coercions a real PMS applies."""
        if section and field in SECTION_INTEGER_FIELDS:
            # Cycle 20: parsed as an integer -- a word is a 500 -- and then stored only
            # if it is one of the four columns that really store. `duration` and
            # `parentIndex` are read and dropped, which is what the copy does.
            text = str(value).strip()
            if not re.fullmatch(r"[+-]?\d+", text):
                raise EditError(500, "unparseable %s %r" % (field, value))
            if field in SECTION_STORED_FIELDS:
                row[field] = int(text)
                if field == "updatedAt":
                    # The artwork version IS this column, so it moves with it -- the
                    # copy re-versions the poster in the same call.
                    for artwork in ("thumb", "art"):
                        current = str(row.get(artwork) or "")
                        head, slash, _ = current.rpartition("/")
                        if slash and head.startswith("/library/metadata/%s/" % row["ratingKey"]):
                            row[artwork] = "%s/%s" % (head, int(text))
            return
        if field in IGNORED_EDIT_FIELDS and not section:
            # Measured: accepted, lock recorded, value discarded -- the star
            # rating is written through /:/rate, not through the item path.
            return
        if field in FLOAT_FIELDS:
            # Measured on a writable copy of the reference: a rating is a number (`abc`
            # is a 500 on both paths), and an EMPTY one is a 500 on the item path and
            # removes the field on the section path. The section path STORES a
            # userRating, stamping lastRatedAt, and -1 or empty clears both -- the Edit
            # dialog's own save is a rating write. plexsim kept every rating as the
            # string it was sent and discarded userRating on both paths.
            text = str(value).strip()
            if (text == "" and not section) \
                    or (text and not re.fullmatch(r"[+-]?\d+(\.\d+)?", text)):
                raise EditError(500, "unparseable %s %r" % (field, value))
            number = float(text) if text else None
            if number is None or (number < 0 and field in ("userRating", "rating",
                                                           "audienceRating")):
                # Cycle 20, measured on plexscratch: a negative `rating` or
                # `audienceRating` REMOVES the field, the way a negative year does --
                # plexsim stored -3.0 and the preplay page printed it. `userRating`
                # already behaved this way.
                row.pop(field, None)
                if field == "userRating":
                    row.pop("lastRatedAt", None)
            else:
                row[field] = number
                if field == "userRating":
                    row["lastRatedAt"] = _now()
            return
        if value == "":
            if field == "year" and section:
                # Measured: on the SECTION path an empty year is removed, not refused.
                row.pop(field, None)
                return
            if field in INTEGER_FIELDS:
                # Measured: `year.value=` is a 500 on the item path, the same refusal
                # the filter grammar gives an empty integer.
                raise EditError(500, "unparseable %s %r" % (field, value))
            if field in KEEP_EMPTY_FIELDS:
                # Measured field by field: only `title` and `summary` are STORED as
                # the empty string. `studio`, `contentRating`, `originalTitle`,
                # `editionTitle` and `titleSort` are REMOVED, which is what lets the
                # agent supply them again -- and `titleSort` in particular is derived
                # from the title when absent.
                row[field] = ""
            else:
                row.pop(field, None)
            return
        if field in INTEGER_FIELDS:
            text = str(value).strip()
            if not re.fullmatch(r"[+-]?\d+", text):
                # Measured: a real PMS answers 500 for `year.value=abc`.
                raise EditError(500, "unparseable %s %r" % (field, value))
            number = int(text)
            if field == "year" and not -2147483648 <= number <= 2147483647:
                # Cycle 20, bisected on plexscratch: the column is a signed 32-bit
                # integer and the bound is `year`'s alone -- 2147483647 stores and
                # 2147483648 is a 500, -2147483648 stores and -2147483649 is a 500,
                # while the columns the item path does not read take anything.
                raise EditError(500, "year out of range %r" % value)
            if field in PARSED_NOT_STORED:
                # Cycle 31: parsed, refused if it is not an integer, and then dropped.
                row.pop(field, None)
            elif number < 0 or (field == "year" and number == 0):
                # Measured: a negative year REMOVES the field, and so does 0 -- while
                # `+2000` is 2000, where plexsim answered 500.
                row.pop(field, None)
            else:
                row[field] = number
            return
        if field in DATE_FIELDS:
            parsed = parse_date(value)
            if parsed is None and section:
                # Measured: the section path REFUSES a date it cannot read, with 500.
                raise EditError(500, "unparseable %s %r" % (field, value))
            if parsed is None:
                # Measured: the item path removes the field rather than storing the
                # text, which is what plexsim did.
                row.pop(field, None)
            else:
                row[field] = parsed
            return
        row[field] = value

    @staticmethod
    def _capitalised(value):
        """A NEW tag's spelling: its first character upper-cased when it is an ASCII letter.

        Cycle 61, measured on plexscratch42 (1.42.2) and plexscratch (1.43.4) for genre,
        label, director and collection tags, on the section and item edit paths: `c61c
        lowgenre` is stored `C61c lowgenre`, while `élan`, `ÿoyo`, `ǆemal`, `ßeta` and
        `1abc` keep their spelling and an EXISTING tag keeps its canonical one.
        """
        value = str(value)
        if value[:1] and "a" <= value[0] <= "z":
            return value[0].upper() + value[1:]
        return value

    def collection_tag_names(self):
        """Every collection tag name this server has held, lowercased -- the tag TABLE's."""
        return {lowered for (table_key, lowered) in self._tags if table_key == "Collection"}

    def _set_tags(self, row, tag_type, values):
        """Replace a tag list, which is what an indexed write means.

        Measured: `genre[0].tag.tag=A&genre[1].tag.tag=B` leaves EXACTLY [A, B] --
        the originals are gone. plexsim appended, so the Edit dialog could only
        ever add tags and the list grew every time it was saved.
        """
        key = TAG_KEYS.get(tag_type)
        if not key:
            return
        before = {str(t.get("tag")) for t in row.get(key) or []}
        existing = {str(t.get("tag")).lower(): t for t in row.get(key) or []}
        # A tag's canonical spelling and id survive: `huntgenrea` matches
        # `HuntGenreA` and comes back spelled the way it was first stored -- and it
        # survives the tag being removed from every row, because a real PMS keeps a
        # tag TABLE rather than storing tags only on the items. Measured: id 235
        # came back on a re-add after the tag had been cleared.
        for other in self.catalog.items.values():
            for tag in other.get(key) or []:
                existing.setdefault(str(tag.get("tag")).lower(), tag)
        for (table_key, lowered), tag in self._tags.items():
            if table_key == key:
                existing.setdefault(lowered, tag)
        fresh = []
        for value in values:
            known = existing.get(str(value).lower())
            if known is not None:
                fresh.append(dict(known))
                continue
            value = self._capitalised(value)
            tag_id = zlib.crc32(("%s:%s" % (key, value)).encode()) % 900000 + 1000
            minted = {"id": tag_id, "tag": value,
                      "filter": "%s=%s" % (tag_type, tag_id)}
            if key in PERSON_TAG_ELEMENTS:
                # Cycle 27: a PERSON tag carries a `tagKey` wherever it comes from, and
                # a tag the Edit dialog creates is a person tag like any other. The
                # catalog's own are minted at load (`hierarchy.normalize_rows`); this is
                # the other door into the tag table, and the suite found it -- after an
                # unmatch had dropped the agent's people and an edit had added one back,
                # the row's only `Role` had no key and its Cast & Crew tile linked to
                # `/library/people/undefined` again.
                minted["tagKey"] = person_tag_key(value)
            if key == "Role":
                # Measured: an actor added through an edit carries `role: ""`, where
                # plexsim omitted the field.
                minted["role"] = ""
            self._tags[(key, str(value).lower())] = minted
            existing[str(value).lower()] = minted
            fresh.append(minted)
        if fresh:
            row[key] = fresh
        else:
            row.pop(key, None)
        if key == "Collection":
            row.pop("_collectionLight", None)
            for entry in fresh:
                self._sync_collection_tag(row, entry["tag"])
            for gone in before - {str(t["tag"]) for t in fresh}:
                self._untag_collection(row, gone)

    def _follow_parent(self, row, written):
        """Copy a show's or a season's title and studio onto the rows beneath it.

        Measured on a writable copy of the reference: a season's title and studio land
        on its episodes' `parentTitle` and `parentStudio`, and a show's on its seasons'
        `parentTitle` and `parentStudio` and every episode's `grandparentTitle` -- on
        the detail row, the child list and the grid alike. An EMPTY show title removes
        them rather than sending "". plexsim copied these once, when the catalog
        loaded, so a renamed show kept its old name on every episode.
        """
        kind, key = row.get("type"), str(row.get("ratingKey"))
        if kind not in ("show", "season"):
            return

        def carry(child, name, value):
            if value:
                child[name] = value
            else:
                child.pop(name, None)
        for child in self.catalog.items.values():
            if str(child.get("parentRatingKey")) == key:
                if "title" in written:
                    carry(child, "parentTitle", row.get("title"))
                if "studio" in written:
                    carry(child, "parentStudio", row.get("studio"))
                if "index" in written and kind == "show":
                    # Cycle 16, measured on plexscratch: a SHOW's `index` lands on its
                    # seasons' `parentIndex` the same way a season's lands on its
                    # episodes'. plexsim moved the show's own index and left every
                    # season claiming the old one.
                    child["parentIndex"] = row.get("index")
                if "index" in written and kind == "season":
                    # Re-measured on the copy: a SEASON's `index` lands on its
                    # episodes' `parentIndex`, on the detail row and in `/children`
                    # alike -- the same rule as the title and the studio above, which
                    # cycle 9 measured and this one missed. An episode left pointing at
                    # a season number that no longer exists draws an empty season in
                    # any client that groups by `parentIndex`.
                    child["parentIndex"] = row.get("index")
            elif kind == "show" and str(child.get("grandparentRatingKey")) == key \
                    and "title" in written:
                carry(child, "grandparentTitle", row.get("title"))
            if kind == "show" and "contentRating" in written \
                    and str(child.get("grandparentRatingKey")) == key:
                # Measured on the copy: a show's `contentRating` moves onto the
                # episodes beneath it, and clearing it clears theirs. The read side on
                # kids and zen is the same rule seen from the other end -- every
                # episode of a show carries the show's certificate, a season carries
                # none. plexsim derived it only at load, so a show edited afterwards
                # left its episodes showing a certificate it no longer had. A real PMS
                # keeps an episode's OWN agent rating separate and moves only the
                # inherited ones; plexsim's catalog has one column for both.
                # Cycle 16, re-measured on plexscratch: the cascade reaches only the
                # episodes that carry NO rating of their own. Three matched episodes
                # kept theirs while three unmatched ones followed the show, and an
                # episode written explicitly survived the next show write. plexsim
                # stamped the show's rating on every episode, destroying an explicit
                # per-episode rating on the next edit of its show. The catalog has one
                # column for both, so the flag records which ones are the row's own.
                if not child.get("_own_content_rating"):
                    carry(child, "contentRating", row.get("contentRating"))

    def _empty_title(self, row, old_title):
        """An EMPTIED title, measured on a writable copy of the reference.

        A season becomes `Season N` and an episode `Episode N` -- a title GENERATED from
        the index, which follows a later index edit -- while a movie, a show and a list
        keep "". Every one keeps its old title's sort form as `titleSort` (`The Old
        List` sorts as `Old List`, `Bushwhacked` as `Bushwhacked`) unless the sort title
        is locked. plexsim stored "" for all of them and dropped the sort title.
        """
        locked = any(f.get("name") == "titleSort" and f.get("locked") for f in row.get("Field") or [])
        if old_title and not locked:
            row["titleSort"] = sort_title(old_title)
        if row.get("type") in ("season", "episode"):
            row["_generatedTitle"] = True
            row["title"] = "%s %s" % ("Season" if row["type"] == "season" else "Episode", row.get("index"))

    def _derive_title_sort(self, row):
        """Measured on a writable copy of the reference: a Sort Title that is not
        LOCKED does not stick. A title write, a sort-title write and an unlock all
        re-derive it from the title, article stripped -- `The Thing` sorts as `Thing`
        whatever `titleSort.value` said, a season retitled `The Season Name` sorts as
        `Season Name` -- and only a lock, sent with the value or before the title
        changes, keeps what was typed. An EMPTY title leaves it as it was. A
        collection and a playlist follow the same rule. plexsim stored whatever it
        was sent, so a Sort Title typed without its lock filed the row where a real
        server would not.
        """
        if any(f.get("name") == "titleSort" and f.get("locked") for f in row.get("Field") or []):
            return
        if row.pop("_section_title_sort", None):
            # Cycle 16, measured on plexscratch: the SECTION save path STORES an
            # unlocked `titleSort.value=` -- it survives until the next TITLE write,
            # which re-derives it -- while the ITEM path discards it, which is the rule
            # already recorded. That recorded rule is the item path's alone; plexsim
            # applied it to both, so a Sort Title typed into the grid's own edit dialog
            # vanished the moment it was saved.
            return
        title = row.get("title")
        if not title:
            return
        if row.get("type") == "season" and GENERATED_SEASON_TITLE.match(title):
            # Cycle 20: a title the SERVER generated stores no sort title at all --
            # zen's five `[Unknown Season]` rows carry none, where stripping the
            # leading `[` would leave `Unknown Season]`. `Season N` and `Specials`
            # derive to themselves and so were already absent; this is the third
            # label, and the only one where the derivation differs from the title.
            row.pop("titleSort", None)
            return
        derived = sort_title(title)
        if derived != title:
            row["titleSort"] = derived
        else:
            row.pop("titleSort", None)

    def _merge_tags(self, row, tag_type, wanted, removed):
        """The SECTION path's tag write, which merges where the item path replaces.

        Measured on a writable copy of the reference, through the Edit dialog's own
        save (`PUT /library/sections/{id}/all?type=&id=`): the indexed list goes
        FIRST, deduplicated case-insensitively, and the row's other tags follow in
        their order; a `tag-` removal removes exactly the names it lists, ignoring
        ones the row does not have; and an empty indexed value changes nothing.
        plexsim replaced or cleared the list here as it does on the item path, so a
        partial save -- python-plexapi's addGenre, a second client -- lost tags.
        """
        key = TAG_KEYS.get(tag_type)
        gone = {str(name).lower() for name in removed}
        first, seen = [], set()
        for value in wanted:
            if str(value).lower() not in seen:
                seen.add(str(value).lower())
                first.append(value)
        if not first and not gone:
            return
        kept = [str(t.get("tag")) for t in row.get(key) or []
                if str(t.get("tag")).lower() not in seen | gone]
        self._set_tags(row, tag_type, first + kept)

    def _set_lock(self, row, field, locked):
        """A real PMS reports locks as a `Field` array on the row, in COLUMN order.

        Measured: the order is fixed (`FIELD_ORDER`, the metadata table's columns),
        not the order the client wrote them -- plexsim appended, so re-locking a
        field moved it to the end.
        """
        fields = [f for f in row.get("Field", []) if f.get("name") != field]
        if locked:
            fields.append({"locked": True, "name": field})
        fields.sort(key=lambda f: (FIELD_ORDER.index(f["name"])
                                   if f.get("name") in FIELD_ORDER
                                   else len(FIELD_ORDER), f.get("name")))
        if fields:
            row["Field"] = fields
        else:
            row.pop("Field", None)

    def _add_tag(self, row, tag_type, value):
        key = TAG_KEYS.get(tag_type)
        if not key or not value:
            return
        tags = row.setdefault(key, [])
        if any(t.get("tag") == value for t in tags):
            return
        # Minted the way the catalog mints them (Catalog._number_tags): from the
        # text, so the same tag added twice keeps one id and the filter agrees.
        tag_id = zlib.crc32(("%s:%s" % (TAG_KEYS[tag_type], value)).encode()) % 900000 + 1000
        minted = {"id": tag_id, "tag": value,
                  "filter": "%s=%s" % (tag_type, tag_id)}
        if key in PERSON_TAG_ELEMENTS:
            minted["tagKey"] = person_tag_key(value)      # see `_set_tags`
        # Cycle 61: a membership puts the tag in the TABLE too, where it outlives the
        # collection (a later rename onto it is a 409 -- see `collection_tag_names`).
        self._tags.setdefault((key, str(value).lower()), dict(minted))
        tags.append(minted)
        if key == "Collection":
            self._sync_collection_tag(row, value)

    def _remove_tags(self, row, tag_type, values):
        key = TAG_KEYS.get(tag_type)
        if not key:
            return
        lowered = {v.lower() for v in values}
        kept = [t for t in row.get(key, []) if str(t.get("tag", "")).lower() not in lowered]
        if kept:
            row[key] = kept
        else:
            row.pop(key, None)
        if key == "Collection":
            row.pop("_collectionLight", None)
            for value in values:
                self._untag_collection(row, value)

    def remove_collection_item(self, collection, row):
        """Drop one member from a collection, and its Collection tag with it."""
        before = len(collection["items"])
        collection["items"] = [k for k in collection["items"]
                               if int(k) != int(row["ratingKey"])]
        if len(collection["items"]) != before:
            collection["updatedAt"] = _now()
        tags = [t for t in row.get("Collection") or []
                if str(t.get("tag")) != str(collection.get("tag", collection["title"]))]
        if tags:
            row["Collection"] = tags
        else:
            row.pop("Collection", None)
        return before != len(collection["items"])

    def _untag_collection(self, row, title):
        """Drop this row from the named collection. The collection itself SURVIVES.

        Cycle 49 REFUTED the premise the deletion here rested on. The old comment said
        the reference "also leaves the stale tag on the item, which is a state plexsim
        has no way to represent consistently" -- it does not. Measured on plexscratch42
        (1.42.2): a collection created over one movie, then that movie's collection list
        replaced, leaves the collection ITEM alive (`/library/metadata/{id}` 200, still
        in `/library/all?type=18`) AND removes the tag from the item (the `/collection`
        axis carries only the surviving tag). So the state is perfectly representable,
        and plexsim was deleting a row a real PMS keeps -- which a client sees as a
        collection disappearing out of its Collections pivot when an item is edited.
        """
        collection = self.find_collection(title, row.get("librarySectionID"))
        if not collection:
            return
        collection["items"] = [k for k in collection["items"]
                               if int(k) != int(row["ratingKey"])]

    # -- match / unmatch / refresh / split / merge ------------------------

    # What `PUT .../unmatch` actually drops, measured field for field by diffing
    # a reference row's keys either side of the call. Far less than "everything
    # the agent supplied": the artwork, the external `Guid` array, the ratings,
    # the air date and the slug all SURVIVE, and only the agent-sourced
    # descriptive fields and people go.
    UNMATCH_DROPS = ("Genre", "Country", "Role", "Director", "Writer", "Producer",
                     "studio", "contentRating", "contentRatingAge", "tagline",
                     "audienceRating", "rating")

    def unmatch(self, row, identify=None):
        """Strip the agent's identity, the way `PUT .../unmatch` does.

        Measured on the reference by diffing the row's keys across the call:

          guid      becomes `tv.plex.agents.none://<ratingKey>`, NOT `local://`
          title     becomes the FILE NAME without its extension
          summary   is emptied but kept
          thumb/art/Image/Guid/Rating/year/originallyAvailableAt/slug  all KEPT

        The artwork surviving is the one that matters: dropping `thumb` leaves
        the row with no artwork URL for the client to ask for at all, so the grid
        tile goes blank -- where a real unmatched item still shows its poster.
        Either way the guid is what flips the menu from "Fix Match..." to
        "Match...", because both predicates test it for `local://` or `none://`.
        """
        if row.get("type") in ("episode", "season"):
            # Re-measured on a writable copy of the reference for an EPISODE and a
            # SEASON: the guid becomes `local://<ratingKey>`, the title the generated
            # `Episode N` / `Season N`, and the external Guid array is kept. The
            # filename title and the `none://` guid below were read off a movie.
            row["guid"] = "local://%s" % row["ratingKey"]
            row["_generatedTitle"] = True
            row["title"] = "%s %s" % ("Season" if row["type"] == "season" else "Episode",
                                      row.get("index"))
            if row["type"] == "season":
                row["titleSort"] = row["title"]
            else:
                row.pop("titleSort", None)
            self._follow_parent(row, {"title"})
            row["updatedAt"] = _now()
            if row["type"] == "episode":
                # Re-measured on the copy against a DIRECT episode unmatch, with the
                # season left matched: the same field set goes as in a season's
                # cascade -- the four people lists, `audienceRating`, `year`, and
                # `summary` emptied -- so these belong to "unmatch an episode" rather
                # than to the cascade. plexsim changed the guid and the title and kept
                # the rest, which left an unmatched episode carrying the agent's
                # summary, rating and cast under a `local://` guid.
                for field in self.EPISODE_UNMATCH_DROPS:
                    row.pop(field, None)
                row["summary"] = ""
            if row["type"] == "season":
                self._unmatch_season_children(row, identify)
            return row
        for key in self.UNMATCH_DROPS:
            row.pop(key, None)
        row["summary"] = ""
        row["guid"] = "tv.plex.agents.none://%s" % row["ratingKey"]
        if row.get("type") == "show":
            # Re-measured on the copy by snapshotting the show, both seasons and all
            # six episodes either side of one call: a SHOW unmatch cascades the whole
            # way down. Every season becomes `local://` and every episode with it, each
            # season's `parentGuid` and each episode's `grandparentGuid` follow the
            # show's new `none://` guid, and the artwork the children borrowed from the
            # show goes with it -- as does the `contentRating` they inherited. plexsim
            # edited the show row alone, which left matched seasons and episodes under
            # an unmatched show: a state a real PMS cannot produce, and one where the
            # client's own Match / Fix Match predicate disagrees from row to row.
            for season in list(self.catalog.children(row["ratingKey"])):
                if season.get("type") != "season":
                    continue
                self.unmatch(season, identify)
                season["parentGuid"] = row["guid"]
                season.pop("parentThumb", None)
                season.pop("parentStudio", None)
            for leaf in list(self.catalog.leaves(row["ratingKey"])):
                leaf["grandparentGuid"] = row["guid"]
                leaf.pop("grandparentThumb", None)
                leaf.pop("grandparentArt", None)
                leaf.pop("contentRating", None)
            row.pop("art", None)

        # The FILE NAME is the only title left that an agent did not supply, so
        # that is what an unmatched row is called -- the same thing a real PMS
        # falls back to. Keeping the agent's title would leave the row claiming
        # an identity it no longer has.
        for media in row.get("Media") or []:
            for part in media.get("Part") or []:
                name = str(part.get("file") or "").rsplit("/", 1)[-1]
                if name:
                    row["title"] = name.rsplit(".", 1)[0]
                    break
        row["updatedAt"] = _now()
        # Re-measured on the copy: a movie unmatch runs the full refresh sequence and
        # its activity ENDS -- `/activities` is empty straight afterwards. plexsim
        # opened an `Unmatching` activity at progress 0 and never closed it, so the
        # Dashboard spinner turned for the life of the process. The caller pushes the
        # refresh frames; nothing is left open here.
        return row

    # Measured on a writable copy of the reference by diffing the show, the season
    # and all three episodes either side of one `PUT .../unmatch` on the SEASON: the
    # call CASCADES. Every episode under the season is unmatched as well, and each
    # one's `parentGuid` follows the season to its new `local://` guid -- so an
    # unmatched season never has matched episodes under it. The season also loses
    # its own `year` (the show's then shows through as `parentYear`) and its `thumb`
    # falls back to the SHOW's, because the season no longer has an agent-supplied
    # poster of its own. kids shows the same end state from the other side: its
    # unmatched season 11 carries the show's thumb, no `year` and `parentYear`.
    #
    # What an episode unmatch drops, by either path. Re-measured on the copy against a
    # DIRECT unmatch with the season left matched: the field set is the same one the
    # season cascade produces, so it belongs to the episode write itself. `parentGuid`
    # and `parentThumb` are NOT in it -- those move only because the season moved.
    EPISODE_UNMATCH_DROPS = ("Director", "Writer", "Producer", "Role",
                             "audienceRating", "year")

    def _unmatch_season_children(self, season, identify=None):
        parent = self.catalog.items.get(int(season["parentRatingKey"])) \
            if str(season.get("parentRatingKey") or "").isdigit() else None
        season.pop("year", None)
        if parent is not None:
            if parent.get("year") is not None:
                season["parentYear"] = parent["year"]
            if parent.get("thumb"):
                season["thumb"] = parent["thumb"]
        for leaf in list(self.catalog.items.values()):
            if leaf.get("type") != "episode" \
                    or str(leaf.get("parentRatingKey")) != str(season["ratingKey"]):
                continue
            self.unmatch(leaf, identify)
            leaf["parentGuid"] = season["guid"]
            leaf.pop("parentThumb", None)

    def matches(self, row, query=None, year=None, identify=None):
        """`GET /library/metadata/{id}/matches` -- candidates for Fix Match.

        `identify` is metadata.py's TMDB search. With no network the honest
        answer is an empty SearchResult list, not an invented one.
        """
        if identify is None:
            return []
        return identify(query or row.get("title") or "", year)

    def apply_match(self, row, guid, name=None, year=None, identify=None):
        """`PUT /library/metadata/{id}/match?guid=&name=&year=`.

        Order matters. The re-identification runs FIRST and the chosen guid is
        written last, because metadata.enrich mints its own guid from whichever
        external id it finds (`imdb://` in preference to `tmdb:`) -- so letting
        it run afterwards silently replaces the identity the user picked with a
        different string, and the item does not come back as the thing they
        chose.
        """
        if identify is not None:
            identify(row, guid)
            # The agent supplies tag names and no ids; without this the row
            # keeps its detail page and disappears from every secondary filter.
            self.catalog.number_tags(row)
        if guid:
            # Measured: an EMPTY guid with a name is a 200 that renames the row and
            # refreshes it, leaving the identity alone -- not a write of an empty guid.
            row["guid"] = guid
        if name:
            row["title"] = name
        if year:
            try:
                row["year"] = int(year)
            except (TypeError, ValueError):
                pass
        # Cycle 59: a rename here reaches the children as an edit's does -- a real PMS
        # reads a child's `parentTitle`/`grandparentTitle` off the parent row -- where
        # plexsim renamed the show and left every season and episode on the old name.
        self._follow_parent(row, {"title", "studio"})
        row["updatedAt"] = _now()
        # No activity here: the server runs the measured refresh sequence around this call,
        # which starts one and ENDS it. The activity added here was never finished, so
        # /activities grew a stale `Matching` row per match for the life of the process.
        return row

    def refresh(self, row):
        """`PUT /library/metadata/{id}/refresh`.

        Cycle 21, measured on plexscratch four times on a movie and four on a show:
        a refresh does NOT move the row's `updatedAt` -- and the reference really ran
        it, because it pushed the seven-frame sequence each time. Cycle 20 established
        that an artwork URL's version IS the row's `updatedAt`, so the two move
        together and neither does: plexsim stamped both, and every Refresh Metadata
        therefore told every client that an unchanged row's poster and background had
        changed. (plexscratch is isolated from plex.tv, so what is measured is that
        running the refresh sequence does not by itself stamp the row.)
        """
        self.catalog.number_tags(row)
        # The activity belongs to the notification SEQUENCE, not to this function:
        # the server runs it start-to-end around the refresh so it also ends. Adding
        # one here as well pushed two `started` frames for one refresh.
        return row

    def split(self, row):
        """`PUT /library/metadata/{id}/split` -- one row per Media again.

        Only offered by the client when the item has more than one Media, which
        for plexsim only happens after a merge. Splitting returns the extra
        Media to rows of their own.
        """
        media = row.get("Media") or []
        if len(media) < 2:
            return []
        kept, extra = media[:1], media[1:]
        row["Media"] = kept
        created = []
        for source in extra:
            clone = dict(row)
            clone["Media"] = [source]
            clone.pop("Field", None)
            # A real PMS sends ratingKey as a STRING at every level; an int
            # here would be the one row in the library that differs.
            new_key = str(self.catalog.next_key(row.get("type")))
            clone["ratingKey"] = new_key
            clone["key"] = "/library/metadata/%s" % new_key
            clone["guid"] = "local://%s" % new_key
            self.catalog.add(clone)
            created.append(clone)
        row["updatedAt"] = _now()
        return created

    def merge(self, row, other_keys):
        """`PUT /library/metadata/{id}/merge?ids=a,b` -- one row, every Media.

        Measured warning, the hard way: on a real PMS this is not reversible.
        The split that undoes it leaves the second item on a NEW ratingKey
        carrying the first one's metadata, so a merge on a library you care
        about costs a re-match. plexsim's is in memory, so a restart undoes it.
        """
        # Cycle 14, measured on the copy and re-measured by the driver: a merge whose
        # target has no `Media` of its OWN absorbs nothing. `PUT /library/metadata/7/
        # merge?ids=11` on two seasons is a 200 with an empty body, NO frames, and a
        # tree that is completely unchanged -- both seasons still there, all six
        # episodes still there, the show's `leafCount` still 6 -- and the `split` that
        # would undo it is a no-op too. An EPISODE merge beside it is real on the same
        # server: 9 goes into 8, 8 carries two Media, 9 is a 404, and the season's and
        # show's leaf counts drop 3->2 and 6->5.
        #
        # This was plexsim's one DATA-LOSS bug. `catalog.remove` cascades to children,
        # so merging one season into another deleted the absorbed season AND every
        # episode under it, leaving the show's `leafCount` stale and the episodes gone
        # from every listing. The split does not bring them back.
        #
        # The witness is a SEASON. Cycle 41 subsequently measured SHOW merge on both
        # disposable PMS versions; that is a different operation implemented by
        # `merge_shows` below. A season still takes this no-op branch.
        if not row.get("Media"):
            return []
        merged = []
        for key in other_keys:
            other = self.catalog.items.get(int(key)) if str(key).isdigit() else None
            if other is None or int(key) == int(row["ratingKey"]):
                continue
            row.setdefault("Media", []).extend(other.get("Media") or [])
            self.catalog.remove(int(key))
            merged.append(other)
        if merged:
            row["updatedAt"] = _now()
        return merged

    def merge_shows(self, row, other_keys):
        """Merge identical show hierarchies, combining each episode's Media.

        Cycle 41 measured the one-season/one-episode case on both supported PMS
        versions. The absorbed episode's Media precedes the survivor's, the show
        keeps its own Location before the absorbed Location, and the absorbed
        show/season/episode all disappear. A shape whose leaves cannot be paired
        by season and episode index is deliberately left alone: that hierarchy
        was not measured, and inventing how Plex reconciles it would violate the
        server's measurement rule.

        Returns ``(removed_rows, changed_episodes)``. Removed rows are in the
        exact parent-before-child order used by the reference's delete frames.
        """
        if row.get("type") != "show":
            return [], []

        changed = []
        removed_rows = []
        target_leaves = {
            (leaf.get("parentIndex"), leaf.get("index")): leaf
            for leaf in self.catalog.leaves(row["ratingKey"])
        }
        for key in other_keys:
            other = self.catalog.items.get(int(key)) if str(key).isdigit() else None
            if other is None or other is row or other.get("type") != "show":
                continue
            source_seasons = self.catalog.children(other["ratingKey"])
            source_leaves = self.catalog.leaves(other["ratingKey"])
            if not source_leaves:
                continue
            pairs = []
            for source in source_leaves:
                target = target_leaves.get((source.get("parentIndex"), source.get("index")))
                if target is None:
                    pairs = []
                    break
                pairs.append((target, source))
            if len(pairs) != len(source_leaves):
                continue

            for target, source in pairs:
                target["Media"] = list(source.get("Media") or []) + list(target.get("Media") or [])
                target["updatedAt"] = _now()
                if target not in changed:
                    changed.append(target)

            locations = row.setdefault("Location", [])
            seen_locations = {(entry.get("id"), entry.get("path"))
                              for entry in locations if isinstance(entry, dict)}
            for location in other.get("Location") or []:
                marker = (location.get("id"), location.get("path")) \
                    if isinstance(location, dict) else location
                if marker not in seen_locations:
                    locations.append(location)
                    seen_locations.add(marker)

            source_rows = [other]
            for season in source_seasons:
                source_rows.append(season)
                source_rows.extend(self.catalog.children(season["ratingKey"]))
            removed = self.catalog.remove(int(other["ratingKey"]))
            removed_rows.extend(source_rows)
            for gone in removed:
                self._forget_item(gone)
            for playlist in self._playlists.values():
                playlist["items"] = [item for item in playlist["items"]
                                     if int(item["ratingKey"]) not in removed]
            for collection in self._collections.values():
                collection["items"] = [item_key for item_key in collection["items"]
                                       if int(item_key) not in removed]

        if removed_rows:
            row["updatedAt"] = _now()
            self.recount(row)
        return removed_rows, changed

    def delete(self, rating_key):
        """`DELETE /library/metadata/{id}` -- drop the row and its descendants."""
        row = self.catalog.items.get(rating_key)
        if row is None:
            return None
        parent_key = row.get("parentRatingKey") or row.get("grandparentRatingKey")
        removed = self.catalog.remove(rating_key)
        # `catalog.remove` returns the SET of keys it took out.
        for gone in (removed or {rating_key}):
            self._forget_item(gone)
        # Measured through the reference's scanner: removing a season's LAST episode
        # removes the season too, and the show's childCount drops with it. plexsim
        # left the empty season listed, so the show page drew a season tile with
        # nothing behind it.
        if row.get("type") == "episode" and parent_key:
            season = self.catalog.items.get(int(parent_key))
            if season is not None and not self.catalog.children(parent_key):
                self.catalog.remove(int(parent_key))
                parent_key = season.get("parentRatingKey") or parent_key
        for playlist in self._playlists.values():
            playlist["items"] = [i for i in playlist["items"]
                                 if int(i["ratingKey"]) not in removed]
        for collection in self._collections.values():
            collection["items"] = [k for k in collection["items"] if int(k) not in removed]
        if parent_key:
            parent = self.catalog.items.get(int(parent_key))
            if parent is not None:
                self.recount(parent)
        return row

    def delete_media(self, row, media_id):
        """`DELETE /library/metadata/{id}/media/{mediaID}` -- one version."""
        media = row.get("Media") or []
        kept = [m for m in media if str(m.get("id")) != str(media_id)]
        if len(kept) == len(media):
            return False
        row["Media"] = kept
        row["updatedAt"] = _now()
        return True

    # -- playlists -------------------------------------------------------

    def playlists(self, playlist_type=None):
        rows = []
        for playlist in self._playlists.values():
            rows.append(self._playlist_row(playlist))
        if playlist_type:
            rows = [r for r in rows if r.get("playlistType") == playlist_type]
        # Cycle 16, measured on plexscratch over five lists whose ratingKey, addedAt
        # and title orders are all different: the SMART ones come first, then
        # `titleSort` within each group, and every tie under an explicit sort falls
        # back to the same. plexsim ordered by titleSort alone.
        return sorted(rows, key=lambda r: (not r.get("smart"),
                                           str(r.get("titleSort") or r.get("title") or "")))

    def create_playlist(self, title, playlist_type, rows, smart=False, uri=None):
        # the item-id sequence is the SERVER's, not the playlist's -- measured cycle 16
        if not hasattr(self, "_next_playlist_item"):
            self._next_playlist_item = 0
        self._next_playlist += 1
        key = self._next_playlist
        stamp = _now()
        playlist = {
            "ratingKey": key,
            # Cycle 16, measured on plexscratch: an EMPTY title is stored
            # verbatim and reads back as `""`; a single space is kept too.
            # plexsim substituted "Untitled", inventing a name the user never
            # typed. (A MISSING title is a 400 on both, refused before this.)
            "title": title,
            "playlistType": playlist_type or "video",
            "smart": bool(smart),
            "summary": "",
            "addedAt": stamp,
            "updatedAt": stamp,
            "items": [],
            # Cycle 16, measured on plexscratch: a playlist is born `viewCount: 1`
            # with `lastViewedAt` equal to its `addedAt`. Cycle 21 moved the pair onto
            # the STORED row, because a scrobble counts up from it (1 -> 2 -> 3) and
            # an unscrobble clears it.
            "viewCount": 1,
            "lastViewedAt": stamp,
            # The query a smart playlist re-evaluates, kept verbatim so the
            # engine sees exactly what the client sent.
            "uri": uri if smart else None,
        }
        # Measured: a list created as `The Fresh List` already sorts as `Fresh List`.
        self._derive_title_sort(playlist)
        self._playlists[key] = playlist
        if not playlist["smart"]:
            self.add_playlist_items(playlist, rows)
        return playlist

    @staticmethod
    def playlist_content(uri):
        """The `content` a smart PLAYLIST reports, which is not the URI it was given.

        Measured: a playlist REWRITES
        `server://<machine>/com.plexapp.plugins.library/library/sections/1/all?type=1&genre=6`
        into
        `library://x/directory/%2Flibrary%2Fsections%2F1%2Fall%3Ftype%3D1%26genre%3D6`
        -- the provider prefix becomes `library://x/directory/` and the rest is
        URI-encoded whole. A smart COLLECTION keeps the `server://` URI exactly as
        it arrived. Two endpoints, two encodings of the same query.
        """
        path = urllib.parse.unquote(uri or "")
        marker = "/com.plexapp.plugins.library"
        if marker in path:
            path = path.split(marker, 1)[1]
        # Cycle 31, measured on plexscratch42: the middle segment is `item` when the uri
        # names ONE metadata row and `directory` otherwise -- a comma-joined key list is a
        # `directory` (`library://x/directory/%2Flibrary%2Fmetadata%2F1%2C2`) and a single
        # key is an `item` (`library://x/item/%2Flibrary%2Fmetadata%2F4`). plexsim wrote
        # `directory` for both, so the Save of a single-item smart query came back with a
        # content string the client cannot reopen as a rule.
        kind = ("item" if (path.startswith("/library/metadata/")
                           and "," not in path and "?" not in path)
                else "directory")
        return "library://x/%s/%s" % (kind, urllib.parse.quote(path, safe=""))

    def add_playlist_items(self, playlist, rows):
        """`PUT /playlists/{id}/items?uri=` -- returns how many were added.

        The real response carries `leafCountAdded` and `leafCountRequested`, so
        the client can tell the user "3 of 5 added" -- and measured, a row already in
        the playlist is NOT added again: re-adding it answers `leafCountAdded: 0` and
        leaves the count alone, and a uri naming the same key twice adds it once.
        plexsim appended every time, so Add to Playlist on something already there
        silently duplicated it.
        """
        added = 0
        present = {int(entry["ratingKey"]) for entry in playlist["items"]}
        for row in rows:
            key = int(row["ratingKey"])
            if key in present:
                continue
            present.add(key)
            # Cycle 16, measured on plexscratch: `playlistItemID` is a SERVER-WIDE
            # sequence -- three lists built in a row took 40/41, 42/43, 44/45, unbroken
            # across them. plexsim restarted at 1 inside each playlist, so two lists
            # handed the client the same ids for different rows and a cached id from
            # one list addressed a row in another.
            self._next_playlist_item += 1
            playlist["items"].append({
                "ratingKey": key,
                "playlistItemID": self._next_playlist_item,
            })
            added += 1
        if added:
            playlist["updatedAt"] = _now()
        return added

    def move_playlist_item(self, playlist, item_id, after=None):
        """`PUT /playlists/{id}/items/{itemID}/move[?after=]`.

        Measured on a real PMS: with no `after` the item goes to the FRONT, and
        with one it goes directly after that item. Drag-to-reorder is the only
        way a playlist's order can be changed, so without this the handle moves
        and the order snaps back on the next read.
        """
        items = playlist["items"]
        moving = next((i for i in items if str(i["playlistItemID"]) == str(item_id)), None)
        if moving is None:
            return False
        if after is not None and str(after) == str(item_id):
            # Measured: moving an item after ITSELF leaves the order alone.
            return True
        items.remove(moving)
        index = None if after is None else next(
            (n for n, i in enumerate(items) if str(i["playlistItemID"]) == str(after)), None)
        # Measured on a writable copy of the reference: an `after` the playlist does not
        # hold -- an unknown id, `0`, `abc` -- sends the item to the FRONT, as no `after`
        # does. plexsim appended it at the end.
        items.insert(0 if index is None else index + 1, moving)
        playlist["updatedAt"] = _now()
        return True

    def move_collection_item(self, collection, rating_key, after=None):
        """`PUT /library/collections/{id}/items/{ratingKey}/move[?after=]`.

        Keyed by the member's own ratingKey here, not by a playlistItemID --
        the two endpoints look alike and their ids are not the same thing.
        """
        items = collection["items"]
        try:
            moving = int(next(k for k in items if int(k) == int(rating_key)))
        except (StopIteration, TypeError, ValueError):
            return False
        # Measured on a writable copy of the reference: a move starts from the order the
        # collection is SHOWN in -- by release date unless a custom order is set -- and
        # switches the collection to its custom order, `collectionSort` 2.
        shown = [int(r["ratingKey"]) for r in self.collection_children(collection)]
        items[:] = shown + [int(k) for k in items if int(k) not in shown]
        items.remove(moving)
        if after is None:
            items.insert(0, moving)
        else:
            index = next((n for n, k in enumerate(items) if str(k) == str(after)), None)
            # Cycle 17, measured on plexscratch: an `after=` that names NO member --
            # `999999`, `0` -- moves the row to the FRONT, exactly as no `after=` does,
            # rather than to the end. A stale id, from a member another client has just
            # removed, is the case that reaches this.
            items.insert(0 if index is None else index + 1, moving)
        collection["collectionSort"] = "2"
        collection["updatedAt"] = _now()
        return True

    def remove_playlist_item(self, playlist, item_id):
        before = len(playlist["items"])
        playlist["items"] = [i for i in playlist["items"]
                             if str(i["playlistItemID"]) != str(item_id)]
        if len(playlist["items"]) != before:
            playlist["updatedAt"] = _now()
            return True
        return False

    def clear_playlist(self, playlist):
        """`DELETE /playlists/{id}/items` -- every item goes."""
        playlist["items"] = []
        playlist["updatedAt"] = _now()

    def playlist(self, key):
        return self._playlists.get(int(key)) if str(key).isdigit() else None

    def edit_playlist(self, playlist, title=None, summary=None):
        # Measured: `title=` stores the EMPTY string rather than being ignored, so
        # the test is `is not None` and not truthiness -- and an emptied list keeps its
        # old title's sort form, where a new title re-derives it.
        if title is not None:
            old_title = playlist["title"]
            playlist["title"] = title
            if title:
                self._derive_title_sort(playlist)
            else:
                self._empty_title(playlist, old_title)
        if summary is not None:
            playlist["summary"] = summary
        playlist["updatedAt"] = _now()

    def delete_playlist(self, key):
        return self._playlists.pop(int(key), None) is not None

    def playlist_items(self, playlist):
        """The playlist's rows, each carrying its playlistItemID.

        A smart playlist has no stored items: it is its query, re-run now. The
        item ids are positional, which is what a real PMS's are too -- there is
        nothing to reorder in a list the server rebuilds.
        """
        if playlist.get("smart"):
            # Cycle 16, measured on plexscratch: a SMART list's items carry NO
            # `playlistItemID` at all -- its members are its query, so there is nothing
            # to address. plexsim stamped 1, 2, 3 on them, which offers the client an
            # item id for a row that cannot be moved or removed.
            return [(row, None) for row in self.smart_rows(playlist.get("uri"))]
        out = []
        for entry in playlist["items"]:
            row = self.catalog.items.get(entry["ratingKey"])
            if row is None:
                continue
            out.append((row, entry["playlistItemID"]))
        return out

    def smart_rows(self, uri):
        """The rows a stored smart query resolves to right now."""
        if not uri or self._resolve is None:
            return []
        return self._resolve(uri)

    def global_views(self, row):
        """How many completed views of this row the server has ever recorded."""
        return self._views.get(str(row.get("ratingKey")), 0)

    def _playlist_row(self, playlist, content=False, state=True):
        """Field set measured on a real PMS's POST /playlists response.

        `content` is measured to be conditional: the LIST at /playlists omits it
        and the single read at /playlists/{id} carries it. A client that reads the
        list alone therefore cannot tell what a smart playlist selects, which is
        why the Edit Filters dialog opens the item rather than the list.
        """
        if playlist.get("smart"):
            rows = self.smart_rows(playlist.get("uri"))
        else:
            rows = [self.catalog.items.get(i["ratingKey"]) for i in playlist["items"]]
        rows = [r for r in rows if r]
        # Cycle 55, from `sum(round(duration/1000, 0))` over each item's longest Media
        # (SQLite integer division) and measured on plexscratch42: 1800689 + 1800689 +
        # 20032 ms answers 3620000. plexsim summed the raw milliseconds.
        duration = 1000 * sum(
            max([int(m.get("duration") or 0) for m in r.get("Media") or []]
                or [int(r.get("duration") or 0)]) // 1000 for r in rows)
        stamp = playlist["updatedAt"]
        row = {
            "ratingKey": str(playlist["ratingKey"]),
            "key": "/playlists/%s/items" % playlist["ratingKey"],
            # Cycle 26: an unmatched playlist carries `local://{rk}` and loses its
            # `duration`. See the collection row below.
            "guid": ("local://%s" % playlist["ratingKey"] if playlist.get("_unmatched")
                     else "com.plexapp.agents.none://plexsim-playlist-%s"
                     % playlist["ratingKey"]),
            "type": "playlist",
            "title": playlist["title"],
            "summary": playlist["summary"],
            "smart": playlist["smart"],
            "playlistType": playlist["playlistType"],
            "leafCount": len(rows),
            "addedAt": playlist["addedAt"],
            "updatedAt": stamp,
        }
        if playlist.get("titleSort"):
            row["titleSort"] = playlist["titleSort"]
        # Cycle 16, measured on plexscratch: `PUT /playlists/{id}` takes the whole ITEM
        # edit grammar, so the row has to carry back what it stored -- `year`, `studio`,
        # `originallyAvailableAt`, the locks, and artwork minted under
        # `/library/metadata/{ratingKey}/`, which also shows up as an `Image` row.
        for stored in ("year", "studio", "originallyAvailableAt", "thumb", "art",
                       "Field"):
            if playlist.get(stored) not in (None, "", []):
                row[stored] = playlist[stored]
        images = [{"alt": playlist["title"], "type": kind, "url": playlist[field]}
                  for field, kind in (("thumb", "coverPoster"), ("art", "background"))
                  if playlist.get(field)]
        if images:
            row["Image"] = images
        if rows:
            # Measured: an EMPTY playlist -- never filled, or emptied -- carries
            # neither a composite nor a duration. plexsim kept both, with the
            # duration at 0 and a composite of nothing.
            row["composite"] = "/playlists/%s/composite/%d" % (playlist["ratingKey"], stamp)
            if not playlist.get("_unmatched"):
                # Cycle 26: an unmatch removes it, where the composite survives.
                row["duration"] = duration
        if playlist.get("smart"):
            # Measured, and the only thing that marks a smart playlist in the
            # list: the client draws its own icon from this value.
            row["icon"] = "playlist://image.smart"
            if content and playlist.get("uri"):
                row["content"] = self.playlist_content(playlist["uri"])
        if state:
            # Cycle 21, measured on plexscratch: the per-user-state block is on
            # `/playlists` AND on `/library/metadata/{id}`, and omitted by
            # `/playlists/{id}` and by the create response. It is a SEPARATE switch
            # from `content`, which `/library/metadata/{id}` also carries. Cycle 16
            # saw only the listing and called the pair the listing row's own.
            row = self._with_list_state(
                row, playlist, "composite" if "composite" in row else "playlistType")
        return row

    # -- collections -----------------------------------------------------

    def collections(self, section_id=None):
        rows = [self._collection_row(c) for c in self._collections.values()
                if section_id is None or str(c["librarySectionID"]) == str(section_id)]
        return sorted(rows, key=lambda r: str(r.get("titleSort") or r.get("title") or ""))

    def create_collection(self, title, section_id, rows, subtype="movie", smart=False,
                          uri=None):
        self._next_collection += 1
        key = self._next_collection
        stamp = _now()
        collection = {
            "ratingKey": key,
            "title": title or "Untitled",
            "librarySectionID": section_id,
            "subtype": subtype,
            "smart": bool(smart),
            "summary": "",
            "addedAt": stamp,
            "updatedAt": stamp,
            "items": [],
            # A smart collection keeps the `server://` URI VERBATIM, where a
            # smart playlist rewrites it -- see playlist_content().
            "uri": uri if smart else None,
        }
        self._derive_title_sort(collection)
        # Cycle 61, measured on both copies: the members' tag of a NEW collection titled
        # `c61c lowcreate` is `C61c lowcreate` -- the title stays as typed -- and an
        # existing tag of that name keeps its own spelling.
        known = self._tags.get(("Collection", collection["title"].lower()))
        collection["tag"] = known["tag"] if known else self._capitalised(collection["title"])
        collection["_names"] = {collection["tag"].lower()}
        self._collections[key] = collection
        if not collection["smart"]:
            # A smart collection has no members to tag: its membership is its
            # query, re-run on every read.
            self.add_collection_items(collection, rows)
        return collection

    def add_collection_items(self, collection, rows):
        added = 0
        for row in rows:
            if int(row["ratingKey"]) in [int(k) for k in collection["items"]]:
                continue
            collection["items"].append(int(row["ratingKey"]))
            # A collection is a tag on its members, and the item detail page
            # reads it from the row rather than from /library/collections.
            self._add_tag(row, "collection", collection.get("tag", collection["title"]))
            # Cycle 60, measured on plexscratch42 and plexscratch: joining a collection --
            # its create or an items add -- LOCKS the member's `collection` field, and
            # neither removing the member nor deleting the collection unlocks it. A
            # `collection[0].tag.tag` edit on the item locks nothing (the dialog sends its
            # own `collection.locked`).
            self._set_lock(row, "collection", True)
            added += 1
        if added:
            collection["updatedAt"] = _now()
        return added

    def collection(self, key):
        return self._collections.get(int(key)) if str(key).isdigit() else None

    def find_collection(self, title, section_id=None):
        for collection in self._collections.values():
            if str(collection.get("tag", collection["title"])).lower() == str(title).lower() and (
                    section_id is None
                    or str(collection["librarySectionID"]) == str(section_id)):
                return collection
        return None

    def _sync_collection_tag(self, row, title):
        """Editing a `collection[0].tag.tag` on an item makes a TAG, not a collection.

        Cycle 30, measured on BOTH writable references -- plexscratch (1.43.4) and the
        new plexscratch42 (1.42.2): the write is a 200, the tag appears on the row and in
        `/library/sections/{id}/collection`, it filters (`?collection=<id>` answers the
        member) and both search endpoints find it as a `tagType: 2` row -- and
        `/library/all?type=18` and `/library/sections/{id}/collections` list NOTHING new,
        twenty minutes later and after a refresh. The copy's axis holds eleven collection
        tags against four collection items, which is its steady state. Only
        `POST /library/collections` makes an item.
        //
        plexsim materialised a full, browsable collection from the tag write, so "Add to
        Collection" on an item created a row in the Collections tab that a real PMS does
        not have -- and the search row it answered carried a `guid`, which a tag-only
        collection's does not.
        """
        section = row.get("librarySectionID")
        collection = self.find_collection(title, section)
        if collection is None:
            return
        if int(row["ratingKey"]) not in [int(k) for k in collection["items"]]:
            collection["items"].append(int(row["ratingKey"]))
            collection["updatedAt"] = _now()

    def edit_collection(self, collection, title=None, summary=None):
        """Rename or re-summarise a collection.

        Measured on a writable copy of the reference: renaming a collection leaves its
        members' Collection TAG as it was. plexsim renamed the tag in place; now the
        collection keeps the tag it was created under (`tag`) and finds, removes and
        untags its members by that, so the rename changes the title alone.
        """
        if title == "" and collection["title"]:
            # Measured: an emptied collection title is stored as "" and keeps its old
            # title's sort form. plexsim ignored an empty title here.
            self._empty_title(collection, collection["title"])
            collection["title"] = ""
        if title and title != collection["title"]:
            collection.setdefault("tag", collection["title"])
            collection["title"] = title
            self._derive_title_sort(collection)
        if summary is not None:
            collection["summary"] = summary
        collection["updatedAt"] = _now()

    def rename_collection_tag(self, collection, title):
        """Move the members' `Collection` tag to a new name. Cycle 15.

        The section-path save does this and the item path does not, measured on the
        copy twice over. The tag keeps its ID -- a real PMS keeps a tag TABLE and the
        row is renamed in place, so the `/collection` axis key does not move.
        """
        if collection is None or not title:
            return
        old = collection.get("tag", collection["title"])
        if title == old:
            return
        # The tag keeps its ID across the rename -- measured on the copy, where tag 239
        # stayed 239 through it. plexsim derives a tag id by hashing the NAME, so the
        # id the members already carry has to be pinned onto the collection before the
        # name moves, or the row's `index` and the axis key would jump.
        collection.setdefault("tagID", self.catalog.tag_id("Collection", old))
        for member in collection["items"]:
            row = self.catalog.items.get(int(member))
            for tag in (row or {}).get("Collection") or []:
                if tag.get("tag") == old:
                    tag["tag"] = title
                    # Cycle 62, measured on plexscratch42 and plexscratch, and through
                    # python-plexapi's `section.all()`: a member's LIGHT row -- the grid,
                    # the section search, Recently Added -- keeps the name it had before the
                    # rename (the FIRST one, through a chain of renames) while its detail row
                    # and the axis move. Only a collection tag written on the item itself
                    # refreshes it, and the delete clears it.
                    light = row.setdefault("_collectionLight", {})
                    light[title.lower()] = light.pop(str(old).lower(), old)
        collection["tag"] = title
        # Cycle 61: the new name joins the tag TABLE and the old one stays in it.
        self._tags.setdefault(("Collection", title.lower()),
                              {"id": collection["tagID"], "tag": title,
                               "filter": "collection=%s" % collection["tagID"]})
        collection.setdefault("_names", set()).add(title.lower())

    def delete_collection(self, key):
        collection = self._collections.pop(int(key), None)
        if collection is None:
            return False
        for member in collection["items"]:
            row = self.catalog.items.get(int(member))
            if row is not None:
                self._remove_tags(row, "collection", [collection.get("tag", collection["title"])])
        return True

    def stamp_collection_tags(self, collection):
        """Put the collection's guid on every member's `Collection` tag. Cycle 15.

        Measured on the copy: the tag carries the COLLECTION's own guid and no
        `count` -- cycle 14 recorded both and only the guid half is real, which cycle
        12 had already shown for a detail row's tags in general.
        """
        name = collection.get("tag", collection["title"])
        guid = "collection://plexsim-%s" % collection["ratingKey"]
        # A SMART collection has no stored member list -- it tags nobody, measured.
        for member in collection.get("items") or []:
            row = self.catalog.items.get(int(member))
            for tag in (row or {}).get("Collection") or []:
                if tag.get("tag") == name:
                    tag["guid"] = guid

    def collection_children(self, collection):
        if collection.get("smart"):
            return self.smart_rows(collection.get("uri"))
        rows = [self.catalog.items.get(int(k)) for k in collection["items"]]
        return self._collection_order(collection, [r for r in rows if r])

    @staticmethod
    def _collection_order(collection, rows):
        """A collection's children in the order its `collectionSort` names.

        Measured on a writable copy of the reference: with no sort set, or `0`, the
        members come back by RELEASE DATE, oldest first, whatever order they were added
        in; `2` is the custom order a move writes. `1` gave the same order on members
        whose release order and titles agree, and is alphabetical by python-plexapi's
        mapping (0 release, 1 alpha, 2 custom). plexsim listed insertion order.
        """
        sort = str(collection.get("collectionSort") or "0")
        if sort == "2":
            return rows
        # Cycle 15, measured on the copy over twelve values: ALPHABETICAL for anything
        # that parses as a plain non-zero integer -- `3`, `99`, `-1`, `01` and `+1` as
        # well as `1` -- and RELEASE DATE for `0` and for anything that does not parse
        # (`1.5`, a leading space, `abc`, an empty value). So a leading `+` is allowed
        # and a decimal point and a leading space are not. plexsim honoured `1` alone
        # and fell back to release date for every other number, which a free-text pref
        # can hold easily enough.
        if sort.lstrip("+-").isdigit() and sort.lstrip("+-").lstrip("0"):
            return sorted(rows, key=lambda r: str(r.get("titleSort")
                                                  or r.get("title") or "").lower())
        if sort == "1":
            return sorted(rows, key=lambda r: str(r.get("titleSort") or r.get("title") or "").lower())
        return sorted(rows, key=lambda r: str(r.get("originallyAvailableAt") or ""))

    def _collection_row(self, collection):
        # Cycle 15: the members' tags carry this row's guid. Stamped on every read so
        # a member added by any path picks it up.
        self.stamp_collection_tags(collection)
        """Field set measured on a real PMS's POST /library/collections."""
        rows = self.collection_children(collection)
        years = sorted(str(r.get("year")) for r in rows if r.get("year"))
        stamp = collection["updatedAt"]
        row = {
            "ratingKey": str(collection["ratingKey"]),
            "key": "/library/collections/%s/children" % collection["ratingKey"],
            # Cycle 26, measured on the copy: an UNMATCHED collection carries
            # `local://{rk}`, its `subtype` becomes the literal `unknown` and its
            # `ratingCount` is removed entirely. The flag lives on the STORED row
            # because this projection is rebuilt on every read -- which is why the
            # unmatch handler's own write to the row it was handed never survived.
            "guid": ("local://%s" % collection["ratingKey"]
                     if collection.get("_unmatched")
                     else "collection://plexsim-%s" % collection["ratingKey"]),
            "type": "collection",
            "title": collection["title"],
            "subtype": ("unknown" if collection.get("_unmatched")
                        else collection["subtype"]),
            "summary": collection["summary"],
            # Cycle 14, measured on the copy: a collection row's `index` is its own
            # TAG id -- the same number its members carry in their `Collection` tag and
            # the same one the `/collection` axis keys on (235 and 236 for two
            # collections made in order). plexsim sent 1 for every collection, so the
            # number the client uses to tie a row to its tag was the same on all of
            # them. The tag id comes from the catalog's own allocator, so a rename
            # follows the tag the collection was created under.
            "index": (collection.get("tagID")
                      or self.catalog.tag_id("Collection",
                                             collection.get("tag", collection["title"]))),
            # Cycle 15, measured on the copy over eleven collections of four member
            # types and both smart values: this is NOT a count. It is the Plex TYPE
            # NUMBER of the members -- movie 1, show 2, season 3, episode 4 -- plus
            # 100 when the collection is smart (101/102/104). Cycle 14 saw the `1` on
            # a three-member collection and left it unexplained; the rule predicted
            # 102 and 104 before either was measured. plexsim sent the member count,
            # which `childCount` already carries.
            "ratingCount": (TYPE_NUMBERS.get(collection["subtype"], 1)
                            + (100 if collection.get("smart") else 0)),
            "thumb": "/library/collections/%s/composite/%d?width=400&height=600"
                     % (collection["ratingKey"], stamp),
            "addedAt": collection["addedAt"],
            "updatedAt": stamp,
            "childCount": len(rows),
            # Cycle 15: a NUMBER here, as on every other row. plexsim already sent
            # an int on a movie detail row, so the collection path disagreed with its
            # own library rows.
            "librarySectionID": int(collection["librarySectionID"])
            if str(collection["librarySectionID"]).isdigit()
            else collection["librarySectionID"],
        }
        if collection.get("_unmatched"):
            # Cycle 26: an unmatch REMOVES it. Built above and popped here rather than
            # made conditional in the literal, so the surviving keys keep the order a
            # real PMS writes them in -- its JSON key order is its XML order.
            row.pop("ratingCount", None)
        section = self.catalog.section(collection["librarySectionID"]) \
            if hasattr(self.catalog, "section") else None
        if section:
            # Measured on fun's collection rows: the section's title and key ride on
            # the row itself, as they do on every library row.
            row["librarySectionTitle"] = section.get("title")
            row["librarySectionKey"] = "/library/sections/%s" % collection["librarySectionID"]
        if collection.get("smart"):
            # Measured twice over: a collection's `smart` is the STRING "1", not
            # the playlist's boolean, and a NON-smart collection omits the field
            # entirely rather than sending a false. A playlist always sends it.
            row["smart"] = "1"
        if collection.get("smart") and collection.get("uri"):
            # Unlike a playlist, a collection carries `content` in the LIST as
            # well as the single read -- measured at both
            # /library/sections/{id}/collections and /library/collections/{id}.
            row["content"] = urllib.parse.unquote(collection["uri"])
        for name in ("titleSort", "contentRating", "collectionSort", "collectionMode", "Field", "Label"):
            # Whatever the Edit Collection dialog and its prefs wrote. Measured: the
            # sort and mode come back as STRING attributes on the row, which is what
            # the dialog reads to show the current selection.
            if collection.get(name) is not None:
                row[name] = collection[name]
        if collection.get("collectionFilterBasedOnUser"):
            # Cycle 59: an empty value is stored (the Preferences block reads it back
            # empty) and is not on the row.
            row["collectionFilterBasedOnUser"] = collection["collectionFilterBasedOnUser"]
        if years and not collection.get("smart"):
            # Measured: a SMART collection carries no minYear/maxYear -- its
            # membership is a query, so there is no fixed span to report. plexsim
            # computed them from whatever the query matched at that moment.
            row["minYear"] = years[0]
            row["maxYear"] = years[-1]
        # Cycle 26: with `ratingCount` gone the state block keeps the same slot --
        # measured with a rating and an offset on an unmatched collection, where it
        # still sits after `index` and before `thumb`.
        return self._with_list_state(
            row, collection, "ratingCount" if "ratingCount" in row else "index")

    # -- helpers ---------------------------------------------------------

    # Cycle 21, measured on a writable copy of the reference over a collection and a
    # playlist taken through every `/:/` write: the per-user state comes back in THIS
    # order and in the MIDDLE of the row -- after a collection's `ratingCount` and
    # before its `thumb`, after a playlist's `composite` and before its `duration`.
    # A collection never carries `viewCount` at all, whatever is scrobbled at it.
    _LIST_STATE = ("userRating", "viewOffset", "viewCount", "lastViewedAt", "lastRatedAt")

    def _with_list_state(self, row, stored, after):
        """Splice a list row's per-user state in after `after`, where it is measured."""
        state = {name: stored[name] for name in self._LIST_STATE
                 if stored.get(name) is not None}
        if not state or after not in row:
            return row
        spliced = {}
        for key, value in row.items():
            spliced[key] = value
            if key == after:
                spliced.update(state)
        return spliced

    def _leaves_or_self(self, row):
        if row.get("type") in ("show", "season"):
            return self.catalog.leaves(row["ratingKey"])
        return [row]
