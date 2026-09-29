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
    let mut out = String::from("\"");
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
            c if c as u32 >= 0x80 => out.push_str(&format!("&#{};", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
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
fn attr_order(name: &str, row: &Map<String, Value>, key: &str) -> usize {
    if row.get("_session").is_some() {
        return match key {
            "selected" => 1,
            "decision" => 2,
            _ => 0,
        };
    }
    if row.get("_pluginLibrary").is_some() {
        return match key {
            "art" => 0,
            "thumb" => 1,
            "key" => 2,
            "title" => 3,
            "share" => 4,
            "identifier" => 5,
            _ => 6,
        };
    }
    let bucket = if name == "Video" && row.get("_history1").is_some() {
        "Video[history1]".to_string()
    } else if name == "Video" && row.get("_history").is_some() {
        "Video[history]".to_string()
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
    let order: &[&str] = match bucket.as_str() {
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
            "audienceRatingImage",
            "episodeSort",
            "flattenSeasons",
        ],
        "Field" => &["locked", "name"],
        "Agent" => &["identifier", "name", "hasPrefs", "id", "primary"],
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
        "Stream" => &[
            "id",
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
            "audienceRatingImage",
            "chapterSource",
            "primaryExtraKey",
            "useOriginalTitle",
            "ratingImage",
        ],
        "Writer" => &["id", "filter", "tag", "tagKey", "thumb"],
        _ => &[],
    };
    order
        .iter()
        .position(|value| *value == key)
        .unwrap_or(usize::MAX)
}
fn split(name: &str, row: &Map<String, Value>, container: bool) -> (Vec<String>, Vec<String>) {
    let mut attrs = Vec::new();
    let mut children = Vec::new();
    for (k, v) in row {
        if k.starts_with('_') || v.is_null() {
            continue;
        }
        match v {
            Value::Object(_) => children.push(k.clone()),
            Value::Array(values) => {
                if !values.is_empty() && values.iter().all(Value::is_object) {
                    children.push(k.clone())
                } else if !values.is_empty() {
                    attrs.push(k.clone())
                }
            }
            _ => attrs.push(k.clone()),
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
        attrs.sort_by_key(|k| (lead(k), k.clone()));
        children.sort_by_key(|k| if k == "Meta" { 0 } else { 1 });
    } else {
        // Row ordering is a stable topological order. Unknown fields retain the
        // insertion order supplied by the measured JSON document; alphabetising
        // the tie changes the byte stream for DOVI and other agent extensions.
        if name == "Player" {
            attrs.sort_by_key(|key| {
                (
                    if row.get(key).and_then(Value::as_str).is_some() {
                        0
                    } else {
                        1
                    },
                    key.clone(),
                )
            });
        } else if row.get("_session").is_some() || row.get("_pluginLibrary").is_some() {
            attrs.sort_by_key(|k| (attr_order(name, row, k), k.clone()));
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
            attrs.sort_by_key(|k| (lead(k), attr_order(name, row, k)));
        } else {
            attrs.sort_by_key(|k| attr_order(name, row, k));
        }
        // A STABLE sort, as Python's: unknown children keep their order.
        children.sort_by_key(|k| child_rank(name, k, row));
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
    for key in &attrs {
        let value = match row.get(key).unwrap() {
            Value::Array(values) => values.iter().map(attr_value).collect::<Vec<_>>().join(","),
            value => format_value(key, value, row),
        };
        out.push(' ');
        out.push_str(key);
        out.push('=');
        out.push_str(&escape(&value));
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
    for key in children {
        if let Some(value) = row.get(&key) {
            match value {
                Value::Object(child) => {
                    let child_name = element_name(&key, child, overrides);
                    render(&child_name, child, out, None, false)
                }
                Value::Array(values) => {
                    for child in values {
                        if let Some(child) = child.as_object() {
                            let child_name = element_name(&key, child, overrides);
                            render(&child_name, child, out, None, false);
                        }
                    }
                }
                _ => {}
            }
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
    fn walk(
        key: &str,
        row: &Map<String, Value>,
        overrides: Option<&HashMap<String, String>>,
        container: bool,
    ) -> Value {
        let name = element_name(key, row, overrides);
        let (attrs, children) = split(&name, row, container);
        let mut out = Map::new();
        for k in attrs {
            out.insert(k.clone(), row.get(&k).cloned().unwrap_or(Value::Null));
        }
        for k in children {
            if let Some(value) = row.get(&k) {
                match value {
                    Value::Object(child) => {
                        out.insert(k.clone(), walk(&k, child, overrides, false));
                    }
                    Value::Array(values) => {
                        out.insert(
                            k.clone(),
                            Value::Array(
                                values
                                    .iter()
                                    .filter_map(|v| {
                                        v.as_object().map(|child| walk(&k, child, overrides, false))
                                    })
                                    .collect(),
                            ),
                        );
                    }
                    _ => {}
                }
            }
        }
        Value::Object(out)
    }
    let Some(root) = container.as_object().and_then(|m| m.iter().next()) else {
        return container.clone();
    };
    let (root_name, body) = root;
    if let Some(body) = body.as_object() {
        let mut out = Map::new();
        out.insert(
            root_name.clone(),
            walk(
                root_name,
                body,
                overrides,
                root_name.ends_with("Container")
                    || root_name == "ButlerTasks"
                    || root_name == "Query",
            ),
        );
        Value::Object(out)
    } else {
        container.clone()
    }
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
    for key in attrs {
        let value = body.get(&key).unwrap_or(&Value::Null);
        let value = if let Value::Array(values) = value {
            values.iter().map(attr_value).collect::<Vec<_>>().join(",")
        } else {
            format_value(&key, value, body)
        };
        out.push(' ');
        out.push_str(&key);
        out.push('=');
        out.push_str(&escape(&value));
    }
    let mut rows = Vec::new();
    for key in children {
        let Some(value) = body.get(&key) else {
            continue;
        };
        let values: Vec<&Value> = match value {
            Value::Array(values) => values.iter().collect(),
            value => vec![value],
        };
        for value in values {
            let Some(row) = value.as_object() else {
                continue;
            };
            let name = element_name(&key, row, overrides);
            let (row_attrs, _) = split(&name, row, false);
            let mut rendered = format!("  <{}", name);
            for attr in row_attrs {
                let value = row.get(&attr).unwrap_or(&Value::Null);
                let value = if let Value::Array(values) = value {
                    values.iter().map(attr_value).collect::<Vec<_>>().join(",")
                } else {
                    format_value(&attr, value, row)
                };
                rendered.push(' ');
                rendered.push_str(&attr);
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
