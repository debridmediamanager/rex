//! The envelope refusals `server.py`'s handler makes before a request reaches its
//! route: the read-only verb gates (cycles 15-31) and the `limit=`/`parent=` grammar
//! (cycle 15, 45, 55).

use super::*;
use super::resume::int_param;

/// Routes that answer 404 to every verb but GET and HEAD.
const READ_ONLY_ROUTES: &[&str] = &[
    "/", "/library/recentlyAdded", "/library/onDeck", "/library/tags", "/library/matches",
    "/playlists/all", "/hubs/promoted", "/hubs/continueWatching", "/hubs/search",
    "/status/sessions/history/all", "/:/plugins", "/channels/all", "/services/browse",
    "/updater/status",
    // Cycle 31, swept over OPTIONS, POST, PUT and DELETE on plexscratch42 and
    // plexscratch, which agree on all 44 answers.
    "/statistics", "/statistics/bandwidth", "/statistics/media", "/status/sessions",
    "/clients/profiles", "/playlists/sorts", "/library/sections/prefs",
];
/// Cycle 31: the three whose POST is a real refusal of its own (400).
const READ_ONLY_EXCEPT_POST: &[&str] = &["/library/sections", "/media/providers", "/media/providers/metadata"];
/// Item leaves that are pure reads.
const READ_ONLY_ITEM_LEAVES: &[&str] = &["allLeaves", "tree", "related", "similar", "matches", "grandchildren"];
/// Cycle 17/18: an item ACTION leaf refuses POST with a 404.
const ITEM_ACTION_LEAVES: &[&str] = &["refresh", "analyze", "unmatch", "split", "merge"];
/// The section leaves that parse `limit=` (cycle 15).
const PARSES_LIMIT: &[&str] = &[
    "all", "allLeaves", "search", "common", "genre", "year", "decade", "contentRating",
    "studio", "director", "writer", "producer", "country", "actor", "label", "collection",
    "resolution", "firstCharacter", "userRating", "subtitleLanguage", "audioLanguage", "hdr",
    "unwatched", "duplicate", "editionTitle", "unmatched",
];

fn parts_of(route: &str) -> Vec<&str> {
    route.split('/').filter(|piece| !piece.is_empty()).collect()
}

fn is_digits(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit())
}

/// 404 for a verb the collection item paths do not implement (cycle 17).
fn collection_item_refusal(method: &str, parts: &[&str]) -> bool {
    if parts.len() < 4 || parts[..2] != ["library", "collections"] || parts[3] != "items" {
        return false;
    }
    match parts.len() {
        4 => !matches!(method, "GET" | "HEAD" | "PUT"),
        5 => method != "DELETE",
        6 if parts[5] == "move" => method != "PUT",
        _ => false,
    }
}

/// 404 for a verb a playlist path does not take (cycle 18).
fn playlist_item_refusal(method: &str, parts: &[&str]) -> bool {
    if parts.len() < 2 || parts[0] != "playlists" || !is_digits(parts[1]) {
        return false;
    }
    if parts.len() == 2 {
        return !matches!(method, "GET" | "HEAD" | "PUT" | "DELETE");
    }
    if parts[2] != "items" {
        return false;
    }
    match parts.len() {
        3 => !matches!(method, "GET" | "HEAD" | "PUT" | "DELETE"),
        4 => !matches!(method, "DELETE" | "PUT"),
        5 if parts[4] == "move" => method != "PUT",
        _ => false,
    }
}

/// 404 for a write to a route that only reads (cycle 15 onward).
pub(super) fn read_only_refusal(method: &str, route: &str) -> bool {
    let route = if route.len() > 1 { route.trim_end_matches('/') } else { route };
    let route = if route.is_empty() { "/" } else { route };
    let parts = parts_of(route);
    if collection_item_refusal(method, &parts) || playlist_item_refusal(method, &parts) {
        return true;
    }
    if parts.len() == 4
        && parts[..2] == ["library", "metadata"]
        && parts[3] == "subtitles"
        && !matches!(method, "GET" | "HEAD" | "POST" | "PUT")
    {
        return true;
    }
    if method == "POST"
        && parts.len() == 4
        && parts[..2] == ["library", "metadata"]
        && ITEM_ACTION_LEAVES.contains(&parts[3])
    {
        return true;
    }
    if matches!(method, "GET" | "HEAD") {
        return false;
    }
    if READ_ONLY_ROUTES.contains(&route) {
        return true;
    }
    // Cycle 23, measured on plexscratch over 138 path templates x 7 verbs: three
    // more read-only routes answer 404 to every verb but GET and HEAD.
    if route == "/hubs/home/recentlyAdded"
        || (parts.len() == 3 && parts[..2] == ["hubs", "sections"])
        || (parts.len() == 4 && parts[..2] == ["library", "metadata"] && parts[3] == "children")
    {
        return true;
    }
    if READ_ONLY_EXCEPT_POST.contains(&route) && method != "POST" {
        return true;
    }
    if parts.len() == 4 && parts[..2] == ["library", "metadata"] && READ_ONLY_ITEM_LEAVES.contains(&parts[3]) {
        return true;
    }
    // Cycle 29: `/extras/all` is five segments, and read-only like the rest.
    if parts.len() == 5
        && parts[3] == "extras"
        && parts[4] == "all"
        && (parts[..2] == ["library", "metadata"] || parts[..2] == ["library", "sections"])
    {
        return true;
    }
    if parts.len() == 4
        && parts[..2] == ["library", "metadata"]
        && matches!(parts[3], "extras" | "themes" | "clearLogos" | "posters" | "arts")
        && method != "POST"
    {
        return true;
    }
    false
}

/// Cycle 55: a number-column value as 1.42.2 reads it -- `float()` after a
/// surrounding space comes off.
pub(super) fn number_param(value: &str) -> bool {
    let text = value.trim();
    !text.is_empty() && text.parse::<f64>().is_ok()
}

/// The status a malformed `limit=`/`parent=` gets on a GET, or 0 (cycle 15).
pub(super) fn query_grammar_refusal(method: &str, route: &str, query: &HashMap<String, String>) -> u16 {
    if !matches!(method, "GET" | "HEAD") {
        return 0;
    }
    let bad_limit = query.get("limit").is_some_and(|value| !int_param(value, None));
    let bad_parent = query.get("parent").is_some_and(|value| !number_param(value));
    if !bad_limit && !bad_parent {
        return 0;
    }
    let route = if route.len() > 1 { route.trim_end_matches('/') } else { route };
    if route == "/status/sessions/history/all" {
        return if bad_limit { 400 } else { 0 };
    }
    let parts = parts_of(route);
    if parts.len() == 4 && parts[..2] == ["library", "sections"] && PARSES_LIMIT.contains(&parts[3]) {
        return 500;
    }
    if matches!(route, "/library/all" | "/playlists" | "/library/arts") {
        return 500;
    }
    if parts.len() == 4 && parts[..2] == ["library", "metadata"] && matches!(parts[3], "children" | "grandchildren") {
        return 500;
    }
    0
}
