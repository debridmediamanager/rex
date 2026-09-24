"""Capture the per-locale tables `capture_i18n.py` cannot diff out, for a list of locales.

Cycle 66. `translations.json` is a positional diff of whole documents, which is right for
the server's vocabulary and wrong for four things plexsim composes itself:

  names      tag -> language name, off zen's Audio and Subtitle Language axes (both
             libraries); what a stream's `language` and titles are built from.
  leads      the inherit option of an item's language enum, off zen's show 5189
             (`:Account default`) and its first season (`:Series default`).
  words      `Forced`, `SDH`, `External` off zen's movies 1924, 2925 and 19813 (the
             stream's title less its own localised language name), and `Unknown` off
             plexscratch42's language-less audio track (movie 2).
  templates  `Recently Added in %s` and `Top Movies in %s` off zen's movie hub page, and
             `%s Collection` off plexscratch42's `collection.related` hub.

Cycles 59 and 60 captured the same four for the 28 catalogue languages; these are the
same reads, parameterised by locale. GET only; zen is paced.

    ZEN_TOKEN=... S42_TOKEN=... LOCALES=af,no,pt-BR python3 capture_locale_tables.py OUT
"""
import json
import os
import sys
import time
import urllib.request

ZEN = os.environ.get("ZEN_BASE", "http://zen:32400")
S42 = os.environ.get("S42_BASE", "http://100.85.222.1:32411")
LOCALES = os.environ["LOCALES"].split(",")
S42_COLLECTION = os.environ.get("S42_COLLECTION")


def get(base, token, path, language):
    time.sleep(0.3)
    headers = {"Accept": "application/json", "X-Plex-Token": token,
               "X-Plex-Container-Start": "0", "X-Plex-Container-Size": "500"}
    if language:
        headers["X-Plex-Language"] = language
    request = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as handle:
        return json.loads(handle.read())["MediaContainer"]


def zen(path, language):
    return get(ZEN, os.environ["ZEN_TOKEN"], path, language)


def s42(path, language):
    return get(S42, os.environ["S42_TOKEN"], path, language)


def subtitles(container):
    return [stream for media in container["Metadata"][0].get("Media") or []
            for part in media.get("Part") or [] for stream in part.get("Stream") or []
            if stream.get("streamType") == 3]


def setting(container, key):
    for row in (container["Metadata"][0].get("Preferences") or {}).get("Setting") or []:
        if row.get("id") == key:
            return row
    return {}


out = {}
for locale in [None] + LOCALES:
    code = locale or "en"
    entry = out.setdefault(code, {"names": {}, "leads": {}, "words": {}, "templates": {}})
    for section in ("1", "2"):
        for axis in ("audioLanguage", "subtitleLanguage"):
            for row in zen("/library/sections/%s/%s" % (section, axis), locale).get(
                    "Directory") or []:
                if row.get("key") and row.get("title"):
                    entry["names"].setdefault(row["key"], row["title"])
    show = zen("/library/metadata/5189?includePreferences=1", locale)
    entry["leads"][":Account default"] = setting(show, "audioLanguage").get(
        "enumValues", "").split("|")[0]
    season_key = zen("/library/metadata/5189/children", locale)["Metadata"][0]["ratingKey"]
    season = zen("/library/metadata/%s?includePreferences=1" % season_key, locale)
    entry["leads"][":Series default"] = setting(season, "audioLanguage").get(
        "enumValues", "").split("|")[0]
    for stream in subtitles(zen("/library/metadata/1924", locale)):
        if stream.get("forced") and stream.get("language"):
            entry["words"]["Forced"] = stream["displayTitle"].replace(
                stream["language"], "", 1).strip()
    for stream in subtitles(zen("/library/metadata/2925", locale)):
        if stream.get("hearingImpaired") and stream.get("language"):
            entry["words"]["SDH"] = stream["displayTitle"].replace(
                stream["language"], "", 1).strip()
    for stream in subtitles(zen("/library/metadata/19813", locale)):
        extended = stream.get("extendedDisplayTitle") or ""
        if extended.endswith(")") and "(" in extended:
            entry["words"]["External"] = extended.rsplit("(", 1)[1][:-1].split(" ", 1)[-1]
    movie = s42("/library/metadata/2", locale)["Metadata"][0]
    for media in movie.get("Media") or []:
        for part in media.get("Part") or []:
            for stream in part.get("Stream") or []:
                if stream.get("streamType") == 2 and not stream.get("languageTag"):
                    entry["words"]["Unknown"] = stream["displayTitle"].split(" (")[0]
    hubs = zen("/hubs/sections/1?count=1", locale).get("Hub") or []
    for hub in hubs:
        identifier = hub.get("hubIdentifier") or ""
        if identifier.startswith("movie.recentlyadded"):
            library = zen("/library/sections/1", None).get("title1") or "Zen Movies"
            if library in hub.get("title", ""):
                entry["templates"]["Recently Added in %s"] = hub["title"].replace(
                    library, "%s")
        if identifier.startswith("movie.genre."):
            genre_key = identifier.rsplit(".", 1)[-1]
            genres = {row.get("key"): row.get("title") for row in zen(
                "/library/sections/1/genre", locale).get("Directory") or []}
            genre = genres.get(genre_key)
            if genre and genre in hub.get("title", ""):
                entry["templates"]["Top Movies in %s"] = hub["title"].replace(genre, "%s")
    if S42_COLLECTION:
        related = s42("/library/collections/%s/related" % S42_COLLECTION, locale)
        title = s42("/library/collections/%s" % S42_COLLECTION, None)["Metadata"][0]["title"]
        for hub in related.get("Hub") or []:
            if hub.get("hubIdentifier") == "collection.related" and title in hub["title"]:
                entry["templates"]["%s Collection"] = hub["title"].replace(title, "%s")
    print(code, {key: len(value) for key, value in entry.items()}, flush=True)

open(sys.argv[1], "w").write(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True))
