#!/usr/bin/env python3
"""A source names what Plex does not know: `display` on a feed item (docs/feed-api.md, "Display").

The rex-youtube addon pushes YouTube videos no metadata agent knows; until 2026-10-23 their
titles, summaries, dates and artwork could only be written through the Plex edit API with the
server owner's token. `fixtures/feed/display_msrachel_1023.json` is two of its episodes as it
pushes them: the episode's own fields, its show's and its season's.

    python3 tests/test_feed_display.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import urllib.parse

from test_feed import MOUNT, Bytes, Server, catalog, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "display_msrachel_1023.json")))
# A film known only by its year: the Internet Archive's His Girl Friday, as the addon pushes it.
FILM = json.load(open(os.path.join(HERE, "fixtures", "feed", "display_archive_1023.json")))["items"][0]


def pushed(item):
    return dict(copy.deepcopy(item), file="%s/%s" % (MOUNT, item["path"]))


def artwork(server, key, kind):
    """The remote URLs a row's poster or background picker lists."""
    status, body = server.request("/library/metadata/%s/%s" % (key, kind))
    rows = (body or {}).get("MediaContainer", {}).get("Metadata", []) if status == 200 else []
    return [row.get("key") for row in rows]


def main():
    bytes_port = serve(Bytes().handler())
    work = tempfile.mkdtemp(prefix="rex-display-")
    catalog(work)
    server = Server(work, bytes_port)
    first, second = FIXTURE["items"]
    try:
        status, body = server.feed("POST", "items", {"items": [pushed(first), pushed(second)]})
        check("both episodes are created", status == 200 and (body or {}).get("created") == 2, (status, body))
        keys = server.keys()
        episode = server.row(keys[first["id"]])
        display = first["display"]
        check("the episode is titled as its source says, not 'Episode 1'",
              episode.get("title") == display["title"], episode.get("title"))
        check("its summary and date are its source's",
              episode.get("summary") == display["summary"] and episode.get("originallyAvailableAt") == display["date"]
              and episode.get("year") == int(display["date"][:4]),
              (episode.get("summary", "")[:40], episode.get("originallyAvailableAt"), episode.get("year")))
        check("it sits where its source placed it", (episode.get("parentIndex"), episode.get("index")) == (1, 1),
              (episode.get("parentIndex"), episode.get("index")))
        show = server.row(episode.get("grandparentRatingKey"))
        season = server.row(episode.get("parentRatingKey"))
        check("its show is titled and described as its source says",
              show.get("title") == display["show"]["title"] and show.get("summary") == display["show"]["summary"],
              (show.get("title"), show.get("summary", "")[:40]))
        check("both episodes are that one show's", show.get("leafCount") == 2, show.get("leafCount"))
        check("its season is described as its source says", season.get("summary") == display["season"]["summary"],
              season.get("summary"))
        check("the episode's still is its thumb", display["thumb"] in artwork(server, episode["ratingKey"], "posters"),
              artwork(server, episode["ratingKey"], "posters"))
        check("the show's poster and background are its source's",
              display["show"]["poster"] in artwork(server, show["ratingKey"], "posters")
              and display["show"]["art"] in artwork(server, show["ratingKey"], "arts"),
              (artwork(server, show["ratingKey"], "posters"), artwork(server, show["ratingKey"], "arts")))
        check("the season's poster is its source's", display["season"]["poster"] in artwork(server, season["ratingKey"], "posters"),
              artwork(server, season["ratingKey"], "posters"))

        # A later push: the video was retitled and the channel's description changed.
        again = pushed(first)
        again["display"]["title"] = display["title"] + " (Remastered)"
        again["display"]["show"]["summary"] = "The channel, described again."
        status, body = server.feed("POST", "items", {"items": [again]})
        check("a changed display updates the row", status == 200 and (body or {}).get("updated") == 1, (status, body))
        check("the episode takes its new title", server.row(episode["ratingKey"]).get("title") == again["display"]["title"],
              server.row(episode["ratingKey"]).get("title"))
        check("the show takes its new summary", server.row(show["ratingKey"]).get("summary") == "The channel, described again.",
              server.row(show["ratingKey"]).get("summary"))

        # A person's edit is theirs: a locked title stays, an unlocked summary still follows.
        query = urllib.parse.urlencode({"type": 4, "id": episode["ratingKey"], "title.value": "Our clean-up song", "title.locked": 1})
        status, _ = server.request("/library/sections/2/all?" + query, "PUT")
        check("a person retitles the episode", status == 200, status)
        third = pushed(first)
        third["display"]["title"] = "Yet another title"
        third["display"]["summary"] = "A new description."
        server.feed("POST", "items", {"items": [third]})
        row = server.row(episode["ratingKey"])
        check("a push leaves a title a person locked", row.get("title") == "Our clean-up song", row.get("title"))
        check("and still writes the summary they did not", row.get("summary") == "A new description.", row.get("summary"))

        # A poster a person uploaded is theirs: a later display.thumb does not replace it.
        jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
        status, _ = server.request("/library/metadata/%s/posters" % episode["ratingKey"], "POST", jpeg, {"Content-Type": "image/jpeg"})
        check("a person uploads the episode's poster", status == 200, status)
        fourth = pushed(first)
        fourth["display"]["thumb"] = "https://i.ytimg.com/vi/w264Mn-2MnQ/hqdefault.jpg"
        server.feed("POST", "items", {"items": [fourth]})
        status, body = server.request("/library/metadata/%s/posters" % episode["ratingKey"])
        chosen = [row.get("key") for row in (body or {}).get("MediaContainer", {}).get("Metadata", []) if row.get("selected")]
        check("a push leaves the poster a person uploaded",
              len(chosen) == 1 and "upload%3A%2F%2F" in chosen[0] and fourth["display"]["thumb"] not in chosen, chosen)

        bad = pushed(second)
        bad["id"] = "bad"
        bad["path"] = "shows/msrachel/bad.m3u8"
        bad["file"] = MOUNT + "/shows/msrachel/bad.m3u8"
        bad["display"]["thumb"] = "file:///etc/passwd"
        status, body = server.feed("POST", "items", {"items": [bad]})
        check("artwork that is no http(s) URL is refused",
              status == 200 and (body or {}).get("rejected") == [{"id": "bad", "reason": "display.thumb must be an http(s) URL"}],
              (status, body))

        # An old film's source knows its year and no day: rex shows the year and makes up no day.
        status, body = server.feed("POST", "items", {"items": [FILM]})
        check("a film dated by its year alone is taken", status == 200 and (body or {}).get("created") == 1, (status, body))
        film = server.row(server.keys()[FILM["id"]])
        check("it carries that year and no release day",
              film.get("year") == 1940 and "originallyAvailableAt" not in film, (film.get("year"), film.get("originallyAvailableAt")))
        odd = dict(copy.deepcopy(FILM), id="odd", path="movies/odd/odd.mp4", file=MOUNT + "/movies/odd/odd.mp4",
                   display=dict(FILM["display"], date="194"))
        status, body = server.feed("POST", "items", {"items": [odd]})
        check("a date of another shape is still refused",
              status == 200 and (body or {}).get("rejected") == [{"id": "odd", "reason": "display.date must be YYYY-MM-DD or YYYY"}],
              (status, body))
    finally:
        server.stop()
    shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
