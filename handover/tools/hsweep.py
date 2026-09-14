"""Hold plexsim's own DERIVATION engine out against a real library's rows.

Cycle 20. `fsweep.py` did this for `filters.compare` and `ssweep.py` for the sort
engine; this is the same shape for `hierarchy.py` -- every rule `normalize_rows`
and `_carry` apply, checked row by row against zen's 507 shows, 741 seasons and
9,287 episodes. Read-only: the rows are the cached listing rows from fetch_zen.py.

A rule that agrees on 10,535 real rows is settled; one that disagrees names the rows.
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, "/Users/bensarmiento/debridmediamanager/plexsim")
import hierarchy as H

SP = os.environ.get("ZEN_CACHE", os.path.dirname(os.path.abspath(__file__)))
shows = json.load(open(SP + "/zen/shows.json"))
seasons = json.load(open(SP + "/zen/seasons.json"))
eps = json.load(open(SP + "/zen/episodes.json"))
by_key = {str(r["ratingKey"]): r for r in shows + seasons + eps}

results = []


def rule(name, rows, predicate):
    """predicate(row) -> None to skip, True to agree, or a string naming the disagreement."""
    agree = 0
    bad = Counter()
    examples = {}
    skipped = 0
    for r in rows:
        v = predicate(r)
        if v is None:
            skipped += 1
        elif v is True:
            agree += 1
        else:
            bad[v] += 1
            examples.setdefault(v, (r.get("ratingKey"), r.get("title"),
                                    r.get("parentTitle"), r.get("grandparentTitle")))
    results.append((name, agree, sum(bad.values()), skipped, bad, examples))


# --- the parent carry -------------------------------------------------------
def season_parent(field, pfield=None):
    def f(r):
        show = by_key.get(str(r.get("parentRatingKey")))
        if show is None:
            return None
        want = show.get(pfield or field)
        got = r.get(field if pfield is None else pfield)
        if want is None and got is None:
            return True
        if want is None:
            return "season has %s=%r, show has none" % (pfield or field, got)
        if got is None:
            return "show has %s=%r, season has none" % (field, want)
        return True if got == want else "differs"
    return f


rule("season.parentTitle == show.title", seasons,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentTitle") == s.get("title")
                 else "%r != %r" % (r.get("parentTitle"), s.get("title"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("season.parentGuid == show.guid", seasons,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentGuid") == s.get("guid")
                 else "%r != %r" % (r.get("parentGuid"), s.get("guid"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("season.parentIndex == show.index", seasons,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentIndex") == s.get("index")
                 else "%r != %r" % (r.get("parentIndex"), s.get("index"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("season.parentStudio == show.studio", seasons, season_parent("studio", "parentStudio"))
rule("season.parentThumb == show.thumb", seasons,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentThumb") == s.get("thumb")
                 else "%r != %r" % (r.get("parentThumb"), s.get("thumb"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("season.art == show.art", seasons,
     lambda r: (lambda s: None if s is None else
                (True if r.get("art") == s.get("art")
                 else "%r != %r" % (r.get("art"), s.get("art"))))(
         by_key.get(str(r.get("parentRatingKey")))))

rule("episode.parentTitle == season.title", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentTitle") == s.get("title")
                 else "%r != %r" % (r.get("parentTitle"), s.get("title"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("episode.parentGuid == season.guid", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentGuid") == s.get("guid")
                 else "%r != %r" % (r.get("parentGuid"), s.get("guid"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("episode.parentIndex == season.index", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentIndex") == s.get("index")
                 else "%r != %r" % (r.get("parentIndex"), s.get("index"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("episode.grandparentTitle == show.title", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("grandparentTitle") == s.get("title")
                 else "%r != %r" % (r.get("grandparentTitle"), s.get("title"))))(
         by_key.get(str(r.get("grandparentRatingKey")))))
rule("episode.grandparentGuid == show.guid", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("grandparentGuid") == s.get("guid")
                 else "%r != %r" % (r.get("grandparentGuid"), s.get("guid"))))(
         by_key.get(str(r.get("grandparentRatingKey")))))
rule("episode.grandparentThumb == show.thumb", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("grandparentThumb") == s.get("thumb")
                 else "%r != %r" % (r.get("grandparentThumb"), s.get("thumb"))))(
         by_key.get(str(r.get("grandparentRatingKey")))))
rule("episode.grandparentArt == show.art", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("grandparentArt") == s.get("art")
                 else "%r != %r" % (r.get("grandparentArt"), s.get("art"))))(
         by_key.get(str(r.get("grandparentRatingKey")))))
rule("episode.art == season.art", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("art") == s.get("art")
                 else "%r != %r" % (r.get("art"), s.get("art"))))(
         by_key.get(str(r.get("parentRatingKey")))))
rule("episode.parentThumb == season.thumb", eps,
     lambda r: (lambda s: None if s is None else
                (True if r.get("parentThumb") == s.get("thumb")
                 else "%r != %r" % (r.get("parentThumb"), s.get("thumb"))))(
         by_key.get(str(r.get("parentRatingKey")))))

# --- normalize_rows' own rules ---------------------------------------------
rule("show.key ends /children", shows,
     lambda r: True if str(r.get("key", "")).endswith("/children") else str(r.get("key")))
rule("season.key ends /children", seasons,
     lambda r: True if str(r.get("key", "")).endswith("/children") else str(r.get("key")))
rule("episode.key is the bare item path", eps,
     lambda r: True if str(r.get("key", "")) == "/library/metadata/%s" % r["ratingKey"]
     else str(r.get("key")))
rule("show.index == 1", shows,
     lambda r: True if r.get("index") == 1 else repr(r.get("index")))

rule("season: year => titleSort == title and no parentYear", seasons,
     lambda r: None if not r.get("year") else
     (True if r.get("titleSort") == r.get("title") and r.get("parentYear") is None
      else "titleSort=%r title=%r parentYear=%r" % (r.get("titleSort"), r.get("title"),
                                                    r.get("parentYear"))))
rule("season: no year => parentYear present, year absent", seasons,
     lambda r: None if r.get("year") else
     (True if r.get("parentYear") is not None else "no parentYear either"))
rule("season: skipCount never 0", seasons,
     lambda r: True if r.get("skipCount") != 0 else "skipCount 0")
rule("season: no contentRating", seasons,
     lambda r: True if r.get("contentRating") is None else repr(r.get("contentRating")))
rule("season: no audienceRating", seasons,
     lambda r: True if r.get("audienceRating") is None else repr(r.get("audienceRating")))
rule("episode: contentRating == show's", eps,
     lambda r: (lambda s: None if s is None or not s.get("contentRating") else
                (True if r.get("contentRating") == s.get("contentRating")
                 else "%r != show %r" % (r.get("contentRating"), s.get("contentRating"))))(
         by_key.get(str(r.get("grandparentRatingKey")))))
rule("episode: audienceRating => audienceRatingImage", eps,
     lambda r: None if r.get("audienceRating") is None else
     (True if r.get("audienceRatingImage") else "no audienceRatingImage"))
rule("episode: no rating column", eps,
     lambda r: True if r.get("rating") is None else repr(r.get("rating")))
rule("episode: chapterSource only with chapters", eps,
     lambda r: True if r.get("chapterSource") is None else repr(r.get("chapterSource")))
rule("no row names a theme", shows + seasons + eps,
     lambda r: True if not any(r.get(k) for k in ("theme", "parentTheme", "grandparentTheme"))
     else "has one")

# --- the sort title, on every row type -------------------------------------
def ts(r):
    title = r.get("title") or ""
    derived = H.sort_title(title)
    want = derived if derived and derived != title else None
    got = r.get("titleSort")
    if r.get("type") == "season" and r.get("year"):
        want = title  # the one row type that carries an equal one
    if got == want:
        return True
    return "title=%r want=%r got=%r" % (title, want, got)


rule("titleSort derivation (shows)", shows, ts)
rule("titleSort derivation (seasons)", seasons, ts)
rule("titleSort derivation (episodes)", eps, ts)

for name, agree, dis, skipped, bad, ex in results:
    flag = "OK " if dis == 0 else "!! "
    print("%s%-48s agree %5d  differ %5d  skipped %5d" % (flag, name, agree, dis, skipped))
    if dis:
        for reason, n in bad.most_common(4):
            print("      %5d  %s   e.g. %r" % (n, reason[:90], ex[reason]))
