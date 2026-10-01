"""MediaContainer -> Plex XML.

Plex serves the same container as JSON or XML depending on the client, and the
mapping is not a generic json2xml: scalars are ATTRIBUTES, lists of dicts are
CHILD ELEMENTS, and the element name for a `Metadata` row is chosen from the
row's own `type` (`<Video>` for movie/episode/clip, `<Directory>` for
show/season, `<Track>` for a track).  pd_inject sidestepped all of this by
hardcoding one giant XML string per response; that does not survive a second
endpoint, so this is the real mapping.

Booleans go on the wire as 1/0 and `None` is dropped, both because that is what
a real PMS emits and because plexsim's own clients gate on attribute presence.
"""


# A `Metadata` row's element name comes from its `type`.
_METADATA_ELEMENT = {
    "movie": "Video",
    "episode": "Video",
    "clip": "Video",
    "show": "Directory",
    "season": "Directory",
    "artist": "Directory",
    "album": "Directory",
    "collection": "Directory",
    "playlist": "Playlist",
    "track": "Track",
    # Cycle 45: a `type: "image"` row -- what `/library/arts` answers -- is a `<Photo>`,
    # measured on kids. Without it the row fell through to the `Video` default, which is
    # the trap this project already records for three other endpoints: `xmlout` names a
    # `Metadata` row's element from the row's `type`, and XML is the DEFAULT format.
    "image": "Photo",
}


# The order a row's child elements come in, measured on both servers across movie,
# show and episode reads with every include option: media first, then artwork, then
# the tags, then the blocks an option adds. plexsim wrote them in whatever order the
# row dict held them -- Genre before Image, Guid after the people, Preferences after
# Extras -- 41 pairs inverted. A child not listed keeps its place after these.
# Cycle 19: a LIGHT row -- a section grid's, a hub shelf's, a search hit's -- puts
# `Guid` directly after `UltraBlurColors`, before the tags, on both references. The
# builder marks the projection with a private `_light` key because nothing in the row
# itself distinguishes the two.
# Cycle 49 dropped `Label`: no reference light row carries one. See `GRID_TAGS`.
_LIGHT_CHILD_ORDER = {name: index for index, name in enumerate((
    "Media", "Image", "UltraBlurColors", "Guid", "Genre", "Country", "Director",
    "Writer", "Role", "Collection", "Location"))}

# Cycle 45: `includeAlternates=1` inlines `Thumb` and `Art` between the tags and
# `Guid`, and `Theme` directly after `Rating` -- kids' show 6 answers `Genre, Country,
# Thumb, Art, Guid, Rating, Theme, Role` and zen's movie 380 `Genre, Country, Thumb,
# Art, Guid, Rating, Director`. Without the entries they would fall to the END, which
# is this table's documented failure mode for a pair no reference emitted.
# Cycle 49 moved `Style`: it sits AFTER `Country` and before `Thumb`, not between
# `UltraBlurColors` and `Genre`. Measured on plexscratch42 (1.42.2) and plexscratch
# (1.43.4) on a movie MADE to carry the whole tag family at once -- `Genre Country Style
# Collection Director Producer Label Similar Field` -- which is a row shape no reference
# library holds, so cycle 23's 2,994-path sweep could not have seen it. `Mood` is
# deliberately absent from this table still: a real PMS refuses a `mood` write on a movie
# and stores nothing, so its slot is unmeasured rather than guessed.
_CHILD_ORDER = {name: index for index, name in enumerate((
    "Media", "Image", "UltraBlurColors", "Genre", "Country", "Style",
    "Thumb", "Art", "Guid", "Rating", "Theme",
    "Collection", "Director", "Writer", "Role", "Producer", "Label", "Similar",
    "CommonSenseMedia", "Chapter", "Marker", "Review", "Field", "Location", "Preferences",
    "OnDeck", "Extras", "Related", "Children"))}

# Cycle 16, measured on kids and zen: inside a `Meta/Type` the order is
# `Filter, Sort, Field` -- which is NOT the item order above, where `Field` comes
# before `Location`. plexsim sorted these three by the item table, which knows only
# `Field`, so they came out in whatever order the dict held: `Field, Filter, Sort`.
_TYPE_CHILD_ORDER = {name: index for index, name in enumerate(
    ("Filter", "Sort", "Field"))}

# Cycle 19, measured on kids (1.43.4) and zen (1.42.2): inside a `Meta` the two `Type`
# rows come BEFORE the eight `FieldType` rows -- `Type, Type, FieldType x8` on a movie
# library's `?includeMeta=1`. plexsim sorted them by the item table, which knows
# neither name, so the stable sort left the dict's own order and every Meta came out
# `FieldType x8, Type, Type`. It gets the same pair right where they sit directly on
# the container (`?includeDetails=1` is `Directory x20, Type, FieldType x8` on all
# three servers); only the `Meta` wrapper was unordered.
_META_CHILD_ORDER = {name: index for index, name in enumerate(("Type", "FieldType"))}


# Cycle 17, measured on kids (1.43.4) and zen (1.42.2), which agree byte for byte:
# a real PMS escapes an XML attribute value into pure ASCII even though the document
# declares UTF-8. `&`, `<` and `>` take the named entities; `"` and `'` take NUMERIC
# ones (`&#34;`, `&#39;`) rather than `&quot;`/`&apos;`; TAB is decimal `&#9;` while
# NEWLINE and CR are HEX (`&#xA;`, `&#xD;`); every codepoint from U+0080 up becomes its
# DECIMAL reference, astral plane included (an emoji is `&#128512;`, one reference and
# not a surrogate pair); and every control character XML 1.0 forbids is replaced with a
# literal `?`. plexsim used `xml.sax.saxutils.quoteattr`, which writes `&quot;`, leaves
# `'` alone, sends the newline as decimal and passes every non-ASCII character through
# raw -- a different body from a real PMS on every response carrying an accent or an
# apostrophe, which on a real library is most of them.
_NAMED = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&#34;", "'": "&#39;",
          "\t": "&#9;", "\n": "&#xA;", "\r": "&#xD;"}
_FORBIDDEN = frozenset(
    [chr(code) for code in list(range(0x00, 0x09)) + [0x0b, 0x0c]
     + list(range(0x0e, 0x20)) + [0x7f]])


def _escape(text):
    out = []
    for character in text:
        named = _NAMED.get(character)
        if named is not None:
            out.append(named)
        elif character in _FORBIDDEN:
            out.append("?")
        elif ord(character) >= 0x80:
            out.append("&#%d;" % ord(character))
        else:
            out.append(character)
    return '"%s"' % "".join(out)


# Cycle 17, derived from kids (1.43.4) and zen (1.42.2) and verified against them: the
# order a ROW's attributes come out in. Cycle 16 settled the CONTAINER's (`size`, then
# `totalSize`/`offset` when paged, then alphabetical) and left a row's explicitly
# unmapped -- it is not alphabetical, and a tag comes out `id, filter, tag`. Each order
# below is a topological sort of every attribute PAIR the two references were seen to
# emit, over 25 element buckets and every row type, and each was checked to be a
# superset of every observed sequence. Only witnessed pairs constrain it; attributes
# never seen together fall where they fall, which is why an attribute missing from a
# list keeps its place after the ones that are in it rather than being moved.
#
# A row that names its own element (`Video`, `Directory`, `Track`, `Playlist`) is keyed
# by element plus the row's `type`, except a `/library/sections` row -- the one
# Directory that carries `agent` -- which has an order of its own.
_ATTR_ORDER = {
    # Cycle 19, measured on kids (1.43.4) and zen (1.42.2), which agree on all five:
    # four element buckets `_ATTR_ORDER` had no entry for at all, plus the split
    # `Directory[PROVIDER]` above. Without an entry the attributes come out in
    # whatever order the dict that built the row held them.
    "Common": (
        # `/library/sections/{id}/common`, a single-row form on both servers. zen's
        # sequence is a superset of kids'.
        # Cycle 23, measured on kids and zen: the GRANDPARENT pair LEADS the element
        # and the PARENT pair comes before `ratingKey`. plexsim appended all four
        # after the scalars, because this bucket knew none of them.
        "grandparentRatingKey", "grandparentTitle",
        # Cycle 25: `userRating` follows `mixedFields` directly, ahead of `guid` --
        # measured on plexscratch, the only instrument that can show the field at all.
        "mixedFields", "userRating", "parentRatingKey", "parentTitle",
        "ratingKey", "key", "guid", "studio", "type", "title",
        "titleSort", "contentRating", "summary", "index", "year", "tagline",
        "originallyAvailableAt"
    ),
    "Feature": (
        # `/media/providers`. Only the scrobble feature carries the first two, and
        # both references put them FIRST; plexsim appended them.
        # Cycle 22: `flavor` leads a `Feature` on both references; plexsim
        # appended it, the same topological-sort gap as `Part` above.
        "flavor",
        "scrobbleKey", "unscrobbleKey", "key", "type"
    ),
    "Type": (
        # A `Meta/Type` row. plexsim sorted it alphabetically -- `active, key, title,
        # type` -- where both references answer `key, type, title, active`.
        "key", "type", "title", "active", "activeDirection"
    ),
    "Directory[PROVIDER]": (
        "agent", "language", "refreshing", "scanner", "uuid", "id", "key", "hubKey",
        "type", "title", "updatedAt", "scannedAt"
    ),
    "AgeRating": (
        "type", "rating", "age"
    ),
    "CommonSenseMedia": (
        "id", "oneLiner"
    ),
    "Country": (
        "id", "filter", "tag"
    ),
    # Cycle 45: an `includeAlternates=1` artwork row. Measured on kids and zen --
    # `key`, then `previewKey` where the provider gives one, then `source`.
    "Thumb": (
        "key", "previewKey", "source"
    ),
    "Art": (
        "key", "previewKey", "source"
    ),
    "Theme": (
        "key", "previewKey", "source"
    ),
    # Cycle 41 measured these four writable tag children on disposable copies of
    # both supported PMS versions. Like Genre/Country they are exactly
    # `id, filter, tag` in JSON and XML; without their own buckets plexsim kept the
    # catalog dict's `id, tag, filter` construction order.
    "Collection": (
        "id", "filter", "tag"
    ),
    "Label": (
        "id", "filter", "tag"
    ),
    "Similar": (
        "id", "filter", "tag"
    ),
    "Style": (
        "id", "filter", "tag"
    ),
    "Director": (
        "id", "filter", "tag", "tagKey", "thumb"
    ),
    "Directory[SECTION]": (
        # Cycle 45: the `/library/shared` projection of the same row leads with `art`
        # and puts `thumb` directly after `key`, and carries none of the first three.
        # One bucket serves both forms, because a name a row does not carry is skipped.
        "art",
        "allowSync", "filters", "refreshing", "key", "thumb", "type", "title", "agent",
        "scanner", "language", "metadataAgentProviderGroupId", "uuid",
        "updatedAt", "createdAt", "scannedAt", "content", "directory",
        "contentChangedAt", "hidden"
    ),
    "Directory[filter]": (
        "filter", "filterType", "key", "title", "type"
    ),
    "Directory[genre]": (
        "fastKey", "key", "title", "type"
    ),
    "Directory[none]": (
        # Cycle 23, measured on kids and zen: a `/library/tags` and
        # `/library/people/{id}` row is `id, filter, tag, tagType, tagKey, thumb` --
        # `thumb` LAST. This bucket knew `thumb` and none of the other five, so they
        # all fell behind it and the tag row came out led by its image.
        "id", "filter", "tag", "tagType", "tagKey",
        # Cycle 22: a section's PIVOT rows lead with these on both references --
        # `<Directory secondary="1" key="genre" title="By Genre" />` and
        # `<Directory prompt="Search ..." search="1" key="search?type=1" ... />`.
        "secondary", "prompt", "search",
        "default", "defaultDirection", "descKey", "firstCharacterKey", "size",
        "fastKey", "leafCount", "thumb", "viewedLeafCount", "count", "hubKey",
        "key", "title"
    ),
    "Directory[season]": (
        "ratingKey", "key", "parentRatingKey", "guid", "parentGuid", "parentSlug",
        "parentStudio", "type", "title", "titleSort", "parentKey",
        "librarySectionTitle", "librarySectionID", "librarySectionKey",
        # Cycle 21: `parentSummary` comes directly after `parentTitle`, before
        # `summary` -- measured on the season rows of `/library/recentlyAdded`, the one
        # list that carries it, on both references. plexsim appended it.
        "parentTitle", "parentSummary", "summary", "index", "parentIndex",
        "viewCount", "skipCount", "viewOffset", "lastViewedAt", "includedAt",
        "userRating",
        "lastRatedAt",
        "year", "parentYear",
        "thumb", "art", "parentThumb", "parentTheme", "leafCount",
        "viewedLeafCount", "addedAt", "updatedAt", "deletedAt"
    ),
    "Directory[show]": (
        "ratingKey", "key", "guid", "slug", "studio", "type", "title",
        "librarySectionTitle", "librarySectionID", "librarySectionKey",
        "titleSort", "originalTitle", "contentRating", "contentRatingAge",
        "summary", "index", "audienceRating", "userRating",
        # Cycle 21, measured on zen's watched show 19756: `viewCount` and `skipCount`
        # sit between the rating and `lastViewedAt`, and the whole block precedes
        # `year`.
        "viewCount", "skipCount", "viewOffset", "lastViewedAt", "includedAt",
        "lastRatedAt", "year",
        "tagline", "thumb", "art", "theme", "duration", "originallyAvailableAt",
        "leafCount", "viewedLeafCount", "childCount",
        # Cycle 79: `seasonCount` -- a show with a season 0 -- follows `childCount` on zen.
        "seasonCount", "addedAt", "updatedAt",
        "deletedAt", "audienceRatingImage", "episodeSort", "flattenSeasons"
    ),
    "Field": (
        "locked", "name"
    ),
    "Agent": (
        # Cycle 27, measured on kids and zen: a MODERN agent row leads `identifier,
        # name`, where plexsim emitted its builder's dict order. Cycle 19 parked "the
        # Agent element's attribute order" on the LEGACY pairs (`hasPrefs < identifier
        # < primary`), which the rows plexsim actually emits do not carry.
        "identifier", "name", "hasPrefs", "id", "primary"
    ),
    "Agent[legacy]": (
        # Cycle 87, measured on zen (1.42.2) in JSON and XML over the bare path and every
        # `mediaType=`: a LEGACY plug-in row (the one that carries `primary`) is
        # `hasPrefs, identifier, primary, name` -- the order cycle 19 parked -- and not
        # the modern row's `identifier, name, hasPrefs`. Kids (1.43.4) agrees after the
        # `id` it alone leads with. One bucket for both put `identifier` first on all
        # eleven legacy rows.
        "id", "hasPrefs", "identifier", "primary", "name"
    ),
    "Video[history1]": (
        # Cycle 27, measured on kids (1.43.4) and zen (1.42.2): a SINGLE history read
        # answers its nine (an episode's twelve) attributes in this order, which is
        # neither the listing row's nor a movie row's.
        "historyKey", "librarySectionID", "title", "grandparentTitle", "type",
        "index", "parentIndex", "originallyAvailableAt", "viewedAt", "accountID",
        "deviceID", "viewType"
    ),
    "Genre": (
        "id", "filter", "tag"
    ),
    "Hub": (
        # Cycle 23: the `/hubs/sections/{id}/manage` rows lead with `identifier`, not
        # `title`, on both references.
        # Cycle 27: `random` comes BEFORE `style` on both -- measured on the Rediscover
        # and Start-Watching shelves of `/hubs/sections/{id}`, which are the only rows
        # that carry it. It was absent from this bucket, so it fell to the end.
        # Cycle 70: a post-play `tv.upnext` hub carries `upNext` straight after `hubKey`
        # (plexscratch42 and plexscratch).
        "identifier",
        "hubKey", "upNext", "key", "title", "type", "hubIdentifier", "context", "size",
        "more", "random", "style", "promoted"
    ),
    "Sort": (
        # Cycle 27, measured on kids and zen over five `includeMeta=1` paths: a `Sort`
        # row leads with the ACTIVE pair, not with `default`. plexsim had no `Sort`
        # bucket, so the two flags it adds last came out last.
        "active", "activeDirection", "default", "defaultDirection", "descKey",
        "firstCharacterKey", "key", "title"
    ),
    "Image": (
        "alt", "type", "url"
    ),
    "Location": (
        "id", "path"
    ),
    "Media": (
        "id", "duration", "bitrate", "width", "height", "aspectRatio",
        "audioChannels", "audioCodec", "videoCodec", "videoResolution",
        "container", "videoFrameRate", "optimizedForStreaming", "audioProfile",
        "has64bitOffsets", "videoProfile", "deletedAt", "hasVoiceActivity"
    ),
    "Part": (
        # Cycle 22, harvested from 633 zen paths and 133 kids paths: the two
        # `checkFiles=1` flags LEAD the element on both references. `_ATTR_ORDER` is a
        # topological sort of observed PAIRS, and no reference had ever been seen to
        # emit them beside their neighbours, so they fell to the END.
        "accessible", "exists",
        "id", "key", "duration", "file", "size", "audioProfile", "container",
        "failureBIFResultCode", "failureBIFRetryCount", "has64bitOffsets",
        "hasThumbnail", "indexes", "optimizedForStreaming", "packetLength",
        "videoProfile"
    ),
    "Producer": (
        "id", "filter", "tag", "tagKey", "thumb"
    ),
    "Rating": (
        "image", "value", "type"
    ),
    "Role": (
        "id", "filter", "tag", "tagKey", "role", "thumb"
    ),
    # Cycle 25, measured on zen's 73 episode rows and kids' five movie rows: a history
    # row's order. The movie form is a subsequence of this, so one bucket answers both.
    "Video[history]": (
        "historyKey", "key", "ratingKey", "librarySectionID", "parentKey",
        "grandparentKey", "title", "grandparentTitle", "type", "thumb", "parentThumb",
        "grandparentThumb", "grandparentArt", "index", "parentIndex",
        "originallyAvailableAt", "viewedAt", "accountID", "deviceID", "viewType"
    ),
    "Stream": (
        "id", "streamType", "canAutoSync", "selected", "default", "codec",
        "index", "channels", "bitrate", "language", "languageTag", "languageCode",
        # Cycle 25, measured on zen's own XML for two rows carrying them: `anamorphic`
        # sits between `bitrate` and `bitDepth` and `pixelAspectRatio` between `level`
        # and `profile`. Without an entry a name falls to the END of the bucket, which
        # is this table's documented failure mode.
        "anamorphic",
        "bitDepth", "chromaLocation", "chromaSubsampling", "codedHeight",
        "codedWidth", "audioChannelLayout", "colorPrimaries", "colorRange",
        "colorSpace", "colorTrc", "frameRate", "hasScalingMatrix", "height",
        "level", "pixelAspectRatio", "profile", "refFrames", "scanType", "samplingRate",
        "streamIdentifier", "hearingImpaired", "title", "width", "displayTitle",
        "extendedDisplayTitle"
    ),
    "UltraBlurColors": (
        "topLeft", "topRight", "bottomRight", "bottomLeft"
    ),
    "Video[episode]": (
        "ratingKey", "key", "parentRatingKey", "grandparentRatingKey", "guid",
        "parentGuid", "grandparentGuid", "grandparentSlug", "studio", "type",
        "title", "titleSort", "grandparentKey", "parentKey", "librarySectionTitle",
        "librarySectionID", "librarySectionKey", "grandparentTitle",
        "parentTitle", "contentRating", "summary", "index", "parentIndex",
        "audienceRating",
        # Cycle 21, measured on zen's watched episode 19764: the per-user-state block
        # sits HERE, after the rating and before the year -- where a movie row has
        # carried it since cycle 17. Without an entry these names fell out in dict
        # insertion order, which appended them after `updatedAt`, and cycle 19's "a
        # real PMS's JSON key order is its XML order" moved both formats with them.
        "viewCount", "skipCount", "viewOffset", "lastViewedAt", "includedAt",
        "userRating",
        "lastRatedAt",
        "year", "thumb", "art", "parentThumb",
        "grandparentThumb", "grandparentArt", "grandparentTheme", "duration",
        "originallyAvailableAt", "addedAt", "updatedAt", "audienceRatingImage",
        "deletedAt", "chapterSource"
    ),
    "Video[clip]": (
        # Cycle 29, identical across 3,905 extras on kids and zen. extraType is
        # deliberately last, after every optional timestamp and duration field.
        "ratingKey", "key", "guid", "type", "title", "titleSort", "summary",
        "index", "year", "thumb", "primaryGuid", "art", "subtype", "duration",
        "originallyAvailableAt", "addedAt", "updatedAt", "extraType"
    ),
    "Video[movie]": (
        "ratingKey", "key", "guid", "slug", "studio", "type", "title",
        "titleSort", "librarySectionTitle", "librarySectionID",
        "librarySectionKey", "originalTitle", "isAdult", "contentRating",
        "contentRatingAge",
        "summary", "rating", "audienceRating", "userRating", "viewCount",
        "lastViewedAt", "includedAt", "lastRatedAt", "year", "tagline", "thumb", "art",
        "duration", "originallyAvailableAt", "addedAt", "updatedAt", "deletedAt",
        "audienceRatingImage", "chapterSource", "primaryExtraKey",
        "useOriginalTitle", "ratingImage"
    ),
    "Writer": (
        "id", "filter", "tag", "tagKey", "thumb"
    ),
}
_ATTR_INDEX = {bucket: {name: index for index, name in enumerate(names)}
               for bucket, names in _ATTR_ORDER.items()}


# Cycle 19, measured on kids (1.43.4) and zen (1.42.2): the rows of
# `/library/recentlyAdded` and `/library/onDeck` -- and only those -- LEAD with
# `allowSync` and the librarySection triple, before `ratingKey`. A section grid carries
# none of the four on the row, and `/library/all` and `/hubs/home/recentlyAdded` carry
# the triple in the middle where every other row does. `allowSync` on the ROW is the
# tell: it appears there and nowhere else, so it is the signal rather than the path.
# Cycle 23: a `/search` row carries `personal` and `sourceTitle` inside the same lead,
# directly after `librarySectionUUID` and before `ratingKey`, on kids and zen alike.
_LIBRARY_SECTION_LEAD = {"allowSync": -6, "librarySectionID": -5,
                         "librarySectionTitle": -4, "librarySectionUUID": -3,
                         "personal": -2, "sourceTitle": -1}

# Cycle 25, measured on kids and zen: a `/hubs/search` and a `/library/search` hit LEAD
# with `librarySectionTitle` before `ratingKey`, and a `/hubs/search` hit puts its own
# `score` second. HANDOVER 3.3 carried this as single-witness because the driver could
# only reproduce it on zen -- kids' hubs answered no `Video` for the queries that cycle
# tried. `query=Sintel` hits on kids, and both references then agree exactly. plexsim
# carried both fields and put `librarySectionTitle` after `title` and `score` LAST.
#
# It needs a marker rather than a signal read off the row: `/library/all` and
# `/hubs/home/recentlyAdded` rows carry `librarySectionTitle` too and keep it in the
# middle, so there is nothing in a search hit itself that tells the two apart. That is
# the same proxy `_light` already is, and `_drop_private` takes it off both writers.
_SEARCH_LEAD = {"librarySectionTitle": -2, "score": -1}


# Cycle 26, measured on the copy against the same row's own detail read, in BOTH
# formats: every element of a `/status/sessions` row -- the `Video` itself, its `Media`,
# its `Part` -- writes its attributes in plain ALPHABETICAL order, where every other
# container on the surface follows cycle 22's measured `_ATTR_ORDER`. The flags a
# decision arms (`selected`, `decision`) come AFTER that block, which is why they are a
# tail rather than part of the sort.
#
# Cycle 22 swept 2,994 paths for attribute order and this one is not among them: reading
# it needs a live playback session, which a read-only reference cannot be given. It is
# the one path known to contradict that table, so it gets a marker rather than an entry.
_SESSION_TAIL = {"selected": 1, "decision": 2}


def _attr_bucket(name, row):
    """The `_ATTR_ORDER` key for one element."""
    if name == "Video" and row.get("_history1"):
        # Cycle 27: the SINGLE history read is a third projection, with neither the
        # listing row's order nor an item row's. Measured on kids and zen against every
        # readable row on each.
        return "Video[history1]"
    if name == "Video" and row.get("_history"):
        # Cycle 25: a `/status/sessions/history/all` row is its own projection with its
        # own order, measured on zen and plexscratch -- `historyKey` first, `title`
        # between the parent keys and the type, `originallyAvailableAt` between the
        # indexes and `viewedAt`. Nothing in the row distinguishes it from an ordinary
        # `Video`, so the builder marks it, as `_light` already does; `_drop_private`
        # takes the marker off both writers.
        return "Video[history]"
    if name == "Agent" and row.get("primary") is not None:
        # Cycle 87: a legacy plug-in agent row; the modern rows carry no `primary`.
        return "Agent[legacy]"
    if name in ("Video", "Directory", "Track", "Playlist"):
        if row.get("agent") is not None or row.get("scanner") is not None:
            # Cycle 19: the library `Directory` of `/media/providers` is a DIFFERENT
            # order from `/library/sections`'s, and plexsim reused one bucket for
            # both. Measured identically on kids and zen: the provider form is
            # `agent, language, refreshing, scanner, uuid, id, key, hubKey, type,
            # title, updatedAt, scannedAt` where the section form leads `allowSync,
            # filters, refreshing, key, type, title, agent, ...`. `hubKey` is what
            # tells them apart -- the section rows carry none.
            return "Directory[PROVIDER]" if row.get("hubKey") is not None \
                else "Directory[SECTION]"
        return "%s[%s]" % (name, row.get("type") or "none")
    return name


def _attr(value):
    """Render one scalar the way PMS does: bools as 1/0, everything else str."""
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value)


def _format(key, value, row):
    """One attribute, where the few number formats that are per FIELD apply.

    Measured: a real PMS writes most floats the way Python does (`audienceRating`
    is `"5.0"`, never `"5"`), but a stream's `frameRate` always carries three
    decimals (`"24.000"`, `"29.970"`), and a `double` Setting's whole value drops
    its fraction (`requestLimitsScale` is `value="1"`). plexsim wrote `"24.0"` and
    `"1.0"`.
    """
    if isinstance(value, float) and not isinstance(value, bool):
        if key == "frameRate":
            return "%.3f" % value
        # Cycle 28, measured on kids and zen: a `/library/search` `SearchResult` prints
        # its score with FIVE decimals in XML (`score="0.52000"`, `score="0.93095"`) and
        # sends the same value as a plain number in JSON. It is the one place on the
        # surface where the score is a float rather than the string `/hubs/search`
        # carries, so it is the only one this rule can reach -- and plexsim wrote
        # Python's own `"0.32"` there, a string no real PMS sends.
        if key == "score":
            return "%.5f" % value
        if (key in ("value", "default") and row.get("type") == "double"
                and value == int(value)):
            return str(int(value))
    return _attr(value)


def _element_name(key, row, overrides=None):
    """The element a container key renders as.

    A `Metadata` row usually names its own element through `type`, but some
    endpoints carry rows with no type at all and a fixed element that only the
    endpoint knows: an artwork list is `<Photo>` and a folder list is
    `<Directory>`, both measured against a real PMS. Those pass an override,
    because guessing from the row would make both `<Video>`.
    """
    # Cycle 41: a mixed folder level carries Directory and Video rows under the
    # same `Metadata` key. A container-wide override cannot name both, while a
    # public `type` would leak into JSON. The private marker selects only that
    # row's XML element and is stripped with the other private projection state.
    if row.get("_element"):
        return row["_element"]
    if overrides and key in overrides:
        return overrides[key]
    if key == "Metadata":
        return _METADATA_ELEMENT.get(str(row.get("type", "")).lower(), "Video")
    return key


# The roots on the surface that are not a `MediaContainer` but order their attributes
# the way a container does, both measured: `/butler` answers `ButlerTasks` and
# `/library/sections/{id}/query/parse` a `Query`. `MyPlex`, the third non-container root,
# is NOT one of them -- see `to_xml`.
_ROOT_NAMES = {"ButlerTasks", "Query"}


def _split(name, row, container=False):
    """One element's ATTRIBUTE keys and CHILD keys, each in the order a real PMS
    emits them.

    Cycle 19: split out of `_render` because the JSON writer needs exactly the same
    answer -- a real PMS's JSON key order IS its XML order, measured on kids (1.43.4)
    and zen (1.42.2) for the container, the row, `Media`, `Part` and the row's child
    element keys, on all four row types.
    """
    attrs = []
    children = []

    for key, value in row.items():
        if value is None or str(key).startswith("_"):
            continue
        if isinstance(value, dict):
            children.append(key)
        elif isinstance(value, (list, tuple)):
            # A list of dicts is a child element set; a list of scalars has no
            # representation in Plex XML, so it is joined the way tag lists are.
            if value and all(isinstance(v, dict) for v in value):
                children.append(key)
            elif value:
                attrs.append(key)
        else:
            attrs.append(key)

    if container:
        # Measured: a listing's `Meta` comes BEFORE its rows; the rest keep their order.
        children.sort(key=lambda child: 0 if child == "Meta" else 1)
        # Cycle 16, measured on kids and zen over five containers: the ATTRIBUTES come
        # out `size`, then `totalSize` and `offset` when the container is paged, then
        # everything else alphabetically. plexsim emitted whatever order its dict held.
        lead = {"size": 0, "totalSize": 1, "offset": 2}
        if "noHistory" in attrs:
            # Cycle 18, measured on kids and zen: the PLUG-IN FRAMEWORK's container
            # has an order of its own -- `noHistory, replaceParent, size, identifier`,
            # with `totalSize` FIRST of all when it is paged. It is the one container
            # shape cycle 16's rule does not describe, and the only one on the surface
            # that emits `totalSize` without `offset`.
            lead = {"totalSize": 0, "noHistory": 1, "replaceParent": 2, "size": 3}
        attrs.sort(key=lambda key: (lead.get(key, 4), key))
    elif name == "Type":
        children.sort(key=lambda child: _TYPE_CHILD_ORDER.get(child,
                                                              len(_TYPE_CHILD_ORDER)))
    elif name == "Meta":
        children.sort(key=lambda child: _META_CHILD_ORDER.get(child,
                                                              len(_META_CHILD_ORDER)))
    elif row.get("_light"):
        children.sort(key=lambda child: _LIGHT_CHILD_ORDER.get(child,
                                                               len(_LIGHT_CHILD_ORDER)))
    else:
        children.sort(key=lambda child: _CHILD_ORDER.get(child, len(_CHILD_ORDER)))
    if not container:
        # Cycle 17: a row's attributes come out in a measured order too, not in
        # whatever order the dict that built the row happened to hold them.
        if name == "Player":
            # Cycle 27, measured on the copy with two clients: `Player` is the ONE
            # element inside a `/status/sessions` row that is NOT plainly alphabetical.
            # Its STRING-valued attributes come first, alphabetically, and its
            # non-string ones follow, alphabetically:
            #
            #   address device machineIdentifier model platform platformVersion
            #   product profile state title version | local relayed secure userID
            #
            # Two witnesses -- a Plex Web client sending six strings and a Roku client
            # sending eleven -- and in both the four non-strings (`local`, `relayed` and
            # `secure` are JSON booleans, `userID` an int) sit after every string,
            # although `local` would sort third alphabetically. A FIXED bucket list fits
            # the same data; the type split is the model that also answers for an
            # attribute neither witness carried, and HANDOVER 3.3 records the pair.
            # plexsim emitted whatever order its builder's dict happened to hold.
            attrs.sort(key=lambda key: (not isinstance(row.get(key), str), key))
            return attrs, children
        if row.get("_session"):
            # The alphabetical block, then the decision flags. See `_SESSION_TAIL`.
            attrs.sort(key=lambda key: (_SESSION_TAIL.get(key, 0), key))
            return attrs, children
        if row.get("_pluginLibrary"):
            # Cycle 50's exact legacy-prefix replay: the synthetic library plug-in
            # Directory has its own six-attribute order, byte-identical on claimed
            # 1.43.4 and the unclaimed 1.43.4/1.42.2 copies. It shares neither the
            # ordinary key/title Directory order nor a section row's order.
            order = {key: position for position, key in enumerate(
                ("art", "thumb", "key", "title", "share", "identifier"))}
            attrs.sort(key=lambda key: order.get(key, len(order)))
            return attrs, children
        index = _ATTR_INDEX.get(_attr_bucket(name, row))
        if index:
            lead = _LIBRARY_SECTION_LEAD if row.get("allowSync") is not None else {}
            if row.get("_searchhit"):
                lead = _SEARCH_LEAD
            attrs.sort(key=lambda key: (lead.get(key, 1), index.get(key, len(index))))
    return attrs, children


def ordered(container, overrides=None):
    """The same container with every dict's keys in the order the XML writer emits.

    Cycle 19, measured on kids (1.43.4) and zen (1.42.2): a real PMS's JSON object
    keys come out in exactly its XML order -- the container's attributes, then its
    child keys; a row's attributes in the row's attribute order, then its child
    element keys. plexsim ordered the XML alone and left the JSON in whatever order
    the dict that built it held, so the two formats disagreed on every response. It
    costs a correct parser nothing, and that is the same reason cycle 17's attribute
    order mattered anyway: the document is a measurement.
    """
    def walk(name, row, overrides=None):
        attrs, children = _split(name, row, container=(name.endswith("Container")
                                                       or name in _ROOT_NAMES))
        out = {}
        for key in attrs:
            out[key] = row[key]
        for key in children:
            value = row[key]
            if isinstance(value, dict):
                out[key] = walk(_element_name(key, value, overrides), value)
            else:
                # The ELEMENT name, not the container key: `_attr_bucket` reads it,
                # and a `Metadata` row names its own element through `type`.
                # Cycle 23: the `_xml=` OVERRIDES have to reach this writer too. Without
                # them `/library/sections/{id}/common` bucketed its row as
                # `Video[common]`, which no index knows, so its JSON keys stayed in
                # build order while the XML sorted them by the `Common` bucket -- the
                # last place plexsim's two writers contradicted each other, which cycle
                # 19 established a real PMS never does.
                out[key] = [walk(_element_name(key, entry, overrides), entry)
                            for entry in value]
        return out

    root, body = next(iter(container.items()))
    return {root: walk(root, body, overrides)}


# Cycle 21, measured on kids and zen: a `Feature` element is never self-closed. All
# fifteen on `/media/providers` are written as PAIRED empty elements on both servers,
# where `Directory`, `Action` and `Pivot` in the same document self-close when they have
# no children. It is the reference writer's own quirk and it is element-specific.
# Cycle 22, censused over ~45 paths on kids and zen: six more names are written PAIRED
# when empty. `Part` self-closes while `Stream` never does (37,464 of 37,464 on zen,
# 222 of 222 on kids), so it is an element-NAME table and not "anything that can hold
# children" -- the same shape cycle 21 concluded for `Feature`. `Directory` is left
# out on purpose: both references write it BOTH ways and which is which is an
# attribute-level rule this cycle did not isolate.
# Cycle 23: `Hub` comes OUT of the flat list. Measured on kids and zen, the
# `/hubs/sections/{id}/manage` Hub -- which has no child collection at all -- is
# SELF-CLOSED, while an ordinary hub path's `size="0"` Hub is paired on all three
# servers. Cycle 22 censused ~45 paths and none of them was `/manage`, so it read a
# name rule where the rule is the one it left open for `Directory`: paired when the
# element carries a (possibly empty) child collection, self-closed when it carries none.
# Cycle 27, measured on kids and zen: two more names join the table. An empty `Type`
# (a `Meta/Type` row with no `Filter`, `Sort` or `Field`, which is what
# `/hubs/home/recentlyAdded?includeMeta=1` answers) and an empty `Related` block are
# both written PAIRED on both references, where plexsim self-closed them.
# Cycle 50 added `Playlist`, the eighth: a playlist row with no children is
# `<Playlist ...>\n</Playlist>` on zen, kids and plexscratch42 -- the ordinary rows of
# `/playlists` and the background-processing row alike -- where plexsim self-closed it.
# `PlayQueue`, in the same container family, self-closes on every one of them.
_ALWAYS_PAIRED = ("Feature", "MediaContainer", "Stream", "Extras", "Media",
                  "Activity", "Type", "Related", "Playlist",
                  # Cycle 54: a `Common` is paired on both references even when it
                  # carries no child element -- a mixed set, an empty one.
                  "Common",
                  # Cycle 87: the six legacy `/system/agents` rows with no `MediaType`
                  # (fanarttv, htbackdrops, ...) are `<Agent ...>\n</Agent>` on kids
                  # and zen alike; plexsim self-closed them.
                  "Agent")
_PAIRED_WITH_EMPTY_CHILDREN = ("Hub",)

# Cycle 27: and the `/media/providers` provider rows are paired even when they carry no
# children at all -- the `Home` row, which names no library, is `<Directory hubKey="/hubs"
# title="Home">\n</Directory>` on both references while every other childless `Directory`
# on the surface (an axis, a pivot, a sort) self-closes. That is the attribute-level rule
# cycle 22 left OPEN for `Directory`, and `hubKey` is the signal cycle 19 already uses to
# tell a provider row from a `/library/sections` one.
_PAIRED_DIRECTORY_KEY = "hubKey"


def _render(name, row, out, overrides=None, container=False):
    attrs, children = _split(name, row, container)

    out.append("<" + name)
    for key in attrs:
        value = row[key]
        if isinstance(value, (list, tuple)):
            value = ",".join(_attr(v) for v in value)
        else:
            value = _format(key, value, row)
        out.append(" %s=%s" % (key, _escape(_attr(value))))

    if not children:
        # Cycle 21: a `Feature` is written paired even when it is empty.
        # Cycle 22: every tag ends its own LINE -- see `to_xml`.
        # Cycle 23: a `Hub` is paired only when it CARRIES a child collection, even an
        # empty one -- `/manage`'s, which has none, self-closes on both references.
        paired = name in _ALWAYS_PAIRED or (
            name in _PAIRED_WITH_EMPTY_CHILDREN
            and any(isinstance(value, (list, tuple, dict))
                    for key, value in row.items() if not str(key).startswith("_"))
        ) or (name == "Directory" and row.get(_PAIRED_DIRECTORY_KEY) is not None)
        out.append(">\n</%s>\n" % name if paired else " />\n")
        return

    out.append(">\n")
    for key in children:
        value = row[key]
        for child in ([value] if isinstance(value, dict) else list(value)):
            # Overrides apply to the container's own children only; a nested
            # `Metadata` inside one of them still names itself.
            _render(_element_name(key, child, overrides), child, out)
    out.append("</%s>\n" % name)


def to_system_xml(container, elements=None):
    """Write the plug-in framework's `/system` XML form.

    Cycle 50 measured this byte-identically on kids, zen, and plexscratch42. It
    differs from the core writer in its prologue, indentation, compact `/>`, and
    self-closing empty root. The response's `application/xml` type is selected by
    the caller.
    """
    root, body = next(iter(container.items()))
    attrs, children = _split(root, body, container=True)

    def _pair(name, row, key):
        return " %s=%s" % (key, _escape(_attr(_format(key, row[key], row))))

    text = "<?xml version='1.0' encoding='utf-8'?>\n<%s%s" % (
        root, "".join(_pair(root, body, key) for key in attrs))
    rows = []
    for key in children:
        value = body[key]
        for row in (value if isinstance(value, list) else [value]):
            if not isinstance(row, dict):
                continue
            name = (elements or {}).get(key) or _element_name(key, row)
            row_attrs, _ = _split(name, row)
            rows.append("  <%s%s/>" % (
                name, "".join(_pair(name, row, attr) for attr in row_attrs)))
    if not rows:
        return (text + "/>\n").encode("utf-8")
    return (text + ">\n" + "\n".join(rows) + "\n</%s>\n" % root).encode("utf-8")


def to_xml(container, elements=None):
    """Serialize a {"MediaContainer": {...}} dict to a Plex XML document.

    `elements` overrides the element name for a container key, for the endpoints
    whose rows do not carry a `type` to name themselves with.
    """
    root, body = next(iter(container.items()))
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n']
    # Cycle 49: the ROOT is not automatically a container. `to_xml` hard-coded it, so
    # `/myplex/account` -- whose root is `MyPlex` -- came out with its attributes sorted
    # alphabetically in XML while `ordered()`, which computes the same predicate
    # properly, left the JSON in the reference's order. Both references emit ONE order in
    # both formats (`authToken username mappingState mappingError signInState
    # publicAddress publicPort privateAddress privatePort subscriptionFeatures
    # subscriptionActive subscriptionState`), so this was the fifth instance of the
    # two-writer bug cycle 19 established never happens on a real PMS -- and the only one
    # left, because `ButlerTasks` and `Query` carry no attributes at all to disagree on.
    _render(root, body, out, elements,
            container=(root.endswith("Container") or root in _ROOT_NAMES))
    # Cycle 22, measured on zen (236 newlines in one movie's document) and kids (34):
    # a real PMS writes ONE element per line, with no indentation, and the document
    # ends with a newline. plexsim joined the pieces with nothing, so every XML
    # response was a single line -- which changes `Content-Length` on all of them and
    # what a `Range` read of a container returns (cycle 16: `Range` applies to every
    # body). `_render` now ends each tag with its own newline.
    return "".join(out).encode("utf-8")
