#!/usr/bin/env python3
"""The native core modules against the Python oracle's recorded answers, row by row.

`tests/fixtures/oracle_golden/native_phase2.json` is every case this suite once compared
with the Python oracle (`oracle/filters.py`, `gate.py`, `hierarchy.py`, `mediainfo.py`,
`metadata.py`, `xmlout.py`), recorded from the oracle itself before it was removed: the
native CLI call (its arguments and `--input` document), what the oracle answered for the
same input, and how the two were compared. The driver invokes only the native CLI's test
adapters (`rust/src/main.rs`); no service or socket is started.

    REX_NATIVE_BIN=rust/target/release/rex python3 tests/test_native_phase2.py
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOLDEN = os.path.join(HERE, "fixtures", "oracle_golden", "native_phase2.json")

BIN = os.environ.get("REX_NATIVE_BIN",
                     os.path.join(ROOT, "rust", "target", "debug", "rex"))

# The check groups the suite always reported, by adapter.
GROUPS = {
    "mediainfo": "mediainfo fixtures", "normalize-media": "mediainfo fixtures",
    "hierarchy-build": "hierarchy rows", "hierarchy-normalize": "hierarchy rows",
    "hierarchy-fold": "hierarchy rows", "hierarchy-case": "hierarchy rows",
    "xml": "XML documents", "ordered": "XML documents", "system-xml": "XML documents",
    "metadata-enrich": "metadata/provider rows",
    "provider-guids": "metadata/provider rows",
    "gate": "gate/catalog rows", "catalog-normalize": "gate/catalog rows",
}


def equivalent(left, right):
    """Compare JSON while preserving presence, type, and list order."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            equivalent(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            equivalent(a, b) for a, b in zip(left, right)
        )
    return left == right


def ordered_equivalent(left, right):
    """Compare JSON recursively, including object insertion order."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return list(left) == list(right) and all(
            ordered_equivalent(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            ordered_equivalent(a, b) for a, b in zip(left, right)
        )
    return left == right


def filter_refusal(result, status):
    """A FilterError is exit 2 with `filter <status>:` on stderr, never a panic."""
    return (result.returncode == 2 and f"filter {status}:" in result.stderr
            and not any(marker in result.stderr.lower()
                        for marker in ("panicked at", "stack overflow", "fatal runtime")))


def agrees(compare, expect, result):
    """Whether the native answer is the recorded one, compared as the suite compared it."""
    if compare == "match":
        if "error" in expect:
            return filter_refusal(result, expect["error"])
        if result.returncode:
            return False
        payload = json.loads(result.stdout)
        return (list(payload) == ["match"] and type(payload["match"]) is bool
                and payload["match"] == expect["match"])
    if compare == "source-order":
        if "error" in expect:
            return filter_refusal(result, expect["error"])
        return ordered_equivalent(expect["json"], json.loads(result.stdout))
    if result.returncode:
        return False
    if compare == "text":
        return result.stdout == expect
    got = json.loads(result.stdout)
    if compare == "equivalent":
        return equivalent(expect, got)
    if compare == "ordered":
        return ordered_equivalent(expect, got)
    if compare == "equal":
        return expect == got
    if compare == "enrich":
        return got["ok"] == expect["ok"] and equivalent(expect["row"], got["row"])
    if compare == "guids":
        return got["count"] == expect["count"] and equivalent(expect["items"], got["items"])
    if compare == "parse":
        return got["tree"] == expect["tree"] and got["errors"] == expect["errors"]
    if compare == "gate":
        return (got["ok"], got["text"]) == (expect["ok"], expect["text"])
    raise AssertionError(f"unknown comparison {compare!r}")


def run_case(case, directory):
    """Run one recorded native CLI call, writing its JSON through task-local files."""
    def written(value):
        path = os.path.join(directory, "%d.json" % len(os.listdir(directory)))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    argv = [written(arg["json"]) if isinstance(arg, dict) else arg for arg in case["argv"]]
    if "input_file" in case:
        reference = case["input_file"]
        path = os.path.join(ROOT, reference["path"])
        with open(path, "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != reference["sha256"]:
                raise AssertionError("%s changed since the oracle's answer for it was recorded"
                                     % reference["path"])
        argv += ["--input", path]
    elif "input" in case:
        argv += ["--input", written(case["input"])]
    return subprocess.run([BIN, *argv], text=True, capture_output=True, check=False)


def main():
    if not os.path.exists(BIN):
        raise SystemExit(f"native binary not found: {BIN}")
    with open(GOLDEN, encoding="utf-8") as handle:
        cases = json.load(handle)["cases"]
    counts = collections.Counter()
    failed_groups = set()
    failures = []
    with tempfile.TemporaryDirectory(prefix="rex-phase2-") as directory:
        for case in cases:
            result = run_case(case, directory)
            try:
                ok = agrees(case["compare"], case["expect"], result)
            except (ValueError, KeyError, TypeError) as error:
                ok = False
                result.stderr += "\n%s: %s" % (type(error).__name__, error)
            group = GROUPS.get(case["argv"][0], "filter cases")
            counts[group] += 1
            if not ok:
                failed_groups.add(group)
                failures.append((case["label"], result.returncode, result.stderr.strip()[-300:]))
    print("native phase2 parity against the oracle's recorded answers")
    for name in ("mediainfo fixtures", "hierarchy rows", "XML documents",
                 "metadata/provider rows", "filter cases", "gate/catalog rows"):
        print(f"{'FAIL' if name in failed_groups else 'PASS'} {name}: {counts[name]} checks")
    for label, code, stderr in failures:
        print(f"FAIL {label} (exit {code}) {stderr}")
    if failures:
        print(f"FAIL {len(failures)} of {len(cases)} checks")
        return 1
    print(f"PASS total: {len(cases)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
