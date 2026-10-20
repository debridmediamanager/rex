# Record a real PMS applying a subtitle search result (plexlab, Sintel), then undo it.
import json, subprocess, threading, time, urllib.request, urllib.parse
T = subprocess.run(['docker', 'exec', 'plexlab-plex', 'sh', '-c',
    "grep -o 'PlexOnlineToken=\"[^\"]*\"' '/config/Library/Application Support/Plex Media Server/Preferences.xml'"],
    capture_output=True, text=True).stdout.split('"')[1]
P = 'http://127.0.0.1:32400'
H = {'Accept': 'application/json', 'X-Plex-Token': T, 'X-Plex-Client-Identifier': 'rex-subs-probe'}
def req(method, path, **q):
    url = P + path + ('?' + urllib.parse.urlencode(q) if q else '')
    r = urllib.request.urlopen(urllib.request.Request(url, method=method, headers=H), timeout=60)
    return r.status, dict(r.headers), r.read().decode()
events = []
def listen():
    r = urllib.request.urlopen(urllib.request.Request(P + '/:/eventsource/notifications?filters=activity', headers=H), timeout=120)
    for line in r:
        line = line.decode().strip()
        if line: events.append((round(time.time(), 2), line))
threading.Thread(target=listen, daemon=True).start(); time.sleep(1)
before = json.loads(req('GET', '/library/metadata/3')[2])['MediaContainer']['Metadata'][0]['Media'][0]['Part'][0]['Stream']
st, hd, body = req('GET', '/library/metadata/3/subtitles', language='en')
cand = json.loads(body)['MediaContainer']['Stream'][0]
print('CANDIDATE', json.dumps(cand))
q = dict(key=cand['key'], codec=cand['codec'], language=cand['languageCode'], hearingImpaired=0, forced=0,
         providerTitle=cand['providerTitle'], mediaItemID=json.loads(req('GET', '/library/metadata/3')[2])['MediaContainer']['Metadata'][0]['Media'][0]['id'])
print('PUT QUERY', q)
st, hd, body = req('PUT', '/library/metadata/3/subtitles', **q)
print('PUT', st, json.dumps(hd), repr(body[:500]))
time.sleep(8)
after = json.loads(req('GET', '/library/metadata/3')[2])['MediaContainer']['Metadata'][0]['Media'][0]['Part'][0]['Stream']
new = [s for s in after if s['id'] not in {b['id'] for b in before}]
print('NEW STREAMS', json.dumps(new))
print('ALL SUB STREAMS AFTER', json.dumps([s for s in after if s['streamType'] == 3]))
for t, e in events: print('EVENT', t, e)
if new:
    s = new[0]
    st, hd, body = req('GET', s['key']); print('STREAM GET', st, hd.get('Content-Type'), len(body), repr(body[:120]))
    for s in new:
        print('DELETE', req('DELETE', f"/library/streams/{s['id']}")[0])
final = json.loads(req('GET', '/library/metadata/3')[2])['MediaContainer']['Metadata'][0]['Media'][0]['Part'][0]['Stream']
print('RESTORED', [s['id'] for s in final] == [s['id'] for s in before])
