#!/usr/bin/env python3
"""Guarded HTTP client for a write hunt, from cycle 10.

WRITES: only plexscratch (100.85.222.1:32410) and plexsim (127.0.0.1:32581).
READS of kids (kids:32400) and fun (fun:32400): GET/HEAD only, and never a
write-shaped GET path (WRITE_GET below). Anything else raises before a socket opens.
"""
import gzip, json, os, re, sys, threading, time, urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = 'http://100.85.222.1:32410'
SIM = os.environ.get('PLEXSIM_BASE', 'http://127.0.0.1:32581')
KIDS = 'http://kids:32400'
FUN = 'http://fun:32400'
ALLOWED_WRITE_HOSTS = {'100.85.222.1:32410', SIM.split('//', 1)[-1]}
READONLY_HOSTS = {'kids:32400', 'fun:32400'}

WRITE_GET = re.compile(
    r'/:/(scrobble|unscrobble|rate|progress|timeline|plugins)|/refresh|/analyze|/emptyTrash|/optimize|'
    r'/clean|/match(\?|$)|/unmatch|/split|/merge|/actions/|/updater|/myplex/claim|/downloadQueue|'
    r'/playQueues|/security/token|/sync|/diagnostics|'
    r'/\:/prefs\?|/library/metadata/\d+/(poster|art|theme|logo)\?url|/library/metadata/\d+/(posters|arts)\?url|'
    r'/servers/|/resources\?includeHttps|/transcode/universal/(start|ping|stop)|/video/:/transcode|'
    r'/websockets|/eventsource|/butler|/prefs\?')


def _token():
    with open(os.path.join(HERE, 'scratch.token')) as f:
        return f.read().strip()


BASES = {'scratch': SCRATCH, 'sim': SIM, 'kids': KIDS, 'fun': FUN}
TOKENS = {'scratch': _token(), 'sim': None,
          'kids': os.environ.get('PLEXLAB_TOKEN'), 'fun': os.environ.get('FUN_TOKEN')}

HDR = {
    'User-Agent': 'curl/8',
    'X-Plex-Client-Identifier': 'plexsim-c10-writes',
    'X-Plex-Product': 'Plex Web',
    'X-Plex-Version': '4.147.1',
    'X-Plex-Platform': 'Chrome',
    'X-Plex-Platform-Version': '128.0',
    'X-Plex-Device': 'OSX',
    'X-Plex-Device-Name': 'Chrome',
    'Accept-Encoding': 'identity',
}

LOG = []


def guard(url, method):
    host = urllib.parse.urlsplit(url).netloc
    path = urllib.parse.urlsplit(url).path + ('?' + urllib.parse.urlsplit(url).query if urllib.parse.urlsplit(url).query else '')
    if host in ALLOWED_WRITE_HOSTS:
        return
    if host in READONLY_HOSTS:
        if method not in ('GET', 'HEAD'):
            raise RuntimeError('REFUSED: %s to read-only host %s' % (method, host))
        if WRITE_GET.search(path):
            raise RuntimeError('REFUSED: write-shaped GET %s to read-only host %s' % (path, host))
        return
    raise RuntimeError('REFUSED: request to unknown host %s' % host)


def req(target, method, path, headers=None, body=None, xml=False, timeout=60):
    base = BASES[target]
    url = base + path
    guard(url, method)
    h = dict(HDR)
    if not xml:
        h['Accept'] = 'application/json'
    tok = TOKENS[target]
    if tok:
        h['X-Plex-Token'] = tok
    if headers:
        h.update(headers)
    data = body.encode() if isinstance(body, str) else body
    r = urllib.request.Request(url, method=method, headers=h, data=data)
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        st, hd, b = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        st, hd, b = e.code, dict(e.headers), e.read()
    except Exception as e:
        st, hd, b = None, {}, repr(e).encode()
    if hd.get('Content-Encoding') == 'gzip':
        b = gzip.decompress(b)
    j = None
    ct = hd.get('Content-Type', '') or ''
    if b and 'json' in ct:
        try:
            j = json.loads(b)
        except Exception:
            j = None
    LOG.append(dict(target=target, method=method, path=path, status=st, ct=ct, len=len(b), ms=int((time.time()-t0)*1000)))
    return st, hd, b, j


def mc(j):
    return (j or {}).get('MediaContainer', {})


def rows(j, key='Metadata'):
    return mc(j).get(key) or []


def detail(target, rk, extra=''):
    st, hd, b, j = req(target, 'GET', '/library/metadata/%s?includeExternalMedia=1%s' % (rk, extra))
    r = rows(j)
    return (r[0] if r else None), st


def redact(s):
    s = re.sub(r'X-Plex-Token=[^&"\s]+', 'X-Plex-Token=REDACTED', s)
    for t in TOKENS.values():
        if t:
            s = s.replace(t, 'REDACTED')
    return s


def show(label, st, hd, b, j, maxlen=700):
    ct = (hd.get('Content-Type') or '').split(';')[0]
    txt = b.decode('utf-8', 'replace')
    if j is not None:
        txt = json.dumps(j, sort_keys=False)
    print('  [%s] %s %s len=%d' % (label, st, ct or 'no-ctype', len(b)))
    if b:
        print('     ' + redact(txt)[:maxlen])


def both(method, spath, simpath=None, headers=None, body=None, xml=False, maxlen=700, quiet=False, targets=('scratch', 'sim')):
    if simpath is None:
        simpath = spath
    out = {}
    for tgt in targets:
        p = simpath if tgt == 'sim' else spath
        st, hd, b, j = req(tgt, method, p, headers=headers, body=body, xml=xml)
        out[tgt] = (st, hd, b, j)
        if not quiet:
            print('%s %s' % (method, redact(p)))
            show(tgt, st, hd, b, j, maxlen)
    return out


def flat(o, p='', out=None, maxlist=50):
    if out is None:
        out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            flat(v, f'{p}.{k}' if p else k, out, maxlist)
    elif isinstance(o, list):
        out[p + '[]len'] = len(o)
        for i, v in enumerate(o[:maxlist]):
            flat(v, f'{p}[{i}]', out, maxlist)
    else:
        out[p] = o
    return out


def diff(a, b, la='scratch', lb='sim', ignore=(), maxlist=50):
    fa, fb = flat(a, maxlist=maxlist), flat(b, maxlist=maxlist)
    def skip(k):
        return any(i in k for i in ignore)
    only_a = [k for k in fa if k not in fb and not skip(k)]
    only_b = [k for k in fb if k not in fa and not skip(k)]
    vd = [(k, fa[k], fb[k]) for k in fa if k in fb and fa[k] != fb[k] and not skip(k)]
    if only_a:
        print(f'  only in {la} ({len(only_a)}):')
        for k in only_a:
            print(f'    {k} = {redact(json.dumps(fa[k]))[:140]}')
    if only_b:
        print(f'  only in {lb} ({len(only_b)}):')
        for k in only_b:
            print(f'    {k} = {redact(json.dumps(fb[k]))[:140]}')
    if vd:
        print(f'  value diffs ({len(vd)}):')
        for k, x, y in vd:
            print(f'    {k}: {la}={redact(json.dumps(x))[:100]}  {lb}={redact(json.dumps(y))[:100]}')
    if not (only_a or only_b or vd):
        print('  IDENTICAL')
    return only_a, only_b, vd


# ---- XML structure -----------------------------------------------------------
def vclass(v):
    if v == "": return "empty"
    if v in ("true", "false"): return "truefalse"
    if re.fullmatch(r"-?\d+", v): return "int"
    if re.fullmatch(r"-?\d+\.\d+", v): return "float"
    return "str"


def xshape(root):
    shape = {}
    def walk(el, path):
        p = path + "/" + el.tag
        s = shape.setdefault(p, {"attrs": {}, "children": [], "n": 0, "text": False})
        s["n"] += 1
        if (el.text or "").strip(): s["text"] = True
        for k, v in el.attrib.items():
            s["attrs"].setdefault(k, set()).add(vclass(v))
        seq = []
        for c in el:
            if not seq or seq[-1] != c.tag:
                seq.append(c.tag)
        if seq and (len(seq) > len(s["children"])):
            s["children"] = seq
        for c in el:
            walk(c, p)
    walk(root, "")
    return shape


def xdiff(rb, sb, la='scratch', lb='sim'):
    """Structural diff of two XML bodies. Returns list of lines."""
    out = []
    if not rb and not sb:
        return ['  both empty']
    if not rb or not sb:
        return ['  EMPTY-vs-BODY %s=%d bytes %s=%d bytes' % (la, len(rb), lb, len(sb))]
    try:
        rt = ET.fromstring(rb)
    except Exception as e:
        return ['  PARSE %s: %s %r' % (la, e, rb[:120])]
    try:
        st = ET.fromstring(sb)
    except Exception as e:
        return ['  PARSE %s: %s %r' % (lb, e, sb[:120])]
    if rt.tag != st.tag:
        out.append('  ROOT %s <%s> %s <%s>' % (la, rt.tag, lb, st.tag))
    a, b = xshape(rt), xshape(st)
    a2 = {re.sub(r"^/[^/]+", "/ROOT", k): v for k, v in a.items()}
    b2 = {re.sub(r"^/[^/]+", "/ROOT", k): v for k, v in b.items()}
    for p, s in a2.items():
        if p not in b2:
            out.append('  ELEMENT only-%s %s (n=%d) attrs=%s' % (la, p, s['n'], list(s['attrs'])[:12]))
            continue
        t = b2[p]
        if s["children"] != t["children"] and s["children"] and t["children"]:
            out.append('  CHILD-ORDER %s: %s %s %s %s' % (p, la, s['children'], lb, t['children']))
        ra, sa = s["attrs"], t["attrs"]
        miss = [k for k in ra if k not in sa]
        extra = [k for k in sa if k not in ra]
        if miss: out.append('  ATTR only-%s %s: %s' % (la, p, miss))
        if extra: out.append('  ATTR only-%s %s: %s' % (lb, p, extra))
        for k in ra:
            if k in sa and ra[k] != sa[k]:
                out.append('  VCLASS %s@%s: %s %s %s %s' % (p, k, la, sorted(ra[k]), lb, sorted(sa[k])))
    for p, t in b2.items():
        if p not in a2:
            out.append('  ELEMENT only-%s %s (n=%d) attrs=%s' % (lb, p, t['n'], list(t['attrs'])[:12]))
    return out or ['  XML IDENTICAL (structure)']


# ---- notification websocket listener -----------------------------------------
class Listener:
    def __init__(self, target, filters=None):
        import websocket
        assert target in ('scratch', 'sim'), 'never open a socket on the reference'
        self.target = target
        base = BASES[target].replace('http://', 'ws://')
        q = ''
        if TOKENS[target]:
            q = '?X-Plex-Token=' + TOKENS[target]
        if filters is not None:
            q += ('&' if q else '?') + 'filters=' + filters
        url = base + '/:/websockets/notifications' + q
        guard(url.replace('ws://', 'http://'), 'GET')
        self.frames = []
        self.lock = threading.Lock()
        self.ws = websocket.WebSocketApp(url, on_message=self._on_msg, header=['X-Plex-Client-Identifier: plexsim-c10-ws', 'User-Agent: curl/8'])
        self.th = threading.Thread(target=self.ws.run_forever, kwargs=dict(ping_interval=0), daemon=True)
        self.th.start()
        time.sleep(1.0)

    def _on_msg(self, ws, msg):
        with self.lock:
            try:
                j = json.loads(msg)
            except Exception:
                j = msg
            self.frames.append((time.time(), j))

    def mark(self):
        with self.lock:
            return len(self.frames)

    def since(self, n, settle=2.0):
        time.sleep(settle)
        with self.lock:
            return [f for _, f in self.frames[n:]]

    def since_timed(self, n, settle=2.0):
        time.sleep(settle)
        with self.lock:
            return list(self.frames[n:])

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def summarize_frames(frames):
    out = []
    for f in frames:
        if not isinstance(f, dict):
            out.append(repr(f)[:120]); continue
        nc = f.get('NotificationContainer', f)
        t = nc.get('type')
        if t == 'timeline':
            for e in nc.get('TimelineEntry', []):
                out.append('timeline  type=%s state=%s ms=%s id=%s sec=%s title=%r%s' % (
                    e.get('type'), e.get('state'), e.get('metadataState'), e.get('itemID'), e.get('sectionID'), e.get('title'),
                    ('' if 'mediaState' not in e else ' mediaState=' + str(e.get('mediaState')))))
        elif t == 'activity':
            for e in nc.get('ActivityNotification', []):
                a = e.get('Activity', {})
                out.append('activity  %s type=%s progress=%s title=%r subtitle=%r ctx=%s' % (e.get('event'), a.get('type'), a.get('progress'), a.get('title'), a.get('subtitle'), json.dumps(a.get('Context')) if a.get('Context') else ''))
        elif t == 'playing':
            for e in nc.get('PlaySessionStateNotification', []):
                out.append('playing   state=%s rk=%s off=%s' % (e.get('state'), e.get('ratingKey'), e.get('viewOffset')))
        elif t == 'progress':
            for e in nc.get('ProgressNotification', []):
                out.append('progress  %r' % e.get('message'))
        elif t == 'status':
            for e in nc.get('StatusNotification', []):
                out.append('status    %r %r %r' % (e.get('title'), e.get('description'), e.get('notificationName')))
        elif t == 'setting':
            for e in nc.get('Setting', []):
                out.append('setting   %s' % json.dumps(e)[:200])
        else:
            out.append('%s %s' % (t, redact(json.dumps(nc))[:300]))
    return out


def frames_for(listener, fn, settle=3.0):
    n = listener.mark()
    r = fn()
    fr = listener.since(n, settle)
    return r, fr


def save(name, obj):
    with open(os.path.join(HERE, name), 'w') as f:
        json.dump(obj, f, indent=1, default=str)


if __name__ == '__main__':
    for t in ('scratch', 'sim'):
        st, hd, b, j = req(t, 'GET', '/identity')
        print(t, st, redact(b.decode()[:200]))
    # prove the guard
    for m, t, p in (('PUT', 'kids', '/library/metadata/1?title.value=x'), ('GET', 'kids', '/library/metadata/1/refresh'),
                    ('GET', 'fun', '/:/scrobble?key=1'), ('POST', 'fun', '/butler'), ('GET', 'kids', '/butler')):
        try:
            req(t, m, p)
            print('NOT REFUSED', m, t, p)
        except RuntimeError as e:
            print('ok refused:', e)
