"""Apply, prove and commit server patches one at a time.

    WT=<worktree> WORK=<scratch dir> python3 sdrive.py PATCHFILE NAME [NAME ...]

For each NAME: export the worktree's CURRENT HEAD and run it as the parent server, apply the
patch, run the fixed server, execute the patch's suite block against both, and commit only on
fail-then-pass. Unlike cycle 9's driver the parent is re-exported per patch, so "fails on the
parent" means the commit's own parent and not some older tree.
"""
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

# The script's own directory holds runblock.py; WORK is where the scratch files go.
TOOLS = os.path.dirname(os.path.abspath(__file__))
HERE = os.environ.get("WORK", TOOLS)
W = os.environ.get("WT")
if not W:
    raise SystemExit("set WT to the worktree being patched (and WORK to a scratch dir for logs)")
OLD_PORT = os.environ.get("OLD_PORT", "32592")
NEW_PORT = os.environ.get("NEW_PORT", "32593")
AUTHOR = ["-c", "user.name=Ben Adrian Sarmiento", "-c", "user.email=me@bensarmiento.com"]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def serve(tree, port, label):
    for pid in run(["lsof", "-ti", "tcp:%s" % port, "-sTCP:LISTEN"]).stdout.split():
        os.kill(int(pid), signal.SIGTERM)
    for _ in range(100):
        if not run(["lsof", "-ti", "tcp:%s" % port, "-sTCP:LISTEN"]).stdout.strip():
            break
        time.sleep(0.2)
    log = open(os.path.join(HERE, "%s%s.log" % (label, port)), "w")
    subprocess.Popen([sys.executable, os.path.join(tree, "server.py"), "--port", port, "--catalog",
                      os.path.join(tree, "tests", "catalog_ui.json"), "--assert-claimed"],
                     stdout=log, stderr=log, cwd=tree, start_new_session=True,
                     env=dict(os.environ, RD_TOKEN="dummy"))
    for _ in range(160):
        try:
            urllib.request.urlopen("http://127.0.0.1:%s/identity" % port, timeout=2)
            return
        except Exception:
            time.sleep(0.25)
    raise SystemExit("%s server on %s did not start" % (label, port))


def export_head(dest):
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    tar = subprocess.run(["git", "-C", W, "archive", "HEAD"], capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", dest], input=tar, check=True)


spec = importlib.util.spec_from_file_location("patches", os.path.abspath(sys.argv[1]))
patches = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patches)

for name in sys.argv[2:]:
    if run(["git", "-C", W, "status", "--porcelain"]).stdout.strip():
        raise SystemExit("worktree not clean before %s" % name)
    parent = os.path.join(HERE, "parent_srv")
    export_head(parent)
    serve(parent, OLD_PORT, "old")
    patch = patches.PATCHES[name]
    patch["apply"](W)
    changed = [f for f in run(["git", "-C", W, "diff", "--name-only"]).stdout.split() if f]
    untracked = [f for f in run(["git", "-C", W, "ls-files", "--others", "--exclude-standard"]).stdout.split() if f]
    compiled = run([sys.executable, "-m", "py_compile"]
                   + [os.path.join(W, f) for f in changed + untracked if f.endswith(".py")])
    if compiled.returncode:
        raise SystemExit("compile %s failed:\n%s" % (name, compiled.stderr[-2000:]))
    serve(W, NEW_PORT, "new")
    proof = run([sys.executable, os.path.join(TOOLS, "runblock.py"), os.path.join(W, "tests", "test_ui_surface.py"),
                 "%s:%s" % (os.path.abspath(sys.argv[1]), name),
                 "http://127.0.0.1:%s" % OLD_PORT, "http://127.0.0.1:%s" % NEW_PORT], timeout=900)
    print("== %s" % name)
    for line in proof.stdout.splitlines():
        if "PASS" in line or "FAIL" in line or "SKIP" in line or line.startswith("verdicts"):
            print("   " + line.strip()[:400])
    if "verdicts [False, True]" not in proof.stdout:
        print(proof.stdout[-1500:])
        print(proof.stderr[-1500:])
        raise SystemExit("%s did not prove out; left uncommitted" % name)
    run(["git", "-C", W, "add"] + changed + untracked)
    committed = run(["git", "-C", W] + AUTHOR + ["commit", "-q", "-m", patch["message"]])
    if committed.returncode:
        raise SystemExit("commit %s failed:\n%s" % (name, committed.stderr))
    print("   committed", run(["git", "-C", W, "log", "--oneline", "-1"]).stdout.strip())
