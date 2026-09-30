//! The `/children` and `/allLeaves` CONTAINER, ported from `server.py`'s
//! `child_container`.
//!
//! The parent's own identity is repeated on the container, not just on the rows --
//! `parentTitle`, `parentIndex`, `parentYear`, `thumb`, `art`, `viewMode` and, for an
//! episode list, the whole `grandparent*` set.  The desktop client rendered an empty
//! season list while these were missing, even though the rows were correct.

use super::*;

/// Return an artwork path only when `row` owns it.  Cycle 50: borrowed hierarchy
/// artwork keeps the owner's ratingKey in its path; all eleven owning seasons sampled
/// on zen carried `thumb` on the `/children` container and all four borrowing ones
/// omitted it.
fn owned_artwork(row: &Value, field: &str) -> Option<Value> {
    let value = row.get(field)?;
    let prefix = format!(
        "/library/metadata/{}/",
        row.get("ratingKey").map(string_value).unwrap_or_default()
    );
    string_value(value)
        .starts_with(&prefix)
        .then(|| value.clone())
}

fn non_null(value: Option<&Value>) -> Option<Value> {
    value.filter(|value| !value.is_null()).cloned()
}

impl Runtime {
    /// A child list's row: the light grid projection without the librarySection
    /// fields, carrying `Guid` only under `includeGuids=1` (and then a one-entry list
    /// naming the row's own guid when it has none).
    pub(super) fn child_list_row(&self, row: &Value, query: &HashMap<String, String>) -> Value {
        let include_guids = query.get("includeGuids").is_some_and(|value| value == "1");
        let mut shown = self.grid_row(row, false, include_guids, false);
        if include_guids {
            apply_listing_guids(&mut shown, row);
        }
        shown
    }

    /// The container fields of a child list (everything but its rows and paging).
    /// `first_row_type` is the type of the list's first row, which decides whether it
    /// is an episode list.
    pub(super) fn child_container_fields(
        &self,
        parent: &Value,
        leaf: &str,
        first_row_type: Option<&str>,
    ) -> Map<String, Value> {
        let kind = parent
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let section_key = parent
            .get("librarySectionID")
            .map(string_value)
            .unwrap_or_default();
        let section = self.section(&section_key).cloned().unwrap_or(Value::Null);
        let is_episode_list = first_row_type == Some("episode")
            || (leaf == "allLeaves" && matches!(kind, "season" | "episode"));
        let is_movie_leaves = leaf == "allLeaves" && kind == "movie";
        let mut fields: Vec<(&str, Option<Value>)> = vec![
            (
                "key",
                Some(json!(parent
                    .get("ratingKey")
                    .map(string_value)
                    .unwrap_or_default())),
            ),
            ("nocache", Some(json!(true))),
            // Cycle 50: a MOVIE's own `/allLeaves` is `viewGroup="movie"`, 65592.
            (
                "viewGroup",
                Some(json!(if is_movie_leaves {
                    "movie"
                } else if is_episode_list {
                    "episode"
                } else {
                    "season"
                })),
            ),
            // Measured on fun: a SEASON list is 65593 and an EPISODE list is 65592.
            (
                "viewMode",
                Some(json!(if is_episode_list || is_movie_leaves {
                    65592
                } else {
                    65593
                })),
            ),
            ("librarySectionID", non_null(parent.get("librarySectionID"))),
            (
                "librarySectionTitle",
                non_null(parent.get("librarySectionTitle")),
            ),
            ("librarySectionUUID", non_null(section.get("uuid"))),
            ("title1", non_null(parent.get("librarySectionTitle"))),
            (
                "summary",
                Some(json!(parent
                    .get("summary")
                    .map(string_value)
                    .unwrap_or_default())),
            ),
            // Measured on both servers: every child list says it can sync.
            ("allowSync", Some(json!(true))),
        ];
        let mut set =
            |fields: &mut Vec<(&str, Option<Value>)>, name: &'static str, value: Option<Value>| {
                if let Some(slot) = fields.iter_mut().find(|(known, _)| *known == name) {
                    slot.1 = value;
                } else {
                    fields.push((name, value));
                }
            };
        if kind == "season" {
            // An episode list is presented as the SHOW's, with the season as the
            // parent.  `title1` is the SHOW's title, `grandparentRatingKey` a number,
            // no `grandparentYear`; `parentTitle` is "" only while the season keeps its
            // generated title, and `summary` is absent while the season has none.
            let show_key = parent
                .get("parentRatingKey")
                .map(string_value)
                .unwrap_or_default();
            let show = self.catalog.get(&show_key).cloned();
            if parent
                .get("summary")
                .map(string_value)
                .unwrap_or_default()
                .is_empty()
            {
                set(&mut fields, "summary", None);
            }
            let title = parent.get("title").map(string_value).unwrap_or_default();
            let index = parent.get("index").map(string_value).unwrap_or_default();
            let generated = title.is_empty()
                || title == format!("Season {index}")
                || (index == "0" && title == "Specials");
            set(
                &mut fields,
                "title1",
                show.as_ref()
                    .and_then(|show| non_null(show.get("title")))
                    .or_else(|| non_null(parent.get("parentTitle"))),
            );
            set(&mut fields, "title2", non_null(parent.get("title")));
            set(&mut fields, "parentIndex", non_null(parent.get("index")));
            set(
                &mut fields,
                "parentTitle",
                Some(json!(if generated { String::new() } else { title })),
            );
            set(&mut fields, "parentYear", non_null(parent.get("year")));
            set(&mut fields, "thumb", owned_artwork(parent, "thumb"));
            set(&mut fields, "art", non_null(parent.get("art")));
            if let Some(show) = &show {
                let rating_key = show.get("ratingKey").map(string_value).unwrap_or_default();
                set(
                    &mut fields,
                    "grandparentRatingKey",
                    Some(
                        rating_key
                            .parse::<i64>()
                            .map(|number| json!(number))
                            .unwrap_or_else(|_| json!(rating_key)),
                    ),
                );
                set(&mut fields, "grandparentTitle", non_null(show.get("title")));
                set(&mut fields, "grandparentThumb", non_null(show.get("thumb")));
                set(
                    &mut fields,
                    "grandparentStudio",
                    non_null(show.get("studio")),
                );
                set(
                    &mut fields,
                    "grandparentContentRating",
                    non_null(show.get("contentRating")),
                );
            }
        } else {
            set(&mut fields, "title2", non_null(parent.get("title")));
            set(
                &mut fields,
                "parentIndex",
                Some(
                    parent
                        .get("index")
                        .filter(|index| !index.is_null() && string_value(index) != "0")
                        .cloned()
                        .unwrap_or(json!(1)),
                ),
            );
            set(&mut fields, "parentTitle", non_null(parent.get("title")));
            set(&mut fields, "parentYear", non_null(parent.get("year")));
            set(&mut fields, "thumb", owned_artwork(parent, "thumb"));
            set(&mut fields, "art", non_null(parent.get("art")));
        }
        if leaf == "allLeaves" && kind == "season" {
            // A season's (empty) allLeaves names the show only by title.
            for name in [
                "grandparentRatingKey",
                "grandparentThumb",
                "grandparentStudio",
                "grandparentContentRating",
            ] {
                set(&mut fields, name, None);
            }
        }
        if leaf == "allLeaves" && kind == "episode" {
            // An episode's allLeaves is presented as its SEASON's.
            set(&mut fields, "title1", non_null(parent.get("parentTitle")));
            set(&mut fields, "grandparentTitle", Some(json!("")));
        }
        if leaf == "allLeaves" {
            // The allLeaves container says `mixedParents` and carries neither `thumb`
            // nor `summary`, both of which /children does.
            set(&mut fields, "mixedParents", Some(json!(true)));
            set(&mut fields, "thumb", None);
            set(&mut fields, "summary", None);
        }
        let mut out = Map::new();
        for (name, value) in fields {
            if let Some(value) = value {
                out.insert(name.to_string(), value);
            }
        }
        out
    }
}
