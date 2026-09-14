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
import unicodedata

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

        if kind == "show":
            # The library folder a real show lives in. plexsim owns no files, so
            # this is the synthetic path already used for Part.file. Measured: a
            # MOVIE carries no Location on either real server, and a show carries it
            # on its detail row only.
            row.setdefault("Location", [{"path": "/plexsim/%s/%s"
                                         % (row.get("librarySectionID"), rating_key)}])
        if kind in ("show", "movie"):
            row.setdefault("slug", _slug(row.get("title") or ""))
            if row.get("rating") is not None:
                row.setdefault("Rating", [
                    {"image": "themoviedb://image.rating", "value": row["rating"], "type": "audience"},
                ])
                row.setdefault("audienceRatingImage", "themoviedb://image.rating")

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
        if kind == "episode":
            row.setdefault("grandparentSlug", _slug(row.get("grandparentTitle") or ""))


_ARTICLE = re.compile(r"(?i)^(the|a|an)\s+")
# Cycle 17, measured on plexscratch character by character: the characters a
# real PMS drops from the FRONT of a sort title, alongside whitespace. See
# `sort_title` for why this is a membership table and not a predicate.
_LEADING_STRIP = ("\"'(-.[{~"
                  + "".join(chr(code) for code in range(0x80, 0xc0))
                  + "".join(chr(code) for code in range(0x2000, 0x3000))
                  + " \t\r\n\f\v")
_MEDIA_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts",
                     ".wmv", ".flv", ".mpg", ".mpeg")


# The characters a real PMS folds that NFD does NOT decompose. Cycle 18, swept
# character by character on plexscratch over every codepoint from U+0080 to U+02FF that
# NFD leaves alone -- 282 alphabetic and 106 non-alphabetic -- by writing a title with
# each one in the middle and reading back the sort title. Exactly ELEVEN fold, all of
# them in Latin-1, and the targets are not a transliteration anyone would write by hand:
# `\u00f8` and `\u00d8` become the DIGIT ZERO and `\u00f0` becomes a capital `O`. Two of
# them are witnessed on zen's real library as well (`D\u00f8d Kalm` sorts as `D0d Kalm`
# and `D\u00e6monicus` as `Demonicus`), which is what found the gap: plexsim folded
# combining marks only, so both rows carried no `titleSort` at all where a real PMS
# sends one. Nothing above U+02FF folds -- Greek, Cyrillic and CJK all survive, as the
# 51 non-ASCII rows of zen already showed.
_FOLD = {"\u00c6": "E", "\u00d0": "D", "\u00d7": "x", "\u00d8": "0", "\u00de": "P",
         "\u00df": "s", "\u00e6": "e", "\u00f0": "O", "\u00f7": "/", "\u00f8": "0",
         "\u00fe": "p"}


def fold_marks(text):
    """Drop Unicode combining marks, then the eleven characters NFD cannot reach."""
    return "".join(_FOLD.get(ch, ch)
                   for ch in unicodedata.normalize("NFD", text)
                   if not unicodedata.combining(ch))


def sort_title(title):
    """The title a real PMS sorts by: whitespace trimmed, combining marks folded
    away, then the leading English article stripped.

    Cycle 14 re-measured this on zen (1.42.2, the build plexsim reports) over all
    5,294 rows of a real library, because the old rule had only fun -- now out of
    scope -- as its witness and a five-clip lab has no title that can show the rest.
    Three things, in this order, reproduce 5,289 of those rows exactly:

    * Leading whitespace goes, and ONLY leading whitespace. 90 rows of zen begin
      with a space and every one carries a `titleSort` with it removed; none is
      missing one. Cycle 14 read that as `.strip()` and recorded the symmetric
      half as unwitnessed, because zen holds no row with trailing or doubled
      whitespace at all. Cycle 15 settled it by writing one on the copy:
      `'  Zeta Both  '` sorts as `'Zeta Both  '`, and `'Zeta Trail '` carries NO
      `titleSort`, because the derived string still equals the title. The trailing
      space survives, so this is an `lstrip`; `'The Zeta Art '` gives `'Zeta Art '`,
      so the article still comes off afterwards.
    * Combining marks are folded to ASCII: `Amelie`, `Bahubali`, `Bogota`, `Cafe`,
      `Senor's`. All 21 accented rows across movies, shows and episodes carry the
      folded form and not one lacks a titleSort -- which is what makes this the
      server's own derivation rather than something an agent supplied. The fold is
      combining marks ONLY: the 51 rows whose non-ASCII survives NFD (CJK, the
      curly apostrophe, an en dash) carry NO titleSort at all, because for them the
      derived string equals the title.
    * Then the article, as before.

    The five rows this misses are Spanish `La` (4) and German `Das` (1). Those are
    NOT mechanical -- `Les hommes de la baleine`, `Die Die Delta Pi II` and
    `O Ultimo Portal` keep theirs -- so the article list is per-item language, which
    plexsim's catalog does not carry. Left alone deliberately; see docs/findings.md.

    Cycle 17 re-measured the leading trim on plexscratch character by character and
    found it is not whitespace alone: a leading run of PUNCTUATION comes off too, and
    before the article. zen's episode 10866, "'Til Death Do Us Art", sorts as "Til Death
    Do Us Art" and sits in the `T` bucket of an episode `firstCharacter` axis that has
    no apostrophe bucket at all -- 27 buckets, `#` then A-Z -- so the rule holds on
    1.42.2 as well as on the copy where the table was measured.

    The set is NOT a Unicode category and cannot be written as one. In ASCII exactly
    eight characters strip -- `"` `'` `(` `-` `.` `[` `{` `~` -- while `! # $ % & ) * +
    , / : ; < = > ? @ \ ] ^ _ ` | }` do not, so it is neither "opening brackets" nor
    "Po" nor "Sm". Outside ASCII it is two whole ranges: every character from U+0080 to
    U+00BF and every character from U+2000 to U+2FFF strips (85 of 85 tested across
    them, plus every block boundary probed), while U+0100..U+1FFF and everything from
    U+3000 up does not -- the CJK and fullwidth brackets stay. The run may mix those
    characters with whitespace and repeat, and the guard is that a title made only of
    them keeps its own text rather than sorting as the empty string.

    Only the LEADING half of the whitespace trim has a witness (zen holds no title
    with trailing or doubled whitespace); `.strip()` here is the symmetric reading.
    """
    folded = fold_marks(title or "")
    trimmed = folded.lstrip(_LEADING_STRIP)
    # A title made of nothing but strippable characters keeps its own text: measured,
    # `'''` and `(((` carry no titleSort at all, which is what a derived string equal
    # to the title looks like.
    return _ARTICLE.sub("", trimmed or folded)


# The title forms a real PMS generates when it has none of its own. A season plexsim
# synthesises is always one of them: `hierarchy` builds "Season %d" from the index.
_GENERATED_TITLE = re.compile(r"^Season (\d+)$")


def stored_title(row):
    """The title a real PMS stores for a row, which is "" where it GENERATED one.

    Cycle 18, measured on plexscratch with a control and corroborated on zen and kids:
    a row whose title the server made up is not in the query surface at all. The
    reference's season 11 is titled `Season 2` and `/library/sections/2/search?type=3&
    query=season` answers NOTHING, while the same season renamed `Zulu Season Probe`
    answers to `zulu`, to `season` and to `probe`; its three unmatched episodes
    (`Episode 1`-`3`) answer nothing to `query=episode` while `The Train Job` answers
    `train`. The `title` FILTER agrees: zen's `type=3&title=Season` is 0 of 741 seasons
    and its `type=4&title=Episode` is 203 -- the agent-supplied `Episode N` titles, the
    ones carrying `plex://episode` guids -- of 9,287 episodes.

    It is the same rule `stored_sort_title` reads from the other side: the generated
    title is composed for DISPLAY and stored nowhere, so nothing that queries the
    column can see it. plexsim synthesises every season title, so its seasons answered
    a search for "season" where a real PMS answers none.
    """
    return "" if _GENERATED_TITLE.match(row.get("title") or "") \
        and row.get("type") == "season" else (row.get("title") or "")


def stored_sort_title(row):
    """The sort title a real PMS STORES, which is not the one it sends.

    Measured on zen (1.42.2) over 14,433 rows of two real libraries and confirmed on
    kids (1.43.4): a row whose title the SERVER generated stores the EMPTY STRING as
    its sort title, where a row whose title an agent supplied stores the derived one.
    The three observables all agree, and they are what a client sees:

    * `?titleSort=` -- a bare, EMPTY value, which on this field is an equality --
      answers exactly 740 of zen's 741 seasons, 4,630 of its 9,287 episodes, kids'
      one unmatched season and its three unmatched episodes, and 0 of either
      library's movies and shows. `titleSort!=` answers the complement.
    * `?titleSort<<=0` answers the same sets: the empty string compares below `0`.
    * `/library/sections/{id}/firstCharacter/%23` answers them, and every LETTER
      bucket of a season list answers none -- which is the other half of cycle 16's
      "the axis counts by the SHOW's sort title and the listing filters on the row's
      own". The listing half was implemented reading the row's TITLE when it stores
      no sort title, so plexsim's season buckets answered under `S`.
    * `sort=titleSort` puts them first ascending and last descending, as an empty
      string does and as cycle 13's "no value sorts BELOW every value" says.

    The one season and the 176 episodes that DO store a sort title while reading
    `Season 1`/`Episode 1` are rows an agent gave that exact title to (kids' season 7
    gained its own in the metadata refresh recorded in HANDOVER 4.1, and zen's 176
    carry `plex://episode` guids). plexsim cannot tell those apart from a title it
    generated itself, so it decides on the title FORM -- a proxy for the mechanism,
    recorded in HANDOVER 3.3. Every season in a plexsim catalog is synthesised here,
    so the proxy is exact for the rows plexsim actually holds.

    An emptied title is NOT this case: a title write that empties a row regenerates
    `Season N`/`Episode N` and KEEPS the old title's sort form, which is measured and
    already implemented.
    """
    title = row.get("title") or ""
    if row.get("type") == "season" and _GENERATED_TITLE.match(title):
        stored = row.get("titleSort")
        if not stored or stored == title:
            return ""
    return row.get("titleSort") or title


def normalize_rows(rows):
    """The row rules a real PMS follows, applied to rows however they were built.

    Run at the end of a build and again when a catalog LOADS, because catalogs
    built before these were measured carry the old fields baked in.

    * `titleSort` is emitted only when it differs from `title` -- 0 of 3,856 movies
      and 0 of 599 shows on fun carry an equal one -- and it is the stripped title.
      plexsim sent the unstripped title on every movie, so "The Simpsons" filed
      under T, and every episode carried its SHOW's name.
    * `originalTitle` is the original-language title, present only when it differs
      (`Twelve Monkeys` for `12 Monkeys`). plexsim put the media FILENAME there, and
      the client prints it on the preplay page, in the window title and as the
      OS media-session artist line.
    * No `Field` lock and no empty `Label` by default: a real row lists a locked
      field only when one is locked, and a `Field locked thumb` on every row told
      the Edit dialog every poster was pinned.
    * A movie has no `Location`; neither real server sends one.
    * `rating` is the CRITIC rating (Rotten Tomatoes on fun), absent when there is
      none -- the lab's own TMDB-matched movies carry only `audienceRating`.
      plexsim copied the TMDB vote into both.
    * A season with a year of its own carries it, a `titleSort` EQUAL to its title
      (the one row type that does) and no `parentYear`; one without has
      `parentYear` only. A zero `skipCount` is omitted. An episode has no
      `chapterSource` without chapters. A show has `index: 1`, and its `Location`
      is `{path}` with no id -- the id belongs to a SECTION's location.
    * No row names a theme -- `theme`, `parentTheme`, `grandparentTheme` -- because
      plexsim has no theme audio and its theme endpoint 404s. Measured on fun: a show
      without a theme carries none of the three, on itself or its children, and the
      web client asks for whichever it finds when theme music is on.
    """
    by_key = {str(row.get("ratingKey")): row for row in rows}
    # Cycle 16, measured on kids and zen: a row that borrows its background carries
    # the OWNER's path, not one of its own. A catalog built before that was measured
    # carries a re-keyed copy on every season and episode, and the `_art_url` hints
    # the builder used are stripped from the stored file -- but the VERSION is a hash
    # of the source image, so a season whose art carries its show's version is showing
    # its show's picture. Rewrite it to the show's path, then hand it down: an
    # episode's `art` is its season's resolved value, which is the show's when the
    # season borrows.
    for row in rows:
        if row.get("type") != "season":
            continue
        show = by_key.get(str(row.get("parentRatingKey")))
        art, parent_art = str(row.get("art") or ""), str((show or {}).get("art") or "")
        if parent_art and art.rsplit("/", 1)[-1] == parent_art.rsplit("/", 1)[-1]:
            row["art"] = parent_art
    for row in rows:
        if row.get("type") != "episode":
            continue
        season = by_key.get(str(row.get("parentRatingKey")))
        if season and season.get("art"):
            row["art"] = season["art"]
    for row in rows:
        if row.get("type") not in ("show", "season"):
            continue
        # Cycle 19, measured on zen (300 of 300 shows, 300 of 300 seasons) and kids
        # (both its seasons): a row that HAS children names its child list, not
        # itself -- `key="/library/metadata/{id}/children"` -- while an episode keeps
        # the bare item path. `hierarchy` builds one that way, but three of the four
        # seasons cycle 13 added to `tests/catalog_ui.json` by hand carry the bare
        # path, and the client follows `key` verbatim: a click on Season 20, 21 or 22
        # fetched the season's own metadata row instead of its episode list, so the
        # season page drew nothing. Corrected at LOAD, like the other row rules here,
        # because the wrong value is baked into catalogs already written.
        key = str(row.get("key") or "")
        if key and not key.endswith("/children"):
            row["key"] = key.rstrip("/") + "/children"
    for row in rows:
        kind = row.get("type")
        title = row.get("title") or ""
        derived = sort_title(title)
        if derived and derived != title:
            row["titleSort"] = derived
        else:
            row.pop("titleSort", None)
        original = row.get("originalTitle")
        if original is not None:
            files = {str(part.get("file") or "").rsplit("/", 1)[-1]
                     for media in row.get("Media") or [] for part in media.get("Part") or []}
            if (original in files or original == title
                    or str(original).lower().endswith(_MEDIA_EXTENSIONS)):
                row.pop("originalTitle", None)
        if row.get("Field") == [{"locked": True, "name": "thumb"}]:
            row.pop("Field")
        if row.get("Label") == []:
            row.pop("Label")
        if kind == "movie":
            row.pop("Location", None)
        if kind in ("movie", "show", "episode"):
            # `rating` is the CRITIC rating and plexsim copies the TMDB vote into both,
            # so it is dropped -- extended to EPISODE here because no episode on either
            # reference carries one (0 of 20 One Piece episodes on zen, 0 of 3 on kids).
            row.pop("rating", None)
        if kind == "episode" and not row.get("contentRating"):
            # Measured on kids (1.43.4) and zen (1.42.2): an episode carries its SHOW's
            # `contentRating` -- 6 of 6 on the lab, the three unmatched ones included
            # although they have no agent metadata at all, and 18 of 18 across three zen
            # shows (TV-14, TV-Y, TV-PG). A SEASON carries none on either server, and the
            # write side agrees: on a writable copy, editing the show's rating moved it
            # on the episodes that had none of their own and a show unmatch removed it
            # from all of them. plexsim left an episode with no certificate at all.
            parent = by_key.get(str(row.get("grandparentRatingKey")))
            if parent and parent.get("contentRating"):
                row["contentRating"] = parent["contentRating"]
        if kind == "episode" and row.get("audienceRating") is not None:
            # Measured on kids (1.43.4) and zen (1.42.2): an EPISODE with an
            # `audienceRating` carries `audienceRatingImage` and a `Rating` element as
            # well -- 60 of 60 One Piece episodes and 3 of 3 on the lab, with no
            # exception, while the unmatched episodes with no rating carry none of the
            # three and a SEASON never carries any of them (0 of 12 sampled across both
            # servers). plexsim built these for a show and a movie only, so its own
            # episode rows contradicted its own movie rows and no episode page or
            # episode listing row drew a rating badge. A real PMS sends two agents'
            # rows here (imdb and themoviedb); plexsim has one agent, so it sends the
            # one row it can honestly source.
            row.setdefault("Rating", [{"image": "themoviedb://image.rating",
                                       "value": row["audienceRating"], "type": "audience"}])
            row.setdefault("audienceRatingImage", "themoviedb://image.rating")
        if kind == "show":
            row.setdefault("index", 1)
        for key in ("theme", "parentTheme", "grandparentTheme"):
            row.pop(key, None)
        if kind == "season":
            if not row.get("skipCount"):
                row.pop("skipCount", None)
            if row.get("year"):
                # Cycle 20, held out against zen's 741 real seasons: the `year` does NOT
                # bring a `titleSort` with it. 740 carry none, and the only one that does
                # -- the only one with a `year` -- carries `Egghead`, the arc name its
                # agent stored, where the row DISPLAYS `Season 22`. Cycle 7 read kids'
                # season 7 (`Season 1`, agent-supplied and therefore stored) as "a year
                # means titleSort == title"; the year is a coincidence of a season an
                # agent really identified. plexsim generates every season title, so the
                # general rule above is the right one: a derived sort title equal to the
                # title is not sent -- which is what `stored_sort_title` has answered to
                # `?titleSort=` all along, so the row and the filter now agree.
                row.pop("parentYear", None)
            else:
                row.pop("year", None)
        if kind == "show":
            for location in row.get("Location") or []:
                location.pop("id", None)
        if kind == "episode" and row.get("parentGuid") == row.get("grandparentGuid"):
            # Measured on kids (1.43.4) and zen (1.42.2): an episode's parentGuid is the
            # SEASON's guid. A catalog built before that was measured carries the show's
            # guid in both, so the season's own guid is looked up here at load.
            season_row = by_key.get(str(row.get("parentRatingKey")))
            if season_row and season_row.get("guid"):
                row["parentGuid"] = season_row["guid"]
        if kind == "episode":
            if not row.get("Chapter"):
                row.pop("chapterSource", None)
    return rows


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
    # Cycle 16, measured on kids and zen: a row that owns no background of its own
    # carries its SHOW's art PATH verbatim -- kids' season 7 and episode 8 both carry
    # `/library/metadata/6/art/<v>` -- and `/library/metadata/7/art` is a 404 there,
    # while zen's season 11114, which DOES have art of its own, carries its own path
    # and answers 200. Stamping re-keyed every borrowed URL to the row holding it, so
    # three rows claimed the same image under three paths. This runs before the
    # episode fix-up below, which then inherits the corrected value.
    for row in rows:
        if row.get("type") != "season":
            continue
        show = by_key.get(str(row.get("parentRatingKey") or ""))
        if not show or not show.get("art"):
            continue
        own = row.get("_art_url")
        if not own or own == show.get("_art_url"):
            row["art"] = show["art"]
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
            # External ids for the SHOW. A real season and episode carry their own
            # too (tmdb/tvdb, and imdb for an episode, measured on both servers);
            # the catalog records none for them, so theirs stay absent rather than
            # borrowed from the show.
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
                "index": season_number,
                # A season carries a `year` only when it has an air date of its OWN
                # -- then `titleSort` too, equal to its title -- and `parentYear`
                # otherwise. Measured on 74 seasons on fun; never the show's year
                # borrowed. An older note tied the season list rendering to `year`;
                # that was the paging bug.
                "parentIndex": 1,
                "year": leaf.get("_season_year"),
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
        # Measured on kids (1.43.4) and zen (1.42.2): an episode's `parentGuid` is the
        # SEASON's own guid (`plex://season/...`), not the show's. plexsim copied the
        # season's `parentGuid` -- which is the show, correct for the season row itself
        # and wrong on the leaf -- so every episode of a show claimed to belong to one
        # and the same "season", and a client that keys per-season artwork or state off
        # it had nothing to tell the seasons apart with.
        leaf["parentGuid"] = season.get("guid") or season.get("parentGuid")
        # Verified against a real episode: `art` points at the SEASON.
        leaf["art"] = season["art"]
        if leaf.get("contentRating"):
            # the leaf's OWN rating, which a later show edit must not overwrite
            leaf["_own_content_rating"] = True
        elif show.get("contentRating"):
            leaf["contentRating"] = show["contentRating"]
        # The episode's own still, when TMDB had one.
        leaf["_poster_url"] = episode_still or leaf.get("_show_poster")
        leaf["_art_url"] = leaf.get("_show_art")
        show["leafCount"] += 1
        season["leafCount"] += 1
        rows.append(leaf)

    _fill_real_pms_fields(rows)
    _stamp_artwork(rows)
    normalize_rows(rows)

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
