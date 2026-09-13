# Runs probes/ui_surface.py with every non-GET/HEAD request refused locally,
# on BOTH servers so the tables line up. Kids is read-only this cycle.
import sys
sys.path.insert(0, '/Users/bensarmiento/debridmediamanager/plexsim/probes')
import ui_surface
_orig = ui_surface.request
def guarded(base, token, method, path, timeout=30):
    if method not in ('GET', 'HEAD'):
        return 999, '', b'SKIPPED-NONGET'
    return _orig(base, token, method, path, timeout)
ui_surface.request = guarded
sys.exit(ui_surface.main())
