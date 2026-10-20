# Download every candidate subtitle for the corpus. Plex's through metadata.provider.plex.tv
# (the URL a PMS applies from), the Stremio addon's from its own url. Run on fun.
import json, os, sys, time, urllib.request, urllib.error, concurrent.futures as cf
claim = json.load(open(os.environ['REX_CLAIM']))
H = {'X-Plex-Token': claim['token'], 'X-Plex-Client-Identifier': claim['machine'],
     'User-Agent': 'PlexMediaServer/1.42.2.10156-f737b826c'}
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
jobs = []
for r in json.load(open(sys.argv[2])):
    for lang in ('en', 'pt-BR'):
        for i, s in enumerate(r.get('plex_' + lang) or []):
            jobs.append((f"{out}/{r['key']}_plex_{lang}_{i}", 'https://metadata.provider.plex.tv' + s['key'], H))
    for i, s in enumerate(r.get('stremio') or []):
        if s['lang'] in ('eng', 'pob'):
            jobs.append((f"{out}/{r['key']}_stremio_{s['lang']}_{i}", s['url'], {'User-Agent': 'rex/0.1'}))
def one(j):
    path, url, h = j
    if os.path.exists(path): return 0
    for attempt in range(8):
        try:
            b = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60).read()
            open(path, 'wb').write(b)
            if os.path.exists(path + '.err'): os.remove(path + '.err')
            time.sleep(1); return 0
        except urllib.error.HTTPError as e:
            if e.code != 429: err = str(e); break
            wait = int(e.headers.get('Retry-After') or 0) or 5 * 2 ** attempt
            print('429', url.split('/')[2], 'wait', wait, flush=True); time.sleep(min(wait, 300)); err = str(e)
        except Exception as e:
            err = str(e); break
    open(path + '.err', 'w').write(err); return 1
# Two lanes, one per host, each one request at a time.
plex = [j for j in jobs if 'plex.tv' in j[1]]; rest = [j for j in jobs if 'plex.tv' not in j[1]]
with cf.ThreadPoolExecutor(2) as ex:
    a = ex.submit(lambda: sum(map(one, plex))); b = ex.submit(lambda: sum(map(one, rest)))
    print(len(jobs), 'jobs', a.result() + b.result(), 'failed', flush=True)
