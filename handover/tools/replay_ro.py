import sys
sys.path.insert(0, '/Users/bensarmiento/debridmediamanager/plexsim/harness/scripts')
import replay
_load = replay.load
def load(c):
    recs = _load(c)
    kept = [r for r in recs if r['method'] in ('GET', 'HEAD')]
    print('skipped non-GET:', [(r['method'], r['path']) for r in recs if r not in kept])
    return kept
replay.load = load
sys.exit(replay.main())
