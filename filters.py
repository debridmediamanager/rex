#!/usr/bin/env python3
"""Plex's filter query language, as the client puts it on the wire.

The thing that makes this unguessable is that **the operator is part of the KEY,
not the value**. The client holds operators with a trailing `=` (`>>=`, `<=`,
`==`) and then strips that last character before appending its own separator
(Plex Web 4.147.1, `main-*.js` module 14056):

    const t = e.operator.slice(0, -1)
    r.push(`${e.field}${encodeURIComponent(t)}=${e.value}`)

So "year is after 2000" goes out as `year%3E%3E=2000`, and the server reads a
parameter **named** `year>>` whose value is `2000`. There is no `year>=2000`
anywhere on the wire. A server that iterates its query parameters looking for
names it knows therefore sees nothing it recognises and applies no constraint at
all -- which is what plexsim did, so every filter returned the whole library
while looking like it worked.

What each (type, operator) pair means was measured against two real servers; the
operator lists per type are not hard-coded here, they come from the `FieldType`
block of the captured `Meta` that plexsim already serves, so the engine can only
honour what it advertises.

Three more shapes, all measured:

  * a REPEATED key is AND (`?genre=6&genre=23` is the intersection), while a
    COMMA-separated value is OR (`?genre=6,27` is the union);
  * `push=1` / `pop=1` are parentheses and `and=1` / `or=1` are the separator
    between siblings, with AND binding tighter than OR, exactly as in SQL;
  * a date value may be relative (`-30d`), an absolute epoch, an ISO date or an
    ISO datetime. The suffixes are `s m h d w mon y` -- `m` is MINUTES, and
    `mon` is the only month form. An unsigned value means the past and a leading
    `+` means the future.

The same grammar is what `/library/sections/{id}/query/parse` reads back, which
is how the Edit Filters dialog reopens a smart filter it saved. `describe()` and
`document()` render a parsed query as that endpoint's `Query` document.
"""

import json
import os
import re
import time

from hierarchy import fold_query, fold_case

# The operator a key's trailing punctuation means, keyed by what survives the
# client's `slice(0, -1)`. The right-hand side is the operator as the `Meta`
# FieldType block advertises it, which is also what /query/parse echoes back.
OPERATOR_SUFFIXES = [
    ("!=", "!=="),
    (">>", ">>="),
    ("<<", "<<="),
    ("!", "!="),
    ("=", "=="),
    (">", ">="),
    ("<", "<="),
    ("", "="),
]

# Grouping, ordering and limiting tokens, which are not filters. Deliberately
# short: a name in here can never be filtered on, and `title` IS an advertised
# field as well as a creation parameter -- listing it here made `?title=Sin`
# match the whole library, which is the exact failure this module exists to fix.
# `having` is NOT here on purpose: measured, a real PMS reports it as an unknown
# FIELD at /query/parse, so it has to reach the field lookup and be rejected
# there rather than be quietly swallowed.
# `sourceType` is here for the same reason: measured on both references, the parser
# reports no Rule and no Error for it and a listing filtered on it keeps every row. It
# is not a no-op, though -- see `source_order` below and the 500 its value can raise.
STRUCTURAL = {"push", "pop", "and", "or", "sort", "type", "group", "limit", "sourceType"}

# Request options a real PMS does not treat as fields at all. Measured at
# /query/parse: `includeGuids`, `includeCollections`, `includeMeta` and
# `excludeFields` are ignored silently, while `checkFiles`, `asyncCheckFiles`,
# `nocache` and `having` come back as unknown-field errors. So the rule is a
# prefix rule, not a list of every option name.
IGNORED_PREFIXES = ("X-Plex-", "include", "exclude")

# The fields whose values are rows in the server's own tag table. Two things
# follow from a field being one, both measured: only `=` and `!=` are valid KEYS
# on it (`genre>>=x` comes back as an unknown FIELD, `genre>>`, not as a bad
# operator), and /query/parse resolves a numeric value to the tag's name as
# `valueTitle`. `contentRating`, `videoCodec`, `audioCodec`, `subtitleCodec`,
# `audioLayout` and `location` are declared `tag` in the Meta and are deliberately
# NOT in this set: measured, they accept every operator key and get no
# `valueTitle`, because they are enum columns rather than tag rows.
TAG_TABLE_FIELDS = {"genre", "collection", "director", "writer", "producer",
                    "actor", "country", "label", "network", "similar",
                    # Cycle 54: seventeen more, from the binary's parameter names.
                    "mood", "style", "format", "subformat", "tag", "make", "model", "lens",
                    "aperture", "exposure", "iso", "device", "autotag", "place", "channel",
                    "mediaProcessingTarget", "sharedWith"}

# A string field has no ordering, so `<<` and `>>` are unknown keys on it and it
# keeps exactly the six the FieldType block advertises. Every OTHER type takes
# any operator key: `resolution>>=720` parses on a real PMS even though
# `resolution` advertises only `=` -- it simply then matches nothing.
STRING_OPERATORS = {"=", "!=", "==", "!==", "<=", ">="}
TAG_OPERATORS = {"=", "!="}

# ...except that the restriction is the advertised FieldType's, not the python type's.
# Cycle 14, on kids and zen: `titleSort>>=ZZZZ` answers 0 and 1 row, `summary>>=A` 4
# and 1493, `tagline>>=A` 2 and 977, `originalTitle>>=M` 0 and 393 -- real orderings --
# while the Meta-ADVERTISED strings `title` and `studio` answer every row to the same
# key, which is what an ignored unknown field does. These four appear in no Meta.
UNLISTED_STRING_FIELDS = {"titleSort", "summary", "tagline", "originalTitle",
                          # Cycle 20: the artwork columns parse the same way, under
                          # every operator key including `>>` and `<<`.
                          "theme", "banner", "art", "thumb", "slug"}

# The `resolution` values a real PMS accepts, probed value by value against a
# 6000-item production library. It is a closed enum, not a continuum: `240`,
# `360`, `400`, `1081`, `1440`, `2160`, `3k`, `9k`, `10k`, `SD`, `hd` and `1080p`
# are each a 500, and so is an empty value. An accepted value that no row has is
# a plain empty 200.
RESOLUTIONS = {"sd", "480", "576", "720", "1080", "2k", "2.7k", "4k", "8k", "16k"}

# How each of those is LABELLED on the `/library/sections/{id}/resolution` axis:
# a bare number gains a `p`, a `k` form uppercases, and `sd` is `SD`.
RESOLUTION_TITLES = {"sd": "SD", "480": "480p", "576": "576p", "720": "720p",
                     "1080": "1080p", "2k": "2K", "2.7k": "2.7K", "4k": "4K",
                     "8k": "8K", "16k": "16K"}

# A `sort=` term's colon-separated tail holds direction and modifiers in any
# order; anything here is not a `random` seed.
SORT_MODIFIERS = {"asc", "desc", "nocollate"}

_RELATIVE = re.compile(r"^([-+]?)(\d+)(s|m|h|d|w|mon|y|days|weeks)$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
            "mon": 2592000, "y": 31536000, "days": 86400, "weeks": 604800}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 639-2/T -> 639-1, the same table `mediainfo` names a track's language with. A stream
# carries both spellings and a client may filter with either; cycle 20 measured that the
# two answer identically under every operator, so both sides fold to the tag here.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages.json")) as _handle:
    _LANGUAGE_TAGS = {code: tag for code, (_name, tag) in json.load(_handle).items()}


def language_code(value):
    """One canonical spelling of a language code."""
    text = str(value)
    return _LANGUAGE_TAGS.get(text, text)
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


class FilterError(Exception):
    """A query a real PMS refuses. `status` is the code it answers with."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def split_key(key):
    """`year>>` -> ("year", ">>="). Raises FilterError(400) for a malformed key.

    Measured: `?year%3E2010` -- a comparison character with no `=` separating it
    from the value, so the whole thing arrives as one parameter name -- is a 400
    on a real PMS, not an ignored parameter.
    """
    for suffix, operator in OPERATOR_SUFFIXES:
        if suffix and key.endswith(suffix):
            name = key[: -len(suffix)]
            break
    else:
        name, operator = key, "="
    if any(character in name for character in "<>!="):
        # Cycle 22, measured on zen and kids: a key whose NAME still carries one of
        # these after the suffix comes off -- `collection==`, `year>=`, `title!!` --
        # is an UNKNOWN FIELD, not a refusal. A listing ignores it and answers every
        # row, and `/query/parse` reports it verbatim as an `Error` of type `field`.
        # The 400 this used to raise belongs to a DIFFERENT shape, which the pair
        # parser still refuses: a pair with no `=` at all (`?year%3E2010`).
        # Returning the whole key as the field name is what makes `field_type` answer
        # None and both callers report it as unknown.
        return key, "="
    return name, operator


def split_scope(name):
    """`episode.unwatched` -> ("episode", "unwatched"); `year` -> ("", "year").

    A show library advertises its fields PREFIXED with the type they belong to
    (`show.genre`, `episode.title`), and the client sends the advertised key
    verbatim. The prefix is a scope, not part of the field name: measured,
    /query/parse echoes `{"type": "episode", "field": "unwatched"}` for
    `episode.unwatched`, and it honours a prefix from any section -- `show.title`
    parses against a MOVIE library too.

    plexsim looked the incoming name up after stripping the prefix, in a map
    keyed by the PREFIXED names, so nothing a show library advertised could ever
    match and every filter on a show grid silently returned everything.
    """
    scope, _, bare = name.rpartition(".")
    return scope, (bare or name)


# Cycle 20, measured on both references: `guid` takes exactly two operator KEYS,
# `=` and `<=`, and every other one is reported as an unknown FIELD with the operator
# still attached (`guid!`, `guid>`) and ignored by a listing. It is the only field whose
# key set is neither the tag pair nor the string six.
GUID_OPERATORS = {"=", "<="}


def operator_allowed(kind, field, operator):
    """Whether this (field, operator) pair is a key a real PMS recognises.

    Not the same question as whether the FieldType block advertises the operator:
    `resolution>>=720` parses although `resolution` advertises only `=`. What is
    closed is the two sets measured above -- tag-table fields and string fields.
    """
    if field in TAG_TABLE_FIELDS:
        return operator in TAG_OPERATORS
    if field == "guid":
        return operator in GUID_OPERATORS
    if kind == "string":
        return field in UNLISTED_STRING_FIELDS or operator in STRING_OPERATORS
    return True


def as_epoch(value, now=None):
    """A date filter's value as an epoch. Raises FilterError(500) if unparseable.

    Measured: an unrecognised relative suffix is a 500 on a real PMS, and it is
    case sensitive -- `-1mo`, `-1M`, `-1min` and `-1week` all fail where `-1mon`
    and `-1w` work.
    """
    now = now if now is not None else time.time()
    value = str(value).strip()
    if "." in value and value.replace(".", "", 1).lstrip("-").isdigit():
        # Cycle 15: a fractional epoch is a 500 on both references -- `addedAt=1.5`,
        # `updatedAt=1.5` and the other three alike.
        raise FilterError(500, "fractional date %r" % value)
    match = _RELATIVE.match(value)
    if match:
        sign, amount, unit = match.groups()
        seconds = int(amount) * _SECONDS[unit]
        # No sign means the past, which is the direction the UI's "in the last"
        # control means; a leading `+` is the future.
        return now + seconds if sign == "+" else now - seconds
    if _ISO_DATETIME.match(value):
        return _to_epoch(value[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
    if _ISO_DATE.match(value):
        return _to_epoch(value, "%Y-%m-%d")
    try:
        return float(value)
    except ValueError:
        raise FilterError(500, "unparseable date %r" % value)


def _to_epoch(text, form):
    try:
        return time.mktime(time.strptime(text, form))
    except ValueError:
        raise FilterError(500, "unparseable date %r" % text)


# Cycle 15, measured on kids (1.43.4) and zen (1.42.2): the two per-user-state
# columns that refuse a FRACTIONAL value. The five date columns refuse one through
# `as_epoch`; `year`, `duration`, `index`, `skipCount`, `ratingCount`, `mediaCount`,
# `absoluteIndex` and `userRating` take it and answer no rows.
FRACTIONAL_REFUSED = {"viewCount", "viewOffset"}

# The two columns whose ABSENCE is read as a value: cycle 20, measured on both
# references. A row with no stored view state matches `=0` and every `<=`/`<<=`
# whatever the value, and nothing else. `skipCount` looks identical on a row and is
# not in the family.
NULL_IS_ZERO_FIELDS = {"viewCount", "viewOffset"}
# Cycle 54, measured on zen (1.42.2) and kids (1.43.4): the three per-user DATE columns,
# read from the same settings row, behave the same way -- `lastRatedAt=0`, `<<=1` and
# `<<=2020-01-01` are every unrated row, `!=5` and `>>=0` none of them. `viewUpdatedAt`
# is not in the family: its nulls match nothing.
NULL_IS_ZERO_DATES = {"lastViewedAt", "lastRatedAt", "lastSkippedAt"}

# The boolean-looking column whose OPERATOR a real PMS ignores: cycle 20, measured on
# both references. Only the value decides, and only the literal `1` is true.
OPERATOR_BLIND_BOOLEANS = {"unmatched"}


def as_number(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise FilterError(500, "unparseable number %r" % value)


def _values(actual):
    """A row's value for an exact-match field, as a list of comparable strings."""
    entries = actual if isinstance(actual, (list, tuple)) else [actual]
    out = []
    for entry in entries:
        if entry is None or entry == "":
            continue
        if isinstance(entry, dict):
            # A tag matches by its id OR by its name -- measured: both `?genre=6`
            # and `?genre=Animation` return the same three rows.
            out.extend(str(v) for v in (entry.get("id"), entry.get("tag"))
                       if v is not None)
        else:
            out.append(str(entry))
    return out


# String fields whose bare `=` is equality, not containment (measured).
EXACT_STRING_FIELDS = {"titleSort", "summary", "tagline"}


def _word_prefixes(wanted, text, sort=None):
    """`title=` on a listing: each query WORD begins a distinct title word, in order.

    Measured on two servers, and not the substring match every other string field
    gets: `title=Fire` finds Firefly and `title=ire` does not, `Tears Steel` and
    `b b b` find their titles where `steel of` and `b b b b` do not, and `,` and `.`
    in the query are ignored. The query splits on SINGLE spaces, so an empty word
    begins any word: `title=` is every row, `title=%20` every title of two words or
    more, and `Tears%20%20Steel` still finds Tears of Steel. Only `=` works this
    way; `title!=ears` excludes Tears of Steel, so `!=` still reads a substring.

    A row matches if EITHER of two passes does, which is cycle 15's correction of
    cycle 14. Cycle 14 measured that `title=Bogota` finds "Bogotá: City of the Lost"
    while `title=Bogota City` finds nothing, and read the SPACE as the switch that
    turns folding off. It is not: `title=Adoravel Trapalhao` finds "Adorável
    Trapalhão" and `title=Sex and Lucia`, `title=and Lucia` and `title=Dire a Lamine`
    all find their accented rows. What breaks the Bogotá query is the COLON.

    Measured over 52 queries on zen (1.42.2) -- 27 to build the model and 25 more on
    rows it had never seen, which it reproduced exactly:

    * The RAW pass is the old matcher with no folding at all: each query word begins
      a title word, in order, on the raw spellings. `title=Bogot City` matches
      because `Bogot` is a prefix of `Bogotá` before any folding.
    * The FOLDED pass folds both sides, splits the query on SINGLE spaces and the
      title on whitespace, and then -- this is the part nothing predicted -- every
      query term but the LAST must equal a whole title word, not merely begin one.
      The last may begin one. So `Bogota` alone matches (it is the last term, and
      `bogota` begins `bogota:`), `Bogota City` does not (`bogota` is not the whole
      word `bogota:`), and `Bogota: City` does. `Adoravel Trapalhao` matches because
      both folded terms are whole words. A query holding an empty term -- a trailing
      or doubled space -- fails this pass, which is what cycle 14's trailing-space
      pair was really showing.

    Punctuation rides along in a folded term, so `tournee` does NOT find
    "Compilation Pyroman (tournée 2018-2022)" -- the word there is `(tournee` -- while
    `Pyroman (tournee` does. That case is the clearest proof the folded pass compares
    whitespace words rather than an alphanumeric tokenisation.

    `!=` is a substring and does not fold at all (`title!=Amelie` excludes nothing;
    `title!=Amelie` with the accent excludes exactly its one row), which is why this
    is the only place the fold goes.
    """
    # Cycle 31, measured on kids and zen, which agree: a COMMA splits the query into
    # ALTERNATIVES and a row matches if ANY of them does, while a DOT is a literal
    # character of the word.
    #
    #   kids `title=Steel,,` -> all 5 rows, `title=of,` -> all 5, `title=Tears, of` -> 1
    #   zen  `title=Love,` -> all 3,887, `title=Love` -> 59, `title=Actually,Love` -> 59
    #        (Love Actually is already in the 59, so the union is 59)
    #   kids `title=Steel.` -> 0, `title=Tears of Steel.` -> 0     zen `Actually.` -> 0
    #
    # An empty alternative matches every row -- which is the recorded `title=` rule -- so a
    # trailing comma answers the whole library. plexsim STRIPPED both characters from the
    # query, so `Steel,` found one row where a real PMS answers all of them and `Steel.`
    # found one where a real PMS answers none. It is the filter bar's own text box, so a
    # typed comma or a trailing period silently gave a different row set.
    # Cycle 32, measured on kids and zen: the RAW pass reads the title and the FOLDED
    # pass reads the row's stored SORT title. kids' episode 8 is "The Train Job" with a
    # sort title of "Train Job": `title=The Train` is 1 row (the raw pass), `Tráin Job` is
    # 1 (the folded pass over the sort title), and `The Tráin`, `Thé Train` and
    # `Thé Tráin Jób` are each **0** -- a folded query carrying the article cannot match,
    # because the article is not in the string the folded pass reads. zen's season 11046,
    # whose generated title is `Season 22` and whose agent sort title is `Egghead`, is the
    # witness with no article at all: `title=Egghead` and `Egghéad` each answer it and
    # `title=Season 22` answers nothing.
    #
    # Rows with no article match either way, which is why thirteen cycles of this matcher
    # never saw it. plexsim folded the TITLE, so it answered rows a real PMS does not
    # (`Thé Simpsons`) and missed every row findable only by its sort title -- including,
    # on a real library, every generated-title season whose agent gave it one.
    return (_raw_word_prefixes(wanted, text)
            or _folded_word_run(wanted, sort if sort is not None else text))


def _original_phrase(wanted, text):
    """`title=` against the row's ORIGINAL title: a consecutive run of whole words.

    Cycle 31, measured on zen. Unfolded on both sides, split on whitespace: every term but
    the last must EQUAL a title word and the last may begin one, and the run has to be
    adjacent. See `compare`'s `title` branch for the measurements.
    """
    terms = [t for t in str(wanted).lower().split(" ") if t]
    words = str(text).lower().split()
    if not terms or not words:
        return False
    for start in range(len(words) - len(terms) + 1):
        run = words[start:start + len(terms)]
        if all(run[i] == terms[i] for i in range(len(terms) - 1)) \
                and run[-1].startswith(terms[-1]):
            return True
    return False


def _raw_word_prefixes(wanted, text):
    """The unfolded pass: each query word BEGINS a title word, in order."""
    # Cycle 31: neither character is stripped -- the comma is handled one level up as an
    # alternation and the dot is a literal part of the word.
    words = wanted.lower().split(" ")
    title_words = text.lower().split()
    position = 0
    for word in words:
        while position < len(title_words) and not title_words[position].startswith(word):
            position += 1
        if position == len(title_words):
            return False
        position += 1
    return True


def _folded_word_run(wanted, text):
    """The folded pass: whole words but the last, which may be a prefix.

    An empty term fails outright -- that is `title=Bogota ` and `title=Bogota:  City`,
    both of which a real PMS answers nothing to while the raw pass also misses them.
    """
    # Cycle 32, swept on kids over 2,098 codepoints and on zen, which agree, then held out
    # on both writable copies: the QUERY is folded by the same 100-character table a sort
    # title is derived with and then case-folded, while the STORED sort title is only
    # case-folded. A locked sort title `\u00c9gg\u0119head Zo\u017fa` answers `Zosa` (the
    # case fold takes the long s) and answers neither `Egghead` nor `Egg\u0119head` -- the
    # stored side is not run through the table again. That pair is the whole of hunt B's
    # 102-character query table: the 100 plus `\u017f` and `\u212a`, which only a case fold
    # reaches.
    #
    # plexsim decomposed both sides with NFD, which folds 134 characters a real PMS does
    # not (`Sint\u0119l`, `\u0100ra`) and misses the two the case fold does.
    words = fold_query(wanted).split(" ")
    if any(word == "" for word in words):
        return False
    title_words = fold_case(text).split()
    position = 0
    for index, word in enumerate(words):
        last = index == len(words) - 1
        while position < len(title_words) and not (
                title_words[position].startswith(word) if last
                else title_words[position] == word):
            position += 1
        if position == len(title_words):
            return False
        position += 1
    return True


# What a BARE `<field>=` selects, measured on kids (1.43.4) and zen (1.42.2), which
# agree exactly. Three behaviours, by field:
#   TAG_TABLE_FIELDS      match nothing            (genre= is 0 of 5 and 0 of 3887)
#   EMPTY_MATCHES_EVERY   match every row          (studio= is 5 of 5 and 3887 of 3887,
#                                                   though 14 of 200 zen movies have none)
#   everything else       match the valueless rows (contentRating= is 3 and 2864)
# The codec axes disagree across the two versions (kids nothing, zen everything), so
# they are a version difference rather than a rule and keep the middle behaviour.
EMPTY_MATCHES_EVERY = {"studio", "editionTitle"}

# `contentRating=None`, in that exact spelling, is a synonym for the empty value on
# both servers (2864 and 3 rows, the same as `contentRating=`), while `none` and
# `NONE` match nothing. It is the key of the axis own trailing bucket, and no other
# field axis has one.
EMPTY_SYNONYM = {"contentRating": "None"}


def _exact(operator, actual, wanted, field=""):
    """Exact set membership: tag, language and resolution fields.

    Three measurements shape this. `contentRating=G` matches the row rated `G`
    and NOT the one rated `PG`, so a bare `=` here is equality rather than the
    substring match a `string` field gets. An EMPTY value matches the rows that
    have no value at all -- `contentRating=` returned exactly the two rows
    without one, and `genre=` returned none, because every row has a genre. And
    an operator the type does not advertise matches nothing rather than raising:
    `resolution>>=720` is a 200 with an empty list.
    """
    values = _values(actual)
    wanted = str(wanted)
    if wanted == EMPTY_SYNONYM.get(field):
        wanted = ""
    if wanted == "":
        if field in TAG_TABLE_FIELDS:
            hit = False
        elif field in EMPTY_MATCHES_EVERY:
            hit = True
        else:
            hit = not values
    else:
        hit = wanted in values
    if operator == "!=":
        return not hit
    if operator == "=":
        return hit
    return False


def _tag_column(operator, actual, wanted, field=""):
    """A `tag`-typed COLUMN under an operator the tag-table fields never take.

    Cycle 19, measured on kids (1.43.4) and zen (1.42.2). `contentRating` is declared
    `tag` in the Meta like `genre` is, but it is a COLUMN on the row rather than a join
    against a tag table -- and unlike a tag-table field, which `operator_allowed` closes
    to `=`/`!=`, it answers all eight:

        zen, 3,887 movies, 2,864 of them with no rating at all
        contentRating=PG     112     ==PG    112      (exact; `=` is CASE-SENSITIVE,
        contentRating!=PG   3,775    !==PG  3,775      `==` is not: `==pg` is 112 too
        contentRating<=PG   3,197    <<=PG  3,085      and `=pg` is 0)
        contentRating>=PG     802    >>=PG    690
        contentRating<=      2,864   <<=        0     the empty value compares as ""
        contentRating>=      3,887   >>=    1,023     so a row with none is the low end

    The four ordering keys partition exactly -- 802 + 3,085 = 3,887 and 3,197 + 690 =
    3,887 -- so they are `>=`, `<`, `<=`, `>` over the stored string, and the comparison
    is BYTE-ordered rather than case-folded: `<=pg` answers every row (a lowercase `p`
    sorts above every rating a real library holds) where `<=PG` answers 3,197. kids
    agrees on all sixteen counts at its own scale.

    plexsim answered NOTHING to six of the eight, so the Content Rating filter's
    "is before"/"is after" controls -- and `contentRating>=` with no value, which the
    client sends as "has any rating" -- came back empty.
    """
    values = _values(actual)
    text = str(values[0]) if values else ""
    wanted = str(wanted)
    if wanted == EMPTY_SYNONYM.get(field):
        wanted = ""
    if operator in ("==", "!=="):
        hit = text.lower() == wanted.lower()
        return hit if operator == "==" else not hit
    if operator == "<=":
        return text <= wanted
    if operator == "<<=":
        return text < wanted
    if operator == ">=":
        return text >= wanted
    return text > wanted


def compare(kind, operator, actual, wanted, field=""):
    """One (type, operator) pair, measured against two real servers.

        tag       =  is (by id OR by name)            != is not
        integer   =  is            != is not          <<= strictly <   >>= strictly >
                  <= at most      >= at least
        string    =  CONTAINS      != does not        == is exactly    !== is not exactly
                  <= begins with  >= ends with
        boolean   =  is true (value `1`)              != is false
        date      <<= before      >>= after           >= inclusive after

    The surprise is `string` with a bare `=`: it is a substring match, not
    equality, and `==` is the exact one.
    """
    if field == "location":
        # Declared `tag` in the Meta, but the value is a section Location ID, not
        # a path: measured, `location=1` returns the whole of section 1 and
        # `location=/data/movies` is a 500. So it compares as an integer.
        kind = "integer"

    if kind == "resolution":
        # Measured: the value is validated against a fixed set, and anything
        # outside it -- `2160`, `4K`, `hd`, `108`, or empty -- is a 500 rather
        # than an empty result.
        if str(wanted).strip() not in RESOLUTIONS:
            raise FilterError(500, "unknown resolution %r" % wanted)
        # Cycle 19: every operator answers the same set as `=`, `!=` INCLUDED. On zen
        # `resolution=1080` and all seven others each answer 3,608 of 3,887; `=sd` and
        # its seven answer 2; `=720` and its seven answer 6. kids agrees (5 and 0 and 0
        # on its five clips), and a show library is the same (490 of 507 for every
        # operator at `1080`). plexsim negated `!=` and answered nothing to the other
        # six, so a client's "not 4k" filter emptied the grid.
        return _exact("=", actual, wanted)

    if kind in ("audioLanguage", "subtitleLanguage"):
        # Cycle 20, and the answer to the question cycle 19 left open. A bare `=` is
        # "SOME stream of that type has this language"; every other operator is "NO
        # stream satisfies the operator's own comparison" -- a universally quantified
        # negation -- with `!=` and `==` both comparing EQUALITY and `!==` inequality.
        # Measured on kids' six all-English episodes (`=`6 `!=`0 `==`0 `!==`6 `<=`0
        # `>=`0 `<<=`6 `>>=`6) and reproduced over zen's 9,287 episodes and 3,887
        # movies, including a code no row holds (`=zz` 0, `!=zz` 3,887, `<=zz` 21 --
        # the rows with no audio stream at all, which match every non-`=` operator
        # vacuously). The three-letter code normalises for every operator alike.
        values = [language_code(v)
                  for v in (actual if isinstance(actual, (list, tuple)) else [actual])
                  if v is not None]
        # The three-letter code normalises for EVERY operator, not just `=`: zen
        # answers `audioLanguage=eng` and `=en` with the same 2,375 rows and
        # `<=eng`/`<=en` with the same 1,330, so both sides are folded to the
        # 639-1 tag before anything is compared.
        wanted = language_code(wanted)
        values = list(dict.fromkeys(values))
        if operator == "=":
            return any(str(v) == wanted for v in values)

        def holds(one):
            one = str(one)
            if operator in ("!=", "=="):
                return one == wanted
            if operator == "!==":
                return one != wanted
            if operator == "<=":
                return one <= wanted
            if operator == "<<=":
                return one < wanted
            if operator == ">=":
                return one >= wanted
            return one > wanted

        return not any(holds(v) for v in values)

    if kind == "media_string":
        # Cycle 54, fitted exactly on zen's 3,887 movies: `mediaVideoCodec`,
        # `mediaAudioCodec` and `mediaContainer` compare EACH Media's value and match
        # when ANY does, under every operator, with a Media that names none counting as
        # the empty string. `=h264` 1,816; `!=h264` 2,120 (every row with some OTHER
        # codec, not the 2,071 a negated membership gives); `=` 23 (a Media with no
        # codec); `>>=h` 3,685 and `<<=h` 222, both ANY; and `==H264` 0 -- the exact
        # pair is case-SENSITIVE here, unlike `contentRating`'s.
        values = [str(v or "") for v in (actual if isinstance(actual, (list, tuple))
                                           else [actual])]
        wanted = str(wanted)
        tests = {"=": lambda v: v == wanted, "==": lambda v: v == wanted,
                 "!=": lambda v: v != wanted, "!==": lambda v: v != wanted,
                 "<=": lambda v: v <= wanted, "<<=": lambda v: v < wanted,
                 ">=": lambda v: v >= wanted, ">>=": lambda v: v > wanted}
        return any(tests.get(operator, lambda v: False)(v) for v in values)

    if kind == "tag":
        if field not in TAG_TABLE_FIELDS and operator not in TAG_OPERATORS:
            # A tag-typed COLUMN, not a join against the tag table: it takes all eight
            # operators. `operator_allowed` already closes the tag-TABLE fields to
            # `=`/`!=`, so nothing else reaches here.
            return _tag_column(operator, actual, wanted, field)
        return _exact(operator, actual, wanted, field)

    if kind == "date":
        # Cycle 15: the VALUE is parsed before the row's own is looked at. plexsim
        # returned here first, so on a library where nothing carries the column --
        # `lastRatedAt`, `createdAt`, `lastViewedAt` on most of them -- an
        # unparseable value was never refused at all. Both references 500 it.
        moment = as_epoch(wanted)
        if actual is None and field in NULL_IS_ZERO_DATES:
            # Cycle 54, measured on zen and kids: the three per-user DATES are the
            # `viewCount` family, not this one -- an unset value is below every value and
            # equal to 0. zen's `lastRatedAt<<=2020-01-01` is the 3,465 unrated movies.
            return operator in ("<=", "<<=") or (operator == "=" and moment == 0)
        if actual is None:
            # Cycle 20, re-measured on zen (1.42.2) and kids (1.43.4): a NULL date
            # matches NOTHING, on every operator. Of zen's 3,887 movies exactly 1,518
            # carry an `originallyAvailableAt`, and `<=2100-01-01` answers those 1,518
            # while `<=1800-01-01` and `<<=1800-01-01` answer 0 -- so the 2,369 without
            # one are in neither half, which is cycle 19's numeric rule exactly. The
            # old reading ("a NULL date matches is-before") came from fun, which is out
            # of scope, and it put every season of a show library into every `<=` set.
            return False
        # A row may hold a date as an ISO string (`originallyAvailableAt`) or as
        # an epoch (`addedAt`); both are dates to a filter.
        actual = as_epoch(actual) if not str(actual).replace(".", "").isdigit() \
            else as_number(actual)
        if operator in (">>=",):
            return actual > moment
        if operator in (">=",):
            return actual >= moment
        if operator in ("<<=",):
            return actual < moment
        if operator in ("<=",):
            return actual <= moment
        if operator in ("!=", "!=="):
            # Cycle 20: `!=` really compares. `addedAt!=2020-01-01` answers
            # every row on both references, where plexsim fell through to the equality
            # below and answered none.
            return abs(actual - moment) >= 1
        return abs(actual - moment) < 1

    if kind == "boolean":
        # Measured: `unwatched=2` returns the same single row as `unwatched=0`,
        # so the value is not a number being compared -- it is true only when it
        # is exactly `1`, and everything else (an empty value included) is false.
        #
        # Cycle 19 measured the rest of the grammar, on kids (1.43.4) and zen
        # (1.42.2) alike, over `unwatched` and `duplicate`, seven values and all
        # eight operators: exactly TWO requests answer the true set -- `field=1`
        # and `field!=0`. Everything else answers the rows where the column is
        # FALSE. zen's `unwatched=1` is 3,866 of 3,887 and its `!=1`, `==1`,
        # `!==1`, `<=1`, `>=1`, `<<=1`, `>>=1`, `=0`, `==0`, `!==0`, `<=0`, `>=0`,
        # `<<=0`, `>>=0`, `=2`, `!=2`, `=`, `=01`, `=true` and `=1 ` are all the
        # same 21; `duplicate=1` is 214 and every one of those the other 3,673.
        #
        # `!=2` is what rules the obvious model out: a value parsed to 0 and then
        # negated would answer the true set there, and it does not. So the operator
        # is honoured only for the two pairs above and the rest is a constant
        # "the column is false". plexsim compared 0/1 numerically, so `unwatched>=0`
        # answered the whole library and `unwatched!=2` the unwatched half -- the
        # Unplayed filter inverted for every value the client does not send.
        if isinstance(actual, (list, tuple)):
            # A parent row reads a boolean media column from its LEAVES, and matches
            # when ANY of them does -- which is what makes the Duplicates filter's two
            # halves overlap on a show. Same shape as the numeric branch above.
            values = [v for v in actual if v is not None]
            if not values:
                return False
            return any(compare(kind, operator, v, wanted, field) for v in values)
        state = 1 if actual else 0
        wanted = str(wanted)
        if field in OPERATOR_BLIND_BOOLEANS:
            # Cycle 20, measured on kids' two seasons and zen's 741 over all eight
            # operators and six values: `unmatched` IGNORES the operator entirely --
            # only the literal `1` selects the true rows, and it does so under every
            # operator, while every other value selects the false ones under every
            # operator. It is `resolution`'s rule, not `unwatched`'s, and `unwatched`
            # itself is the control: it follows the two-request boolean rule on both
            # references. plexsim read it as a boolean, so `unmatched!=1` was inverted.
            return state == (1 if wanted == "1" else 0)
        if (operator == "=" and wanted == "1") or (operator == "!=" and wanted == "0"):
            return state == 1
        return state == 0

    if kind == "id":
        # `id` is a real filter field on every listing -- measured on kids (1.43.4)
        # and zen (1.42.2), and echoed by `/query/parse` as a Rule with an EMPTY
        # `fieldTitle`, because it is a field no Meta advertises. It compares like
        # the database column it is: a value that converts to a number compares
        # numerically (`id=01` and `id= 1` both find row 1), and one that cannot --
        # `abc`, an empty value -- sorts ABOVE every number, so nothing is `>` it,
        # everything is `<` it, and `=` matches none while `!=` matches all. `==`
        # and `!==` are the exact-string pair and never match a number at all.
        # plexsim had no such field, so python-plexapi's `editions()` -- which asks
        # `guid=<x>&id!=<self>` -- reported every item as its own duplicate.
        try:
            target = float(str(wanted).strip())
        except (TypeError, ValueError):
            # A value that is not a number sorts ABOVE every id: nothing is `>` it,
            # everything is `<` it, `=` matches none and `!=` all. The exact pair
            # matches nothing there either, which is what cycle 11 measured -- and
            # then read onto a numeric value as well.
            if operator in ("==", "!=="):
                return operator == "!=="
            return operator in ("!=", "<<=", "<=")
        if operator in ("==", "!=="):
            # Cycle 20, measured on kids and zen over two libraries and two row types:
            # against a NUMBER the exact pair compares like `=`/`!=`. zen's
            # `type=3&id==5190` is that one season and `!==5190` the other 740.
            hit = as_number(actual if actual is not None else 0) == target
            return hit if operator == "==" else not hit
        actual = as_number(actual if actual is not None else 0)
        if operator == ">>=":
            return actual > target
        if operator == ">=":
            return actual >= target
        if operator == "<<=":
            return actual < target
        if operator == "<=":
            return actual <= target
        if operator == "!=":
            return actual != target
        return actual == target

    if kind == "integer":
        if field in FRACTIONAL_REFUSED and "." in str(wanted):
            # Cycle 15: a fractional value is a 500 on the per-user-state columns and
            # NOT on `year`, `duration`, `index` and the rest -- so it is the family,
            # not the python type. Measured on both references.
            raise FilterError(500, "fractional value %r on %s" % (wanted, field))
        target = as_number(wanted)
        if isinstance(actual, (list, tuple)):
            # Cycle 19: a MEDIA column can hold several values -- one per Media on the
            # row, or one per Media of every leaf on a show or a season -- and a real
            # PMS matches when ANY of them does. zen's `type=2&duration<=1200000` (39)
            # and `>=1200000` (483) overlap on 505 rows, which only an ANY can produce.
            values = [v for v in actual if v is not None]
            if not values:
                return False
            return any(compare(kind, operator, v, wanted, field) for v in values)
        # Cycle 19, measured on kids and zen: a row with NO value for a numeric column
        # matches NOTHING -- `!=` and `!==` included, which is what separates this from
        # treating the absence as a 0. On zen 742 of 3,887 movies carry a `rating`, and
        # `rating!=7` is 736, `rating!==7` 742, `rating<=7` 368 and `rating>>=7` 374:
        # 368 + 374 = 742 exactly, so the 3,145 rows without one are in neither half.
        # Its show library, where NO row carries a rating at all, answers 0 to all
        # eight. plexsim read the absence as 0, so every `<=`/`<<=`/`!=` on Rating,
        # Plays, Critic Rating or Audience Rating answered the rows that have no value
        # -- the majority of a real library -- and `rating<=7` was the whole grid.
        # The VALUE is still parsed first (cycle 15), so a word is a 500 here whether
        # or not the column is null.
        if actual is None:
            # ...except on the two per-user columns, where a row with NO stored value
            # is read as BELOW every value and equal to 0. Cycle 20, measured on kids
            # -- whose six episodes split three with a stored `view_count = 0` and
            # three with no settings row at all -- and on zen: `viewCount=0` answers
            # all six and 8,943 of 9,287, `<<=0` answers only the three with no row,
            # `>=0` only the three with one, and `!=0` none. `skipCount` is NOT in the
            # family: its nulls match nothing, which is what separates the two.
            if field in NULL_IS_ZERO_FIELDS:
                return (operator in ("<=", "<<=")
                        or (operator == "=" and target == 0))
            return False
        if operator in ("==", "!=="):
            # ...and this pair is an exact STRING comparison of the stored number, not
            # a numeric one. On zen `rating==7` answers 0 where `rating==7.0` answers
            # the same 6 rows `rating=7` does, and `userRating==5` answers 0 against
            # `==5.0`'s 43 -- while the INTEGER column answers `year==2000` with the
            # same 39 as `year=2000` and `year==2000.0` with none. So the comparison is
            # against the column's own rendering: a float prints `7.0`, an integer
            # `2000`. plexsim compared numerically, so `rating==7` found the 7.0 rows a
            # real PMS does not.
            hit = str(actual) == str(wanted)
            return hit if operator == "==" else not hit
        actual = as_number(actual)
        if operator == ">>=":
            return actual > target
        if operator == ">=":
            return actual >= target
        if operator == "<<=":
            return actual < target
        if operator == "<=":
            return actual <= target
        if operator == "!=":
            return actual != target
        return actual == target

    # Strings. A value that arrives as a list here has no string meaning, so it
    # falls back to set membership rather than stringifying the list.
    if isinstance(actual, (list, tuple)):
        return _exact(operator, actual, wanted)

    text = "" if actual is None else str(actual)
    wanted = str(wanted)
    if field == "title" and operator == "=":
        # Cycle 18, measured on zen over all 3,887 rows of a real movie library: the
        # bare `title=` matcher -- and ONLY it -- also reads the row's ORIGINAL title.
        # `title=the` answers 1,484 rows where the title alone explains 1,481, and the
        # three extras are `Interstate 60` ("Interstate 60: Episodes of the Road"),
        # `Millennial Killer` ("The Millennial Killer") and `Vae Victis` ("The Art of
        # Evil"); `title=B` is 815 against 807 and `title=art` 7 against 6. Every other
        # operator reads the title alone -- `title!=`, `==`, `!==`, `<=` and `>=` all
        # reproduce exactly on the title and DISAGREE once the original title is added,
        # which is what separates the two halves. Held out over 24 (operator, value)
        # pairs. A client searching a foreign film by its own-language title finds it.
        # Cycle 31, measured on zen -- whose 471 rows with an `originalTitle` are the only
        # instrument for it: the ORIGINAL title is matched as a CONSECUTIVE run of whole
        # words, unfolded, and not by the title's two-pass folded matcher.
        #
        #   `Le Salaire`, `Salaire de`, `de la peur`, `Le Sal`, `la peur` -> 1 row
        #   `Salaire peur`, `Le peur`, `Sal de`, `e Salaire`, `Meurtres Douai` -> 0
        #   raw `Aşkı`, `Işığı`, `Ồn`, `tuổi` -> 1 each;  `Aski`, `Isigi` -> 0
        #
        # So the terms must be adjacent (the title's matcher allows gaps), each but the
        # last must be a whole word, the last may begin one, and nothing folds. plexsim ran
        # the title matcher over it, which matched 14 extra rows in a 1,244-query corpus --
        # `A` finding 'The Doway Murders' through `à`, `La La` finding a row whose two
        # `la`s are not adjacent.
        # Cycle 32: `text` is the plain title (compare() stringifies `actual` early);
        # the folded pass needs the row's stored SORT title, which rides on the value.
        return (_word_prefixes(wanted, text,
                               str(getattr(actual, "sort", text) or ""))
                or _original_phrase(wanted, str(getattr(actual, "original", "") or "")))
    if field in UNLISTED_STRING_FIELDS and operator in ("<=", "<<=", ">=", ">>="):
        # Cycle 14 measured that `titleSort`, `summary`, `tagline` and `originalTitle`
        # -- the string fields no Meta advertises -- take `>>`/`<<` where an advertised
        # one ignores them, and recorded `titleSort>>=ZZZZ` answering 1 row of zen's
        # 3,887. plexsim allowed the operators and then fell through to CONTAINMENT,
        # so `titleSort<<=0` matched every row whose title happens to hold a `0`.
        #
        # Cycle 18, on zen (1.42.2): all four keys are a case-insensitive
        # LEXICOGRAPHIC comparison on these fields, and both sides are STRICT.
        # `titleSort<<=B` answers 127 and `>>=B` 3,760, which partition the library
        # exactly; `<<=Ba` answers 128, taking in `B2: Stealth at War`; `B` and `b`
        # agree to the row; `<<=ZZZZ` is 3,886 and `>>=ZZZZ` the one CJK title.
        # Against a value a row holds EXACTLY -- `titleSort<<=Baasha` and
        # `>>=Baasha`, 128 + 3,758 = 3,886 of 3,887 -- that row is in NEITHER set,
        # which is what makes both strict rather than one of them inclusive.
        #
        # `<=` and `>=` are NOT begins-with and ends-with here: `titleSort<=B`
        # answers the same 127 rows as `<<=B`, starting with `01 Kid Krrish`. That
        # pair keeps its advertised meaning on an ADVERTISED string field -- zen's
        # `title<=B` is 136 rows all beginning with B and `title>=ing` 26 ending in
        # `ing` -- so the two families genuinely differ, and the split is the
        # advertised FieldType's, exactly as cycle 14 found for `>>`/`<<`.
        # Cycle 20, measured on zen's 3,887 movies and on kids: only `titleSort`
        # folds case. `titleSort<=b` and `<=B` answer the same 127 rows, while
        # `summary<=b` is 3,884 against `<=B`'s 2,911, `tagline<=b` 3,878 against 3,011
        # and `originalTitle<=b` 3,548 against 3,439 -- a BYTE-ordered comparison, in
        # which every upper-case letter sorts below every lower-case one. Cycle 18
        # measured `titleSort` and read the fold onto all four.
        if field == "titleSort":
            lower, target = text.lower(), wanted.lower()
        else:
            lower, target = text, wanted
        return lower < target if operator in ("<=", "<<=") else lower > target
    if field in EXACT_STRING_FIELDS and operator in ("=", "!="):
        # Measured: `titleSort=Sin` and `summary=e` match nothing where
        # `titleSort=sintel` matches, and `titleSort!=Sin` keeps every row -- on
        # these fields both operators are a case-insensitive EQUALITY, and an
        # empty value matches a row with none (`tagline=` is the three rows
        # without one).
        hit = text.lower() == wanted.lower()
        return hit if operator == "=" else not hit
    if operator == "==":
        # Measured: `title%3D=sintel` finds Sintel. Exact, but not case-sensitive.
        return text.lower() == wanted.lower()
    if operator == "!==":
        return text.lower() != wanted.lower()
    if operator == "<=":
        return text.lower().startswith(wanted.lower())
    if operator == ">=":
        return text.lower().endswith(wanted.lower())
    if operator == "!=":
        return wanted.lower() not in text.lower()
    return wanted.lower() in text.lower()


def parse(pairs, field_type, structural=STRUCTURAL, errors=None):
    """Ordered (key, value) pairs -> an expression tree.

    `field_type(name)` returns the bare field's advertised type, or None for a
    field no library has -- an unknown field is IGNORED by a listing (measured:
    200 with the library unfiltered), which is why it cannot simply raise.

    Pass `errors` (a list) to collect what /query/parse reports instead of
    swallowing it: an unknown field or an operator its type does not take becomes
    `{"type": "field", "value": <the raw key>}`, in the order the keys arrived.

    The tree is `{"op": "and"|"or", "rules": [...]}` with leaves
    `{"field", "operator", "value", "type", "scope"}`. Grouping follows SQL:
    `and` binds tighter than `or`, `push`/`pop` are parentheses.
    """
    # Each level collects OR-groups of AND-lists: [[a, b], [c]] is (a and b) or c.
    stack = [[[]]]
    pending = None   # the separator seen since the last clause
    last = None      # the clause most recently placed, for the trailing-`and` quirk
    stray_pop = False

    def report(kind, value, **extra):
        if errors is not None:
            entry = {"type": kind, "value": value}
            entry.update(extra)
            errors.append(entry)

    def place(node):
        groups = stack[-1]
        if pending == "or":
            groups.append([node])
        else:
            groups[-1].append(node)

    def collapse(groups):
        rules = []
        for group in groups:
            if not group:
                continue
            rules.append(group[0] if len(group) == 1
                         else {"op": "and", "rules": group})
        if not rules:
            return None
        return rules[0] if len(rules) == 1 else {"op": "or", "rules": rules}

    for key, value in pairs:
        if key.startswith(IGNORED_PREFIXES):
            continue
        if key == "push":
            stack.append([[]])
            pending = None
            continue
        if key == "pop":
            if len(stack) > 1:
                group = collapse(stack.pop())
                if group is not None:
                    place(group)
                    last = group
            else:
                # Measured: a `pop` with no matching `push` is reported as an
                # unknown field and leaves the rules wrapped in a one-child
                # composite rather than collapsed.
                stray_pop = True
                report("field", key)
            pending = None
            continue
        if key in ("and", "or"):
            if last is None:
                # Measured: a separator with nothing to its left is reported as
                # an unknown field AND abandons every rule in the query.
                report("field", key)
                return None
            pending = key
            continue
        if key in structural:
            continue
        name, operator = split_key(key)
        scope, bare = split_scope(name)
        kind = field_type(bare)
        if kind is None or not operator_allowed(kind, bare, operator):
            report("field", key)
            continue
        # A comma-separated value is an OR over the same field -- except under a
        # negating operator, where it is an AND: measured, `?genre!=6,27` returns
        # the rows with NEITHER tag, so it is not(6) and not(27). Read as an OR
        # it would match everything that is missing either one, i.e. everything.
        if bare == "guid" and operator == "=":
            # Measured: the parser echoes a bare `guid=` back as `<=`, because that is
            # what it does -- cycle 13 measured the prefix match from the listing side.
            operator = "<="
        alternatives = [v for v in value.split(",") if v != ""] or [value]
        if bare == "title" and operator == "=" and "," in value:
            # Cycle 31, measured on kids and zen: on `title` an EMPTY comma member is KEPT,
            # and since an empty `title=` matches every row a trailing comma answers the
            # whole library -- kids' `title=Steel,,` and `title=of,` are 5 of 5, zen's
            # `title=Love,` is 3,887 where `title=Love` is 59, and `title=Actually,Love` is
            # the union (59, Love Actually being in it already). The empty member is dropped
            # above because a TAG field's comma list is a set of ids where an empty member
            # means nothing; on the title matcher it means "any row".
            alternatives = value.split(",")
        leaves = [{"field": bare, "scope": scope, "operator": operator,
                   "value": v, "type": kind} for v in alternatives]
        joiner = "and" if operator in ("!=", "!==") else "or"
        node = leaves[0] if len(leaves) == 1 else {"op": joiner, "rules": leaves}
        place(node)
        last = node
        pending = None

    if pending is not None and last is not None:
        # Measured: a TRAILING `and=1` re-places the last clause and reports
        # `eof` as an unknown field. Reproduced quirk and all, because the
        # harness diffs plexsim's answer against the reference's.
        place(last)
        report("field", "eof")
    while len(stack) > 1:
        report("mismatch", "eof", expected="pop")
        group = collapse(stack.pop())
        if group is not None:
            place(group)
    tree = collapse(stack[0])
    if stray_pop and tree is not None and "op" not in tree:
        tree = {"op": "and", "rules": [tree]}
    return tree


def evaluate(tree, row, value_of, resolve=None):
    """Test one row against a parsed tree. `value_of(field, row)` reads the row.

    `resolve(scope, row)` names the rows a SCOPED rule reads its field from, and
    the rule holds when ANY of them matches. Measured on a show library:
    `type=4&show.title=Fire` is every Firefly episode (the show's title, not the
    episode's), `type=3&episode.index=3` is both seasons that have an episode 3,
    and `type=2&episode.title!=Train` still finds Firefly, whose other episodes
    do not contain the word. No rows to read is no match.
    """
    if tree is None:
        return True
    if "op" in tree:
        results = (evaluate(rule, row, value_of, resolve) for rule in tree["rules"])
        return all(results) if tree["op"] == "and" else any(results)
    candidates = [row]
    if resolve is not None and tree.get("scope"):
        candidates = resolve(tree["scope"], row)
    return any(compare(tree["type"], tree["operator"], value_of(tree["field"], candidate),
                       tree["value"], tree["field"])
               for candidate in candidates)


def sort_terms(spec):
    """`sort=` -> [(key, descending, seed)].

    A spec is a COMMA-separated list and each term is a COLON-separated one: the
    key, then any of `asc`, `desc`, `nocollate` or a `random` seed, in any order.
    Splitting a term on its first colon reads the direction off the wrong end --
    `titleSort:desc:nocollate` came out ASCENDING, and the reference orders it
    descending.
    """
    terms = []
    for term in (spec or "").split(","):
        term = term.strip()
        if not term:
            continue
        parts = term.split(":")
        modifiers = parts[1:]
        seed = next((m for m in modifiers if m not in SORT_MODIFIERS), "")
        terms.append((parts[0], "desc" in modifiers, seed))
    return terms


def orders(spec, supported):
    """`sort=` -> the `Order` list /query/parse answers with.

    Measured: the seed of `random:<seed>` is DROPPED here (the document says
    `{"field": "random", "ascending": true}`), `nocollate` does not change the
    direction, and an unknown key is a 500 exactly as it is on a listing.
    """
    out = []
    for key, descending, _seed in sort_terms(spec):
        if not supported(key):
            raise FilterError(500, "unsupported sort key %s" % key)
        out.append({"field": key, "ascending": not descending})
    return out


def describe(tree, title_of, value_title_of):
    """A parsed tree as the `Query` document /query/parse answers with.

    Shape measured on a real PMS: a leaf is `{field, operator, fieldTitle, value}`
    plus `valueTitle` for a tag-table field whose value resolves, and a prefixed
    field puts its scope FIRST as `type`. A group is
    `{type: "composite", operator: "and"|"or", Rule: [...]}`. The operator is
    echoed in its FULL form, trailing `=` included.
    """
    if tree is None:
        return None
    if "op" in tree:
        return {"type": "composite", "operator": tree["op"],
                "Rule": [describe(rule, title_of, value_title_of)
                         for rule in tree["rules"]]}
    leaf = {}
    if tree.get("scope"):
        leaf["type"] = tree["scope"]
    leaf["field"] = tree["field"]
    leaf["operator"] = tree["operator"]
    # Measured: the title is looked up for (scope, field) and is the EMPTY string
    # when the scope has no such field -- `?type=3&title=x` answers
    # `fieldTitle: ""`, it does not omit it and it is not an error.
    if tree["field"] != "type":
        # Cycle 20: `type` is the one field whose Rule carries no `fieldTitle` KEY at
        # all -- not an empty one -- on both references. Every other unadvertised
        # column (`id`, `guid`, `slug`, the artwork four) carries `""`.
        leaf["fieldTitle"] = title_of(tree["field"], tree.get("scope") or "")
    if tree["field"] in TAG_TABLE_FIELDS:
        # Measured: only a value that resolves in the tag table gets a title --
        # `genre=6` is "Animation", `genre=Animation` and `genre=99999` get none.
        name = value_title_of(tree["field"], tree["value"])
        if name is not None:
            leaf["valueTitle"] = name
    leaf["value"] = tree["value"]
    return leaf


# The order a real PMS falls back to when the query names a source other than the
# library's own: cycle 20, measured on kids and zen, `sourceType=2` (or 0, or -1, or any
# integer but 1) makes /query/parse carry `Order: [{"field": "id", "ascending": true}]`
# where the same query without it carries no Order at all. An explicit `sort=` still
# wins, and the Rule, Limit and Group blocks are untouched.
SOURCE_DEFAULT_ORDER = [{"field": "id", "ascending": True}]


def source_order(value):
    """The Order block a `sourceType=` value implies. Raises for one that cannot parse."""
    if value is None:
        return None
    # Measured: an empty value, `1.5` and `abc` are each a 500, on a listing and at
    # /query/parse alike. Cycle 55, measured on zen, kids and plexscratch42: so are a
    # value past the signed 32-bit range (`2147483648`) and a surrounding space (`%201`),
    # which `int(value.strip())` accepted; `+1` and `01` are the library's own source.
    text = str(value)
    if not re.fullmatch(r"[+-]?[0-9]+", text) or not -2 ** 31 <= int(text) < 2 ** 31:
        raise FilterError(500, "bad sourceType %r" % value)
    number = int(text)
    return None if number == 1 else list(SOURCE_DEFAULT_ORDER)


def document(type_number, tree, order, limit, group, errors,
             title_of, value_title_of):
    """The whole `/library/sections/{id}/query/parse` response.

    The root is `Query`, NOT a MediaContainer -- one of two responses on the
    whole surface that are not (the other is `/butler`'s `ButlerTasks`). Block
    order is the reference's: type, Rule, Order, Limit, Group, Error.
    """
    body = {"type": type_number}
    rule = describe(tree, title_of, value_title_of)
    if rule is not None:
        body["Rule"] = rule
    if order:
        body["Order"] = order
    if limit is not None:
        body["Limit"] = [{"limit": limit}]
    if group:
        body["Group"] = group
    if errors:
        body["Error"] = errors
    return {"Query": body}
