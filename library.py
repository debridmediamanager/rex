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

import time
import uuid
import zlib

# Fields a row carries as a number. The client sends every edit as a string.
INTEGER_FIELDS = {"year", "index", "parentIndex", "duration", "viewOffset",
                  "viewCount", "leafCount", "childCount"}

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

    def __init__(self, catalog, notify=None):
        self.catalog = catalog
        # Called with (event, activity) when an activity starts or ends, so the
        # server can push it down the notification socket. A client watches that
        # channel for the Activity indicator rather than polling /activities.
        self._notify = notify
        self._playlists = {}
        self._collections = {}
        self._next_playlist = PLAYLIST_BASE
        self._next_collection = COLLECTION_BASE
        self._next_tag_id = 90000
        # Play history, newest last -- the order /status/sessions/history/all
        # returns (measured on fun: the newest rows are at the highest offset).
        self._history = []
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
                self.record_view(leaf, account_id=account_id, device_id=device_id)
            else:
                leaf.pop("viewCount", None)
                leaf.pop("viewOffset", None)
                leaf.pop("lastViewedAt", None)
        self.recount(row)
        return targets

    def rate(self, rating_key, rating):
        """`/:/rate?key=&rating=` -- the star rating. -1 clears it."""
        row = self.catalog.items.get(rating_key)
        if row is None:
            return None
        if rating is None or rating < 0:
            row.pop("userRating", None)
            row.pop("lastRatedAt", None)
        else:
            row["userRating"] = float(rating)
            row["lastRatedAt"] = _now()
        return row

    def progress(self, rating_key, offset, state=None, duration=None):
        """A timeline update. `stopped` past 90% counts as a view, as PMS does."""
        row = self.catalog.items.get(rating_key)
        if row is None:
            return None
        total = duration or row.get("duration") or 0
        if offset is not None:
            row["viewOffset"] = int(offset)
            row["lastViewedAt"] = _now()
        if state == "stopped":
            if total and row.get("viewOffset", 0) >= total * 0.9:
                row["viewCount"] = int(row.get("viewCount", 0)) + 1
                row.pop("viewOffset", None)
                self.record_view(row)
            self.recount(row)
        return row

    def remove_from_continue_watching(self, rating_key):
        """The menu entry of the same name: drop the resume point, keep plays."""
        row = self.catalog.items.get(rating_key)
        if row is None:
            return None
        row.pop("viewOffset", None)
        self.recount(row)
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
        entry = {
            "historyKey": "/status/sessions/history/%d" % (len(self._history) + 1),
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

    def history(self):
        return list(self._history)

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

    def edit(self, row, params):
        """`PUT /library/metadata/{id}?title.value=X&title.locked=1`.

        Three param shapes, all from the client's own `getSaveParams`:
          `<field>.value=`         set a field
          `<field>.locked=0|1`     lock it against a future agent refresh
          `<tag>[N].tag.tag=`      set tag N of a tag type
          `<tag>[].tag.tag-=a,b`   remove those tags
        """
        changed = False
        for name, value in params.items():
            if name.endswith(".value"):
                field = name[: -len(".value")]
                if value == "":
                    row.pop(field, None)
                elif field in INTEGER_FIELDS and str(value).lstrip("-").isdigit():
                    # `year.value=2012` arrives as a string; a real row carries
                    # an int here, and the client sorts and filters on it.
                    row[field] = int(value)
                else:
                    row[field] = value
                changed = True
            elif name.endswith(".locked"):
                self._set_lock(row, name[: -len(".locked")], value not in ("0", "", None))
                changed = True
            elif ".tag.tag-" in name:
                tag_type = name.split("[", 1)[0].lower()
                self._remove_tags(row, tag_type, [v for v in value.split(",") if v])
                changed = True
            elif ".tag.tag" in name:
                tag_type = name.split("[", 1)[0].lower()
                self._add_tag(row, tag_type, value)
                changed = True
        if changed:
            row["updatedAt"] = _now()
        return changed

    def _set_lock(self, row, field, locked):
        """A real PMS reports locks as a `Field` array on the row."""
        fields = [f for f in row.get("Field", []) if f.get("name") != field]
        if locked:
            fields.append({"locked": True, "name": field})
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
                collection = self.find_collection(value, row.get("librarySectionID"))
                if not collection:
                    continue
                collection["items"] = [k for k in collection["items"]
                                       if int(k) != int(row["ratingKey"])]
                if not collection["items"]:
                    # A Plex collection exists only as the tag on its members:
                    # untag the last one and the collection is gone, rather than
                    # lingering in the Collections pivot as an empty row.
                    self._collections.pop(collection["ratingKey"], None)

    # -- match / unmatch / refresh / split / merge ------------------------

    def unmatch(self, row, identify=None):
        """Strip the agent's identity, the way `PUT .../unmatch` does.

        A real PMS keeps the file and drops everything the agent supplied, and
        the row's guid becomes a `local://` one -- which is exactly what flips
        the menu from "Fix Match..." to "Match...".
        """
        for key in ("guid", "Guid", "summary", "tagline", "studio", "contentRating",
                    "rating", "audienceRating", "audienceRatingImage", "Genre",
                    "Country", "Role", "Director", "Writer", "Producer", "slug",
                    "originallyAvailableAt", "year", "UltraBlurColors",
                    "_poster_url", "_art_url", "thumb", "art"):
            row.pop(key, None)
        row["guid"] = "local://%s" % row["ratingKey"]
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
        self.add_activity(row.get("title") or "", title="Unmatching")
        return row

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
        row["guid"] = guid
        if name:
            row["title"] = name
        if year:
            try:
                row["year"] = int(year)
            except (TypeError, ValueError):
                pass
        row["updatedAt"] = _now()
        self.add_activity(row.get("title") or "", title="Matching")
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
        self.add_activity(row.get("title") or "")
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

    def create_playlist(self, title, playlist_type, rows, smart=False):
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
        }
        self._playlists[key] = playlist
        self.add_playlist_items(playlist, rows)
        return playlist

    def add_playlist_items(self, playlist, rows):
        """`PUT /playlists/{id}/items?uri=` -- returns how many were added.

        The real response carries `leafCountAdded` and `leafCountRequested`, so
        the client can tell the user "3 of 5 added".
        """
        added = 0
        for row in rows:
            playlist["items"].append({
                "ratingKey": int(row["ratingKey"]),
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
        items.remove(moving)
        if after is None:
            items.insert(0, moving)
        else:
            index = next((n for n, i in enumerate(items)
                          if str(i["playlistItemID"]) == str(after)), None)
            items.insert(len(items) if index is None else index + 1, moving)
        playlist["updatedAt"] = _now()
        return True

    def move_collection_item(self, collection, rating_key, after=None):
        """`PUT /library/collections/{id}/items/{ratingKey}/move[?after=]`.

        Keyed by the member's own ratingKey here, not by a playlistItemID --
        the two endpoints look alike and their ids are not the same thing.
        """
        items = collection["items"]
        try:
            moving = next(k for k in items if int(k) == int(rating_key))
        except (StopIteration, TypeError, ValueError):
            return False
        items.remove(moving)
        if after is None:
            items.insert(0, moving)
        else:
            index = next((n for n, k in enumerate(items) if str(k) == str(after)), None)
            items.insert(len(items) if index is None else index + 1, moving)
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

    def playlist(self, key):
        return self._playlists.get(int(key)) if str(key).isdigit() else None

    def edit_playlist(self, playlist, title=None, summary=None):
        if title:
            playlist["title"] = title
        if summary is not None:
            playlist["summary"] = summary
        playlist["updatedAt"] = _now()

    def delete_playlist(self, key):
        return self._playlists.pop(int(key), None) is not None

    def playlist_items(self, playlist):
        """The playlist's rows, each carrying its playlistItemID."""
        out = []
        for entry in playlist["items"]:
            row = self.catalog.items.get(entry["ratingKey"])
            if row is None:
                continue
            out.append((row, entry["playlistItemID"]))
        return out

    def _playlist_row(self, playlist):
        """Field set measured on a real PMS's POST /playlists response."""
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
            "composite": "/playlists/%s/composite/%d" % (playlist["ratingKey"], stamp),
            "duration": duration,
            "leafCount": len(rows),
            "addedAt": playlist["addedAt"],
            "updatedAt": stamp,
        }
        return row

    # -- collections -----------------------------------------------------

    def collections(self, section_id=None):
        rows = [self._collection_row(c) for c in self._collections.values()
                if section_id is None or str(c["librarySectionID"]) == str(section_id)]
        return sorted(rows, key=lambda r: str(r.get("titleSort") or r.get("title") or ""))

    def create_collection(self, title, section_id, rows, subtype="movie", smart=False):
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
        }
        self._collections[key] = collection
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
            self._add_tag(row, "collection", collection["title"])
            added += 1
        if added:
            collection["updatedAt"] = _now()
        return added

    def collection(self, key):
        return self._collections.get(int(key)) if str(key).isdigit() else None

    def find_collection(self, title, section_id=None):
        for collection in self._collections.values():
            if collection["title"] == title and (
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
        """Rename or re-summarise a collection, members included.

        The tag is renamed in PLACE rather than removed and re-added. Going
        through the tag helpers instead looks equivalent and is not: removing
        the old tag drops the member from this collection's own item list (that
        is how "Add to Collection" and its reverse work), and adding the new one
        then creates a SECOND collection under the new name -- leaving the
        original empty, so deleting it afterwards untags nobody.
        """
        if title and title != collection["title"]:
            old_title = collection["title"]
            collection["title"] = title
            for key in collection["items"]:
                row = self.catalog.items.get(int(key))
                if row is None:
                    continue
                for tag in row.get("Collection") or []:
                    if tag.get("tag") == old_title:
                        tag["tag"] = title
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
                self._remove_tags(row, "collection", [collection["title"]])
        return True

    def collection_children(self, collection):
        rows = [self.catalog.items.get(int(k)) for k in collection["items"]]
        return [r for r in rows if r]

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
            "smart": collection["smart"],
            "librarySectionID": collection["librarySectionID"],
        }
        if years:
            row["minYear"] = years[0]
            row["maxYear"] = years[-1]
        return row

    # -- helpers ---------------------------------------------------------

    def _leaves_or_self(self, row):
        if row.get("type") in ("show", "season"):
            return self.catalog.leaves(row["ratingKey"])
        return [row]
