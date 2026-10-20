# For each corpus release: its OpenSubtitles hash (read through rex), Plex's own subtitle
# search (the request a PMS makes, recorded on plexlab 2026-10-20) and the Stremio
# OpenSubtitles v3 addon's list.  Run on fun; reads only.
import json, os, struct, sys, urllib.request, urllib.parse, concurrent.futures as cf
claim = json.load(open(os.environ['REX_CLAIM']))
TOKEN, MACHINE = claim['token'], claim['machine']
REX = os.environ.get('REX_URL', 'http://127.0.0.1:32500')

def get(url, headers=None, rng=None, timeout=60):
    req = urllib.request.Request(url, headers=dict(headers or {}))
    if rng: req.add_header('Range', rng)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def os_hash(item):
    url = f"{REX}{item['part']}?X-Plex-Token={TOKEN}"
    size = item['size']
    head = get(url, rng='bytes=0-65535')
    tail = get(url, rng=f'bytes={size-65536}-{size-1}')
    h = size
    for chunk in (head, tail):
        for (q,) in struct.iter_unpack('<Q', chunk[:65536]):
            h = (h + q) & 0xFFFFFFFFFFFFFFFF
    return '%016x' % h

PLEX_HEADERS = {'Accept': 'application/json', 'User-Agent': 'PlexMediaServer/1.42.2.10156-f737b826c',
                'X-Plex-Client-Identifier': MACHINE, 'X-Plex-Provides': 'server',
                'X-Plex-Product': 'Plex Media Server', 'X-Plex-Token': TOKEN}

def plex(item, lang):
    q = [('filename', item['file']), ('guid', item['guid']), ('lang', lang),
         ('openSubtitlesHash', item['hash']), ('type', '1' if item['type'] == 'movie' else '4'),
         ('videoCodec', item['videoCodec'] or ''), ('videoQuality', (item['videoResolution'] or '') + 'p'),
         ('year', str(item['year'] or ''))]
    body = get('https://metadata.provider.plex.tv/library/streams/matches?' + urllib.parse.urlencode(q), PLEX_HEADERS)
    return json.loads(body)['MediaContainer'].get('Stream', [])

def stremio(item):
    if item['type'] == 'movie':
        kind, vid = 'movie', item['imdb']
    else:
        kind, vid = 'series', f"{item['showImdb']}:{item['season']}:{item['episode']}"
    if not vid or vid.startswith('None'):
        return None
    extra = urllib.parse.urlencode([('videoHash', item['hash']), ('videoSize', item['size']), ('filename', item['file'])])
    body = get(f'https://opensubtitles-v3.strem.io/subtitles/{kind}/{vid}/{extra}.json', {'User-Agent': 'rex/0.1'})
    return json.loads(body).get('subtitles', [])

def one(item):
    out = dict(item)
    try:
        out['hash'] = item['hash'] = os_hash(item)
    except Exception as e:
        out['error'] = f'hash: {e}'; return out
    for lang in ('en', 'pt-BR', 'pt'):
        try: out[f'plex_{lang}'] = plex(item, lang)
        except Exception as e: out[f'plex_{lang}'] = f'error: {e}'
    try: out['stremio'] = stremio(item)
    except Exception as e: out['stremio'] = f'error: {e}'
    return out

items = json.load(sys.stdin)
with cf.ThreadPoolExecutor(4) as ex:
    res = list(ex.map(one, items))
json.dump(res, sys.stdout)
