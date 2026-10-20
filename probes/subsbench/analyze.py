"""Score pick strategies against the sync meter's verdicts."""
import json, re, sys, glob, collections

cands = {r['key']: r for r in json.load(open(sys.argv[1]))}
meas = {}
for path in sys.argv[2:]:
    for line in open(path):
        r = json.loads(line); meas[r['key']] = r

TOK = re.compile(r'[a-z0-9]+')
NOISE = {'mkv', 'mp4', 'srt', 'avi', 'the', 'and', 'of', 'a', 'eng', 'en', 'por', 'pt', 'br', 'pob', 'subs', 'sub'}
SOURCES = {'bluray', 'bdrip', 'brrip', 'web', 'webrip', 'webdl', 'hdtv', 'dvdrip', 'remux', 'hdrip'}
SERVICES = {'amzn', 'nf', 'dsnp', 'hmax', 'atvp', 'pcok', 'hulu', 'max', 'pmtp', 'cr'}


def tokens(s):
    s = s.lower().replace('web-dl', 'webdl').replace('web dl', 'webdl')
    return [t for t in TOK.findall(s) if t not in NOISE]


def group(s):
    m = re.search(r'-([A-Za-z0-9]+)(?:\[[^\]]*\])?(?:\.[a-z]{2,4})?$', re.sub(r'\.(srt|mkv|mp4|ass)$', '', s.strip()))
    return m.group(1).lower() if m else None


def similarity(file, release):
    a, b = set(tokens(file)), set(tokens(release))
    score = len(a & b) / max(1, len(a | b))
    if group(file) and group(file) == group(release): score += 1
    sa, sb = a & SOURCES, b & SOURCES
    if sa and sb: score += 0.5 if sa & sb else -0.5
    va, vb = a & SERVICES, b & SERVICES
    if va and vb: score += 0.3 if va & vb else -0.3
    return score


def candidates(item, lang):
    """Every candidate for one language: (name in the meter, source, release name, extra)."""
    out = []
    for i, s in enumerate(item.get('plex_' + lang) or []):
        out.append(dict(name=f'plex_{lang}_{i}', source='plex', index=i, release=s.get('title', ''), score=s.get('score', 0),
                        hi=bool(s.get('hearingImpaired'))))
    sl = 'eng' if lang == 'en' else 'pob'
    j = 0
    for i, s in enumerate(item.get('stremio') or []):
        if s['lang'] != sl: continue
        out.append(dict(name=f'stremio_{sl}_{i}', source='stremio', index=j, release=s.get('movieReleaseName') or s.get('subtitleFileName', ''),
                        file=s.get('subtitleFileName', ''), hash=s.get('m') == 'h', fps=s.get('fpsMilli'), hi='-HI' in s.get('subtitleFileName', '') or '.HI.' in s.get('subtitleFileName', '')))
        j += 1
    return out


FPS = {'24p': 23976, 'PAL': 25000, 'NTSC': 29970}


def rex_rank(item, cs, m):
    def key(c):
        s = similarity(item['file'], c['release'])
        if c.get('file'): s = max(s, similarity(item['file'], c['file']))
        if c.get('hash'): s += 3
        want = FPS.get(item.get('fps'))
        if want and c.get('fps') and abs(c['fps'] - want) > 100: s -= 2
        if (m['cands'].get(c['name']) or {}).get('mojibake', 0) > 3: s -= 1
        if c.get('hi'): s -= 0.1
        return s
    return sorted(cs, key=key, reverse=True)


def verdict(m, c):
    v = m['cands'].get(c['name'])
    return v['verdict'] if v else 'missing'


def main():
    stats = collections.defaultdict(collections.Counter)
    control = collections.Counter()
    for key, m in meas.items():
        item = cands[key]
        for name, v in m['cands'].items():
            if name.startswith('embedded_'): control['embedded ' + v['verdict']] += 1
            if name.startswith('synthetic_shift2'): control['shift2 ' + v['verdict'] + (' %.2f' % v['offset'] if v['offset'] is not None else '')] += 1
            if name.startswith('synthetic_fps'): control['fps ' + v['verdict'] + ' ' + str(v['ratio'])] += 1
        for lang in ('en', 'pt-BR'):
            cs = [c for c in candidates(item, lang) if verdict(m, c) != 'missing']
            if not cs: continue
            measurable = [c for c in cs if verdict(m, c) != 'unknown']
            if not measurable: stats[lang]['all unknown'] += 1; continue
            st = stats[lang]; st['items'] += 1
            plex = [c for c in cs if c['source'] == 'plex']
            strem = [c for c in cs if c['source'] == 'stremio']
            picks = {
                'plex_first': plex[0] if plex else None,
                'plex_best_score': max(plex, key=lambda c: float(c['score'])) if plex else None,
                'stremio_first': strem[0] if strem else None,
                'rex_rank': rex_rank(item, cs, m)[0],
            }
            for k, c in picks.items():
                v = verdict(m, c) if c else 'none'
                st[f'{k} {v}'] += 1
                if c and (m['cands'][c['name']].get('mojibake', 0) > 3): st[f'{k} mojibake'] += 1
            vs = {verdict(m, c) for c in cs}
            st['any in_sync'] += 'in_sync' in vs
            st['any in_sync|shift|fps'] += bool(vs & {'in_sync', 'shift', 'fps'})
    print('CONTROL', dict(sorted(control.items())))
    for lang, st in stats.items():
        print('\n==', lang)
        for k in sorted(st): print(f'  {k:40} {st[k]}')


main()
