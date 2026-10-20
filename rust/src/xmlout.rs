//! Measured Plex JSON-to-XML serializer.

use std::collections::HashMap;

use serde_json::{Map, Value};

const CHILD_ORDER: &[&str] = &[
    "Media",
    "Image",
    "UltraBlurColors",
    "Genre",
    "Country",
    "Style",
    "Thumb",
    "Art",
    "Guid",
    "Rating",
    "Theme",
    "Collection",
    "Director",
    "Writer",
    "Role",
    "Producer",
    "Label",
    "Similar",
    "CommonSenseMedia",
    "Chapter",
    "Marker",
    "Review",
    "Field",
    "Location",
    "Preferences",
    "OnDeck",
    "Extras",
    "Related",
    "Children",
];
const LIGHT_CHILD_ORDER: &[&str] = &[
    "Media",
    "Image",
    "UltraBlurColors",
    "Guid",
    "Genre",
    "Country",
    "Director",
    "Writer",
    "Role",
    "Collection",
    "Location",
];
const TYPE_CHILD_ORDER: &[&str] = &["Filter", "Sort", "Field"];
const META_CHILD_ORDER: &[&str] = &["Type", "FieldType"];
const ALWAYS_PAIRED: &[&str] = &[
    "Feature",
    "MediaContainer",
    "Stream",
    "Extras",
    "Media",
    "Activity",
    "Type",
    "Related",
    "Playlist",
    "Common",
    // Cycle 87: an empty legacy `/system/agents` row is paired on plexlab and the 1.42.2
    // reference.
    "Agent",
];
const PAIRED_EMPTY: &[&str] = &["Hub"];

fn metadata_element(row: &Map<String, Value>) -> String {
    match row.get("_element").and_then(Value::as_str) {
        Some(x) => x.to_string(),
        None => match row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_ascii_lowercase()
            .as_str()
        {
            "movie" | "episode" | "clip" => "Video",
            "show" | "season" | "artist" | "album" | "collection" => "Directory",
            "playlist" => "Playlist",
            "track" => "Track",
            "image" => "Photo",
            _ => "Video",
        }
        .into(),
    }
}
fn element_name(
    key: &str,
    row: &Map<String, Value>,
    overrides: Option<&HashMap<String, String>>,
) -> String {
    if let Some(x) = row.get("_element").and_then(Value::as_str) {
        return x.into();
    }
    if let Some(x) = overrides.and_then(|m| m.get(key)) {
        return x.clone();
    }
    if key == "Metadata" {
        metadata_element(row)
    } else {
        key.into()
    }
}
fn attr_value(value: &Value) -> String {
    match value {
        Value::Bool(v) => {
            if *v {
                "1".into()
            } else {
                "0".into()
            }
        }
        Value::Null => String::new(),
        Value::String(v) => v.clone(),
        Value::Number(v) => v.to_string(),
        _ => value.to_string(),
    }
}
fn format_value(key: &str, value: &Value, row: &Map<String, Value>) -> String {
    if let Some(value) = value.as_f64() {
        if key == "frameRate" {
            return format!("{:.3}", value);
        }
        if key == "score" {
            return format!("{:.5}", value);
        }
        if (key == "value" || key == "default")
            && row.get("type").and_then(Value::as_str) == Some("double")
            && value.fract() == 0.0
        {
            return format!("{}", value as i64);
        }
    }
    attr_value(value)
}
fn escape(text: &str) -> String {
    let mut out = String::new();
    escape_into(&mut out, text);
    out
}
/// `escape`, written onto `out`.
fn escape_into(out: &mut String, text: &str) {
    out.push('"');
    // Most values need no escaping at all.
    if text.bytes().all(|b| (0x20..0x7f).contains(&b) && !matches!(b, b'&' | b'<' | b'>' | b'"' | b'\'')) {
        out.push_str(text);
        out.push('"');
        return;
    }
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&#34;"),
            '\'' => out.push_str("&#39;"),
            '\t' => out.push_str("&#9;"),
            '\n' => out.push_str("&#xA;"),
            '\r' => out.push_str("&#xD;"),
            // Cycle 17: every control character XML 1.0 forbids -- U+0001..U+001F
            // bar TAB/LF/CR, and U+007F -- is a literal `?`.
            c if ((c as u32) < 0x20 && c != '\u{9}' && c != '\u{a}' && c != '\u{d}')
                || c as u32 == 0x7f =>
            {
                out.push('?')
            }
            c if c as u32 >= 0x80 => {
                use std::fmt::Write;
                let _ = write!(out, "&#{};", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
fn child_rank(name: &str, key: &str, row: &Map<String, Value>) -> usize {
    // xmlout.py `_split`: the ELEMENT decides the table -- a `Type` row orders
    // its children Filter, Sort, Field and a `Meta` its own -- then a light row.
    let list = if name == "Type" || row.get("_element").and_then(Value::as_str) == Some("Type") {
        TYPE_CHILD_ORDER
    } else if name == "Meta" {
        META_CHILD_ORDER
    } else if row.get("_light").is_some() {
        LIGHT_CHILD_ORDER
    } else {
        CHILD_ORDER
    };
    list.iter().position(|v| *v == key).unwrap_or(list.len())
}
/// `attr_order` for one row, its table and search lead worked out once.
fn attr_orderer<'a>(name: &str, row: &'a Map<String, Value>) -> impl Fn(&str) -> usize + 'a {
    let table = attr_table(name, row);
    // `xmlout.py` applies `_SEARCH_LEAD` only where the row's bucket HAS an attribute
    // table (`if index:`); a bucket without one -- a playlist hit -- keeps its own
    // order, `score` last where the builder appended it.
    let search_lead = row.get("_searchhit").is_some()
        && row.keys().any(|known| attr_rank(&table, known) != usize::MAX);
    move |key: &str| {
        if search_lead {
            // `xmlout.py` `_SEARCH_LEAD`: a search hit LEADS with `librarySectionTitle`
            // and then its `score`, ahead of the row's own order (cycle 25).
            return match key {
                "librarySectionTitle" => 0,
                "score" => 1,
                _ => attr_rank(&table, key).saturating_add(2),
            };
        }
        attr_rank(&table, key)
    }
}

/// Which attribute order a row's element takes, chosen once per row: `attr_order_base`
/// chose it -- a bucket name formatted and matched -- for every comparison of every
/// attribute, most of an unpaged episode listing's ordering pass (2026-10-17).
enum AttrTable {
    Session,
    Plugin,
    Order(&'static [&'static str]),
}

fn attr_rank(table: &AttrTable, key: &str) -> usize {
    match table {
        AttrTable::Session => match key {
            "selected" => 1,
            "decision" => 2,
            _ => 0,
        },
        AttrTable::Plugin => match key {
            "art" => 0,
            "thumb" => 1,
            "key" => 2,
            "title" => 3,
            "share" => 4,
            "identifier" => 5,
            _ => 6,
        },
        AttrTable::Order(order) => order.iter().position(|value| *value == key).unwrap_or(usize::MAX),
    }
}

fn attr_table(name: &str, row: &Map<String, Value>) -> AttrTable {
    if row.get("_session").is_some() {
        return AttrTable::Session;
    }
    if row.get("_pluginLibrary").is_some() {
        return AttrTable::Plugin;
    }
    let bucket = if name == "Video" && row.get("_history1").is_some() {
        "Video[history1]".to_string()
    } else if name == "Video" && row.get("_history").is_some() {
        "Video[history]".to_string()
    } else if name == "Agent" && row.get("primary").is_some() {
        // Cycle 87, measured on the 1.42.2 reference in JSON and XML: a legacy plug-in
        // row (the one carrying `primary`) is `hasPrefs, identifier, primary, name`, not
        // the modern row's `identifier, name, hasPrefs`.
        "Agent[legacy]".to_string()
    } else if matches!(name, "Video" | "Directory" | "Track" | "Playlist")
        && (row.get("agent").is_some() || row.get("scanner").is_some())
    {
        if row.get("hubKey").is_some() {
            format!("{name}[PROVIDER]")
        } else {
            format!("{name}[SECTION]")
        }
    } else if matches!(name, "Video" | "Directory" | "Track" | "Playlist") {
        format!(
            "{name}[{}]",
            row.get("type").and_then(Value::as_str).unwrap_or("none")
        )
    } else {
        name.to_string()
    };
    let order: &'static [&'static str] = match bucket.as_str() {
        "Common" => &[
            "grandparentRatingKey",
            "grandparentTitle",
            "mixedFields",
            "userRating",
            "parentRatingKey",
            "parentTitle",
            "ratingKey",
            "key",
            "guid",
            "studio",
            "type",
            "title",
            "titleSort",
            "contentRating",
            "summary",
            "index",
            "year",
            "tagline",
            "originallyAvailableAt",
        ],
        "Feature" => &["flavor", "scrobbleKey", "unscrobbleKey", "key", "type"],
        "Type" => &["key", "type", "title", "active", "activeDirection"],
        "Directory[PROVIDER]" => &[
            "agent",
            "language",
            "refreshing",
            "scanner",
            "uuid",
            "id",
            "key",
            "hubKey",
            "type",
            "title",
            "updatedAt",
            "scannedAt",
        ],
        "AgeRating" => &["type", "rating", "age"],
        "CommonSenseMedia" => &["id", "oneLiner"],
        "Country" => &["id", "filter", "tag"],
        "Thumb" => &["key", "previewKey", "source"],
        "Art" => &["key", "previewKey", "source"],
        "Theme" => &["key", "previewKey", "source"],
        "Collection" => &["id", "filter", "tag"],
        "Label" => &["id", "filter", "tag"],
        "Similar" => &["id", "filter", "tag"],
        "Style" => &["id", "filter", "tag"],
        "Director" => &["id", "filter", "tag", "tagKey", "thumb"],
        "Directory[SECTION]" => &[
            "art",
            "allowSync",
            "filters",
            "refreshing",
            "key",
            "thumb",
            "type",
            "title",
            "agent",
            "scanner",
            "language",
            "metadataAgentProviderGroupId",
            "uuid",
            "updatedAt",
            "createdAt",
            "scannedAt",
            "content",
            "directory",
            "contentChangedAt",
            "hidden",
        ],
        "Directory[filter]" => &["filter", "filterType", "key", "title", "type"],
        "Directory[genre]" => &["fastKey", "key", "title", "type"],
        "Directory[none]" => &[
            "id",
            "filter",
            "tag",
            "tagType",
            "tagKey",
            "secondary",
            "prompt",
            "search",
            "default",
            "defaultDirection",
            "descKey",
            "firstCharacterKey",
            "size",
            "fastKey",
            "leafCount",
            "thumb",
            "viewedLeafCount",
            "count",
            "hubKey",
            "key",
            "title",
        ],
        "Directory[season]" => &[
            "ratingKey",
            "key",
            "parentRatingKey",
            "guid",
            "parentGuid",
            "parentSlug",
            "parentStudio",
            "type",
            "title",
            "titleSort",
            "parentKey",
            "librarySectionTitle",
            "librarySectionID",
            "librarySectionKey",
            "parentTitle",
            "parentSummary",
            "summary",
            "index",
            "parentIndex",
            "viewCount",
            "skipCount",
            "viewOffset",
            "lastViewedAt",
            "includedAt",
            "userRating",
            "lastRatedAt",
            "year",
            "parentYear",
            "thumb",
            "art",
            "parentThumb",
            "parentTheme",
            "leafCount",
            "viewedLeafCount",
            "addedAt",
            "updatedAt",
            "deletedAt",
            // Cycle 94, the 1.42.2 reference's `/library/all/top?type=3` row.
            "globalViewCount",
            "userCount",
        ],
        "Directory[show]" => &[
            "ratingKey",
            "key",
            "guid",
            "slug",
            "studio",
            "type",
            "title",
            "librarySectionTitle",
            "librarySectionID",
            "librarySectionKey",
            "titleSort",
            "originalTitle",
            "contentRating",
            "contentRatingAge",
            "summary",
            "index",
            "audienceRating",
            "userRating",
            "viewCount",
            "skipCount",
            "viewOffset",
            "lastViewedAt",
            "includedAt",
            "lastRatedAt",
            "year",
            "tagline",
            "thumb",
            "art",
            "theme",
            "duration",
            "originallyAvailableAt",
            "leafCount",
            "viewedLeafCount",
            "childCount",
            "seasonCount",
            "addedAt",
            "updatedAt",
            "deletedAt",
            // Cycle 94, the 1.42.2 reference's `/library/all/top?type=2` row.
            "globalViewCount",
            "userCount",
            "audienceRatingImage",
            "episodeSort",
            "flattenSeasons",
        ],
        "Field" => &["locked", "name"],
        "Agent" => &["identifier", "name", "hasPrefs", "id", "primary"],
        "Agent[legacy]" => &["id", "hasPrefs", "identifier", "primary", "name"],
        "Video[history1]" => &[
            "historyKey",
            "librarySectionID",
            "title",
            "grandparentTitle",
            "type",
            "index",
            "parentIndex",
            "originallyAvailableAt",
            "viewedAt",
            "accountID",
            "deviceID",
            "viewType",
        ],
        "Genre" => &["id", "filter", "tag"],
        "Hub" => &[
            "identifier",
            "hubKey",
            "upNext",
            "key",
            "title",
            "type",
            "hubIdentifier",
            "context",
            "size",
            "more",
            "random",
            "style",
            "promoted",
        ],
        "Sort" => &[
            "active",
            "activeDirection",
            "default",
            "defaultDirection",
            "descKey",
            "firstCharacterKey",
            "key",
            "title",
        ],
        "Image" => &["alt", "type", "url"],
        "Location" => &["id", "path"],
        "Media" => &[
            "id",
            "duration",
            "bitrate",
            "width",
            "height",
            "aspectRatio",
            "audioChannels",
            "audioCodec",
            "videoCodec",
            "videoResolution",
            "container",
            "videoFrameRate",
            "optimizedForStreaming",
            "audioProfile",
            "has64bitOffsets",
            "videoProfile",
            "deletedAt",
            "hasVoiceActivity",
        ],
        "Part" => &[
            "accessible",
            "exists",
            "id",
            "key",
            "duration",
            "file",
            "size",
            "audioProfile",
            "container",
            "failureBIFResultCode",
            "failureBIFRetryCount",
            "has64bitOffsets",
            "hasThumbnail",
            "indexes",
            "optimizedForStreaming",
            "packetLength",
            "videoProfile",
        ],
        "Producer" => &["id", "filter", "tag", "tagKey", "thumb"],
        "Rating" => &["image", "value", "type"],
        "Role" => &["id", "filter", "tag", "tagKey", "role", "thumb"],
        "Video[history]" => &[
            "historyKey",
            "key",
            "ratingKey",
            "librarySectionID",
            "parentKey",
            "grandparentKey",
            "title",
            "grandparentTitle",
            "type",
            "thumb",
            "parentThumb",
            "grandparentThumb",
            "grandparentArt",
            "index",
            "parentIndex",
            "originallyAvailableAt",
            "viewedAt",
            "accountID",
            "deviceID",
            "viewType",
        ],
        // A subtitle Plex's provider gave puts `key` second, its provider after its language and
        // `transient`/`userID` after its title (plexlab, `subtitles/plex_search_apply_s143.json`);
        // a file's own streams carry none of them.
        "Stream" => &[
            "id",
            "key",
            "streamType",
            "canAutoSync",
            "selected",
            "default",
            "codec",
            "index",
            "channels",
            "bitrate",
            "language",
            "languageTag",
            "languageCode",
            "format",
            "providerTitle",
            "score",
            "sourceKey",
            "anamorphic",
            "bitDepth",
            "chromaLocation",
            "chromaSubsampling",
            "codedHeight",
            "codedWidth",
            "audioChannelLayout",
            "colorPrimaries",
            "colorRange",
            "colorSpace",
            "colorTrc",
            "frameRate",
            "hasScalingMatrix",
            "height",
            "level",
            "pixelAspectRatio",
            "profile",
            "refFrames",
            "scanType",
            "samplingRate",
            "streamIdentifier",
            "hearingImpaired",
            "title",
            "transient",
            "userID",
            "width",
            "displayTitle",
            "extendedDisplayTitle",
        ],
        "UltraBlurColors" => &["topLeft", "topRight", "bottomRight", "bottomLeft"],
        "Video[episode]" => &[
            "ratingKey",
            "key",
            "parentRatingKey",
            "grandparentRatingKey",
            "guid",
            "parentGuid",
            "grandparentGuid",
            "grandparentSlug",
            "studio",
            "type",
            "title",
            "titleSort",
            "grandparentKey",
            "parentKey",
            "librarySectionTitle",
            "librarySectionID",
            "librarySectionKey",
            "grandparentTitle",
            "parentTitle",
            "contentRating",
            "summary",
            "index",
            "parentIndex",
            "audienceRating",
            "viewCount",
            "skipCount",
            "viewOffset",
            "lastViewedAt",
            "includedAt",
            "userRating",
            "lastRatedAt",
            "year",
            "thumb",
            "art",
            "parentThumb",
            "grandparentThumb",
            "grandparentArt",
            "grandparentTheme",
            "duration",
            "originallyAvailableAt",
            "addedAt",
            "updatedAt",
            // Cycle 94, the 1.42.2 reference's `/library/all/top?type=4` rows.
            "globalViewCount",
            "userCount",
            "audienceRatingImage",
            "deletedAt",
            "chapterSource",
        ],
        "Video[clip]" => &[
            "ratingKey",
            "key",
            "guid",
            "type",
            "title",
            "titleSort",
            "summary",
            "index",
            "year",
            "thumb",
            "primaryGuid",
            "art",
            "subtype",
            "duration",
            "originallyAvailableAt",
            "addedAt",
            "updatedAt",
            "extraType",
        ],
        "Video[movie]" => &[
            "ratingKey",
            "key",
            "guid",
            "slug",
            "studio",
            "type",
            "title",
            "titleSort",
            "librarySectionTitle",
            "librarySectionID",
            "librarySectionKey",
            "originalTitle",
            "isAdult",
            "contentRating",
            "contentRatingAge",
            "summary",
            "rating",
            "audienceRating",
            "userRating",
            "viewCount",
            "lastViewedAt",
            "includedAt",
            "lastRatedAt",
            "year",
            "tagline",
            "thumb",
            "art",
            "duration",
            "originallyAvailableAt",
            "addedAt",
            "updatedAt",
            "deletedAt",
            // Cycle 94, plexlab's `/library/all/top?type=1` rows.
            "globalViewCount",
            "userCount",
            "audienceRatingImage",
            "chapterSource",
            "primaryExtraKey",
            "useOriginalTitle",
            "ratingImage",
        ],
        "Writer" => &["id", "filter", "tag", "tagKey", "thumb"],
        _ => &[],
    };
    AttrTable::Order(order)
}
/// A row's attributes and child elements, in the order the XML writes them, borrowed from it.
fn split<'r>(name: &str, row: &'r Map<String, Value>, container: bool) -> (Vec<(&'r str, &'r Value)>, Vec<(&'r str, &'r Value)>) {
    let mut attrs = Vec::new();
    let mut children = Vec::new();
    for (k, v) in row {
        if k.starts_with('_') || v.is_null() {
            continue;
        }
        match v {
            Value::Object(_) => children.push((k.as_str(), v)),
            Value::Array(values) => {
                if !values.is_empty() && values.iter().all(Value::is_object) {
                    children.push((k.as_str(), v))
                } else if !values.is_empty() {
                    attrs.push((k.as_str(), v))
                }
            }
            _ => attrs.push((k.as_str(), v)),
        }
    }
    if container {
        let lead = |key: &str| {
            if row.contains_key("noHistory") {
                match key {
                    "totalSize" => 0,
                    "noHistory" => 1,
                    "replaceParent" => 2,
                    "size" => 3,
                    _ => 4,
                }
            } else {
                match key {
                    "size" => 0,
                    "totalSize" => 1,
                    "offset" => 2,
                    _ => 3,
                }
            }
        };
        attrs.sort_by_key(|&(k, _)| (lead(k), k));
        children.sort_by_key(|&(k, _)| if k == "Meta" { 0 } else { 1 });
    } else {
        // Row ordering is a stable topological order. Unknown fields retain the
        // insertion order supplied by the measured JSON document; alphabetising
        // the tie changes the byte stream for DOVI and other agent extensions.
        if name == "Player" {
            attrs.sort_by_key(|&(key, value)| (if value.as_str().is_some() { 0 } else { 1 }, key));
        } else if row.get("_session").is_some() || row.get("_pluginLibrary").is_some() {
            let rank = attr_orderer(name, row);
            attrs.sort_by_cached_key(|&(k, _)| (rank(k), k));
        } else if row.get("allowSync").is_some_and(|value| !value.is_null()) {
            // Cycle 19: the rows of `/library/recentlyAdded` and `/library/onDeck` -- the
            // only rows carrying `allowSync` -- LEAD with it and the librarySection
            // triple (`personal`, `sourceTitle` inside the same lead on `/search`).
            let lead = |key: &str| match key {
                "allowSync" => 0,
                "librarySectionID" => 1,
                "librarySectionTitle" => 2,
                "librarySectionUUID" => 3,
                "personal" => 4,
                "sourceTitle" => 5,
                _ => 6,
            };
            let rank = attr_orderer(name, row);
            attrs.sort_by_cached_key(|&(k, _)| (lead(k), rank(k)));
        } else {
            // Each key's rank once (`sort_by_cached_key` is stable too): ranking inside
            // the comparisons was 40% of an unpaged episode listing (2026-10-17).
            let rank = attr_orderer(name, row);
            attrs.sort_by_cached_key(|&(k, _)| rank(k));
        }
        // A STABLE sort, as Python's: unknown children keep their order.
        children.sort_by_cached_key(|&(k, _)| child_rank(name, k, row));
    }
    (attrs, children)
}

fn render(
    name: &str,
    row: &Map<String, Value>,
    out: &mut String,
    overrides: Option<&HashMap<String, String>>,
    container: bool,
) {
    let (attrs, children) = split(name, row, container);
    out.push('<');
    out.push_str(name);
    for &(key, value) in &attrs {
        out.push(' ');
        out.push_str(key);
        out.push('=');
        match value {
            Value::Array(values) => escape_into(out, &values.iter().map(attr_value).collect::<Vec<_>>().join(",")),
            Value::String(text) => escape_into(out, text),
            value => escape_into(out, &format_value(key, value, row)),
        }
    }
    if children.is_empty() {
        let paired = ALWAYS_PAIRED.contains(&name)
            || (PAIRED_EMPTY.contains(&name)
                && row.iter().any(|(k, v)| {
                    !k.starts_with('_') && matches!(v, Value::Array(_) | Value::Object(_))
                }))
            || (name == "Directory" && row.get("hubKey").is_some_and(|value| !value.is_null()));
        if paired {
            out.push_str(">\n</");
            out.push_str(name);
            out.push_str(">\n")
        } else {
            out.push_str(" />\n")
        }
        return;
    }
    out.push_str(">\n");
    for (key, value) in children {
        match value {
            Value::Object(child) => {
                let child_name = element_name(key, child, overrides);
                render(&child_name, child, out, None, false)
            }
            Value::Array(values) => {
                for child in values {
                    if let Some(child) = child.as_object() {
                        let child_name = element_name(key, child, overrides);
                        render(&child_name, child, out, None, false);
                    }
                }
            }
            _ => {}
        }
    }
    out.push_str("</");
    out.push_str(name);
    out.push_str(">\n");
}

pub fn to_xml(container: &Value, overrides: Option<&HashMap<String, String>>) -> Vec<u8> {
    let Some(root_map) = container.as_object() else {
        return Vec::new();
    };
    let Some((root, body)) = root_map.iter().next() else {
        return Vec::new();
    };
    let Some(body) = body.as_object() else {
        return Vec::new();
    };
    let mut out = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n".to_string();
    render(
        root,
        body,
        &mut out,
        overrides,
        root.ends_with("Container") || root == "ButlerTasks" || root == "Query",
    );
    out.into_bytes()
}
pub fn ordered(container: &Value, overrides: Option<&HashMap<String, String>>) -> Value {
    into_ordered(container.clone(), overrides)
}

/// `ordered`, moving the values out of `container` instead of copying them: a response of
/// every episode of the production server's library (92 MB of JSON) held the parsed body, its ordered copy
/// and that copy without private markers at once, 2.4 GB above the library (2026-10-16).
pub fn into_ordered(container: Value, overrides: Option<&HashMap<String, String>>) -> Value {
    fn walk(
        key: &str,
        mut row: Map<String, Value>,
        overrides: Option<&HashMap<String, String>>,
        container: bool,
    ) -> Value {
        let name = element_name(key, &row, overrides);
        let (attrs, children) = split(&name, &row, container);
        let owned = |keys: Vec<(&str, &Value)>| keys.into_iter().map(|(k, _)| k.to_string()).collect::<Vec<_>>();
        let (attrs, children) = (owned(attrs), owned(children));
        let mut out = Map::new();
        for k in attrs {
            let value = row.swap_remove(&k).unwrap_or(Value::Null);
            out.insert(k, value);
        }
        for k in children {
            match row.swap_remove(&k) {
                Some(Value::Object(child)) => {
                    let child = walk(&k, child, overrides, false);
                    out.insert(k, child);
                }
                Some(Value::Array(values)) => {
                    let values = values
                        .into_iter()
                        .filter_map(|v| match v {
                            Value::Object(child) => Some(walk(&k, child, overrides, false)),
                            _ => None,
                        })
                        .collect();
                    out.insert(k, Value::Array(values));
                }
                _ => {}
            }
        }
        Value::Object(out)
    }
    let Value::Object(mut root_map) = container else {
        return container;
    };
    let Some(root_name) = root_map.keys().next().cloned() else {
        return Value::Object(root_map);
    };
    match root_map.shift_remove(&root_name) {
        Some(Value::Object(body)) => {
            let is_container = root_name.ends_with("Container") || root_name == "ButlerTasks" || root_name == "Query";
            let mut out = Map::new();
            let body = walk(&root_name, body, overrides, is_container);
            out.insert(root_name, body);
            Value::Object(out)
        }
        Some(other) => {
            root_map.shift_insert(0, root_name, other);
            Value::Object(root_map)
        }
        None => Value::Object(root_map),
    }
}
/// `into_ordered` and `runtime::strip_private_in_place`, then `serde_json::to_vec`, in one
/// pass over the document as it is: the same bytes, without building the ordered copy --
/// each of its maps hashed every key twice, half of an unpaged 34k-row listing (2026-10-18).
/// `None` when the document is not `{"<root>": {...}}`, which `into_ordered` leaves as it is.
pub fn write_ordered_json(container: &Value, overrides: Option<&HashMap<String, String>>) -> Option<Vec<u8>> {
    fn write_key(out: &mut Vec<u8>, key: &str) {
        let _ = serde_json::to_writer(&mut *out, key);
        out.push(b':');
    }
    fn walk(out: &mut Vec<u8>, key: &str, row: &Map<String, Value>, overrides: Option<&HashMap<String, String>>, container: bool) {
        let name = element_name(key, row, overrides);
        let (attrs, children) = split(&name, row, container);
        out.push(b'{');
        let mut first = true;
        let mut comma = |out: &mut Vec<u8>| {
            if !std::mem::take(&mut first) {
                out.push(b',');
            }
        };
        for &(k, value) in &attrs {
            comma(out);
            write_key(out, k);
            stripped(out, value);
        }
        for &(k, value) in &children {
            match value {
                Value::Object(child) => {
                    comma(out);
                    write_key(out, k);
                    walk(out, k, child, overrides, false);
                }
                Value::Array(values) => {
                    comma(out);
                    write_key(out, k);
                    out.push(b'[');
                    let mut first = true;
                    for child in values.iter().filter_map(Value::as_object) {
                        if !std::mem::take(&mut first) {
                            out.push(b',');
                        }
                        walk(out, k, child, overrides, false);
                    }
                    out.push(b']');
                }
                _ => {}
            }
        }
        out.push(b'}');
    }
    /// A value as `strip_private_in_place` leaves it: each map's packed streams unpacked in
    /// their place (`catalog::unpack_part`) and its `_` keys gone, at every depth.
    fn stripped(out: &mut Vec<u8>, value: &Value) {
        match value {
            Value::Object(object) => {
                out.push(b'{');
                let mut first = true;
                for (k, v) in object {
                    let unpacked;
                    let (k, v) = match (k.as_str(), v) {
                        (crate::catalog::PACKED_STREAMS, Value::String(text)) => {
                            unpacked = serde_json::from_str(text).unwrap_or_else(|_| Value::Array(Vec::new()));
                            ("Stream", &unpacked)
                        }
                        (k, _) if k.starts_with('_') => continue,
                        (k, v) => (k, v),
                    };
                    if !std::mem::take(&mut first) {
                        out.push(b',');
                    }
                    write_key(out, k);
                    stripped(out, v);
                }
                out.push(b'}');
            }
            Value::Array(values) => {
                out.push(b'[');
                for (index, value) in values.iter().enumerate() {
                    if index > 0 {
                        out.push(b',');
                    }
                    stripped(out, value);
                }
                out.push(b']');
            }
            scalar => {
                let _ = serde_json::to_writer(&mut *out, scalar);
            }
        }
    }
    let root = container.as_object().filter(|root| root.len() == 1)?;
    let (root_name, Value::Object(body)) = root.iter().next()? else {
        return None;
    };
    let is_container = root_name.ends_with("Container") || root_name == "ButlerTasks" || root_name == "Query";
    let mut out = Vec::with_capacity(1 << 16);
    out.push(b'{');
    write_key(&mut out, root_name);
    walk(&mut out, root_name, body, overrides, is_container);
    out.push(b'}');
    Some(out)
}

/// Serialize the plug-in framework's compact `/system` response form.
///
/// This is a separate measured writer: it uses a lowercase XML declaration,
/// no whitespace before `/>`, and one level of two-space indentation. Child
/// rows are intentionally shallow; the framework's system endpoints do not
/// emit nested row elements in this form.
pub fn to_system_xml(container: &Value, overrides: Option<&HashMap<String, String>>) -> Vec<u8> {
    let Some(root_map) = container.as_object() else {
        return Vec::new();
    };
    let Some((root, body)) = root_map.iter().next() else {
        return Vec::new();
    };
    let Some(body) = body.as_object() else {
        return Vec::new();
    };
    let (attrs, children) = split(root, body, true);
    let mut out = format!("<?xml version='1.0' encoding='utf-8'?>\n<{}", root);
    for (key, value) in attrs {
        let value = if let Value::Array(values) = value {
            values.iter().map(attr_value).collect::<Vec<_>>().join(",")
        } else {
            format_value(key, value, body)
        };
        out.push(' ');
        out.push_str(key);
        out.push('=');
        out.push_str(&escape(&value));
    }
    let mut rows = Vec::new();
    for (key, value) in children {
        let values: Vec<&Value> = match value {
            Value::Array(values) => values.iter().collect(),
            value => vec![value],
        };
        for value in values {
            let Some(row) = value.as_object() else {
                continue;
            };
            let name = element_name(key, row, overrides);
            let (row_attrs, _) = split(&name, row, false);
            let mut rendered = format!("  <{}", name);
            for (attr, value) in row_attrs {
                let value = if let Value::Array(values) = value {
                    values.iter().map(attr_value).collect::<Vec<_>>().join(",")
                } else {
                    format_value(attr, value, row)
                };
                rendered.push(' ');
                rendered.push_str(attr);
                rendered.push('=');
                rendered.push_str(&escape(&value));
            }
            rendered.push_str("/>");
            rows.push(rendered);
        }
    }
    if rows.is_empty() {
        out.push_str("/>\n");
    } else {
        out.push_str(">\n");
        out.push_str(&rows.join("\n"));
        out.push_str("\n</");
        out.push_str(root);
        out.push_str(">\n");
    }
    out.into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn escape_ascii() {
        assert_eq!(escape("a\"é\n"), "\"a&#34;&#233;&#xA;\"");
        // Text with nothing to escape takes the short way, and comes out the same.
        assert_eq!(escape("plain text 1080p"), "\"plain text 1080p\"");
        assert_eq!(escape("it's <a>&\u{7f}\u{1}"), "\"it&#39;s &lt;a&gt;&amp;??\"");
    }
    #[test]
    fn clip_extra_type_last() {
        let xml=String::from_utf8(to_xml(&serde_json::json!({"MediaContainer":{"Metadata":[{"type":"clip","ratingKey":"1","extraType":1}]}}),None)).unwrap();
        assert!(xml.contains("extraType=\"1\""));
    }
    #[test]
    fn system_writer_matches_measured_shape() {
        let xml = String::from_utf8(to_system_xml(
            &serde_json::json!({"MediaContainer":{"size":1,"Metadata":[{"type":"movie","ratingKey":"1","title":"A&B"}]}}),
            None,
        )).unwrap();
        assert_eq!(xml, "<?xml version='1.0' encoding='utf-8'?>\n<MediaContainer size=\"1\">\n  <Video ratingKey=\"1\" type=\"movie\" title=\"A&amp;B\"/>\n</MediaContainer>\n");
    }
}

#[cfg(test)]
mod ordered_json_tests {
    use super::*;
    use serde_json::json;

    fn old(document: &Value) -> Vec<u8> {
        fn strip(value: &mut Value) {
            match value {
                Value::Object(object) => {
                    crate::catalog::unpack_part(object);
                    object.retain(|key, _| !key.starts_with('_'));
                    object.values_mut().for_each(strip);
                }
                Value::Array(values) => values.iter_mut().for_each(strip),
                _ => {}
            }
        }
        let mut ordered = into_ordered(document.clone(), None);
        strip(&mut ordered);
        serde_json::to_vec(&ordered).unwrap()
    }

    /// Every row of the UI suite's catalog, its streams packed as the catalog holds them,
    /// listed: the one-pass writer answers the bytes the ordered copy did.
    #[test]
    fn writing_in_order_answers_what_the_ordered_copy_did() {
        let catalog: Value = serde_json::from_str(include_str!("../../tests/catalog_ui.json")).unwrap();
        let mut rows = catalog["items"].as_array().unwrap().clone();
        for row in rows.iter_mut() {
            crate::catalog::pack_streams(row);
        }
        rows[0]["allowSync"] = json!(true);
        rows[1]["Extra"] = json!([{"a": 1, "_b": 2}, 3, null]);
        rows[2]["Empty"] = json!([]);
        rows[3]["Nothing"] = Value::Null;
        let listing = json!({"MediaContainer": {"title1": "x", "size": rows.len(), "offset": 0, "totalSize": 9, "Metadata": rows.clone()}});
        assert_eq!(write_ordered_json(&listing, None).unwrap(), old(&listing));
        let paged = json!({"MediaContainer": {"noHistory": true, "replaceParent": false, "size": 2, "Directory": rows[..2].to_vec()}});
        assert_eq!(write_ordered_json(&paged, None).unwrap(), old(&paged));
        let hub = json!({"MediaContainer": {"size": 1, "Hub": [{"hubIdentifier": "h", "Metadata": rows[..5].to_vec()}]}});
        assert_eq!(write_ordered_json(&hub, None).unwrap(), old(&hub));
    }
}
