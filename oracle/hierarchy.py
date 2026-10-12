"""Turn identified leaves into a real Plex library shape.

A Plex TV library is not a flat list of files. It is show -> season -> episode,
and a client relies on that: an episode row carries `grandparent*` and `parent*`
pointers, and the poster a grid draws for an episode comes from its SHOW, not
from the episode itself. rex originally emitted every file as `type=movie`
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
    # "plexsim/" is the pre-rename namespace, frozen: changing it re-mints every id it seeds.
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
    """Add the fields a real PMS show/season row carries that rex omitted.

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
            # The library folder a real show lives in. rex owns no files, so
            # this is the synthetic path already used for Part.file. Measured: a
            # MOVIE carries no Location on either real server, and a show carries it
            # on its detail row only.
            row.setdefault("Location", [{"path": "/rex/%s/%s"
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
            row.setdefault("UltraBlurColors", dict(DEFAULT_ULTRABLUR))

        if kind == "season":
            parent = row.get("parentTitle") or ""
            row.setdefault("parentSlug", _slug(parent))
        if kind == "episode":
            row.setdefault("grandparentSlug", _slug(row.get("grandparentTitle") or ""))


# Every field that can name a row's artwork path, its own or one it borrows.
_ARTWORK_FIELDS = ("thumb", "art", "banner", "theme", "composite",
                   "parentThumb", "parentArt", "parentTheme",
                   "grandparentThumb", "grandparentArt", "grandparentTheme")

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
# them are witnessed on the 1.42.2 reference's real library as well (`D\u00f8d Kalm` sorts
# as `D0d Kalm` and `D\u00e6monicus` as `Demonicus`), which is what found the gap: rex
# folded combining marks only, so both rows carried no `titleSort` at all where a real PMS
# sends one. Nothing above U+02FF folds -- Greek, Cyrillic and CJK all survive, as the
# 51 non-ASCII rows of the 1.42.2 reference already showed.

# The characters a real PMS folds in a TITLE, and it is a TABLE, not a decomposition.
# Cycle 32, swept on both writable copies -- plexscratch42 (1.42.2) and plexscratch
# (1.43.4), which agree on every codepoint -- by writing 2,076 codepoints across
# U+0080..U+02FF, Greek, Cyrillic, the phonetic and Latin Extended Additional blocks,
# the letterlike symbols, Latin Extended-C/D and the fullwidth forms into a title, forty
# at a time between two ASCII letters, and reading the sort title back. Exactly these
# 100 fold, and nothing outside Latin-1 and part of Latin Extended-A/B does.
#
# NFD cannot express it in either direction. It OVER-folds: `\u0119 \u0105 \u015f \u0107
# \u0142` and the CAPITAL macrons `\u0100 \u0112 \u012a \u014c \u016a` all survive a real
# PMS while their lower-case macrons fold, so a Polish, Turkish, Romanian or Baltic title
# sorted and bucketed on a letter it does not start with. And cycle 18's eleven are the
# ones it cannot reach at all. The targets are not a transliteration anyone would write:
# `\u00f8` becomes the digit zero, `\u00f0` a capital `O`, `\u00d7` an `x`, `\u00f7` a
# slash. Cycle 41 completed the mark boundary over 7,531 assigned punctuation, symbol,
# separator, mark and format characters, then repeated every disagreement between ASCII
# sentinels: exactly U+0300..U+036F is removed. The two PMS versions agree. A Unicode
# combining-class predicate over-removes 699 marks from other scripts and misses U+034F.
_TITLE_FOLD = {source: target for target, sources in (
    ("/", "\u00f7"),
    ("0", "\u00d8\u00f8"),
    ("A", "\u00c0\u00c1\u00c2\u00c3\u00c4\u00c5\u01cd"),
    ("a", "\u00e0\u00e1\u00e2\u00e3\u00e4\u00e5\u0101\u01ce"),
    ("C", "\u00c7\u010c"),
    ("c", "\u00e7\u010d"),
    ("D", "\u00d0\u010e"),
    ("d", "\u010f"),
    ("E", "\u00c6\u00c8\u00c9\u00ca\u00cb\u011a"),
    ("e", "\u00e6\u00e8\u00e9\u00ea\u00eb\u0113\u011b"),
    ("G", "\u01e6"),
    ("g", "\u01e7"),
    ("I", "\u00cc\u00cd\u00ce\u00cf\u01cf"),
    ("i", "\u00ec\u00ed\u00ee\u00ef\u012b\u01d0"),
    ("j", "\u01f0"),
    ("K", "\u01e8"),
    ("k", "\u01e9"),
    ("L", "\u013d"),
    ("l", "\u013e"),
    ("N", "\u00d1\u0147"),
    ("n", "\u00f1\u0148"),
    ("O", "\u00d2\u00d3\u00d4\u00d5\u00d6\u00f0\u01d1"),
    ("o", "\u00f2\u00f3\u00f4\u00f5\u00f6\u014d\u01d2"),
    ("P", "\u00de"),
    ("p", "\u00fe"),
    ("R", "\u0158"),
    ("r", "\u0159"),
    ("S", "\u0160"),
    ("s", "\u00df\u0161"),
    ("T", "\u0164"),
    ("t", "\u0165"),
    ("U", "\u00d9\u00da\u00db\u00dc\u01d3"),
    ("u", "\u00f9\u00fa\u00fb\u00fc\u016b\u01d4"),
    ("x", "\u00d7"),
    ("Y", "\u00dd"),
    ("y", "\u00fd\u00ff"),
    ("Z", "\u017d"),
    ("z", "\u017e"),
) for source in sources}


def fold_title(text):
    """The title with the measured mark block removed and fold table applied."""
    return "".join(_TITLE_FOLD.get(ch, ch) for ch in text
                   if not 0x0300 <= ord(ch) <= 0x036f)


def fold_case(text):
    """Simple case folding, one character to one character.

    Cycle 32, measured on both copies: the `title=` folded pass compares CASE-FOLDED text,
    and it is the simple fold -- the long s `\u017f` and the Kelvin sign `\u212a` match `s`
    and `k`, which `lower()` leaves alone, while `\u1e9e` does not become `ss`.
    """
    out = []
    for ch in text:
        folded = ch.casefold()
        out.append(folded if len(folded) == 1 else ch.lower())
    return "".join(out)


def fold_query(text):
    """A `title=` query as the folded pass reads it: the title table, then the case fold."""
    return fold_case(fold_title(text))


def sort_title(title):
    """The title a real PMS sorts by: whitespace trimmed, combining marks folded
    away, then the leading English article stripped.

    Cycle 14 re-measured this on the 1.42.2 reference (the build rex reports) over all
    5,294 rows of a real library, because the old rule had only the production server --
    now out of scope -- as its witness and a five-clip lab has no title that can show the
    rest. Three things, in this order, reproduce 5,289 of those rows exactly:

    * Leading whitespace goes, and ONLY leading whitespace. 90 rows of the 1.42.2 reference
      begin
      with a space and every one carries a `titleSort` with it removed; none is
      missing one. Cycle 14 read that as `.strip()` and recorded the symmetric
      half as unwitnessed, because the 1.42.2 reference holds no row with trailing or
      doubled whitespace at all. Cycle 15 settled it by writing one on the copy:
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
    rex's catalog does not carry. Left alone deliberately; see docs/findings.md.

    Cycle 17 re-measured the leading trim on plexscratch character by character and
    found it is not whitespace alone: a leading run of PUNCTUATION comes off too, and
    before the article. The 1.42.2 reference's episode 10866, "'Til Death Do Us Art", sorts
    as "Til Death Do Us Art" and sits in the `T` bucket of an episode `firstCharacter` axis
    that has no apostrophe bucket at all -- 27 buckets, `#` then A-Z -- so the rule holds
    on 1.42.2 as well as on the copy where the table was measured.

    The set is NOT a Unicode category and cannot be written as one. In ASCII exactly
    eight characters strip -- `"` `'` `(` `-` `.` `[` `{` `~` -- while `! # $ % & ) * +
    , / : ; < = > ? @ \\ ] ^ _ ` | }` do not, so it is neither "opening brackets" nor
    "Po" nor "Sm". Outside ASCII it is two whole ranges: every character from U+0080 to
    U+00BF and every character from U+2000 to U+2FFF strips (85 of 85 tested across
    them, plus every block boundary probed), while U+0100..U+1FFF and everything from
    U+3000 up does not -- the CJK and fullwidth brackets stay. The run may mix those
    characters with whitespace and repeat, and the guard is that a title made only of
    them keeps its own text rather than sorting as the empty string.

    Only the LEADING half of the whitespace trim has a witness (the 1.42.2 reference holds
    no title with trailing or doubled whitespace); `.strip()` here is the symmetric
    reading.
    """
    # Cycle 32: the fold is the measured TABLE, not NFD -- see `_TITLE_FOLD`.
    folded = fold_title(title or "")
    trimmed = folded.lstrip(_LEADING_STRIP)
    # A title made of nothing but strippable characters keeps its own text: measured,
    # `'''` and `(((` carry no titleSort at all, which is what a derived string equal
    # to the title looks like.
    return _ARTICLE.sub("", trimmed or folded)


# The title forms a real PMS generates when it has none of its own. A season rex
# synthesises is always one of them: `hierarchy` builds "Season %d" from the index.
# Cycle 20, measured on the 1.42.2 reference's 741 real seasons: a real PMS generates THREE
# season labels. `Specials` is season 0 (4 rows there, each `index: 0`) and `[Unknown
# Season]` is a season with no index at all (5 rows); both behave exactly as `Season N`
# does -- no stored sort title, absent from `title=` and from the section search box, and
# in the `#` bucket of the firstCharacter listing. plexlab holds neither, so the two labels
# are single-reference while the mechanism behind them is cycle 18's two-witness rule.
_GENERATED_TITLE = re.compile(r"^(Season \d+|Specials|\[Unknown Season\])$")


def stored_title(row):
    """The title a real PMS stores for a row, which is "" where it GENERATED one.

    Cycle 18, measured on plexscratch with a control and corroborated on the 1.42.2 reference and plexlab:
    a row whose title the server made up is not in the query surface at all. The
    reference's season 11 is titled `Season 2` and `/library/sections/2/search?type=3&
    query=season` answers NOTHING, while the same season renamed `Zulu Season Probe`
    answers to `zulu`, to `season` and to `probe`; its three unmatched episodes
    (`Episode 1`-`3`) answer nothing to `query=episode` while `The Train Job` answers
    `train`. The `title` FILTER agrees: the 1.42.2 reference's `type=3&title=Season` is 0 of 741 seasons
    and its `type=4&title=Episode` is 203 -- the agent-supplied `Episode N` titles, the
    ones carrying `plex://episode` guids -- of 9,287 episodes.

    It is the same rule `stored_sort_title` reads from the other side: the generated
    title is composed for DISPLAY and stored nowhere, so nothing that queries the
    column can see it. rex synthesises every season title, so its seasons answered
    a search for "season" where a real PMS answers none.
    """
    return "" if _GENERATED_TITLE.match(row.get("title") or "") \
        and row.get("type") == "season" else (row.get("title") or "")


def stored_sort_title(row):
    """The sort title a real PMS STORES, which is not the one it sends.

    Measured on the 1.42.2 reference over 14,433 rows of two real libraries and confirmed on
    plexlab (1.43.4): a row whose title the SERVER generated stores the EMPTY STRING as
    its sort title, where a row whose title an agent supplied stores the derived one.
    The three observables all agree, and they are what a client sees:

    * `?titleSort=` -- a bare, EMPTY value, which on this field is an equality --
      answers exactly 740 of the 1.42.2 reference's 741 seasons, 4,630 of its 9,287 episodes, plexlab's
      one unmatched season and its three unmatched episodes, and 0 of either
      library's movies and shows. `titleSort!=` answers the complement.
    * `?titleSort<<=0` answers the same sets: the empty string compares below `0`.
    * `/library/sections/{id}/firstCharacter/%23` answers them, and every LETTER
      bucket of a season list answers none -- which is the other half of cycle 16's
      "the axis counts by the SHOW's sort title and the listing filters on the row's
      own". The listing half was implemented reading the row's TITLE when it stores
      no sort title, so rex's season buckets answered under `S`.
    * `sort=titleSort` puts them first ascending and last descending, as an empty
      string does and as cycle 13's "no value sorts BELOW every value" says.

    The one season and the 176 episodes that DO store a sort title while reading
    `Season 1`/`Episode 1` are rows an agent gave that exact title to (plexlab's season 7
    gained its own in a metadata refresh, and the 1.42.2 reference's 176
    carry `plex://episode` guids). rex cannot tell those apart from a title it
    generated itself, so it decides on the title FORM -- a proxy for the mechanism,
    left open. Every season in a rex catalog is synthesised here,
    so the proxy is exact for the rows rex actually holds.

    An emptied title is NOT this case: a title write that empties a row regenerates
    `Season N`/`Episode N` and KEEPS the old title's sort form, which is measured and
    already implemented.
    """
    title = row.get("title") or ""
    if row.get("type") == "season" and _GENERATED_TITLE.match(title):
        # Cycle 31 read this off the reference's OWN DATABASE, which settles what the API
        # could only imply. plexlab's season 7 -- the one agent-titled `Season 1` -- stores
        # `title = ''` and `title_sort = 'Season 1'`, while its unmatched season 11 stores
        # both empty, and every season and episode of a fresh 1.42.2 copy stores `''`/`''`.
        # So the generated DISPLAY title is derived from the index in both cases, and what
        # an agent supplied for season 7 is the stored SORT title.
        #
        # The consequence is the clause that used to be here. `stored == title` treated a
        # stored sort title equal to the generated title as no sort title at all -- which is
        # exactly plexlab's season 7, the only row in either reference that can show the
        # difference. Measured there: `titleSort=Season 1` answers season 7, `titleSort=`
        # answers season 11 alone, and `titleSort>>=Season 0` answers 7. rex answered
        # NOTHING to the first and third and wrongly included the row in the second, under
        # every operator -- one clause with three visible symptoms.
        #
        # A row that stores no sort title at all still answers `""`, which is every season
        # rex synthesises for itself, so cycle 18's whole measured set is unchanged.
        if not row.get("titleSort"):
            return ""
    return row.get("titleSort") or title


# Cycle 27, measured on plexlab (1.43.4) and the 1.42.2 reference and recorded through a
# real client: every PERSON tag carries a `tagKey` -- a 24-hex plex.tv person id -- on a
# detail row's `Director`, `Writer`, `Role` and `Producer`, and on the rows of
# `/library/tags?type=4|5|6|7`. `Genre`, `Country` and `Studio` carry none, and a
# `Collection` carries a `guid` instead.
#
# It is not decoration. The web client's Cast & Crew link builder reads
# `/library/people/${tag.tagKey}` for any server that is not plex.tv's cloud, so a tag
# without one links every person tile to `/library/people/undefined`.
#
# rex has no source for plex.tv's own person ids, so this MINTS one: the first 24
# hex digits of the sha1 of the person's name, which is deterministic, stable across
# restarts and the same length and alphabet as the real thing. That is a substitute for
# a value, not a measurement of it -- the SHAPE is measured, the value is invented, and
# `/library/people/{key}` resolves the minted form exactly as it resolves the numeric id
# (both are 200s on a real PMS). It is the same shape as the `plex://` guids rex
# already mints from TMDB ids.
PERSON_TAG_ELEMENTS = ("Director", "Writer", "Role", "Producer")


def person_tag_key(name):
    """The 24-hex `tagKey` rex mints for a person. See above."""
    # "plexsim/" is the pre-rename namespace, frozen: changing it re-mints every id it seeds.
    return hashlib.sha1(("plexsim-person:%s" % name).encode("utf-8")).hexdigest()[:24]


SPECIALS_TITLE = "Specials"


def set_season_count(show, seasons):
    """Give a show its `seasonCount`, or take it away, from its season rows.

    Cycle 79, measured on the 1.42.2 reference: exactly the four of its 509 shows that hold a
    season 0 carry `seasonCount`, and it is the count WITHOUT the specials -- Bluey's
    `childCount="4" seasonCount="3"`, MythBusters' 17 and 16 -- sitting straight after
    `childCount` on the detail row, the section listing and every hub. A show with no
    season 0 carries none. python-plexapi's `Show.seasonCount` reads the attribute and
    falls back to `childCount`, so without it every show with specials counted one
    season too many.
    """
    specials = sum(1 for season in seasons if season.get("index") == 0)
    show.pop("seasonCount", None)
    if not specials:
        return
    count = len(seasons) - specials
    ordered = list(show.items())
    show.clear()
    for key, value in ordered:
        show[key] = value
        if key == "childCount":
            show["seasonCount"] = count
    show.setdefault("seasonCount", count)


def normalize_rows(rows):
    """The row rules a real PMS follows, applied to rows however they were built.

    Run at the end of a build and again when a catalog LOADS, because catalogs
    built before these were measured carry the old fields baked in.

    * `titleSort` is emitted only when it differs from `title` -- 0 of 3,856 movies
      and 0 of 599 shows on the production server carry an equal one -- and it is the stripped title.
      rex sent the unstripped title on every movie, so "The Simpsons" filed
      under T, and every episode carried its SHOW's name.
    * `originalTitle` is the original-language title, present only when it differs
      (`Twelve Monkeys` for `12 Monkeys`). rex put the media FILENAME there, and
      the client prints it on the preplay page, in the window title and as the
      OS media-session artist line.
    * No `Field` lock and no empty `Label` by default: a real row lists a locked
      field only when one is locked, and a `Field locked thumb` on every row told
      the Edit dialog every poster was pinned.
    * A movie has no `Location`; neither real server sends one.
    * `rating` is the CRITIC rating (Rotten Tomatoes on the production server), absent when there is
      none -- the lab's own TMDB-matched movies carry only `audienceRating`.
      rex copied the TMDB vote into both.
    * A season with a year of its own carries it, a `titleSort` EQUAL to its title
      (the one row type that does) and no `parentYear`; one without has
      `parentYear` only. A zero `skipCount` is omitted. An episode has no
      `chapterSource` without chapters. A show has `index: 1`, and its `Location`
      is `{path}` with no id -- the id belongs to a SECTION's location.
    * No row names a theme -- `theme`, `parentTheme`, `grandparentTheme` -- because
      rex has no theme audio and its theme endpoint 404s. Measured on the production server: a show
      without a theme carries none of the three, on itself or its children, and the
      web client asks for whichever it finds when theme music is on.
    """
    by_key = {str(row.get("ratingKey")): row for row in rows}
    # Cycle 16, measured on plexlab and the 1.42.2 reference: a row that borrows its
    # background carries the OWNER's path, not one of its own. A catalog built before that
    # was measured carries a re-keyed copy on every season and episode, and the `_art_url`
    # hints the builder used are stripped from the stored file -- but the VERSION is a hash
    # of the source image, so a season whose art carries its show's version is showing
    # its show's picture. Rewrite it to the show's path, then hand it down: an
    # episode's `art` is its season's resolved value, which is the show's when the
    # season borrows.
    # Cycle 44: whether a season STORED a sort title of its own has to be read before
    # any pass below rewrites it -- the derived-equals rule strips a `titleSort` equal to
    # the title, and by the season branch there is nothing left to read.
    stored_sort_titles = {id(row): bool(row.get("titleSort")) for row in rows}
    for row in rows:
        if row.get("type") != "season":
            continue
        show = by_key.get(str(row.get("parentRatingKey")))
        art, parent_art = str(row.get("art") or ""), str((show or {}).get("art") or "")
        if parent_art and art.rsplit("/", 1)[-1] == parent_art.rsplit("/", 1)[-1]:
            row["art"] = parent_art
    for row in rows:
        for element in PERSON_TAG_ELEMENTS:
            for tag in row.get(element) or []:
                if isinstance(tag, dict) and tag.get("tag") and not tag.get("tagKey"):
                    tag["tagKey"] = person_tag_key(tag["tag"])
    for row in rows:
        if row.get("type") != "episode":
            continue
        season = by_key.get(str(row.get("parentRatingKey")))
        if season and season.get("art"):
            row["art"] = season["art"]
        # Cycle 20, held out against 9,293 real episodes on both references: a season
        # hands its poster down only when the poster is its OWN. The 1.42.2 reference's 654
        # seasons with a thumb of their own give every episode a `parentThumb` and its 27
        # whose thumb is the SHOW's give none, with no exception on either side; plexlab
        # agrees on both of its seasons. `art` above is the opposite -- handed down
        # borrowed or not -- so this is a property of the FIELD and not of the row. The
        # test is the stored path, which is the only thing the response exposes: cycle 16
        # measured that a borrowing row carries the owner's path verbatim, so a `thumb`
        # naming another ratingKey IS the borrowed case.
        if season is not None:
            thumb = str(season.get("thumb") or "")
            if not thumb.startswith("/library/metadata/%s/" % season.get("ratingKey")):
                row.pop("parentThumb", None)
    for row in rows:
        if row.get("type") not in ("show", "season"):
            continue
        # Cycle 19, measured on the 1.42.2 reference (300 of 300 shows, 300 of 300 seasons)
        # and plexlab (both its seasons): a row that HAS children names its child list, not
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
    # Cycle 79, measured on the 1.42.2 reference across all 509 shows of its show library:
    # a real PMS titles season 0 `Specials` -- never `Season 0` -- and its episodes carry
    # that as `parentTitle`. `build` titled every season `Season %d`, so a catalog holding
    # an `S00E01` release drew a "Season 0" block on every client. Corrected at LOAD as
    # well, because catalogs built before this carry the old title.
    seasons_of = {}
    for row in rows:
        if row.get("type") != "season":
            continue
        seasons_of.setdefault(str(row.get("parentRatingKey")), []).append(row)
        if row.get("index") == 0 and row.get("title") in (None, "", "Season 0"):
            row["title"] = SPECIALS_TITLE
    for row in rows:
        if row.get("type") == "episode" and row.get("parentTitle") == "Season 0":
            season_row = by_key.get(str(row.get("parentRatingKey")))
            if season_row is not None and season_row.get("index") == 0:
                row["parentTitle"] = season_row.get("title") or SPECIALS_TITLE
        elif row.get("type") == "show":
            set_season_count(row, seasons_of.get(str(row.get("ratingKey"))) or [])
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
            # `rating` is the CRITIC rating and rex copies the TMDB vote into both,
            # so it is dropped -- extended to EPISODE here because no episode on either
            # reference carries one (0 of 20 One Piece episodes on the 1.42.2 reference, 0
            # of 3 on plexlab).
            row.pop("rating", None)
        if kind == "episode" and not row.get("contentRating"):
            # Measured on plexlab (1.43.4) and the 1.42.2 reference: an episode carries its
            # SHOW's `contentRating` -- 6 of 6 on the lab, the three unmatched ones
            # included although they have no agent metadata at all, and 18 of 18 across
            # three of the 1.42.2 reference's shows (TV-14, TV-Y, TV-PG). A SEASON carries
            # none on either server, and the write side agrees: on a writable copy, editing
            # the show's rating moved it on the episodes that had none of their own and a
            # show unmatch removed it from all of them. rex left an episode with no
            # certificate at all.
            parent = by_key.get(str(row.get("grandparentRatingKey")))
            if parent and parent.get("contentRating"):
                row["contentRating"] = parent["contentRating"]
        if kind == "episode" and row.get("audienceRating") is not None:
            # Measured on plexlab (1.43.4) and the 1.42.2 reference: an EPISODE with an
            # `audienceRating` carries `audienceRatingImage` and a `Rating` element as
            # well -- 60 of 60 One Piece episodes and 3 of 3 on the lab, with no
            # exception, while the unmatched episodes with no rating carry none of the
            # three and a SEASON never carries any of them (0 of 12 sampled across both
            # servers). rex built these for a show and a movie only, so its own
            # episode rows contradicted its own movie rows and no episode page or
            # episode listing row drew a rating badge. A real PMS sends two agents'
            # rows here (imdb and themoviedb); rex has one agent, so it sends the
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
            # Cycle 44, corrected at LOAD like the other pipeline-produced row errors
            # here: a season built by `metadata.py` before this cycle carries TMDB's
            # season air-date year in its own `year` column. That is the shape 1 of
            # the 1.42.2 reference's 742 seasons has and 689 do not, and carrying it takes
            # `parentYear` away, because the rule below reads "has a year of its own" off
            # that column. A real Plex series agent gives a season the SHOW's year as
            # `parentYear`. A season's own year stays legal -- it is simply not what
            # this builder produces, and this builder wrote every season rex holds.
            if row.get("year") is not None and not stored_sort_titles.get(id(row)):
                # The discriminator is the STORED SORT TITLE, and it is measured on both
                # references: every season that carries a year of its own also stores
                # one. plexlab's season 7 is `Season 1` with `titleSort: "Season 1"` and
                # `year: 2002`; the 1.42.2 reference's single year-carrying season of 742
                # stores `Egghead`, its agent's arc name. A season with a year and NO
                # stored sort title is a shape neither reference has -- and it is exactly
                # what `metadata.py` wrote before this cycle, so this corrects the
                # builder's own rows without touching a season an agent really identified.
                show_row = by_key.get(str(row.get("parentRatingKey")))
                row.pop("year", None)
                if show_row is not None and show_row.get("year") is not None:
                    row["parentYear"] = show_row["year"]
            if row.get("year"):
                # Cycle 20, held out against the 1.42.2 reference's 741 real seasons: the
                # `year` does NOT bring a `titleSort` with it. 740 carry none, and the only
                # one that does -- the only one with a `year` -- carries `Egghead`, the arc
                # name its agent stored, where the row DISPLAYS `Season 22`. Cycle 7 read
                # plexlab's season 7 (`Season 1`, agent-supplied and therefore stored) as
                # "a year means titleSort == title"; the year is a coincidence of a season
                # an agent really identified. rex generates every season title, so the
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
            # Measured on plexlab (1.43.4) and the 1.42.2 reference: an episode's
            # parentGuid is the SEASON's guid. A catalog built before that was measured
            # carries the show's guid in both, so the season's own guid is looked up here
            # at load.
            season_row = by_key.get(str(row.get("parentRatingKey")))
            if season_row and season_row.get("guid"):
                row["parentGuid"] = season_row["guid"]
        if kind == "episode":
            if not row.get("Chapter"):
                row.pop("chapterSource", None)
    # Cycle 20: an artwork path's version is the OWNING row's `updatedAt` -- 738 of 738
    # owned paths across the two references. Every reference to a row's artwork follows
    # it, borrowed or not, so the rewrite is keyed on the ratingKey INSIDE the path
    # rather than on the row carrying it: a season's `parentThumb` and an episode's
    # `grandparentArt` name the show's path and have to move with it. Catalogs written
    # before this carry the old content hash, which is why it runs at LOAD as well as
    # in `_stamp_artwork`.
    stamps = {str(row.get("ratingKey")): row.get("updatedAt") or row.get("addedAt") or 1
              for row in rows}
    for row in rows:
        for field in _ARTWORK_FIELDS:
            value = str(row.get(field) or "")
            parts = value.split("/")
            if not value.startswith("/library/metadata/") or len(parts) != 6:
                continue
            if parts[3] in stamps:
                row[field] = "/".join(parts[:5] + [str(stamps[parts[3]])])
    return rows


# The palette a row without measured artwork colours carries; the values are only used
# for background tinting.
DEFAULT_ULTRABLUR = {
    "topLeft": "1a1a1e", "topRight": "24242a",
    "bottomRight": "15151a", "bottomLeft": "1f1f26",
}


def _slug(title):
    out = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return out or "item"


def _stamp_artwork(rows):
    """Version every artwork URL so a client cannot serve a stale poster.

    A real PMS emits `/library/metadata/<rk>/thumb/<version>` -- the production server uses a
    timestamp. rex emitted a constant `/thumb/1`, so when a ratingKey was
    reused by a later catalog build the URL stayed identical while the image
    behind it changed: The Simpsons at rk=20001 rendered with The Mentalist's
    poster, cached by the client from a previous build.

    Cycle 20, measured over every owned artwork path of both references: the version
    is the row's `updatedAt`, exactly -- 726 of 726 on the 1.42.2 reference and 12 of 12 on plexlab, `thumb`
    and `art` alike. The copy shows the mechanism from the write side: writing
    `updatedAt` through the section save path re-versions the poster onto the new stamp
    in the same call. A hash of the source URL was a defensible version and not the
    measured one, and it is a column a client can compare with the row's own.
    """
    for row in rows:
        for field in ("thumb", "art"):
            if not row.get(field):
                continue
            version = row.get("updatedAt") or row.get("addedAt") or 1
            row[field] = "/library/metadata/%s/%s/%s" % (row["ratingKey"], field, version)

    # Parent pointers must match the parent's stamped URL, not the old constant.
    by_key = {r["ratingKey"]: r for r in rows}
    # Cycle 16, measured on plexlab and the 1.42.2 reference: a row that owns no background
    # of its own carries its SHOW's art PATH verbatim -- plexlab's season 7 and episode 8
    # both carry `/library/metadata/6/art/<v>` -- and `/library/metadata/7/art` is a 404
    # there, while the 1.42.2 reference's season 11114, which DOES have art of its own,
    # carries its own path and answers 200. Stamping re-keyed every borrowed URL to the row
    # holding it, so three rows claimed the same image under three paths. This runs before
    # the episode fix-up below, which then inherits the corrected value.
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
                "title": SPECIALS_TITLE if season_number == 0 else "Season %d" % season_number,
                "index": season_number,
                # A season carries a `year` only when it has an air date of its OWN
                # -- then `titleSort` too, equal to its title -- and `parentYear`
                # otherwise. Measured on 74 seasons on the production server; never the
                # show's year borrowed. An older note tied the season list rendering to
                # `year`; that was the paging bug.
                "parentIndex": 1,
                # Cycle 44: the pipeline no longer supplies `_season_year` -- see
                # `metadata.py`. A season's own year stays possible (the 1.42.2 reference
                # has exactly one such row of 742) and `normalize_rows` still applies the
                # year / parentYear rule below; what changed is that a Plex series agent
                # does not give a season one, so rex's never should either.
                "year": leaf.get("_season_year"),
                # The season's EXTERNAL ids. Both references carry them on a season
                # detail row; this pipeline fetched and discarded them until cycle 44.
                "Guid": leaf.get("_season_guids"),
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
        else:
            named = _fill_season(season, leaf)
            if named:
                # The season's earlier episodes took the show's guid as their parent's.
                for row in rows:
                    if row.get("type") == "episode" and row.get("parentRatingKey") == season["ratingKey"]:
                        row["parentGuid"] = named

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
        # Measured on plexlab (1.43.4) and the 1.42.2 reference: an episode's `parentGuid`
        # is the SEASON's own guid (`plex://season/...`), not the show's. rex copied the
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
                    "_season", "_episode", "_season_guid", "_season_guids",
                    "_season_poster",
                    "_show_external_guids",
                    "_season_year", "_season_air", "_season_summary"):
            row.pop(key, None)
    return rows


def _fill_season(season, leaf):
    """A season is made from its first leaf. A later leaf that identifies a season the first
    left unidentified (its TMDB season was not had) gives it that identity, and one carrying
    artwork gives artwork to a season with none: the production rex kept 186 seasons of 131 shows on
    the blank tile under a show with a poster (2026-10-12), because nothing after the first
    leaf was ever read. Returns the guid the season took, if it took one."""
    def present(value):
        return value is not None and value != ""
    guid = leaf.get("_season_guid") if not present(season.get("guid")) else None
    if present(guid):
        for field, hint in (("guid", "_season_guid"), ("year", "_season_year"), ("Guid", "_season_guids"),
                            ("summary", "_season_summary"), ("_poster_url", "_season_poster")):
            if present(leaf.get(hint)):
                season[field] = leaf[hint]
    for field, hint in (("_poster_url", "_show_poster"), ("_art_url", "_show_art")):
        if not present(season.get(field)) and present(leaf.get(hint)):
            season[field] = leaf[hint]
    return guid if present(guid) else None
