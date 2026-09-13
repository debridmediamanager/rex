"""Apply, prove and commit media patches one at a time.

    WT=<worktree> WORK=<scratch dir> python3 mdrive.py PATCHFILE NAME [NAME ...]

For each NAME: copy the PARENT commit's mediainfo.py aside, apply the patch to the worktree,
byte-compile, run the patch's checks with mediainfo imported from the parent (must FAIL)
and from the worktree (must PASS), run the whole media suite on the worktree (must pass),
and commit only then. Stops at the first patch that does not prove out, leaving its
changes uncommitted for inspection.
"""
import importlib.util, os, shutil, subprocess, sys

# The script's own directory holds runblock.py; WORK is where the scratch files go.
TOOLS = os.path.dirname(os.path.abspath(__file__))
HERE = os.environ.get("WORK", TOOLS)
W = os.environ.get("WT")
if not W:
    raise SystemExit("set WT to the worktree being patched (and WORK to a scratch dir for logs)")
PARENT = os.path.join(HERE, "parent")
AUTHOR = ["-c", "user.name=Ben Adrian Sarmiento", "-c", "user.email=me@bensarmiento.com"]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def suite(root, only=None):
    cmd = [sys.executable, os.path.join(W, "tests", "test_mediainfo.py"), "--root", root, "--examples", "4"]
    if only:
        cmd += ["--only", ",".join(only)]
    return run(cmd, cwd=W)


spec = importlib.util.spec_from_file_location("patches", sys.argv[1])
patches = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patches)

for name in sys.argv[2:]:
    patch = patches.PATCHES[name]
    if run(["git", "-C", W, "status", "--porcelain"]).stdout.strip():
        raise SystemExit("worktree not clean before %s" % name)
    shutil.rmtree(PARENT, ignore_errors=True)
    os.makedirs(PARENT)
    # mediainfo.py and every sidecar it may load beside itself: a parent missing one dies
    # at import, which reads as a failing check and proves nothing.
    tracked = run(["git", "-C", W, "ls-tree", "--name-only", "HEAD"]).stdout.split()
    for module in ["mediainfo.py"] + [f for f in tracked if f.endswith(".json")]:
        with open(os.path.join(PARENT, module), "wb") as handle:
            handle.write(subprocess.run(["git", "-C", W, "show", "HEAD:" + module], capture_output=True).stdout)
    patch["apply"](W)
    changed = [f for f in run(["git", "-C", W, "diff", "--name-only"]).stdout.split() if f]
    untracked = [f for f in run(["git", "-C", W, "ls-files", "--others", "--exclude-standard"]).stdout.split() if f]
    compiled = run([sys.executable, "-m", "py_compile"] + [os.path.join(W, f) for f in changed + untracked if f.endswith(".py")])
    if compiled.returncode:
        raise SystemExit("compile %s failed:\n%s" % (name, compiled.stderr[-2000:]))
    print("== %s" % name)
    checks = patch.get("checks") or []
    if checks:
        old = suite(PARENT, checks)
        new = suite(W, checks)
        for label, result in (("parent", old), ("fixed", new)):
            for line in result.stdout.splitlines():
                print("   %-6s %s" % (label, line[:300]))
        if "Traceback" in old.stderr or not old.stdout.strip():
            raise SystemExit("%s: the parent run did not reach its checks:\n%s" % (name, old.stderr[-1500:]))
        if old.returncode == 0 or new.returncode != 0:
            raise SystemExit("%s did not prove out (parent rc %s, fixed rc %s); left uncommitted"
                             % (name, old.returncode, new.returncode))
    whole = suite(W)
    print("   whole  " + (whole.stdout.strip().splitlines() or ["(no output)"])[-1])
    if whole.returncode:
        print(whole.stdout[-3000:])
        raise SystemExit("%s broke the media suite; left uncommitted" % name)
    run(["git", "-C", W, "add"] + changed + untracked)
    committed = run(["git", "-C", W] + AUTHOR + ["commit", "-q", "-m", patch["message"]])
    if committed.returncode:
        raise SystemExit("commit %s failed:\n%s" % (name, committed.stderr))
    print("   committed", run(["git", "-C", W, "log", "--oneline", "-1"]).stdout.strip())
