"""adb UI driver. ui.py <serial> dump | tap <text|x,y> | key <code> | shot <file> | swipe x1 y1 x2 y2 | text <s>"""
import subprocess, sys, re, time, xml.etree.ElementTree as ET
ser = sys.argv[1]
def adb(*a, **k):
    return subprocess.run(['adb', '-s', ser, *a], capture_output=True, **k)
def nodes():
    for _ in range(3):
        adb('shell', 'rm', '-f', '/sdcard/ui.xml'); adb('shell', 'uiautomator', 'dump', '/sdcard/ui.xml')
        x = adb('shell', 'cat', '/sdcard/ui.xml').stdout.decode('utf-8', 'replace')
        if '<hierarchy' in x: break
        time.sleep(1)
    root = ET.fromstring(x[x.index('<hierarchy'):])
    out = []
    for n in root.iter('node'):
        b = [int(v) for v in re.findall(r'\d+', n.get('bounds', ''))]
        label = n.get('text') or n.get('content-desc') or n.get('resource-id') or ''
        out.append((label, n.get('clickable'), n.get('focused'), b, n.get('class')))
    return out
cmd = sys.argv[2]
if cmd == 'dump':
    for label, c, f, b, cls in nodes():
        if label.strip():
            print(('C' if c == 'true' else ' ') + ('F' if f == 'true' else ' '), b, cls.split('.')[-1], repr(label[:90]))
elif cmd == 'tap':
    t = sys.argv[3]
    if re.fullmatch(r'\d+,\d+', t):
        x, y = t.split(',')
    else:
        m = [n for n in nodes() if n[0] == t] or [n for n in nodes() if t.lower() in n[0].lower()]
        if not m: sys.exit(f'no node {t!r}')
        b = m[int(sys.argv[4]) if len(sys.argv) > 4 else 0][3]
        x, y = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
    adb('shell', 'input', 'tap', str(x), str(y)); print('tap', x, y)
elif cmd == 'key':
    adb('shell', 'input', 'keyevent', sys.argv[3])
elif cmd == 'swipe':
    adb('shell', 'input', 'swipe', *sys.argv[3:7], '400')
elif cmd == 'text':
    adb('shell', 'input', 'text', sys.argv[3].replace(' ', '%s'))
elif cmd == 'shot':
    open(sys.argv[3], 'wb').write(adb('exec-out', 'screencap', '-p').stdout)
