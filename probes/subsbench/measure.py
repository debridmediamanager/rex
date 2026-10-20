"""Sync meter: how far each candidate subtitle's cues sit from the release's own speech.

Per release, six 90 s windows of audio are read through rex (ffmpeg seeks by Range), run
through Silero VAD at 32 ms, and each candidate's cue mask is cross-correlated against the
speech mask (+-1 encoding, as ffsubsync does) for lags within +-30 s.  A frame-rate mismatch
grows the offset across the film, so each candidate is also tried under the common rate
ratios and the best one kept.  The release's own embedded text subtitles are read from the
same windows and measured the same way: they are the meter's control.
"""
import json, os, re, subprocess, sys, urllib.parse
import numpy as np
from charset_normalizer import from_bytes

REX = os.environ['REX_URL']
TOKEN = os.environ['REX_TOKEN']
FRAME = 0.032
WIN = 90.0
FRACTIONS = (0.1, 0.25, 0.4, 0.55, 0.7, 0.85)
LAG = 30.0
RATIOS = {'1': 1.0, '25/23.976': 25 / 23.976, '23.976/25': 23.976 / 25, '24/23.976': 24 / 23.976,
          '23.976/24': 23.976 / 24, '25/24': 25 / 24, '24/25': 24 / 25, '30/29.97': 30 / 29.97, '29.97/30': 29.97 / 30}
CACHE = os.path.expanduser('~/subsbench/cache')


def part_url(item):
    return f"{REX}{item['part']}?X-Plex-Token={TOKEN}"


def probe_subs(item):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=index,codec_type,codec_name:stream_tags=language',
                          '-of', 'json', part_url(item)], capture_output=True, text=True, timeout=300)
    streams = json.loads(out.stdout or '{}').get('streams', [])
    text = [s for s in streams if s.get('codec_type') == 'subtitle' and s.get('codec_name') in ('subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text')]
    return text


def read_window(item, start, base):
    """One window's audio as 16 kHz mono PCM."""
    if os.path.exists(base + '.pcm'):
        return
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{start:.3f}', '-i', part_url(item), '-t', f'{WIN}',
                    '-map', '0:a:0', '-ac', '1', '-ar', '16000', '-f', 's16le', base + '.pcm.tmp'], check=True, timeout=600)
    os.rename(base + '.pcm.tmp', base + '.pcm')


# A subtitle stream is sparse, so ffmpeg reads on past a window to find its next packet:
# the control reads a whole embedded track, and only for files small enough to afford it.
CONTROL_MAX = 700_000_000


def read_embedded(item, s, path):
    if os.path.exists(path):
        return
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', part_url(item), '-map', f"0:{s['index']}", '-c:s', 'srt',
                    '-f', 'srt', path + '.tmp'], check=True, timeout=1800)
    os.rename(path + '.tmp', path)


SILERO = None


def speech_mask(path):
    """Silero VAD v5 over 32 ms chunks (512 samples plus the 64-sample context its wrapper keeps).
    WebRTC VAD called 66-89% of film audio speech -- music and effects -- which flattened every peak."""
    global SILERO
    if SILERO is None:
        import onnxruntime as ort
        opts = ort.SessionOptions(); opts.intra_op_num_threads = 1
        SILERO = ort.InferenceSession(os.path.expanduser('~/subsbench/silero_vad.onnx'), opts)
    pcm = np.frombuffer(open(path, 'rb').read(), dtype=np.int16).astype(np.float32) / 32768
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros(64, dtype=np.float32)
    sr = np.array(16000, dtype=np.int64)
    out = []
    for i in range(0, len(pcm) - 512 + 1, 512):
        chunk = pcm[i:i + 512]
        x = np.concatenate([context, chunk])[None, :]
        prob, state = SILERO.run(None, {'input': x, 'state': state, 'sr': sr})
        context = chunk[-64:]
        out.append(prob[0][0] > 0.5)
    return np.array(out, dtype=np.int8)


TIME = re.compile(r'(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})')
ASS = re.compile(r'^Dialogue:\s*[^,]*,(\d+):(\d\d):(\d\d)\.(\d\d),(\d+):(\d\d):(\d\d)\.(\d\d),', re.M)


def decode(raw):
    """The text, plus whether it reads as mojibake (UTF-8 decoded once more as cp1252)."""
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        best = from_bytes(raw).best()
        text = str(best) if best else raw.decode('latin-1')
    mojibake = len(re.findall(r'Ã[\x80-\xbf¡-¿]|â€|â™', text))
    return text, mojibake


def cues(text):
    out = []
    for m in TIME.finditer(text):
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + int(str(g[3]).ljust(3, '0')[:3]) / 1000
        b = g[4] * 3600 + g[5] * 60 + g[6] + int(str(g[7]).ljust(3, '0')[:3]) / 1000
        if b > a: out.append((a, b))
    if not out:
        for m in ASS.finditer(text):
            g = [int(x) for x in m.groups()]
            a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 100
            b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 100
            if b > a: out.append((a, b))
    return out


def lag_curve(speech, cue_list, start):
    """Correlation of the cue mask against one window's speech for every lag in +-LAG
    (index k is a lag of (k - pad) frames; positive = the subtitle is late), or None
    when the window holds too few cues or too little speech to say anything."""
    n = len(speech)
    if speech.mean() < 0.02:
        return None
    pad = int(LAG / FRAME)
    total = n + 2 * pad
    mask = np.zeros(total, dtype=np.int8)
    lo = start - LAG
    covered = 0
    for a, b in cue_list:
        if b < lo or a > start + WIN + LAG:
            continue
        i, j = max(0, int((a - lo) / FRAME)), min(total, int((b - lo) / FRAME))
        mask[i:j] = 1
        if a < start + WIN and b > start: covered += 1
    if covered < 3:
        return None
    s = speech.astype(np.float64) * 2 - 1
    c = mask.astype(np.float64) * 2 - 1
    size = 1 << (total + n).bit_length()
    return np.fft.irfft(np.fft.rfft(c, size) * np.conj(np.fft.rfft(s, size)), size)[:2 * pad + 1]


def peak(curve):
    pad = (len(curve) - 1) // 2
    k = int(np.argmax(curve))
    sd = float(np.std(curve)) or 1.0
    return round((k - pad) * FRAME, 2), round(float((curve[k] - np.mean(curve)) / sd), 1)


Z = 3.0      # a joint peak this many deviations above the curve's mean is a reading
AGREE = 0.5  # a window agrees with the joint lag within this many seconds


def measure(windows, cue_list):
    """Sum each window's lag curve into one joint curve per frame-rate ratio. The ratio
    whose joint peak stands highest wins (the true rate, 1, by a 10% margin); a window
    agrees when its own peak sits within AGREE of the joint one."""
    best = None
    for name, r in RATIOS.items():
        scaled = [(a * r, b * r) for a, b in cue_list]
        curves = [lag_curve(sp, scaled, st) for st, sp in windows]
        used = [c for c in curves if c is not None]
        if not used:
            continue
        lag, z = peak(np.sum(used, axis=0))
        per = [peak(c) if c is not None else None for c in curves]
        agree = sum(1 for p in per if p and abs(p[0] - lag) <= AGREE)
        cand = dict(ratio=name, lag=lag, z=z, windows=len(used), agree=agree, lags=per)
        if best is None or z > best['z'] * (1.1 if best['ratio'] == '1' else 1):
            best = cand
    if best is None or best['z'] < Z or best['agree'] < 3:
        verdict = 'unknown'
    elif best['agree'] * 2 < best['windows']:
        verdict = 'broken'
    elif best['ratio'] != '1':
        verdict = 'fps'
    elif abs(best['lag']) <= 0.5:
        verdict = 'in_sync'
    else:
        verdict = 'shift'
    if best is None:
        return dict(verdict='unknown', ratio=None, offset=None, z=0, lags=[])
    best['verdict'] = verdict
    best['offset'] = best['lag']
    return best


def main(cands_path, files_dir, out_path, shard='0/1'):
    k, n = map(int, shard.split('/'))
    os.makedirs(CACHE, exist_ok=True)
    done = {}
    if os.path.exists(out_path):
        for line in open(out_path):
            r = json.loads(line); done[r['key']] = r
    out = open(out_path, 'a')
    for item in json.load(open(cands_path))[k::n]:
        if item['key'] in done or not item.get('duration'):
            continue
        try:
            text_subs = probe_subs(item)
            dur = item['duration'] / 1000
            windows = []
            for f in FRACTIONS:
                st = round(dur * f, 1)
                base = f"{CACHE}/{item['key']}_{int(st)}"
                read_window(item, st, base)
                windows.append((st, speech_mask(base + '.pcm')))
            row = dict(key=item['key'], type=item['type'], file=item['file'], speech=[round(float(w.mean()), 3) for _, w in windows], cands={})
            for s in text_subs:
                lang = (s.get('tags') or {}).get('language', 'und')
                if item['size'] > CONTROL_MAX:
                    break
                p = f"{CACHE}/{item['key']}.emb{s['index']}.srt"
                read_embedded(item, s, p)
                cl = cues(open(p, encoding='utf-8', errors='replace').read())
                row['cands'][f"embedded_{lang}_{s['index']}"] = measure(windows, cl)
                # Synthetic controls: the same track 2 s late, and played at the wrong rate.
                row['cands'][f"synthetic_shift2_{lang}_{s['index']}"] = measure(windows, [(a + 2, b + 2) for a, b in cl])
                row['cands'][f"synthetic_fps_{lang}_{s['index']}"] = measure(windows, [(a * 23.976 / 25, b * 23.976 / 25) for a, b in cl])
            # Null control: another title's subtitle against this release's audio, which the
            # meter must never call in sync. It sets how much agreement chance produces.
            others = sorted(n for n in os.listdir(files_dir) if '_plex_en_' in n and not n.endswith('.err')
                            and not n.startswith(item['key'] + '_'))
            if others:
                other = others[int(item['key']) % len(others)]
                text, _ = decode(open(os.path.join(files_dir, other), 'rb').read())
                row['cands']['null_' + other] = measure(windows, cues(text))
            for name in sorted(os.listdir(files_dir)):
                if not name.startswith(item['key'] + '_') or name.endswith('.err'):
                    continue
                text, moj = decode(open(os.path.join(files_dir, name), 'rb').read())
                m = measure(windows, cues(text))
                m['mojibake'] = moj
                row['cands'][name[len(item['key']) + 1:]] = m
            # Positive control: the first candidate read in sync, moved 2 s late and played at
            # the wrong rate. The meter has to call those a shift and a frame-rate error.
            synced = next((n for n, v in row['cands'].items() if v['verdict'] == 'in_sync'
                           and not n.startswith(('embedded_', 'synthetic_', 'null_'))), None)
            if synced:
                text, _ = decode(open(os.path.join(files_dir, f"{item['key']}_{synced}"), 'rb').read())
                cl = cues(text)
                row['cands']['synthetic_shift2_' + synced] = measure(windows, [(a + 2, b + 2) for a, b in cl])
                row['cands']['synthetic_fps_' + synced] = measure(windows, [(a * 23.976 / 25, b * 23.976 / 25) for a, b in cl])
            out.write(json.dumps(row) + '\n'); out.flush()
            print(item['key'], {k: v['verdict'] for k, v in row['cands'].items()}, flush=True)
        except Exception as e:
            print(item['key'], 'ERROR', e, flush=True)


if __name__ == '__main__':
    main(*sys.argv[1:5])
