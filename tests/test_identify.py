#!/usr/bin/env python3
"""identify.py against the user's own Real-Debrid library, with DMM's labels as the key.

`fixtures/identify/corpus.json` is 200 labelled torrents (names and hashes as
Real-Debrid lists them, the IMDb id DMM maps each hash to) and every TMDB and Plex
answer the identifier read for them, trimmed, recorded 2026-10-06 by
`probes/record_identify_corpus.py`. Replayed offline: no network, no keys.

The old builder (the name to TMDB's first hit) named 167 of them right. The labels are
DMM's, not ground truth: the few disputed pairs are films sharing a title and a year,
where the label is as likely wrong as the match.

    python3 tests/test_identify.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "oracle"))
import identify  # noqa: E402
import metadata  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))


CORPUS = json.load(open(os.path.join(HERE, "fixtures", "identify", "corpus.json")))


class ReplayTmdb(metadata.Tmdb):
    def __init__(self, record):
        self._record = record
        self.missed = []

    def _get(self, path, **params):
        key = json.dumps([path, sorted(params.items())])
        if key not in self._record:
            self.missed.append(key)
        return self._record.get(key) or {}


class ReplayPlex(metadata.PlexProvider):
    def __init__(self, record):
        super().__init__("replay", None, opener=self._refuse)
        self._record = record
        self.missed = []

    @staticmethod
    def _refuse(method, path, body):
        raise AssertionError("no network in a replay")

    def _call(self, method, path, body=None):
        key = json.dumps([method, path, body], sort_keys=True)
        if key not in self._record:
            self.missed.append(key)
        return self._record.get(key) or {}


def parsing():
    # Each one a release in the library that the old parser got wrong.
    cases = [
        ("www.UIndex.org    -    The Matrix 1999 1080p Bluray OPUS 7 1 AV1-WhiskeyJack",
         "The Matrix", 1999, False),
        ("The.Matrix.Remastered.1999.2160p.UHD.BluRay.10bit.HDR.x265-HazMatt.mkv",
         "The Matrix", 1999, False),
        ("Matrix 4Kremux2160.pctreload1.com.mkv", "Matrix", None, False),
        ("The.Sopranos.S01.1080p.BluRay.Rus.Eng.HDCLUB", "The Sopranos", None, True),
        ("Planet.Earth.II.S01.1080p.BluRay.HEVC-tlacatlc6", "Planet Earth II", None, True),
        ("[Lia] One Piece - 0001-1000 [1080p]", "One Piece", None, True),
        ("For the Birds (2000).mkv", "For the Birds", 2000, False),
        ("How to Make a Killing (2026) {imdb-tt4357198}", "How to Make a Killing", 2026, False),
        ("Lanterns.2026.S01E08.1080p.x265-ELiTE.mkv", "Lanterns", 2026, True),
        ("Avatar - The Last Airbender (2005 - 2008) [1080p]", "Avatar - The Last Airbender",
         2005, True),
        # Named without SxxEyy; zurg filed each under movies on the test host or in production until
        # 2026-10-11.
        ("Mysterious Love E06 1080p WEB.DL AAC H.264-Luvmichelle.mp4", "Mysterious Love", None, True),
        ("Some Day or One Day - 1x02 (1080p VIKI WEB.DL Marshall).mkv", "Some Day or One Day",
         None, True),
        ("00000 (E01-The.Arrangement).m2ts", "00000 m2ts", None, True),
        # Filed under movies by the production server's Usenet zurg (2026-10-12), each an episode.
        ("Detektiv.Conan.E0221.Nichts.als.Luegen.2.German.1996.ANiME.FS.DL.1080p.BluRay.x264-STARS",
         "Detektiv Conan", 1996, True),
        ("One.Piece.EP1161.A.Dangerous.Deal.Loki.of.the.Underworld.and.Luffy.2160p.BILI.WEB-DL",
         "One Piece", None, True),
        ("Gintama.Ep.14.German.DTS.5.1.DL.1080p.BluRay.x264-AST4u", "Gintama", None, True),
        ("[Moozzi2] Eighty-Six-15 [BD 3840x2160 x265-10Bit FLACx2]", "Eighty-Six", None, True),
        ("[HorribleSubs].Gintama-340.[1080p]", "Gintama", None, True),
        ("[philosophy-raws][Samurai Champloo][13][BDRIP][Hi10P FLAC][1920X1080]", "Samurai Champloo", None, True),
        ("[Moozzi2] Natsume Yuujinchou Shichi-12 END [BD 1920x1080 x265-10Bit Flac]",
         "Natsume Yuujinchou Shichi", None, True),
        ("[HorribleSubs].Gintama-364.[1080p]-RakuvFIN", "Gintama", None, True),
        ("[RH] Shigatsu wa Kimi no Uso (Your Lie in April) - 01v2 - Monotone-Colorful [Dual Audio] [BDRip]",
         "Shigatsu wa Kimi no Uso", None, True),
        # Season packs named with `seasons`, and a film named twice.
        ("Family.Matters.seasons.01-09.AMZN.1080p.10bit.hevc.x265-bearfish", "Family Matters", None, True),
        ("South Park - Seasons 1 to 25 (S01-S25) Master Collection, The Movie & Extras [1080p-2160p]",
         "South Park", None, True),
        ("Der.mude.Tod.AKA.Destiny.1921.576p.BluRay.AAC.x264-HANDJOB", "Der mude Tod", 1921, False),
        ("Mr.Nice.Guy.aka.Yat.goh.ho.yan.1997.Extended.Original.Cut.2160p.UHD.Blu-ray", "Mr Nice Guy", 1997, False),
        # And what must stay a film: a year beside the dash, one digit after it, and the
        # release of the production server's only false hit, whose extra is `… - 2011 documentary.mkv`.
        ("Blade-Runner-2049", "Blade-Runner", 2049, False),
        ("Spider-Man-2", "Spider-Man-2", None, False),
        ("A Trip to the Moon (1902) Arrow (1080p BluRay x265 HEVC 10bit AAC 2.0 afm72)",
         "A Trip to the Moon", 1902, False),
    ]
    for name, title, year, show in cases:
        parsed = identify.parse_name(name)
        got = (parsed["title"], parsed["year"], identify.is_show(parsed))
        check("parse %r" % name[:50], got == (title, year, show), "got %s" % (got,))
    check("an id written into the name is read",
          identify.explicit_ids("How to Make a Killing (2026) {imdb-tt4357198}")
          == {"imdb": "tt4357198"})
    check("a tmdb tag is read", identify.explicit_ids("Film (2020) {tmdb-12345}") == {"tmdb": "12345"})


def corpus():
    tmdb, plex = ReplayTmdb(CORPUS["tmdb"]), ReplayPlex(CORPUS["plex"])
    identifier = identify.Identifier(tmdb=tmdb, plex=plex)
    torrents = CORPUS["torrents"]
    identities = [(t, identifier.identify(t["name"], files=t["files"])) for t in torrents]
    check("every provider answer the identifier asked for is recorded",
          not tmdb.missed and not plex.missed,
          "tmdb %s plex %s" % (tmdb.missed[:2], plex.missed[:2]))
    right = sum(1 for t, i in identities if i["imdb"] == t["truth"])
    old = sum(1 for t in torrents if t["old"] == t["truth"])
    check("names more of the library right than the old builder (%d vs %d of %d)"
          % (right, old, len(torrents)), right >= 190 and right > old,
          "right %d, old %d" % (right, old))
    agreed = [(t, i) for t, i in identities if i["confidence"] == identify.AGREED]
    agreed_right = sum(1 for t, i in agreed if i["imdb"] == t["truth"])
    check("where Plex and TMDB agree the answer is right at least 97%% (%d/%d)"
          % (agreed_right, len(agreed)),
          agreed and agreed_right / len(agreed) >= 0.97 and len(agreed) >= 150,
          "%d of %d" % (agreed_right, len(agreed)))
    wrong_confident = [t["name"] for t, i in identities
                       if i["imdb"] and i["imdb"] != t["truth"] and i["confidence"] != identify.AGREED
                       and "disputed" not in i]
    check("a wrong answer that is not marked agreed carries the competing match",
          len(wrong_confident) <= 2, "undisputed wrong: %s" % wrong_confident)
    check("every answer names a Plex guid, a kind and a source",
          all(i["plex"] and i["kind"] in ("movie", "show") and i["source"]
              for _t, i in identities if i["imdb"]))


def exact():
    known = {"a" * 40: "tt0133093"}

    class FindTmdb(ReplayTmdb):
        def _get(self, path, **params):
            if path == "find/tt0133093":
                return {"movie_results": [{"id": 603}], "tv_results": []}
            return {}

    class GuidPlex(ReplayPlex):
        def _call(self, method, path, body=None):
            if body == {"type": 1, "guid": "tmdb://603"}:
                return {"MediaContainer": {"Metadata": [{"guid": "plex://movie/5d776825880197001ec967c8"}]}}
            return {}

    identifier = identify.Identifier(tmdb=FindTmdb({}), plex=GuidPlex({}), known=known)
    by_hash = identifier.identify("Some.Unparseable.Thing.mkv", info_hash="A" * 40)
    check("a hash DMM knows is exact, whatever the name says",
          by_hash["confidence"] == identify.EXACT and by_hash["imdb"] == "tt0133093"
          and by_hash["tmdb"] == 603 and by_hash["plex"] == "plex://movie/5d776825880197001ec967c8",
          str(dict(by_hash)))
    tagged = identifier.identify("Matrix {imdb-tt0133093}")
    check("an id in the name is exact", tagged["confidence"] == identify.EXACT
          and tagged["source"] == "name id", str(dict(tagged)))
    nothing = identify.Identifier().identify("Unknown.Release.2020.mkv")
    check("with no source, nothing is guessed", nothing["confidence"] == identify.NONE
          and nothing["imdb"] is None)


def episode_ids():
    """`fixtures/identify/episode_ids.json`: one release of each of the 285 shows the production rex
    left unmatched on 2026-10-11 although zurg pushed the IMDb id DMM knows its hash by.
    271 of those ids are an episode's (Arrow's tt2340185 is its pilot), which TMDB's /find
    answers under `tv_episode_results`; the identifier read only movie and tv results, so
    each show stayed on `local://` with no TMDB id or Plex guid."""
    record = json.load(open(os.path.join(HERE, "fixtures", "identify", "episode_ids.json")))
    tmdb, plex = ReplayTmdb(record["tmdb"]), ReplayPlex(record["plex"])
    torrents = record["torrents"]
    known = {t["hash"]: t["known"] for t in torrents}
    identifier = identify.Identifier(tmdb=tmdb, plex=plex, known=known)
    identities = [(t, identifier.identify(t["name"], info_hash=t["hash"], files=t["files"]))
                  for t in torrents]
    check("every provider answer the identifier asked for is recorded",
          not tmdb.missed and not plex.missed,
          "tmdb %s plex %s" % (tmdb.missed[:2], plex.missed[:2]))
    shows = [(t, i) for t, i in identities if t["truth"]]
    right = [(t, i) for t, i in shows if i["kind"] == "show" and i["tmdb"] == t["truth"]
             and (i["plex"] or "").startswith("plex://show/")]
    check("an episode's IMDb id names its show, with a Plex guid (%d of %d)"
          % (len(right), len(shows)), len(shows) >= 270 and len(right) == len(shows),
          "missed %s" % [t["name"] for t, i in shows if (t, i) not in right][:3])
    arrow = next(i for t, i in identities if t["known"] == "tt2340185")
    check("the show is known by its own IMDb id, not the episode's",
          arrow["imdb"] == "tt2193021", str(dict(arrow)))


def blank_tiles():
    """`fixtures/identify/blank_tile_releases.json`: one release of each movie and show the production rex
    drew the blank tile for on 2026-10-12 (56 films, 45 shows), as zurg pushed it -- the
    IMDb id DMM gave its hash, the TMDB id zurg's own Plex gave it, or nothing -- in the
    library the feed files it in, with the TMDB id it is labelled by hand. Before this,
    10 of the 112 labelled were named (82 now): TMDB knew none of DMM's episode ids and none of
    zurg's episode TMDB ids, Plex's matcher fed DMM stub films with no TMDB id, a
    library's kind did not decide the kind asked, and absolute anime episode numbers and
    `AKA` titles were not read."""
    record = json.load(open(os.path.join(HERE, "fixtures", "identify", "blank_tile_releases.json")))
    tmdb, plex = ReplayTmdb(record["tmdb"]), ReplayPlex(record["plex"])
    torrents = record["torrents"]
    identifier = identify.Identifier(tmdb=tmdb, plex=plex,
                                     known={t["hash"]: t["known"] for t in torrents if t.get("known")})
    identities = [(t, identify.identify_release(identifier, t)) for t in torrents]
    check("every provider answer the identifier asked for is recorded",
          not tmdb.missed and not plex.missed,
          "tmdb %s plex %s" % (tmdb.missed[:2], plex.missed[:2]))
    labelled = [(t, i) for t, i in identities if t["truth"]]
    right = [(t, i) for t, i in labelled if i["tmdb"] == t["truth"] and i["kind"] == t["kind"]]
    check("a release on the blank tile is named in its own library's kind (%d of %d)"
          % (len(right), len(labelled)), len(labelled) == 112 and len(right) >= 82,
          "missed %s" % [t["name"][:40] for t, i in labelled if (t, i) not in right][:5])
    check("a release the library cannot hold is never given the other kind's id",
          all(i["kind"] == t["kind"] for t, i in identities if i["tmdb"] and i["confidence"] != identify.EXACT),
          [(t["name"][:40], i["kind"]) for t, i in identities if i["tmdb"] and i["kind"] != t["kind"]][:3])


def season_decides():
    """`fixtures/identify/tom_and_jerry.json`, recorded on the test host 2026-10-11. Plex's matcher
    answers `The.Tom.and.Jerry.Show.S02E05` with the 1975 series, one season long; TMDB's
    first hit is the 2014 series, five seasons. Plex won the dispute and every episode sat
    under a show that has no season 2 or 3."""
    record = json.load(open(os.path.join(HERE, "fixtures", "identify", "tom_and_jerry.json")))
    tmdb, plex = ReplayTmdb(record["tmdb"]), ReplayPlex(record["plex"])
    identifier = identify.Identifier(tmdb=tmdb, plex=plex)
    identities = [(t, identifier.identify(t["name"], files=t["files"])) for t in record["torrents"]]
    check("every provider answer the identifier asked for is recorded",
          not tmdb.missed and not plex.missed,
          "tmdb %s plex %s" % (tmdb.missed[:2], plex.missed[:2]))
    check("a season only TMDB's show has decides the dispute, with that show's Plex guid",
          all(i["tmdb"] == t["truth"] and i["kind"] == "show"
              and (i["plex"] or "").startswith("plex://show/") for t, i in identities),
          str([(i["tmdb"], i["plex"]) for _t, i in identities]))
    check("and the show Plex named is kept as the disputed one",
          all((i.get("disputed") or {}).get("tmdb") == 7842 for _t, i in identities),
          str([i.get("disputed") for _t, i in identities]))

    class ShowWithoutTheEpisode:
        """TMDB as it answered the 1975 show: the show, and nothing for S02E05."""
        def details(self, kind, tmdb_id):
            return {"id": 7842, "name": "The Tom and Jerry Show", "first_air_date": "1975-09-06",
                    "external_ids": {"imdb_id": "tt0130417"}}

        def season(self, tmdb_id, season):
            return {}

        def episode(self, tmdb_id, season, episode):
            return None

    row = {"guid": "local://100095", "title": "Episode 5"}
    metadata.enrich(row, ShowWithoutTheEpisode(),
                    {"title": "The Tom and Jerry Show", "year": None, "season": 2, "episode": 5},
                    chosen=("tv", 7842))
    check("an episode TMDB lacks keeps its own guid, not a minted movie one or its show's ids",
          row["guid"] == "local://100095" and "Guid" not in row, str((row["guid"], row.get("Guid"))))


def unplaced_episodes():
    """`fixtures/identify/unplaced_episodes.json`: files the production rex's feed dropped as
    "no episode number" on 2026-10-13 (6,214 of them since that morning's start), one or more
    of each name shape, and two it placed on the wrong episode. The feed reads a file's
    episode as `episode_of` in `rust/src/runtime/scan.rs` does: the file's number, else the
    release's; the file's season, else the release's, else 1."""
    record = json.load(open(os.path.join(HERE, "fixtures", "identify", "unplaced_episodes.json")))
    for case in record["files"]:
        release, file = identify.parse_name(case["release"]), identify.parse_name(case["file"])
        episode = file["episode"] if file["episode"] is not None else release["episode"]
        got = None if episode is None else (file["season"] or release["season"] or 1, episode)
        want = None if case["episode"] is None else (case["season"], case["episode"])
        check("%s: %s" % (case["shape"], case["file"][:60]), got == want, "got %s" % (got,))


if __name__ == "__main__":
    parsing()
    unplaced_episodes()
    corpus()
    exact()
    episode_ids()
    blank_tiles()
    season_decides()
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)
