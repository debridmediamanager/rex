//! An item's related shelves and `/hubs/metadata/{id}/{related,postplay}`, ported
//! from `server.py`'s `similar_rows`, `collection_related_hub`, `related_hubs` and
//! `item_hubs`.

use super::*;

/// Cycle 55: the Related list is capped at 200 rows.
const SIMILAR_CAP: usize = 200;

fn tag_names(tags: Option<&Value>, limit: Option<usize>) -> HashSet<String> {
    tags.and_then(Value::as_array)
        .into_iter()
        .flatten()
        .take(limit.unwrap_or(usize::MAX))
        .filter_map(|tag| tag.get("tag").map(string_value))
        .filter(|name| !name.is_empty())
        .map(|name| name.to_lowercase())
        .collect()
}

impl Runtime {
    /// Rows a client should offer as "Related".  Cycle 55 REFUTED "the reference
    /// fills this from its metadata agent": the binary counts the item's FIRST THREE
    /// genres and FIRST FOUR roles against each candidate's tags, a candidate needs AT
    /// LEAST TWO in common, and the list is shared-count descending then release date
    /// descending, capped at 200 -- which reproduces zen's movie 19770 exactly.
    pub(super) fn similar_rows(&self, row: &Value) -> Vec<Value> {
        let genres = tag_names(row.get("Genre"), Some(3));
        let roles = tag_names(row.get("Role"), Some(4));
        if genres.is_empty() && roles.is_empty() {
            return Vec::new();
        }
        let key = key_of(row).unwrap_or_default();
        let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
        // The candidates are the library's own TOP-LEVEL rows (`section_items`), so an
        // episode or a season never has a Related shelf.
        let candidates = self.section_rows(&section);
        let mut scored = candidates
            .iter()
            .filter(|other| {
                key_of(other).unwrap_or_default() != key && other.get("type") == row.get("type")
            })
            .filter_map(|other| {
                let shared = genres.intersection(&tag_names(other.get("Genre"), None)).count()
                    + roles.intersection(&tag_names(other.get("Role"), None)).count();
                (shared >= 2).then(|| {
                    let released = other.get("originallyAvailableAt").map(string_value).unwrap_or_default();
                    (shared, released, other.clone())
                })
            })
            .collect::<Vec<_>>();
        scored.sort_by(|left, right| {
            right
                .0
                .cmp(&left.0)
                .then_with(|| left.1.is_empty().cmp(&right.1.is_empty()))
                .then_with(|| right.1.cmp(&left.1))
        });
        scored.into_iter().take(SIMILAR_CAP).map(|(_, _, row)| row).collect()
    }

    /// Cycle 57, measured on plexscratch42 and plexscratch: the
    /// `collection.related.{section}.1` shelf of a collection member.  It exists only
    /// while the collection has TWO members or more, and while it exists it REPLACES
    /// `movie.similar`.  An item in two such collections answers the earlier-created
    /// collection's; a smart collection contributes no shelf.
    fn collection_related_hub(&self, row: &Value, query: &HashMap<String, String>) -> Option<Value> {
        let key = key_of(row)?;
        let mut collections = self.collections.values().collect::<Vec<_>>();
        collections.sort_by_key(|collection| collection.id.parse::<i64>().unwrap_or(i64::MAX));
        for collection in collections {
            if collection.smart || !collection.items.iter().any(|member| *member == key) {
                continue;
            }
            let mut rows = collection
                .items
                .iter()
                .filter_map(|member| self.catalog.get(member).cloned())
                .collect::<Vec<_>>();
            if rows.len() < 2 {
                continue;
            }
            // Release date, then year with nulls last (3, 1, 4 for 2006, 2008, 2010).
            rows.sort_by_key(|row| {
                let released = row.get("originallyAvailableAt").map(string_value).unwrap_or_default();
                let year = row.get("year").and_then(Value::as_i64);
                (
                    !released.is_empty(),
                    released,
                    year.is_none(),
                    year.unwrap_or(0),
                    string_value(row.get("titleSort").or_else(|| row.get("title")).unwrap_or(&Value::Null))
                        .to_lowercase(),
                )
            });
            let kind = if collection.subtype.is_empty() { "movie" } else { collection.subtype.as_str() };
            let type_number = match kind {
                "show" => 2,
                "season" => 3,
                "episode" => 4,
                _ => 1,
            };
            let tag = tag_id(
                "Collection",
                if collection.tag.is_empty() { &collection.title } else { &collection.tag },
            );
            let section = &collection.section;
            let mut built = self.hub(
                &format!("collection.related.{section}.1"),
                &format!("{} Collection", collection.title),
                &rows,
                query.get("count"),
                kind,
                true,
                vec![
                    (
                        "key",
                        json!(format!(
                            "/library/sections/{section}/all?type={type_number}&tagId={tag}&sort=originallyAvailableAt,year:nullsLast"
                        )),
                    ),
                    ("context", json!("hub.collection.related")),
                ],
            );
            let keys = built
                .get("Metadata")
                .and_then(Value::as_array)
                .map(|rows| rows.iter().filter_map(key_of).collect::<Vec<_>>())
                .unwrap_or_default();
            built.insert("hubKey".to_string(), json!(format!("/library/metadata/{}", keys.join(","))));
            return Some(Value::Object(built));
        }
        None
    }

    /// An item's related shelves, shared by every endpoint that carries them: the
    /// collection shelf when there is one, else the FULL-row `movie.similar` (or
    /// `show.similar`) hub.
    pub(super) fn related_hubs(&self, row: &Value, query: &HashMap<String, String>, collections: bool) -> Vec<Value> {
        if collections {
            if let Some(shelf) = self.collection_related_hub(row, query) {
                return vec![shelf];
            }
        }
        let rows = self.similar_rows(row);
        if rows.is_empty() {
            return Vec::new();
        }
        let kind = match row.get("type").and_then(Value::as_str) {
            Some("show") => "show",
            _ => "movie",
        };
        let key = key_of(row).unwrap_or_default();
        let identifier = format!("{kind}.similar");
        let mut built = self.hub(
            &identifier,
            if kind == "show" { "Related Shows" } else { "Related Movies" },
            &rows,
            query.get("count"),
            kind,
            true,
            vec![
                ("key", json!(format!("/library/metadata/{key}/similar"))),
                ("context", json!(format!("hub.{identifier}"))),
                ("style", json!("shelf")),
            ],
        );
        let keys = built
            .get("Metadata")
            .and_then(Value::as_array)
            .map(|rows| rows.iter().filter_map(key_of).collect::<Vec<_>>())
            .unwrap_or_default();
        if keys.is_empty() {
            built.remove("hubKey");
        } else {
            built.insert("hubKey".to_string(), json!(format!("/library/metadata/{}", keys.join(","))));
        }
        vec![Value::Object(built)]
    }

    /// `/hubs/metadata/{id}/{related,postplay}` -- the preplay page's shelves.
    pub(super) fn route_item_hubs(&self, rating_key: &str, leaf: &str, query: &HashMap<String, String>) -> Response {
        if leaf != "related" && leaf != "postplay" {
            return Response::error(404, "plexsim: not found");
        }
        let row = if !rating_key.is_empty() && rating_key.bytes().all(|b| b.is_ascii_digit()) {
            let trimmed = rating_key.trim_start_matches('0');
            self.catalog.get(if trimmed.is_empty() { "0" } else { trimmed })
        } else {
            None
        };
        let Some(row) = row else {
            // Cycle 36, measured on kids and zen: the `related` leaf never resolves
            // the row -- an unknown id is an empty hub container -- where `postplay`
            // on the same id is a 404.
            if leaf == "related" {
                return Response::json(200, self.hub_container(Map::new()));
            }
            return Response::error(404, "plexsim: no such item");
        };
        let mut hubs = self.related_hubs(row, query, leaf == "related");
        if leaf == "related" {
            for hub in hubs.iter_mut() {
                if let Some(rows) = hub.get_mut("Metadata").and_then(Value::as_array_mut) {
                    self.expand_full_rows(rows, query);
                }
            }
        }
        if leaf == "postplay" {
            // Cycle 70, measured on plexscratch42 and plexscratch: an EPISODE's
            // post-play carries a `tv.upnext` hub holding its show's On Deck row
            // whenever that row comes from view state and is not the episode itself.
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                let show = row.get("grandparentRatingKey").map(string_value);
                let pick = self.continue_watching(false).into_iter().find(|candidate| {
                    candidate.get("type").and_then(Value::as_str) == Some("episode")
                        && candidate.get("grandparentRatingKey").map(string_value) == show
                });
                if let Some(pick) = pick.filter(|pick| key_of(pick) != key_of(row)) {
                    let pick_key = key_of(&pick).unwrap_or_default();
                    hubs.insert(
                        0,
                        json!({
                            "hubKey": format!("/library/metadata/{pick_key}"),
                            "upNext": true,
                            "title": "Up Next",
                            "type": "episode",
                            "hubIdentifier": "tv.upnext",
                            "context": "hub.tv.upnext",
                            "size": 1,
                            "more": false,
                            "Metadata": [self.detail_row(&pick)],
                        }),
                    );
                }
            }
            // Cycle 14: postplay's container says `allowSync` FALSE.
            return Response::json(
                200,
                self.hub_container(Map::from_iter([
                    ("Hub".to_string(), Value::Array(hubs)),
                    ("allowSync".to_string(), json!(false)),
                ])),
            );
        }
        let section_key = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
        let section = self.section(&section_key);
        let mut fields = Map::from_iter([("Hub".to_string(), Value::Array(hubs))]);
        for (name, value) in [
            ("librarySectionID", row.get("librarySectionID").cloned()),
            ("librarySectionTitle", section.and_then(|s| s.get("title")).cloned()),
            ("librarySectionUUID", section.and_then(|s| s.get("uuid")).cloned()),
        ] {
            if let Some(value) = value.filter(|value| !value.is_null()) {
                fields.insert(name.to_string(), value);
            }
        }
        Response::json(200, self.hub_container(fields))
    }

    /// The inline `includeRelated` block: the item's related hubs as `{"Hub": [...]}`,
    /// an empty OBJECT for an item with none.  Cycle 15: it caps at FIVE rows per hub
    /// and says `more`; `includeRelatedCount=N` sets that cap and (cycle 22) `=0`
    /// EMPTIES the hub.
    pub(super) fn related_block(&self, row: &Value, query: &HashMap<String, String>) -> Value {
        let cap = query
            .get("includeRelatedCount")
            .filter(|value| !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(5);
        let trimmed = self
            .related_hubs(row, query, true)
            .into_iter()
            .map(|mut hub| {
                let rows = hub.get("Metadata").and_then(Value::as_array).cloned().unwrap_or_default();
                let more = rows.len() > cap || hub.get("more").and_then(Value::as_bool) == Some(true);
                let kept = rows.into_iter().take(cap).collect::<Vec<_>>();
                hub["size"] = json!(kept.len());
                hub["more"] = json!(more);
                hub["Metadata"] = Value::Array(kept);
                hub
            })
            .collect::<Vec<_>>();
        if trimmed.is_empty() {
            json!({})
        } else {
            json!({"Hub": trimmed})
        }
    }

    /// Cycle 55, measured on zen and kids: the `include*` blocks reach the FULL rows of
    /// every hub and related list -- `/similar?includeExtras=1` carries `Extras` on each
    /// row and `/related?includeRelated=1` a `Related` block inside its hub.  The
    /// options are handed on one at a time, so a nested Related block is not expanded
    /// again (the oracle's `expand_full_rows`).
    pub(super) fn expand_full_rows(&self, rows: &mut [Value], query: &HashMap<String, String>) {
        let include_int = |name: &str| {
            query.get(name).is_some_and(|value| {
                int_param(value, Some(32)) && value.trim_start_matches('+').parse::<i64>().unwrap_or(0) != 0
            })
        };
        let related = include_int("includeRelated");
        let children = include_int("includeChildren");
        let mut blocks = HashMap::new();
        if include_int("includeExtras") {
            blocks.insert("includeExtras".to_string(), "1".to_string());
        }
        if include_int("includeOnDeck") {
            blocks.insert("includeOnDeck".to_string(), "1".to_string());
        }
        if query.get("includePreferences").map(String::as_str) == Some("1") {
            blocks.insert("includePreferences".to_string(), "1".to_string());
        }
        if !(related || children || !blocks.is_empty()) {
            return;
        }
        let mut related_query = HashMap::new();
        if let Some(count) = query.get("includeRelatedCount") {
            related_query.insert("includeRelatedCount".to_string(), count.clone());
        }
        for projected in rows.iter_mut() {
            let Some(key) = key_of(projected) else { continue };
            let Some(stored) = self.catalog.get(&key).cloned() else { continue };
            if !blocks.is_empty() {
                self.apply_detail_blocks(&stored, projected, &blocks);
            }
            if children {
                let inlined = self
                    .children(&key)
                    .iter()
                    .map(|child| {
                        let mut shown = self.detail_row(child);
                        if let Some(object) = shown.as_object_mut() {
                            object.retain(|name, _| !name.starts_with("librarySection"));
                        }
                        shown
                    })
                    .collect::<Vec<_>>();
                if !inlined.is_empty() {
                    projected["Children"] = json!({"size": inlined.len(), "Metadata": inlined});
                }
            }
            if related {
                projected["Related"] = self.related_block(&stored, &related_query);
            }
        }
    }
}
