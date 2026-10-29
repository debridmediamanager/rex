//! Manage Recommendations and a collection's "Visible on": which hubs a library shows on its
//! Recommended page, on its owner's Home and on its friends' Home, in what order.
//!
//! Measured on a real, online PMS 1.43.5 driven by Plex Web 4.162.1
//! (`tests/fixtures/collection_hub_pins_s1435.json`, `tests/test_collection_hub_pins.py`):
//!
//! * The layout is the SERVER's, one per library: the owner's Plex Web writes it and every
//!   reader is answered from it. "Home" is the owner's Home (`homeVisibility="admin"`),
//!   "Friends' Home" every other account's (`"shared"`; both `"all"`).
//! * A collection joins the list through `POST manage?metadataItemId=` (Visible on) and stays
//!   in it, all three unticked, until Remove Hub (`DELETE manage/{id}`) or Reset Customization
//!   (`DELETE manage`). Only a collection is `deletable`; a built-in's DELETE is a 400.
//! * `PUT manage/{id}/move?after=` puts a hub after another; with no `after`, first.
//! * The served shelf is `custom.collection.{section}.{id}.{id}`, the collection's members
//!   in its own order, placed among the library's hubs by the list's order, and `promoted`
//!   where the reader's Home shows it.
//!
//! A real PMS serves the shelf only when its owner has Plex Pass (the same server, owned by
//! an account without, took every write and showed nothing); rex claims Plex Pass.

use std::collections::HashMap;

use serde_json::{json, Map, Value};

use super::*;

/// One row of a library's list: a built-in hub's identifier or `custom.collection.{s}.{id}`.
#[derive(Clone, Debug, PartialEq)]
pub(super) struct HubSlot {
    pub identifier: String,
    pub recommended: bool,
    pub own_home: bool,
    pub shared_home: bool,
}

impl HubSlot {
    pub(super) fn to_value(&self) -> Value {
        json!([self.identifier, self.recommended, self.own_home, self.shared_home])
    }

    pub(super) fn from_value(value: &Value) -> Option<Self> {
        Some(Self {
            identifier: value.get(0)?.as_str()?.to_string(),
            recommended: value.get(1)?.as_bool()?,
            own_home: value.get(2)?.as_bool()?,
            shared_home: value.get(3)?.as_bool()?,
        })
    }

    fn home_for(&self, owner: bool) -> bool {
        if owner {
            self.own_home
        } else {
            self.shared_home
        }
    }
}

/// The built-in list a library starts with, and what Reset Customization returns to.
fn builtin_hubs(is_show: bool) -> &'static [(&'static str, &'static str, bool)] {
    if is_show {
        &[
            ("tv.recentlyaired", "Recently Released Episodes", false),
            ("tv.recentlyadded", "Recently Added TV", true),
            ("recent.library.playlists", "Library Playlists", false),
            ("tv.startwatching", "Start Watching", false),
            ("tv.rediscover", "Rediscover", false),
            ("tv.morefromnetwork", "More from (Network)", false),
            ("tv.moreingenre", "More in (Genre)", false),
            ("tv.toprated", "Top Rated TV", false),
            ("tv.recentlyviewed", "Recently Watched Episodes", false),
        ]
    } else {
        &[
            ("movie.recentlyreleased", "Recently Released Movies", false),
            ("movie.recentlyadded", "Recently Added Movies", true),
            ("recent.library.playlists", "Library Playlists", false),
            ("movie.genre", "Top Movies in (Genre)", false),
            ("movie.by.actor.or.director", "Top Movies by (Actor or Director)", false),
            ("movie.topunwatched", "Top Unwatched Movies", false),
            ("movie.curated", "Seasonal Movies", true),
            ("movie.recentlyviewed", "Recently Watched Movies", false),
        ]
    }
}

fn flag(query: &HashMap<String, String>, name: &str) -> bool {
    query.get(name).is_some_and(|value| is_true_option(value))
}

/// The section and collection id a `custom.collection.{s}.{id}` identifier names.
fn custom_collection(identifier: &str) -> Option<(&str, &str)> {
    identifier.strip_prefix("custom.collection.")?.split_once('.')
}

impl Runtime {
    fn section_is_show(&self, section_key: &str) -> bool {
        self.section(section_key)
            .and_then(|section| section.get("type").and_then(Value::as_str))
            == Some("show")
    }

    /// A library's list: what was saved, else the built-ins; a collection deleted since, or
    /// one of another library, is left out.
    pub(super) fn hub_layout(&self, section_key: &str) -> Vec<HubSlot> {
        let Some(saved) = self.hub_layouts.get(section_key) else {
            return builtin_hubs(self.section_is_show(section_key))
                .iter()
                .map(|(identifier, _, home)| HubSlot {
                    identifier: identifier.to_string(),
                    recommended: true,
                    own_home: *home,
                    shared_home: *home,
                })
                .collect();
        };
        saved
            .iter()
            .filter(|slot| match custom_collection(&slot.identifier) {
                Some((section, id)) => {
                    section == section_key
                        && self.collections.get(id).is_some_and(|collection| collection.section == section_key)
                }
                None => true,
            })
            .cloned()
            .collect()
    }

    fn manage_hub(&self, section_key: &str, slot: &HubSlot) -> Value {
        let custom = custom_collection(&slot.identifier);
        let title = match custom {
            Some((_, id)) => self.collections.get(id).map(|collection| collection.title.clone()).unwrap_or_default(),
            None => builtin_hubs(self.section_is_show(section_key))
                .iter()
                .find(|(identifier, _, _)| *identifier == slot.identifier)
                .map(|(_, title, _)| title.to_string())
                .unwrap_or_default(),
        };
        let home = match (slot.own_home, slot.shared_home) {
            (true, true) => "all",
            (true, false) => "admin",
            (false, true) => "shared",
            (false, false) => "none",
        };
        let mut hub = Map::from_iter([
            ("identifier".to_string(), json!(slot.identifier)),
            ("title".to_string(), json!(title)),
            ("recommendationsVisibility".to_string(), json!(if slot.recommended { "all" } else { "none" })),
            ("homeVisibility".to_string(), json!(home)),
            ("promotedToRecommended".to_string(), json!(slot.recommended)),
            ("promotedToOwnHome".to_string(), json!(slot.own_home)),
            ("promotedToSharedHome".to_string(), json!(slot.shared_home)),
        ]);
        if custom.is_some() {
            hub.insert("deletable".to_string(), json!(true));
        }
        Value::Object(hub)
    }

    fn manage_answer(&self, section_key: &str, slots: &[HubSlot]) -> Response {
        let hubs = slots.iter().map(|slot| self.manage_hub(section_key, slot)).collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("size".to_string(), json!(hubs.len())), ("Hub".to_string(), Value::Array(hubs))]),
                false,
            ),
        )
    }

    /// `/hubs/sections/{s}/manage[/{identifier}[/move]]`, every verb Plex Web sends.
    pub(super) fn route_hub_manage(
        &mut self,
        request: &Request,
        section_key: &str,
        rest: &[&str],
        query: &HashMap<String, String>,
    ) -> Response {
        if self.section(section_key).is_none() {
            return Response::error(404, "rex: not found");
        }
        let method = request.method.as_str();
        let mut layout = self.hub_layout(section_key);
        let position = |layout: &[HubSlot], identifier: &str| layout.iter().position(|slot| slot.identifier == identifier);
        match (method, rest) {
            ("GET" | "HEAD", []) => {
                if let Some(raw_item) = query.get("metadataItemId") {
                    // Measured on both servers: naming an item -- even with an empty id --
                    // is a 400 on a read.  Cycle 55 refined it on the 1.42.2 reference and
                    // plexlab: the id is a signed 32-bit integer and there are THREE answers.
                    // `-1` (and `-01`) is the bare hub list, a number naming NO item (`0`,
                    // `-2`, `999999`, `+999999`) is an EMPTY 200, and an item the server holds
                    // -- in any library -- is the 400, as are an empty value, a word, `1.5`, `
                    // 1` and `2147483648`. A collection is the third kind: its own row of this
                    // library's list, or an empty 200 when it has none here (1.43.5).
                    if !int_param(raw_item, Some(32)) {
                        return Response::error(400, "rex: bad request");
                    }
                    let item = raw_item.parse::<i64>().unwrap_or(0);
                    if item != -1 {
                        let identifier = format!("custom.collection.{section_key}.{item}");
                        if self.collections.contains_key(&item.to_string()) {
                            let own = layout.iter().filter(|slot| slot.identifier == identifier).cloned().collect::<Vec<_>>();
                            return self.manage_answer(section_key, &own);
                        }
                        if self.catalog.get(&item.to_string()).is_some() {
                            return Response::error(400, "rex: bad request");
                        }
                        return Response::json(
                            200,
                            self.container(Map::from_iter([("size".to_string(), json!(0))]), false),
                        );
                    }
                }
                self.manage_answer(section_key, &layout)
            }
            // Visible on: the collection's row, added at the end, or its flags changed in place.
            ("POST", []) => {
                let Some(id) = query
                    .get("metadataItemId")
                    .filter(|id| self.collections.get(*id).is_some_and(|collection| collection.section == section_key))
                else {
                    // ponytail: only a collection of this library was measured; anything else
                    // keeps the 404 every write here answered before.
                    return Response::error(404, "rex: not found");
                };
                let slot = HubSlot {
                    identifier: format!("custom.collection.{section_key}.{id}"),
                    recommended: flag(query, "promotedToRecommended"),
                    own_home: flag(query, "promotedToOwnHome"),
                    shared_home: flag(query, "promotedToSharedHome"),
                };
                match position(&layout, &slot.identifier) {
                    Some(index) => layout[index] = slot.clone(),
                    None => layout.push(slot.clone()),
                }
                self.hub_layouts.insert(section_key.to_string(), layout);
                self.dirty = true;
                self.manage_answer(section_key, &[slot])
            }
            // The manage page's three ticks, on any hub of the list.
            ("PUT", [identifier]) => {
                let Some(index) = position(&layout, identifier) else {
                    return Response::error(404, "rex: not found");
                };
                layout[index].recommended = flag(query, "promotedToRecommended");
                layout[index].own_home = flag(query, "promotedToOwnHome");
                layout[index].shared_home = flag(query, "promotedToSharedHome");
                self.hub_layouts.insert(section_key.to_string(), layout);
                self.dirty = true;
                Response::empty(200)
            }
            ("PUT", [identifier, "move"]) => {
                let Some(index) = position(&layout, identifier) else {
                    return Response::error(400, "rex: bad request");
                };
                let moved = layout.remove(index);
                let at = match query.get("after").filter(|after| !after.is_empty()) {
                    None => 0,
                    Some(after) => match position(&layout, after) {
                        Some(found) => found + 1,
                        None => {
                            // ponytail: an `after` naming no hub is unmeasured; refused as an
                            // unknown hub is, and nothing moves.
                            return Response::error(400, "rex: bad request");
                        }
                    },
                };
                layout.insert(at, moved);
                self.hub_layouts.insert(section_key.to_string(), layout.clone());
                self.dirty = true;
                self.manage_answer(section_key, &layout)
            }
            ("DELETE", [identifier]) => {
                let Some(index) = position(&layout, identifier) else {
                    return Response::error(404, "rex: not found");
                };
                if custom_collection(identifier).is_none() {
                    return Response::error(400, "rex: bad request");
                }
                layout.remove(index);
                self.hub_layouts.insert(section_key.to_string(), layout);
                self.dirty = true;
                Response::empty(200)
            }
            // Reset Customization: the built-ins as they were, and no collection.
            ("DELETE", []) => {
                self.hub_layouts.remove(section_key);
                self.dirty = true;
                Response::empty(200)
            }
            _ => Response::error(404, "rex: not found"),
        }
    }

    /// A collection's shelf as a library's hub page and Home serve it.
    fn collection_shelf(&self, collection_id: &str, query: &HashMap<String, String>, promoted: bool) -> Option<Value> {
        let collection = self.collections.get(collection_id)?;
        let rows = self
            .collection_members(collection)
            .into_iter()
            .filter(|row| self.visible(row))
            .collect::<Vec<_>>();
        let kind = if collection.subtype.is_empty() { "movie" } else { collection.subtype.as_str() };
        let section = &collection.section;
        let mut extra = vec![
            ("key", json!(format!("/library/collections/{collection_id}/children"))),
            ("context", json!("hub.custom.collection")),
            ("style", json!("shelf")),
        ];
        if promoted {
            extra.push(("promoted", json!(true)));
        }
        let mut built = self.hub(
            &format!("custom.collection.{section}.{collection_id}.{collection_id}"),
            &collection.title,
            &rows,
            query.get("count"),
            kind,
            false,
            extra,
        );
        let keys = built
            .get("Metadata")
            .and_then(Value::as_array)
            .map(|rows| rows.iter().filter_map(key_of).collect::<Vec<_>>())
            .unwrap_or_default();
        if keys.is_empty() {
            built.shift_remove("hubKey");
        } else {
            built.insert("hubKey".to_string(), json!(format!("/library/metadata/{}", keys.join(","))));
        }
        Some(Value::Object(built))
    }

    /// The managed identifier a served hub belongs to (`movie.recentlyadded.1` is
    /// `movie.recentlyadded`'s), if the list names it.
    fn slot_of<'a>(layout: &'a [HubSlot], hub: &Value) -> Option<(usize, &'a HubSlot)> {
        let identifier = hub.get("hubIdentifier").and_then(Value::as_str)?;
        layout.iter().enumerate().find(|(_, slot)| {
            identifier == slot.identifier
                || identifier
                    .strip_prefix(slot.identifier.as_str())
                    .is_some_and(|rest| rest.starts_with('.'))
        })
    }

    /// A library's Recommended page in its list's order, its collections' shelves among the
    /// built-ins. A hub the list does not name (Continue Watching) keeps its place ahead.
    pub(super) fn arrange_section_hubs(&self, section_key: &str, hubs: Vec<Value>, query: &HashMap<String, String>) -> Vec<Value> {
        if !self.hub_layouts.contains_key(section_key) {
            return hubs;
        }
        let layout = self.hub_layout(section_key);
        let owner = self.viewer.is_owner();
        let mut unmanaged = Vec::new();
        let mut managed: Vec<(usize, Value)> = Vec::new();
        for hub in hubs {
            match Self::slot_of(&layout, &hub) {
                Some((index, _)) => managed.push((index, hub)),
                None => unmanaged.push(hub),
            }
        }
        for (index, slot) in layout.iter().enumerate() {
            if let Some((_, id)) = custom_collection(&slot.identifier) {
                if slot.recommended {
                    managed.extend(self.collection_shelf(id, query, slot.home_for(owner)).map(|hub| (index, hub)));
                }
            }
        }
        managed.sort_by_key(|(index, _)| *index);
        unmanaged.extend(managed.into_iter().map(|(_, hub)| hub));
        unmanaged
    }

    /// The Home shelves of one library's collections, around the library's own Recently
    /// Added: those listed before it, and those after.
    pub(super) fn home_collection_shelves(&self, section_key: &str, query: &HashMap<String, String>) -> (Vec<Value>, Vec<Value>) {
        if !self.hub_layouts.contains_key(section_key) {
            return (Vec::new(), Vec::new());
        }
        let layout = self.hub_layout(section_key);
        let owner = self.viewer.is_owner();
        let added = layout
            .iter()
            .position(|slot| slot.identifier.ends_with(".recentlyadded"))
            .unwrap_or(layout.len());
        let (mut before, mut after) = (Vec::new(), Vec::new());
        for (index, slot) in layout.iter().enumerate() {
            let Some((_, id)) = custom_collection(&slot.identifier) else {
                continue;
            };
            if !slot.home_for(owner) {
                continue;
            }
            if let Some(hub) = self.collection_shelf(id, query, true) {
                if index < added {
                    before.push(hub);
                } else {
                    after.push(hub);
                }
            }
        }
        (before, after)
    }
}
