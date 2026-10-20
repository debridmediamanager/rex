"""Export the sync corpus as a fixture: no subtitle text, only cue timings, each window's
energy mask (what rex computes: FFmpeg highpass 300 / lowpass 3400, RMS per 512 samples,
above the window's median dB) and the Silero meter's verdict per candidate as the truth."""
import json, os, sys, base64, gzip, re
import numpy as np
import measure as M
import variant  # noqa: energy_mask lives there; argv is patched below

cands = {r['key']: r for r in json.load(open('cands.json'))}
items = []
for line in open('final.jsonl'):
    ref = json.loads(line)
    item = cands[ref['key']]
    dur = item['duration'] / 1000
    windows = []
    for f in M.FRACTIONS:
        st = round(dur * f, 1)
        mask = variant.energy_mask(f"{M.CACHE}/{item['key']}_{int(st)}.pcm", M.WIN)
        windows.append(dict(start=st, energy=base64.b64encode(np.packbits(mask).tobytes()).decode(), frames=int(len(mask))))
    keep = [(w['start'] - 40 - 0.06 * w['start'], w['start'] + M.WIN + 40 + 0.06 * w['start']) for w in windows]
    out_c = []
    for lang in ('en', 'pt-BR'):
        sl = 'eng' if lang == 'en' else 'pob'
        rows = [(f'plex_{lang}_{i}', 'plex', s) for i, s in enumerate(item.get('plex_' + lang) or [])]
        rows += [(f'stremio_{sl}_{i}', 'stremio', s) for i, s in enumerate(item.get('stremio') or []) if s['lang'] == sl]
        for name, source, s in rows:
            v = ref['cands'].get(name)
            path = f"subsbench-files/{item['key']}_{name}"
            if not v or not os.path.exists(path):
                continue
            text, moj = M.decode(open(path, 'rb').read())
            cl, prev = [], 0
            for a, b in M.cues(text):
                if any(lo <= a <= hi for lo, hi in keep):
                    for t in (round(a * 100), round(b * 100)):
                        cl.append(t - prev); prev = t
            meta = dict(name=name, lang=lang, source=source, mojibake=moj, cues=cl,
                        truth=dict(verdict=v['verdict'], ratio=v.get('ratio'), offset=v.get('offset')))
            if source == 'plex':
                meta.update(release=s.get('title', ''), score=s.get('score'), hearingImpaired=bool(s.get('hearingImpaired')))
            else:
                meta.update(release=s.get('movieReleaseName') or '', file=s.get('subtitleFileName', ''),
                            hashMatch=s.get('m') == 'h', fpsMilli=s.get('fpsMilli'))
            out_c.append(meta)
    items.append(dict(type=item['type'], file=item['file'], size=item['size'], duration=item['duration'],
                      videoFrameRate=item.get('fps'), windows=windows, candidates=out_c))
doc = dict(source='rex-fun library, 120 releases sampled 2026-10-20 (60 movies, 60 episodes, no remux or 2160p); '
           'candidates from metadata.provider.plex.tv/library/streams/matches (lang en, pt-BR) and the Stremio '
           'OpenSubtitles v3 addon; truth from Silero VAD v5 over six 90 s windows (subsbench measure.py)',
           cueEncoding='cues: flat centiseconds, each value the difference from the one before (start, end, start, end, ...)',
           frame=M.FRAME, window=M.WIN, lag=M.LAG, items=items)
raw = json.dumps(doc, separators=(',', ':')).encode()
open('sync_corpus.json.gz', 'wb').write(gzip.compress(raw, 9))
print(len(items), sum(len(i['candidates']) for i in items), len(raw), os.path.getsize('sync_corpus.json.gz'))
