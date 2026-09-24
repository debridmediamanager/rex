"""Capture the reference's own translation table, mechanically.

Cycle 16's first finding: a real PMS LOCALISES by `X-Plex-Language` (falling back to
`Accept-Language`). Rather than guess at translations, fetch each endpoint in English
and in every language the reference answers differently, walk the two JSON documents in
parallel, and record every string that changed AT THE SAME PATH. That gives an English
-> per-language map with no invention in it at all.

Cycle 17 widened the path list. §3.3 had carried "the MECHANISM is measured; the
COVERAGE is only as wide as this list" since cycle 16, and it was right: sweeping 87
paths found 119 (attribute, string) pairs the reference translates that the table did
not hold -- the whole library and item PREFERENCES family (every `label`, `summary` and
`enumValues` of the Edit Library dialog and the per-item Advanced pane), the seventeen
`/hubs/search` hub titles, the butler's task titles and descriptions, a GENERATED
`Episode N`, and `/query/parse`'s `fieldTitle`.

A string that differs between two ENGLISH fetches of the same path is NOT a
translation: the random actor/director shelf regenerates per request. Every path is
therefore fetched in English TWICE and only the stable strings are diffed.

Read-only: GET only.

    PLEXLAB_TOKEN=... python3 capture_i18n.py [OUT]
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("I18N_BASE", "http://kids:32400")
TOKEN = os.environ.get("I18N_TOKEN") or os.environ["PLEXLAB_TOKEN"]
# Cycle 59: the table is PER VERSION. It was captured off kids (1.43.4) and plexsim reports
# 1.42.2, whose catalogues word a few strings differently (Danish "Bibliotekets standard",
# Serbian, Ukrainian, Hebrew and Portuguese enum rows), so it is re-captured off zen and
# overlaid. zen holds different items, so the kids ratingKeys the paths name are REMAPPED:
# I18N_IDS='{"6": "11113", ...}' maps each kids id to the zen row of the same kind.
IDS = json.loads(os.environ.get("I18N_IDS") or "{}")

LANGUAGES = ['ar', 'ca', 'cs', 'da', 'de', 'es', 'et', 'fi', 'fr', 'he', 'hr', 'hu',
             'it', 'ja', 'ko', 'lt', 'nb', 'nl', 'pl', 'pt', 'ro', 'ru', 'sk', 'sl',
             'sr', 'sv', 'uk', 'zh']
# Cycle 66: Plex Web sends `no`, `af` and `pt-BR`, which have catalogues of their own, and a
# dozen locales (`bg`, `tr`, `zh-TW`, `en-GB`, `es-419`, ...) whose ICU country and language
# names follow the full locale. `I18N_LANGS` (comma-separated) captures those instead.
if os.environ.get('I18N_LANGS'):
    LANGUAGES = os.environ['I18N_LANGS'].split(',')

# Cycle 16's list.
PATHS = [
    '/library/sections/1/filters',
    '/library/sections/1/filters?includeAdvanced=1',
    '/library/sections/2/filters',
    '/library/sections/2/filters?includeAdvanced=1',
    '/library/sections/1/sorts',
    '/library/sections/1/sorts?includeAdvanced=1',
    '/library/sections/2/sorts',
    '/library/sections/2/sorts?includeAdvanced=1',
    '/library/sections/1',
    '/library/sections/2',
    '/library/sections/1/all?includeMeta=1&X-Plex-Container-Start=0&X-Plex-Container-Size=1',
    '/library/sections/2/all?includeMeta=1&X-Plex-Container-Start=0&X-Plex-Container-Size=1',
    '/hubs?X-Plex-Container-Start=0&X-Plex-Container-Size=20',
    '/hubs/promoted?contentDirectoryID=1&pinnedContentDirectoryID=1',
    '/hubs/promoted?contentDirectoryID=2&pinnedContentDirectoryID=2',
    '/:/prefs',
    '/library/sections/1/genre',
    '/library/sections/1/year',
    '/library/sections/1/decade',
    '/library/sections/1/contentRating',
    '/library/sections/1/resolution',
    '/library/sections/1/firstCharacter',
    '/library/sections/1/userRating',
    '/library/sections/2/network',
    '/library/sections/1/unwatched',
    '/library/metadata/6/children',
    '/library/metadata/8',
    '/library/metadata/1',
    '/playlists?includeMeta=1',
    '/library/sections',
    '/media/providers',
]

# Cycle 17's additions, each of which carries server-authored text the old list never
# asked for in another language.
PATHS += [
    # The library and item PREFERENCES family: label, summary and enumValues. This is
    # the Edit Library dialog and the per-item Advanced pane, and none of it was
    # covered -- 31 labels, 30 summaries and 19 enum lists on kids alone.
    '/library/sections/1/prefs',
    '/library/sections/2/prefs',
    '/library/sections/prefs?type=1&agent=tv.plex.agents.movie',
    '/library/sections/prefs?type=2&agent=tv.plex.agents.series',
    '/library/metadata/1?includePreferences=1',
    '/library/metadata/6?includePreferences=1',
    '/library/metadata/7?includePreferences=1',
    '/library/metadata/8?includePreferences=1',
    # The seventeen typed search hubs, whose titles the client lays the page out from.
    # An empty query is the one that shows every hub, hits included or not.
    '/hubs/search?query=a',
    '/hubs/search?query=zzzznothing',
    '/library/search?query=a',
    # The butler's task titles and descriptions.
    '/butler',
    # A GENERATED `Episode N`, the sibling of the `Season N` rule already in the table.
    '/library/metadata/11/children',
    '/library/metadata/11',
    # `/query/parse`'s own fieldTitle.
    '/library/sections/1/query/parse?type=1&year=2000&genre=1&unwatched=1',
    # The Meta at includeAdvanced, and the episode Meta's own Type titles.
    '/library/sections/1/all?includeMeta=1&includeAdvanced=1'
    '&X-Plex-Container-Start=0&X-Plex-Container-Size=1',
    '/library/sections/2/all?includeMeta=1&includeAdvanced=1&type=4'
    '&X-Plex-Container-Start=0&X-Plex-Container-Size=1',
    # The library subsets, each of which names itself in title2.
    '/library/sections/1/onDeck', '/library/sections/1/recentlyAdded',
    '/library/sections/1/newest', '/library/sections/1/recentlyViewed',
    '/library/sections/2/onDeck', '/library/sections/2/recentlyAdded',
    '/library/sections/2/newest', '/library/sections/2/recentlyViewedShows',
    '/library/sections/2/unwatched', '/library/sections/1/folder',
    # The remaining secondary axes of both library types.
    '/library/sections/1/collection', '/library/sections/1/director',
    '/library/sections/1/actor', '/library/sections/1/writer',
    '/library/sections/1/producer', '/library/sections/1/country',
    '/library/sections/1/studio', '/library/sections/1/label',
    '/library/sections/1/audioLanguage', '/library/sections/1/subtitleLanguage',
    '/library/sections/2/genre', '/library/sections/2/year',
    '/library/sections/2/firstCharacter', '/library/sections/2/userRating',
    '/library/sections/1/common?type=1',
    # The server surface.
    # NOT /system/agents: its Agent.Language list is REORDERED per request language
    # rather than translated, so a positional diff reads `code` "ja-JP" as a
    # translation of "vi-VN". The volatility control cannot see it, because the
    # order is stable between two ENGLISH fetches.
    '/', '/activities', '/devices', '/accounts',
    '/playlists/sorts', '/library/all?X-Plex-Container-Start=0&X-Plex-Container-Size=1',
    '/library/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=1',
    '/library/onDeck', '/status/sessions',
]


# Cycle 18's additions. §3.3 has carried "the mechanism is measured; the coverage is only
# as wide as this list" since cycle 16, and a third sweep found five more families.
PATHS += [
    # Every `firstCharacter` BUCKET names itself in `title2` -- "Start with 'A'" -- and
    # the QUOTE MARKS move with the language too (`Beginne mit „A“` in German), so the
    # string cannot be composed from a translated stem and the key. Each bucket is its
    # own entry; kids answers 27 of them on a movie library.
    *['/library/sections/1/firstCharacter/%s' % key for key in
      ['%23'] + [chr(c) for c in range(ord('A'), ord('Z') + 1)]],
    # `/query/parse` names every rule's field in `fieldTitle`. Cycle 17 captured the four
    # fields one query happened to carry; a client builds rules on all of them, and a
    # show library's are SCOPED ("Show Title", "Episode Year", "Date Episode Added").
    '/library/sections/1/query/parse?type=1&title=a&resolution=1080&contentRating=G'
    '&studio=x&decade=2000&actor=1&collection=1&label=1&audioLanguage=en'
    '&subtitleLanguage=en&duplicate=1&unmatched=1&hdr=1&addedAt>>=1&lastViewedAt>>=1'
    '&userRating>>=1&viewCount>>=1',
    '/library/sections/2/query/parse?type=2&show.title=a&episode.title=b&show.unwatched=1',
    '/library/sections/2/query/parse?type=4&episode.title=a&episode.year=2000'
    '&episode.unwatched=1&episode.resolution=1080&episode.addedAt>>=1',
    # `/filters` is keyed by the asked `type=`, and the types a movie library does not
    # hold still answer a list: the music and photo axes (Mood, Style, Record Label,
    # Camera Make, Aperture, Lens, Tag) are server vocabulary plexsim answers too.
    '/library/sections/1/filters?type=8', '/library/sections/1/filters?type=9',
    '/library/sections/1/filters?type=10', '/library/sections/1/filters?type=13',
    # The Add Library dialog's own prefs list for a MUSIC library: 24 labels, summaries
    # and enum lists no other type carries.
    '/library/sections/prefs?type=8&agent=tv.plex.agents.music',
    # The per-library hub page's own shelf titles ("Recently Released Movies",
    # "Rediscover", "Recently Watched Episodes"). This path also carries the RANDOM
    # genre and actor shelves, whose genre changes per request -- which is why the
    # volatility control below fetches the target language twice as well as English.
    '/hubs/sections/1?X-Plex-Container-Size=20',
    '/hubs/sections/2?X-Plex-Container-Size=20',
]


def remap(path):
    import re
    return re.sub(r'/library/metadata/(\d+)', lambda m: '/library/metadata/%s'
                  % IDS.get(m.group(1), m.group(1)), path)


# Cycle 60: the paths cycle 59's hunts found the table never held -- the Related and
# post-play shelves and the Manage Recommendations titles.
PATHS += [
    '/hubs/metadata/1/related',
    '/hubs/metadata/1/postplay',
    '/library/metadata/1?includeRelated=1',
    '/hubs/sections/1/manage',
    '/hubs/sections/2/manage',
]
# Cycle 60: `I18N_ONLY` narrows the run to paths containing any of its comma-separated
# words -- plexscratch42 is captured for `/:/prefs` and `/media/providers` alone, which
# zen's token cannot read in full and whose playlist pivot zen does not hold.
if os.environ.get('I18N_ONLY'):
    PATHS = [p for p in PATHS
             if any(word in p for word in os.environ['I18N_ONLY'].split(','))]
# Cycle 60: with `I18N_KEEP_SAME=1` a cell the target language leaves in ENGLISH is
# recorded too, as English -- the table's own "no translation" -- so a merge can remove a
# translation the other version carries. Cycle 59 merged only the changed cells, and 591
# cells kept 1.43.4's translation where 1.42.2 answers English.
KEEP_SAME = os.environ.get('I18N_KEEP_SAME') == '1'


def fetch(path, language=None):
    import time
    time.sleep(float(os.environ.get('I18N_DELAY', '0')))
    path = remap(path)
    # zen is production with a real library: every listing is paged. Cycle 61: and
    # `/:/prefs` PAGES too -- 20 of zen's 173 settings were all cycle 60 captured, which
    # it misread as the token's reach. `I18N_PAGE` widens the window for that path.
    headers = {'X-Plex-Token': TOKEN, 'Accept': 'application/json',
               'User-Agent': 'curl/8', 'X-Plex-Container-Start': '0',
               'X-Plex-Container-Size': os.environ.get('I18N_PAGE', '20')}
    if language:
        headers['X-Plex-Language'] = language
    request = urllib.request.Request(BASE + path, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=60) as handle:
            body = handle.read()
        return json.loads(body) if body else {}
    except Exception:
        return None


# A list item is keyed by its own identity where it has one. Cycle 18: the per-library
# hub page picks a random genre and a random actor per request, so index 3 of the English
# document and index 3 of the German one are DIFFERENT rows, and a positional diff reads
# every attribute of them -- `container`, `art`, an `audienceRatingImage` -- as a
# translation. Fetching each language twice does not catch it, because the pick is stable
# inside one language's pair. This is the general form of cycle 17's `/system/agents`
# exclusion, whose `Language` list is REORDERED per request language.
_IDENTITY = ('ratingKey', 'hubIdentifier', 'key', 'id', 'code', 'guid', 'filter')


def item_key(value, index):
    if isinstance(value, dict):
        for name in _IDENTITY:
            found = value.get(name)
            if isinstance(found, (str, int)) and not isinstance(found, bool):
                return '%s=%s' % (name, found)
    return index


def walk(node, path=()):
    """Every string in a document, keyed by its structural path."""
    if isinstance(node, dict):
        for key, value in node.items():
            for item in walk(value, path + (key,)):
                yield item
    elif isinstance(node, list):
        for index, value in enumerate(node):
            for item in walk(value, path + (item_key(value, index),)):
                yield item
    elif isinstance(node, str):
        yield path, node


# The English side is fetched once per path, twice, and the volatile strings dropped.
english = {}
for path in PATHS:
    first, second = fetch(path), fetch(path)
    if not isinstance(first, dict) or not isinstance(second, dict):
        print('skipped %s' % path, flush=True)
        continue
    left, right = dict(walk(first)), dict(walk(second))
    english[path] = {key: value for key, value in left.items()
                     if right.get(key) == value and value.strip()}

table = {}
for language in LANGUAGES:
    changed = 0
    for path, stable in english.items():
        other, again = fetch(path, language), fetch(path, language)
        if not isinstance(other, dict) or not isinstance(again, dict):
            continue
        seen_all, repeat = dict(walk(other)), dict(walk(again))
        # Cycle 18: a string that differs between two fetches IN THE TARGET LANGUAGE is
        # not a translation either. `/hubs/sections/{id}` picks a random genre and a
        # random actor per request, so its shelf titles read as translations of each
        # other ("Top Movies in Drama" -> "Top-Filme in Action") whenever the two
        # English fetches happen to agree.
        seen_all = {key: value for key, value in seen_all.items()
                    if repeat.get(key) == value}
        for key, value in stable.items():
            seen = seen_all.get(key)
            if seen is None or (seen == value and not KEEP_SAME):
                continue
            # Keyed by the ATTRIBUTE as well as the string: the reference translates
            # "By Genre" as a section PIVOT's `title` and leaves the same words alone
            # as a secondary axis's `title2`, so a flat English map would
            # over-translate. The attribute is the last dict key on the path.
            attribute = next((piece for piece in reversed(key)
                              if isinstance(piece, str) and '=' not in piece), "")
            table.setdefault(attribute, {}).setdefault(value, {})[language] = seen
            changed += 1
    print('%s %d strings' % (language, changed), flush=True)

out = sys.argv[1] if len(sys.argv) > 1 else 'translations.json'
payload = {
    '_provenance': ('Captured from ' + os.environ.get('I18N_WHO', 'the plexlab reference (Plex 1.43.4)') + ' by fetching each '
                    'endpoint in English and in each language the server answers '
                    'differently, then diffing the two documents string by string at the '
                    'same structural path. English key -> language code -> the '
                    "reference's own translation. Nothing here is invented. Cycle 16 "
                    'captured 31 paths; cycle 17 widened the list to cover the library '
                    'and item preferences, the search hubs, the butler and a generated '
                    'episode title, and dropped the strings that differ between two '
                    'English fetches of the same path. Cycle 18 added the '
                    'firstCharacter buckets, /query/parse fieldTitles, the '
                    'music and photo filter axes, a music library prefs list '
                    'and the per-library hub page, and dropped the strings '
                    'that differ between two fetches in the TARGET language '
                    'as well.'),
    'strings': {attribute: {source: languages
                            for source, languages in sorted(values.items()) if languages}
                for attribute, values in sorted(table.items()) if values},
}
open(out, 'w').write(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
print('%d attributes, %d string entries, %d languages -> %s'
      % (len(payload['strings']),
         sum(len(value) for value in payload['strings'].values()),
         len(LANGUAGES), out))
