#!/usr/bin/env python3
"""A source corrects its recorded TorBox name through the ordinary same-ID feed.

The provider's names/path/hash are captured input. The ready probe and Plex answers
are separate real PMS fixtures; their use here models an already-probed file, not
an identification of the TorBox release from its name. The identified stale-title
leg explicitly models an old cached row while retaining its measured guid/artwork.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

from test_feed import Bytes, CACHE, ENTRIES, MOUNT, Server, catalog, check, item, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "torbox_hash_name.json")))
OTHER_GUID = CACHE["plex"]['POST /library/metadata/matches {"guid": "imdb://tt0103827", "type": 1}'][
    "MediaContainer"]["Metadata"][0]["Similar"][0]["guid"]


def main():
    source = Bytes()
    path = "anime/" + FIXTURE["file"]
    pushed = {"id": path, "path": path, "file": MOUNT + "/" + path, "name": FIXTURE["name"],
              "release": "anime/" + FIXTURE["corrected_name"], "hash": FIXTURE["hash"],
              "size": FIXTURE["size"], "media_info": ENTRIES["episode"]["entry"]["media_info"]}
    movie = dict(item("movie"), name=FIXTURE["name"])
    source.files.update(("/dav/" + path, "/dav/" + movie["path"]))
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-source-names-")
    catalog(work)
    with open(os.path.join(work, "catalog.json")) as handle:
        document = json.load(handle)
    for section in document["sections"]:
        if section["type"] == "show":
            section["Location"] = [{"id": 2, "path": MOUNT + "/anime"}]
    with open(os.path.join(work, "catalog.json"), "w") as handle:
        json.dump(document, handle)
    server = Server(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": [pushed, movie]})
        keys = server.keys()
        episode_key, movie_key = keys[path], keys[movie["id"]]
        before = server.row(episode_key)
        show_key, season_key = before["grandparentRatingKey"], before["parentRatingKey"]
        check("the captured old provider name is the unmatched show's source title",
              server.row(show_key).get("title") == FIXTURE["name"])
        movie_before = server.row(movie_key)
        check("a measured Plex metadata title wins over the provider name",
              movie_before.get("title") == "Blame It on the Bellboy", movie_before.get("title"))
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % episode_key)
        corrected = dict(pushed, name=FIXTURE["corrected_name"])
        status, body = server.feed("POST", "items", {"items": [corrected, dict(movie, name=FIXTURE["corrected_name"])]})
        after = server.row(episode_key)
        check("normal same-ID POST corrects the unmatched source title on the same show key",
              status == 200 and server.row(show_key).get("title") == FIXTURE["corrected_name"],
              (body, server.row(show_key).get("title")))
        check("renaming preserves episode/season/show/library keys and watch state",
              server.keys() == keys and after.get("parentRatingKey") == season_key
              and after.get("grandparentRatingKey") == show_key and after.get("viewCount") == 1
              and after.get("librarySectionID") == before.get("librarySectionID"), after)
        check("the metadata title, guid and poster survive the source correction",
              all(server.row(movie_key).get(field) == movie_before.get(field)
                  for field in ("title", "guid", "thumb")))
        server.stop()
        # Model a title unavailable from an otherwise measured Plex response,
        # not a new authoritative response manufactured from the source name.
        partial = json.loads(json.dumps(CACHE["plex"]))
        for response in partial.values():
            for row in response.get("MediaContainer", {}).get("Metadata", []):
                if row.get("guid") == movie_before["guid"]:
                    row.pop("title", None)
        for answer in (partial, CACHE["plex"]):
            with open(os.path.join(work, "plex_cache.json"), "w") as handle:
                json.dump(answer, handle)
            cache = os.path.join(work, "plex_cache.db")
            if os.path.exists(cache):
                os.remove(cache)
            server = Server(work, bytes_port)
            if answer is partial:
                server.feed("POST", "items", {"items": [dict(movie, name="Source label is not an agent title")]})
                check("a same-guid agent answer without a title cannot replace a metadata-owned title",
                      server.row(movie_key).get("title") == movie_before["title"])
                server.stop()
        for source_name in (movie_before["title"], FIXTURE["corrected_name"]):
            server.feed("POST", "items", {"items": [dict(movie, name=source_name)]})
        check("a metadata title equal to the previous source name still belongs to metadata",
              server.row(movie_key).get("title") == movie_before["title"])
        # A modeled companion file checks that correcting the display name does
        # not leave the source's existing show behind on its previous identity.
        next_path = path + "-next.mkv"
        companion = dict(corrected, id=next_path, path=next_path, file=MOUNT + "/" + next_path)
        server.feed("POST", "items", {"items": [companion]})
        next_key = server.keys()[next_path]
        check("a later unplaced file stays under the same show and continues its file-order numbering",
              server.row(next_key).get("grandparentRatingKey") == show_key
              and server.row(next_key).get("index") == 2, server.row(next_key))
        server.request("/library/metadata/%s?title.value=Our%%20Unlocked%%20Show" % show_key, "PUT")
        server.feed("PUT", "snapshot", {"items": [dict(corrected, name="Snapshot source name"), companion, movie]})
        check("an unlocked manual title also survives an ordinary snapshot",
              server.row(show_key).get("title") == "Our Unlocked Show")
        server.request("/library/metadata/%s?title.value=Our%%20Show&title.locked=1" % show_key, "PUT")
        server.feed("POST", "items", {"items": [dict(corrected, name="The source's later name")]})
        check("a manually edited and locked show title is never replaced by its source",
              server.row(show_key).get("title") == "Our Show")
        server.request("/library/metadata/%s?title.value=Our%%20Movie" % movie_key, "PUT")
        server.feed("POST", "items", {"items": [dict(movie, name="Source after manual movie edit")]})
        check("an unlocked manual movie title wins over authoritative metadata on a name update",
              server.row(movie_key).get("title") == "Our Movie")
        server.stop()
        server = Server(work, bytes_port)
        server.feed("POST", "items", {"items": [dict(movie, name="Source after manual movie restart")]})
        check("the existing persisted edit record protects an unlocked title after restart",
              server.row(movie_key).get("title") == "Our Movie")
        # A modeled second version exercises the existing metadata-guid folding
        # owner; the legacy title belongs to the row, not to either Media.
        second_release = movie["release"] + "-second"
        second_path = second_release + "/" + movie["path"].rsplit("/", 1)[1]
        second = dict(movie, id=second_path, path=second_path, file=MOUNT + "/" + second_path,
                      release=second_release, name=movie["name"] + " second version")
        server.feed("POST", "items", {"items": [second]})
        check("a second version retains the movie row and both source identities",
              server.keys().get(second_path) == movie_key and len(server.row(movie_key).get("Media", [])) == 2)
        server.stop()
        # Model the reported old-cache title explicitly, not an agent response: a
        # previously described movie whose title stayed on the old provider name.
        with sqlite3.connect(os.path.join(work, "rex.db")) as db:
            raw = db.execute("SELECT body FROM row WHERE coll='state:items' AND key=?", (movie_key,)).fetchone()[0]
            stale = json.loads(raw)
            stale["title"] = FIXTURE["corrected_name"]
            # This leg models an old source-owned title, not the manual edit above.
            db.execute("DELETE FROM row WHERE coll='state:edits' AND key=?", (movie_key,))
            stale.pop("_titleGuid", None)
            # An explicitly modeled mismatched legacy GUID, not reporter state.
            stale["guid"] = OTHER_GUID
            db.execute("UPDATE row SET body=? WHERE coll='state:items' AND key=?", (json.dumps(stale), movie_key))
        server = Server(work, bytes_port)
        server.feed("POST", "items", {"items": [dict(movie, name="Source after manual movie restart")]})
        check("a different metadata GUID is not authority to rewrite an ambiguous legacy title",
              server.row(movie_key).get("title") == FIXTURE["corrected_name"]
              and server.row(movie_key).get("guid") == OTHER_GUID)
        server.stop()
        with sqlite3.connect(os.path.join(work, "rex.db")) as db:
            raw = db.execute("SELECT body FROM row WHERE coll='state:items' AND key=?", (movie_key,)).fetchone()[0]
            stale = json.loads(raw)
            stale["guid"] = movie_before["guid"]
            db.execute("UPDATE row SET body=? WHERE coll='state:items' AND key=?", (json.dumps(stale), movie_key))
        server = Server(work, bytes_port)
        server.feed("POST", "items", {"items": [dict(movie, name="Source after manual movie restart")]})
        check("a legacy named multiversion row refreshes from same pushed IDs without a name change",
              server.row(movie_key).get("title") == movie_before.get("title")
              and server.row(movie_key).get("guid") == movie_before.get("guid")
              and len(server.row(movie_key).get("Media", [])) == 2
              and server.keys().get(second_path) == movie_key, server.row(movie_key).get("title"))
        status, body = server.feed("POST", "items", {"items": [dict(movie, name="Source after manual movie restart")]})
        check("repeating the corrected push is idempotent", status == 200 and body.get("unchanged") == 1, body)
        server.feed("POST", "items", {"items": [dict(movie, name=movie_before["title"])]})
        server.stop()
        for filename in ("plex_cache.json", "plex_cache.db"):
            cache = os.path.join(work, filename)
            if os.path.exists(cache):
                os.remove(cache)
        server = Server(work, bytes_port)
        server.feed("POST", "items", {"items": [dict(movie, name="Offline provider correction")]})
        check("unavailable metadata never hands a previously named title back to its source",
              server.row(movie_key).get("title") == movie_before["title"])
        server.stop()
        with sqlite3.connect(os.path.join(work, "rex.db")) as db:
            raw = db.execute("SELECT body FROM row WHERE coll='state:items' AND key=?", (movie_key,)).fetchone()[0]
            matched = json.loads(raw)
            matched["title"] = "Offline provider correction"
            matched["_matched"] = True  # the persisted marker of an earlier Fix Match
            db.execute("UPDATE row SET body=? WHERE coll='state:items' AND key=?", (json.dumps(matched), movie_key))
        server = Server(work, bytes_port)
        server.feed("POST", "items", {"items": [dict(movie, name="The source's newest spelling")]})
        check("a prior Fix Match keeps its title even when it equals the previous source name",
              server.row(movie_key).get("title") == "Offline provider correction"
              and server.row(movie_key).get("guid") == movie_before.get("guid"))
        server.stop()
        # A second cache-free server models a captured, source-placed movie while
        # Plex's agent is unavailable; its ID is still the pushed ID.
        fallback_work = tempfile.mkdtemp(prefix="rex-source-fallback-")
        try:
            catalog(fallback_work)
            for filename in ("plex_cache.json", "plex_cache.db"):
                cache = os.path.join(fallback_work, filename)
                if os.path.exists(cache):
                    os.remove(cache)
            server = Server(fallback_work, bytes_port)
            server.feed("POST", "items", {"items": [movie]})
            fallback_key = server.keys()[movie["id"]]
            fallback_before = server.row(fallback_key)
            check("an unavailable agent leaves the placed movie on its pushed fallback title",
                  fallback_before.get("title") == FIXTURE["name"]
                  and fallback_before.get("guid", "").startswith("local://"))
            server.feed("POST", "items", {"items": [dict(movie, name=FIXTURE["corrected_name"])]})
            check("a source-owned placed fallback follows the correction without losing its key or IDs",
                  server.row(fallback_key).get("title") == FIXTURE["corrected_name"]
                  and server.row(fallback_key).get("guid") == fallback_before.get("guid")
                  and server.row(fallback_key).get("Guid") == fallback_before.get("Guid")
                  and server.keys() == {movie["id"]: fallback_key})
            status, body = server.feed("POST", "items", {"items": [dict(movie, name=FIXTURE["corrected_name"])]})
            check("repeating a placed fallback correction is idempotent",
                  status == 200 and body.get("unchanged") == 1, body)
        finally:
            server.stop()
            shutil.rmtree(fallback_work, ignore_errors=True)
        partial_work = tempfile.mkdtemp(prefix="rex-partial-title-")
        try:
            catalog(partial_work)
            with open(os.path.join(partial_work, "plex_cache.json"), "w") as handle:
                json.dump(partial, handle)
            server = Server(partial_work, bytes_port)
            server.feed("POST", "items", {"items": [movie]})
            partial_key = server.keys()[movie["id"]]
            check("a new same-guid response without a title records a source-owned fallback",
                  server.row(partial_key).get("title") == FIXTURE["name"]
                  and server.row(partial_key).get("guid") == movie_before["guid"])
            server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % partial_key)
            server.feed("POST", "items", {"items": [dict(movie, name=FIXTURE["corrected_name"])]})
            check("a known source-owned fallback follows its source even when Plex supplied a guid",
                  server.row(partial_key).get("title") == FIXTURE["corrected_name"]
                  and server.row(partial_key).get("guid") == movie_before["guid"]
                  and server.row(partial_key).get("viewCount") == 1)
            server.stop()
            with open(os.path.join(partial_work, "plex_cache.json"), "w") as handle:
                json.dump(CACHE["plex"], handle)
            os.remove(os.path.join(partial_work, "plex_cache.db"))
            server = Server(partial_work, bytes_port)
            server.feed("POST", "items", {"items": [dict(movie, name=FIXTURE["corrected_name"])]})
            check("an actual title arriving later replaces only its source fallback on the same key and guid",
                  server.row(partial_key).get("title") == movie_before["title"]
                  and server.row(partial_key).get("guid") == movie_before["guid"]
                  and server.keys() == {movie["id"]: partial_key}
                  and server.row(partial_key).get("viewCount") == 1)
        finally:
            server.stop()
            shutil.rmtree(partial_work, ignore_errors=True)
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("%d/%d source-name checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
