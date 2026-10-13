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
# A site is the last word of its run: `Modern.Family.S03E14.Me.Jealous` and
# `Crazy.Ex-Girlfriend.S03E02.To.Josh` are no sites, and reading them as `E14.Me` and
# `E02.To` took the episode number with them (the production server's feed, 2026-10-13).
_SITE_RUN_ON = re.compile(r"(?i)\.(?!(?:mkv|mp4|webm|avi|ts|m4v)\b)\w")
_RANGE = re.compile(r"\s[-–]\s(\d{1,4})\s?-\s?(\d{1,4})\b.*$")
# `Avatar - The Last Airbender (2005 - 2008) [1080p]`: the years a series ran.
_YEARS_RAN = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[-–]\s*(?:(?:19|20)\d{2})?\s*[)\]]")
_EDITION = re.compile(r"(?i)\b(?:remastered|extended(?: edition)?|director'?s cut|unrated|theatrical|4kremux\d*|uhd|imax)\b")
# A season pack names its season without an episode: `Planet.Earth.II.S01.1080p…`,
# `The Sopranos S01 …`, `Show Season 2 …`, `S01-S03`, and on the production server `Family.Matters.seasons.01-09`
# and `South Park - Seasons 1 to 25`.
# An episode named without `SxxEyy`: a bare number (`Mysterious Love E06 1080p`), the NxNN
# form (`Some Day or One Day - 1x02`), or only inside the brackets `parse_release` drops (a
# disc rip's `00000 (E01-The.Arrangement).m2ts`, Suits.S03 on the production server). zurg files
# these as shows since 2026-10-11, and with no number here the feed placed none of them. The
# production server's Usenet library (2026-10-12) adds four digits and an `EP` or `Ep.`
# (`Detektiv.Conan.E0221`, `One.Piece.EP1161`, `Gintama.Ep.14`), each filed under movies.
# The production server's feed (2026-10-13) adds three digits and a year for the NxNN season
# (`Animaniacs - 1x123`, `MythBusters - 2004x09`; never a bare 0, which is the hex of a group
# tag such as `[0x539] Seishun Buta Yarou …`), `Episode` spelled out
# (`Digimon Adventure Zero Two Episode 12 - …`) and a version alone (`[CyC] Mushishi 01v2`).
_LOOSE_NX = re.compile(r"(?i)(?:^|[\s._\-\[(])(0[1-9]|[1-9]\d?|(?:19|20)\d{2})x(\d{2,3})(?:[\s._\-\])]|$)")
_LOOSE_E = re.compile(r"(?i)(?:^|[\s._\-\[(])(?:E(?:P[.\s]?)?(\d{2,4})|Episode[\s._]?(\d{1,4})|(\d{2,4})v\d)"
                      r"(?:[\s._\-\])]|$)")
# An absolute episode glued to its title by a dash (`[Moozzi2] Eighty-Six-15 [BD …]`,
# `[HorribleSubs].Gintama-340.[1080p]`, the production server's Usenet movies): two or three
# digits and no year, so `Blade-Runner-2049` stays a film; a version, a last episode's `END`
# and a reposter's tag may follow (`Shichi-12 END`, `Gintama-364.[1080p]-RakuvFIN`). Spaced,
# it needs the version, since `parse_release` already reads ` - 12`: `Uso - 01v2 - Monotone`.
_DASH_EPISODE = re.compile(r"^(.*\S)(?:-(\d{2,3})(?:v\d)?(?:\s+END)?(?:\s+-\S+)?|\s+-\s+(\d{2,3})v\d(?:\s+-\s.*)?)$")
# `[philosophy-raws][Samurai Champloo][13][BDRIP]…`: every part bracketed, which
# `parse_release` drops whole, the title second and the episode third.
_BRACKETED = re.compile(r"^\s*\[[^\]]*\]\s*\[([^\]]+)\]\s*\[(\d{1,4})(?:v\d)?\]")
# `Der.mude.Tod.AKA.Destiny.1921`, `El.laberinto.del.fauno.aka.Pans.Labyrinth`: a film named
# twice, its original name first.
_AKA = re.compile(r"(?i)\s+aka\s+")
# `[Erai-raws].Kingdom.4th.Season-21.[1080p]`, `Gintama. Shirogane no Tamashii-hen 2nd Season - 14 END`:
# a season written as an ordinal, the episode after it. Read as a pack, Kingdom's 21st
# episode was a season 21 film on the production server (2026-10-13).
_ORDINAL_SEASON = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)[\s._-]+Season\b")
_ORDINAL_SEASON_EP = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)[\s._-]+Season[\s._]*-[\s._]*(\d{1,4})(?:v\d)?\b")
# `[Sakurato] Bleach： Sennen Kessen Hen - Kashin Tan [07][AVC-8bit 1080P AAC][CHS]`: the episode
# bracketed right after the title, which `parse_release` drops with the other brackets.
# A resolution is never one (`[720]`).
_BRACKET_EPISODE = re.compile(r"^\s*(?:\[[^\]]*\][\s.]*)?[^\[\]]*[^\[\]\s.]\s*\.?\[(\d{2,3})(?:v\d)?\]")
_SEASON_PACK = re.compile(r"(?i)\b(?:S(\d{1,2})(?:\s?-\s?S?\d{1,2})?|Seasons?[\s._-]?(\d{1,2}))\b(?![\s._-]?E\d)")


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
    stem = _SITE_ANY.sub(lambda m: m.group(0) if _SITE_RUN_ON.match(m.string, m.end()) else " ", stem)
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
    # Only a rising one: `One Piece - 0160 - 10 Survival Rate!` is episode 160 and its title.
    run = _RANGE.search(stem)
    ranged = bool(run) and int(run.group(2)) > int(run.group(1))
    if ranged:
        stem = stem[:run.start()] + stem[run.end():]
    parsed = metadata_module.parse_release(stem)
    parsed["pack"] = ranged or bool(ran)
    if ran and not parsed["year"]:
        parsed["year"] = int(ran.group(1))
    if parsed["season"] is None and parsed["episode"] is None:
        nx, bare = _LOOSE_NX.search(stem), _LOOSE_E.search(stem)
        # A year is a season with two digits after it (`2004x09`); `1920x816` is a resolution.
        if nx and int(nx.group(1)) >= 1900 and len(nx.group(2)) > 2:
            nx = None
        if nx or bare:
            if nx:
                parsed["season"], parsed["episode"] = int(nx.group(1)), int(nx.group(2))
            else:
                parsed["episode"] = int(bare.group(1) or bare.group(2) or bare.group(3))
            cut = (_LOOSE_NX if nx else _LOOSE_E).search(parsed["title"])
            if cut:
                parsed["title"] = parsed["title"][:cut.start()]
    # `[SubsPlease] Tower of God S2 - 07v2 (1080p)`: an absolute number after the season it is
    # in. Read as season 1 it was placed on that season's episode 7 as another version of it.
    # `Shiguang Dailiren Season 3 - 01` and `… 2nd Season - 14 END` the same way.
    if parsed["season"] is None and parsed["episode"] is not None:
        season = _SEASON_PACK.search(parsed["title"])
        ordinal = _ORDINAL_SEASON.search(parsed["title"])
        if season and (season.group(1) or season.group(2)):
            parsed["season"] = int(season.group(1) or season.group(2))
            parsed["title"] = parsed["title"][:season.start()]
        elif ordinal:
            parsed["season"] = int(ordinal.group(1))
            parsed["title"] = parsed["title"][:ordinal.start()]
    if parsed["season"] is None and parsed["episode"] is None:
        ordinal = _ORDINAL_SEASON_EP.search(stem)
        if ordinal:
            parsed["season"], parsed["episode"] = int(ordinal.group(1)), int(ordinal.group(2))
            cut = _ORDINAL_SEASON.search(parsed["title"])
            if cut:
                parsed["title"] = parsed["title"][:cut.start()]
    if parsed["season"] is None and parsed["episode"] is None:
        pack = _SEASON_PACK.search(parsed["title"]) or _SEASON_PACK.search(stem.replace(".", " "))
        if pack:
            parsed["season"] = int(pack.group(1) or pack.group(2))
            parsed["pack"] = True
            head = _SEASON_PACK.split(parsed["title"].replace(".", " "), maxsplit=1)[0]
            parsed["title"] = re.sub(r"\s+", " ", head).strip(" -–—")
    parsed["title"] = _AKA.split(re.sub(r"\s+", " ", parsed["title"]).strip(" -–—"), maxsplit=1)[0]
    dashed = _DASH_EPISODE.match(parsed["title"])
    if dashed and parsed["season"] is None and parsed["episode"] is None and not parsed["year"] \
            and not parsed["pack"]:
        parsed["title"] = dashed.group(1).strip(" -–—")
        parsed["episode"] = int(dashed.group(2) or dashed.group(3))
    numbered = _BRACKET_EPISODE.match(name)
    if numbered and parsed["season"] is None and parsed["episode"] is None and not parsed["year"] \
            and not parsed["pack"] and int(numbered.group(1)) not in (480, 576, 720):
        parsed["episode"] = int(numbered.group(1))
    bracketed = _BRACKETED.match(name)
    if bracketed and not parsed["title"] and parsed["season"] is None and parsed["episode"] is None:
        parsed["title"] = re.sub(r"\s+", " ", bracketed.group(1)).strip(" -–—")
        parsed["episode"] = int(bracketed.group(2))
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
        episode's (271 of the 285 shows the production rex left unmatched on 2026-10-11, Arrow's
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

    def _tmdb_identify(self, parsed, kind, fixed=False):
        """`Tmdb.identify`, but asked in the order the name implies: a season pack has
        no episode number, and `Tmdb.identify` searched it as a film first --
        `Planet.Earth.II.S01` found a 2019 documentary film of that name. A kind the caller
        `fixed` (its library's) is the only one asked: the other places nothing there."""
        order = ("tv", "movie") if kind == "show" else ("movie", "tv")
        order = order[:1] if fixed else order
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

    def _season_only_tmdb_has(self, plex_answer, tmdb_kind, tmdb_id, tmdb_imdb, parsed):
        """Plex and TMDB named different shows, and the release's season is one only
        TMDB's has. Plex's matcher answers one show for a bare title: for
        `The.Tom.and.Jerry.Show.S02E05` (the test host, 2026-10-10) the 1975 series, one season
        long, where TMDB's first hit is the 2014 one with five; every episode then sat
        under a show with no such season, with no episode metadata."""
        season = parsed.get("season")
        if plex_answer["kind"] != "show" or tmdb_kind != "show" or not season or self.plex is None:
            return False
        if plex_answer.get("tmdb") == tmdb_id or (tmdb_imdb and plex_answer.get("imdb") == tmdb_imdb):
            return False
        if season > ((self.tmdb.details("tv", tmdb_id) or {}).get("number_of_seasons") or 0):
            return False
        plex_seasons = self.plex.children(plex_answer["guid"])
        return bool(plex_seasons) and season not in plex_seasons

    def identify(self, name, info_hash=None, files=1, kind=None):
        """`files`: how many video files the torrent holds. Several with no episode
        numbers and no single year are a series more often than not, so they are
        tried as a show first. `kind` ('movie' or 'show') is the caller's own, a library's:
        an answer of the other kind places nothing there, so the name is asked as that kind."""
        parsed = parse_name(name)
        fixed = kind
        kind = kind or ("show" if is_show(parsed) or (files > 2 and not parsed["year"]) else "movie")
        identity = Identity(name=name, parsed=parsed, kind=kind, imdb=None, tmdb=None,
                            plex=None, confidence=NONE, source=None, thumb=None, art=None)

        exact = explicit_ids(name)
        imdb = exact.get("imdb") or (self.known.get(info_hash.lower()) if info_hash else None)
        if not (imdb or exact.get("tmdb")):
            return self._by_name(identity, fixed)
        identity.update(confidence=EXACT, source="name id" if exact else "dmm hash")
        if imdb:
            identity["imdb"] = imdb
            found_kind, tmdb_id = self._tmdb_for_imdb(imdb)
            if found_kind:
                identity.update(kind=found_kind, tmdb=tmdb_id)
            # An episode's id found its show: the show is known by its own id.
            if identity["kind"] == "show" and identity["tmdb"]:
                identity["imdb"] = self._tmdb_imdb("show", identity["tmdb"]) or imdb
        elif self.tmdb is None or self.tmdb.details("tv" if kind == "show" else "movie", int(exact["tmdb"])):
            # zurg hands over the TMDB id of whatever its own Plex matched, an episode's
            # among them: the production server's Pawn Stars came as 1559521, which no show has (2026-10-12).
            identity["tmdb"] = int(exact["tmdb"])
        if self.plex is not None:
            externals = ["tmdb://%s" % identity["tmdb"]] if identity["tmdb"] else []
            externals += ["imdb://%s" % identity["imdb"]] if identity["imdb"] else []
            row = self.plex.match_row(identity["kind"], externals)
            if row:
                answer = self.plex.answer(row, identity["kind"])
                identity.update(plex=answer["guid"], thumb=answer["thumb"], art=answer["art"])
                # TMDB's /find does not know every IMDb id Plex does: DMM gives Knock at the
                # Cabin as tt15679400, which Plex lists beside TMDB's 631842 (the production server, 2026-10-12).
                # Plex's own TMDB ids are not all TMDB's: Digimon Adventure 02's 229048 is none.
                details = self.tmdb.details("tv" if identity["kind"] == "show" else "movie", answer["tmdb"]) \
                    if not identity["tmdb"] and answer["tmdb"] and self.tmdb is not None else None
                if details:
                    identity["tmdb"] = answer["tmdb"]
                    identity["imdb"] = ((details.get("external_ids") or {}).get("imdb_id")
                                        or details.get("imdb_id") or identity["imdb"])
        if identity["tmdb"] and (not fixed or identity["kind"] == fixed):
            return identity
        # The id names no title TMDB has, or one of the other kind than the library's: on
        # the production server (2026-10-12) DMM's episode ids for Boy Meets World and Courage the Cowardly Dog,
        # Bill Nye's id for a one-episode film, and zurg's ids for episodes left 45 shows and
        # 12 films on the blank tile. The name decides then, as it does with no id at all:
        # an id TMDB does not know is as likely DMM's mistake as Plex's -- its tt6257382 for
        # A Serbian Film is Plex's "Film Postcards: Serbia".
        named = self._by_name(Identity(identity, kind=kind, imdb=None, tmdb=None, plex=None,
                                       confidence=NONE, source=None, thumb=None, art=None), fixed)
        return named if named["tmdb"] or (named["plex"] and not identity["tmdb"]) else identity

    def _by_name(self, identity, fixed):
        """The cleaned name to Plex's matcher and TMDB, their agreement the confidence."""
        name, parsed, kind = identity["name"], identity["parsed"], identity["kind"]
        if not parsed["title"]:
            return identity
        plex_answer = None
        if self.plex is not None:
            plex_answer = self.plex.match_title(kind, parsed["title"], parsed["year"], name)
        tmdb_kind = tmdb_id = tmdb_imdb = None
        if self.tmdb is not None:
            tmdb_kind, tmdb_id = self._tmdb_identify(parsed, kind, bool(fixed))
            if tmdb_id:
                tmdb_imdb = self._tmdb_imdb(tmdb_kind, tmdb_id)

        # A film Plex knows by no TMDB id is a record of its own, and the one DMM's map was
        # fed (the production server, 2026-10-12): Plex answered `A.Serbian.Film.2010` with "Film Postcards:
        # Serbia" and `Mar.Adentro.2004` with a yearless stub, where TMDB's search was right.
        stub_film = plex_answer and plex_answer["kind"] == "movie" and not plex_answer.get("tmdb") \
            and tmdb_kind == "movie"
        overruled = None
        if plex_answer and tmdb_id and (stub_film or self._season_only_tmdb_has(
                plex_answer, tmdb_kind, tmdb_id, tmdb_imdb, parsed)):
            overruled, plex_answer = plex_answer, None

        if plex_answer:
            identity.update(plex=plex_answer["guid"], imdb=plex_answer.get("imdb"),
                            tmdb=plex_answer.get("tmdb"), kind=plex_answer["kind"],
                            thumb=plex_answer.get("thumb"), art=plex_answer.get("art"))
            agree = (tmdb_id and plex_answer.get("tmdb") == tmdb_id) or (
                tmdb_imdb and plex_answer.get("imdb") == tmdb_imdb)
            identity.update(confidence=AGREED if agree else SINGLE,
                            source="plex+tmdb" if agree else "plex")
            if tmdb_id and not agree:
                identity["disputed"] = {"tmdb": tmdb_id, "imdb": tmdb_imdb, "kind": tmdb_kind}
        elif tmdb_id:
            identity.update(kind=tmdb_kind, tmdb=tmdb_id, imdb=tmdb_imdb,
                            confidence=SINGLE, source="tmdb")
            if overruled:
                identity["disputed"] = {"tmdb": overruled.get("tmdb"), "imdb": overruled.get("imdb"),
                                        "kind": overruled["kind"]}
            # Plex did not match the name, but it knows the title by its id: without a
            # real `plex://` guid the Android app's detail page never loads (cycle 70).
            if self.plex is not None:
                row = self.plex.match_row(tmdb_kind, ["tmdb://%s" % tmdb_id])
                if row:
                    answer = self.plex.answer(row, tmdb_kind)
                    identity.update(plex=answer["guid"], thumb=answer["thumb"], art=answer["art"])
        return identity


def identify_release(identifier, release):
    """A release as the native feed asks for it (`name_release`, rust/src/runtime/scan.rs):
    `{name, hash, known, tmdb, files, kind}`, where `known` is the IMDb id its source sent,
    a TMDB id sent without one is read as a `{tmdb-…}` tag in the name, and `kind` is the
    library's. The identify corpora are replayed through it."""
    name = release["name"]
    if release.get("tmdb") and not release.get("known"):
        name = "%s {tmdb-%s}" % (name, release["tmdb"])
    return identifier.identify(name, info_hash=release.get("hash") or "zurg",
                               files=release.get("files", 1), kind=release.get("kind"))
