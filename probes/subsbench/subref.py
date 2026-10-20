"""Align candidates against the release's own embedded text track instead of its audio:
the embedded cues in each window stand in for the speech mask. Compared with the audio
verdicts on the releases whose embedded track the meter extracted."""
import json, os, glob, collections
import numpy as np
import measure as M

cands = {r['key']: r for r in json.load(open('cands.json'))}
agree = collections.Counter()
for line in open('final.jsonl'):
    ref = json.loads(line)
    embs = sorted(glob.glob(f"{M.CACHE}/{ref['key']}.emb*.srt"))
    if not embs: continue
    item = cands[ref['key']]; dur = item['duration'] / 1000
    emb = M.cues(open(embs[0], encoding='utf-8', errors='replace').read())
    if len(emb) < 50: continue
    windows = []
    for f in M.FRACTIONS:
        st = round(dur * f, 1)
        n = int(M.WIN / M.FRAME); m = np.zeros(n, dtype=np.int8)
        for a, b in emb:
            i, j = max(0, int((a - st) / M.FRAME)), min(n, int((b - st) / M.FRAME))
            if j > i: m[i:j] = 1
        windows.append((st, m))
    for name, rv in ref['cands'].items():
        if name.startswith(('embedded', 'synthetic')): continue
        fn = name[5:] if name.startswith('null_') else f"{ref['key']}_{name}"
        cl = M.cues(M.decode(open(os.path.join('subsbench-files', fn), 'rb').read())[0])
        v = M.measure(windows, cl)
        agree[('null' if name.startswith('null_') else 'cand', rv['verdict'], v['verdict'])] += 1
        if rv['verdict'] == 'in_sync':
            agree[('syn-shift', v['verdict'], M.measure(windows, [(a + 2, b + 2) for a, b in cl])['verdict'])] += 1
            agree[('syn-fps', v['verdict'], M.measure(windows, [(a * 23.976 / 25, b * 23.976 / 25) for a, b in cl])['verdict'])] += 1
for k, v in sorted(agree.items()): print(k, v)
