//! Cycle 55: the listing grammar's structural parameters, refused where they are READ
//! (`server.py`'s `structural_refusal` and `_structural_parse_refusal`).

use super::*;
use super::resume::int_param;

/// Section leaves that PARSE `sort`, `group`, `sourceType` and `limit` and refuse a
/// bad one with a 500 -- measured on zen (1.42.2) and kids (1.43.4), identical -- while
/// answering exactly the same body to a GOOD one.  `userRating`, `rating`,
/// `location`, `categories`, `folder` and the subsets answer 200 to all four.
const STRUCTURAL_SECTION_LEAVES: &[&str] = &[
    "actor", "aperture", "audioLanguage", "autotag", "channel", "collection", "collections",
    "contentRating", "country", "decade", "director", "editionTitle", "exposure",
    "firstCharacter", "format", "genre", "iso", "label", "lens", "make", "model", "mood",
    "narrator", "network", "onDeck", "place", "platform", "producer", "publisher",
    "resolution", "search", "source", "sport", "studio", "style", "subformat",
    "subtitleLanguage", "tag", "team", "unwatched", "writer", "year",
];
/// The fields `group=` groups by (the oracle's `GROUPABLE`).
const GROUPABLE: &[&str] = &[
    "absoluteIndex", "addedAt", "audienceRating", "contentRating", "contentRatingAge",
    "createdAt", "decade", "editionTitle", "guid", "id", "index", "isAdult", "mediaCount",
    "originalTitle", "originallyAvailableAt", "rating", "ratingCount", "remote",
    "resourcesChangedAt", "studio", "summary", "tagline", "title", "titleSort", "type",
    "updatedAt", "year",
];
/// Every field the listing grammar types (the oracle's `FILTER_FIELD_TYPES` keys).
const FILTER_FIELDS: &[&str] = &[
    "absoluteIndex", "actor", "addedAt", "agent", "aperture", "audienceRating",
    "audioLanguage", "autotag", "beginsAt", "channel", "cluster", "clusterStartsAt",
    "clusterZoomLevel", "clustering", "collection", "contentRating", "contentRatingAge",
    "country", "createdAt", "decade", "device", "director", "dovi", "duplicate", "duration",
    "editionTitle", "endsAt", "exposure", "file", "format", "genre", "hdr", "id",
    "inProgress", "index", "isAdult", "iso", "label", "lastRatedAt", "lastSkippedAt",
    "lastViewedAt", "lens", "location", "make", "mediaAnalysisVersion",
    "mediaAudioChannels", "mediaAudioCodec", "mediaBitrate", "mediaContainer", "mediaCount",
    "mediaHeight", "mediaProcessingTarget", "mediaSize", "mediaVideoCodec", "mediaWidth",
    "model", "mood", "network", "originalTitle", "originallyAvailableAt", "parent", "place",
    "producer", "rating", "ratingCount", "remote", "resolution", "resourcesChangedAt",
    "sharedWith", "similar", "skipCount", "source", "studio", "style", "subformat",
    "subtitleLanguage", "summary", "tag", "tagId", "tagline", "title", "titleSort", "trash",
    "type", "unmatched", "unviewedLeafCount", "unwatched", "unwatchedLeaves", "updatedAt",
    "userRating", "viewCount", "viewOffset", "viewUpdatedAt", "viewedLeafCount",
    "viewedOrInProgressLeafCount", "writer", "year",
];
/// Cycle 55: these four fields, the refused filter fields and `leafCount` are 500s at
/// the parse on both references.
const UNGROUPABLE: &[&str] = &["hdr", "duplicate", "unmatched", "inProgress", "leafCount"];
const REFUSED_FILTER_FIELDS: &[&str] =
    &["parentIndex", "theme", "banner", "art", "thumb", "slug", "extraType", "userFields"];

#[derive(Clone, Copy)]
struct Parse {
    sort: bool,
    group: bool,
    source: bool,
    limit: bool,
}

const ALL: Parse = Parse { sort: true, group: true, source: true, limit: true };

impl Runtime {
    /// Cycle 55: a `group=` that PARSES is a known field name -- `title`, `genre`,
    /// `duration`, `viewCount`, `file`, `random`, a type-prefixed `zzz.title` --
    /// whether or not the listing could then group by it.
    fn group_parses(&self, field: &str) -> bool {
        let bare = field.split_once('.').map_or(field, |(_, bare)| bare);
        if bare.is_empty() || REFUSED_FILTER_FIELDS.contains(&bare) || UNGROUPABLE.contains(&bare) {
            return false;
        }
        GROUPABLE.contains(&bare)
            || FILTER_FIELDS.contains(&bare)
            || self.sort_rows(Vec::new(), Some(bare)).is_ok()
    }

    /// The refusal a malformed structural parameter gets where it is only PARSED.
    fn structural_parse_refusal(&self, query: &HashMap<String, String>, parse: Parse) -> Option<u16> {
        if parse.sort {
            if let Some(sort) = query.get("sort").filter(|sort| !sort.is_empty()) {
                if self.sort_rows(Vec::new(), Some(sort)).is_err() {
                    return Some(500);
                }
            }
        }
        if parse.group {
            if let Some(group) = query.get("group") {
                if !self.group_parses(group) {
                    return Some(500);
                }
            }
        }
        if parse.source && crate::filters::source_order(query.get("sourceType").map(String::as_str)).is_err() {
            return Some(500);
        }
        if parse.limit && query.get("limit").is_some_and(|limit| !int_param(limit, Some(32))) {
            return Some(500);
        }
        None
    }

    /// Cycle 55: the listing grammar's structural parameters, refused where READ.
    /// Runs for GET and HEAD only, as the oracle's route does.
    pub(super) fn structural_refusal(&self, path: &str, query: &HashMap<String, String>) -> Option<u16> {
        let segments = path.split('/').filter(|piece| !piece.is_empty()).collect::<Vec<_>>();
        let is_int = |name: &str, bits: Option<u32>| query.get(name).is_none_or(|value| int_param(value, bits));
        if segments.len() >= 3
            && segments[..2] == ["library", "sections"]
            && matches!(segments.len(), 4 | 5)
            && self.section(segments[2]).is_some()
        {
            let leaf = segments[3];
            if STRUCTURAL_SECTION_LEAVES.contains(&leaf) && (segments.len() == 4 || leaf == "firstCharacter") {
                if let Some(status) = self.structural_parse_refusal(query, ALL) {
                    return Some(status);
                }
            }
            if leaf == "autocomplete" && segments.len() == 4 {
                // ...but the autocomplete refuses its own `type` and `limit` with a 400.
                if !is_int("type", Some(32)) || !is_int("limit", Some(32)) {
                    return Some(400);
                }
                return self.structural_parse_refusal(query, Parse { limit: false, ..ALL });
            }
            if (leaf == "common" && segments.len() == 4) || segments[3..] == ["extras", "all"] {
                if let Some(status) = self.structural_parse_refusal(query, ALL) {
                    return Some(status);
                }
            }
            // Cycle 55: `index=` is PARSED by the listings that read the query, and a
            // show library's `/recentlyViewedShows` parses `limit=`.
            if ((matches!(leaf, "collections" | "search") && segments.len() == 4)
                || segments[3..] == ["extras", "all"])
                && !is_int("index", None)
            {
                return Some(500);
            }
            if leaf == "recentlyViewedShows" && segments.len() == 4 && !is_int("limit", Some(32)) {
                return Some(500);
            }
        }
        if segments == ["playlists"] && !is_int("index", None) {
            return Some(500);
        }
        if segments == ["playlists", "all"] && !is_int("limit", Some(32)) {
            return Some(500);
        }
        if segments == ["library", "all", "top"] && (query.contains_key("group") || query.contains_key("sort")) {
            // Cycle 55: Top Played refuses ANY `group=` or `sort=` with a 400, and an
            // item's `/users/top` refuses the same two with a 404.
            return Some(400);
        }
        if segments.len() == 5
            && segments[..2] == ["library", "metadata"]
            && segments[3..] == ["users", "top"]
            && (query.contains_key("group") || query.contains_key("sort"))
        {
            return Some(404);
        }
        let hub_page = segments == ["hubs"]
            || segments == ["hubs", "promoted"]
            || (segments.len() == 3
                && segments[..2] == ["hubs", "sections"]
                && self
                    .section(segments[2])
                    .and_then(|section| section.get("type").and_then(Value::as_str).map(str::to_string))
                    .as_deref()
                    == Some("show"));
        if hub_page {
            // A hub PAGE whose TV shelf runs the listing query parses `sourceType`, and
            // `/hubs/promoted` refuses a `group=`.  An EMPTY value is dropped first.
            if query.get("sourceType").is_some_and(|value| !value.is_empty()) {
                if let Some(status) = self.structural_parse_refusal(
                    query,
                    Parse { sort: false, group: false, source: true, limit: false },
                ) {
                    return Some(status);
                }
            }
            if segments == ["hubs", "promoted"] && query.get("group").is_some_and(|value| !value.is_empty()) {
                return Some(500);
            }
        }
        if segments == ["hubs", "home", "recentlyAdded"]
            && query.get("type").is_some_and(|value| {
                int_param(value, Some(32)) && value.trim_start_matches('+').parse::<i64>().ok() == Some(2)
            })
        {
            // ...and the TV Recently Added list parses both with the empty value.
            if query.contains_key("group") {
                return Some(500);
            }
            if let Some(status) = self.structural_parse_refusal(
                query,
                Parse { sort: false, group: false, source: true, limit: false },
            ) {
                return Some(status);
            }
        }
        if segments == ["library", "all", "top"] && !is_int("limit", Some(32)) {
            return Some(500);
        }
        if segments == ["library", "onDeck"] && !is_int("type", Some(32)) {
            return Some(400);
        }
        if segments.len() == 5
            && segments[..2] == ["library", "metadata"]
            && segments[3..] == ["users", "top"]
            && !is_int("limit", Some(32))
        {
            return Some(500);
        }
        if segments.len() == 4 && segments[..2] == ["library", "metadata"] && segments[3] == "nearest" {
            if let Some(limit) = query.get("limit") {
                // Cycle 55: `limit=abc` is a 400 and `limit=-1` a 500.
                if !int_param(limit, Some(32)) {
                    return Some(400);
                }
                if limit.starts_with('-') && limit.trim_start_matches(['-', '0']) != "" {
                    return Some(500);
                }
            }
        }
        if segments.len() == 4
            && segments[..2] == ["library", "metadata"]
            && matches!(segments[3], "children" | "grandchildren")
            && !segments[2].is_empty()
            && segments[2].bytes().all(|b| b.is_ascii_digit())
        {
            let key = segments[2].trim_start_matches('0');
            let kind = self
                .catalog
                .get(if key.is_empty() { "0" } else { key })
                .and_then(|row| row.get("type").and_then(Value::as_str).map(str::to_string));
            if matches!(kind.as_deref(), Some("show" | "season")) {
                if segments[3] == "grandchildren" && !is_int("type", Some(32)) {
                    return Some(500);
                }
                return self.structural_parse_refusal(query, ALL);
            }
        }
        if segments.len() == 4 && segments[..2] == ["library", "people"] && segments[3] == "media" {
            return self.structural_parse_refusal(query, ALL);
        }
        self.parameter_refusal(&segments, query)
    }

    /// Cycle 54/55, from the binary's parameter names and measured on kids, zen and
    /// plexscratch42: parameters that are PARSED -- a value the grammar refuses is a
    /// 400 -- on paths whose handlers otherwise ignore them.
    fn parameter_refusal(&self, segments: &[&str], query: &HashMap<String, String>) -> Option<u16> {
        let bad_int32 = |name: &str| query.get(name).is_some_and(|value| !int_param(value, Some(32)));
        let item_exists = |key: &str| self.catalog.get(key).is_some();
        // `augmentCount` on an item is an int32 -- after the row is looked up, since an
        // unknown id is still a 404 -- and so is a Related list's `count`.
        if segments.len() == 3 && segments[..2] == ["library", "metadata"] && item_exists(segments[2]) {
            if bad_int32("augmentCount") {
                return Some(400);
            }
        }
        if segments.len() == 4
            && segments[..2] == ["library", "metadata"]
            && segments[3] == "similar"
            && item_exists(segments[2])
            && bad_int32("count")
        {
            return Some(400);
        }
        // The legacy `/search`: `count` is an int32.
        if segments == ["search"] && bad_int32("count") {
            return Some(400);
        }
        if segments == ["hubs", "search"] {
            // Cycle 55: an id that PARSES as a signed 32-bit integer and names no
            // section is refused; what does not parse (`abc`, `-1`, `1.5`, ` 3`,
            // `2147483648`) is ignored.
            if let Some(wanted) = query.get("sectionId") {
                if int_param(wanted, Some(32)) {
                    let number = wanted.trim_start_matches('+').parse::<i64>().unwrap_or(0);
                    if number >= 0 && self.section(&number.to_string()).is_none() {
                        return Some(400);
                    }
                }
            }
        }
        None
    }
}
