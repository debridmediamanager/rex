//! Live TV: the live channels sources push, as the one DVR a Plex client's guide reads.
//!
//! A live channel is a pushed `.m3u8` whose probe has no duration (`docs/feed-api.md`); one
//! pushed with a `channel` (its number, name and guide) is also a channel of a DVR, as an
//! HDHomeRun's channels are with an XMLTV guide. Everything here was measured on plexav, a
//! claimed 1.43.4 PMS with an HDHomeRun of three channels and an XMLTV guide, for Plex Web
//! 4.163.0's and Plex for Android 2026.21.0's own requests (findings, "Live TV, 2026-10-31";
//! `tests/fixtures/livetv/`). Its owner has no Plex Pass, so PMS offers its guide and live
//! playback but no recording: the provider has no `subscribe` feature, which is what hides
//! Record in Plex Web (`showRecord` needs `features.subscribe.flavor === "record"`). rex records
//! nothing, so it answers that shape.
//!
//! rex identifies nothing here either: a programme is what its source says (`type`, `show`,
//! `season`, `episode`), and its guid is built from those words as PMS builds XMLTV's
//! (`tv.plex.xmltv://movie/<title>`, `.../episode/<show>/<season>/<episode>`). A channel row stays
//! a row of its library; watching it from the guide plays that row's own live conversion
//! (`universal.rs`), under the session key a tune hands out.

use super::*;
use super::sessions::plex_value;
use crate::catalog::By;

/// The DVR's key, its EPG provider and the provider's id: plexav's values, the ones PMS gives
/// the first DVR it sets up (device 1, DVR 2, provider 3). Clients key on the identifier.
const DVR: &str = "2";
const PROVIDER_ID: i64 = 3;
const IDENTIFIER: &str = "tv.plex.providers.epg.xmltv";
const PROVIDER: &str = "tv.plex.providers.epg.xmltv:2";
pub(super) const PREFIX: &str = "/tv.plex.providers.epg.xmltv:2";
/// Measured: a tune's capture media is offered for five minutes from the tune (`endsAt`).
const CAPTURE_WINDOW: i64 = 300;
/// A tuned session nobody has asked about for this long is forgotten.
const SESSION_IDLE: i64 = 6 * 3600;

/// The guide's clock: the wall clock, or `REX_LIVETV_NOW` (epoch seconds), which only the suites
/// set, to answer a reference's recorded requests at the moment they were recorded.
fn now() -> i64 {
    std::env::var("REX_LIVETV_NOW").ok().and_then(|value| value.trim().parse().ok()).unwrap_or_else(super::now)
}

/// What a tune started: its channel's row, the airing it began on, and who asked.
#[derive(Clone, Debug)]
pub(super) struct Tuned {
    row: String,
    client: String,
    at: i64,
    seen: i64,
    media: i64,
    /// The session's `grabber.grab` activity, as `/activities` lists it while the session runs.
    activity: Value,
}

/// The tuned sessions, the subscriptions numbered for them, and each account's channels tuned
/// most recently (What's On's Recent Channels; kept while rex runs).
#[derive(Default, Debug)]
pub(super) struct Sessions {
    tuned: HashMap<String, Tuned>,
    subscriptions: i64,
    recent: HashMap<i64, Vec<String>>,
}

/// One channel of the lineup.
struct Channel {
    row: Value,
    key: String,
    number: String,
    title: String,
    call_sign: String,
    /// `channelID` and the Channel tag's id: its place in the lineup, from 1.
    id: i64,
}

/// One airing of a programme on a channel.
struct Airing {
    id: i64,
    channel: usize,
    begins: i64,
    ends: i64,
    programme: usize,
}

/// A programme: every airing of one guid.
struct Programme {
    guid: String,
    episode: bool,
    title: String,
    summary: String,
    show: String,
    season: i64,
    index: i64,
    categories: Vec<String>,
    thumb: Option<String>,
    /// Measured: every airing of an episode is a premiere once one of them is pushed `new`
    /// (plexav's Firefly, `<new/>` on one airing in three, shows `premiere` on all of them).
    premiere: bool,
    /// Its airings, by start.
    airings: Vec<usize>,
}

/// The lineup and every pushed airing, read off the channel rows once a request.
struct Guide {
    channels: Vec<Channel>,
    airings: Vec<Airing>,
    programmes: Vec<Programme>,
    /// Every category any programme names, in the order first named: a Genre tag's id is its
    /// place after the channels' ids, as PMS numbers its tags in one table.
    genres: Vec<String>,
    added: i64,
    now: i64,
}

/// A row's three shapes of a guid: the guid itself, its `ratingKey` and its `key`.
fn rating_key(guid: &str) -> String {
    // Measured: every byte but a letter or a digit is escaped, `.` included
    // (`tv%2Eplex%2Exmltv%3A%2F%2Fmovie%2FBig%2520Buck%2520Bunny`).
    let mut out = String::new();
    for byte in guid.bytes() {
        if byte.is_ascii_alphanumeric() {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// A title as it sits inside a guid: `Big Buck Bunny` is `Big%20Buck%20Bunny`.
fn guid_part(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

fn item_key(guid: &str) -> String {
    format!("{PREFIX}/metadata/{}", rating_key(guid))
}

fn show_guid(show: &str) -> String {
    format!("tv.plex.xmltv://show/{}", guid_part(show))
}

fn season_guid(show: &str, season: i64) -> String {
    format!("tv.plex.xmltv://season/{}/{season}", guid_part(show))
}

/// Epoch seconds from a guide time: a number, or ISO 8601 with a `Z` or `+hh:mm` offset (a
/// time with neither is read as UTC).
fn guide_time(value: &Value) -> Option<i64> {
    if let Some(number) = value.as_i64() {
        return Some(number);
    }
    let text = value.as_str()?.trim();
    if let Ok(number) = text.parse::<i64>() {
        return Some(number);
    }
    let bytes = text.as_bytes();
    if bytes.len() < 16 || bytes[4] != b'-' || bytes[7] != b'-' || !matches!(bytes[10], b'T' | b' ') || bytes[13] != b':' {
        return None;
    }
    let number = |range: std::ops::Range<usize>| text.get(range)?.parse::<i64>().ok();
    let (year, month, day, hour, minute) = (number(0..4)?, number(5..7)?, number(8..10)?, number(11..13)?, number(14..16)?);
    let mut rest = &text[16..];
    let mut second = 0;
    if let Some(after) = rest.strip_prefix(':') {
        second = after.get(0..2)?.parse::<i64>().ok()?;
        rest = &after[2..];
        if let Some(fraction) = rest.strip_prefix('.') {
            rest = fraction.trim_start_matches(|c: char| c.is_ascii_digit());
        }
    }
    let offset = match rest {
        "" | "Z" | "z" => 0,
        zone => {
            let sign = match zone.as_bytes()[0] {
                b'+' => 1,
                b'-' => -1,
                _ => return None,
            };
            let digits = zone[1..].replace(':', "");
            if digits.len() != 4 {
                return None;
            }
            sign * (digits[0..2].parse::<i64>().ok()? * 3600 + digits[2..4].parse::<i64>().ok()? * 60)
        }
    };
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) || hour > 23 || minute > 59 || second > 60 {
        return None;
    }
    Some(super::edit::days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second - offset)
}

/// A pushed `channel`, checked and kept as the row's `_channel` (`docs/feed-api.md`, "Live TV"):
/// its `number`, `title`, `call_sign` and `guide`, each programme's times as epoch seconds and
/// its `type` one of `episode` (with its `show`, `season` and `episode`) or `movie`.
pub(super) fn channel_field(value: &Value) -> Result<Value, String> {
    let Some(object) = value.as_object() else {
        return Err("channel must be an object".to_string());
    };
    let text = |object: &Map<String, Value>, name: &str| {
        object.get(name).and_then(Value::as_str).map(str::trim).filter(|text| !text.is_empty()).map(str::to_string)
    };
    let number = match object.get("number") {
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::String(number)) => number.trim().to_string(),
        _ => String::new(),
    };
    // It is a path segment (`/livetv/dvrs/2/channels/2.1/tune`) and a query value.
    if number.is_empty() || !number.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_')) {
        return Err("channel.number must be letters, digits, '.', '-' or '_' (\"2.1\")".to_string());
    }
    let mut kept = Map::new();
    kept.insert("number".to_string(), json!(number));
    for name in ["title", "call_sign"] {
        if let Some(value) = text(object, name) {
            kept.insert(name.to_string(), json!(value));
        }
    }
    let entries = match object.get("guide") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(entries)) => entries.clone(),
        Some(_) => return Err("channel.guide must be a list".to_string()),
    };
    let mut guide = Vec::new();
    for (at, entry) in entries.iter().enumerate() {
        let named = |reason: &str| format!("channel.guide[{at}]: {reason}");
        let Some(entry) = entry.as_object() else {
            return Err(named("must be an object"));
        };
        let (Some(start), Some(end)) = (entry.get("start").and_then(guide_time), entry.get("end").and_then(guide_time)) else {
            return Err(named("start and end must be epoch seconds or ISO 8601 times"));
        };
        if end <= start {
            return Err(named("end must be after start"));
        }
        let Some(title) = text(entry, "title") else {
            return Err(named("no title"));
        };
        let mut programme = Map::new();
        programme.insert("start".to_string(), json!(start));
        programme.insert("end".to_string(), json!(end));
        programme.insert("title".to_string(), json!(title));
        if let Some(summary) = entry.get("summary").and_then(Value::as_str) {
            programme.insert("summary".to_string(), json!(summary));
        }
        // Measured: XMLTV's programmes with an episode number are episodes, every other one
        // (news, a match) a movie, whatever its category.
        let numbers = (
            text(entry, "show"),
            entry.get("season").and_then(|n| n.as_i64().or_else(|| n.as_str()?.trim().parse().ok())).filter(|n| *n >= 0),
            entry.get("episode").and_then(|n| n.as_i64().or_else(|| n.as_str()?.trim().parse().ok())).filter(|n| *n >= 0),
        );
        match (text(entry, "type").as_deref(), numbers) {
            (Some("episode"), (Some(show), Some(season), Some(episode))) => {
                programme.insert("type".to_string(), json!("episode"));
                programme.insert("show".to_string(), json!(show));
                programme.insert("season".to_string(), json!(season));
                programme.insert("episode".to_string(), json!(episode));
            }
            (Some("episode"), _) => return Err(named("an episode needs show, season and episode")),
            _ => {
                programme.insert("type".to_string(), json!("movie"));
            }
        }
        match entry.get("categories") {
            None | Some(Value::Null) => {}
            Some(Value::Array(names)) => {
                let names = names.iter().filter_map(Value::as_str).map(str::trim).filter(|name| !name.is_empty()).collect::<Vec<_>>();
                if !names.is_empty() {
                    programme.insert("categories".to_string(), json!(names));
                }
            }
            Some(_) => return Err(named("categories must be a list of names")),
        }
        if let Some(thumb) = text(entry, "thumb") {
            if !(thumb.starts_with("http://") || thumb.starts_with("https://")) {
                return Err(named("thumb must be an http(s) URL"));
            }
            programme.insert("thumb".to_string(), json!(thumb));
        }
        if entry.get("new").and_then(Value::as_bool) == Some(true) {
            programme.insert("new".to_string(), json!(true));
        }
        guide.push(Value::Object(programme));
    }
    guide.sort_by_key(|programme| programme.get("start").and_then(Value::as_i64));
    kept.insert("guide".to_string(), Value::Array(guide));
    Ok(Value::Object(kept))
}

/// A channel number's order: `2.1` before `4.1` before `10.1`, part by part.
fn number_order(number: &str) -> Vec<(u64, String)> {
    number.split('.').map(|part| (part.parse::<u64>().unwrap_or(u64::MAX), part.to_string())).collect()
}

/// Seconds since the epoch at local midnight, plus `hours`, of the local day holding `at`.
fn local_hour(at: i64, hours: i32) -> i64 {
    let Some(mut tm) = crate::platform::local_tm(at) else {
        return at - at.rem_euclid(86400) + i64::from(hours) * 3600;
    };
    tm.tm_hour = hours;
    tm.tm_min = 0;
    tm.tm_sec = 0;
    tm.tm_isdst = -1;
    crate::platform::local_mktime(&mut tm)
}

impl Guide {
    fn on_air(&self, airing: &Airing) -> bool {
        airing.begins <= self.now && self.now < airing.ends
    }

    /// The airing on a channel now.
    fn current(&self, channel: usize) -> Option<usize> {
        (0..self.airings.len()).find(|at| self.airings[*at].channel == channel && self.on_air(&self.airings[*at]))
    }

    /// A programme's next airing not yet over, else its last.
    fn next_of(&self, programme: &Programme) -> Option<&Airing> {
        let airings = programme.airings.iter().map(|at| &self.airings[*at]);
        airings.clone().find(|airing| airing.ends > self.now).or_else(|| airings.last())
    }

    fn channel_tag(channel: &Channel) -> String {
        format!("{} {}", channel.number, channel.title)
    }

    fn genre_id(&self, name: &str) -> i64 {
        self.channels.len() as i64 + 1 + self.genres.iter().position(|genre| genre == name).unwrap_or(0) as i64
    }

    /// A row's `Media` for one airing.
    fn media(&self, airing: &Airing) -> Value {
        let channel = &self.channels[airing.channel];
        let programme = &self.programmes[airing.programme];
        let mut media = Map::new();
        media.insert("id".to_string(), json!(airing.id));
        media.insert("duration".to_string(), json!((airing.ends - airing.begins) * 1000));
        // Measured with a guide that says nothing of picture or sound: SD stereo.
        media.insert("audioChannels".to_string(), json!(2));
        media.insert("videoResolution".to_string(), json!("480"));
        media.insert("channelCallSign".to_string(), json!(channel.call_sign));
        media.insert("channelIdentifier".to_string(), json!(channel.number));
        media.insert("channelTitle".to_string(), json!(Self::channel_tag(channel)));
        media.insert("channelVcn".to_string(), json!(channel.number));
        // Measured: XMLTV's `<new/>` marks an episode a premiere, never a movie.
        if programme.premiere && programme.episode {
            media.insert("premiere".to_string(), json!("1"));
        }
        media.insert("protocol".to_string(), json!("livetv"));
        media.insert("beginsAt".to_string(), json!(airing.begins));
        media.insert("endsAt".to_string(), json!(airing.ends));
        if self.on_air(airing) {
            media.insert("onAir".to_string(), json!(true));
        }
        media.insert("channelID".to_string(), json!(channel.id));
        Value::Object(media)
    }

    /// A programme as one of the rows PMS answers it in, listing `airings` as its Media.
    fn row(&self, programme: &Programme, airings: &[&Airing], shape: Shape) -> Value {
        let mut row = Map::new();
        if shape == Shape::Search {
            row.insert("librarySectionTitle".to_string(), json!(if programme.episode { "Shows" } else { "Movies" }));
        }
        row.insert("ratingKey".to_string(), json!(rating_key(&programme.guid)));
        row.insert("key".to_string(), json!(item_key(&programme.guid)));
        let section = |row: &mut Map<String, Value>| {
            if shape == Shape::Page {
                let (title, id) = if programme.episode { ("Shows", 2) } else { ("Movies", 1) };
                row.insert("librarySectionTitle".to_string(), json!(title));
                row.insert("librarySectionID".to_string(), json!(id));
                row.insert("librarySectionKey".to_string(), json!(format!("{PREFIX}/sections/{id}")));
            }
        };
        if programme.episode {
            let show = show_guid(&programme.show);
            row.insert("skipParent".to_string(), json!(true));
            row.insert("grandparentRatingKey".to_string(), json!(rating_key(&show)));
            row.insert("guid".to_string(), json!(programme.guid));
            row.insert("parentGuid".to_string(), json!(season_guid(&programme.show, programme.season)));
            row.insert("grandparentGuid".to_string(), json!(show));
            row.insert("type".to_string(), json!("episode"));
            row.insert("title".to_string(), json!(programme.title));
            row.insert("grandparentKey".to_string(), json!(item_key(&show)));
            section(&mut row);
            row.insert("grandparentTitle".to_string(), json!(programme.show));
            row.insert("parentTitle".to_string(), json!(format!("Season {}", programme.season)));
        } else {
            row.insert("guid".to_string(), json!(programme.guid));
            row.insert("type".to_string(), json!("movie"));
            row.insert("title".to_string(), json!(programme.title));
            section(&mut row);
        }
        row.insert("summary".to_string(), json!(programme.summary));
        // ponytail: a programme's picture (XMLTV's `<icon>`) was not measured (plexav's guide has
        // none); it is the pushed URL as `thumb`, which Plex clients fetch through
        // `/photo/:/transcode` as they fetch an agent's.
        if let Some(thumb) = &programme.thumb {
            row.insert("thumb".to_string(), json!(thumb));
        }
        if programme.episode {
            row.insert("index".to_string(), json!(programme.index));
            row.insert("parentIndex".to_string(), json!(programme.season));
        }
        let first = airings.first().copied().or_else(|| programme.airings.first().map(|at| &self.airings[*at]));
        row.insert("duration".to_string(), json!(first.map_or(0, |airing| (airing.ends - airing.begins) * 1000)));
        row.insert("addedAt".to_string(), json!(self.added));
        if airings.iter().any(|airing| self.on_air(airing)) {
            row.insert("onAir".to_string(), json!(true));
        }
        row.insert("Media".to_string(), Value::Array(airings.iter().map(|airing| self.media(airing)).collect()));
        // Measured: a movie names its categories as Genre tags, an episode none (its show does).
        if !programme.episode {
            row.insert("Genre".to_string(), self.genre_tags(&programme.categories, matches!(shape, Shape::Grid | Shape::Page)));
        }
        if matches!(shape, Shape::Grid | Shape::Page) {
            let mut channels = airings.iter().map(|airing| airing.channel).collect::<Vec<_>>();
            channels.sort_unstable();
            channels.dedup();
            row.insert("Channel".to_string(), self.channel_tags(&channels));
        }
        Value::Object(row)
    }

    /// Genre tags, by name (measured: `Football` before `Sports`); with their ids where the row
    /// is a guide's or a page's, by name alone in a hub, a search hit or a tune.
    fn genre_tags(&self, names: &[String], ids: bool) -> Value {
        let mut names = names.to_vec();
        names.sort();
        names.dedup();
        Value::Array(
            names
                .iter()
                .map(|name| {
                    if ids {
                        let id = self.genre_id(name);
                        json!({"id": id, "filter": format!("genre={id}"), "tag": name})
                    } else {
                        json!({"tag": name})
                    }
                })
                .collect(),
        )
    }

    fn channel_tags(&self, channels: &[usize]) -> Value {
        Value::Array(
            channels
                .iter()
                .map(|at| {
                    let channel = &self.channels[*at];
                    json!({"id": channel.id, "filter": format!("channel={}", channel.id), "tag": Self::channel_tag(channel)})
                })
                .collect(),
        )
    }

    /// A programme's row listing every airing pushed for it.
    fn full_row(&self, programme: &Programme, shape: Shape) -> Value {
        let airings = programme.airings.iter().map(|at| &self.airings[*at]).collect::<Vec<_>>();
        self.row(programme, &airings, shape)
    }

    /// The episodes of a show, by their next airing (measured: the one on now first, one whose
    /// airings today are over after tonight's).
    fn episodes(&self, show: &str) -> Vec<&Programme> {
        let mut episodes = self.programmes.iter().filter(|programme| programme.episode && programme.show == show).collect::<Vec<_>>();
        episodes.sort_by_key(|programme| self.next_of(programme).map_or(i64::MAX, |airing| airing.begins));
        episodes
    }

    fn show_row(&self, show: &str, search: bool, extras: bool) -> Value {
        let episodes = self.episodes(show);
        let guid = show_guid(show);
        let mut seasons = episodes.iter().map(|episode| episode.season).collect::<Vec<_>>();
        seasons.sort_unstable();
        seasons.dedup();
        let mut categories = episodes.iter().flat_map(|episode| episode.categories.clone()).collect::<Vec<_>>();
        categories.sort();
        categories.dedup();
        let mut row = Map::new();
        if search {
            row.insert("librarySectionTitle".to_string(), json!("Shows"));
        }
        row.insert("ratingKey".to_string(), json!(rating_key(&guid)));
        row.insert("key".to_string(), json!(format!("{}/children", item_key(&guid))));
        row.insert("skipChildren".to_string(), json!(seasons.len() == 1));
        row.insert("guid".to_string(), json!(guid));
        row.insert("type".to_string(), json!("show"));
        row.insert("title".to_string(), json!(show));
        if !search {
            row.insert("librarySectionTitle".to_string(), json!("Shows"));
        }
        row.insert("librarySectionID".to_string(), json!(2));
        row.insert("librarySectionKey".to_string(), json!(format!("{PREFIX}/sections/2")));
        row.insert("summary".to_string(), json!(""));
        row.insert("index".to_string(), json!(1));
        row.insert("leafCount".to_string(), json!(episodes.len()));
        row.insert("viewedLeafCount".to_string(), json!(0));
        row.insert("childCount".to_string(), json!(seasons.len()));
        row.insert("addedAt".to_string(), json!(self.added));
        row.insert("Genre".to_string(), self.genre_tags(&categories, !search));
        if !search {
            let mut channels = episodes
                .iter()
                .flat_map(|episode| episode.airings.iter().map(|at| self.airings[*at].channel))
                .collect::<Vec<_>>();
            channels.sort_unstable();
            channels.dedup();
            row.insert("Channel".to_string(), self.channel_tags(&channels));
        }
        if extras {
            row.insert("Extras".to_string(), json!({"size": 0}));
        }
        Value::Object(row)
    }

    fn season_row(&self, show: &str, season: i64, page: bool) -> Value {
        let guid = season_guid(show, season);
        let show_guid = show_guid(show);
        let leaves = self.programmes.iter().filter(|programme| programme.episode && programme.show == show && programme.season == season).count();
        let mut row = Map::new();
        row.insert("ratingKey".to_string(), json!(rating_key(&guid)));
        row.insert("key".to_string(), json!(format!("{}/children", item_key(&guid))));
        row.insert("parentRatingKey".to_string(), json!(rating_key(&show_guid)));
        row.insert("guid".to_string(), json!(guid));
        row.insert("parentGuid".to_string(), json!(show_guid));
        row.insert("type".to_string(), json!("season"));
        row.insert("title".to_string(), json!(format!("Season {season}")));
        row.insert("parentKey".to_string(), json!(item_key(&show_guid)));
        if page {
            row.insert("librarySectionTitle".to_string(), json!("Shows"));
            row.insert("librarySectionID".to_string(), json!(2));
            row.insert("librarySectionKey".to_string(), json!(format!("{PREFIX}/sections/2")));
        }
        row.insert("parentTitle".to_string(), json!(show));
        row.insert("summary".to_string(), json!(""));
        row.insert("index".to_string(), json!(season));
        row.insert("parentIndex".to_string(), json!(1));
        row.insert("leafCount".to_string(), json!(leaves));
        row.insert("viewedLeafCount".to_string(), json!(0));
        row.insert("addedAt".to_string(), json!(self.added));
        Value::Object(row)
    }

}

#[derive(Clone, Copy, PartialEq)]
enum Shape {
    /// `/grid`: the airings of one day on one channel.
    Grid,
    /// `/metadata/<key>`: every airing, and the section the provider files it in.
    Page,
    /// `/grandchildren`: a show's episodes.
    Child,
    /// `/library/search`.
    Search,
    /// `/hubs/discover`.
    Hub,
    /// A tune's Timeline and AlsoAiring.
    Tune,
}

impl Runtime {
    /// The lineup and its guide, or `None` while no source has pushed a channel: rex is then
    /// the tunerless server it always was.
    // ponytail: the guide is read off the channel rows on every Live TV request; a lineup of
    // hundreds of channels a week deep wants it kept per catalog generation.
    fn guide(&self) -> Option<Guide> {
        let mut rows = self
            .catalog
            .rows_by(By::Channel, "")
            .into_iter()
            .filter(|row| row.get("deletedAt").is_none())
            .filter_map(|row| Some((row.get("_channel")?.clone(), row.clone())))
            .collect::<Vec<_>>();
        if rows.is_empty() {
            return None;
        }
        rows.sort_by_key(|(channel, _)| number_order(channel.get("number").and_then(Value::as_str).unwrap_or_default()));
        let mut seen = HashSet::new();
        let mut channels = Vec::new();
        let mut entries = Vec::new();
        let mut added = 0;
        for (channel, row) in rows {
            let number = channel.get("number").and_then(Value::as_str).unwrap_or_default().to_string();
            // Two items naming one number: the first keeps it.
            if !seen.insert(number.clone()) {
                continue;
            }
            let title = channel
                .get("title")
                .and_then(Value::as_str)
                .or_else(|| row.get("title").and_then(Value::as_str))
                .unwrap_or(&number)
                .to_string();
            let call_sign = channel.get("call_sign").and_then(Value::as_str).unwrap_or(&title).to_string();
            added = added.max(row.get("addedAt").and_then(Value::as_i64).unwrap_or(0));
            for entry in channel.get("guide").and_then(Value::as_array).into_iter().flatten() {
                entries.push((channels.len(), entry.clone()));
            }
            channels.push(Channel { key: key_of(&row).unwrap_or_default(), row, number, title, call_sign, id: channels.len() as i64 + 1 });
        }
        // Airings are numbered by start, then lineup order; a programme and a category in the
        // order they first air.
        entries.sort_by_key(|(channel, entry)| (entry.get("start").and_then(Value::as_i64).unwrap_or(0), *channel));
        let mut guide = Guide { channels, airings: Vec::new(), programmes: Vec::new(), genres: Vec::new(), added, now: now() };
        let mut by_guid: HashMap<String, usize> = HashMap::new();
        for (channel, entry) in entries {
            let text = |name: &str| entry.get(name).and_then(Value::as_str).unwrap_or_default().to_string();
            let episode = entry.get("type").and_then(Value::as_str) == Some("episode");
            let (show, season, index) = (text("show"), entry.get("season").and_then(Value::as_i64).unwrap_or(0), entry.get("episode").and_then(Value::as_i64).unwrap_or(0));
            let guid = if episode {
                format!("tv.plex.xmltv://episode/{}/{season}/{index}", guid_part(&show))
            } else {
                format!("tv.plex.xmltv://movie/{}", guid_part(&text("title")))
            };
            let categories = entry
                .get("categories")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect::<Vec<_>>();
            for category in &categories {
                if !guide.genres.contains(category) {
                    guide.genres.push(category.clone());
                }
            }
            let programme = *by_guid.entry(guid.clone()).or_insert_with(|| {
                guide.programmes.push(Programme {
                    guid,
                    episode,
                    title: text("title"),
                    summary: text("summary"),
                    show,
                    season,
                    index,
                    categories,
                    thumb: entry.get("thumb").and_then(Value::as_str).map(str::to_string),
                    premiere: false,
                    airings: Vec::new(),
                });
                guide.programmes.len() - 1
            });
            guide.programmes[programme].premiere |= entry.get("new").and_then(Value::as_bool) == Some(true);
            guide.programmes[programme].airings.push(guide.airings.len());
            guide.airings.push(Airing {
                id: guide.airings.len() as i64 + 1,
                channel,
                begins: entry.get("start").and_then(Value::as_i64).unwrap_or(0),
                ends: entry.get("end").and_then(Value::as_i64).unwrap_or(0),
                programme,
            });
        }
        Some(guide)
    }

    /// `/media/providers`' Live TV entry, as plexav without a Plex Pass answers it: What's On and
    /// the Guide, no DVR Schedule or Recording Priority, and no `subscribe`.
    pub(super) fn livetv_provider(&self) -> Option<Value> {
        let guide = self.guide()?;
        Some(json!({
            "id": PROVIDER_ID, "parentID": DVR.parse::<i64>().unwrap_or(2), "identifier": PROVIDER,
            "providerIdentifier": IDENTIFIER, "title": "Live TV & DVR", "types": "video", "protocols": "livetv",
            "epgSource": "XMLTV", "friendlyName": self.friendly,
            "Feature": [
                {"key": format!("{PREFIX}/sections"), "type": "content", "Directory": [
                    {"id": PROVIDER, "hubKey": format!("{PREFIX}/hubs/discover"), "title": "Live TV & DVR", "Pivot": [
                        {"id": "dvr.whatson", "key": format!("{PREFIX}/hubs/discover"), "type": "hub", "title": "What's On",
                         "context": "content.dvr.discover", "symbol": "star"},
                        {"id": "dvr.guide", "key": "view://dvr/guide", "type": "view", "title": "Guide",
                         "context": "content.dvr.guide", "symbol": "guide"}]},
                    {"type": "mixed", "key": format!("{PREFIX}/watchnow"), "title": "Guide", "icon": "/:/resources/dvr/dvr-watchnow-icon.png"},
                    {"key": format!("{PREFIX}/sections/1"), "type": "movie", "title": "Movies", "icon": "/:/resources/dvr/dvr-movies-icon.png", "updatedAt": guide.added},
                    {"key": format!("{PREFIX}/sections/2"), "type": "show", "title": "Shows", "icon": "/:/resources/dvr/dvr-allshows-icon.png", "updatedAt": guide.added}]},
                {"key": format!("{PREFIX}/hubs/search"), "type": "search"},
                {"key": format!("{PREFIX}/matches"), "type": "match"},
                {"key": format!("{PREFIX}/metadata"), "type": "metadata"},
                {"key": "/photo/:/transcode", "type": "imagetranscoder"},
                {"key": format!("{PREFIX}/hubs/discover?promoted=1&includeTypeFirst=1"), "type": "promoted"},
                {"key": format!("{PREFIX}/grid"), "type": "grid"},
                {"key": format!("{PREFIX}/collections"), "type": "collection"},
                {"scrobbleKey": "/:/scrobble", "unscrobbleKey": "/:/unscrobble", "key": "/:/timeline", "type": "timeline"},
                {"key": format!("{PREFIX}/library/search"), "type": "universalsearch"}
            ]
        }))
    }

    /// Whether a source has pushed a channel: `allowTuners`, as a PMS with a DVR answers it.
    pub(super) fn has_livetv(&self) -> bool {
        self.catalog.rows_by(By::Channel, "").iter().any(|row| row.get("deletedAt").is_none())
    }

    /// The tuned session a `/livetv/sessions/<uuid>` path names, with its uuid.
    fn tuned_by_path(&self, path: &str) -> Option<(String, Tuned)> {
        let uuid = path.strip_prefix("/livetv/sessions/")?.split(['/', '?']).next()?;
        self.livetv.tuned.get(uuid).map(|tuned| (uuid.to_string(), tuned.clone()))
    }

    /// Live TV's routes, before the route table: the provider's, the DVR's and a tune's own, and a
    /// tuned session's conversion and beats handed to its channel's row. `None` leaves the request
    /// to the table, which answers a server with no channel as it always has.
    pub(super) fn route_livetv(&mut self, request: &Request, path: &str, query: &mut HashMap<String, String>) -> Option<Response> {
        if path.starts_with("/video/:/transcode/universal/") || path == "/:/timeline" {
            let field = if path == "/:/timeline" { "key" } else { "path" };
            if let Some((uuid, tuned)) = query.get(field).and_then(|value| self.tuned_by_path(value)) {
                query.insert(field.to_string(), format!("/library/metadata/{}", tuned.row));
                if let Some(tuned) = self.livetv.tuned.get_mut(&uuid) {
                    tuned.seen = super::now();
                }
                // Measured: Plex for Android plays a tune's session with `start.m3u8` alone; the
                // tune was its decision. rex's conversion starts from one, so it is made here.
                let playback = self.playback_session(query, &request.headers);
                if path.starts_with("/video/:/transcode/universal/start")
                    && !self.decided_sessions.contains(&(sessions::account_session_id(self.viewer.id, &playback), tuned.row.clone()))
                {
                    self.route_decision(query, &request.headers);
                }
                if path == "/:/timeline" && query.get("state").map(String::as_str) == Some("stopped") {
                    self.end_tuned(&uuid);
                }
            }
            return None;
        }
        let in_livetv = path.starts_with(PREFIX) || path.starts_with("/livetv/");
        if !in_livetv {
            return None;
        }
        let guide = self.guide()?;
        let get = matches!(request.method.as_str(), "GET" | "HEAD");
        let segments = path.trim_start_matches('/').split('/').collect::<Vec<_>>();
        match (request.method.as_str(), segments.as_slice()) {
            (_, ["livetv", "dvrs"]) if get => Some(Response::json(200, self.dvrs(&guide))),
            (_, ["livetv", "dvrs", key]) if get => Some(if *key == DVR { Response::json(200, self.dvrs(&guide)) } else { Response::error(404, "rex: no such DVR") }),
            ("POST", ["livetv", "dvrs", key, "channels", number, "tune"]) => Some(if *key != DVR {
                Response::error(404, "rex: no such DVR")
            } else {
                self.tune(&guide, number, request, query)
            }),
            (_, ["livetv", "sessions"]) if get => {
                let rows = self.livetv.tuned.keys().cloned().collect::<Vec<_>>();
                let rows = rows.iter().filter_map(|uuid| self.session_row(&guide, uuid, &request.headers, query)).collect::<Vec<_>>();
                Some(Response::json(200, json!({"MediaContainer": {"size": rows.len(), "Metadata": rows}})))
            }
            (_, ["livetv", "sessions", uuid]) if get => Some(match self.session_row(&guide, uuid, &request.headers, query) {
                Some(row) => Response::json(200, json!({"MediaContainer": {"size": 1, "Metadata": [row]}})),
                None => Response::error(404, "rex: no such session"),
            }),
            _ if !path.starts_with(PREFIX) || !get => None,
            _ => self.route_provider(&guide, &path[PREFIX.len()..], query),
        }
    }

    /// The EPG provider's own routes, under `/tv.plex.providers.epg.xmltv:2`.
    fn route_provider(&mut self, guide: &Guide, tail: &str, query: &HashMap<String, String>) -> Option<Response> {
        let container = |rows: Vec<Value>| json!({"MediaContainer": {"size": rows.len(), "Metadata": rows}});
        match tail {
            "/lineups/dvr/channels" => {
                let rows = guide
                    .channels
                    .iter()
                    .map(|channel| {
                        json!({"id": channel.number, "gridKey": channel.number, "vcn": channel.number,
                               "title": channel.title, "callSign": channel.call_sign})
                    })
                    .collect::<Vec<_>>();
                Some(Response::json(200, json!({"MediaContainer": {"size": rows.len(), "Channel": rows}})))
            }
            "/grid" => Some(Response::json(200, container(self.grid(guide, query)))),
            "/hubs/discover" => Some(Response::json(200, self.discover(guide, query))),
            "/library/search" => Some(Response::json(200, self.livetv_search(guide, query))),
            _ => {
                let tail = tail.strip_prefix("/metadata/")?;
                self.provider_metadata(guide, tail, query)
            }
        }
    }

    /// `/grid?channelGridKey=&date=`: one channel's airings that touch one day, the day read as
    /// UTC (measured: `date=2026-10-31` ran to 00:00 UTC, an hour past Amsterdam's midnight, on a
    /// PMS in Europe/Amsterdam), the past ones of the day included. Airings of one programme in a
    /// row are one row (Rex Football Live, hourly, was one row of eleven Media).
    fn grid(&self, guide: &Guide, query: &HashMap<String, String>) -> Vec<Value> {
        let (Some(number), Some(date)) = (query.get("channelGridKey"), query.get("date")) else {
            return Vec::new();
        };
        let Some(channel) = guide.channels.iter().position(|channel| &channel.number == number) else {
            return Vec::new();
        };
        let Some(start) = guide_time(&json!(format!("{date}T00:00Z"))) else {
            return Vec::new();
        };
        let end = start + 86400;
        let mut rows: Vec<(usize, Vec<&Airing>)> = Vec::new();
        for airing in guide.airings.iter().filter(|airing| airing.channel == channel && airing.ends > start && airing.begins < end) {
            match rows.last_mut() {
                Some((programme, airings)) if *programme == airing.programme => airings.push(airing),
                _ => rows.push((airing.programme, vec![airing])),
            }
        }
        rows.iter().map(|(programme, airings)| guide.row(&guide.programmes[*programme], airings, Shape::Grid)).collect()
    }

    /// `/metadata/<ratingKey>` and its `/children`, `/grandchildren` and `/related`: a movie, an
    /// episode, a show or a season of the guide. Several keys joined by commas (a hub's `hubKey`)
    /// answer each.
    fn provider_metadata(&mut self, guide: &Guide, tail: &str, query: &HashMap<String, String>) -> Option<Response> {
        let (key, leaf) = ["/children", "/grandchildren", "/related"]
            .iter()
            .find_map(|leaf| tail.strip_suffix(leaf).map(|key| (key, *leaf)))
            .filter(|(key, _)| self.guide_item(guide, key).is_some())
            .unwrap_or((tail, ""));
        let page = |rows: Vec<Value>| {
            json!({"MediaContainer": {"size": rows.len(), "allowSync": false, "identifier": IDENTIFIER,
                   "mediaTagPrefix": MEDIA_TAG_PREFIX, "mediaTagVersion": MEDIA_TAG_VERSION, "Metadata": rows}})
        };
        if leaf.is_empty() {
            let rows = key.split(',').filter_map(|key| self.guide_item(guide, key)).collect::<Vec<_>>();
            if rows.is_empty() {
                return Some(Response::error(404, "rex: not found"));
            }
            let rows = rows
                .into_iter()
                .map(|item| match item {
                    Item::Programme(at) => guide.full_row(&guide.programmes[at], Shape::Page),
                    Item::Show(show) => guide.show_row(&show, false, query.get("includeExtras").map(String::as_str) == Some("1")),
                    Item::Season(show, season) => guide.season_row(&show, season, true),
                })
                .collect::<Vec<_>>();
            let on = |name: &str| query.get(name).is_some_and(|value| is_true_option(value));
            // Measured (a show's page, Plex Web's request): under `asyncCheckFiles` or
            // `asyncRefreshLocalMediaAgent` the container is bare, as a library item's page is.
            let mut response = if on("asyncCheckFiles") || on("asyncRefreshLocalMediaAgent") {
                Response::json(200, json!({"MediaContainer": {"size": rows.len(), "allowSync": false, "Metadata": rows}}))
            } else {
                Response::json(200, page(rows))
            };
            // Measured: `asyncAugmentMetadata=1` answers an activity (never listed in
            // `/activities`: it ends at once); no client reads it but the tune's and a subtitle's.
            if on("asyncAugmentMetadata") {
                response.headers.push(("X-Plex-Activity".to_string(), self.activity_uuid()));
                response.headers.push(("Access-Control-Expose-Headers".to_string(), "X-Plex-Activity".to_string()));
            }
            return Some(response);
        }
        let item = self.guide_item(guide, key)?;
        match (leaf, item) {
            ("/related", _) => Some(Response::json(200, page(Vec::new()))),
            ("/children", Item::Show(show)) => {
                let mut seasons = guide.episodes(&show).iter().map(|episode| episode.season).collect::<Vec<_>>();
                seasons.sort_unstable();
                seasons.dedup();
                let rows = seasons.iter().map(|season| guide.season_row(&show, *season, false)).collect::<Vec<_>>();
                Some(Response::json(200, json!({"MediaContainer": {
                    "size": rows.len(), "allowSync": false, "art": "/:/resources/show-fanart.jpg", "identifier": IDENTIFIER,
                    "key": "1", "mediaTagPrefix": MEDIA_TAG_PREFIX, "mediaTagVersion": MEDIA_TAG_VERSION, "nocache": true,
                    "parentIndex": 1, "parentTitle": show, "title1": "Shows", "title2": show, "viewGroup": "season",
                    "viewMode": 65593, "Metadata": rows}})))
            }
            ("/grandchildren", Item::Show(show)) => {
                let rows = guide.episodes(&show).iter().map(|episode| guide.full_row(episode, Shape::Child)).collect::<Vec<_>>();
                Some(Response::json(200, json!({"MediaContainer": {
                    "size": rows.len(), "art": "/:/resources/show-fanart.jpg", "identifier": IDENTIFIER,
                    "mediaTagPrefix": MEDIA_TAG_PREFIX, "mediaTagVersion": MEDIA_TAG_VERSION, "nocache": true,
                    "title1": "Shows", "viewGroup": "show", "viewMode": 65592, "Metadata": rows}})))
            }
            // ponytail: a season's /children and an item's other leaves were not measured; they
            // fall to the route table's 404.
            _ => None,
        }
    }

    /// What a decoded `ratingKey` (the guid itself) names in the guide.
    fn guide_item(&self, guide: &Guide, key: &str) -> Option<Item> {
        if let Some(at) = guide.programmes.iter().position(|programme| programme.guid == key) {
            return Some(Item::Programme(at));
        }
        let shows = guide.programmes.iter().filter(|programme| programme.episode);
        for programme in shows {
            if show_guid(&programme.show) == key {
                return Some(Item::Show(programme.show.clone()));
            }
            if season_guid(&programme.show, programme.season) == key {
                return Some(Item::Season(programme.show.clone(), programme.season));
            }
        }
        None
    }

    /// `/library/search?searchTypes=livetv` (Plex Web's search, beside the library's): each movie,
    /// show and episode whose title holds every word asked, scored as the library's search scores
    /// a title (measured: `Sintel` 0.93, `rex` 0.73, `fire` 0.53 for Firefly, `bunny` 0.33,
    /// `Episode 3` 0.91, `sintell` nothing).
    fn livetv_search(&self, guide: &Guide, query: &HashMap<String, String>) -> Value {
        let needle = query.get("query").cloned().unwrap_or_default();
        let terms = needle.split_whitespace().map(str::to_lowercase).collect::<Vec<_>>();
        let matches = |title: &str| {
            let words = title.split_whitespace().map(str::to_lowercase).collect::<Vec<_>>();
            !terms.is_empty() && terms.iter().all(|term| words.iter().any(|word| word.starts_with(term.as_str())))
        };
        let mut hits: Vec<(f64, Value)> = Vec::new();
        let mut shows = Vec::new();
        for programme in &guide.programmes {
            if programme.episode && !shows.contains(&programme.show) {
                shows.push(programme.show.clone());
                if matches(&programme.show) {
                    let score = search::search_score_value(&json!({"type": "show", "title": programme.show}), &needle);
                    hits.push((score, guide.show_row(&programme.show, true, false)));
                }
            }
            if matches(&programme.title) {
                let score = search::search_score_value(&json!({"type": if programme.episode { "episode" } else { "movie" }, "title": programme.title}), &needle);
                hits.push((score, guide.full_row(programme, Shape::Search)));
            }
        }
        // ponytail: PMS also answers a category whose name matches as a tag hit (`news`, 0.92);
        // not built.
        hits.sort_by(|left, right| right.0.partial_cmp(&left.0).unwrap_or(std::cmp::Ordering::Equal));
        let limit = query.get("limit").and_then(|limit| limit.parse::<usize>().ok()).unwrap_or(usize::MAX);
        let results = hits
            .into_iter()
            .take(limit)
            .map(|(score, row)| json!({"score": (score * 100.0).round() / 100.0, "Metadata": row}))
            .collect::<Vec<_>>();
        json!({"MediaContainer": {"size": results.len(), "SearchResult": results}})
    }

    /// `/hubs/discover`, What's On, with plexav's nine hubs (ten with Recent Channels); under
    /// `promoted=1` only the promoted one.
    fn discover(&self, guide: &Guide, query: &HashMap<String, String>) -> Value {
        let count = query.get("count").and_then(|count| count.parse::<usize>().ok()).unwrap_or(usize::MAX);
        let now = guide.now;
        let hub = |key: String, title: &str, kind: &str, id: &str, rows: Vec<(usize, Value)>, promoted: bool| {
            let rows = rows.into_iter().take(count).collect::<Vec<_>>();
            let mut hub = Map::new();
            if !rows.is_empty() {
                let keys = rows.iter().map(|(at, _)| rating_key(&guide.programmes[*at].guid)).collect::<Vec<_>>().join(",");
                hub.insert("hubKey".to_string(), json!(format!("{PREFIX}/metadata/{keys}")));
            }
            hub.insert("key".to_string(), json!(key));
            hub.insert("title".to_string(), json!(title));
            hub.insert("type".to_string(), json!(kind));
            hub.insert("hubIdentifier".to_string(), json!(id));
            hub.insert("context".to_string(), json!(format!("hub.{id}")));
            hub.insert("size".to_string(), json!(rows.len()));
            hub.insert("more".to_string(), json!(false));
            hub.insert("style".to_string(), json!("shelf"));
            if promoted {
                hub.insert("promoted".to_string(), json!(true));
            }
            hub.insert("Metadata".to_string(), Value::Array(rows.into_iter().map(|(_, row)| row).collect()));
            Value::Object(hub)
        };
        let row = |at: usize| (at, guide.full_row(&guide.programmes[at], Shape::Hub));
        // On now: each channel's airing, in lineup order; "Shows On Now" the same by start.
        let on_now = (0..guide.channels.len()).filter_map(|channel| guide.current(channel)).collect::<Vec<_>>();
        let mut by_start = on_now.clone();
        by_start.sort_by_key(|at| (guide.airings[*at].begins, guide.airings[*at].programme));
        let programmes = |airings: &[usize]| airings.iter().map(|at| row(guide.airings[*at].programme)).collect::<Vec<_>>();
        let shows_on_now = hub(
            format!("{PREFIX}/all?type=1,4&beginsAt<=now&endsAt>=%2B1s&sort=beginsAt,mediaHeight:desc,mediaAnalysisVersion:desc"),
            "Shows On Now",
            "mixed",
            "today.onrightnow",
            programmes(&by_start),
            true,
        );
        if query.get("promoted").map(String::as_str) == Some("1") {
            return json!({"MediaContainer": {"size": 1, "identifier": IDENTIFIER, "Hub": [shows_on_now]}});
        }
        // Next: each channel's airing after the one on now.
        let later = (0..guide.channels.len())
            .filter_map(|channel| (0..guide.airings.len()).find(|at| guide.airings[*at].channel == channel && guide.airings[*at].begins > now))
            .collect::<Vec<_>>();
        // A programme's next start not before now (measured: Movies On Later, ties in the order
        // the programmes first aired).
        let upcoming = |keep: &dyn Fn(&Programme) -> bool| {
            let mut found = guide
                .programmes
                .iter()
                .enumerate()
                .filter(|(_, programme)| keep(programme))
                .filter_map(|(at, programme)| {
                    let next = programme.airings.iter().map(|airing| &guide.airings[*airing]).find(|airing| airing.begins >= now)?;
                    Some((next.begins, at))
                })
                .collect::<Vec<_>>();
            found.sort();
            found.into_iter().map(|(_, at)| row(at)).collect::<Vec<_>>()
        };
        let named = |programme: &Programme, wanted: &str| programme.categories.iter().any(|category| category.eq_ignore_ascii_case(wanted));
        // Tonight: premieres starting from 19:00 to 23:00 local time (measured on a PMS in
        // Europe/Amsterdam: beginsAt>=18:00Z&beginsAt<=22:00Z).
        let (evening, night) = (local_hour(now, 19), local_hour(now, 23));
        let mut tonight = guide
            .airings
            .iter()
            .filter(|airing| guide.programmes[airing.programme].premiere && guide.programmes[airing.programme].episode && airing.begins >= evening && airing.begins <= night)
            .map(|airing| (airing.begins, airing.programme))
            .collect::<Vec<_>>();
        tonight.sort();
        let mut seen = HashSet::new();
        tonight.retain(|(_, programme)| seen.insert(*programme));
        let mut hubs = Vec::new();
        if query.get("includeRecentChannels").map(String::as_str) == Some("1") {
            let recent = self
                .livetv
                .recent
                .get(&self.viewer.id)
                .into_iter()
                .flatten()
                .filter_map(|number| guide.channels.iter().position(|channel| &channel.number == number))
                .map(|channel| {
                    let channel_row = &guide.channels[channel];
                    let mut directory = json!({"identifier": channel_row.number, "key": channel_row.number,
                        "channelVcn": channel_row.number, "title": Guide::channel_tag(channel_row),
                        "callSign": channel_row.call_sign, "type": "channel"});
                    // Measured: the channel's airing on now, with that airing alone as its Media.
                    if let Some(current) = guide.current(channel) {
                        let airing = &guide.airings[current];
                        directory["Metadata"] = guide.row(&guide.programmes[airing.programme], &[airing], Shape::Hub);
                    }
                    directory
                })
                .take(count)
                .collect::<Vec<_>>();
            if !recent.is_empty() {
                hubs.push(json!({"key": format!("{PREFIX}/hubs/channels/recent"), "title": "Recent Channels", "type": "channels",
                    "hubIdentifier": "recent.channels", "context": "hub.recent.channels", "size": recent.len(), "more": false,
                    "style": "shelf", "promoted": true, "Directory": recent}));
            }
        }
        hubs.push(hub(format!("{PREFIX}/hubs/onnow/all"), "What's On Now", "mixed", "today.whatsonnow", programmes(&on_now), false));
        hubs.push(shows_on_now);
        hubs.push(hub(
            format!("{PREFIX}/sections/2/all?type=4&beginsAt>={evening}&beginsAt<={night}&mediaAnalysisVersion=1&sort=beginsAt"),
            "New Shows On Tonight",
            "episode",
            "today.premierstonight",
            tonight.iter().map(|(_, at)| row(*at)).collect(),
            false,
        ));
        hubs.push(hub(format!("{PREFIX}/hubs/playlists/fav"), "My Upcoming Shows", "show", "today.upcomingfavorites", Vec::new(), false));
        hubs.push(hub(format!("{PREFIX}/hubs/playlists/rec"), "Recommended For You", "show", "today.recommended", Vec::new(), false));
        hubs.push(hub(format!("{PREFIX}/hubs/onlater/all"), "What's On Later", "mixed", "today.whatsonlater", programmes(&later), false));
        let sort = "beginsAt>=now&sort=beginsAt,mediaHeight:desc,mediaAnalysisVersion:desc";
        hubs.push(hub(format!("{PREFIX}/sections/3/all?type=4&{sort}"), "Sports On Later", "episode", "today.upcomingsports",
            upcoming(&|programme: &Programme| programme.episode && named(programme, "sports")), false));
        hubs.push(hub(format!("{PREFIX}/sections/1/all?type=1&{sort}"), "Movies On Later", "movie", "today.upcomingmovies",
            upcoming(&|programme: &Programme| !programme.episode), false));
        hubs.push(hub(format!("{PREFIX}/sections/4/all?type=4&{sort}"), "News On Later", "episode", "today.upcomingnews",
            upcoming(&|programme: &Programme| programme.episode && named(programme, "news")), false));
        json!({"MediaContainer": {"size": hubs.len(), "identifier": IDENTIFIER, "Hub": hubs}})
    }

    /// `/livetv/dvrs`: the one DVR, its lineup, and the device its channels are mapped from. The
    /// device is rex's own sources, so its names are rex's; the shape and settings are plexav's.
    fn dvrs(&self, guide: &Guide) -> Value {
        let device = self.machine.chars().take(8).collect::<String>().to_uppercase();
        let lineup = format!("lineup://{IDENTIFIER}/rex#{}", guid_part(&self.friendly));
        let mappings = guide
            .channels
            .iter()
            .map(|channel| json!({"channelKey": channel.number, "deviceIdentifier": channel.number, "enabled": "1", "lineupIdentifier": channel.number}))
            .collect::<Vec<_>>();
        let setting = |id: &str, label: &str, summary: &str, kind: &str, default: &str, values: Option<String>| {
            let mut row = json!({"id": id, "label": label, "summary": summary, "type": kind, "default": default,
                                 "value": default, "hidden": false, "advanced": false, "group": ""});
            if let Some(values) = values {
                row["enumValues"] = json!(values);
            }
            row
        };
        let hours = (0..24)
            .map(|hour: i32| {
                let name = |hour: i32| match hour {
                    0 | 24 => "Midnight".to_string(),
                    12 => "Noon".to_string(),
                    hour if hour < 12 => format!("{hour} am"),
                    hour => format!("{} pm", hour - 12),
                };
                format!("{hour}:{} - {}", name(hour), name(hour + 1))
            })
            .collect::<Vec<_>>()
            .join("|");
        json!({"MediaContainer": {"size": 1, "Dvr": [{
            "key": DVR, "uuid": crate::hierarchy::uuid(&format!("{}:dvr", self.machine)), "language": "deu",
            "lineupTitle": self.friendly, "lineup": lineup, "country": "deu", "refreshedAt": guide.added,
            "epgIdentifier": PROVIDER,
            "Device": [{
                "parentID": DVR.parse::<i64>().unwrap_or(2), "key": "1",
                "uuid": format!("device://tv.plex.grabbers.hdhomerun/{device}"), "uri": format!("http://127.0.0.1:{}", self.private_port),
                "protocol": "livetv", "status": "alive", "state": "enabled", "lastSeenAt": super::now(), "canTranscode": "1",
                "deviceAuth": "", "deviceId": device, "make": "rex", "model": "rex live channels", "modelNumber": "rex",
                "source": "1", "sources": "1", "thumb": "/:/resources/dvr/device-generic-560.png", "title": self.friendly,
                "tuners": guide.channels.len().to_string(),
                "ChannelMapping": mappings,
                "Setting": [setting("transcodeProfile", "Transcoder Quality",
                    "How live TV is converted. The original format can make very large files.", "text", "heavy",
                    Some("none:Original format|heavy:Highest quality|mobile:High quality (30fps limit)|internet540:Medium quality (540p)|internet480:Medium quality (480p)|internet360:Low quality (360p)|internet240:Low quality (240p)".to_string()))]
            }],
            "Lineup": [{"id": lineup, "title": self.friendly}],
            "Setting": [
                setting("useUmp", "Enhanced Guide", "Download richer details for some shows and movies; guide refreshes take longer.", "bool", "false", None),
                setting("mediaProviderEpgXmltvGuideRefreshStartTime", "Guide Refresh Time", "The guide refreshes within this hour.", "int", "2", Some(hours)),
                setting("xmltvCustomRefreshInHours", "Guide Refresh Interval", "Hours between guide refreshes.", "int", "24",
                    Some((1..=24).map(|hour| format!("{hour}:{hour}")).collect::<Vec<_>>().join("|"))),
                setting("kidsCategories", "Kids Categories", "Categories that count as kids' programmes, separated by commas.", "text", "kids", None),
                setting("newsCategories", "News Categories", "Categories that count as news, separated by commas.", "text", "news", None),
                setting("sportsCategories", "Sports Categories", "Categories that count as sports, separated by commas.", "text", "sports", None)
            ]
        }]}})
    }

    /// A tuned session's metadata, as `/livetv/sessions/<uuid>` answers it (measured: the airing
    /// it began on, `live`, the capture media under the session's own key).
    fn session_row(&self, guide: &Guide, uuid: &str, headers: &HashMap<String, String>, query: &HashMap<String, String>) -> Option<Value> {
        let tuned = self.livetv.tuned.get(uuid)?;
        let channel = guide.channels.iter().position(|channel| channel.key == tuned.row)?;
        let (programme, _) = self.tuned_programme(guide, channel, tuned.at);
        let client = plex_value(query, headers, "X-Plex-Client-Identifier").unwrap_or_default();
        let mut row = Map::new();
        row.insert("ratingKey".to_string(), json!(tuned.row));
        row.insert("guid".to_string(), json!(programme.guid));
        row.insert("type".to_string(), json!(if programme.episode { "episode" } else { "movie" }));
        row.insert("title".to_string(), json!(programme.title));
        if programme.episode {
            row.insert("grandparentTitle".to_string(), json!(programme.show));
            row.insert("index".to_string(), json!(programme.index));
        }
        row.insert("summary".to_string(), json!(""));
        row.insert("addedAt".to_string(), json!(tuned.at));
        row.insert("genuineMediaAnalysis".to_string(), json!("1"));
        row.insert("key".to_string(), json!(format!("/livetv/sessions/{uuid}")));
        row.insert("live".to_string(), json!("1"));
        row.insert("parentIndex".to_string(), json!(if programme.episode { programme.season } else { -1 }));
        let mut media = self.capture_media(&guide.channels[channel], uuid, tuned.media, &client, false);
        media.as_object_mut()?.insert("TranscodeSession".to_string(), self.capture_session(guide, channel, uuid, tuned.at));
        row.insert("Media".to_string(), json!([media]));
        Some(Value::Object(row))
    }

    /// The programme a channel showed at `at`, or the channel itself where its guide has nothing.
    fn tuned_programme(&self, guide: &Guide, channel: usize, at: i64) -> (Programme, Option<usize>) {
        let airing = (0..guide.airings.len()).find(|index| {
            let airing = &guide.airings[*index];
            airing.channel == channel && airing.begins <= at && at < airing.ends
        });
        if let Some(index) = airing {
            let programme = &guide.programmes[guide.airings[index].programme];
            return (
                Programme {
                    guid: programme.guid.clone(),
                    episode: programme.episode,
                    title: programme.title.clone(),
                    summary: programme.summary.clone(),
                    show: programme.show.clone(),
                    season: programme.season,
                    index: programme.index,
                    categories: programme.categories.clone(),
                    thumb: programme.thumb.clone(),
                    premiere: programme.premiere,
                    airings: programme.airings.clone(),
                },
                Some(index),
            );
        }
        // ponytail: what PMS tunes into a gap in its guide was not measured; the channel is then
        // a movie named after itself.
        let title = guide.channels[channel].title.clone();
        let programme = Programme {
            guid: format!("tv.plex.xmltv://movie/{}", guid_part(&title)),
            episode: false,
            title,
            summary: String::new(),
            show: String::new(),
            season: 0,
            index: 0,
            categories: Vec::new(),
            thumb: None,
            premiere: false,
            airings: Vec::new(),
        };
        (programme, None)
    }

    /// The tuned channel's own picture and sound, as the capture media PMS lists for a session: its
    /// row's probe, under the session's key.
    fn capture_media(&self, channel: &Channel, uuid: &str, id: i64, client: &str, tune: bool) -> Value {
        let mut row = channel.row.clone();
        crate::catalog::unpack_streams(&mut row);
        let media = row.pointer("/Media/0").cloned().unwrap_or_default();
        let streams = media.pointer("/Part/0/Stream").and_then(Value::as_array).cloned().unwrap_or_default();
        let video = streams.iter().find(|stream| stream.get("streamType").and_then(Value::as_i64) == Some(1));
        let audio = streams.iter().find(|stream| stream.get("streamType").and_then(Value::as_i64) == Some(2));
        let field = |stream: Option<&Value>, name: &str| stream.and_then(|stream| stream.get(name)).cloned().unwrap_or(Value::Null);
        let height = field(video, "height");
        let mut out = Map::new();
        out.insert("id".to_string(), json!(id));
        out.insert("width".to_string(), field(video, "width"));
        out.insert("height".to_string(), height.clone());
        // Measured: a tune answers the ratio as text ("1.78"), a session's read as a number.
        let ratio = media.get("aspectRatio").and_then(Value::as_f64);
        out.insert("aspectRatio".to_string(), ratio.map_or(Value::Null, |ratio| if tune { json!(format!("{ratio:.2}")) } else { json!(ratio) }));
        out.insert("audioChannels".to_string(), field(audio, "channels"));
        out.insert("audioCodec".to_string(), field(audio, "codec"));
        out.insert("videoCodec".to_string(), field(video, "codec"));
        out.insert("videoResolution".to_string(), height.as_i64().map_or(Value::Null, |height| json!(height.to_string())));
        out.insert("container".to_string(), json!("mpegts"));
        out.insert("origin".to_string(), json!("livetv"));
        out.insert("protocol".to_string(), json!("hls"));
        out.insert("uuid".to_string(), json!(uuid));
        if tune {
            let now = now();
            out.insert("beginsAt".to_string(), json!(now));
            out.insert("endsAt".to_string(), json!(now + CAPTURE_WINDOW));
            out.insert("channelCallSign".to_string(), json!(channel.call_sign));
            out.insert("channelIdentifier".to_string(), json!(channel.number));
        }
        // Measured: a capture's streams carry these attributes alone, whatever the tuner sends.
        const VIDEO: &[&str] = &["id", "streamType", "codec", "index", "frameRate", "height", "level", "pixelAspectRatio", "profile", "width", "displayTitle", "extendedDisplayTitle"];
        const AUDIO: &[&str] = &["id", "streamType", "selected", "codec", "index", "channels", "bitrate", "audioChannelLayout", "samplingRate", "displayTitle", "extendedDisplayTitle"];
        let streams = streams
            .iter()
            .filter_map(|stream| {
                let names = match stream.get("streamType").and_then(Value::as_i64) {
                    Some(1) => VIDEO,
                    Some(2) => AUDIO,
                    _ => return None,
                };
                let mut object = stream.as_object()?.clone();
                // A library's analysis leaves square pixels unsaid; a capture says `1:1`.
                if names == VIDEO && !object.contains_key("pixelAspectRatio") {
                    object.insert("pixelAspectRatio".to_string(), json!("1:1"));
                }
                Some(Value::Object(names.iter().filter_map(|name| Some((name.to_string(), object.get(*name)?.clone()))).collect()))
            })
            .collect::<Vec<_>>();
        // Measured: the Part's key names the session and the asking client (a GET from another
        // client names that one).
        let mut part = json!({"id": id, "container": "mpegts",
                              "key": format!("/livetv/sessions/{uuid}/{client}/index.m3u8?offset=-1.000000"),
                              "protocol": "hls", "Stream": streams});
        if tune {
            part["size"] = json!(0);
        }
        out.insert("Part".to_string(), json!([part]));
        Value::Object(out)
    }

    /// The capture's own conversion, as PMS lists it beside a tuned session: a copy of the channel.
    fn capture_session(&self, guide: &Guide, channel: usize, uuid: &str, at: i64) -> Value {
        let mut row = guide.channels[channel].row.clone();
        crate::catalog::unpack_streams(&mut row);
        let streams = row.pointer("/Media/0/Part/0/Stream").and_then(Value::as_array).cloned().unwrap_or_default();
        let video = streams.iter().find(|stream| stream.get("streamType").and_then(Value::as_i64) == Some(1));
        let duration = self.tuned_programme(guide, channel, at).1.map_or(0, |airing| {
            let airing = &guide.airings[airing];
            (airing.ends - airing.begins) * 1000
        });
        json!({"key": format!("/transcode/sessions/{uuid}"), "throttled": false, "complete": false, "progress": -1.0,
               "size": -1, "speed": 1.0, "error": false, "duration": duration, "context": "static",
               "sourceVideoCodec": "", "sourceAudioCodec": "", "videoDecision": "copy", "audioDecision": "copy",
               "subtitleDecision": "copy", "protocol": "hls", "container": "mpegts", "videoCodec": "*", "audioCodec": "*",
               "audioChannels": 2, "width": video.and_then(|video| video.get("width")).cloned(),
               "height": video.and_then(|video| video.get("height")).cloned(),
               "transcodeHwRequested": false, "transcodeHwFullPipeline": false, "timeStamp": at as f64,
               "maxOffsetAvailable": (super::now() - at).max(0) as f64, "minOffsetAvailable": 0.0})
    }

    /// `POST /livetv/dvrs/2/channels/<number>/tune`: a session on the channel, answered as plexav
    /// answers it -- the rolling "This Episode" subscription (a movie's too), its grab operation
    /// holding the session's metadata and capture media, the channel's airing now and next two
    /// (`Timeline`) and what is on the other channels (`AlsoAiring`).
    fn tune(&mut self, guide: &Guide, number: &str, request: &Request, query: &HashMap<String, String>) -> Response {
        let Some(channel) = guide.channels.iter().position(|channel| channel.number == number) else {
            return Response::error(404, "rex: no such channel");
        };
        let at = now();
        let stamp = super::now();
        let idle = self.livetv.tuned.iter().filter(|(_, tuned)| stamp - tuned.seen >= SESSION_IDLE).map(|(uuid, _)| uuid.clone()).collect::<Vec<_>>();
        for uuid in idle {
            self.end_tuned(&uuid);
        }
        let uuid = plextv::uuid4();
        let client = plex_value(query, &request.headers, "X-Plex-Client-Identifier").unwrap_or_default();
        let session = plex_value(query, &request.headers, "X-Plex-Session-Identifier").unwrap_or_else(|| client.clone());
        self.livetv.subscriptions += 1;
        let subscription = self.livetv.subscriptions;
        let media = 1_000_000 + subscription;
        let row = guide.channels[channel].key.clone();
        // Measured (Plex Web's and Android's tunes): the answer names the session's activity in
        // `X-Plex-Activity`, exposed to a browser, and `/activities` lists it while the session
        // runs. Plex Web refuses a tune without it ("Could not tune channel (No activity)") and
        // ends its player when the activity ends.
        // ponytail: PMS's `progress` was 11 at 39 s and 33 at 142 s, a formula unmeasured; and its
        // rolling subscription's `provider.subscription.refresh` activity is not listed.
        let activity = json!({"uuid": self.activity_uuid(), "type": "grabber.grab", "cancellable": true, "userID": self.viewer.id,
            "title": "Recording", "subtitle": "Live TV - Session", "progress": 0,
            "Context": {"deviceID": "1", "grabber": "tv.plex.grabbers.hdhomerun", "itemKey": "", "itemMediaIndex": "0",
                        "itemRatingKey": "", "source": format!("provider://{IDENTIFIER}"), "status": "inprogress",
                        "subscriptionID": subscription.to_string(), "transcode_session": uuid}});
        self.push_activity(&activity, "started");
        let activity_uuid = activity.get("uuid").map(string_value).unwrap_or_default();
        self.livetv.tuned.insert(uuid.clone(), Tuned { row: row.clone(), client, at, seen: stamp, media, activity });
        let recent = self.livetv.recent.entry(self.viewer.id).or_default();
        recent.retain(|recent| recent != number);
        recent.insert(0, number.to_string());
        let (programme, current) = self.tuned_programme(guide, channel, at);
        info!("LIVETV tune {number} ({}) as /livetv/sessions/{uuid} on /library/metadata/{row}", programme.title);
        let mut video = Map::new();
        video.insert("_element".to_string(), json!("Video"));
        video.insert("addedAt".to_string(), json!(guide.added));
        video.insert("duration".to_string(), json!(current.map_or(0, |at| (guide.airings[at].ends - guide.airings[at].begins) * 1000)));
        if programme.episode {
            let show = show_guid(&programme.show);
            video.insert("grandparentGuid".to_string(), json!(show));
            video.insert("grandparentKey".to_string(), json!(item_key(&show)));
            video.insert("grandparentRatingKey".to_string(), json!(rating_key(&show)));
            video.insert("grandparentTitle".to_string(), json!(programme.show));
        }
        video.insert("guid".to_string(), json!(programme.guid));
        if programme.episode {
            video.insert("index".to_string(), json!(programme.index));
        }
        video.insert("key".to_string(), json!(format!("/livetv/sessions/{uuid}")));
        video.insert("onAir".to_string(), json!("1"));
        if programme.episode {
            video.insert("parentGuid".to_string(), json!(season_guid(&programme.show, programme.season)));
            video.insert("parentIndex".to_string(), json!(programme.season));
            video.insert("parentTitle".to_string(), json!(format!("Season {}", programme.season)));
        }
        video.insert("ratingKey".to_string(), json!(row));
        if programme.episode {
            video.insert("skipParent".to_string(), json!("1"));
        }
        video.insert("summary".to_string(), json!(programme.summary));
        video.insert("title".to_string(), json!(programme.title));
        video.insert("type".to_string(), json!(if programme.episode { "episode" } else { "movie" }));
        video.insert("Media".to_string(), json!([self.capture_media(&guide.channels[channel], &uuid, media, &session, true)]));
        if !programme.episode && !programme.categories.is_empty() {
            video.insert("Genre".to_string(), guide.genre_tags(&programme.categories, false));
        }
        video.insert("TranscodeSession".to_string(), self.capture_session(guide, channel, &uuid, at));
        let operation = format!("{number}-{session}");
        // The airing on now and the next two on this channel.
        let timeline = (0..guide.airings.len())
            .filter(|index| guide.airings[*index].channel == channel && guide.airings[*index].ends > at)
            .take(3)
            .map(|index| {
                let airing = &guide.airings[index];
                guide.row(&guide.programmes[airing.programme], &[airing], Shape::Tune)
            })
            .collect::<Vec<_>>();
        let also = (0..guide.channels.len())
            .filter(|other| *other != channel)
            .filter_map(|other| guide.current(other))
            .map(|index| {
                let airing = &guide.airings[index];
                guide.row(&guide.programmes[airing.programme], &[airing], Shape::Tune)
            })
            .collect::<Vec<_>>();
        let mut subscription_row = json!({
            "key": subscription.to_string(), "type": 4, "createdAt": at, "title": "This Episode",
            "airingsType": "New Airings Only", "channelIdentifier": number,
            "MediaGrabOperation": [{
                "rolling": true, "mediaSubscriptionID": subscription, "mediaIndex": 0, "id": operation,
                "key": format!("/media/grabbers/operations/{operation}"), "grabberIdentifier": "tv.plex.grabbers.hdhomerun",
                "grabberProtocol": "livetv", "percent": -1, "currentSize": 0, "deviceID": 1, "status": "inprogress",
                "provider": PROVIDER, "Metadata": Value::Object(video)}]
        });
        if !timeline.is_empty() {
            subscription_row["Timeline"] = json!({"Metadata": timeline});
        }
        if !also.is_empty() {
            subscription_row["AlsoAiring"] = json!({"Metadata": also});
        }
        let mut response = Response::json(200, json!({"MediaContainer": {"size": 1, "MediaSubscription": [subscription_row]}}));
        response.headers.push(("X-Plex-Activity".to_string(), activity_uuid));
        response.headers.push(("Access-Control-Expose-Headers".to_string(), "X-Plex-Activity".to_string()));
        response
    }

    /// A tuned session over: forgotten, and its activity ended.
    fn end_tuned(&mut self, uuid: &str) {
        if let Some(tuned) = self.livetv.tuned.remove(uuid) {
            self.push_activity(&tuned.activity, "ended");
        }
    }

    /// `/activities`: the tuned sessions' activities (every other activity of rex's starts and
    /// ends inside the request that ran it).
    pub(super) fn livetv_activities(&self) -> Value {
        let mut rows = self.livetv.tuned.values().map(|tuned| (tuned.at, tuned.activity.clone())).collect::<Vec<_>>();
        rows.sort_by_key(|(at, _)| *at);
        if rows.is_empty() {
            return json!({"MediaContainer": {"size": 0}});
        }
        json!({"MediaContainer": {"size": rows.len(), "Activity": rows.into_iter().map(|(_, row)| row).collect::<Vec<_>>()}})
    }

    /// After the route: a tuned session's decision and Now Playing row describe the session, as
    /// PMS's do (`live`, the session's key, the airing's title), not the channel's row.
    pub(super) fn livetv_finish(&self, request: &Request, response: &mut Response) {
        if self.livetv.tuned.is_empty() || response.status != 200 || !response.content_type.starts_with("application/json") {
            return;
        }
        let (path, raw_query) = split_target(&request.target);
        let decision = path == "/video/:/transcode/universal/decision";
        if !decision && path != "/status/sessions" {
            return;
        }
        let Some(guide) = self.guide() else {
            return;
        };
        let session = if decision {
            let wanted = parse_query(&raw_query).into_iter().rev().find(|(name, _)| name == "path").map(|(_, value)| value).unwrap_or_default();
            match self.tuned_by_path(&wanted) {
                Some(found) => Some(found),
                None => return,
            }
        } else {
            None
        };
        let Some(mut document) = response.take_document() else {
            return;
        };
        if let Some(container) = document.get_mut("MediaContainer").and_then(Value::as_object_mut) {
            if decision {
                // Measured: a live session's decision carries none of a library's container fields.
                for name in ["allowSync", "identifier", "librarySectionID", "librarySectionTitle", "librarySectionUUID", "mediaTagPrefix", "mediaTagVersion"] {
                    container.shift_remove(name);
                }
            }
            for row in container.get_mut("Metadata").and_then(Value::as_array_mut).into_iter().flatten() {
                let key = row.get("ratingKey").map(string_value).unwrap_or_default();
                let player = row.pointer("/Player/machineIdentifier").and_then(Value::as_str).unwrap_or_default().to_string();
                // Now Playing: the session its own player tuned, newest first; the same channel
                // played from its library by another player stays that library row.
                let found = session.clone().or_else(|| {
                    let mut matching = self.livetv.tuned.iter().filter(|(_, tuned)| tuned.row == key && tuned.client == player).collect::<Vec<_>>();
                    matching.sort_by_key(|(_, tuned)| -tuned.at);
                    matching.first().map(|(uuid, tuned)| ((*uuid).clone(), (*tuned).clone()))
                });
                let Some((uuid, tuned)) = found.filter(|(_, tuned)| tuned.row == key) else {
                    continue;
                };
                let Some(channel) = guide.channels.iter().position(|channel| channel.key == tuned.row) else {
                    continue;
                };
                let (programme, _) = self.tuned_programme(&guide, channel, tuned.at);
                let Some(object) = row.as_object_mut() else {
                    continue;
                };
                // Measured (Plex Web's decision and Now Playing row): the session's own fields
                // and the conversion's Media and play state, nothing of a library row.
                let kept = ["Media", "User", "Player", "Session", "TranscodeSession", "sessionKey", "_element"];
                object.retain(|name, _| kept.contains(&name.as_str()));
                object.insert("addedAt".to_string(), json!(tuned.at));
                object.insert("genuineMediaAnalysis".to_string(), json!("1"));
                if programme.episode {
                    object.insert("grandparentTitle".to_string(), json!(programme.show));
                    object.insert("index".to_string(), json!(programme.index));
                    object.insert("parentIndex".to_string(), json!(programme.season));
                }
                object.insert("guid".to_string(), json!(programme.guid));
                object.insert("key".to_string(), json!(format!("/livetv/sessions/{uuid}")));
                if !decision {
                    object.insert("librarySectionID".to_string(), json!("-4"));
                }
                object.insert("live".to_string(), json!("1"));
                object.insert("ratingKey".to_string(), json!(tuned.row));
                object.insert("title".to_string(), json!(programme.title));
                object.insert("type".to_string(), json!(if programme.episode { "episode" } else { "movie" }));
                for media in object.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                    if let Some(media) = media.as_object_mut() {
                        media.insert("origin".to_string(), json!("livetv"));
                        media.insert("uuid".to_string(), json!(uuid));
                    }
                }
            }
        }
        response.store_document(document);
    }
}

/// What a guide key names.
enum Item {
    Programme(usize),
    Show(String),
    Season(String, i64),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_escape_as_measured() {
        let guid = format!("tv.plex.xmltv://movie/{}", guid_part("Big Buck Bunny"));
        assert_eq!(guid, "tv.plex.xmltv://movie/Big%20Buck%20Bunny");
        assert_eq!(rating_key(&guid), "tv%2Eplex%2Exmltv%3A%2F%2Fmovie%2FBig%2520Buck%2520Bunny");
        assert_eq!(rating_key(&show_guid("Firefly")), "tv%2Eplex%2Exmltv%3A%2F%2Fshow%2FFirefly");
    }

    #[test]
    fn guide_times() {
        assert_eq!(guide_time(&json!("2026-10-31T13:00:00Z")), Some(1793451600));
        assert_eq!(guide_time(&json!("2026-10-31T14:00:00+01:00")), Some(1793451600));
        assert_eq!(guide_time(&json!(1793451600)), Some(1793451600));
        assert_eq!(guide_time(&json!("2026-10-31 13:00")), Some(1793451600));
        assert_eq!(guide_time(&json!("yesterday")), None);
    }

    #[test]
    fn channel_field_keeps_a_guide_and_refuses_a_bad_one() {
        let kept = channel_field(&json!({"number": "2.1", "title": "Rex News", "guide": [
            {"start": "2026-10-31T13:30:00Z", "end": 1793455200, "title": "Episode 3", "type": "episode", "show": "Firefly", "season": 1, "episode": 3, "new": true},
            {"start": 1793451600, "end": 1793453400, "title": "Rex Evening News", "type": "show", "categories": ["News"]}]}))
        .unwrap();
        assert_eq!(kept["guide"][0]["title"], "Rex Evening News");
        assert_eq!(kept["guide"][0]["type"], "movie");
        assert_eq!(kept["guide"][1]["start"], 1793453400);
        assert!(channel_field(&json!({"number": "2/1"})).is_err());
        assert!(channel_field(&json!({"number": "2.1", "guide": [{"start": 2, "end": 1, "title": "x"}]})).is_err());
        assert!(channel_field(&json!({"number": "2.1", "guide": [{"start": 1, "end": 2, "title": "x", "type": "episode"}]})).is_err());
    }
}
