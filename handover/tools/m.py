#!/usr/bin/env python3
"""Read-only triage fetcher: one request to kids, fun and plexsim, summarised.

usage: m.py [--xml] [--raw N] [--rows N] [-H 'k: v'] [--only ref,fun,sim] REFPATH [FUNPATH] [SIMPATH]
A path of '-' skips that server; FUNPATH/SIMPATH default to REFPATH.
"""
import argparse, gzip, json, os, re, sys, urllib.error, urllib.request

WRITE_GET = re.compile(
    r'/:/(scrobble|unscrobble|rate|progress|timeline|plugins)|/refresh|/analyze|/emptyTrash|/optimize|'
    r'/clean|/match(\?|$)|/unmatch|/split|/merge|/actions/|/updater|/myplex/claim|/downloadQueue|'
    r'/playQueues|/security/token|/sync|/diagnostics|'
    r'/\:/prefs\?|/library/metadata/\d+/(poster|art|theme|logo)\?url|/library/metadata/\d+/(posters|arts)\?url|'
    r'/servers/|/resources\?includeHttps|/transcode/universal/(start|ping|stop)|/video/:/transcode')

SIM = os.environ.get('PLEXSIM_BASE', 'http://127.0.0.1:32553')
SERVERS = {'ref': ('http://kids:32400', os.environ['PLEXLAB_TOKEN']),
           'fun': ('http://fun:32400', os.environ['FUN_TOKEN']),
           'sim': (SIM, None)}


def fetch(name, path, xml, headers):
    base, tok = SERVERS[name]
    if name != 'sim' and WRITE_GET.search(path):
        return None, {}, b'REFUSED write-shaped GET'
    h = {'User-Agent': 'curl/8', 'X-Plex-Client-Identifier': 'plexsim-c8-triage', 'X-Plex-Product': 'Plex Web',
         'X-Plex-Platform': 'Chrome', 'Accept-Encoding': 'identity'}
    if not xml:
        h['Accept'] = 'application/json'
    if tok:
        h['X-Plex-Token'] = tok
    h.update(headers)
    req = urllib.request.Request(base + path, headers=h, method='GET')
    try:
        r = urllib.request.urlopen(req, timeout=60)
        st, hd, b = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        st, hd, b = e.code, dict(e.headers), e.read()
    except Exception as e:
        return None, {}, repr(e).encode()
    if hd.get('Content-Encoding') == 'gzip':
        b = gzip.decompress(b)
    return st, hd, b


def summarise(b, ct, rows):
    if 'json' in ct:
        try:
            mc = json.loads(b).get('MediaContainer', {})
        except Exception:
            return b[:300]
        attrs = {k: v for k, v in mc.items() if not isinstance(v, (list, dict))}
        out = ['  container: ' + json.dumps(attrs)[:600]]
        for k, v in mc.items():
            if isinstance(v, list):
                out.append('  %s[%d]' % (k, len(v)))
                for row in v[:rows]:
                    flat = {a: (x if not isinstance(x, (list, dict)) else '<%s>' % type(x).__name__)
                            for a, x in row.items()}
                    out.append('    ' + json.dumps(flat)[:500])
            elif isinstance(v, dict):
                out.append('  %s: %s' % (k, json.dumps(v)[:300]))
        return '\n'.join(out)
    return '  ' + b[:rows * 400].decode('utf-8', 'replace')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--xml', action='store_true')
    ap.add_argument('--rows', type=int, default=2)
    ap.add_argument('-H', action='append', default=[])
    a = ap.parse_args()
    paths = a.paths + [a.paths[0]] * (3 - len(a.paths))
    headers = dict((k.strip(), v.strip()) for k, v in (x.split(':', 1) for x in a.H))
    for name, path in zip(('ref', 'fun', 'sim'), paths):
        if path == '-':
            continue
        st, hd, b = fetch(name, path, a.xml, headers)
        ct = hd.get('Content-Type', '')
        print('[%s] %s -> %s %s len=%d' % (name, path, st, ct.split(';')[0], len(b)))
        print(summarise(b, ct, a.rows))


if __name__ == '__main__':
    main()
