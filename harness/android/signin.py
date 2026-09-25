"""signin.py <serial>: from a cleared app to Home, waiting for each screen."""
import subprocess, sys, time, re
sys.argv = [sys.argv[0], sys.argv[1], "x", "x", "x", "x", "x"]
ser = sys.argv[1]
import importlib.util, os
spec = importlib.util.spec_from_file_location("walk_helpers", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.py"))


def adb(*a):
    return subprocess.run(["adb", "-s", ser, *a], capture_output=True)


def texts():
    adb("shell", "rm", "-f", "/sdcard/ui.xml")
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    x = adb("shell", "cat", "/sdcard/ui.xml").stdout.decode("utf-8", "replace")
    return re.findall(r'(?:text|content-desc)="([^"]+)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', x)


def wait_tap(label, timeout=90, tap=True):
    end = time.time() + timeout
    while time.time() < end:
        for t, a, b, c, d in texts():
            if t == label:
                if tap:
                    adb("shell", "input", "tap", str((int(a) + int(c)) // 2), str((int(b) + int(d)) // 2))
                return True
        time.sleep(3)
    return False


adb("shell", "am", "start", "-W", "-n", "com.plexapp.android/tv.plex.app.MainActivity")
print("sign in", wait_tap("Sign in"))
time.sleep(8)
adb("shell", "am", "start", "-W", "-n", "com.plexapp.android/tv.plex.app.MainActivity")
end = time.time() + 120
state = None
while time.time() < end and state is None:
    labels = {t: (a, b, c, d) for t, a, b, c, d in texts()}
    if "Home, tab 1 of 5" in labels:
        state = "home"
    elif "Continue" in labels:
        a, b, c, d = labels["Continue"]
        adb("shell", "input", "tap", str((int(a) + int(c)) // 2), str((int(b) + int(d)) // 2))
        time.sleep(6)
    else:
        time.sleep(3)
print("state", state)
