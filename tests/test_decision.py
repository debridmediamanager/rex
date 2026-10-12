#!/usr/bin/env python3
"""`decision.py` against a real PMS's own decisions. No server, no network.

A plain-form decision -- one with no `hasMDE=1` -- runs the CLIENT's profile file against
the item, so the model has two measured inputs: `client_profiles.json`, read out of the
reference's own `Resources/Profiles`, and this corpus, which is what the endpoint actually
answered. `tests/fixtures/decisions/rows.json` holds each row's `Media` exactly as the
reference wrote it and `cells.jsonl` one line per (profile, row) pair: 78 profiles against
24 rows on plexlab (1.43.4) and the 1.42.2 reference, chosen for their containers, codecs,
channel counts, bit depths and subtitle kinds, plus 78 against one row MADE for the model
on plexscratch -- two audio tracks that carry no language at all.

That made row is recorded in BOTH of its states, because the reference answers differently
for the same bytes:

  * `selected` -- after a client has chosen a track (`PUT /library/parts/{id}?
    audioStreamID=<id>`). Every answer is then a function of the row, and the model
    reproduces all 78.
  * `pre-selection` -- after `audioStreamID=-1` clears that choice. 41 of the 78 profiles
    then refuse the row as "not the first audio stream" although `selected` still names
    the first track, and the item's `Stream` rows are byte-identical to the other state.
    That selection is per-user state the row does not carry, so rex -- which holds a
    chosen track -- cannot express it. These cells are RECORDED and not emulated; the
    suite counts them and never fails on them.

    python3 tests/test_decision.py [--examples N]
"""
import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "oracle"))
import decision  # noqa: E402  -- after the path is set, like the other module suites

FIXTURES = os.path.join(HERE, "fixtures", "decisions")
GREEN, RED, GREY, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def test_python_json_semantics():
    """Keep native numeric helpers aligned with the Python oracle.

    These are type/whitespace probes, not measured PMS rows: the corpus remains the
    only source for server outcomes and decision text.
    """
    assert decision._number(" 2.0 ") == 2.0
    numeric = {"Part": [{"Stream": [{"streamType": 2, "selected": True}]}]}
    textual = {"Part": [{"Stream": [{"streamType": "2", "selected": True}]}]}
    assert decision.evaluated_audio(numeric)["streamType"] == 2
    assert decision.evaluated_audio(textual) is None


def outcome(cell):
    """The reference's answer as the model's own vocabulary names it."""
    if cell["status"] != 200:
        return "refuse"
    direct = cell.get("directPlayDecisionCode")
    general = cell.get("generalDecisionCode")
    transcode = cell.get("transcodeDecisionCode")
    if direct == 1000:
        return "directplay"
    if general == 1001 or transcode == 4002:
        # The 1.42.2 reference refuses some conversions on its own CPU budget (4002); a
        # target existed.
        return "convert"
    if general == 2000:
        return "neither"
    return "?%s/%s/%s" % (direct, general, transcode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", type=int, default=6,
                        help="how many mismatching cells to print")
    args = parser.parse_args()

    test_python_json_semantics()
    corpus = json.load(open(os.path.join(FIXTURES, "rows.json"), encoding="utf-8"))
    rows, texts = corpus["rows"], corpus["texts"]
    cells = [json.loads(line) for line in
             open(os.path.join(FIXTURES, "cells.jsonl"), encoding="utf-8") if line.strip()]

    counted = collections.Counter()
    misses = []
    recorded = collections.Counter()
    for cell in cells:
        media = rows[cell["row"]]["Media"]
        answer, text = decision.decide(cell["profile"], media)
        wanted = outcome(cell)
        wanted_text = texts[cell["text"]] if "text" in cell else None
        exact = answer == wanted and (answer == "refuse" or text == wanted_text)
        if cell["state"] == "pre-selection":
            recorded["cells"] += 1
            recorded["reproduced"] += exact
            continue
        counted["cells"] += 1
        counted["outcome"] += answer == wanted
        counted["exact"] += exact
        if not exact:
            misses.append((cell, wanted, wanted_text, answer, text))

    print("%d cells measured on plexlab, the 1.42.2 reference and plexscratch" % counted["cells"])
    print("  outcome  %d" % counted["outcome"])
    print("  and text %d" % counted["exact"])
    for cell, wanted, wanted_text, answer, text in misses[:args.examples]:
        print("%s  %s on %s\n    reference %s %r\n    model     %s %r"
              % (RED + "MISS" + OFF, cell["profile"], cell["row"],
                 wanted, wanted_text, answer, text))
    if len(misses) > args.examples:
        print("  ... %d more" % (len(misses) - args.examples))
    print("%s%d of %d cells the reference answers before a client has chosen an audio "
          "track are reproduced -- recorded, not emulated%s"
          % (GREY, recorded["reproduced"], recorded["cells"], OFF))
    if misses:
        print("%sFAIL%s  %d of %d cells exact" % (RED, OFF, counted["exact"], counted["cells"]))
        return 1
    print("%sPASS%s  %d of %d cells exact" % (GREEN, OFF, counted["exact"], counted["cells"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
