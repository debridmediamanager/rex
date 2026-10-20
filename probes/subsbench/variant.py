"""Re-score the corpus with a different VAD and window plan, against the Silero/6x90 s verdicts.
energy: ffmpeg band-pass 300-3400 Hz, RMS per 32 ms frame, speech above the window's own
percentile -- what rex could compute with the FFmpeg it already runs and no new crate."""
import json, os, subprocess, sys, collections
import numpy as np
import measure as M

MODE, NWIN, SECS, PCT = (sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])) if __name__ == '__main__' else ('energy', 6, 90.0, 50.0)


def energy_mask(path, secs):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', path, '-t', str(secs),
                          '-af', 'highpass=f=300,lowpass=f=3400', '-f', 's16le', '-'], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    n = len(x) // 512
    rms = np.sqrt((x[:n * 512].reshape(n, 512) ** 2).mean(axis=1))
    db = 20 * np.log10(rms + 1)
    return (db > np.percentile(db, PCT)).astype(np.int8)


def silero(path, secs):
    m = M.speech_mask(path)
    return m[:int(secs / M.FRAME)]


def _main():
    cands = {r['key']: r for r in json.load(open('cands.json'))}
    pick = [1, 3, 5] if NWIN == 3 else [0, 2, 4, 5] if NWIN == 4 else list(range(6))
    agree = collections.Counter()
    out = []
    for line in open(sys.argv[5]):
        ref = json.loads(line)
        item = cands[ref['key']]
        dur = item['duration'] / 1000
        windows = []
        for i in pick:
            st = round(dur * M.FRACTIONS[i], 1)
            p = f"{M.CACHE}/{item['key']}_{int(st)}.pcm"
            windows.append((st, energy_mask(p, SECS) if MODE == 'energy' else silero(p, SECS)))
        M.WIN = SECS
        row = dict(key=ref['key'], cands={})
        for name, rv in ref['cands'].items():
            if name.startswith('synthetic') or name.startswith('embedded'):
                continue
            fn = name[5:] if name.startswith('null_') else f"{item['key']}_{name}"
            text, _ = M.decode(open(os.path.join('subsbench-files', fn), 'rb').read())
            cl = M.cues(text)
            v = M.measure(windows, cl)['verdict']
            row['cands'][name] = v
            agree[(rv['verdict'], v)] += 1
            if rv['verdict'] == 'in_sync':
                row['cands']['synthetic_shift2_' + name] = M.measure(windows, [(a + 2, b + 2) for a, b in cl])['verdict']
                row['cands']['synthetic_fps_' + name] = M.measure(windows, [(a * 23.976 / 25, b * 23.976 / 25) for a, b in cl])['verdict']
        out.append(row)
    json.dump(out, open(f'variant_{MODE}_{NWIN}x{int(SECS)}_{int(PCT)}.json', 'w'))
    print(MODE, NWIN, SECS, PCT)
    for (a, b), n in sorted(agree.items()): print(f'  silero {a:8} -> {b:8} {n}')
    syn = collections.Counter((k.split('_')[1], v) for r in out for k, v in r['cands'].items() if k.startswith('synthetic'))
    nul = collections.Counter(v for r in out for k, v in r['cands'].items() if k.startswith('null_'))
    print('  synthetic', dict(syn)); print('  null', dict(nul))


if __name__ == '__main__':
    _main()
