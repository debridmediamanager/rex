"""Read-only: compare the reference's library state with ref_cycle5_before.json."""
import os
import json, sys, urllib.request
T = os.environ["PLEXLAB_TOKEN"]; B = 'http://kids:32400'
def get(p):
    r = urllib.request.Request(B + p, headers={'X-Plex-Token': T, 'Accept': 'application/json',
                                               'User-Agent': 'curl/8'}, method='GET')
    return json.load(urllib.request.urlopen(r, timeout=30))['MediaContainer']
before = json.load(open(sys.argv[1]))
now = {}
now['sections'] = [[d['key'], d['title'], d.get('uuid'), [l['path'] for l in d.get('Location') or []]]
                   for d in get('/library/sections').get('Directory') or []]
def rows(p):
    return get(p).get('Metadata') or []
third = ('titleSort', 'originalTitle', 'contentRating', 'lastViewedAt', 'studio', 'tagline')
def listing(p, snap):
    out, diffs = [], []
    for r, old in zip(rows(p), snap or [None] * 99):
        base = [r.get('ratingKey'), r.get('title'), (r.get('summary') or '')[:40] if r.get('summary') is not None else None,
                r.get('year'), r.get('viewCount'), r.get('viewOffset'), r.get('userRating')]
        out.append(base)
        if old is not None:
            cand = [k for k in third if r.get(k) == old[2]]
            mine = [old[0], old[1], old[3], old[4], old[5], old[6], old[7]]
            if mine != base or not cand:
                diffs.append((old, base, cand))
    return out, diffs
report = {}
for name, path in (('all1', '/library/sections/1/all'), ('all2', '/library/sections/2/all')):
    got, diffs = listing(path, before[name])
    report[name] = (len(got), len(before[name]), diffs)
ep = [[r.get('ratingKey'), r.get('title'), r.get('viewCount'), r.get('viewOffset')]
      for r in rows('/library/metadata/6/allLeaves')]
report['episodes'] = ep == before['episodes'] or (ep, before['episodes'])
for name, path in (('sprefs1', '/library/sections/1/prefs'), ('sprefs2', '/library/sections/2/prefs'),
                   ('prefs', '/:/prefs')):
    try:
        got = [[s['id'], s.get('value')] for s in get(path).get('Setting') or []]
    except Exception as error:
        report[name] = 'ERROR %s %s' % (path, error); continue
    old = before[name]
    report[name] = 'same' if got == old else [(o, g) for o, g in zip(old, got) if o != g] + (
        ['length %d -> %d' % (len(old), len(got))] if len(old) != len(got) else [])
# Item settings are the Preferences block of the detail row; /library/metadata/{id}/prefs 404s.
for name, key in (('iprefs1', '1'), ('iprefs6', '6')):
    row = (get('/library/metadata/%s?includePreferences=1' % key).get('Metadata') or [{}])[0]
    got = [[s['id'], s.get('value')] for s in (row.get('Preferences') or {}).get('Setting') or []]
    report[name] = 'same' if got == before[name] else [(o, g) for o, g in zip(before[name], got) if o != g]
report['sections'] = now['sections'] == before['sections'] or (now['sections'], before['sections'])
report['playlists'] = len(get('/playlists').get('Metadata') or []) == len(before['playlists'])
report['collections1'] = len(get('/library/sections/1/collections').get('Metadata') or []) == len(before['collections1'])
for k, v in report.items():
    print(k, json.dumps(v, default=str)[:600])
