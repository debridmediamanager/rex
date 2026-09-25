"""Build `activity_strings.json` from the reference's own gettext catalogues.

Cycle 65 read the activity titles out of zen's (1.42.2) `plex.mo` catalogues by hand;
this is that read as a tool. The catalogues are the PMS image's
`Resources/locale/<file>/LC_MESSAGES/plex.mo`, copied off zen with a GET-only `cat`, and
are NOT kept in the repository.

  strings   the activity titles and subtitles plexsim pushes (`ActivityNotification`).
  progress  the butler's sentences (`ProgressNotification`). Cycle 66, measured on
            plexscratch42: they follow the socket's language as the titles do; a scan's
            progress and status lines are in no catalogue and stay English.

`LANGUAGE_FILES` is the language -> catalogue file map, fitted in cycle 65 to
plexscratch42's live frames in 12 languages (ca reads `es`, nb `no`, the regional files
for cs/et/hr/ja/zh) and widened in cycle 66 to `af`, `no` and `pt-BR`, which Plex Web
sends and which have catalogues of their own. Cycle 67: `hu-HU`, which a Hungarian browser's
socket handshake carries, reads `hu_HU` (eight ids worded apart from `hu`, on plexscratch42).

    python3 build_activity_strings.py MO_DIR OUT
"""
import gettext
import json
import os
import sys

LANGUAGE_FILES = {
    "af": "af", "ar": "ar", "ca": "es", "cs": "cs_CZ", "da": "da", "de": "de", "es": "es",
    "et": "et_EE", "fi": "fi", "fr": "fr", "he": "he", "hr": "hr_HR", "hu": "hu", "hu-HU": "hu_HU", "it": "it",
    "ja": "ja_JP", "ko": "ko", "lt": "lt", "nb": "no", "nl": "nl", "no": "no", "pl": "pl",
    "pt": "pt", "pt-BR": "pt_BR", "ro": "ro", "ru": "ru", "sk": "sk", "sl": "sl", "sr": "sr",
    "sv": "sv", "uk": "uk", "zh": "zh_CN",
}

STRINGS = [
    "Analyzing media bundles", "Analyzing metadata bundles", "Backing up database",
    "Butler tasks", "Cleaning Bundles", "Cleaning bundles", "Database", "Deleting Library",
    "Deleting section {1}", "Deleting {1} media bundles", "Deleting {1} metadata bundles",
    "Generating video chapter thumbnails", "Optimizing Database", "Optimizing database",
    "Processing subscriptions", "Scanning", "Scanning {1}",
]

# Every `The butler ...` message id, taken from the catalogues rather than listed:
# the sentence plexsim emits is looked up by shape at send time, so an id it never emits
# costs nothing and one it starts emitting is already here.

mo_dir, out = sys.argv[1], sys.argv[2]
catalogues = {}
for language, name in LANGUAGE_FILES.items():
    path = os.path.join(mo_dir, name + ".mo")
    with open(path, "rb") as handle:
        catalogues[language] = gettext.GNUTranslations(handle)._catalog
# The union over every catalogue: `en.mo` itself lacks three of the 22 ids `de.mo` holds.
progress_ids = sorted({key for catalogue in catalogues.values() for key in catalogue
                       if isinstance(key, str) and key.startswith("The butler ")})


def table(ids):
    result = {}
    for msgid in ids:
        row = {language: catalogue[msgid] for language, catalogue in sorted(catalogues.items())
               if isinstance(catalogue.get(msgid), str) and catalogue[msgid]
               and catalogue[msgid] != msgid}
        if row:
            result[msgid] = row
    return result


payload = {
    "_provenance": (
        "Cycle 65. The activity titles and subtitles a real PMS pushes on the notification "
        "sockets and answers on /activities, per X-Plex-Language, read out of zen's (1.42.2) "
        "own gettext catalogues (Resources/locale/*/LC_MESSAGES/plex.mo) for the message ids "
        "plexsim emits; {1} is the inserted name or count. The language-to-catalogue map (ca "
        "reads es, nb reads no, cs/et/hr/ja/zh their regional files) was fitted to "
        "plexscratch42's live frames in 12 languages, which the table reproduces exactly. A "
        "message id the catalogue lacks (Analyzing Media, Updating Metadata) stays English on "
        "a real PMS too. Cycle 66: `progress` holds the butler's sentences, which follow the "
        "socket's language the same way (plexscratch42, ten languages), and af, no and pt-BR "
        "read their own catalogues. Built by handover/tools/build_activity_strings.py."),
    "strings": table(STRINGS),
    "progress": table(progress_ids),
}
open(out, "w").write(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
print(len(payload["strings"]), "strings,", len(payload["progress"]), "progress ids,",
      len(catalogues), "languages")
