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
import re
import time
import urllib.parse
import uuid
import zlib

from hierarchy import sort_title

# Fields a row carries as a number. The client sends every edit as a string.
INTEGER_FIELDS = {"year", "index", "parentIndex", "duration", "viewOffset",
                  "viewCount", "leafCount", "childCount"}

DATE_FIELDS = {"originallyAvailableAt"}

# Fields the Edit dialog may write. Measured: anything else is a 400, including
# `sortTitle` (the field is `titleSort`) -- plexsim stored whatever it was sent,
# so a client typo became a row attribute and a `Field` lock for a field that
# does not exist.
EDITABLE_FIELDS = {
    "title", "titleSort", "originalTitle", "studio", "tagline", "summary",
    "contentRating", "originallyAvailableAt", "year", "index", "editionTitle",
    "userRating", "audienceRating", "rating", "duration",
}

# Accepted, locked, and DISCARDED on the ITEM path: the star rating is written
# through /:/rate there. The section path stores it (see `_set_field`).
IGNORED_EDIT_FIELDS = {"userRating"}

# Accepted with a 200 and DISCARDED as values. Measured on a writable copy of the
# reference: `thumb.value=`, `art.value=`, `addedAt.value=`, `guid.value=`,
# `lastViewedAt.value=`, `viewCount.value=` and an episode's `parentIndex.value=` change
# nothing, where plexsim refused each as an unknown field. Artwork is written through
# the poster and art endpoints, play state through /:/scrobble.
IGNORED_VALUE_FIELDS = {"thumb", "art", "addedAt", "guid", "lastViewedAt", "viewCount",
                        "parentIndex"}

# A tag type accepted and discarded: a show's `network[0].tag.tag=` is a 200 that stores
# nothing visible, where plexsim 400'd it.
IGNORED_TAG_TYPES = {"network"}

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
LOCK_ONLY_FIELDS = {"thumb", "art", "theme", "banner", "guid", "slug", "addedAt",
                    "viewCount", "network", "parentIndex"}


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

    def scrobble(self, rating_key, watched, account_id=1, device_id=1):
        """Mark played/unplayed. A show or season marks every episode beneath it.

        The client's own menu predicate reads `unviewedLeafCount` for a show, so
        marking a show played without updating its leaves leaves the menu
        offering "Mark as Played" forever.
        """
        row = self.catalog.items.get(rating_key)
        if row is None:
            return []
        targets = self._leaves_or_self(row)
        for leaf in targets:
            if watched:
                leaf["viewCount"] = int(leaf.get("viewCount", 0)) + 1
                leaf["lastViewedAt"] = _now()
                leaf.pop("viewOffset", None)
                # Measured: a scrobble writes NO history row -- the reference's
                # history stayed at one row through three scrobbles, because rows
                # come only from a timeline that reaches the end. plexsim recorded
                # one per mark-as-played, so the History page and the Top shelf
                # counted things nobody had watched.
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
        self.recount(row)
        return targets

    def rate(self, rating_key, rating):
        """`/:/rate?key=&rating=` -- the star rating. -1 clears it."""
        row = self.catalog.items.get(rating_key)
        if row is None:
            return None
        if rating is None or rating == -1:
            row.pop("userRating", None)
            row.pop("lastRatedAt", None)
        elif rating < 0:
            # Measured on a writable copy of the reference: any OTHER negative rating
            # clears the stars and KEEPS `lastRatedAt`; only -1 clears both.
            row.pop("userRating", None)
        else:
            row["userRating"] = float(rating)
            row["lastRatedAt"] = _now()
        return row

    # What each playback session last reported for an item, keyed (session, ratingKey).
    # A completion is a CROSSING, so the previous position is the whole state it needs.
    _reported = {}

    def progress(self, rating_key, offset, state=None, duration=None, device_id=1,
                 session="", completes=True):
        """A timeline update. `stopped` past 90% counts as a view, as PMS does.

        `completes=False` is `/:/progress`, which never completes a play: measured on
        a writable copy of the reference, `time=999999999` on a ten-second item is
        stored as the resume point, with no view and no history row.

        `device_id` is the client that played: the history row names it, and a
        real PMS lists every such id in the Dashboard's Device block.
        """
        row = self.catalog.items.get(rating_key)
        if row is None:
            return None
        # The duration the BEAT carries, and nothing else: measured on a writable copy of
        # the reference with half-hour media, a beat with no `duration` stores nothing and
        # completes nothing, however far along it says it is -- 95% of the item's own length
        # included. plexsim fell back to the row's duration, so a player that omits it (and
        # `/:/progress` never sends one) drove the whole watched-state machine.
        total = int(duration) if duration else 0
        if offset is not None:
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
                # Measured: a progress beat stamps `lastViewedAt` on the SEASON and the SHOW
                # as well, before anything is watched -- plexsim touched the parents only on
                # a completion, so a show part-way through an episode sorted as never played.
                for name in ("parentRatingKey", "grandparentRatingKey"):
                    parent = self.catalog.items.get(int(row[name])) \
                        if str(row.get(name) or "").isdigit() else None
                    if parent is not None:
                        parent["lastViewedAt"] = row["lastViewedAt"]
            elif total and int(offset) <= 60000:
                # Measured: a beat at or under a minute DELETES the stored resume point,
                # rather than being ignored -- starting a title again from the beginning is
                # exactly this sequence, and plexsim kept the old position in Continue
                # Watching. `lastViewedAt` is left alone.
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
        seen = self._reported.get((session or "", int(rating_key)))
        if offset is not None and total:
            self._reported[(session or "", int(rating_key))] = position
        crossed = seen is not None and total and seen < total * 0.9
        if completes and crossed and total and position > 60000 and position >= total * 0.9:
            if row.get("_completed") != (session or True):
                row["_completed"] = session or True
                row.pop("_completionStopped", None)
                row["viewCount"] = int(row.get("viewCount", 0)) + 1
                row.pop("viewOffset", None)
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

    def record_view(self, row, account_id=1, device_id=1):
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
            for key in ("title", "originallyAvailableAt", "thumb"):
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
                 and k not in ("_pairs", "id", "type")]
        if section:
            # On the SECTION path a bare `field=value` is a FILTER that scopes the
            # edit, not a field to write -- measured: `?type=1&title=Sintel` edits
            # only Sintel. On the ITEM path the same shape IS an edit (a bare
            # `studio=` is applied). So which it means depends on the path.
            items = [(k, v) for k, v in items
                     if k.endswith((".value", ".locked")) or ".tag.tag" in k]
        if not items:
            # Measured: a PUT with no parameters at all is a 400.
            raise EditError(400, "bad request")

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
                        and field not in LOCK_ONLY_FIELDS:
                    raise EditError(400, "unknown field %r" % field)
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
                if field in IGNORED_VALUE_FIELDS \
                        or (field in ("duration", "index") and row.get("type") == "movie") \
                        or (field in TAG_KEYS and not name.endswith(".value")):
                    # Measured: each is a 200 that changes nothing -- the value-less
                    # columns above, a movie's `duration` and `index`, and a bare tag
                    # name (`genre=Drama`) on the item path, where plexsim stored the
                    # first two and 400'd the rest.
                    changed = True
                    continue
                if field not in EDITABLE_FIELDS:
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
        if "index" in written and row.get("_generatedTitle"):
            # Measured: a generated title follows the index -- `Episode 2` is
            # `Episode 7` after `index.value=7`.
            row["title"] = "%s %s" % ("Season" if row.get("type") == "season" else "Episode",
                                      row.get("index"))
            written = written | {"title"}
        if written & {"title", "studio", "index", "contentRating"}:
            self._follow_parent(row, written)
        if changed:
            row["updatedAt"] = _now()
        return changed

    def _set_field(self, row, field, value, section=False):
        """One scalar field, with the coercions a real PMS applies."""
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
            if number is None or (field == "userRating" and number < 0):
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
            if number < 0 or (field == "year" and number == 0):
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
            tag_id = zlib.crc32(("%s:%s" % (key, value)).encode()) % 900000 + 1000
            minted = {"id": tag_id, "tag": value,
                      "filter": "%s=%s" % (tag_type, tag_id)}
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
        title = row.get("title")
        if not title:
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
        tags.append({"id": tag_id, "tag": value,
                     "filter": "%s=%s" % (tag_type, tag_id)})
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
        """Drop this row from the named collection, and the collection if empty."""
        collection = self.find_collection(title, row.get("librarySectionID"))
        if not collection:
            return
        collection["items"] = [k for k in collection["items"]
                               if int(k) != int(row["ratingKey"])]
        if not collection["items"]:
            # A Plex collection exists only as the tag on its members: untag the
            # last one and the collection is gone, rather than lingering in the
            # Collections pivot as an empty row. (The reference DOES keep an empty
            # collection row -- measured, childCount 0 and still listed -- but it
            # also leaves the stale tag on the item, which is a state plexsim has
            # no way to represent consistently; a vanishing collection is the
            # lesser divergence.)
            self._collections.pop(collection["ratingKey"], None)

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
        row["updatedAt"] = _now()
        # No activity here: the server runs the measured refresh sequence around this call,
        # which starts one and ENDS it. The activity added here was never finished, so
        # /activities grew a stale `Matching` row per match for the life of the process.
        return row

    def refresh(self, row):
        """`PUT /library/metadata/{id}/refresh`.

        plexsim has nothing on disk to re-read, so a refresh re-versions the
        artwork keys and bumps updatedAt -- which is what makes the client drop
        its cached poster. Artwork URLs are versioned for exactly this reason:
        an unversioned /thumb/1 plus ratingKey reuse serves a stale poster.
        """
        stamp = _now()
        row["updatedAt"] = stamp
        self.catalog.number_tags(row)
        for key, kind in (("thumb", "thumb"), ("art", "art")):
            if row.get(key):
                row[key] = "/library/metadata/%s/%s/%d" % (row["ratingKey"], kind, stamp)
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
        rows = [self._playlist_row(p) for p in self._playlists.values()]
        if playlist_type:
            rows = [r for r in rows if r.get("playlistType") == playlist_type]
        return sorted(rows, key=lambda r: str(r.get("titleSort") or r.get("title") or ""))

    def create_playlist(self, title, playlist_type, rows, smart=False, uri=None):
        self._next_playlist += 1
        key = self._next_playlist
        stamp = _now()
        playlist = {
            "ratingKey": key,
            "title": title or "Untitled",
            "playlistType": playlist_type or "video",
            "smart": bool(smart),
            "summary": "",
            "addedAt": stamp,
            "updatedAt": stamp,
            "items": [],
            "next_item_id": 1,
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
        return "library://x/directory/" + urllib.parse.quote(path, safe="")

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
            playlist["items"].append({
                "ratingKey": key,
                "playlistItemID": playlist["next_item_id"],
            })
            playlist["next_item_id"] += 1
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
            items.insert(len(items) if index is None else index + 1, moving)
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
            return [(row, index) for index, row
                    in enumerate(self.smart_rows(playlist.get("uri")), 1)]
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

    def _playlist_row(self, playlist, content=False):
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
        duration = sum(int(r.get("duration") or 0) for r in rows)
        stamp = playlist["updatedAt"]
        row = {
            "ratingKey": str(playlist["ratingKey"]),
            "key": "/playlists/%s/items" % playlist["ratingKey"],
            "guid": "com.plexapp.agents.none://plexsim-playlist-%s" % playlist["ratingKey"],
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
        if rows:
            # Measured: an EMPTY playlist -- never filled, or emptied -- carries
            # neither a composite nor a duration. plexsim kept both, with the
            # duration at 0 and a composite of nothing.
            row["composite"] = "/playlists/%s/composite/%d" % (playlist["ratingKey"], stamp)
            row["duration"] = duration
        if playlist.get("smart"):
            # Measured, and the only thing that marks a smart playlist in the
            # list: the client draws its own icon from this value.
            row["icon"] = "playlist://image.smart"
            if content and playlist.get("uri"):
                row["content"] = self.playlist_content(playlist["uri"])
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
            added += 1
        if added:
            collection["updatedAt"] = _now()
        return added

    def collection(self, key):
        return self._collections.get(int(key)) if str(key).isdigit() else None

    def find_collection(self, title, section_id=None):
        for collection in self._collections.values():
            if collection.get("tag", collection["title"]) == title and (
                    section_id is None
                    or str(collection["librarySectionID"]) == str(section_id)):
                return collection
        return None

    def _sync_collection_tag(self, row, title):
        """Editing a `collection[0].tag.tag` on an item is how the UI's
        "Add to Collection" works, so the collection has to appear from that
        side too -- creating it if this is its first member."""
        section = row.get("librarySectionID")
        collection = self.find_collection(title, section)
        if collection is None:
            self._next_collection += 1
            key = self._next_collection
            stamp = _now()
            collection = {
                "ratingKey": key,
                "title": title,
                "librarySectionID": section,
                "subtype": row.get("type") or "movie",
                "smart": False,
                "summary": "",
                "addedAt": stamp,
                "updatedAt": stamp,
                "items": [],
            }
            self._collections[key] = collection
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

    def delete_collection(self, key):
        collection = self._collections.pop(int(key), None)
        if collection is None:
            return False
        for member in collection["items"]:
            row = self.catalog.items.get(int(member))
            if row is not None:
                self._remove_tags(row, "collection", [collection.get("tag", collection["title"])])
        return True

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
        if sort == "1":
            return sorted(rows, key=lambda r: str(r.get("titleSort") or r.get("title") or "").lower())
        return sorted(rows, key=lambda r: str(r.get("originallyAvailableAt") or ""))

    def _collection_row(self, collection):
        """Field set measured on a real PMS's POST /library/collections."""
        rows = self.collection_children(collection)
        years = sorted(str(r.get("year")) for r in rows if r.get("year"))
        stamp = collection["updatedAt"]
        row = {
            "ratingKey": str(collection["ratingKey"]),
            "key": "/library/collections/%s/children" % collection["ratingKey"],
            "guid": "collection://plexsim-%s" % collection["ratingKey"],
            "type": "collection",
            "title": collection["title"],
            "subtype": collection["subtype"],
            "summary": collection["summary"],
            "index": 1,
            "ratingCount": len(rows),
            "thumb": "/library/collections/%s/composite/%d?width=400&height=600"
                     % (collection["ratingKey"], stamp),
            "addedAt": collection["addedAt"],
            "updatedAt": stamp,
            "childCount": len(rows),
            "librarySectionID": collection["librarySectionID"],
        }
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
        if years and not collection.get("smart"):
            # Measured: a SMART collection carries no minYear/maxYear -- its
            # membership is a query, so there is no fixed span to report. plexsim
            # computed them from whatever the query matched at that moment.
            row["minYear"] = years[0]
            row["maxYear"] = years[-1]
        return row

    # -- helpers ---------------------------------------------------------

    def _leaves_or_self(self, row):
        if row.get("type") in ("show", "season"):
            return self.catalog.leaves(row["ratingKey"])
        return [row]
