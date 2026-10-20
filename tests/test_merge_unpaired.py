#!/usr/bin/env python3
"""A show merge whose episodes do not all pair with the target's.

Measured on plexscratch (PMS 1.43.4, `tests/fixtures/merge_unpaired_show_s43.json`):
`PUT /library/metadata/6/merge?ids=58` of Firefly (S1E1-3, S2E1-3) and a probe show
holding S01E01 and S03E01. The paired S1E1 became a second Media of Firefly's S1E1 and
its own key answers 404; the unpaired S3E1 moved under Firefly on ITS OWN key, into a
season 3 Firefly gained on a new key; the probe show answers 404. Both servers skipped
any show whose leaves did not all pair, so the merge absorbed nothing ("0 row(s)
absorbed") and the wrong-show rows on rex-fun could not be folded into their twins.

The fixture's roles are replayed on `tests/catalog_ui.json`'s Simpsons: the probe's
paired season is one the Simpsons has, its unpaired season one it lacks, and a second
unpaired episode lands in a season the Simpsons already has.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_merge_unpaired.py
"""
import copy
import json
import os
import sys
import tempfile

from test_followups_c87_e4 import ENV, ROOT, check, container, request, results, stop
import test_followups_c87_e4 as harness

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "merge_unpaired_show_s43.json")
ORDER = os.path.join(HERE, "fixtures", "merge_shows_media_order_s43.json")
TARGET = "20001"
PROBE = "20002"


def build_catalog(path):
    """catalog_ui.json plus a probe show: the fixture's paired and unpaired episodes,
    and one more unpaired episode in a season the Simpsons has."""
    with open(os.path.join(HERE, "catalog_ui.json")) as handle:
        document = json.load(handle)
    rows = document["items"]
    show = next(row for row in rows if row["ratingKey"] == TARGET)
    template = next(row for row in rows if row["ratingKey"] == "1002")
    season_template = next(row for row in rows if row["ratingKey"] == "30002")
    fixture = json.load(open(FIXTURE))
    probe_seasons = fixture["before"]["58"]["seasons"]
    # Fixture season 1 pairs (Firefly has it), season 3 does not.
    index_of = {"1": 20, "3": 23}
    roles = {}
    probe = dict(copy.deepcopy(show), ratingKey=PROBE, key="/library/metadata/%s/children" % PROBE,
                 title="Qzxv Merge Probe", guid="local://%s" % PROBE)
    probe.pop("Guid", None)
    rows.append(probe)
    season_key, episode_key = 30100, 1100
    plan = [(index_of[s], int(e), probe_seasons[s]["episodes"][e]) for s in probe_seasons
            for e in probe_seasons[s]["episodes"]] + [(19, 2, "extra")]
    seasons = {}
    for season_index, index, reference in plan:
        if season_index not in seasons:
            season_key += 1
            seasons[season_index] = str(season_key)
            rows.append(dict(copy.deepcopy(season_template), ratingKey=str(season_key),
                             key="/library/metadata/%d/children" % season_key,
                             parentRatingKey=PROBE, index=season_index, title="Season %d" % season_index,
                             guid="local://%d" % season_key, parentTitle=probe["title"],
                             thumb="/library/metadata/%d/thumb/1700000000" % season_key))
        episode_key += 1
        rows.append(dict(copy.deepcopy(template), ratingKey=str(episode_key),
                         key="/library/metadata/%d" % episode_key,
                         parentRatingKey=seasons[season_index], grandparentRatingKey=PROBE,
                         parentIndex=season_index, index=index,
                         guid="local://%d" % episode_key, title="Probe %d" % episode_key))
        roles[reference] = (str(episode_key), season_index, index)
    with open(path, "w") as handle:
        json.dump(document, handle)
    return fixture, roles


def episode_at(port, show, season_index, index):
    _, seasons = container(port, "/library/metadata/%s/children" % show)
    for season in seasons.get("Metadata") or []:
        if season.get("index") == season_index:
            _, episodes = container(port, "/library/metadata/%s/children" % season["ratingKey"])
            for episode in episodes.get("Metadata") or []:
                if episode.get("index") == index:
                    return season, episode
    return None, None


def media_ids(episode):
    return [media.get("id") for media in (episode or {}).get("Media") or []]


def media_order():
    """Two sources, and the order of a merged episode's Media (plexscratch, 2026-10-20,
    `merge_shows_media_order_s43.json`): `merge?ids=A,B` of two shows holding the S1E5 the
    target lacks left one row, A's, with both Media; a merged episode lists its Media by id,
    so a newer source's goes after the target's and an older one's before it. rex paired every
    source with the target as it was before the merge, and put the absorbed Media first."""
    recorded = json.load(open(ORDER))
    setup, steps = recorded["setup"], recorded["steps"]
    by_key = lambda answer: {row["ratingKey"]: [media["id"] for media in row["Media"]]
                             for row in answer["body"]["MediaContainer"]["Metadata"]}
    after_pair = by_key(steps[0]["answers"]["GET /library/metadata/63/allLeaves"])
    after_older = by_key(steps[2]["answers"]["GET /library/metadata/63/allLeaves"])
    check("fixture: the two sources' S1E5 is one row, the first's, with both Media by id",
          after_pair.get("62") == setup["A"]["leaves"]["62"]["media"] + setup["B"]["leaves"]["68"]["media"]
          and "68" not in after_pair, after_pair)
    check("fixture: a newer source's Media go after the target's, an older one's before them",
          steps[1]["leaves"]["65"] == [32, 34] and after_older.get("65") == [6, 32, 34], (steps[1], after_older))

    # The roles on catalog_ui.json's Simpsons (20001), whose S20E1 (1002) holds Media 100200:
    # A and B each hold the S20E5 it lacks, C a newer S20E1 and F an older one.
    workdir = tempfile.mkdtemp(prefix="rex-merge-order-")
    harness.CATALOG = os.path.join(workdir, "catalog.json")
    with open(os.path.join(HERE, "catalog_ui.json")) as handle:
        document = json.load(handle)
    rows = document["items"]
    show = next(row for row in rows if row["ratingKey"] == TARGET)
    template = next(row for row in rows if row["ratingKey"] == "1002")
    season_template = next(row for row in rows if row["ratingKey"] == "30002")
    sources = {"A": ("20003", 5, 100700), "B": ("20004", 5, 100800), "C": ("20005", 1, 100900), "F": ("20006", 1, 100050)}
    episodes = {}
    for offset, (name, (key, index, media)) in enumerate(sorted(sources.items())):
        season_key, episode_key = str(30200 + offset), str(1200 + offset)
        rows.append(dict(copy.deepcopy(show), ratingKey=key, key="/library/metadata/%s/children" % key,
                         title="Order Probe %s" % name, guid="local://%s" % key))
        rows[-1].pop("Guid", None)
        rows.append(dict(copy.deepcopy(season_template), ratingKey=season_key, parentRatingKey=key,
                         key="/library/metadata/%s/children" % season_key, guid="local://%s" % season_key))
        episode = dict(copy.deepcopy(template), ratingKey=episode_key, key="/library/metadata/%s" % episode_key,
                       parentRatingKey=season_key, grandparentRatingKey=key, index=index,
                       guid="local://%s" % episode_key, title="Probe %s" % name)
        episode["Media"][0]["id"] = media
        episode["Media"][0]["Part"][0]["id"] = media // 10
        rows.append(episode)
        episodes[name] = episode_key
    with open(harness.CATALOG, "w") as handle:
        json.dump(document, handle)
    process, port = harness.start()
    try:
        request(port, "PUT", "/library/metadata/%s/merge?ids=%s,%s" % (TARGET, sources["A"][0], sources["B"][0]))
        _, season = container(port, "/library/metadata/30002/children")
        fifth = [episode for episode in season.get("Metadata") or [] if episode.get("index") == 5]
        check("two sources' S20E5 is one row under the target, the first's, with both Media by id",
              [episode["ratingKey"] for episode in fifth] == [episodes["A"]] and media_ids(fifth[0]) == [100700, 100800],
              [(episode["ratingKey"], media_ids(episode)) for episode in fifth])
        request(port, "PUT", "/library/metadata/%s/merge?ids=%s" % (TARGET, sources["C"][0]))
        _, first = container(port, "/library/metadata/1002")
        check("a newer source's Media go after the target's",
              media_ids((first.get("Metadata") or [{}])[0]) == [100200, 100900], media_ids((first.get("Metadata") or [{}])[0]))
        request(port, "PUT", "/library/metadata/%s/merge?ids=%s" % (TARGET, sources["F"][0]))
        _, first = container(port, "/library/metadata/1002")
        check("an older source's Media go before them",
              media_ids((first.get("Metadata") or [{}])[0]) == [100050, 100200, 100900], media_ids((first.get("Metadata") or [{}])[0]))
    finally:
        stop(process)


def movie_media_order():
    """Replay measured older/newer movie merges and Fix Match absorption."""
    measured = json.load(open(os.path.join(HERE, "fixtures", "collection_open_items_s43.json")))
    with tempfile.TemporaryDirectory(prefix="rex-movie-order-") as workdir:
        harness.CATALOG = os.path.join(workdir, "catalog.json")
        with open(os.path.join(HERE, "catalog_ui.json")) as handle:
            document = json.load(handle)
        template = next(row for row in document["items"] if row["type"] == "movie")
        mapping = {key: str(1050 + index) for index, key in enumerate(measured["movies"])}
        document["items"] = []
        for key, recorded in measured["movies"].items():
            if key == "60":
                continue  # Created by the recorded split, not a duplicate of Media 12 at start.
            movie = dict(copy.deepcopy(template), **copy.deepcopy(recorded))
            movie.update(ratingKey=mapping[key], key="/library/metadata/" + mapping[key])
            # Keep complete, renderable Media while giving each its measured identities.
            movie["Media"] = []
            for media in recorded["Media"]:
                full = copy.deepcopy(template["Media"][0])
                full["id"] = media["id"]
                full["Part"][0]["id"] = media["Part"][0]["id"]
                movie["Media"].append(full)
            document["items"].append(movie)
        with open(harness.CATALOG, "w") as handle:
            json.dump(document, handle)
        process, port = harness.start()
        try:
            for step in measured["merges"]:
                path = step["path"]
                source = path.split("/")[3]
                path = path.replace("/metadata/" + source + "/", "/metadata/" + mapping[source] + "/")
                if "ids=" in path:
                    absorbed = path.split("ids=")[1]
                    path = path.replace("ids=" + absorbed, "ids=" + mapping[absorbed])
                else:
                    absorbed = source
                status, _ = request(port, step["method"], path)
                expected = step["after"]
                _, answer = container(port, "/library/metadata/" + mapping[expected["ratingKey"]])
                actual = (answer.get("Metadata") or [{}])[0]
                identities = [{"id": media["id"], "Part": [{"id": part["id"]} for part in media.get("Part", [])]}
                              for media in actual.get("Media", [])]
                check("movie %s preserves numeric Media order and Part identities" % step["path"],
                      status == step["status"] and identities == expected["Media"], (status, identities))
                check("movie merge keeps target metadata, not source metadata",
                      (actual.get("title"), actual.get("guid")) == (expected["title"], expected["guid"]),
                      (actual.get("title"), actual.get("guid")))
                gone, _ = request(port, "GET", "/library/metadata/" + mapping[absorbed])
                check("absorbed movie key is retired", gone == 404, gone)
                if source == "17" and "ids=" in path:
                    # The reference split this first merge before matching its newer Media
                    # into movie 4; replay it so each Media/Part identity remains unique.
                    before = {row["ratingKey"] for row in container(port, "/library/sections/1/all")[1].get("Metadata", [])}
                    request(port, "PUT", "/library/metadata/" + mapping["17"] + "/split")
                    after = {row["ratingKey"] for row in container(port, "/library/sections/1/all")[1].get("Metadata", [])}
                    mapping["60"] = (after - before).pop()
        finally:
            stop(process)


def main():
    movie_media_order()
    media_order()
    workdir = tempfile.mkdtemp(prefix="rex-merge-")
    harness.CATALOG = os.path.join(workdir, "catalog.json")
    fixture, roles = build_catalog(harness.CATALOG)
    process, port = harness.start()
    try:
        _, before = container(port, "/library/metadata/30002/children")
        paired_media = len(next(e for e in before["Metadata"] if e["index"] == 1)["Media"])
        _, show_before = container(port, "/library/metadata/%s/children" % TARGET)
        season_keys_before = {s["ratingKey"] for s in show_before["Metadata"]}

        status, _ = request(port, "PUT", "/library/metadata/%s/merge?ids=%s" % (TARGET, PROBE))
        check("the merge answers 200", status == 200, status)

        want = fixture["after"]["status"]
        status, _ = request(port, "GET", "/library/metadata/%s" % PROBE)
        check("the merged show answers %d" % want["58"], status == want["58"], status)

        paired_key, season_index, index = roles["62"]
        status, _ = request(port, "GET", "/library/metadata/%s" % paired_key)
        check("the paired episode's own key answers %d" % want["62"], status == want["62"], status)
        _, episode = episode_at(port, TARGET, season_index, index)
        check("the paired episode is another Media of the target's (%d -> %d)"
              % (paired_media, fixture["after"]["6"]["seasons"]["1"]["episodes"]["1"]["media"]),
              episode is not None and len(episode.get("Media") or []) == paired_media + 1,
              episode and len(episode.get("Media") or []))

        moved_key, season_index, index = roles["60"]
        status, _ = request(port, "GET", "/library/metadata/%s" % moved_key)
        check("the unpaired episode answers %d on its own key" % want["60"], status == want["60"], status)
        season, episode = episode_at(port, TARGET, season_index, index)
        check("the unpaired episode sits under the target on its own key",
              episode is not None and episode["ratingKey"] == moved_key
              and str(episode.get("grandparentRatingKey")) == TARGET
              and episode.get("grandparentTitle") == "The Simpsons",
              episode)
        check("in a season of its index the target gained on a new key",
              season is not None and season["ratingKey"] not in season_keys_before
              and season["ratingKey"] not in ("30101", "30102", "30103")
              and str(season.get("parentRatingKey")) == TARGET,
              season)

        status, _ = request(port, "GET", season.get("thumb") or "/none") if season else (None, None)
        check("and the new season's poster is one that loads (%s)" % (season or {}).get("thumb"), status == 200, status)

        extra_key, season_index, index = roles["extra"]
        season, episode = episode_at(port, TARGET, season_index, index)
        check("an unpaired episode of a season the target has joins that season",
              episode is not None and episode["ratingKey"] == extra_key and season["ratingKey"] == "30001",
              (season and season["ratingKey"], episode))
        _, show = container(port, "/library/metadata/%s" % TARGET)
        row = (show.get("Metadata") or [{}])[0]
        check("the target counts the moved episodes (childCount 5, leafCount 10)",
              row.get("childCount") == 5 and row.get("leafCount") == 10,
              (row.get("childCount"), row.get("leafCount")))
    finally:
        stop(process)
    passed = sum(results)
    print("%s %d/%d unpaired show merge checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
