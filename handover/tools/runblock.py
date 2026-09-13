"""Run one patch's suite block against servers. Usage: runblock.py SUITE PATCH BASE [BASE ...]

Loads tests/test_ui_surface.py as a module (its helpers and `results`), builds the fixture
variables its main() builds, and executes the patch's BLOCK -- the exact text the patch
adds to the suite -- once per base. Prints PASS/FAIL per base.
"""
import importlib.util, os, sys, textwrap, time, urllib.request

suite_path, patch_path, bases = sys.argv[1], sys.argv[2], sys.argv[3:]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patch_path, _, patch_name = patch_path.partition(":")
sys.path.insert(0, os.path.dirname(os.path.abspath(patch_path)))
patch_module = load("patch", patch_path)
block = textwrap.dedent(patch_module.PATCHES[patch_name]["block"] if patch_name else patch_module.BLOCK)
verdicts = []
for base in bases:
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/identity", timeout=2)
            break
        except Exception:
            time.sleep(0.25)
    suite = load("suite", suite_path)
    get = suite.get
    sections = get(base, "/library/sections")[1]
    movie_section = show_section = None
    for directory in sections.get("Directory") or []:
        if directory.get("type") == "movie" and movie_section is None:
            movie_section = directory["key"]
        if directory.get("type") == "show" and show_section is None:
            show_section = directory["key"]
    movies = get(base, "/library/sections/%s/all" % movie_section)[1].get("Metadata") or []
    shows = (get(base, "/library/sections/%s/all?type=2" % show_section)[1].get("Metadata") or []) \
        if show_section else []
    ns = {name: getattr(suite, name) for name in dir(suite) if not name.startswith("__")}
    ns.update(base=base, machine=get(base, "/identity")[1].get("machineIdentifier", ""),
              movie_section=movie_section, show_section=show_section,
              movie=movies[0]["ratingKey"], spare=movies[-1]["ratingKey"] if len(movies) > 1 else None,
              show=shows[0]["ratingKey"] if shows else None)
    print("== %s" % base)
    exec(block, ns)
    verdicts.append(all(ok for _, ok in suite.results) and bool(suite.results))
print("verdicts", verdicts)
