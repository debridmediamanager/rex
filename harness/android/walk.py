"""Repeatable walk of Plex for Android (phone UI) against whichever server the proxy pins.

walk.py <serial> <label> <movie_lib> <movie_title> <show_lib> <show_title> [steps...]

Each step writes out/<label>/<nn>-<name>.txt (the UI dump) and .png, and
out/<label>/steps.json maps each step to the capture-line range it produced.
"""
import json, os, re, subprocess, sys, time
import xml.etree.ElementTree as ET

C = os.path.join(os.environ.get("DROID_WORK", os.path.expanduser("~/plexsim-android")), "N")
ser, label, MLIB, MTITLE, SLIB, STITLE = sys.argv[1:7]
ONLY = sys.argv[7:]
OUT = f"{C}/out/{label}"
os.makedirs(OUT, exist_ok=True)
CAP = f"{C}/cap/{label}.jsonl"


def adb(*a):
    return subprocess.run(["adb", "-s", ser, *a], capture_output=True)


def nodes():
    for _ in range(4):
        adb("shell", "rm", "-f", "/sdcard/ui.xml")
        adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
        x = adb("shell", "cat", "/sdcard/ui.xml").stdout.decode("utf-8", "replace")
        if "<hierarchy" in x:
            root = ET.fromstring(x[x.index("<hierarchy"):])
            out = []
            for n in root.iter("node"):
                b = [int(v) for v in re.findall(r"\d+", n.get("bounds", ""))]
                lab = n.get("text") or n.get("content-desc") or n.get("resource-id") or ""
                out.append((lab, n.get("clickable") == "true", b))
            return out
        time.sleep(1.5)
    return []


def caplines():
    try:
        return sum(1 for _ in open(CAP))
    except FileNotFoundError:
        return 0


N = [0]
steps = {}
if os.path.exists(f"{OUT}/steps.json"):
    steps = json.load(open(f"{OUT}/steps.json"))


def snap(name):
    N[0] += 1
    tag = f"{N[0]:02d}-{name}"
    ns = nodes()
    with open(f"{OUT}/{tag}.txt", "w") as fh:
        for lab, c, b in ns:
            if lab.strip() and not lab.startswith("\\u") and not re.fullmatch(r"[-]", lab):
                fh.write(("C " if c else "  ") + f"{b} {lab}\n")
    open(f"{OUT}/{tag}.png", "wb").write(adb("exec-out", "screencap", "-p").stdout)
    return ns


def find(text, idx=0, ns=None):
    ns = ns or nodes()
    m = [n for n in ns if n[0] == text] or [n for n in ns if re.search(text, n[0])]
    if len(m) <= idx:
        return None
    b = m[idx][2]
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2


def tap(text, idx=0, wait=5):
    p = find(text, idx)
    if not p:
        print("  MISSING", text)
        return False
    adb("shell", "input", "tap", str(p[0]), str(p[1]))
    time.sleep(wait)
    return True


def back(wait=4):
    adb("shell", "input", "keyevent", "4")
    time.sleep(wait)


def swipe(n=1, wait=3):
    for _ in range(n):
        adb("shell", "input", "swipe", "540", "1800", "540", "700", "400")
        time.sleep(wait)


def step(name):
    def deco(fn):
        if ONLY and name not in ONLY:
            return fn
        a = caplines()
        print("step", name)
        try:
            fn()
        except Exception as e:
            print("  ERROR", name, e)
        time.sleep(2)
        steps[name] = [a, caplines()]
        json.dump(steps, open(f"{OUT}/steps.json", "w"), indent=1)
        return fn
    return deco


def reset():
    adb("shell", "am", "start", "-S", "-W", "-n", "com.plexapp.android/tv.plex.app.MainActivity")
    time.sleep(14)


def to_library(lib):
    reset()
    tap(r"Libraries, tab 2 of 5", wait=7)
    if tap(r" library on ", wait=4):
        adb("shell", "input", "tap", "797", "2248")   # See All Libraries
        time.sleep(5)
        y = 1482 if lib == MLIB else 1632             # row 1 movies, row 2 shows
        adb("shell", "input", "tap", "180", str(y))
        time.sleep(7)


@step("home")
def _():
    reset()
    snap("home")
    for i in range(4):
        swipe()
        snap(f"home{i}")


@step("recommended")
def _():
    to_library(MLIB)
    tap(r"Recommended, 1 of")
    snap("rec")
    swipe(2)
    snap("rec-down")


@step("browse")
def _():
    to_library(MLIB)
    tap(r"Browse, 2 of", wait=6)
    snap("browse")
    for pill in ("^All$", "^Title$", f"^{MLIB}$|^Movies$"):
        if tap(pill, wait=4):
            snap("pill")
            back()
    if tap("^More$", wait=4):
        snap("browse-more")
        back()
    swipe(2)
    snap("browse-down")


@step("third")
def _():
    to_library(MLIB)
    if tap(r", 3 of 3", wait=6):
        snap("third")
        swipe()
        snap("third-down")


def to_movie():
    to_library(MLIB)
    tap(r"Browse, 2 of", wait=6)
    return tap(f"^{re.escape(MTITLE)}", wait=8)


@step("movie")
def _():
    if not to_movie():
        return
    snap("movie")
    for i in range(3):
        swipe()
        snap(f"movie-down{i}")
    for i in range(4):
        adb("shell", "input", "swipe", "540", "700", "540", "1800", "300")
        time.sleep(1)
    if tap("^More$", wait=4):
        snap("movie-more")
        back()
    if tap("^Off$", wait=4):
        snap("movie-subs")
        back()


@step("play")
def _():
    if not to_movie():
        return
    if tap("^Watch$|^Play$|^Resume", wait=12):
        tap("^Got it$", wait=20)
        snap("playing")
        adb("shell", "input", "tap", "540", "1200")
        time.sleep(2)
        snap("playing-controls")
        back(6)
        snap("after-play")
    back()


@step("show")
def _():
    to_library(SLIB)
    tap(r"Browse, 2 of", wait=6)
    if not tap(f"^{re.escape(STITLE)}", wait=8):
        return
    snap("show")
    swipe()
    snap("show-down")
    if tap(r"^Season 1|^S1$|Season 1", wait=7):
        snap("season")
        swipe()
        snap("season-down")
        if tap(r"^Episode 1|^E1|1\. |S1 · E1|S1 E1", wait=7):
            snap("episode")
            swipe()
            snap("episode-down")
            back()
        back()
    back()


@step("search")
def _():
    reset()
    if tap("^Search$", wait=4):
        adb("shell", "input", "text", "the")
        time.sleep(8)
        snap("search")
        back()
        back()


print(json.dumps(steps))
