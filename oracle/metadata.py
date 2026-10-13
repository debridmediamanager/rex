"""Identify a release against TMDB and build Plex metadata for it.

rex's rows previously carried only what the filename and an ffprobe gave:
a mangled title, a year if one happened to be in the name, and a generated
solid-colour poster. A real Plex library row carries an identity (`guid`), a
summary, artwork, genres, cast and crew, ratings and an air date, and clients
use those for far more than decoration -- the grid groups and sorts on them, and
`originallyAvailableAt` drives "recently released" style hubs.

Identification is deliberately two-stage: parse the release name into a title
plus an optional SxxEyy, then ask TMDB. A release name is not metadata, so
anything that cannot be matched keeps its probe-derived row rather than being
given invented details.

Responses are cached on disk: the same release is looked up on every catalog
rebuild, and TMDB rate-limits.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import hierarchy

API = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/"

# Release-name noise. Anything at or after the first of these tokens is not part
# of the title, which is a far more reliable rule than stripping them in place.
_CUT = re.compile(
    r"(?i)\b(1080p|2160p|720p|480p|4k|web[\s._-]?dl|webrip|web|bluray|blu[\s._-]?ray|"
    r"remux|hdtv|dvdrip|brrip|amzn|nf|hmax|glbo|dsnp|atvp|max|hulu|pcok|stan|"
    r"hevc|x264|x265|h[\s._-]?264|h[\s._-]?265|av1|xvid|divx|"
    r"ddp?5[\s._-]?1|dd5[\s._-]?1|ddp|dts|truehd|atmos|aac2[\s._-]?0|aac|ac3|eac3|opus|flac|"
    r"dv|hdr10\+?|hdr|sdr|proper|repack|imax|extended|uncut|dual|multi|complete)\b"
)

# The production server's feed dropped every one of these as "no episode number" (2026-10-13):
# a second episode or a version glued on (`Lost.S04E13E14`, `Oshi no Ko - S02E13v2`), a letter
# or the title glued on (`SpongeBob S01E01A`, `Friends.S06E03The.One`), four digits
# (`One Piece (1999) - s22e1089`) and a year for a season (`Smyth S2006E03`, MythBusters).
# The match is only cut at, so the character after the number may be consumed.
_SEASON_EP = re.compile(r"(?i)\bS(\d{1,2}|(?:19|20)\d{2})[\s._-]?E(\d{1,4})(?:\D|$)")
# `[sam] Hunter x Hunter (2011) - 006v2 [BD …]`: a version may follow the number.
_LOOSE_EP = re.compile(r"(?i)(?:^|\s)-\s(\d{2,4})(?:v\d)?(?:\s|$)")


def parse_release(filename):
    """Release filename -> {title, year, season, episode}."""
    stem = re.sub(r"\.(mkv|mp4|webm|avi|ts|m4v)$", "", filename, flags=re.I)
    # Bracketed groups are release-team noise, never title.
    stem = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", stem)
    stem = stem.replace(".", " ").replace("_", " ")

    season = episode = None
    match = _SEASON_EP.search(stem)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        stem = stem[: match.start()]
    else:
        # "Detective Conan - 1194" style absolute numbering.
        loose = _LOOSE_EP.search(stem)
        if loose:
            episode = int(loose.group(1))
            stem = stem[: loose.start()]

    year = None
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", stem)
    if years:
        year = int(years[-1])
        stem = stem[: stem.rfind(years[-1])]

    cut = _CUT.search(stem)
    if cut:
        stem = stem[: cut.start()]

    title = re.sub(r"\s+", " ", stem).strip(" -–—")
    return {"title": title, "year": year, "season": season, "episode": episode}


class Tmdb:
    def __init__(self, api_key, cache_path):
        self._key = api_key
        self._cache_path = cache_path
        self._cache = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as handle:
                    self._cache = json.load(handle)
            except ValueError:
                self._cache = {}

    def _save(self):
        with open(self._cache_path, "w", encoding="utf-8") as handle:
            json.dump(self._cache, handle)

    def _get(self, path, **params):
        params["api_key"] = self._key
        url = "%s/%s?%s" % (API, path.lstrip("/"), urllib.parse.urlencode(params))
        key = url.replace(self._key, "KEY")
        if key in self._cache:
            return self._cache[key]
        try:
            with urllib.request.urlopen(url, timeout=25) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429:  # rate limited: back off once, then give up
                time.sleep(2)
                try:
                    with urllib.request.urlopen(url, timeout=25) as response:
                        payload = json.load(response)
                except Exception:
                    return None
            else:
                return None
        except Exception:
            return None
        self._cache[key] = payload
        self._save()
        return payload

    # -- identification --------------------------------------------------

    def identify(self, parsed):
        """Return ('movie'|'tv', tmdb_id) or (None, None).

        A release with an SxxEyy is searched as TV first; anything else as a
        movie first, then TV. Guessing wrong here produces a confidently wrong
        library row, so a miss returns nothing rather than a near match.
        """
        title = parsed["title"]
        if not title:
            return None, None

        order = ("tv", "movie") if parsed["episode"] is not None else ("movie", "tv")
        for kind in order:
            params = {"query": title}
            if parsed["year"] and kind == "movie":
                params["year"] = parsed["year"]
            found = self._get("search/" + kind, **params)
            results = (found or {}).get("results") or []
            if results:
                return kind, results[0]["id"]
        return None, None

    def search(self, kind, title, year=None):
        """Every candidate for a title, not just the first.

        `identify()` deliberately takes the top hit and nothing else, because a
        near match in a catalog build is a confidently wrong library row. Fix
        Match is the opposite case: the user is choosing, so they get the list.
        """
        if not title:
            return []
        params = {"query": title}
        if year and kind == "movie":
            params["year"] = year
        found = self._get("search/" + kind, **params)
        return (found or {}).get("results") or []

    def details(self, kind, tmdb_id):
        return self._get(
            "%s/%s" % (kind, tmdb_id),
            append_to_response="credits,external_ids,content_ratings,release_dates",
        )

    def season(self, tmdb_id, season):
        """Season details: its own TMDB id, poster and air date.

        A real PMS season carries a `guid` of its own, and a client appears to
        need one -- the season list rendered empty without it. TMDB gives every
        season a real id, so this is a genuine identifier rather than a minted
        plex:// one.
        """
        return self._get("tv/%s/season/%s" % (tmdb_id, season))

    def episode(self, tmdb_id, season, episode):
        # external_ids gives the EPISODE's own imdb id. Without it every
        # episode of a show ends up sharing the show's guid, so a client cannot
        # tell them apart as distinct items.
        return self._get(
            "tv/%s/season/%s/episode/%s" % (tmdb_id, season, episode),
            append_to_response="external_ids",
        )


def plex_guid(kind, seed):
    """A canonical `plex://<kind>/<24 hex>` guid, minted deterministically.

    Verified against a real PMS: `guid` is ALWAYS the plex:// form at every
    level (show, season, episode, movie) and the external ids live in the
    `Guid[]` array -- on the show only; season and episode carry an empty one.
    rex previously put `imdb://` straight into `guid`, which is the legacy
    agent shape, and the client did not link show -> season -> episode from it.

    The hash is derived from a stable external identity, so the same content
    always produces the same guid across catalog rebuilds.
    """
    # "plexsim/" is the pre-rename namespace, frozen: changing it re-mints every id it seeds.
    return "plex://%s/%s" % (kind, hashlib.sha1(("plexsim/%s/%s" % (kind, seed)).encode()).hexdigest()[:24])


PROVIDER = "https://metadata.provider.plex.tv"


class PlexProvider:
    """Plex's own metadata agent, asked for the one thing TMDB cannot give: the item's
    real `plex://` guid.

    Cycle 70, measured with the native Plex for Android app (2026.9.0) in an emulator:
    a movie or show page whose `guid` is not one plex.tv knows never asks luma.plex.tv
    for its detail screen, so its action row (Watchlist, Rate, Mark as Watched, More)
    stays a skeleton and a show's Seasons block spins forever. Swapping rex's minted
    guid for the real one through the proxy, and nothing else, made the same page draw
    both. A real PMS gets the guid from this agent: the lab's outbound capture shows
    `POST /library/metadata/matches` and then the item's `children`. The match accepts
    the external id rex already holds (`{"type": 2, "guid": "tmdb://5920"}`), and a
    season's and an episode's guids are the parent's `children` by `index`.

    Needs a Plex account token (`PLEX_TOKEN`); without one, or offline, every lookup
    answers None and the minted guid stands. Answers are cached on disk like TMDB's.
    """

    KIND_TYPE = {"movie": 1, "show": 2}

    def __init__(self, token, cache_path, opener=None, client=None):
        self._token = token
        # This server's machineIdentifier: what a real PMS sends as X-Plex-Client-Identifier
        # on every agent call (plexlab's outbound capture, tests/fixtures/plexmeta_matches_s143.json).
        self._client = client
        self._cache_path = cache_path
        self._open = opener or self._urlopen
        self._cache = {}
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as handle:
                    self._cache = json.load(handle)
            except ValueError:
                self._cache = {}

    def _urlopen(self, method, path, body):
        headers = {
            "X-Plex-Token": self._token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Plex-Product": "Plex Media Server",
            "X-Plex-Provider-Version": "6.5.0",
        }
        if self._client:
            headers["X-Plex-Client-Identifier"] = self._client
        if method == "GET":
            # The agent pages at 50 (the lab capture); 500 is refused with a 400.
            headers.update({"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "50"})
        from claim import plextv_url
        request = urllib.request.Request(
            plextv_url(PROVIDER + path), method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)

    def _call(self, method, path, body=None):
        key = "%s %s %s" % (method, path, json.dumps(body, sort_keys=True) if body else "")
        if key in self._cache:
            return self._cache[key]
        try:
            payload = self._open(method, path, body)
        except Exception:
            return None
        self._cache[key] = payload
        if self._cache_path:
            with open(self._cache_path, "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle)
        return payload

    def match_row(self, kind, external_ids):
        """The provider's row for a movie or show named by an external id, or None."""
        for external in sorted(external_ids, key=lambda value: not value.startswith("tmdb://")):
            payload = self._call("POST", "/library/metadata/matches",
                                 {"type": self.KIND_TYPE[kind], "guid": external})
            rows = ((payload or {}).get("MediaContainer") or {}).get("Metadata") or []
            if rows and rows[0].get("guid", "").startswith("plex://%s/" % kind):
                return rows[0]
        return None

    def match(self, kind, external_ids):
        """(guid, slug) of the provider's item for a movie or show, or None."""
        row = self.match_row(kind, external_ids)
        return (row["guid"], row.get("slug")) if row else None

    @staticmethod
    def answer(row, kind):
        """{guid, kind, title, year, imdb, tmdb, thumb, art} of a matched row. The provider's
        own artwork rides along: a title only Plex knows has no TMDB poster (the production server, 2026-10-12:
        the Monogatari seasons Plex keeps as shows of their own drew the blank tile)."""
        ids = [guid.get("id", "") for guid in row.get("Guid") or []]
        imdb = next((value[7:] for value in ids if value.startswith("imdb://")), None)
        tmdb = next((value[7:] for value in ids if value.startswith("tmdb://")), None)
        return {"guid": row["guid"], "kind": kind, "title": row.get("title"),
                "year": row.get("year"), "imdb": imdb,
                "tmdb": int(tmdb) if tmdb and tmdb.isdigit() else None,
                "thumb": row.get("thumb") or None, "art": row.get("art") or None}

    def match_title(self, kind, title, year=None, filename=None):
        """The provider's own match for a release name, as a real PMS asks for it.

        plexlab's outbound capture (1.43.4) holds the body a scan sends:
        `{"excludeElements": "Media", "type": 1, "title": "Big Buck Bunny", "year": 2008,
        "filename": "Big Buck Bunny (2008)/Big Buck Bunny (2008) - 1080p.mkv", "hash":
        ...}`, answered with one row whose `Guid` list carries `imdb://` and `tmdb://`.
        The `hash` is the server's own file hash, which rex, reading no file, cannot compute;
        without it 184 of 198 labelled movies still matched right (identify.py).
        Returns the row's `answer`, or None.
        """
        body = {"excludeElements": "Media", "type": self.KIND_TYPE[kind], "title": title}
        if year:
            body["year"] = year
        if filename:
            body["filename"] = filename
        payload = self._call("POST", "/library/metadata/matches", body)
        rows = ((payload or {}).get("MediaContainer") or {}).get("Metadata") or []
        if not rows or not rows[0].get("guid", "").startswith("plex://%s/" % kind):
            return None
        return self.answer(rows[0], kind)

    def children(self, guid):
        """{index: guid} of a show's seasons or a season's episodes."""
        payload = self._call("GET", "/library/metadata/%s/children" % guid.rsplit("/", 1)[-1])
        rows = ((payload or {}).get("MediaContainer") or {}).get("Metadata") or []
        return {row["index"]: row["guid"] for row in rows if "index" in row and row.get("guid")}


def apply_plex_guids(items, provider):
    """Relink a built catalog's movie, show, season and episode guids to the provider's.

    Rows the provider cannot place keep the guid they have. Every copy of a relinked
    guid moves with it -- a season's `parentGuid`, an episode's `parentGuid` and
    `grandparentGuid` -- and a matched movie or show takes the provider's `slug` (a
    real slug is the agent's, not the slugified title: cycle 20), which
    a season's `parentSlug` and an episode's `grandparentSlug` repeat. Returns the
    number of rows relinked.
    """
    relinked = 0
    for row in items:
        kind = row.get("type")
        if kind not in ("movie", "show"):
            continue
        found = provider.match(kind, [g["id"] for g in row.get("Guid") or [] if g.get("id")])
        if not found:
            continue
        guid, slug = found
        row["guid"] = guid
        if slug:
            row["slug"] = slug
        relinked += 1
        if kind != "show":
            continue
        seasons = provider.children(guid)
        for season in items:
            if season.get("type") != "season" or season.get("parentRatingKey") != row["ratingKey"]:
                continue
            season["parentGuid"] = guid
            if slug:
                season["parentSlug"] = slug
            season_guid = seasons.get(season.get("index"))
            if season_guid:
                season["guid"] = season_guid
                relinked += 1
            episodes = provider.children(season_guid) if season_guid else {}
            for episode in items:
                if episode.get("type") != "episode" or episode.get("parentRatingKey") != season["ratingKey"]:
                    continue
                episode["grandparentGuid"] = guid
                episode["parentGuid"] = season.get("guid")
                if slug:
                    episode["grandparentSlug"] = slug
                episode_guid = episodes.get(episode.get("index"))
                if episode_guid:
                    episode["guid"] = episode_guid
                    relinked += 1
    return relinked


def _people(credits, job=None, department=None, limit=6):
    rows = []
    if job or department:
        for person in (credits or {}).get("crew") or []:
            if job and person.get("job") != job:
                continue
            if department and person.get("department") != department:
                continue
            rows.append({"tag": person.get("name")})
    else:
        for person in ((credits or {}).get("cast") or [])[:limit]:
            entry = {"tag": person.get("name"), "role": person.get("character")}
            if person.get("profile_path"):
                entry["thumb"] = IMAGE_BASE + "w185" + person["profile_path"]
            rows.append(entry)
    # De-duplicate while keeping order; TMDB lists a person once per credit.
    seen = set()
    unique = []
    for row in rows[:limit]:
        if row["tag"] in seen:
            continue
        seen.add(row["tag"])
        unique.append(row)
    return unique


def _content_rating(details, kind):
    if kind == "tv":
        for entry in (details.get("content_ratings") or {}).get("results") or []:
            if entry.get("iso_3166_1") in ("US", "GB") and entry.get("rating"):
                return entry["rating"]
    else:
        for entry in (details.get("release_dates") or {}).get("results") or []:
            if entry.get("iso_3166_1") != "US":
                continue
            for release in entry.get("release_dates") or []:
                if release.get("certification"):
                    return release["certification"]
    return None


def enrich(row, tmdb, parsed, chosen=None):
    """Add TMDB-derived Plex metadata to a catalog row, in place.

    Returns True when the release was identified. Everything written here is
    sourced from TMDB; nothing is invented, so an unidentified release keeps the
    probe-derived row it already had.

    `chosen` is `(kind, tmdb_id)` picked by a person rather than by the search:
    the Fix Match dialog in the UI. It skips identify() entirely, because the
    whole point of that dialog is that the search got it wrong.
    """
    kind, tmdb_id = chosen if chosen else tmdb.identify(parsed)
    if not kind:
        return False

    details = tmdb.details(kind, tmdb_id)
    if not details:
        return False

    show_title = details.get("title") or details.get("name") or parsed["title"]
    date = details.get("release_date") or details.get("first_air_date") or ""
    credits = details.get("credits") or {}
    external = details.get("external_ids") or {}
    imdb_id = external.get("imdb_id") or details.get("imdb_id")

    poster = details.get("poster_path")
    backdrop = details.get("backdrop_path")

    row["title"] = show_title
    # The load-time rule (`hierarchy.sort_title`), sent only when it differs: a bare
    # article strip gave a Fix Match row a titleSort equal to its title -- "Tears of
    # Steel" sorting as "Tears of Steel" -- which a real PMS never sends, and it
    # outlived an unmatch.
    derived = hierarchy.sort_title(show_title)
    if derived and derived != show_title:
        row["titleSort"] = derived
    else:
        row.pop("titleSort", None)
    row["summary"] = details.get("overview") or row.get("summary") or ""
    if date[:4].isdigit():
        row["year"] = int(date[:4])
        row["originallyAvailableAt"] = date
    if details.get("tagline"):
        row["tagline"] = details["tagline"]
    if details.get("vote_average"):
        row["rating"] = round(float(details["vote_average"]), 1)
        row["audienceRating"] = round(float(details["vote_average"]), 1)
    rating = _content_rating(details, kind)
    if rating:
        row["contentRating"] = rating

    companies = details.get("production_companies") or details.get("networks") or []
    if companies:
        row["studio"] = companies[0].get("name")

    genres = [{"tag": g["name"]} for g in details.get("genres") or []]
    if genres:
        row["Genre"] = genres
    countries = details.get("production_countries") or []
    if countries:
        row["Country"] = [{"tag": c.get("name")} for c in countries[:2]]

    cast = _people(credits)
    if cast:
        row["Role"] = cast
    directors = _people(credits, job="Director")
    if directors:
        row["Director"] = directors
    writers = _people(credits, department="Writing")
    if writers:
        row["Writer"] = writers

    episode_imdb = None
    episode_tmdb = None
    episode_tvdb = None
    # An episode overrides title/summary/art with its own, the way Plex does,
    # and keeps the show's identity in the grandparent fields.
    if kind == "tv" and parsed["season"] and parsed["episode"]:
        ep = tmdb.episode(tmdb_id, parsed["season"], parsed["episode"])
        if ep:
            row["title"] = ep.get("name") or row["title"]
            row["summary"] = ep.get("overview") or row["summary"]
            row["index"] = parsed["episode"]
            row["parentIndex"] = parsed["season"]
            row["grandparentTitle"] = show_title
            if ep.get("air_date"):
                row["originallyAvailableAt"] = ep["air_date"]
                if ep["air_date"][:4].isdigit():
                    row["year"] = int(ep["air_date"][:4])
            if ep.get("still_path"):
                backdrop = ep["still_path"]
            episode_tmdb = ep.get("id")
            episode_imdb = (ep.get("external_ids") or {}).get("imdb_id")
            episode_tvdb = (ep.get("external_ids") or {}).get("tvdb_id")

    # Identity. Plex's own agent mints plex:// guids; an external guid is what a
    # server without that agent supplies, and clients resolve it the same way.
    # An episode identifies as ITSELF, not as its show.
    externals = []
    if imdb_id:
        externals.append({"id": "imdb://" + imdb_id})
    externals.append({"id": "tmdb://%s" % tmdb_id})

    if episode_imdb or episode_tmdb:
        # An episode identifies as itself. Cycle 44 REFUTED the second half of the note
        # that used to stand here -- "carries no Guid[], matching a real PMS, which
        # leaves season and episode Guid arrays empty". Both references carry them:
        # plexlab's episode 8 is `imdb://tt0579539`, `tmdb://1048594`, `tvdb://297989`, and
        # 58 of 60 episodes of the 1.42.2 reference surveyed carry a `Guid` block. The ids were
        # already being fetched here and then thrown away.
        row["guid"] = plex_guid("episode", episode_imdb or "tmdb:%s" % episode_tmdb)
        episode_externals = []
        if episode_imdb:
            episode_externals.append({"id": "imdb://" + episode_imdb})
        if episode_tmdb:
            episode_externals.append({"id": "tmdb://%s" % episode_tmdb})
        if episode_tvdb:
            episode_externals.append({"id": "tvdb://%s" % episode_tvdb})
        if episode_externals:
            row["Guid"] = episode_externals
        else:
            row.pop("Guid", None)
    elif not (kind == "tv" and parsed["season"] and parsed["episode"]):
        row["guid"] = plex_guid("movie", imdb_id or "tmdb:%s" % tmdb_id)
        row["Guid"] = externals
    # An episode TMDB does not have keeps the guid it had: it was minted a MOVIE guid
    # and given its show's ids (the test host's Tom and Jerry S02E05 under a one-season
    # show).

    # The show's identity, for the parent row the hierarchy builds.
    row["_show_external_guids"] = externals

    # Artwork. Stored as absolute TMDB URLs on the row; the server resolves its
    # own /library/metadata/<rk>/thumb|art path to these and proxies the bytes.
    if poster:
        row["_poster_url"] = IMAGE_BASE + "w342" + poster
    if backdrop:
        row["_art_url"] = IMAGE_BASE + "w1280" + backdrop

    # Everything a show/season parent row needs, so the hierarchy can be built
    # without a second round of TMDB lookups.
    row["_kind"] = kind
    row["_tmdb_id"] = tmdb_id
    if kind == "tv":
        row["_show_title"] = show_title
        row["_show_summary"] = details.get("overview") or ""
        row["_show_year"] = int(date[:4]) if date[:4].isdigit() else None
        row["_show_poster"] = (IMAGE_BASE + "w342" + details["poster_path"]) if details.get("poster_path") else None
        row["_show_art"] = (IMAGE_BASE + "w1280" + details["backdrop_path"]) if details.get("backdrop_path") else None
        row["_show_guid"] = plex_guid("show", imdb_id or "tmdb:%s" % tmdb_id)
        row["_season"] = parsed["season"]
        row["_episode"] = parsed["episode"]
        if parsed["season"] is not None:
            season_details = tmdb.season(tmdb_id, parsed["season"]) or {}
            if season_details.get("id"):
                row["_season_guid"] = plex_guid("season", season_details["id"])
                # Cycle 44: and the season's EXTERNAL ids, which a real PMS carries and
                # this pipeline fetched and discarded -- plexlab's season 7 answers
                # `tmdb://3846` and `tvdb://15791`.
                season_externals = [{"id": "tmdb://%s" % season_details["id"]}]
                season_tvdb = (season_details.get("external_ids") or {}).get("tvdb_id")
                if season_tvdb:
                    season_externals.append({"id": "tvdb://%s" % season_tvdb})
                row["_season_guids"] = season_externals
            if season_details.get("poster_path"):
                row["_season_poster"] = IMAGE_BASE + "w342" + season_details["poster_path"]
            # Cycle 44: the season's own air-date YEAR is deliberately NOT stored as
            # the season's `year`. Measured over the 1.42.2 reference's whole show library: 689
            # of its 742 seasons carry `parentYear` and exactly ONE carries a `year` of its
            # own (and that one's sort title is its agent's arc name, not `Season N`).
            # A real Plex series agent gives a season the SHOW's year as `parentYear`;
            # writing TMDB's season air-date year into the season's own column put
            # every rex season into the 1-in-742 shape and took its `parentYear`
            # away, because `hierarchy` reads "has a year of its own" off that column.
            # The air date itself is kept: it is what `originallyAvailableAt` uses.
            air = season_details.get("air_date") or ""
            if air[:4].isdigit():
                row["_season_air"] = air
            if season_details.get("overview"):
                row["_season_summary"] = season_details["overview"]
    return True
