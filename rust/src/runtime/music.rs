//! Music: what a source pushes as `kind: "track"` (docs/feed-api.md, "Tracks"), as a real PMS's
//! music library shows it -- artist (type 8), album (9), track (10).
//!
//! rex identifies nothing here either. A track's source names its album, by MusicBrainz's release id
//! (`ids.mbid`) or by Plex's album guid (`ids.plex`), and its `disc` and `track`; rex exchanges the
//! id for Plex's album through Plex's agent exactly as plexav's PMS 1.43.4 asked for its own music
//! library (`identify::plex_album`, its outbound capture of 2026-10-31), and the album's children
//! name the track. The artist is the album's parent there. What the agent says is everything a row
//! shows: titles, artwork, genres, moods, styles, reviews. A track nothing names is shown by its
//! source's `display` (its `artist` and `album`, as an episode's `show` and `season`), grouped by
//! those names; no tag or file name is read.
//!
//! The shapes are plexav's (a real PMS 1.43.4 with four albums, replayed with Plex Web 4.163.0,
//! Plex for Android 2026.21.0 and Plexamp 4.12.4; `tests/fixtures/music/`).

use super::feed::{held_key, lock, Counts, Item};
use super::source::Source;
use super::*;
use crate::identify::{self, PlexSource};
use crate::{hierarchy, mediainfo, metadata};

/// What a source may push as a track: the containers Plex's music scanner reads.
const AUDIO: &[&str] = &[
    "flac", "mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "alac", "aif", "aiff", "wma", "ape", "wv", "dsf", "mka",
];

/// What PMS names an artist and an album no tag names (its local agent's words); rex has no tags
/// to read, so a track pushed with neither ids nor `display` sits under these. Recalled from PMS,
/// not measured on plexav, whose files were all tagged.
const UNKNOWN_ARTIST: &str = "[Unknown Artist]";
const UNKNOWN_ALBUM: &str = "[Unknown Album]";

pub(super) fn is_audio(path: &str) -> bool {
    path.rsplit_once('.').is_some_and(|(_, extension)| AUDIO.contains(&extension.to_ascii_lowercase().as_str()))
}

/// What a track is placed by: its album's ids, its disc and number, and what its source displays.
fn album_ids(item: &Item) -> (Option<String>, Option<String>) {
    let id = |key: &str| item.ids.get(key).map(string_value).filter(|id| !id.is_empty());
    (id("mbid"), id("plex"))
}

fn placed_number(item: &Item, field: &str) -> Option<i64> {
    item.placed.as_ref()?.get(field)?.as_i64()
}

fn display_part<'a>(item: &'a Item, part: &str) -> Option<&'a Map<String, Value>> {
    item.display.as_ref()?.get(part)?.as_object()
}

fn display_text(item: &Item, part: Option<&str>, field: &str) -> Option<String> {
    let from = match part {
        Some(part) => display_part(item, part)?,
        None => item.display.as_ref()?,
    };
    from.get(field)?.as_str().map(str::to_string)
}

/// What the agent said of an album and its artist, by the ids a source pushed.
type Named = HashMap<(Option<String>, Option<String>), Option<(Value, Option<Value>)>>;

enum Plan {
    New,
    Replace(String),
}

/// A track's placement as the row keeps it, to tell a push that moves it.
fn placement(item: &Item) -> Value {
    json!({"placed": item.placed, "ids": item.ids, "display": item.display})
}

/// Tracks a source pushed: held without a probe, placed on their album when new, placed again on
/// the same key when the source moves them; the rest of the feed (removal, snapshots, the source's
/// listing, part reads) is the video path's, by the same `_source`/`_sid` on the row.
pub(super) fn upsert_tracks(
    shared: &SharedRuntime,
    source: &Source,
    items: Vec<Item>,
    counts: &mut Counts,
    mut seen: Option<&mut HashSet<String>>,
    retry: bool,
) {
    let mut plans: Vec<(Item, String, Plan)> = Vec::new();
    let plex = {
        let mut guard = lock(shared);
        let rows = guard.feed_rows(&source.name, items.iter().map(|item| item.id.as_str()));
        for item in items {
            let Some(section) = guard.music_library(&item) else {
                counts.rejected.push(json!({"id": item.id, "reason": format!("no music library holds {}", item.file)}));
                continue;
            };
            if let Some(seen) = seen.as_deref_mut() {
                seen.insert(item.id.clone());
            }
            if retry {
                continue;
            }
            let held = held_key(&source.name, &item.id);
            match rows.get(&item.id) {
                Some(key) => {
                    guard.release(&held);
                    let kept = guard.catalog.get(key).and_then(|row| row.get("_placement")).cloned();
                    if item.media_info.is_none() || kept.as_ref() == Some(&placement(&item)) {
                        counts.unchanged += 1;
                    } else {
                        plans.push((item, section, Plan::Replace(key.clone())));
                    }
                }
                // Named by its probe's digest alone: rex holds no such version, so it asks for the
                // probe (`wants`), as for any item (docs/feed-api.md "Probe digests").
                None if item.media_info.is_none() && item.probe.is_some() => counts.wants.push(item.id.clone()),
                None if item.media_info.is_none() => {
                    let since = guard.feed_held.get(&held).and_then(|old| old.get("since")).and_then(Value::as_i64).unwrap_or_else(now);
                    let record = item.held(since);
                    if guard.feed_held.get(&held) != Some(&record) {
                        info!("FEED {} held {}: no media_info yet", source.name, item.id);
                        guard.hold(held, record);
                        guard.dirty = true;
                    }
                    counts.held += 1;
                }
                None => {
                    guard.release(&held);
                    plans.push((item, section, Plan::New));
                }
            }
        }
        guard.plex.clone()
    };
    if plans.is_empty() {
        return;
    }
    // Off the lock: Plex's agent, once per album.
    let mut named: Named = HashMap::new();
    for (item, _, _) in &plans {
        let ids = album_ids(item);
        if ids == (None, None) || named.contains_key(&ids) {
            continue;
        }
        let answer = plex.as_deref().and_then(|p| identify::plex_album(p as &dyn PlexSource, ids.0.as_deref(), ids.1.as_deref()));
        let what = answer.as_ref().and_then(|(album, _)| album.get("guid")).map(string_value).unwrap_or_else(|| "nothing".to_string());
        info!("FEED identified album {:?} -> {what}", ids.0.as_deref().or(ids.1.as_deref()).unwrap_or_default());
        named.insert(ids, answer);
    }
    let mut guard = lock(shared);
    let mut sections: Vec<String> = Vec::new();
    // Unplaced tracks are numbered in their album's file order, after what it already holds.
    plans.sort_by(|a, b| a.0.path.cmp(&b.0.path));
    for (item, section, plan) in plans {
        let answer = named.get(&album_ids(&item)).cloned().flatten();
        let key = match &plan {
            Plan::Replace(key) => {
                guard.unplace_track(key);
                Some(key.clone())
            }
            Plan::New => None,
        };
        let key = guard.place_track(&section, source, &item, answer.as_ref(), key);
        match plan {
            Plan::New => {
                counts.created += 1;
                guard.push_created_frames(&key);
                if !sections.contains(&section) {
                    sections.push(section);
                }
            }
            Plan::Replace(_) => counts.updated += 1,
        }
    }
    guard.dirty = true;
    guard.touch_gdm();
}

impl Runtime {
    /// Whether a section is a music library (`type=artist`).
    pub(super) fn music_section(&self, section: &str) -> bool {
        self.catalog.sections.iter().any(|row| {
            row.get("key").map(string_value).as_deref() == Some(section) && row.get("type").and_then(Value::as_str) == Some("artist")
        })
    }

    /// The music library a track goes to: the one its `library` names, else the one whose
    /// folder holds its file or names its first directory, else the first music library. rex
    /// makes no library on a push, as for films and shows: Add Library (type `artist`) or a
    /// source's `directories` (`music`) make one.
    pub(super) fn music_library(&self, item: &Item) -> Option<String> {
        let music: Vec<&Value> = self.catalog.sections.iter().filter(|section| section.get("type").and_then(Value::as_str) == Some("artist")).collect();
        let key = |section: &&Value| section.get("key").map(string_value);
        if let Some(wanted) = item.library.as_deref().filter(|wanted| *wanted != "artist") {
            return music.iter().find(|section| key(section).as_deref() == Some(wanted)).and_then(key);
        }
        let directory = item.path.split('/').next().unwrap_or_default();
        music
            .iter()
            .find(|section| self.locations(section).iter().any(|location| super::scan::under(&item.file, location)))
            .or_else(|| {
                music.iter().find(|section| {
                    self.locations(section).iter().any(|location| location.trim_end_matches('/').rsplit('/').next() == Some(directory))
                })
            })
            .or(music.first())
            .and_then(key)
    }

    fn section_title_of(&self, section: &str) -> String {
        self.catalog
            .sections
            .iter()
            .find(|row| row.get("key").map(string_value).as_deref() == Some(section))
            .and_then(|row| row.get("title"))
            .map(string_value)
            .unwrap_or_default()
    }

    /// A row of `kind` in `section` whose identity is `identity` (under `parent`).
    fn music_row(&self, section: &str, kind: &str, identity: &str, parent: Option<&str>) -> Option<String> {
        self.catalog
            .find_where(|row| {
                row.get("type").and_then(Value::as_str) == Some(kind)
                    && string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section
                    && row.get("_identity").and_then(Value::as_str) == Some(identity)
                    && parent.is_none_or(|parent| row.get("parentRatingKey").map(string_value).as_deref() == Some(parent))
            })
            .and_then(key_of)
    }

    /// A track's artist, album and own row, each found or made, the track on `key` when it keeps
    /// one. Answers the track's key.
    fn place_track(&mut self, section: &str, source: &Source, item: &Item, named: Option<&(Value, Option<Value>)>, key: Option<String>) -> String {
        let stamp = now();
        let section_title = self.section_title_of(section);
        let section_id = section.parse::<i64>().map(Value::from).unwrap_or_else(|_| json!(section));
        let library = |row: &mut Map<String, Value>| {
            row.insert("librarySectionID".to_string(), section_id.clone());
            row.insert("librarySectionTitle".to_string(), json!(section_title));
            row.insert("librarySectionKey".to_string(), json!(format!("/library/sections/{section}")));
            row.insert("_scanned".to_string(), json!(true));
        };
        let (album, artist) = match named {
            Some((album, artist)) => (Some(album), artist.as_ref()),
            None => (None, None),
        };
        let text = |row: Option<&Value>, field: &str| row.and_then(|row| row.get(field)).filter(|value| metadata::truthy(value)).cloned();
        let tags = |row: Option<&Value>, field: &str| -> Option<Value> {
            let list: Vec<Value> = row?
                .get(field)?
                .as_array()?
                .iter()
                .filter_map(|entry| Some(json!({"tag": entry.get("tag")?.as_str()?})))
                .collect();
            (!list.is_empty()).then_some(Value::Array(list))
        };
        let ids = |row: Option<&Value>| -> Option<Value> {
            let list: Vec<Value> = row?.get("Guid")?.as_array()?.iter().filter_map(|id| Some(json!({"id": id.get("id")?.as_str()?}))).collect();
            (!list.is_empty()).then_some(Value::Array(list))
        };
        let folder = |path: &str| path.rsplit_once('/').map(|(folder, _)| folder.to_string()).unwrap_or_default();

        // -- the artist ------------------------------------------------------------------------
        let artist_title = text(artist, "title")
            .map(|title| string_value(&title))
            .or_else(|| text(album, "parentTitle").map(|title| string_value(&title)))
            .or_else(|| display_text(item, Some("artist"), "title"))
            .unwrap_or_else(|| UNKNOWN_ARTIST.to_string());
        let artist_identity = text(artist, "guid")
            .or_else(|| text(album, "parentGuid"))
            .map(|guid| string_value(&guid))
            .unwrap_or_else(|| format!("artist:{}", hierarchy::fold_title(&artist_title).to_lowercase()));
        let artist_key = match self.music_row(section, "artist", &artist_identity, None) {
            Some(found) => found,
            None => {
                let rk = self.mint_key();
                let mut row = Map::new();
                row.insert("ratingKey".to_string(), json!(rk));
                row.insert("key".to_string(), json!(format!("/library/metadata/{rk}/children")));
                let guid = text(artist, "guid").or_else(|| text(album, "parentGuid")).unwrap_or_else(|| json!(format!("local://{rk}")));
                row.insert("guid".to_string(), guid);
                row.insert("type".to_string(), json!("artist"));
                row.insert("title".to_string(), json!(artist_title));
                let summary = text(artist, "summary").or_else(|| display_text(item, Some("artist"), "summary").map(Value::from));
                row.insert("summary".to_string(), summary.unwrap_or_else(|| json!("")));
                row.insert("index".to_string(), json!(1));
                row.insert("thumb".to_string(), json!(format!("/library/metadata/{rk}/thumb/{stamp}")));
                row.insert("art".to_string(), json!(format!("/library/metadata/{rk}/art/{stamp}")));
                row.insert("addedAt".to_string(), json!(stamp));
                row.insert("updatedAt".to_string(), json!(stamp));
                for field in ["Genre", "Country", "Style", "Mood", "Similar"] {
                    if let Some(list) = tags(artist, field) {
                        row.insert(field.to_string(), list);
                    }
                }
                if let Some(list) = ids(artist) {
                    row.insert("Guid".to_string(), list);
                }
                // A real PMS's artist Location is the artist's folder: here, the one above the album's.
                row.insert("Location".to_string(), json!([{"path": folder(&folder(&item.file))}]));
                let poster = text(artist, "thumb").or_else(|| text(album, "parentThumb")).or_else(|| display_text(item, Some("artist"), "poster").map(Value::from));
                let art = text(artist, "art").or_else(|| text(album, "parentArt")).or_else(|| display_text(item, Some("artist"), "art").map(Value::from));
                row.insert("_poster_url".to_string(), poster.unwrap_or(Value::Null));
                row.insert("_art_url".to_string(), art.unwrap_or(Value::Null));
                row.insert("_identity".to_string(), json!(artist_identity));
                library(&mut row);
                sort_titled(&mut row);
                let mut value = Value::Object(row);
                number_catalog_tags(std::slice::from_mut(&mut value));
                self.catalog.add(value);
                rk
            }
        };
        let artist_row = self.catalog.get(&artist_key).cloned().unwrap_or_default();
        let artist_field = |field: &str| artist_row.get(field).cloned().unwrap_or(Value::Null);

        // -- the album -------------------------------------------------------------------------
        let album_title = text(album, "title")
            .map(|title| string_value(&title))
            .or_else(|| display_text(item, Some("album"), "title"))
            .unwrap_or_else(|| UNKNOWN_ALBUM.to_string());
        let album_identity = text(album, "guid")
            .map(|guid| string_value(&guid))
            .unwrap_or_else(|| format!("album:{}", hierarchy::fold_title(&album_title).to_lowercase()));
        let album_key = match self.music_row(section, "album", &album_identity, Some(&artist_key)) {
            Some(found) => found,
            None => {
                let rk = self.mint_key();
                let mut row = Map::new();
                row.insert("ratingKey".to_string(), json!(rk));
                row.insert("key".to_string(), json!(format!("/library/metadata/{rk}/children")));
                row.insert("parentRatingKey".to_string(), json!(artist_key));
                row.insert("guid".to_string(), text(album, "guid").unwrap_or_else(|| json!(format!("local://{rk}"))));
                row.insert("parentGuid".to_string(), artist_field("guid"));
                if let Some(studio) = text(album, "studio") {
                    row.insert("studio".to_string(), studio);
                }
                row.insert("type".to_string(), json!("album"));
                row.insert("title".to_string(), json!(album_title));
                row.insert("parentKey".to_string(), json!(format!("/library/metadata/{artist_key}")));
                row.insert("parentTitle".to_string(), artist_field("title"));
                let summary = text(album, "summary").or_else(|| display_text(item, Some("album"), "summary").map(Value::from));
                row.insert("summary".to_string(), summary.unwrap_or_else(|| json!("")));
                row.insert("index".to_string(), json!(1));
                if let Some(rating) = text(album, "rating").and_then(|rating| rating.as_f64()) {
                    row.insert("rating".to_string(), json!(rating));
                }
                let date = text(album, "originallyAvailableAt").map(|date| string_value(&date)).or_else(|| display_text(item, Some("album"), "date"));
                let year = text(album, "year").and_then(|year| year.as_i64()).or_else(|| date.as_deref().and_then(|date| date.get(..4)?.parse().ok()));
                if let Some(year) = year {
                    row.insert("year".to_string(), json!(year));
                }
                row.insert("thumb".to_string(), json!(format!("/library/metadata/{rk}/thumb/{stamp}")));
                // A real PMS's album draws its artist's background.
                row.insert("art".to_string(), artist_field("art"));
                row.insert("parentThumb".to_string(), artist_field("thumb"));
                if let Some(date) = date.filter(|date| date.len() == 10) {
                    row.insert("originallyAvailableAt".to_string(), json!(date));
                }
                row.insert("addedAt".to_string(), json!(stamp));
                row.insert("updatedAt".to_string(), json!(stamp));
                for field in ["Genre", "Style"] {
                    if let Some(list) = tags(album, field) {
                        row.insert(field.to_string(), list);
                    }
                }
                // The agent's `Format` is `{"type": "album"}`; PMS shows it as the tag `Album`.
                let formats: Vec<Value> = album
                    .and_then(|album| album.get("Format"))
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|format| {
                        let kind = format.get("type")?.as_str()?;
                        let mut chars = kind.chars();
                        Some(json!({"tag": chars.next()?.to_uppercase().chain(chars).collect::<String>()}))
                    })
                    .collect();
                if !formats.is_empty() {
                    row.insert("Format".to_string(), Value::Array(formats));
                }
                if let Some(list) = ids(album) {
                    row.insert("Guid".to_string(), list);
                }
                if let Some(list) = tags(album, "Mood") {
                    row.insert("Mood".to_string(), list);
                }
                let poster = text(album, "thumb").or_else(|| display_text(item, Some("album"), "poster").map(Value::from));
                row.insert("_poster_url".to_string(), poster.unwrap_or(Value::Null));
                row.insert("_identity".to_string(), json!(album_identity));
                library(&mut row);
                sort_titled(&mut row);
                let mut value = Value::Object(row);
                number_catalog_tags(std::slice::from_mut(&mut value));
                self.catalog.add(value);
                rk
            }
        };
        let album_row = self.catalog.get(&album_key).cloned().unwrap_or_default();
        let album_field = |field: &str| album_row.get(field).cloned().unwrap_or(Value::Null);

        // -- the track -------------------------------------------------------------------------
        let disc = placed_number(item, "disc").unwrap_or(1);
        let number = placed_number(item, "track");
        let agent_track = match (album, number) {
            (Some(album), Some(number)) => identify::album_track(album, disc, number),
            _ => None,
        };
        let agent_track = agent_track.as_ref();
        let index = number.unwrap_or_else(|| {
            // Unplaced: after the album's last track on this disc, in the order its files arrive.
            self.catalog
                .children_refs(&album_key)
                .iter()
                .filter(|row| row.get("parentIndex").and_then(Value::as_i64).unwrap_or(1) == disc)
                .filter_map(|row| row.get("index").and_then(Value::as_i64))
                .max()
                .unwrap_or(0)
                + 1
        });
        let key = key.unwrap_or_else(|| self.mint_key());
        let rating = key.parse::<i64>().unwrap_or(0);
        let file_name = item.file.rsplit('/').next().unwrap_or_default().to_string();
        let extension = super::scan::extension(&file_name);
        let part_id = rating * 10;
        let mut media = mediainfo::build_media(
            item.media_info.as_ref().unwrap_or(&json!({"format": {}, "streams": []})),
            rating * 100,
            part_id,
            &format!("/library/parts/{part_id}/{stamp}/file.{extension}"),
            &item.file,
            item.size as i64,
            Some(&extension),
        );
        mediainfo::audio_media(&mut media);
        if let Some(hash) = &item.hash {
            media["_hash"] = json!(hash);
        }
        if let (Some(probe), Some(_)) = (&item.probe, &item.media_info) {
            media["_probe"] = json!(probe);
        }
        let title = text(agent_track, "title")
            .map(|title| string_value(&title))
            .or_else(|| display_text(item, None, "title"))
            .unwrap_or_else(|| file_name.rsplit_once('.').map_or(file_name.as_str(), |(stem, _)| stem).to_string());
        let mut row = Map::new();
        row.insert("ratingKey".to_string(), json!(key));
        row.insert("key".to_string(), json!(format!("/library/metadata/{key}")));
        row.insert("parentRatingKey".to_string(), json!(album_key));
        row.insert("grandparentRatingKey".to_string(), json!(artist_key));
        row.insert("guid".to_string(), text(agent_track, "guid").unwrap_or_else(|| json!(format!("local://{key}"))));
        row.insert("parentGuid".to_string(), album_field("guid"));
        row.insert("grandparentGuid".to_string(), artist_field("guid"));
        if let Some(studio) = album_row.get("studio").cloned() {
            row.insert("parentStudio".to_string(), studio);
        }
        row.insert("type".to_string(), json!("track"));
        row.insert("title".to_string(), json!(title));
        row.insert("grandparentKey".to_string(), json!(format!("/library/metadata/{artist_key}")));
        row.insert("parentKey".to_string(), json!(format!("/library/metadata/{album_key}")));
        row.insert("grandparentTitle".to_string(), artist_field("title"));
        row.insert("parentTitle".to_string(), album_field("title"));
        let summary = display_text(item, None, "summary").filter(|_| agent_track.is_none());
        row.insert("summary".to_string(), json!(summary.unwrap_or_default()));
        row.insert("index".to_string(), json!(index));
        row.insert("parentIndex".to_string(), json!(disc));
        if let Some(count) = text(agent_track, "ratingCount") {
            row.insert("ratingCount".to_string(), count);
        }
        if let Some(year) = album_row.get("year").cloned() {
            row.insert("parentYear".to_string(), year);
        }
        // A real PMS's track draws its album's cover and its artist's background.
        row.insert("thumb".to_string(), album_field("thumb"));
        row.insert("art".to_string(), artist_field("art"));
        row.insert("parentThumb".to_string(), album_field("thumb"));
        row.insert("grandparentThumb".to_string(), artist_field("thumb"));
        row.insert("grandparentArt".to_string(), artist_field("art"));
        row.insert("duration".to_string(), media.get("duration").cloned().unwrap_or(json!(0)));
        row.insert("addedAt".to_string(), json!(stamp));
        row.insert("updatedAt".to_string(), json!(stamp));
        row.insert("Media".to_string(), json!([media]));
        for field in ["Genre", "Mood"] {
            if let Some(list) = tags(agent_track, field) {
                row.insert(field.to_string(), list);
            }
        }
        if let Some(list) = ids(agent_track) {
            row.insert("Guid".to_string(), list);
        }
        for (field, value) in [
            ("_source", json!(source.name)),
            ("_sid", json!(item.id)),
            ("_path", json!(item.path)),
            ("_ids", Value::Object(item.ids.clone())),
            ("_group", json!(item.release)),
            ("_release", json!(item.name)),
            ("_placement", placement(item)),
        ] {
            row.insert(field.to_string(), value);
        }
        if let Some(placed) = &item.placed {
            row.insert("_placed".to_string(), placed.clone());
        }
        if let Some(display) = &item.display {
            row.insert("_display".to_string(), Value::Object(display.clone()));
        }
        library(&mut row);
        sort_titled(&mut row);
        let mut value = Value::Object(row);
        number_catalog_tags(std::slice::from_mut(&mut value));
        self.catalog.add(value);
        info!("FEED {} {} -> track {key} on album {album_key} of artist {artist_key}", source.name, item.id);
        key
    }

    /// A track taken off its album before it is placed again on the same key: the row goes, and
    /// an album or artist it leaves with nothing goes with it (their keys retired).
    fn unplace_track(&mut self, key: &str) {
        let Some(row) = self.catalog.remove(key) else {
            return;
        };
        for parent in ["parentRatingKey", "grandparentRatingKey"] {
            let Some(parent) = row.get(parent).map(string_value) else {
                continue;
            };
            if self.catalog.children_refs(&parent).is_empty() && self.catalog.remove(&parent).is_some() {
                self.deleted.insert(parent.clone());
                if let Ok(number) = parent.parse::<i64>() {
                    self.retired_keys.push(number);
                }
            }
        }
    }
}

/// The augmentation a detail read asked for (`asyncAugmentMetadata=1`) is numbered per request, as
/// plexav's PMS numbered them (`/library/metadata/augmentations/1`, `/2`, ...).
static AUGMENTATIONS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// A music row's detail container names its augmentation when the client asks for one (plexav:
/// Plex Web's and Plexamp's `asyncAugmentMetadata=1` reads of an artist, album or track).
pub(super) fn augmentation_key(row: &Value, query: &HashMap<String, String>) -> Option<Value> {
    let music = matches!(row.get("type").and_then(Value::as_str), Some("artist" | "album" | "track"));
    let asked = query.get("asyncAugmentMetadata").is_some_and(|value| is_true_option(value));
    (music && asked).then(|| {
        let number = AUGMENTATIONS.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1;
        json!(format!("/library/metadata/augmentations/{number}"))
    })
}

/// `GET /library/metadata/augmentations/{n}?wait=1` for one a detail read named: what it brought.
/// rex fetches nothing more than its rows hold, so it is the empty container plexav's PMS answered
/// Plexamp for its artist and album (`{"MediaContainer":{"size":0}}`, 2026-10-31). An id never
/// named stays the 404 cycle 52 measured.
pub(super) fn augmentation(id: &str) -> Option<Response> {
    let number = id.parse::<u64>().ok()?;
    (number >= 1 && number <= AUGMENTATIONS.load(std::sync::atomic::Ordering::Relaxed))
        .then(|| Response::json(200, json!({"MediaContainer": {"size": 0}})))
}

/// The artist's album shelves plexav answers in `/related` and `includeRelated=1`, in its order:
/// each keyed on the artist and filled with that artist's albums of the kind. rex knows an album's
/// `Format` (Plex's agent says `album`, `ep`, `single`), never a subformat, so the four subformat
/// shelves stay empty as they were on plexav for four plain albums.
const ARTIST_SHELVES: [(&str, &str, &str); 6] = [
    ("artist.albums.singles", "Singles & EPs", "format=EP,Single"),
    ("artist.albums.live", "Live Albums", "format!=EP,Single&subformat=Live"),
    ("artist.albums.soundtrack", "Soundtracks", "format!=EP,Single&subformat=Soundtrack&subformat!=Live"),
    ("artist.albums.compilation", "Compilations", "format!=EP,Single&subformat=Compilation&subformat!=Live,Soundtrack"),
    ("artist.albums.demo", "Demos", "format!=EP,Single&subformat=Demo&subformat!=Live,Soundtrack,Compilation"),
    ("artist.albums.remix", "Remixes", "format!=EP,Single&subformat=Remix&subformat!=Live,Soundtrack,Compilation,Demo"),
];

impl Runtime {
    /// What an artist's, album's or track's detail read adds (plexav, every client): an album's
    /// `leafCount` and `viewedLeafCount`; an artist's `Preferences` (its one setting, `albumSort`)
    /// and `PopularLeaves` (its five most popular tracks by Plex's `ratingCount`, as the listing
    /// its `key` names answers them). No `Stations`: rex runs no sonic analysis.
    pub(super) fn music_detail(&self, row: &Value, detail: &mut Value, query: &HashMap<String, String>) {
        let include = |name: &str| query.get(name).is_some_and(|value| is_true_option(value));
        let key = key_of(row).unwrap_or_default();
        let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
        let Some(object) = detail.as_object_mut() else {
            return;
        };
        match row.get("type").and_then(Value::as_str) {
            Some("album") => {
                let tracks = self.children_refs(&key);
                let viewed = tracks.iter().filter(|track| track.get("viewCount").is_some_and(truthy)).count();
                let mut out = Map::new();
                for (name, value) in std::mem::take(object) {
                    if name == "addedAt" {
                        out.insert("leafCount".to_string(), json!(tracks.len()));
                        out.insert("viewedLeafCount".to_string(), json!(viewed));
                    }
                    out.insert(name, value);
                }
                *object = out;
            }
            Some("artist") => {
                if query.get("includePreferences").map(String::as_str) == Some("1") {
                    let value = row.pointer("/_prefs/albumSort").map(string_value).unwrap_or_else(|| "-1".to_string());
                    object.insert("Preferences".to_string(), json!({"Setting": [{
                        "id": "albumSort", "label": "Album sorting",
                        "summary": "The order this artist's albums are listed in.",
                        "type": "text", "default": "-1", "value": value, "hidden": false, "advanced": false,
                        "group": "", "enumValues": "-1:Library default|0:Newest first|1:Oldest first|2:By name"}]}));
                }
                if include("includePopularLeaves") {
                    let popular = self.popular_tracks(&key);
                    let rows: Vec<Value> = popular.iter().take(5).map(|track| self.section_listing_row(track)).collect();
                    object.insert("PopularLeaves".to_string(), json!({
                        "size": rows.len(),
                        "key": format!("/library/sections/{section}/all?album.subformat!=Compilation,Live&artist.id={key}&group=title&limit=100&ratingCount>=1&resolveTags=1&sort=ratingCount:desc&type=10"),
                        "Metadata": rows,
                    }));
                }
            }
            _ => {}
        }
    }

    /// A music library's listing of several types at once (Plexamp's home, `type=1,2,9,13,19,21
    /// &sort=addedAt:desc&limit=5`): the rows of each type the library holds, filtered, sorted and
    /// cut as one listing, in the library's listing container (plexav answered its four albums).
    pub(super) fn music_typed_listing(&self, section: &str, types: &str, query: &HashMap<String, String>, pairs: &[(String, String)]) -> Option<Response> {
        let numbers: Vec<i64> = types.split(',').map(|part| part.trim().parse::<i64>().ok()).collect::<Option<Vec<_>>>()?;
        if !self.music_section(section) {
            return None;
        }
        let mut rows: Vec<Value> = numbers.iter().filter(|number| (8..=10).contains(*number)).flat_map(|number| self.choose_rows(Some(section), Some(*number))).collect();
        let filters: Vec<(String, String)> = pairs
            .iter()
            .filter(|(name, _)| !matches!(name.as_str(), "type" | "sort" | "limit" | "group" | "having" | "resolveTags") && !name.starts_with("include") && !name.starts_with("exclude") && !name.starts_with("X-Plex"))
            .cloned()
            .collect();
        rows = self.filter_rows(rows, &filters).ok()?;
        rows = self.sort_rows(rows, query.get("sort").map(String::as_str)).ok()?;
        if let Some(limit) = query.get("limit").and_then(|limit| limit.parse::<usize>().ok()) {
            rows.truncate(limit);
        }
        let shown: Vec<Value> = rows.iter().map(|row| self.section_listing_row(row)).collect();
        let mut fields = self.listing_fields(section, "artist", "artist", "All Artists");
        fields.insert("Metadata".to_string(), Value::Array(shown));
        Some(Response::json(200, self.container(fields, true)))
    }

    /// An artist's or album's plays are its tracks' (plexav after Plex for Android played In
    /// Rainbows: the album and its artist carried `viewCount`, `skipCount` and `lastViewedAt`),
    /// each named only once there is one.
    pub(super) fn music_play_counts(&self, key: &str, object: &mut Map<String, Value>) {
        let tracks = self.leaves_refs(key);
        let sum = |field: &str| tracks.iter().filter_map(|track| track.get(field).and_then(Value::as_i64)).sum::<i64>();
        let last = tracks.iter().filter_map(|track| track.get("lastViewedAt").and_then(Value::as_i64)).max();
        for (field, value) in [("viewCount", sum("viewCount")), ("skipCount", sum("skipCount")), ("lastViewedAt", last.unwrap_or(0))] {
            if value > 0 {
                object.insert(field.to_string(), json!(value));
            } else {
                object.shift_remove(field);
            }
        }
    }

    /// A music listing with no `sort` in plexav's order (its `firstCharacter` sorts name it):
    /// albums by artist and album sort title, tracks by artist, album, year, disc and number.
    pub(super) fn music_listing_order<R: Row>(&self, section: &str, type_number: Option<i64>, query: &HashMap<String, String>, mut rows: Vec<R>) -> Vec<R> {
        if query.contains_key("sort") || !matches!(type_number, Some(9 | 10)) || !self.music_section(section) {
            return rows;
        }
        let sorted = |key: Option<String>| {
            key.and_then(|key| self.catalog.lazy_get(&key)).map(|row| crate::hierarchy::stored_sort_title(&row).to_lowercase()).unwrap_or_default()
        };
        let number = |row: &R, field: &str| row.get(field).and_then(Value::as_i64).unwrap_or(0);
        rows.sort_by_cached_key(|row| {
            let own = crate::hierarchy::stored_sort_title(row).to_lowercase();
            if type_number == Some(9) {
                (sorted(row.get("parentRatingKey").map(string_value)), own, 0, 0, 0)
            } else {
                let album = row.get("parentRatingKey").map(string_value);
                let year = album.as_ref().and_then(|key| self.catalog.lazy_get(key)).and_then(|row| row.get("year").and_then(Value::as_i64)).unwrap_or(0);
                (sorted(row.get("grandparentRatingKey").map(string_value)), sorted(album), year, number(row, "parentIndex"), number(row, "index"))
            }
        });
        rows
    }

    /// A child list's container under an artist or album, as plexav answers `/children`: an
    /// artist's albums are `viewGroup` album at 65592; an album's tracks are `track` at 65593,
    /// presented as the artist's (`title1`, `grandparent*`).
    pub(super) fn music_children_fields(&self, parent: &Value, fields: &mut Map<String, Value>) {
        if parent.get("type").and_then(Value::as_str) == Some("artist") {
            fields.insert("viewGroup".to_string(), json!("album"));
            fields.insert("viewMode".to_string(), json!(65592));
            return;
        }
        fields.insert("viewGroup".to_string(), json!("track"));
        fields.insert("viewMode".to_string(), json!(65593));
        let artist_key = parent.get("parentRatingKey").map(string_value).unwrap_or_default();
        if let Some(artist) = self.catalog.get(&artist_key) {
            fields.insert("title1".to_string(), artist.get("title").cloned().unwrap_or(Value::Null));
            fields.insert("grandparentRatingKey".to_string(), artist_key.parse::<i64>().map(Value::from).unwrap_or_else(|_| json!(artist_key)));
            fields.insert("grandparentThumb".to_string(), artist.get("thumb").cloned().unwrap_or(Value::Null));
            fields.insert("grandparentTitle".to_string(), artist.get("title").cloned().unwrap_or(Value::Null));
        }
        if let Some(year) = parent.get("year") {
            fields.insert("parentYear".to_string(), year.clone());
        }
    }

    /// An artist's tracks Plex's agent counts listeners for, the most played first, one per title.
    fn popular_tracks(&self, artist: &str) -> Vec<Value> {
        let mut tracks: Vec<Value> = self
            .leaves(artist)
            .into_iter()
            .filter(|track| track.get("ratingCount").and_then(Value::as_i64).unwrap_or(0) >= 1)
            .collect();
        tracks.sort_by_key(|track| std::cmp::Reverse(track.get("ratingCount").and_then(Value::as_i64).unwrap_or(0)));
        let mut seen = HashSet::new();
        tracks.retain(|track| seen.insert(track.get("title").map(string_value).unwrap_or_default().to_lowercase()));
        tracks
    }

    /// An artist's related hubs (plexav's six album shelves); an album or track has none.
    pub(super) fn music_related(&self, row: &Value) -> Option<Vec<Value>> {
        match row.get("type").and_then(Value::as_str) {
            Some("artist") => {}
            Some("album" | "track") => return Some(Vec::new()),
            _ => return None,
        }
        let key = key_of(row).unwrap_or_default();
        let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
        let albums = self.children(&key);
        let format = |album: &Value| {
            album.get("Format").and_then(Value::as_array).into_iter().flatten().filter_map(|tag| tag.get("tag").and_then(Value::as_str).map(str::to_lowercase)).collect::<Vec<_>>()
        };
        Some(
            ARTIST_SHELVES
                .iter()
                .map(|(identifier, title, filter)| {
                    let rows: Vec<Value> = if *identifier == "artist.albums.singles" {
                        albums.iter().filter(|album| format(album).iter().any(|f| f == "ep" || f == "single")).map(|album| self.section_listing_row(album)).collect()
                    } else {
                        Vec::new()
                    };
                    let mut hub = Map::new();
                    hub.insert("key".to_string(), json!(format!(
                        "/library/sections/{section}/all?artist.id={key}&type=9&{filter}&resolveTags=1&sort=year:desc,originallyAvailableAt:desc,artist.titleSort:desc,album.titleSort,album.index,album.id,album.originallyAvailableAt"
                    )));
                    hub.insert("title".to_string(), json!(title));
                    hub.insert("type".to_string(), json!("album"));
                    hub.insert("hubIdentifier".to_string(), json!(identifier));
                    hub.insert("context".to_string(), json!(format!("hub.{identifier}")));
                    hub.insert("size".to_string(), json!(rows.len()));
                    hub.insert("more".to_string(), json!(false));
                    hub.insert("style".to_string(), json!("shelf"));
                    if !rows.is_empty() {
                        hub.insert("Metadata".to_string(), Value::Array(rows));
                    }
                    Value::Object(hub)
                })
                .collect(),
        )
    }

    /// A music library's Recommended shelves filled from its rows, as plexav's held four albums
    /// (Plex Web, Plex for Android and Plexamp, before and after plays): Recently Added's albums,
    /// newest first; Recently Played's artists; a Top Albums shelf for a period of the library's
    /// album years; after a play, More by the last played artist and Most Played this month. No
    /// Stations shelf in a library that holds music: rex runs no sonic analysis, and an empty
    /// library keeps the shelves measured on one (`music_section_hubs`).
    pub(super) fn fill_music_hubs(&self, section: &str, hubs: &mut Vec<Value>, query: &HashMap<String, String>) {
        let count = query.get("count").and_then(|count| count.parse::<usize>().ok()).unwrap_or(12);
        let mut albums: Vec<Value> = self.choose_rows(Some(section), Some(9));
        if albums.is_empty() {
            return;
        }
        let number = |row: &Value, field: &str| row.get(field).and_then(Value::as_i64).unwrap_or(0);
        albums.sort_by_key(|album| (std::cmp::Reverse(number(album, "addedAt")), key_of(album).and_then(|key| key.parse::<i64>().ok())));
        let tracks: Vec<Value> = self.choose_rows(Some(section), Some(10));
        let played: Vec<&Value> = {
            let mut played: Vec<&Value> = tracks.iter().filter(|track| number(track, "lastViewedAt") > 0).collect();
            played.sort_by_key(|track| std::cmp::Reverse(number(track, "lastViewedAt")));
            played
        };
        let fill = |hub: &mut Value, rows: Vec<Value>| {
            let total = rows.len();
            // A shelf's rows name their library, which a section listing's leave to the container.
            let rows: Vec<Value> = rows
                .into_iter()
                .take(count)
                .map(|row| {
                    let mut shown = self.section_listing_row(&row);
                    if let Some(object) = shown.as_object_mut() {
                        for field in ["librarySectionTitle", "librarySectionID", "librarySectionKey"] {
                            if let Some(value) = row.get(field) {
                                object.insert(field.to_string(), value.clone());
                            }
                        }
                    }
                    shown
                })
                .collect();
            let keys: Vec<String> = rows.iter().filter_map(key_of).collect();
            if let Some(object) = hub.as_object_mut() {
                if !keys.is_empty() {
                    let mut out = Map::new();
                    out.insert("hubKey".to_string(), json!(format!("/library/metadata/{}", keys.join(","))));
                    out.extend(std::mem::take(object));
                    *object = out;
                }
                object.insert("size".to_string(), json!(rows.len()));
                object.insert("more".to_string(), json!(total > rows.len()));
                if !rows.is_empty() {
                    object.insert("Metadata".to_string(), Value::Array(rows));
                }
            }
        };
        let identifier = |hub: &Value| hub.get("context").and_then(Value::as_str).unwrap_or_default().to_string();
        hubs.retain(|hub| identifier(hub) != "hub.music.stations");
        let base = format!("/library/sections/{section}/all");
        let shelf = |stem: &str, title: String, key: String, extra: &[(&str, Value)]| {
            let mut hub = Map::new();
            hub.insert("key".to_string(), json!(key));
            hub.insert("title".to_string(), json!(title));
            hub.insert("type".to_string(), json!("album"));
            hub.insert("hubIdentifier".to_string(), json!(format!("{stem}.{section}")));
            hub.insert("context".to_string(), json!(format!("hub.{stem}")));
            hub.insert("size".to_string(), json!(0));
            hub.insert("more".to_string(), json!(false));
            hub.insert("style".to_string(), json!("shelf"));
            for (name, value) in extra {
                hub.insert(name.to_string(), value.clone());
            }
            Value::Object(hub)
        };
        // Top Albums: a decade or a year of the library's albums, drawn once per process as the
        // other sampled shelves are; its albums are those with a track a person rated.
        static DRAW: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
        let mut years: Vec<i64> = albums.iter().map(|album| number(album, "year")).filter(|year| *year > 0).collect();
        years.sort_unstable();
        years.dedup();
        let top = (!years.is_empty()).then(|| {
            let draw = *DRAW.get_or_init(|| (now() as usize).wrapping_mul(2654435761) >> 7) % (years.len() * 2);
            let year = years[draw / 2];
            let (range, words, within): (String, String, Box<dyn Fn(i64) -> bool>) = if draw % 2 == 0 {
                let decade = year - year % 10;
                (format!("year>={decade}&year<={}", decade + 9), format!("{decade}s"), Box::new(move |y| y / 10 == decade / 10))
            } else {
                (format!("year={year}"), year.to_string(), Box::new(move |y| y == year))
            };
            let mut rated: Vec<(i64, Value)> = albums
                .iter()
                .filter(|album| within(number(album, "year")))
                .filter_map(|album| {
                    let best = self.children(&key_of(album)?).iter().map(|track| number(track, "userRating")).max().unwrap_or(0);
                    (best >= 1).then(|| (best, album.clone()))
                })
                .collect();
            rated.sort_by_key(|(best, _)| std::cmp::Reverse(*best));
            let mut hub = shelf(
                "music.top.period",
                format!("Top Albums from {words}"),
                format!("{base}?{range}&type=9&sort=track.userRating:desc&track.userRating>=1"),
                &[],
            );
            fill(&mut hub, rated.into_iter().map(|(_, album)| album).collect());
            hub
        });
        let last_artist = played.first().and_then(|track| track.get("grandparentRatingKey")).map(string_value);
        let position = |hubs: &Vec<Value>, context: &str| hubs.iter().position(|hub| identifier(hub) == context);
        for hub in hubs.iter_mut() {
            match identifier(hub).as_str() {
                "hub.music.recent.added" => fill(hub, albums.clone()),
                "hub.music.recent.played" => {
                    let mut artists: Vec<String> = Vec::new();
                    for track in &played {
                        let artist = track.get("grandparentRatingKey").map(string_value).unwrap_or_default();
                        if !artists.contains(&artist) {
                            artists.push(artist);
                        }
                    }
                    fill(hub, artists.iter().filter_map(|key| self.catalog.get(key).cloned()).collect());
                }
                _ => {}
            }
        }
        // Plexamp's On This Day (`includeAnniversaryReleases=1`): albums released on today's date.
        if query.get("includeAnniversaryReleases").is_some_and(|value| is_true_option(value)) {
            let today = local_month_day();
            let rows: Vec<Value> = albums
                .iter()
                .filter(|album| album.get("originallyAvailableAt").and_then(Value::as_str).is_some_and(|date| date.get(5..) == Some(today.as_str())))
                .cloned()
                .collect();
            let mut hub = shelf("music.anniversary.releases", "On This Day".to_string(), "/library/metadata/".to_string(), &[]);
            if let Some(object) = hub.as_object_mut() {
                object.shift_remove("key");
                let mut out = Map::from_iter([("key".to_string(), json!(format!("/library/metadata/{}", rows.iter().filter_map(key_of).collect::<Vec<_>>().join(","))))]);
                out.extend(std::mem::take(object));
                *object = out;
            }
            fill(&mut hub, rows);
            let at = position(hubs, "hub.music.recent.added").map_or(hubs.len(), |at| at + 1);
            hubs.insert(at, hub);
        }
        let mut after = position(hubs, "hub.recent.library.playlists").or_else(|| position(hubs, "hub.music.recent.added")).map_or(0, |at| at + 1);
        if let Some(artist) = last_artist.as_deref().and_then(|key| self.catalog.get(key)) {
            let key = key_of(artist).unwrap_or_default();
            let mut theirs = self.children(&key);
            theirs.sort_by_key(|album| number(album, "year"));
            let mut hub = shelf(
                "music.recent.artist",
                format!("More by {}", artist.get("title").map(string_value).unwrap_or_default()),
                format!("{base}?type=9&artist.id={key}&sort=year"),
                &[],
            );
            fill(&mut hub, theirs);
            hubs.insert(after, hub);
            after += 1;
        }
        if let Some(hub) = top {
            hubs.insert(after, hub);
            after += 1;
        }
        // More in a genre: the last played album's first genre, over the library's albums.
        let genre = played
            .first()
            .and_then(|track| track.get("parentRatingKey"))
            .map(string_value)
            .and_then(|album| self.catalog.get(&album))
            .and_then(|album| album.pointer("/Genre/0").cloned());
        if let Some(genre) = genre {
            let name = genre.get("tag").map(string_value).unwrap_or_default();
            let id = genre.get("id").map(string_value).unwrap_or_default();
            let theirs: Vec<Value> = albums
                .iter()
                .filter(|album| album.get("Genre").and_then(Value::as_array).is_some_and(|tags| tags.iter().any(|tag| tag.get("tag").map(string_value).as_deref() == Some(name.as_str()))))
                .cloned()
                .collect();
            let mut hub = shelf("music.recent.genre", format!("More in {name}"), format!("{base}?type=9&genre={id}"), &[("random", json!(true))]);
            fill(&mut hub, theirs);
            hubs.insert(after, hub);
            after += 1;
        }
        if !played.is_empty() {
            // Most Played this month: the albums of the tracks played since the month began.
            let start = month_start();
            let mut plays: Vec<(String, i64)> = Vec::new();
            for track in played.iter().filter(|track| number(track, "lastViewedAt") >= start) {
                let album = track.get("parentRatingKey").map(string_value).unwrap_or_default();
                match plays.iter_mut().find(|(key, _)| *key == album) {
                    Some((_, count)) => *count += number(track, "viewCount").max(1),
                    None => plays.push((album, number(track, "viewCount").max(1))),
                }
            }
            plays.sort_by_key(|(_, count)| std::cmp::Reverse(*count));
            let mut hub = shelf("music.popular", format!("Most Played in {}", month_name()), format!("/hubs/sections/{section}/popular?monthsAgo=0"), &[]);
            fill(&mut hub, plays.iter().filter_map(|(key, _)| self.catalog.get(key).cloned()).collect());
            if hub.get("size").and_then(Value::as_i64).unwrap_or(0) > 0 {
                hubs.insert(after, hub);
            }
        }
    }

    /// A track's timeline as plexav's PMS answered Plex Web's queue (2026-10-31): `ignore` while it
    /// plays; stopped with `continuing=1` (the client moved on) before its end it is a skip, and
    /// stopped past half its length it is a play -- `complete`, `viewCount` + 1 -- so a track left
    /// at 25 of 45 s counted both, and one left at 34 ms neither. Where between the two the play
    /// starts to count was not measured: half is rex's line.
    pub(super) fn music_timeline(&mut self, key: &str, state: &str, offset: i64, total: i64, query: &HashMap<String, String>, device: i64) -> Option<&'static str> {
        if state != "stopped" || self.catalog.get(key)?.get("type").and_then(Value::as_str) != Some("track") || total <= 0 {
            return None;
        }
        let continuing = query.get("continuing").is_some_and(|value| is_true_option(value));
        if continuing && offset > 0 && offset < total.saturating_mul(9) / 10 {
            let skips = self.view_state(key, "skipCount").and_then(|value| value.as_i64()).unwrap_or(0);
            self.set_view_state(key, "skipCount", Some(json!(skips + 1)));
            self.set_view_state(key, "lastSkippedAt", Some(json!(now())));
            self.dirty = true;
        }
        if offset.saturating_mul(2) >= total {
            self.scrobble_key_on(key, true, None, device);
            return Some("complete");
        }
        None
    }

    /// `GET /music/:/transcode/universal/decision`: a track direct plays, as plexav's PMS answered
    /// Plex Web (`protocol=dash`, `musicBitrate=320`) and Plex for Android (`protocol=hls`) for its
    /// FLAC and MP3 tracks -- `mdeDecisionCode` 1000, `Direct play OK.`, the Part `decision`
    /// `directplay` and its Stream `location` `direct`, the attributes in the converted XML's order
    /// (alphabetical, the decision's own ones last). rex makes no music conversion: every track is
    /// answered as its file plays.
    pub(super) fn route_music_decision(&mut self, query: &HashMap<String, String>, _request: &Request) -> Response {
        let path = query.get("path").map(String::as_str).unwrap_or_default();
        let key = path.strip_prefix("/library/metadata/").map(|tail| tail.split(['/', '?']).next().unwrap_or_default()).unwrap_or_default();
        let Some(row) = self.catalog.get(key).cloned().filter(|row| row.get("type").and_then(Value::as_str) == Some("track")) else {
            return Response::error(400, "rex: bad request");
        };
        // The client's session, as the video decision keys it: the timeline's `Bandwidths` follow it.
        let headers = &_request.headers;
        let session = query
            .get("X-Plex-Session-Identifier")
            .or_else(|| headers.get("x-plex-session-identifier"))
            .or_else(|| query.get("X-Plex-Client-Identifier"))
            .or_else(|| headers.get("x-plex-client-identifier"))
            .cloned()
            .unwrap_or_default();
        self.decided_sessions.insert((sessions::account_session_id(self.viewer.id, &session), key.to_string()));
        let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
        let section_row = self.section(&section).cloned().unwrap_or_default();
        let detail = self.detail_row(&row);
        let sorted = |object: &Map<String, Value>| -> Map<String, Value> {
            let mut names: Vec<&String> = object.keys().collect();
            names.sort();
            names.into_iter().map(|name| (name.clone(), object[name].clone())).collect()
        };
        let as_text = |value: &Value| json!(string_value(value));
        let mut track = Map::new();
        if let Some(object) = detail.as_object() {
            for (name, value) in sorted(object) {
                if value.is_object() || value.is_array() || name == "summary" && value.as_str() == Some("") {
                    continue;
                }
                track.insert(name, value);
            }
            if let Some(skips) = track.get("skipCount").cloned() {
                track.insert("skipCount".to_string(), as_text(&skips));
            }
            let index = query.get("mediaIndex").and_then(|index| index.parse::<usize>().ok()).unwrap_or(0);
            let media = object.get("Media").and_then(Value::as_array).and_then(|media| media.get(index).or(media.first())).cloned();
            if let Some(media) = media.as_ref().and_then(Value::as_object) {
                let mut shaped = Map::new();
                for (name, value) in sorted(media) {
                    if name == "Part" || name.starts_with('_') {
                        continue;
                    }
                    shaped.insert(name.clone(), match name.as_str() {
                        "id" => as_text(&value),
                        "hasVoiceActivity" => json!(if value.as_bool() == Some(true) { "1" } else { "0" }),
                        _ => value,
                    });
                }
                shaped.insert("selected".to_string(), json!(true));
                let part_index = query.get("partIndex").and_then(|index| index.parse::<usize>().ok()).unwrap_or(0);
                let parts = media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default();
                let part = parts.get(part_index).or(parts.first()).and_then(Value::as_object).cloned().unwrap_or_default();
                let mut part_out = Map::new();
                for (name, value) in sorted(&part) {
                    if name == "Stream" || name.starts_with('_') || matches!(name.as_str(), "accessible" | "exists") {
                        continue;
                    }
                    part_out.insert(name.clone(), if name == "id" { as_text(&value) } else { value });
                }
                part_out.insert("decision".to_string(), json!("directplay"));
                part_out.insert("selected".to_string(), json!(true));
                let streams: Vec<Value> = part
                    .get("Stream")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_object)
                    .map(|stream| {
                        let mut out = Map::new();
                        for (name, value) in sorted(stream) {
                            if name.starts_with('_') {
                                continue;
                            }
                            out.insert(name.clone(), match name.as_str() {
                                "id" => as_text(&value),
                                "canNormalizeLoudness" => json!(if value.as_bool() == Some(true) { "1" } else { "0" }),
                                _ => value,
                            });
                        }
                        out.insert("location".to_string(), json!("direct"));
                        Value::Object(out)
                    })
                    .collect();
                part_out.insert("Stream".to_string(), Value::Array(streams));
                shaped.insert("Part".to_string(), json!([part_out]));
                track.insert("Media".to_string(), json!([shaped]));
            }
            for field in ["Genre", "Mood"] {
                let tags: Vec<Value> = object
                    .get(field)
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_object)
                    .map(|tag| Value::Object(sorted(tag).into_iter().map(|(name, value)| {
                        let value = if name == "id" { as_text(&value) } else { value };
                        (name, value)
                    }).collect()))
                    .collect();
                if !tags.is_empty() {
                    track.insert(field.to_string(), Value::Array(tags));
                }
            }
        }
        let fields = Map::from_iter([
            ("size".to_string(), json!(1)),
            ("allowSync".to_string(), json!("1")),
            ("identifier".to_string(), json!("com.plexapp.plugins.library")),
            ("librarySectionID".to_string(), json!(section)),
            ("librarySectionTitle".to_string(), section_row.get("title").cloned().unwrap_or(Value::Null)),
            ("librarySectionUUID".to_string(), section_row.get("uuid").cloned().unwrap_or(Value::Null)),
            ("mdeDecisionCode".to_string(), json!(1000)),
            ("mdeDecisionText".to_string(), json!("Direct play OK.")),
            ("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX)),
            ("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION.to_string())),
            ("resourceSession".to_string(), json!(session)),
            ("Metadata".to_string(), json!([track])),
        ]);
        info!("DECISION direct play (music) for path={path}");
        Response::json(200, json!({"MediaContainer": fields}))
    }
}

/// `titleSort` after `title` when the sort title differs, as the catalog load writes it
/// (`hierarchy::sort_title`): plexav's `The Four of Us Are Dying` sorts as `Four of Us Are Dying`.
fn sort_titled(row: &mut Map<String, Value>) {
    let title = row.get("title").map(string_value).unwrap_or_default();
    let sort = hierarchy::sort_title(&title);
    if sort.is_empty() || sort == title {
        return;
    }
    let mut out = Map::new();
    for (name, value) in std::mem::take(row) {
        let after = name == "title";
        out.insert(name, value);
        if after {
            out.insert("titleSort".to_string(), json!(sort));
        }
    }
    *row = out;
}

/// Today's month and day, `MM-DD`, in the server's local time.
fn local_month_day() -> String {
    let (_, month, day) = civil(now());
    format!("{month:02}-{day:02}")
}

/// The first second of this month (UTC; the shelf's edge is a day either way).
fn month_start() -> i64 {
    let stamp = now();
    let (_, _, day) = civil(stamp);
    stamp - stamp.rem_euclid(86_400) - (day as i64 - 1) * 86_400
}

fn month_name() -> &'static str {
    const MONTHS: [&str; 12] = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    MONTHS[(civil(now()).1 as usize).saturating_sub(1) % 12]
}

/// A Unix time's civil date (UTC): year, month, day.
fn civil(stamp: i64) -> (i64, u32, u32) {
    let days = stamp.div_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(month <= 2), month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates() {
        assert_eq!(civil(0), (1970, 1, 1));
        assert_eq!(civil(1_793_454_632), (2026, 10, 31));
    }

    #[test]
    fn audio_files_are_known_by_their_container_alone() {
        assert!(is_audio("music/Radiohead/In Rainbows (2007)/01 - 15 Step.flac"));
        assert!(is_audio("x/Y.MP3"));
        assert!(!is_audio("movies/Sintel.mkv"));
        assert!(!is_audio("music/no-extension"));
    }
}
