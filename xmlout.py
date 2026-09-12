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

from xml.sax.saxutils import quoteattr

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


def _render(name, row, out, overrides=None):
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

    out.append("<" + name)
    for key, value in attrs:
        out.append(" %s=%s" % (key, quoteattr(_attr(value))))

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
    _render(root, body, out, elements)
    return "".join(out).encode("utf-8")
