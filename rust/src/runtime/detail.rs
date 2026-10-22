//! The blocks a DETAIL read adds when the client asks for them, ported from
//! `server.py`'s `show_detail`, `parent_detail` and the `includePreferences` half of
//! `apply_detail_options`.
//!
//! Every projection of a row (`detail_row`, the grid, the hubs) is the plain row; only
//! `/library/metadata/{id}` itself adds `Extras`, `OnDeck` and `Preferences`, and only
//! under `includeExtras`, `includeOnDeck` and `includePreferences=1`.

use super::*;

use super::showprefs::unmatched_or_none_guid;

impl Runtime {
    pub(super) fn apply_detail_blocks(
        &self,
        row: &Value,
        detail: &mut Value,
        query: &HashMap<String, String>,
    ) {
        let include = |name: &str| query.get(name).is_some_and(|value| is_true_option(value));
        mediainfo::project_chapters(row, detail, include("includeChapters"));
        self.project_analysis(row, detail, include("includeMarkers"));
        let kind = row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let key = key_of(row).unwrap_or_default();
        let Some(object) = detail.as_object_mut() else {
            return;
        };
        if kind == "show" {
            // `show_detail`: the stored columns win; these fill only what is absent.
            // Read from the hot fields, as no episode needs reading whole for these.
            let leaves = self.leaves_refs(&key);
            object.entry("index".to_string()).or_insert(json!(1));
            let durations = leaves
                .iter()
                .filter_map(|leaf| leaf.get("duration").and_then(Value::as_i64))
                .filter(|duration| *duration != 0)
                .collect::<Vec<_>>();
            if !durations.is_empty() && object.get("duration").is_none() {
                // A show's duration is its typical episode length.
                let average = durations.iter().sum::<i64>() / durations.len() as i64;
                object.insert("duration".to_string(), json!(average));
            }
            if object.get("originallyAvailableAt").is_none() {
                if let Some(earliest) = leaves
                    .iter()
                    .filter_map(|leaf| leaf.get("originallyAvailableAt").and_then(Value::as_str))
                    .filter(|date| !date.is_empty())
                    .min()
                {
                    object.insert("originallyAvailableAt".to_string(), json!(earliest));
                }
            }
            // Measured on plexscratch42 (1.42.2): a `local://` show's detail has no
            // `Guid` key; the one-entry list is a MATCHED row's.
            if object.get("Guid").is_none() && !unmatched_guid(row) {
                if let Some(guid) = row
                    .get("guid")
                    .filter(|guid| !string_value(guid).is_empty())
                {
                    object.insert("Guid".to_string(), json!([{"id": guid}]));
                }
            }
        }
        if include("includeExtras") {
            // Measured: a movie, an episode, a show and a season all carry the block --
            // every clip, uncapped, in index order.
            object.insert("Extras".to_string(), self.extras_block(&key));
        }
        if matches!(kind.as_str(), "show" | "season") && include("includeOnDeck") {
            // Cycle 50: On Deck is selected once for the SHOW; a season exposes that
            // same row only when it owns the selected episode.  Cycle 57: a PRISTINE
            // show (no view state at all) carries the first episode the library
            // holds, specials last.
            let show_key = if kind == "season" {
                row.get("parentRatingKey")
                    .map(string_value)
                    .unwrap_or_default()
            } else {
                key.clone()
            };
            let mut pick = self.continue_watching(false).into_iter().find(|candidate| {
                candidate.get("type").and_then(Value::as_str) == Some("episode")
                    && candidate
                        .get("grandparentRatingKey")
                        .map(string_value)
                        .as_deref()
                        == Some(show_key.as_str())
            });
            if pick.is_none() {
                // Borrowed, every field read hot: copying each episode to pick one read a
                // 1,100-episode show's rows from disk on every preplay.
                let leaves = self
                    .leaves_refs(&show_key)
                    .into_iter()
                    .filter(|leaf| leaf.get("type").and_then(Value::as_str) == Some("episode"))
                    .collect::<Vec<_>>();
                let number = |leaf: &RowRef<'_>, name: &str| {
                    leaf.get(name)
                        .and_then(|value| {
                            value.as_i64().or_else(|| string_value(value).parse().ok())
                        })
                        .unwrap_or(0)
                };
                let untouched = !leaves
                    .iter()
                    .any(|leaf| number(leaf, "viewCount") != 0 || number(leaf, "viewOffset") != 0);
                if untouched {
                    pick = leaves
                        .into_iter()
                        .min_by_key(|leaf| {
                            let season = number(leaf, "parentIndex");
                            (season == 0, season, number(leaf, "index"))
                        })
                        .map(|leaf| leaf.body().clone());
                }
            }
            if let Some(chosen) = pick {
                let owner = chosen
                    .get("parentRatingKey")
                    .or_else(|| chosen.get("_parentRatingKey"))
                    .map(string_value)
                    .unwrap_or_default();
                if kind != "season" || owner == key {
                    let mut projected = self.detail_row(&chosen);
                    // Cycle 50: with includeExtras=1 both PMS versions add the episode's
                    // own Extras block to the inline row too.
                    if include("includeExtras") {
                        let extras = self.extras_block(&key_of(&chosen).unwrap_or_default());
                        if let Some(inner) = projected.as_object_mut() {
                            inner.insert("Extras".to_string(), extras);
                        }
                    }
                    object.insert("OnDeck".to_string(), json!({"Metadata": projected}));
                }
            }
        }
        if query.get("includePreferences").map(String::as_str) == Some("1")
            && matches!(kind.as_str(), "movie" | "show" | "season")
        {
            let mut settings = self.item_preferences(&kind, row);
            // Cycle 16: an UNMATCHED item loses its AGENT-driven settings.  A show
            // drops `languageOverride` and `useOriginalTitle`; a MOVIE loses the whole
            // block; a season keeps its three.
            if unmatched_or_none_guid(row) {
                if kind == "movie" {
                    settings.clear();
                } else if kind == "show" {
                    settings.retain(|setting| {
                        !matches!(
                            setting.get("id").and_then(Value::as_str),
                            Some("languageOverride" | "useOriginalTitle")
                        )
                    });
                }
            }
            if !settings.is_empty() {
                object.insert("Preferences".to_string(), json!({"Setting": settings}));
            }
        }
    }
}
