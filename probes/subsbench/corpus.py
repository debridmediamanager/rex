# Pick a stratified sample of real releases from rex-fun's library.
import os, sqlite3, json, random, re, sys
random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
db = sqlite3.connect('file:' + os.environ['REX_DB'] + '?mode=ro', uri=True)
imdb_of = {}
rows = []
for (b,) in db.execute("select body from row where coll='state:items'"):
    r = json.loads(b)
    if r.get('type') == 'show':
        imdb_of[r['ratingKey']] = next((x['id'][7:] for x in r.get('Guid', []) if x.get('id', '').startswith('imdb://')), None)
    if r.get('type') not in ('movie', 'episode'):
        continue
    g = r.get('guid', '')
    if not g.startswith('plex://'):
        continue
    imdb = next((x['id'][7:] for x in r.get('Guid', []) if x.get('id', '').startswith('imdb://')), None)
    m = (r.get('Media') or [{}])[0]
    p = (m.get('Part') or [{}])[0]
    if not p.get('file') or not p.get('size'):
        continue
    name = p['file'].rsplit('/', 1)[-1]
    if re.search(r'remux|2160p', name, re.I):  # keep the audio windows affordable
        continue
    rows.append(dict(key=r['ratingKey'], type=r['type'], guid=g, imdb=imdb, title=r.get('grandparentTitle') or r.get('title'),
                     year=r.get('year'), season=r.get('parentIndex'), episode=r.get('index'),
                     grandparentKey=r.get('grandparentRatingKey'),
                     file=name, size=p['size'], part=p['key'], duration=m.get('duration'),
                     videoCodec=m.get('videoCodec'), videoResolution=m.get('videoResolution'), fps=m.get('videoFrameRate'),
                     embedded=[s.get('languageCode') for s in p.get('Stream', []) if s.get('streamType') == 3]))
for r in rows:
    r['showImdb'] = imdb_of.get(r['grandparentKey'])
movies = [r for r in rows if r['type'] == 'movie']
eps = [r for r in rows if r['type'] == 'episode']
print(len(movies), len(eps), file=sys.stderr)
pick = random.sample(movies, min(60, len(movies))) + random.sample(eps, min(60, len(eps)))
json.dump(pick, sys.stdout)
