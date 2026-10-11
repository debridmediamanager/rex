"""Which title a release is, from the most exact evidence to the least.

Measured on the user's own library on 2026-10-06 (3,347 torrents; DMM's labels as the
answer key, `tests/fixtures/identify/`), before any of this was written:

* The builder's old way -- the file name, TMDB's first search hit -- named 165 of 198
  labelled movies right, 11 wrong and 22 not at all.
* Plex's own matcher, sent what a real PMS sends (`POST /library/metadata/matches` with
  `type`, `title`, `year` and `filename`, read off plexlab's outbound capture) named
  184 right, 8 wrong, 6 not at all.
* Where the two agreed (167 of 198) they were right 163 times; the four left are two
  films sharing a title and a year, where the label itself is the doubtful side. Where
  they disagreed, Plex was right 7 of 10.
* DMM already knows half the library by its info hash (1,687 of 3,347: zurg's own
  matches, and the pages users added each release from), which is exact.

So, in order: an id written into the name, the hash's known IMDb id (zurg asks DMM and
hands it over as the release's `imdb_id`), then the cleaned name to Plex's matcher and
TMDB, with their agreement as the confidence. Nothing is guessed: a release no source
names stays unidentified, for Fix Match. The native server's port is
`rust/src/identify.rs`, held to this module's score on the same corpus.
"""

import re

import metadata as metadata_module

# Confidence, most to least certain. `exact` came from an id, not a name.
EXACT, AGREED, SINGLE, NONE = "exact", "agreed", "single", "none"

# Ids a release names outright: Radarr/Sonarr folder tags (`{imdb-tt4357198}`,
# `[imdbid-tt…]`, `{tmdb-12345}`) and a bare IMDb id. Seen in the library as
# `How to Make a Killing (2026) {imdb-tt4357198}`.
_IMDB_TAG = re.compile(r"(?i)(?:imdb(?:id)?[-_ =:]?)?\b(tt\d{7,9})\b")
_TMDB_TAG = re.compile(r"(?i)[\[{(]tmdb(?:id)?[-_ =:](\d{1,8})[\]})]")

# Noise the release-name parser does not know, each seen in the library:
# `www.UIndex.org    -    The Matrix 1999 …` (a site ahead of the title),
# `Matrix 4Kremux2160.pctreload1.com.mkv` (a site after it),
# `[Lia] One Piece - 0001-1000 [1080p]` (an episode range),
# `The.Matrix.Remastered.1999 …` (an edition word ahead of the year).
_SITE_PREFIX = re.compile(r"(?i)^\s*(?:www\.)?[\w-]+\.(?:org|com|net|to|me|cc|io|ws|xyz|tv|lol|ru|li|se|nl)\s*[-–—_:|]+\s*")
_SITE_ANY = re.compile(r"(?i)\b(?:www\.)?[\w-]+\.(?:org|com|net|to|me|cc|io|ws|xyz|lol|ru)\b")
_RANGE = re.compile(r"\s[-–]\s\d{1,4}\s?-\s?\d{1,4}\b.*$")
# `Avatar - The Last Airbender (2005 - 2008) [1080p]`: the years a series ran.
_YEARS_RAN = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[-–]\s*(?:(?:19|20)\d{2})?\s*[)\]]")
_EDITION = re.compile(r"(?i)\b(?:remastered|extended(?: edition)?|director'?s cut|unrated|theatrical|4kremux\d*|uhd|imax)\b")
# A season pack names its season without an episode: `Planet.Earth.II.S01.1080p…`,
# `The Sopranos S01 …`, `Show Season 2 …`, `S01-S03`.
_SEASON_PACK = re.compile(r"(?i)\b(?:S(\d{1,2})(?:\s?-\s?S?\d{1,2})?|Season[\s._-]?(\d{1,2}))\b(?![\s._-]?E\d)")


def explicit_ids(name):
    """{'imdb': 'tt…'} / {'tmdb': '…'} written into the release name itself."""
    found = {}
    imdb = _IMDB_TAG.search(name)
    if imdb:
        found["imdb"] = imdb.group(1)
    tmdb = _TMDB_TAG.search(name)
    if tmdb:
        found["tmdb"] = tmdb.group(1)
    return found


def parse_name(name):
    """`metadata.parse_release` after removing the noise it does not know.

    Returns its {title, year, season, episode} plus `pack`: True for a season pack,
    which is a show without an episode number.
    """
    stem = _SITE_PREFIX.sub("", name)
    stem = re.sub(r"[{\[(](?:imdb|tmdb|tvdb)(?:id)?[-_ =:][^}\])]*[}\])]", " ", stem, flags=re.I)
    stem = _SITE_ANY.sub(" ", stem)
    stem = _EDITION.sub(" ", stem)
    # `parse_release` drops every bracketed group as release-team noise, and with it
    # the year of `For the Birds (2000).mkv` and `How to Make a Killing (2026) {…}`.
    # The first year a series ran is the one Plex and TMDB know it by: without it,
    # `Avatar - The Last Airbender (2005 - 2008)` matched the 2024 remake.
    ran = _YEARS_RAN.search(stem)
    stem = _YEARS_RAN.sub(" ", stem)
    stem = re.sub(r"[(\[]((?:19|20)\d{2})[)\]]", r" \1 ", stem)
    # An episode range is a show's batch, never a film: `One Piece - 0001-1000` was
    # matched to a 2026 movie called One Piece before this.
    ranged = bool(_RANGE.search(stem))
    stem = _RANGE.sub("", stem)
    parsed = metadata_module.parse_release(stem)
    parsed["pack"] = ranged or bool(ran)
    if ran and not parsed["year"]:
        parsed["year"] = int(ran.group(1))
    if parsed["season"] is None and parsed["episode"] is None:
        pack = _SEASON_PACK.search(parsed["title"]) or _SEASON_PACK.search(stem.replace(".", " "))
        if pack:
            parsed["season"] = int(pack.group(1) or pack.group(2))
            parsed["pack"] = True
            head = _SEASON_PACK.split(parsed["title"].replace(".", " "), maxsplit=1)[0]
            parsed["title"] = re.sub(r"\s+", " ", head).strip(" -–—")
    parsed["title"] = re.sub(r"\s+", " ", parsed["title"]).strip(" -–—")
    return parsed


def is_show(parsed):
    return parsed["episode"] is not None or parsed["season"] is not None or parsed["pack"]


class Identity(dict):
    """kind ('movie'|'show'), imdb, tmdb, plex guid, confidence, source, and the
    parsed name. A dict so it can be written into the catalog as it is."""


class Identifier:
    """Runs the steps in order for one torrent. `plex` is a metadata.PlexProvider,
    `tmdb` a metadata.Tmdb, `known` the info hashes whose IMDb id is already known
    ({hash: imdb}); any of them may be None."""

    def __init__(self, tmdb=None, plex=None, known=None):
        self.tmdb = tmdb
        self.plex = plex
        self.known = known or {}

    def _tmdb_imdb(self, kind, tmdb_id):
        details = self.tmdb.details("tv" if kind == "show" else "movie", tmdb_id) or {}
        return (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id")

    def _tmdb_for_imdb(self, imdb):
        """(kind, tmdb id) of an IMDb id, from TMDB's /find. An episode's or a season's
        id names its show: the id DMM knows a season pack by is usually its first
        episode's (271 of the 285 shows fun's rex left unmatched on 2026-10-11, Arrow's
        tt2340185 among them, were `tv_episode_results`)."""
        if self.tmdb is None:
            return None, None
        found = self.tmdb._get("find/" + imdb, external_source="imdb_id") or {}
        if found.get("movie_results"):
            return "movie", found["movie_results"][0]["id"]
        if found.get("tv_results"):
            return "show", found["tv_results"][0]["id"]
        for key in ("tv_episode_results", "tv_season_results"):
            if found.get(key):
                return "show", found[key][0].get("show_id")
        return None, None

    def _tmdb_identify(self, parsed, kind):
        """`Tmdb.identify`, but asked in the order the name implies: a season pack has
        no episode number, and `Tmdb.identify` searched it as a film first --
        `Planet.Earth.II.S01` found a 2019 documentary film of that name."""
        order = ("tv", "movie") if kind == "show" else ("movie", "tv")
        for tmdb_kind in order:
            if tmdb_kind == "tv" and parsed["year"]:
                # `Tmdb.search` applies a year to films only; a series is found by the
                # year it first aired.
                found = self.tmdb._get("search/tv", query=parsed["title"],
                                       first_air_date_year=parsed["year"]) or {}
                results = found.get("results") or self.tmdb.search("tv", parsed["title"])
            else:
                results = self.tmdb.search(tmdb_kind, parsed["title"], parsed["year"])
            if results:
                return ("show" if tmdb_kind == "tv" else "movie"), results[0]["id"]
        return None, None

    def identify(self, name, info_hash=None, files=1):
        """`files`: how many video files the torrent holds. Several with no episode
        numbers and no single year are a series more often than not, so they are
        tried as a show first."""
        parsed = parse_name(name)
        kind = "show" if is_show(parsed) or (files > 2 and not parsed["year"]) else "movie"
        identity = Identity(name=name, parsed=parsed, kind=kind, imdb=None, tmdb=None,
                            plex=None, confidence=NONE, source=None)

        exact = explicit_ids(name)
        imdb = exact.get("imdb") or (self.known.get(info_hash.lower()) if info_hash else None)
        if imdb or exact.get("tmdb"):
            identity.update(confidence=EXACT, source="name id" if exact else "dmm hash")
            if imdb:
                identity["imdb"] = imdb
                found_kind, tmdb_id = self._tmdb_for_imdb(imdb)
                if found_kind:
                    identity.update(kind=found_kind, tmdb=tmdb_id)
                # An episode's id found its show: the show is known by its own id.
                if identity["kind"] == "show" and identity["tmdb"]:
                    identity["imdb"] = self._tmdb_imdb("show", identity["tmdb"]) or imdb
            else:
                identity["tmdb"] = int(exact["tmdb"])
            if self.plex is not None:
                externals = ["tmdb://%s" % identity["tmdb"]] if identity["tmdb"] else []
                externals += ["imdb://%s" % identity["imdb"]] if identity["imdb"] else []
                matched = self.plex.match(identity["kind"], externals)
                if matched:
                    identity["plex"] = matched[0]
            return identity

        if not parsed["title"]:
            return identity
        plex_answer = None
        if self.plex is not None:
            plex_answer = self.plex.match_title(kind, parsed["title"], parsed["year"], name)
        tmdb_kind = tmdb_id = tmdb_imdb = None
        if self.tmdb is not None:
            tmdb_kind, tmdb_id = self._tmdb_identify(parsed, kind)
            if tmdb_id:
                tmdb_imdb = self._tmdb_imdb(tmdb_kind, tmdb_id)

        if plex_answer:
            identity.update(plex=plex_answer["guid"], imdb=plex_answer.get("imdb"),
                            tmdb=plex_answer.get("tmdb"), kind=plex_answer["kind"])
            agree = (tmdb_id and plex_answer.get("tmdb") == tmdb_id) or (
                tmdb_imdb and plex_answer.get("imdb") == tmdb_imdb)
            identity.update(confidence=AGREED if agree else SINGLE,
                            source="plex+tmdb" if agree else "plex")
            if tmdb_id and not agree:
                identity["disputed"] = {"tmdb": tmdb_id, "imdb": tmdb_imdb, "kind": tmdb_kind}
        elif tmdb_id:
            identity.update(kind=tmdb_kind, tmdb=tmdb_id, imdb=tmdb_imdb,
                            confidence=SINGLE, source="tmdb")
            # Plex did not match the name, but it knows the title by its id: without a
            # real `plex://` guid the Android app's detail page never loads (cycle 70).
            if self.plex is not None:
                matched = self.plex.match(tmdb_kind, ["tmdb://%s" % tmdb_id])
                if matched:
                    identity["plex"] = matched[0]
        return identity
