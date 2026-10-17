//! Playback sessions, the device table and the byte counters behind the Dashboard,
//! ported from `sessions.py` and the `server.py` routes that read them.
//!
//! A session is created by `/:/timeline`, keyed by the playback session's identity
//! (`X-Plex-Session-Identifier`, else the client identifier, else `rex`), and
//! numbered by a server counter whose first key is 2.  Its `/status/sessions` row is
//! projected from the LIVE catalog row when the list is read, so an edit made while it
//! plays shows up on the next read.

use super::*;

/// One live playback session (`Sessions._sessions` entry).
#[derive(Debug, Clone)]
pub(crate) struct SessionEntry {
    pub session_key: String,
    pub id: String,
    pub rating_key: String,
    /// The row as the last beat found it: `entry["row"]` holds the row itself, so a
    /// session on a row deleted since still lists it.
    pub row: Value,
    /// A playlist's ratingKey drove the timeline (`entry["_list"]`).
    pub list: bool,
    pub view_offset: i64,
    pub seen: f64,
    pub player: Map<String, Value>,
    pub session: Value,
    pub user: Value,
    /// Listed from its first `playing` beat (`_listed`).
    pub listed: bool,
    /// When its run of `paused` beats began, 0 while it is not paused.
    pub paused_since: f64,
}

/// `Sessions.PLAYER_FROM`: header (or query parameter) -> the `Player` field it fills.
const PLAYER_FROM: &[(&str, &str)] = &[
    ("X-Plex-Client-Identifier", "machineIdentifier"),
    ("X-Plex-Device", "device"),
    ("X-Plex-Device-Name", "title"),
    ("X-Plex-Product", "product"),
    ("X-Plex-Version", "version"),
    ("X-Plex-Platform", "platform"),
    ("X-Plex-Platform-Version", "platformVersion"),
    ("X-Plex-Model", "model"),
    ("X-Plex-Playback-Id", "playbackId"),
    ("X-Plex-Playback-Session-Id", "playbackSessionId"),
];

/// A session's Player as Plex's webhook payload names it (support.plex.tv, "Webhooks").
fn webhook_player(player: &Map<String, Value>) -> Value {
    let field = |name: &str| player.get(name).cloned().unwrap_or(json!(""));
    json!({"local": player.get("local").cloned().unwrap_or(json!(false)),
           "publicAddress": field("address"),
           "title": field("title"),
           "uuid": field("machineIdentifier")})
}

/// `filters.RESOLUTION_TITLES`: the label a Bandwidth row names its resolution by.
fn resolution_title(resolution: &str) -> String {
    match resolution {
        "sd" => "SD",
        "480" => "480p",
        "576" => "576p",
        "720" => "720p",
        "1080" => "1080p",
        "2k" => "2K",
        "2.7k" => "2.7K",
        "4k" => "4K",
        "8k" => "8K",
        "16k" => "16K",
        other => other,
    }
    .to_string()
}

/// `query.get(name) or headers.get(name)`: an empty value falls through.
pub(super) fn plex_value(
    query: &HashMap<String, String>,
    headers: &HashMap<String, String>,
    name: &str,
) -> Option<String> {
    query
        .get(name)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            headers
                .get(&name.to_ascii_lowercase())
                .filter(|value| !value.is_empty())
        })
        .cloned()
}

/// `sessions._private`: loopback, RFC 1918, CGNAT and an unknown peer count as LAN.
pub(super) fn private_address(address: &str) -> bool {
    address.is_empty()
        || address.starts_with("127.")
        || address.starts_with("10.")
        || address.starts_with("192.168.")
        || address.starts_with("100.")
        || address.starts_with("172.")
        || address == "::1"
}

/// The peer address of the connection this thread is serving, without its port.
pub(super) fn current_peer_ip() -> String {
    let slot = CONNECTION_SLOT.with(|cell| cell.get());
    let peer = LIVE_CONNECTIONS
        .lock()
        .ok()
        .and_then(|rows| rows.iter().find(|row| row.slot == slot).map(|row| row.peer.clone()))
        .unwrap_or_default();
    match peer.parse::<std::net::SocketAddr>() {
        Ok(address) => address.ip().to_string(),
        Err(_) => String::new(),
    }
}

/// `sessions._stored_platform`: a numeric `X-Plex-Platform` is stored typed -- an int,
/// or a float when it has a point or an exponent.
pub(super) fn stored_platform(text: &str) -> Value {
    let trimmed = text.trim_matches(|ch| ch == ' ' || ch == '\t');
    // Compiled once: every request that names a device came through here.
    static NUMERIC: std::sync::OnceLock<Option<regex::Regex>> = std::sync::OnceLock::new();
    let numeric = NUMERIC
        .get_or_init(|| regex::Regex::new(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$").ok())
        .as_ref()
        .is_some_and(|pattern| pattern.is_match(trimmed));
    if !numeric {
        return json!(text);
    }
    if trimmed.contains('.') || trimmed.to_ascii_lowercase().contains('e') {
        return trimmed
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .and_then(serde_json::Number::from_f64)
            .map(Value::Number)
            .unwrap_or_else(|| json!(text));
    }
    trimmed
        .trim_start_matches('+')
        .parse::<i64>()
        .map(|value| json!(value))
        .unwrap_or_else(|_| json!(text))
}

/// Python's `round()`: half to even.
fn round_half_even(value: f64) -> i64 {
    let floor = value.floor();
    let fraction = value - floor;
    let rounded = if (fraction - 0.5).abs() < f64::EPSILON {
        if (floor as i64) % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        value.round()
    };
    rounded as i64
}

/// Who a response's bytes are counted against: `_count_bytes`'s identifier (header,
/// else the query's LAST value), device name and platform (header, else the query's
/// first value), and whether the peer is on the LAN.
#[derive(Debug, Clone, Default)]
pub(super) struct ByteOwner {
    pub identifier: String,
    pub name: String,
    pub platform: String,
    pub lan: bool,
}

impl ByteOwner {
    pub(super) fn of(request: &Request) -> Self {
        let (_, raw_query) = split_target(&request.target);
        let pairs = parse_query(&raw_query);
        let header = |name: &str| {
            request
                .headers
                .get(&name.to_ascii_lowercase())
                .filter(|value| !value.is_empty())
                .cloned()
        };
        let first = |name: &str| {
            pairs
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.clone())
        };
        let identifier = header("X-Plex-Client-Identifier")
            .or_else(|| {
                pairs
                    .iter()
                    .rev()
                    .find(|(key, _)| key == "X-Plex-Client-Identifier")
                    .map(|(_, value)| value.clone())
            })
            .unwrap_or_default();
        Self {
            identifier,
            name: header("X-Plex-Device-Name")
                .or_else(|| first("X-Plex-Device-Name"))
                .unwrap_or_default(),
            platform: header("X-Plex-Platform")
                .or_else(|| first("X-Plex-Platform"))
                .unwrap_or_default(),
            lan: private_address(&current_peer_ip()),
        }
    }
}

thread_local! {
    /// The owner of the response this thread is writing, for a streamed part's bytes.
    pub(super) static BYTE_OWNER: std::cell::RefCell<ByteOwner> =
        std::cell::RefCell::new(ByteOwner::default());
}

impl Runtime {
    /// `Sessions.device_id`: register the client (the anonymous one is 1, every other
    /// takes the next number) and write any device name or platform it sent.
    pub(super) fn note_device(&mut self, identifier: &str, name: &str, platform: &str) -> i64 {
        let id = self.register_device(identifier);
        if !name.is_empty() || !platform.is_empty() {
            let previous = self.devices.get(identifier).cloned();
            let device = Device {
                name: if name.is_empty() {
                    previous.as_ref().map(|device| device.name.clone()).unwrap_or_default()
                } else {
                    name.to_string()
                },
                product: previous
                    .as_ref()
                    .map(|device| device.product.clone())
                    .unwrap_or_default(),
                platform: if platform.is_empty() {
                    previous
                        .as_ref()
                        .map(|device| device.platform.clone())
                        .unwrap_or(json!(""))
                } else {
                    stored_platform(platform)
                },
                last_seen: now(),
            };
            let changed = previous
                .as_ref()
                .is_none_or(|known| known.name != device.name || known.platform != device.platform);
            self.devices.insert(identifier.to_string(), device);
            if changed {
                self.dirty = true;
            }
        }
        id
    }

    /// `Sessions.record_bytes`: count a response against its (device, lan) bucket for
    /// this second.  A zero count still registers the device and its name.
    pub(super) fn record_bytes(&mut self, owner: &ByteOwner, count: u64) {
        let device = self.note_device(&owner.identifier, &owner.name, &owner.platform);
        self.bandwidth_bytes = self.bandwidth_bytes.saturating_add(count);
        let second = now();
        let key = (device, owner.lan);
        let index = match self.byte_buckets.iter().position(|(known, _)| *known == key) {
            Some(index) => index,
            None => {
                self.byte_buckets.push((key, Vec::new()));
                self.byte_buckets.len() - 1
            }
        };
        let buckets = &mut self.byte_buckets[index].1;
        match buckets.iter_mut().find(|(at, _)| *at == second) {
            Some((_, total)) => *total += count as i64,
            None => buckets.push((second, count as i64)),
        }
        // The counters are persisted state (`persist.py` SESSION_FIELDS `_bytes`); a
        // read marks them dirty for the coalescing saver rather than saving before it
        // answers -- this runs after the answer is written.
        self.dirty = true;
    }

    /// `Sessions.bandwidth`: rows rolled into the asked-for bucket; with no timespan,
    /// every rollup (0 to 4) in turn.
    pub(super) fn bandwidth_series(&self, timespan: Option<i64>, since: Option<i64>) -> Vec<Value> {
        let Some(span) = timespan else {
            return (0..=4)
                .flat_map(|span| self.bandwidth_series(Some(span), since))
                .collect();
        };
        let mut rows: Vec<((i64, bool, i64), i64)> = Vec::new();
        for ((device, lan), buckets) in &self.byte_buckets {
            for (second, count) in buckets {
                if *count == 0 {
                    continue;
                }
                let at = statistics_bucket(*second, span);
                if since.is_some_and(|since| at < since) {
                    continue;
                }
                let key = (*device, *lan, at);
                match rows.iter_mut().find(|(known, _)| *known == key) {
                    Some((_, total)) => *total += count,
                    None => rows.push((key, *count)),
                }
            }
        }
        rows.sort_by_key(|((_, _, at), _)| *at);
        rows.into_iter()
            .map(|((device, lan, at), count)| {
                json!({"accountID": 1, "deviceID": device, "timespan": span,
                       "at": at, "lan": lan, "bytes": count})
            })
            .collect()
    }

    fn statistics_account(&self) -> Value {
        json!({"id": 1, "key": "/accounts/1", "name": self.owner_name(),
               "defaultAudioLanguage": "en", "autoSelectAudio": true,
               "defaultSubtitleLanguage": "en", "subtitleMode": 2, "thumb": ""})
    }

    /// `statistics("bandwidth")` past the shared grammar (`statistics_refusal`).
    pub(super) fn route_bandwidth(&self, query: &HashMap<String, String>) -> Response {
        let bare_empty = || Response::json(200, json!({"MediaContainer": {"size": 0}}));
        let timespan = match query.get("timespan").filter(|value| !value.is_empty()) {
            Some(value) => {
                let span = value.trim_start_matches('+').parse::<i64>().unwrap_or(-99);
                if !matches!(span, 0 | 1 | 2 | 3 | 4 | 6) {
                    return bare_empty();
                }
                Some(span)
            }
            None => None,
        };
        let bound = |name: &str| {
            query
                .get(name)
                .filter(|value| !value.is_empty())
                .and_then(|value| value.parse::<i64>().ok())
        };
        let (since, until, exact) = (bound("at>"), bound("at<"), bound("at="));
        let mut rows = self.bandwidth_series(timespan, since);
        let at = |row: &Value| row.get("at").and_then(Value::as_i64).unwrap_or(0);
        if let Some(until) = until {
            rows.retain(|row| at(row) <= until);
        }
        if let Some(exact) = exact {
            rows.retain(|row| at(row) == exact);
        }
        for (name, field) in [("accountID", "accountID"), ("deviceID", "deviceID")] {
            if let Some(value) = query.get(name) {
                if !int_param(value, None) {
                    return Response::error(400, "rex: bad request");
                }
                let number = value.trim_start_matches('+').parse::<i64>().unwrap_or(0);
                rows.retain(|row| row.get(field).and_then(Value::as_i64) == Some(number));
            }
        }
        if let Some(lan) = query.get("lan") {
            if !matches!(lan.as_str(), "0" | "1") {
                return Response::error(400, "rex: bad request");
            }
            let want = lan == "1";
            rows.retain(|row| row.get("lan").and_then(Value::as_bool) == Some(want));
        }
        let rows = statistics_window(rows, query);
        if rows.is_empty() {
            // Measured: nothing but `size`; Device and Account only appear with a series.
            return bare_empty();
        }
        let devices = self.statistics_devices();
        let size = rows.len() + devices.len() + 1;
        Response::json(
            200,
            json!({"MediaContainer": {"size": size, "Device": devices,
                                      "Account": [self.statistics_account()],
                                      "StatisticsBandwidth": rows}}),
        )
    }

    /// `/devices`: every client the server has seen, in id order.  A numeric platform
    /// trips PMS's string cast on this list (500) while the row stays readable.
    pub(super) fn route_devices(&self) -> Response {
        if self
            .devices
            .values()
            .any(|device| device.platform.is_number())
        {
            return Response::error(500, "rex: internal error");
        }
        let rows = self.statistics_devices();
        Response::json(
            200,
            json!({"MediaContainer": {"size": rows.len(),
                                      "identifier": "com.plexapp.system.devices",
                                      "Device": rows}}),
        )
    }

    /// `/devices/{id}`: one row, and a 404 for an id the server never assigned.
    pub(super) fn route_device(&self, id: &str) -> Response {
        let Some(row) = self
            .statistics_devices()
            .into_iter()
            .find(|row| row.get("id").map(string_value).as_deref() == Some(id))
        else {
            return Response::error(404, "rex: not found");
        };
        Response::json(
            200,
            json!({"MediaContainer": {"size": 1,
                                      "identifier": "com.plexapp.system.devices",
                                      "Device": [row]}}),
        )
    }

    /// `Sessions.touch`: create or update the session a timeline describes.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn touch_session(
        &mut self,
        rating_key: &str,
        list: bool,
        bitrate: i64,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
        state: &str,
        offset: i64,
        profile: Option<String>,
    ) -> SessionEntry {
        let identifier = plex_value(query, headers, "X-Plex-Client-Identifier").unwrap_or_default();
        let session_id = plex_value(query, headers, "X-Plex-Session-Identifier")
            .or_else(|| (!identifier.is_empty()).then(|| identifier.clone()))
            .unwrap_or_else(|| "rex".to_string());
        if !self.sessions.contains_key(&session_id) {
            self.next_session_key += 1;
            self.sessions.insert(
                session_id.clone(),
                SessionEntry {
                    session_key: self.next_session_key.to_string(),
                    id: session_id.clone(),
                    rating_key: String::new(),
                    row: Value::Null,
                    list: false,
                    view_offset: 0,
                    seen: 0.0,
                    player: Map::new(),
                    session: Value::Null,
                    user: Value::Null,
                    listed: false,
                    paused_since: 0.0,
                },
            );
        }
        let address = current_peer_ip();
        let local = private_address(&address);
        let mut player = Map::new();
        player.insert("address".to_string(), json!(address));
        player.insert(
            "state".to_string(),
            json!(if state.is_empty() { "playing" } else { state }),
        );
        player.insert("local".to_string(), json!(local));
        player.insert("relayed".to_string(), json!(false));
        player.insert("secure".to_string(), json!(false));
        player.insert("userID".to_string(), json!(1));
        if let Some(profile) = profile {
            player.insert("profile".to_string(), json!(profile));
        }
        for (name, field) in PLAYER_FROM {
            if let Some(found) = plex_value(query, headers, name) {
                player.insert(field.to_string(), json!(found));
            }
        }
        if !local {
            player.insert("remotePublicAddress".to_string(), json!(address));
        }
        let owner = self.owner_name();
        let row = self.catalog.get(rating_key).cloned().unwrap_or(Value::Null);
        let entry = self.sessions.get_mut(&session_id).expect("session just ensured");
        let was = entry.player.get("state").and_then(Value::as_str).map(str::to_string);
        entry.rating_key = rating_key.to_string();
        entry.row = row;
        entry.list = list;
        entry.view_offset = offset;
        entry.seen = now_seconds();
        entry.player = player;
        // Measured as ~2.1x the media bitrate in kbps; the one approximation here.
        entry.session = json!({"id": session_id,
                               "bandwidth": round_half_even(bitrate as f64 * 2.1),
                               "location": if local { "lan" } else { "wan" }});
        entry.user = json!({"id": "1", "thumb": "", "title": owner});
        if state == "playing" {
            entry.listed = true;
        }
        if state != "paused" {
            entry.paused_since = 0.0;
        } else if entry.paused_since == 0.0 {
            entry.paused_since = entry.seen;
        }
        let snapshot = entry.clone();
        let event = match (was.as_deref(), state) {
            (Some(_), "stopped") => Some("media.stop"),
            (None, "playing" | "") => Some("media.play"),
            (Some("paused"), "playing" | "") => Some("media.resume"),
            (Some(before), "paused") if before != "paused" => Some("media.pause"),
            _ => None,
        };
        if let Some(event) = event {
            self.webhook(event, &snapshot.row, Some(webhook_player(&snapshot.player)));
        }
        self.end_long_pause(&snapshot);
        if state == "stopped" {
            self.sessions.remove(&session_id);
        }
        let name = plex_value(query, headers, "X-Plex-Device-Name").unwrap_or_default();
        let platform = plex_value(query, headers, "X-Plex-Platform").unwrap_or_default();
        self.note_device(&identifier, &name, &platform);
        snapshot
    }

    /// `MinutesAllowedPaused`: a session paused past it is ended as a terminate ends one,
    /// through its next timeline; 0 never ends one, and audio is excluded, as the setting's
    /// own summary says. Neither measured: the termination text is ours, not a real PMS's.
    fn end_long_pause(&mut self, entry: &SessionEntry) {
        if entry.paused_since == 0.0
            || entry.row.get("type").and_then(Value::as_str) == Some("track")
            || self.terminated_sessions.contains_key(&entry.id)
        {
            return;
        }
        let minutes = self.server_pref("MinutesAllowedPaused").and_then(|value| value.as_i64()).unwrap_or(0);
        if minutes > 0 && now_seconds() - entry.paused_since > minutes as f64 * 60.0 {
            let text = format!("Your playback was stopped after being paused for more than {minutes} minutes");
            info!("TERMINATE {}: paused over {minutes} min", entry.id);
            self.terminated_sessions.insert(entry.id.clone(), text);
        }
    }

    /// `entry["row"]` is the catalog row ITSELF, so every edit reaches it, and it
    /// survives the row's deletion.  Keep each session's copy current after every
    /// request, so a row deleted since is listed as it stood when it went.
    pub(super) fn refresh_session_rows(&mut self) {
        if self.sessions.is_empty() {
            return;
        }
        let catalog = &self.catalog;
        for entry in self.sessions.values_mut() {
            if let Some(row) = catalog.get(&entry.rating_key) {
                if entry.row != *row {
                    entry.row = row.clone();
                }
            }
        }
    }

    /// `Sessions.live`'s reaper, measured in cycles 26 and 41: a process-wide tick every
    /// 30 s drops each session whose last beat is older than the tick minus 180 s, so a
    /// silent player stays in Now Playing for 180-210 s -- not a per-row timer.
    pub(super) fn reap_sessions(&mut self) {
        const TTL: f64 = 180.0;
        const INTERVAL: f64 = 30.0;
        let elapsed = now_seconds() - self.last_reap;
        if elapsed < INTERVAL {
            return;
        }
        self.last_reap += (elapsed / INTERVAL).floor() * INTERVAL;
        let cutoff = self.last_reap - TTL;
        self.sessions.retain(|_, entry| entry.seen >= cutoff);
    }

    /// `server.py` `session_rows`: the live sessions, oldest key first, each the LIVE
    /// catalog row projected by the session serialiser.
    pub(super) fn session_listing(&self) -> Vec<Value> {
        let mut entries = self.sessions.values().filter(|entry| entry.listed).collect::<Vec<_>>();
        entries.sort_by_key(|entry| entry.session_key.parse::<i64>().unwrap_or(0));
        let mut rows = Vec::new();
        for entry in entries {
            if entry.list {
                // Cycle 59: a playlist's session row is the LISTING row -- its state
                // block included, no empty `summary` -- with the session's `sessionKey`
                // and `viewOffset`, and only `User` and `Player`.
                let Some(playlist) = self.playlists.get(&entry.rating_key) else {
                    continue;
                };
                let mut item = self.dynamic_playlist_row(playlist);
                if let Some(object) = item.as_object_mut() {
                    if object
                        .get("summary")
                        .is_some_and(|value| string_value(value).is_empty())
                    {
                        object.shift_remove("summary");
                    }
                    for (name, value) in self.list_state.get(&entry.rating_key).cloned().unwrap_or_default() {
                        if !name.starts_with('_') {
                            object.insert(name, value);
                        }
                    }
                    object.insert("sessionKey".to_string(), json!(entry.session_key));
                    object.insert("viewOffset".to_string(), json!(entry.view_offset));
                    object.insert("User".to_string(), json!({"id": "1"}));
                    object.insert("Player".to_string(), Value::Object(entry.player.clone()));
                    object.insert("_session".to_string(), json!(true));
                }
                rows.push(item);
                continue;
            }
            let row = match self.catalog.get(&entry.rating_key) {
                Some(row) => row,
                None if entry.row.is_object() => &entry.row,
                None => continue,
            };
            let mut item = self.detail_row(row);
            let decided = self
                .decided_sessions
                .contains(&(entry.id.clone(), entry.rating_key.clone()));
            let Some(object) = item.as_object_mut() else {
                continue;
            };
            object.shift_remove("Guid");
            object.shift_remove("Image");
            object.insert("sessionKey".to_string(), json!(entry.session_key));
            object.insert("viewOffset".to_string(), json!(entry.view_offset));
            let text_id = |value: &mut Map<String, Value>| {
                if let Some(id) = value.get("id").filter(|id| !id.is_null()).cloned() {
                    value.insert("id".to_string(), json!(string_value(&id)));
                }
            };
            if let Some(media_rows) = object.get("Media").and_then(Value::as_array).cloned() {
                let mut projected = Vec::new();
                for media in media_rows {
                    let Some(mut media) = media.as_object().cloned() else {
                        continue;
                    };
                    let parts = media
                        .get("Part")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    let mut played_parts = Vec::new();
                    for part in parts {
                        let Some(mut part) = part.as_object().cloned() else {
                            continue;
                        };
                        crate::catalog::unpack_part(&mut part);
                        if decided {
                            part.insert("decision".to_string(), json!("directplay"));
                            part.insert("selected".to_string(), json!(true));
                        }
                        if let Some(streams) = part.get("Stream").and_then(Value::as_array).cloned() {
                            if !streams.is_empty() {
                                let streams = streams
                                    .into_iter()
                                    .filter_map(|stream| stream.as_object().cloned())
                                    .map(|mut stream| {
                                        if decided {
                                            stream.insert("location".to_string(), json!("direct"));
                                        }
                                        text_id(&mut stream);
                                        stream.insert("_session".to_string(), json!(true));
                                        Value::Object(stream)
                                    })
                                    .collect::<Vec<_>>();
                                part.insert("Stream".to_string(), Value::Array(streams));
                            }
                        }
                        text_id(&mut part);
                        part.insert("_session".to_string(), json!(true));
                        played_parts.push(Value::Object(part));
                    }
                    if decided {
                        media.insert("selected".to_string(), json!(true));
                    }
                    media.insert("Part".to_string(), Value::Array(played_parts));
                    text_id(&mut media);
                    if let Some(aspect) = media.get("aspectRatio").filter(|value| !value.is_null()).cloned() {
                        media.insert("aspectRatio".to_string(), json!(string_value(&aspect)));
                    }
                    media.insert("_session".to_string(), json!(true));
                    projected.push(Value::Object(media));
                }
                if !projected.is_empty() {
                    object.insert("Media".to_string(), Value::Array(projected));
                }
            }
            for name in [
                "Genre", "Country", "Director", "Writer", "Role", "Producer", "Collection",
                "Similar", "Guid", "Field", "Label", "Style", "Mood",
            ] {
                let Some(tags) = object.get(name).and_then(Value::as_array).cloned() else {
                    continue;
                };
                if tags.is_empty() {
                    continue;
                }
                let tags = tags
                    .into_iter()
                    .filter_map(|tag| tag.as_object().cloned())
                    .map(|mut tag| {
                        tag.insert("_session".to_string(), json!(true));
                        text_id(&mut tag);
                        Value::Object(tag)
                    })
                    .collect::<Vec<_>>();
                object.insert(name.to_string(), Value::Array(tags));
            }
            if let Some(ratings) = object.get("Rating").and_then(Value::as_array).cloned() {
                if !ratings.is_empty() {
                    let ratings = ratings
                        .into_iter()
                        .filter_map(|rating| rating.as_object().cloned())
                        .map(|mut rating| {
                            rating.insert("_session".to_string(), json!(true));
                            if let Some(value) = rating.get("value").filter(|v| !v.is_null()).cloned() {
                                rating.insert("value".to_string(), json!(python_str(&value)));
                            }
                            Value::Object(rating)
                        })
                        .collect::<Vec<_>>();
                    object.insert("Rating".to_string(), Value::Array(ratings));
                }
            }
            if let Some(colors) = object.get("UltraBlurColors").and_then(Value::as_object).cloned() {
                let mut colors = colors;
                colors.insert("_session".to_string(), json!(true));
                object.insert("UltraBlurColors".to_string(), json!([colors]));
            }
            if let Some(section) = object.get("librarySectionID").filter(|v| !v.is_null()).cloned() {
                object.insert("librarySectionID".to_string(), json!(string_value(&section)));
            }
            object.insert("_session".to_string(), json!(true));
            object.insert("User".to_string(), entry.user.clone());
            object.insert("Player".to_string(), Value::Object(entry.player.clone()));
            // Cycle 61: `Session` is there only once THIS playback session has asked for
            // a decision, and it stays when the session moves on to another row.
            if self.decided_sessions.iter().any(|(session, _)| *session == entry.id) {
                object.insert("Session".to_string(), entry.session.clone());
            }
            self.session_transcode(&entry.id, &entry.rating_key, object);
            rows.push(item);
        }
        rows
    }

    /// `/status/sessions`: the listing, paged by the container window.
    pub(super) fn route_sessions(&mut self, query: &HashMap<String, String>) -> Response {
        self.reap_sessions();
        let rows = self.session_listing();
        let (page, paged, start, total) = match page_window(rows, query) {
            Ok(window) => window,
            Err(status) => return Response::error(status, "rex: bad container parameter"),
        };
        let mut fields = Map::new();
        fields.insert("size".to_string(), json!(page.len()));
        if paged {
            fields.insert("totalSize".to_string(), json!(total));
            fields.insert("offset".to_string(), json!(start));
        }
        if !page.is_empty() {
            fields.insert("Metadata".to_string(), Value::Array(page));
        }
        Response::json(200, json!({"MediaContainer": fields}))
    }

    /// `/status/sessions/terminate`: keyed on `Session.id`, a 404 for an id that is not
    /// live, and the kill is delivered through that session's NEXT timeline.
    pub(super) fn route_terminate(&mut self, query: &HashMap<String, String>) -> Response {
        let Some(session_id) = query.get("sessionId").filter(|value| !value.is_empty()) else {
            return Response::error(400, "rex: bad request");
        };
        if !self.sessions.contains_key(session_id) {
            return Response::error(404, "rex: no such session");
        }
        let text = format!(
            "\"{}\" asked to stop your playback from this server: {}",
            self.owner_name(),
            query.get("reason").cloned().unwrap_or_default()
        );
        self.terminated_sessions.insert(session_id.clone(), text);
        info!("TERMINATE {session_id}: {}", logging::py_opt(query.get("reason")));
        Response::empty(200)
    }

    /// `server.py` `timeline`: the player's progress report, and what CREATES a session.
    pub(super) fn route_timeline(
        &mut self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
    ) -> Response {
        let mut rating_key = query.get("ratingKey").cloned().unwrap_or_default();
        // `state` is the ONE required parameter, and a closed set.
        let Some(state) = query.get("state").filter(|value| !value.is_empty()).cloned() else {
            return Response::error(400, "rex: bad request");
        };
        if !["playing", "paused", "buffering", "stopped"].contains(&state.as_str()) {
            return Response::error(400, "rex: bad request");
        }
        let digits = |value: &String| !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit());
        let offset = query.get("time");
        let duration = query.get("duration");
        if offset.is_some_and(|value| !digits(value))
            || duration.is_some_and(|value| !digits(value))
        {
            return Response::error(400, "rex: bad request");
        }
        let offset_ms = offset.and_then(|value| value.parse::<i64>().ok()).unwrap_or(0);
        let duration_ms = duration.and_then(|value| value.parse::<i64>().ok());
        if let (Some(_), Some(total)) = (offset, duration_ms) {
            if total > 0 && offset_ms > total {
                return Response::error(400, "rex: bad request");
            }
        }
        // `duration=0` is "no usable duration", the same as sending none.
        let total = duration_ms.filter(|value| *value > 0).unwrap_or(0);
        let numeric_key = digits(&rating_key);
        let mut in_catalog = numeric_key && self.catalog.get(&rating_key).is_some();
        let mut listed = numeric_key && !in_catalog && self.playlists.contains_key(&rating_key);
        if !rating_key.is_empty() && !in_catalog && !listed {
            return Response::error(404, "rex: no such item");
        }
        // The player reports which queue item it is on, and a real PMS MOVES the
        // queue's selection to it.
        if let Some(wanted) = query
            .get("playQueueItemID")
            .filter(|value| digits(value))
            .and_then(|value| value.parse::<i64>().ok())
        {
            let mut named = None;
            for queue in self.queues.values_mut() {
                if let Some(item) = queue.items.iter().find(|item| item.id == wanted) {
                    named = Some(item.rating_key.clone());
                    queue.selected = Some(wanted);
                    queue.deselected = false;
                    break;
                }
            }
            // Cycle 87, plexscratch42 (1.42.2): the queue item NAMES the item the beat is
            // for, over `ratingKey` -- `ratingKey=A&playQueueItemID=<B's row>` at 92%
            // completed B and stored B's resume point, 3/3; an id no queue holds falls back
            // to `ratingKey`.
            if let Some(named) = named.filter(|named| self.catalog.get(named).is_some()) {
                if in_catalog || listed {
                    rating_key = named;
                    in_catalog = true;
                    listed = false;
                }
            }
        } else if !query.contains_key("playQueueItemID") && (in_catalog || listed) {
            // Cycle 87, plexscratch42 (1.42.2): a beat with NO `playQueueItemID` at all
            // moves the selection of the queue its CLIENT owns to that queue's first row
            // of the beat's `ratingKey` -- any state, with or without `time` -- and leaves
            // it alone when no row holds the item. Another client's beat moves nothing,
            // and an EMPTY `playQueueItemID=` (or `0`, `abc`, an id no queue holds) moves
            // nothing either: that is where empty and absent part.
            let client = plex_value(query, headers, "X-Plex-Client-Identifier").unwrap_or_default();
            if !client.is_empty() {
                if let Some(queue) = self.queues.values_mut().find(|queue| queue.client == client) {
                    if let Some(item) = queue.items.iter().find(|item| item.rating_key == rating_key) {
                        queue.selected = Some(item.id);
                        queue.deselected = false;
                    }
                }
            }
        }
        let mut playback_state = "ignore";
        let mut fields = Map::new();
        if in_catalog || listed {
            let key = rating_key.clone();
            let client = plex_value(query, headers, "X-Plex-Client-Identifier").unwrap_or_default();
            let device = self.register_device(&client);
            let playing_session = plex_value(query, headers, "X-Plex-Session-Identifier")
                .unwrap_or_else(|| client.clone());
            // The play's session tag, as `_completed` stores it (`session or True`).
            let session_tag = if playing_session.is_empty() {
                "\u{1}".to_string()
            } else {
                playing_session.clone()
            };
            // A completed play answers `complete` to every beat past 90% until a
            // `stopped` beat has closed it, and `ignore` to every beat after that --
            // cycle 87, plexscratch42: and only while the beats go on naming the SAME item
            // by the SAME `playQueueItemID` (another row's id, none, an empty one or one
            // no queue holds on either beat, or any other item's beat between, answers
            // the stop `ignore` with `viewCount` unchanged).
            //
            // The `progress` answer is the same latch's other half (cycle 87,
            // plexscratch42): it belongs to the session's current PLAY -- the same tag --
            // and outlives a stop. 70 000 on A, a stop, then 3 000 on A answers `progress`;
            // 70 000 on A then 3 000 on B answers `ignore` with no stop between; a change of
            // `playQueueItemID` on the same item resets it too. A stop hands the next play
            // its own position: past the minute keeps it, at or under it clears it. A
            // playlist's own beats keep this and the crossing position on a TRACK of their
            // own. See `server.py` `timeline`.
            let tag = (
                query.get("playQueueItemID").filter(|value| !value.is_empty()).cloned(),
                key.clone(),
            );
            let track = (session_tag.clone(), listed);
            let mut latch = self.timeline_latch.get(&track).cloned().unwrap_or_default();
            if latch.tag.as_ref() != Some(&tag) {
                latch = TimelineLatch { tag: Some(tag), ..TimelineLatch::default() };
            }
            let before = latch.progressed;
            let closed = latch.closed;
            let crossed = self.library_progress(
                &key,
                offset.map(|_| offset_ms),
                total,
                device,
                &session_tag,
                true,
                // A `stopped` beat never deletes the resume point.
                before && state != "stopped",
            );
            if crossed {
                latch.latched = true;
                if let Some(row) = self.catalog.get(&key).cloned() {
                    let player = self.sessions.get(&playing_session).map(|entry| webhook_player(&entry.player));
                    self.webhook("media.scrobble", &row, player);
                }
            } else if offset.is_some() && total > 0 && (offset_ms as f64) < total as f64 * 0.9 {
                // A beat back under 90% is a new play.
                latch.latched = false;
                latch.closed = false;
            }
            let finished = latch.latched;
            // Cycle 87 e3b: a near-end beat that completes nothing does not pass the minute
            // for the latch; a completion does.
            let near_end = total > 0 && offset_ms >= total.saturating_mul(9) / 10;
            let progressed = before || (offset_ms > 60000 && !near_end) || crossed;
            latch.progressed = progressed;
            if state == "stopped" {
                // A stop after a completion CLOSES it until a beat under 90%.
                latch.closed = closed || latch.latched;
                latch.latched = false;
                latch.progressed = offset_ms > 60000;
            }
            self.timeline_latch.insert(track, latch);
            let media = self
                .catalog
                .get(&key)
                .and_then(|row| row.get("Media"))
                .and_then(Value::as_array)
                .and_then(|media| media.first())
                .cloned()
                .unwrap_or(Value::Null);
            let bitrate = media.get("bitrate").and_then(Value::as_i64).unwrap_or(0);
            let profile = self.session_profile(query, headers);
            // A playing client beats every 10 s; only a new item or state is worth INFO.
            let beat_id = plex_value(query, headers, "X-Plex-Session-Identifier")
                .or_else(|| plex_value(query, headers, "X-Plex-Client-Identifier"))
                .unwrap_or_else(|| "rex".to_string());
            let repeat = self.sessions.get(&beat_id).is_some_and(|entry| {
                entry.rating_key == key
                    && entry.player.get("state").and_then(Value::as_str)
                        == Some(state.as_str())
            });
            let entry = self.touch_session(
                &key, listed, bitrate, query, headers, &state, offset_ms, profile,
            );
            // The `playing` frame, one per timeline.
            let mut frame = Map::new();
            frame.insert("sessionKey".to_string(), json!(entry.session_key));
            frame.insert(
                "clientIdentifier".to_string(),
                entry.player.get("machineIdentifier").cloned().unwrap_or(json!("")),
            );
            frame.insert("guid".to_string(), json!(""));
            frame.insert("ratingKey".to_string(), json!(key));
            frame.insert("url".to_string(), json!(""));
            frame.insert("key".to_string(), json!(format!("/library/metadata/{key}")));
            frame.insert("viewOffset".to_string(), json!(offset_ms));
            frame.insert("state".to_string(), json!(state));
            if let Some(wanted) = query
                .get("playQueueItemID")
                .filter(|value| digits(value))
                .and_then(|value| value.parse::<i64>().ok())
            {
                if let Some(queue) = self
                    .queues
                    .values()
                    .find(|queue| queue.items.iter().any(|item| item.id == wanted))
                {
                    frame.insert("playQueueItemID".to_string(), json!(wanted));
                    frame.insert(
                        "playQueueID".to_string(),
                        json!(queue.id.parse::<i64>().unwrap_or(0)),
                    );
                }
            }
            if listed {
                frame.insert("key".to_string(), json!(format!("/playlists/{key}")));
            }
            self.push_event(
                "playing",
                json!({"PlaySessionStateNotification": Value::Object(frame)}),
            );
            // Cycle 45: `playbackTime=` is a VALIDATED signed 32-bit integer.
            if query
                .get("playbackTime")
                .is_some_and(|value| !int_param(value, Some(32)))
            {
                self.save_if_dirty();
                return Response::error(400, "rex: bad request");
            }
            if total > 0 {
                playback_state = if progressed { "progress" } else { "ignore" };
                if offset_ms >= total.saturating_mul(9) / 10 {
                    // Cycle 87 e3b, plexscratch42: inside a play that had already passed
                    // the minute, a near-end beat that completes nothing answers `progress`,
                    // unless a completion's stop closed the play; a fresh play's stays
                    // `ignore`. See `server.py`.
                    playback_state = if finished {
                        "complete"
                    } else if before && !closed {
                        "progress"
                    } else {
                        "ignore"
                    };
                }
            }
            // Cycle 70: the OFFLINE form marks the item watched when `viewCount` is 1 or
            // more; `viewCount=0` on a watched item clears it.
            if !listed
                && query.get("offline").map(String::as_str) == Some("1")
                // Cycle 87 e3b: a beat the play latch answers `progress` syncs the same.
                && playback_state != "complete"
            {
                if let Some(count) = query
                    .get("viewCount")
                    .filter(|value| digits(value))
                    .and_then(|value| value.parse::<i64>().ok())
                {
                    let watched = self
                        .catalog
                        .get(&key)
                        .and_then(|row| row.get("viewCount"))
                        .and_then(Value::as_i64)
                        .unwrap_or(0)
                        > 0;
                    if count >= 1 {
                        self.scrobble_key_on(&key, true, None, device);
                        playback_state = "complete";
                    } else if watched {
                        self.scrobble_key_on(&key, false, None, device);
                        playback_state = "complete";
                    }
                }
            }
            if state == "stopped" && !listed {
                if let Some(row) = self.catalog.get(&key).cloned() {
                    let device = self.register_device(&client);
                    self.record_play(&row, offset_ms / 1000, device);
                }
            }
            // `Bandwidths` appears once a DECISION has been made for this item and then
            // on every timeline including `stopped`; withheld for Plex for Windows.
            let product = plex_value(query, headers, "X-Plex-Product");
            let session_pair = (
                plex_value(query, headers, "X-Plex-Session-Identifier")
                    .or_else(|| plex_value(query, headers, "X-Plex-Client-Identifier"))
                    .unwrap_or_else(|| "rex".to_string()),
                key.clone(),
            );
            if self.decided_sessions.contains(&session_pair)
                && bitrate != 0
                && !matches!(
                    product.as_deref(),
                    Some("Plex for Windows" | "Plex for Windows Phone")
                )
            {
                let resolution = media
                    .get("videoResolution")
                    .map(string_value)
                    .unwrap_or_default();
                fields.insert(
                    "Bandwidths".to_string(),
                    json!([{"Bandwidth": [{"time": 0,
                                           "bandwidth": round_half_even(bitrate as f64 * 2.1),
                                           "resolution": resolution_title(&resolution)}]}]),
                );
            }
            self.timeline_transcode(&session_pair.0, &key, &state, &mut fields);
            if state == "stopped" {
                self.decided_sessions.remove(&session_pair);
            }
            // A terminate is delivered HERE, not in the terminate response.
            if let Some(text) = self.terminated_sessions.get(&entry.id) {
                fields.insert("terminationCode".to_string(), json!(2006));
                fields.insert("terminationText".to_string(), json!(text));
            }
            let line = format!(
                "TIMELINE {rating_key} state={state} t={}ms session={}",
                logging::py_opt(query.get("time")),
                entry.session_key
            );
            if repeat {
                debug!("{line}");
            } else {
                info!("{line}");
            }
        }
        self.save_if_dirty();
        if !in_catalog && !listed {
            // With no ratingKey at all the answer is just the two fields.
            return Response::json(
                200,
                json!({"MediaContainer": {"size": 0, "playbackState": playback_state}}),
            );
        }
        let stored = self
            .view_state(&rating_key, "viewOffset")
            .and_then(|value| value.as_i64())
            .filter(|value| *value != 0)
            .unwrap_or(-1);
        // A playlist is born with a view count of 1.
        let view_count = self
            .view_state(&rating_key, "viewCount")
            .and_then(|value| value.as_i64())
            .unwrap_or(if listed { 1 } else { 0 });
        let mut body = Map::new();
        // Measured with a conversion running: `size` counts the `TranscodeSession` too.
        body.insert(
            "size".to_string(),
            json!(["Bandwidths", "TranscodeSession"].iter().filter(|name| fields.contains_key(**name)).count()),
        );
        body.insert("playbackState".to_string(), json!(playback_state));
        body.insert("skipCount".to_string(), json!(0));
        body.insert("viewCount".to_string(), json!(view_count));
        body.insert("viewOffset".to_string(), json!(stored));
        body.extend(fields);
        Response::json(200, json!({"MediaContainer": body}))
    }
}

/// `str(value)` for a JSON scalar the way Python spells it (`5.7`, `8.0`, `7`).
fn python_str(value: &Value) -> String {
    match value {
        Value::Number(number) => {
            if let Some(float) = number.as_f64().filter(|_| number.is_f64()) {
                if float.fract() == 0.0 && float.abs() < 1e16 {
                    format!("{float:.1}")
                } else {
                    format!("{float}")
                }
            } else {
                number.to_string()
            }
        }
        other => string_value(other),
    }
}

/// `_statistics_window`: `sort=` really orders and `limit=` caps (non-positive: none).
pub(super) fn statistics_window(mut rows: Vec<Value>, query: &HashMap<String, String>) -> Vec<Value> {
    if let Some(sort) = query.get("sort").filter(|sort| !sort.is_empty()) {
        let (key, direction) = sort.split_once(':').unwrap_or((sort.as_str(), ""));
        rows.sort_by(|left, right| {
            let value = |row: &Value| row.get(key).and_then(Value::as_f64);
            let order = (value(left).is_none(), value(left))
                .partial_cmp(&(value(right).is_none(), value(right)))
                .unwrap_or(std::cmp::Ordering::Equal);
            if direction == "desc" {
                order.reverse()
            } else {
                order
            }
        });
    }
    if let Some(limit) = query
        .get("limit")
        .and_then(|value| value.trim_start_matches('+').parse::<i64>().ok())
    {
        if limit > 0 {
            rows.truncate(limit as usize);
        } else {
            rows.clear();
        }
    }
    rows
}
