#!/usr/bin/env python3
"""Capture the reference's own per-language `Language` list for every agent.

Cycle 25: `/system/agents`' modern-agent `Language` rows are TITLED and ORDERED in the
client's language, and cycle 18 deliberately excluded this path from the translation
capture because the list is REORDERED per language, which a positional diff mis-keys.
So the list is captured whole, per language, rather than translated string by string:
the reference's own titles in the reference's own order, with nothing inferred and no
collation model.

GET only, against kids. Writes `agent_languages.json` beside the script.
"""
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

BASE = 'http://kids:32400'
TOKEN = os.environ['PLEXLAB_TOKEN']
# The 28 the reference answers differently, from `capture_i18n.py`, plus English and the
# no-header default -- which is NOT English: each language is then named in ITSELF.
LANGUAGES = ['ar', 'ca', 'cs', 'da', 'de', 'es', 'et', 'fi', 'fr', 'he', 'hr', 'hu',
             'it', 'ja', 'ko', 'lt', 'nb', 'nl', 'pl', 'pt', 'ro', 'ru', 'sk', 'sv',
             'th', 'tr', 'uk', 'zh']
MEDIA_TYPES = (1, 2, 8, 13)


def fetch(path, language=None):
    headers = {'X-Plex-Token': TOKEN, 'User-Agent': 'curl/8'}
    if language:
        headers['X-Plex-Language'] = language
    req = urllib.request.Request(BASE + path, headers=headers, method='GET')
    with urllib.request.urlopen(req, timeout=60) as handle:
        return handle.read()


def lists(language=None):
    """agent identifier -> [[code, title], ...], read from the XML.

    The JSON form keeps exactly ONE `Language` per agent -- the endpoint writes them as
    repeated KEYS rather than as an array (cycle 21) -- so this reads the XML.
    """
    out = {}
    for media_type in MEDIA_TYPES:
        root = ET.fromstring(fetch('/system/agents?mediaType=%d' % media_type, language))
        for agent in root.findall('Agent'):
            rows = [[row.get('code'), row.get('title')]
                    + ([row.get('default')] if row.get('default') else [])
                    for row in agent.findall('Language')]
            if rows:
                out.setdefault(agent.get('identifier'), rows)
    return out


def main():
    # All six modern agents share one list per language -- checked agent by agent in
    # every language -- so the fixture stores that one list, keyed by language. A row
    # is [code, title], or [code, title, default] for the ONE row per language that
    # carries `default="1"`: the preselected entry follows the client's language too.
    table = {'': lists(None), 'en': lists('en')}
    for language in LANGUAGES:
        table[language] = lists(language)
        print('%s: %d agents' % (language, len(table[language])), flush=True)
    # Collapse: most agents share one list per language, so store the DISTINCT lists and
    # point each agent at one. It keeps the fixture small and the rule visible.
    payload = {
        '_provenance': (
            'Captured from the plexlab reference (Plex 1.43.4) on 2026-09-15 by fetching '
            '/system/agents?mediaType=N in each language and reading the XML, which is '
            'the only form that carries every Language row (the JSON writes them as '
            'repeated keys). The empty key is the NO-HEADER default, which is not '
            'English: each language is named in itself there. The ORDER is the '
            "reference's own -- it collates by the localised title -- and is stored "
            'rather than modelled, because ICU collation is not implemented here.'),
        'agents': table,
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent_languages.json')
    with open(out, 'w') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
    print('%d languages -> %s' % (len(table), out))


if __name__ == '__main__':
    main()
