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
}


# The order a row's child elements come in, measured on both servers across movie,
# show and episode reads with every include option: media first, then artwork, then
# the tags, then the blocks an option adds. plexsim wrote them in whatever order the
# row dict held them -- Genre before Image, Guid after the people, Preferences after
# Extras -- 41 pairs inverted. A child not listed keeps its place after these.
_CHILD_ORDER = {name: index for index, name in enumerate((
    "Media", "Image", "UltraBlurColors", "Genre", "Country", "Guid", "Rating",
    "Collection", "Director", "Writer", "Role", "Producer", "Label", "CommonSenseMedia",
    "Chapter", "Marker", "Review", "Field", "Location", "Preferences", "OnDeck",
    "Extras", "Related", "Children"))}

# Cycle 16, measured on kids and zen: inside a `Meta/Type` the order is
# `Filter, Sort, Field` -- which is NOT the item order above, where `Field` comes
# before `Location`. plexsim sorted these three by the item table, which knows only
# `Field`, so they came out in whatever order the dict held: `Field, Filter, Sort`.
_TYPE_CHILD_ORDER = {name: index for index, name in enumerate(
    ("Filter", "Sort", "Field"))}


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
    if overrides and key in overrides:
        return overrides[key]
    if key == "Metadata":
        return _METADATA_ELEMENT.get(str(row.get("type", "")).lower(), "Video")
    return key


def _render(name, row, out, overrides=None, container=False):
    attrs = []
    children = []

    for key, value in row.items():
        if value is None:
            continue
        if isinstance(value, dict):
            children.append((key, [value]))
        elif isinstance(value, (list, tuple)):
            # A list of dicts is a child element set; a list of scalars has no
            # representation in Plex XML, so it is joined the way tag lists are.
            if value and all(isinstance(v, dict) for v in value):
                children.append((key, list(value)))
            elif value:
                attrs.append((key, ",".join(_attr(v) for v in value)))
        else:
            attrs.append((key, _format(key, value, row)))

    if container:
        # Measured: a listing's `Meta` comes BEFORE its rows; the rest keep their order.
        children.sort(key=lambda child: 0 if child[0] == "Meta" else 1)
        # Cycle 16, measured on kids and zen over five containers: the ATTRIBUTES come
        # out `size`, then `totalSize` and `offset` when the container is paged, then
        # everything else alphabetically. plexsim emitted whatever order its dict held.
        lead = {"size": 0, "totalSize": 1, "offset": 2}
        attrs.sort(key=lambda pair: (lead.get(pair[0], 3), pair[0]))
    elif name == "Type":
        children.sort(key=lambda child: _TYPE_CHILD_ORDER.get(child[0],
                                                              len(_TYPE_CHILD_ORDER)))
    else:
        children.sort(key=lambda child: _CHILD_ORDER.get(child[0], len(_CHILD_ORDER)))

    out.append("<" + name)
    for key, value in attrs:
        out.append(" %s=%s" % (key, _escape(_attr(value))))

    if not children:
        out.append(" />")
        return

    out.append(">")
    for key, rows in children:
        for child in rows:
            # Overrides apply to the container's own children only; a nested
            # `Metadata` inside one of them still names itself.
            _render(_element_name(key, child, overrides), child, out)
    out.append("</%s>" % name)


def to_xml(container, elements=None):
    """Serialize a {"MediaContainer": {...}} dict to a Plex XML document.

    `elements` overrides the element name for a container key, for the endpoints
    whose rows do not carry a `type` to name themselves with.
    """
    root, body = next(iter(container.items()))
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n']
    _render(root, body, out, elements, container=True)
    return "".join(out).encode("utf-8")
