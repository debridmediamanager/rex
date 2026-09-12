"""Turn identified leaves into a real Plex library shape.

A Plex TV library is not a flat list of files. It is show -> season -> episode,
and a client relies on that: an episode row carries `grandparent*` and `parent*`
pointers, and the poster a grid draws for an episode comes from its SHOW, not
from the episode itself. plexsim originally emitted every file as `type=movie`
while still attaching `index`, `parentIndex` and `grandparentTitle` -- a row
that claims to be a movie and an episode at once. Observed effect: identified
episodes rendered as blank tiles in the library grid while a genuine movie in
the same grid drew its poster correctly.

So: movies go in a movie section, episodes get built into a show section with
their parents, and anything unidentified stays a movie because that is the
honest answer for a file nothing could be matched to.
"""

import hashlib
import re

# ratingKey ranges, kept disjoint so a key identifies its own row type on sight.
SHOW_BASE = 20000
SEASON_BASE = 30000

MOVIE_SECTION = "1"
SHOW_SECTION = "2"


def _uuid(seed):
    return hashlib.sha1(("plexsim/" + seed).encode()).hexdigest()


def sections(name):
    return [
        {
            "key": MOVIE_SECTION,
            "type": "movie",
            "title": "Debrid Movies",
            "uuid": _uuid("section/1/" + name),
        },
        {
            "key": SHOW_SECTION,
            "type": "show",
            "title": "Debrid TV",
            "uuid": _uuid("section/2/" + name),
        },
    ]


def _carry(source, target, keys):
    for key in keys:
        if source.get(key) is not None:
            target[key] = source[key]


def _fill_real_pms_fields(rows):
    """Add the fields a real PMS show/season row carries that plexsim omitted.

    Individually these all look decorative -- library paths, a theme URL, a
    slug, lock flags, a colour palette. They are added as a batch because the
    Seasons block would not render and every structural difference had already
    been eliminated; if the block appears, bisecting this list finds the one
    that matters.
    """
    for row in rows:
        kind = row.get("type")
        rating_key = row["ratingKey"]

        if kind in ("show", "movie"):
            # The library folder a real item lives in. plexsim owns no files,
            # so this is the synthetic path already used for Part.file.
            row.setdefault("Location", [{"id": int(rating_key), "path":
                                         "/plexsim/%s/%s" % (row.get("librarySectionID"), rating_key)}])
            row.setdefault("slug", _slug(row.get("title") or ""))
            if row.get("rating") is not None:
                row.setdefault("Rating", [
                    {"image": "themoviedb://image.rating", "value": row["rating"], "type": "audience"},
                ])
                row.setdefault("audienceRatingImage", "themoviedb://image.rating")
            row.setdefault("Field", [{"locked": True, "name": "thumb"}])
            row.setdefault("Label", [])

        if kind in ("show", "season", "episode"):
            show_key = (rating_key if kind == "show"
                        else row.get("grandparentRatingKey") or row.get("parentRatingKey"))
            if show_key:
                row.setdefault("theme", "/library/metadata/%s/theme/1" % show_key)

        if kind in ("show", "season", "movie", "episode"):
            # Plex derives this palette from the artwork; the values are only
            # used for background tinting.
            row.setdefault("UltraBlurColors", {
                "topLeft": "1a1a1e", "topRight": "24242a",
                "bottomRight": "15151a", "bottomLeft": "1f1f26",
            })

        if kind == "season":
            parent = row.get("parentTitle") or ""
            row.setdefault("parentSlug", _slug(parent))
            row.setdefault("parentTheme", "/library/metadata/%s/theme/1" % row.get("parentRatingKey"))
        if kind == "episode":
            row.setdefault("grandparentSlug", _slug(row.get("grandparentTitle") or ""))
            row.setdefault("grandparentTheme",
                           "/library/metadata/%s/theme/1" % row.get("grandparentRatingKey"))
            row.setdefault("chapterSource", "media")


def _slug(title):
    out = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return out or "item"


def _stamp_artwork(rows):
    """Version every artwork URL so a client cannot serve a stale poster.

    A real PMS emits `/library/metadata/<rk>/thumb/<version>` -- fun uses a
    timestamp. plexsim emitted a constant `/thumb/1`, so when a ratingKey was
    reused by a later catalog build the URL stayed identical while the image
    behind it changed: The Simpsons at rk=20001 rendered with The Mentalist's
    poster, cached by the client from a previous build.

    The version is a hash of the artwork URL itself, so it changes exactly when
    the artwork does and stays stable when it does not.
    """
    for row in rows:
        for field, source in (("thumb", "_poster_url"), ("art", "_art_url")):
            if not row.get(field):
                continue
            origin = row.get(source) or ""
            version = hashlib.sha1(origin.encode()).hexdigest()[:10] if origin else "1"
            row[field] = "/library/metadata/%s/%s/%s" % (row["ratingKey"], field, version)

    # Parent pointers must match the parent's stamped URL, not the old constant.
    by_key = {r["ratingKey"]: r for r in rows}
    for row in rows:
        parent = by_key.get(str(row.get("parentRatingKey") or ""))
        if parent:
            if parent.get("thumb"):
                row["parentThumb"] = parent["thumb"]
        grand = by_key.get(str(row.get("grandparentRatingKey") or ""))
        if grand:
            if grand.get("thumb"):
                row["grandparentThumb"] = grand["thumb"]
            if grand.get("art"):
                row["grandparentArt"] = grand["art"]
        # An episode's `art` is its season's.
        if row.get("type") == "episode" and parent and parent.get("art"):
            row["art"] = parent["art"]


def build(leaves):
    """[leaf rows] -> [all rows: movies, shows, seasons, episodes].

    Leaves are mutated in place into either a movie or an episode row; show and
    season rows are synthesised from the `_show_*` fields metadata.enrich left
    behind.
    """
    rows = []
    shows = {}      # tmdb id -> show row
    seasons = {}    # (tmdb id, season) -> season row
    next_show = SHOW_BASE
    next_season = SEASON_BASE

    for leaf in leaves:
        is_episode = (
            leaf.get("_kind") == "tv"
            and leaf.get("_season") is not None
            and leaf.get("_episode") is not None
        )

        if not is_episode:
            # A movie, or an unidentified file presented as one.
            leaf["type"] = "movie"
            leaf["librarySectionID"] = int(MOVIE_SECTION)
            leaf["librarySectionTitle"] = "Debrid Movies"
            leaf["librarySectionKey"] = "/library/sections/" + MOVIE_SECTION
            # An earlier version left episode pointers on movie rows; a client
            # then looks for parent artwork that does not exist.
            for key in ("index", "parentIndex", "grandparentTitle"):
                leaf.pop(key, None)
            rows.append(leaf)
            continue

        tmdb_id = leaf["_tmdb_id"]
        season_number = leaf["_season"]

        show = shows.get(tmdb_id)
        if show is None:
            next_show += 1
            show = {
                "ratingKey": str(next_show),
                "key": "/library/metadata/%d/children" % next_show,
                # The show's OWN guid. The leaf's guid is the episode's, so
                # reusing it made every show identify as its first episode.
                "guid": leaf.get("_show_guid") or leaf.get("guid"),
                "type": "show",
                "title": leaf.get("_show_title") or leaf.get("title"),
                "titleSort": leaf.get("_show_title") or leaf.get("title"),
                "summary": leaf.get("_show_summary") or "",
                "year": leaf.get("_show_year"),
                "thumb": "/library/metadata/%d/thumb/1" % next_show,
                "art": "/library/metadata/%d/art/1" % next_show,
                "librarySectionID": int(SHOW_SECTION),
                "librarySectionTitle": "Debrid TV",
                "librarySectionKey": "/library/sections/" + SHOW_SECTION,
                "addedAt": leaf.get("addedAt"),
                "updatedAt": leaf.get("updatedAt"),
                "childCount": 0,
                "leafCount": 0,
                "viewedLeafCount": 0,
                "_poster_url": leaf.get("_show_poster"),
                "_art_url": leaf.get("_show_art"),
            }
            # Show-level tags and ratings belong to the show, not the episode.
            _carry(leaf, show, ("contentRating", "studio", "Genre", "Country",
                                "Role", "rating", "audienceRating", "tagline"))
            # External ids live on the SHOW's Guid[] only; a real PMS leaves
            # the season and episode arrays empty.
            if leaf.get("_show_external_guids"):
                show["Guid"] = leaf["_show_external_guids"]
            shows[tmdb_id] = show
            rows.append(show)

        season = seasons.get((tmdb_id, season_number))
        if season is None:
            next_season += 1
            season = {
                "ratingKey": str(next_season),
                "key": "/library/metadata/%d/children" % next_season,
                "type": "season",
                # The season's own TMDB identity. Without a guid the season
                # list rendered empty in the client even though the rows were
                # otherwise complete.
                "guid": leaf.get("_season_guid"),
                "title": "Season %d" % season_number,
                "titleSort": "Season %d" % season_number,
                "index": season_number,
                # A real season carries parentIndex and year; without them the
                # desktop client renders the season list empty.
                "parentIndex": 1,
                "year": leaf.get("_season_year") or leaf.get("_show_year"),
                "summary": leaf.get("_season_summary") or "",
                "parentRatingKey": show["ratingKey"],
                "parentKey": "/library/metadata/%s" % show["ratingKey"],
                "parentTitle": show["title"],
                "parentThumb": show["thumb"],
                "parentGuid": show.get("guid"),
                "thumb": show["thumb"],
                "art": show["art"],
                "librarySectionID": int(SHOW_SECTION),
                "librarySectionTitle": "Debrid TV",
                "librarySectionKey": "/library/sections/" + SHOW_SECTION,
                "addedAt": leaf.get("addedAt"),
                "updatedAt": leaf.get("updatedAt"),
                "leafCount": 0,
                "viewedLeafCount": 0,
                "skipCount": 0,
                "parentStudio": show.get("studio"),
                "parentYear": show.get("year"),
                # A season's own poster when TMDB has one, else the show's.
                "_poster_url": leaf.get("_season_poster") or leaf.get("_show_poster"),
                "_art_url": leaf.get("_show_art"),
            }
            seasons[(tmdb_id, season_number)] = season
            show["childCount"] += 1
            rows.append(season)

        # The leaf becomes a proper episode: its own still as `thumb`, the
        # show's artwork reachable through the grandparent fields, which is
        # where a client looks for a poster when drawing an episode.
        episode_still = leaf.pop("_art_url", None)
        leaf["type"] = "episode"
        leaf["index"] = leaf.pop("_episode")
        leaf["parentIndex"] = season_number
        leaf["librarySectionID"] = int(SHOW_SECTION)
        leaf["librarySectionTitle"] = "Debrid TV"
        leaf["librarySectionKey"] = "/library/sections/" + SHOW_SECTION
        leaf["parentRatingKey"] = season["ratingKey"]
        leaf["parentKey"] = "/library/metadata/%s" % season["ratingKey"]
        leaf["parentTitle"] = season["title"]
        leaf["parentThumb"] = season["thumb"]
        leaf["grandparentRatingKey"] = show["ratingKey"]
        leaf["grandparentKey"] = "/library/metadata/%s" % show["ratingKey"]
        leaf["grandparentTitle"] = show["title"]
        leaf["grandparentThumb"] = show["thumb"]
        leaf["grandparentArt"] = show["art"]
        leaf["grandparentGuid"] = show.get("guid")
        leaf["parentGuid"] = season.get("parentGuid")
        # Verified against a real episode: `art` points at the SEASON.
        leaf["art"] = season["art"]
        if show.get("contentRating") and not leaf.get("contentRating"):
            leaf["contentRating"] = show["contentRating"]
        # The episode's own still, when TMDB had one.
        leaf["_poster_url"] = episode_still or leaf.get("_show_poster")
        leaf["_art_url"] = leaf.get("_show_art")
        show["leafCount"] += 1
        season["leafCount"] += 1
        rows.append(leaf)

    _fill_real_pms_fields(rows)
    _stamp_artwork(rows)

    # Internal hints are consumed now; the server strips `_` keys anyway, but
    # leaving stale ones in the catalog file invites confusion.
    for row in rows:
        for key in ("_kind", "_tmdb_id", "_show_title", "_show_summary",
                    "_show_year", "_show_poster", "_show_art", "_show_guid",
                    "_season", "_episode", "_season_guid", "_season_poster",
                    "_show_external_guids",
                    "_season_year", "_season_air", "_season_summary"):
            row.pop(key, None)
    return rows
