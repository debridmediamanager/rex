#!/usr/bin/env python3
import json, sys
from collections import defaultdict

sim = {r['probe']: r for r in json.load(open('out_sim.json'))['results']}
ref = {r['probe']: r for r in json.load(open('out_ref.json'))['results']}
fun = {r['probe']: r for r in json.load(open('out_fun.json'))['results']}

mode = sys.argv[1] if len(sys.argv) > 1 else 'all'

def flat(rec):
    """-> {(cls, tag): set(attrs)}, {(cls, tag, childtag): set(attrs)}, {(cls,tag,childtag,grandtag): set}"""
    a = defaultdict(set); c = defaultdict(set); g = defaultdict(set); ctags = defaultdict(set)
    for o in rec.get('objects', []):
        if 'attrs' not in o: continue
        k = (o['cls'], o['tag'])
        a[k] |= set(o['attrs'])
        for ct, cd in (o.get('children') or {}).items():
            ctags[k].add(ct)
            c[(o['cls'], o['tag'], ct)] |= set(cd['attrs'])
            for gt, gd in (cd.get('children') or {}).items():
                g[(o['cls'], o['tag'], ct, gt)] |= set(gd['attrs'])
    return a, c, g, ctags

if mode in ('errors', 'all'):
    print('=== probes whose raise/no-raise status differs (sim vs ref) ===')
    for p in ref:
        if p not in sim: continue
        se, re_ = sim[p]['error'], ref[p]['error']
        fe = fun.get(p, {}).get('error')
        if bool(se) != bool(re_):
            print(f'- {p}\n    sim: {se}\n    ref: {re_}\n    fun: {fe}')
        elif se and re_ and se.split(':')[0] != re_.split(':')[0]:
            print(f'- {p} (different exception type)\n    sim: {se}\n    ref: {re_}\n    fun: {fe}')
    print()
    print('=== probes where sim count differs and the difference is 0 vs >0 ===')
    for p in ref:
        if p not in sim: continue
        sc, rc = sim[p].get('count'), ref[p].get('count')
        fc = fun.get(p, {}).get('count')
        if sc is not None and rc is not None and ((sc == 0) != (rc == 0)):
            print(f'- {p}: sim={sc} ref={rc} fun={fc}')
    print()
    print('=== value probes that differ in kind (None vs value / type) ===')
    for p in ref:
        if p not in sim: continue
        sv, rv = sim[p].get('value'), ref[p].get('value')
        fv = fun.get(p, {}).get('value')
        if sv is None or rv is None: continue
        def kind(v):
            if v.startswith("'MISSING'"): return 'MISSING'
            if v == 'None': return 'None'
            if v in ('True', 'False'): return 'bool'
            if v.startswith('('): return 'tuple'
            try: float(v); return 'num'
            except Exception: return 'str'
        if kind(sv) != kind(rv):
            print(f'- {p}\n    sim: {sv[:200]}\n    ref: {rv[:200]}\n    fun: {str(fv)[:200]}')
    print()

if mode in ('attrs', 'all'):
    print('=== attribute / child differences per probe (ref-only = plexsim omits; sim-only = plexsim adds) ===')
    for p in ref:
        if p not in sim: continue
        if sim[p]['error'] or ref[p]['error']: continue
        sa, sc, sg, sct = flat(sim[p]); ra, rc, rg, rct = flat(ref[p]); fa, fc, fg, fct = flat(fun.get(p, {}))
        lines = []
        for k in sorted(set(sa) | set(ra)):
            if k not in sa:
                lines.append(f'  element only on ref: {k}'); continue
            if k not in ra:
                lines.append(f'  element only on sim: {k}'); continue
            ro = ra[k] - sa[k]; so = sa[k] - ra[k]
            if ro:
                fk = fa.get(k, set())
                lines.append(f'  {k} ref-only attrs: ' + ', '.join(f'{x}{"*" if x in fk else ""}' for x in sorted(ro)))
            if so:
                fk = fa.get(k, set())
                lines.append(f'  {k} sim-only attrs: ' + ', '.join(f'{x}{"*" if x in fk else ""}' for x in sorted(so)))
            rct_ = rct.get(k, set()); sct_ = sct.get(k, set())
            if rct_ - sct_:
                lines.append(f'  {k} ref-only children: ' + ', '.join(f'{x}{"*" if x in fct.get(k, set()) else ""}' for x in sorted(rct_ - sct_)))
            if sct_ - rct_:
                lines.append(f'  {k} sim-only children: ' + ', '.join(f'{x}{"*" if x in fct.get(k, set()) else ""}' for x in sorted(sct_ - rct_)))
        for k in sorted(set(sc) & set(rc)):
            ro = rc[k] - sc[k]; so = sc[k] - rc[k]
            fk = fc.get(k, set())
            if ro: lines.append(f'  child {k} ref-only attrs: ' + ', '.join(f'{x}{"*" if x in fk else ""}' for x in sorted(ro)))
            if so: lines.append(f'  child {k} sim-only attrs: ' + ', '.join(f'{x}{"*" if x in fk else ""}' for x in sorted(so)))
        for k in sorted(set(sg) & set(rg)):
            ro = rg[k] - sg[k]; so = sg[k] - rg[k]
            fk = fg.get(k, set())
            if ro: lines.append(f'  grandchild {k} ref-only attrs: ' + ', '.join(f'{x}{"*" if x in fk else ""}' for x in sorted(ro)))
            if so: lines.append(f'  grandchild {k} sim-only attrs: ' + ', '.join(f'{x}{"*" if x in fk else ""}' for x in sorted(so)))
        if lines:
            print(f'- {p}  (sim n={sim[p].get("count")} ref n={ref[p].get("count")} fun n={fun.get(p, {}).get("count")})')
            print('\n'.join(lines))
    print('\n(* = also present on fun for the same probe)')
