import sys, re
sys.path.insert(0, '/Users/bensarmiento/debridmediamanager/plexsim/harness/scripts')
import replay
MAP = {'1': '1007', '6': '20001', '7': '30001', '11': '30002', '8': '1001'}
_load = replay.load
replay.load = lambda c: [r for r in _load(c) if r['method'] in ('GET', 'HEAD')]
_fetch = replay.fetch
def fetch(base, token, rec, timeout=30):
    if 'kids' in base:
        return _fetch(base, token, rec, timeout)
    r = dict(rec)
    p = r['path']
    p = re.sub(r'^/(library|hubs)/metadata/(\d+)', lambda m: '/%s/metadata/%s' % (m.group(1), MAP.get(m.group(2), m.group(2))), p)
    p = re.sub(r'^/library/metadata/20001/theme/\d+', '/library/metadata/20001/theme/1', p)
    r['path'] = p
    return _fetch(base, token, r, timeout)
replay.fetch = fetch
sys.exit(replay.main())
