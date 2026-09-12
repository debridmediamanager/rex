"""Identify a release against TMDB and build Plex metadata for it.

plexsim's rows previously carried only what the filename and an ffprobe gave:
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

_SEASON_EP = re.compile(r"(?i)\bS(\d{1,2})[\s._-]?E(\d{1,3})\b")
_LOOSE_EP = re.compile(r"(?i)(?:^|\s)-\s(\d{2,4})(?:\s|$)")


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
                with open(cache_path) as handle:
                    self._cache = json.load(handle)
            except ValueError:
                self._cache = {}

    def _save(self):
        with open(self._cache_path, "w") as handle:
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
    plexsim previously put `imdb://` straight into `guid`, which is the legacy
    agent shape, and the client did not link show -> season -> episode from it.

    The hash is derived from a stable external identity, so the same content
    always produces the same guid across catalog rebuilds.
    """
    return "plex://%s/%s" % (kind, hashlib.sha1(("plexsim/%s/%s" % (kind, seed)).encode()).hexdigest()[:24])


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


def enrich(row, tmdb, parsed):
    """Add TMDB-derived Plex metadata to a catalog row, in place.

    Returns True when the release was identified. Everything written here is
    sourced from TMDB; nothing is invented, so an unidentified release keeps the
    probe-derived row it already had.
    """
    kind, tmdb_id = tmdb.identify(parsed)
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
    row["titleSort"] = re.sub(r"(?i)^(the|a|an)\s+", "", show_title)
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

    # Identity. Plex's own agent mints plex:// guids; an external guid is what a
    # server without that agent supplies, and clients resolve it the same way.
    # An episode identifies as ITSELF, not as its show.
    externals = []
    if imdb_id:
        externals.append({"id": "imdb://" + imdb_id})
    externals.append({"id": "tmdb://%s" % tmdb_id})

    if episode_imdb or episode_tmdb:
        # An episode identifies as itself, and carries no Guid[] -- matching a
        # real PMS, which leaves season and episode Guid arrays empty.
        row["guid"] = plex_guid("episode", episode_imdb or "tmdb:%s" % episode_tmdb)
        row.pop("Guid", None)
    else:
        row["guid"] = plex_guid("movie", imdb_id or "tmdb:%s" % tmdb_id)
        row["Guid"] = externals

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
            if season_details.get("poster_path"):
                row["_season_poster"] = IMAGE_BASE + "w342" + season_details["poster_path"]
            air = season_details.get("air_date") or ""
            if air[:4].isdigit():
                row["_season_year"] = int(air[:4])
                row["_season_air"] = air
            if season_details.get("overview"):
                row["_season_summary"] = season_details["overview"]
    return True
