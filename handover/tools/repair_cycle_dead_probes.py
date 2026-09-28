#!/usr/bin/env python3
"""Repair cycle-local probes whose inputs were dropped by the previous copy."""

from __future__ import annotations

import re
import sys
from pathlib import Path


CYCLE_PARENT = Path("/private/tmp")


def safe_target(root: Path, relative: str) -> Path:
    """Resolve an edit target and refuse symlinks that leave this cycle tree."""
    target = (root / relative).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            f"repair target escapes cycle tree: {root / relative} -> {target}"
        ) from exc
    return target


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if new in text:
        return
    if old not in text:
        raise RuntimeError(f"repair anchor missing: {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1))


def repair_c(root: Path) -> None:
    path = safe_target(root, "C/f587/run74.py")
    replace_once(path, "import json, hashlib, re, subprocess, sys, os\n",
                 "import json, hashlib, re, subprocess, sys, os, shutil\n")
    old = "rows = [json.loads(l) for l in open('../ax1/r1.jsonl')]\n"
    new = old + (
        "for r in rows:\n"
        "    if r.get('h') == 'sim' or not r.get('body'):\n"
        "        continue\n"
        "    src = os.path.join('../ax1', r['body'])\n"
        "    dst = os.path.join('bodies', os.path.basename(r['body']))\n"
        "    if not os.path.exists(src):\n"
        "        raise RuntimeError('missing measured ax1 body: ' + src)\n"
        "    shutil.copy2(src, dst)\n"
    )
    replace_once(path, old, new)
    old = "r2 = [json.loads(l) for l in open('../ax1/r2.jsonl')]\n"
    new = old + (
        "for r in r2:\n"
        "    if r.get('h') == 'sim' or not r.get('body'):\n"
        "        continue\n"
        "    src = os.path.join('../ax1', r['body'])\n"
        "    dst = os.path.join('bodies', os.path.basename(r['body']))\n"
        "    if not os.path.exists(src):\n"
        "        raise RuntimeError('missing measured ax1 body: ' + src)\n"
        "    shutil.copy2(src, dst)\n"
    )
    replace_once(path, old, new)


def repair_p(root: Path) -> None:
    path = safe_target(root, "P/wr.py")
    replace_once(path, "import cl, json, sys, time, uuid, urllib.parse as up\n",
                 "import cl, json, os, sys, time, uuid, urllib.parse as up\n")
    required = (
        "REQUIRED_READ_IDS = ('machine', 'pl1', 'pl2', 'col1', 'col2', 'pqB', 'pqS', 'newsec')\n"
        "\n"
        "def require_read_ids(ids, host, tag):\n"
        "    if tag == 'r0':\n"
        "        return\n"
        "    missing = [key for key in REQUIRED_READ_IDS if not ids.get(key)]\n"
        "    if missing:\n"
        "        raise RuntimeError('missing write-created ids for %s/%s: %s' % (host, tag, ','.join(missing)))\n"
    )
    import_anchor = "import cl, json, os, sys, time, uuid, urllib.parse as up\n"
    replace_once(path, import_anchor, import_anchor + "\n" + required)
    old = "    k = kfor(host); ids = json.load(open('%s/ids_%s_%s.json' % (P, host, tag)))\n"
    new = (
        "    k = kfor(host)\n"
        "    manifest = '%s/ids_%s_%s.json' % (P, host, tag)\n"
        "    if os.path.exists(manifest):\n"
        "        ids = json.load(open(manifest))\n"
        "    elif tag == 'r0':\n"
        "        # R0 is a pre-write read; its only identity is measured now.\n"
        "        ids = {'machine': machine(host)}\n"
        "        json.dump(ids, open(manifest, 'w'), indent=1)\n"
        "    else:\n"
        "        raise FileNotFoundError(manifest)\n"
    )
    replace_once(path, old, new)
    step = safe_target(root, "P/step1.sh")
    step_text = step.read_text()
    if "python3 rref.py clean >/dev/null" not in step_text:
        step_text, count = re.subn(
            r"(?m)^(cd /private/tmp/plexsim-cycle[^/]+/P)\n",
            r"\1\npython3 rref.py clean >/dev/null\n",
            step_text,
            count=1,
        )
        if count != 1:
            raise RuntimeError(f"repair anchor missing: {step}: cd cycle P")
        step.write_text(step_text)
    new = ("    if ids.get('pqLive'):\n"
           "        R.append('/playQueues/%s?%s' % (ids['pqLive'], PQX[1:]))\n"
           "    # The baseline has no write-created keys; no synthetic /None requests.\n"
           "    if tag == 'r0':\n"
           "        return [p for p in R if 'None' not in p]\n"
           "    return R\n")
    text = path.read_text()
    if new not in text:
        old = ("    if ids.get('pqLive'):\n"
               "        R.append('/playQueues/%s?%s' % (ids['pqLive'], PQX[1:]))\n"
               "    return R\n")
        old_repaired = ("    if ids.get('pqLive'):\n"
                        "        R.append('/playQueues/%s?%s' % (ids['pqLive'], PQX[1:]))\n"
                        "    # The baseline has no write-created keys; no synthetic /None requests.\n"
                        "    return [p for p in R if 'None' not in p]\n")
        if old in text:
            path.write_text(text.replace(old, new, 1))
        elif old_repaired in text:
            path.write_text(text.replace(old_repaired, new, 1))
        else:
            raise RuntimeError(f"repair anchor missing: {path}: readset return")

    call_old = ("    else:\n"
                "        raise FileNotFoundError(manifest)\n"
                "    A, B, Cm = k['A'], k['B'], k['Cm']\n")
    call_new = ("    else:\n"
                "        raise FileNotFoundError(manifest)\n"
                "    require_read_ids(ids, host, tag)\n"
                "    A, B, Cm = k['A'], k['B'], k['Cm']\n")
    replace_once(path, call_old, call_new)


def repair_b(root: Path) -> None:
    for name in ("B/drive59.py", "B/sesssub63.py"):
        path = safe_target(root, name)
        if path.exists():
            text = path.read_text()
            text2 = text.replace("c61b", "c59b")
            if text2 != text:
                path.write_text(text2)
    path = safe_target(root, "B/sesssub63.py")
    if path.exists():
        replace_once(
            path,
            "srt = open(os.path.join(FX, 'c59b.srt'), 'rb').read() if os.path.exists(os.path.join(FX, 'c59b.srt')) else b'1\\n00:00:01,000 --> 00:00:02,000\\nc63b\\n'\n",
            "srt = open(os.path.join(FX, 'c59b.srt'), 'rb').read()\n",
        )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: repair_cycle_dead_probes.py /private/tmp/plexsim-cycleN")
    root = Path(sys.argv[1]).expanduser().resolve()
    if (not root.is_dir() or root.parent != CYCLE_PARENT
            or re.fullmatch(r"plexsim-cycle[0-9]+", root.name) is None):
        raise SystemExit(f"refusing non-cycle tree: {root}")
    repair_c(root)
    repair_p(root)
    repair_b(root)
    print(f"repaired dead probes in {root}")


if __name__ == "__main__":
    main()
