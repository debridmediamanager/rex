//! Accounts: which plex.tv account each request comes from, and what that account keeps
//! and sees, as a claimed PMS 1.43.4 answered its owner and a shared Home user side by side
//! (`tests/fixtures/shared_user_s1434.json`, `shared_user_label_s1434.json`, recorded by
//! `probes/record_shared_user.py`).
//!
//! A token's account is what plex.tv's access-token list says of it
//! (`plextv_access_tokens_shared_s1434.json`): the owner's devices are account 1, as a real
//! PMS numbers its owner locally, and a share is the `<invited>` user's plex.tv id, with the
//! share's libraries, filters and flags.
//!
//! The rows keep the owner's watch state, as they always have. A request from anyone else
//! runs with that account's state swapped onto the rows (`enter_account`) and swapped back
//! before anything saves (`leave_account`), so every reader -- Continue Watching, On Deck,
//! the leaf counts, the unwatched filters, the badges -- answers for the requester without
//! knowing accounts exist. The swap is a pass over the rows, paid only by other accounts'
//! requests.

use super::*;

/// The owner's local account id, as a real PMS numbers it (`/accounts`).
pub(crate) const OWNER: i64 = 1;

/// The fields a real PMS keeps per account and item (`metadata_item_settings`).
const VIEW_FIELDS: [&str; 7] = [
    "viewCount",
    "viewOffset",
    "lastViewedAt",
    "lastRatedAt",
    "userRating",
    "_viewUpdatedAt",
    "lastSkippedAt",
];

/// What a token may do: whose account it is, and what the share grants.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Grant {
    pub id: i64,
    pub title: String,
    pub thumb: String,
    pub prefs: Option<crate::mediainfo::TrackPrefs>,
    /// The library section ids the share names; `None` is every library (the owner).
    pub sections: Option<HashSet<String>>,
    pub filter_movies: String,
    pub filter_television: String,
    pub allow_sync: bool,
    pub allow_camera_upload: bool,
    pub allow_channels: bool,
    pub allow_tuners: bool,
}

impl Grant {
    pub(crate) fn owner() -> Self {
        Self {
            id: OWNER,
            title: String::new(),
            thumb: String::new(),
            prefs: None,
            sections: None,
            filter_movies: String::new(),
            filter_television: String::new(),
            allow_sync: true,
            allow_camera_upload: true,
            allow_channels: true,
            allow_tuners: true,
        }
    }

    pub(crate) fn is_owner(&self) -> bool {
        self.id == OWNER
    }

    /// Whether a share hides anything, so its answers need pruning.
    fn restricted(&self) -> bool {
        self.sections.is_some() || !self.filter_movies.is_empty() || !self.filter_television.is_empty()
    }

    pub(crate) fn to_value(&self) -> Value {
        let mut sections = self.sections.iter().flatten().cloned().collect::<Vec<_>>();
        sections.sort();
        json!({
            "id": self.id,
            "title": self.title,
            "thumb": self.thumb,
            "prefs": self.prefs.as_ref().map(|prefs| json!({
                "autoSelectAudio": prefs.auto_audio,
                "defaultAudioLanguage": prefs.audio,
                "autoSelectSubtitle": prefs.subtitles,
                "defaultSubtitleLanguage": prefs.subtitle,
            })),
            "sections": self.sections.as_ref().map(|_| sections),
            "filterMovies": self.filter_movies,
            "filterTelevision": self.filter_television,
            "allowSync": self.allow_sync,
            "allowCameraUpload": self.allow_camera_upload,
            "allowChannels": self.allow_channels,
            "allowTuners": self.allow_tuners,
        })
    }

    pub(crate) fn from_value(value: &Value) -> Option<Self> {
        let text = |name: &str| value.get(name).and_then(Value::as_str).unwrap_or_default().to_string();
        let flag = |name: &str| value.get(name).and_then(Value::as_bool).unwrap_or(false);
        Some(Self {
            id: value.get("id").and_then(Value::as_i64)?,
            title: text("title"),
            thumb: text("thumb"),
            prefs: value.get("prefs").filter(|prefs| prefs.is_object()).map(|prefs| crate::mediainfo::TrackPrefs {
                auto_audio: prefs.get("autoSelectAudio").and_then(Value::as_bool).unwrap_or(false),
                audio: prefs.get("defaultAudioLanguage").and_then(Value::as_str).map(str::to_string),
                subtitles: prefs.get("autoSelectSubtitle").and_then(Value::as_i64).unwrap_or(0),
                subtitle: prefs.get("defaultSubtitleLanguage").and_then(Value::as_str).map(str::to_string),
            }),
            sections: value
                .get("sections")
                .and_then(Value::as_array)
                .map(|ids| ids.iter().map(string_value).collect()),
            filter_movies: text("filterMovies"),
            filter_television: text("filterTelevision"),
            allow_sync: flag("allowSync"),
            allow_camera_upload: flag("allowCameraUpload"),
            allow_channels: flag("allowChannels"),
            allow_tuners: flag("allowTuners"),
        })
    }
}

#[derive(Debug, PartialEq)]
pub(crate) enum ShareRestrictionError {
    InvalidAccount,
    InvalidFormat(String),
}

impl Runtime {
    pub(crate) fn share_restrictions(&self, account: i64) -> Value {
        self.prefs.get("_rexShareRestrictions").and_then(|all| all.get(account.to_string())).cloned().unwrap_or(Value::Null)
    }

    /// Local restrictions intersect the cloud grant; they cannot add sections or sync access.
    pub(crate) fn set_share_restrictions(&mut self, account: i64, value: &Value) -> Result<(), ShareRestrictionError> {
        if account <= OWNER { return Err(ShareRestrictionError::InvalidAccount); }
        let object = value.as_object().ok_or_else(|| ShareRestrictionError::InvalidFormat("expected object".into()))?;
        for (field, value) in object {
            match field.as_str() {
                "allowSync" if value.is_boolean() => {}
                "filterMovies" | "filterTelevision" => {
                    let spec = value.as_str().ok_or_else(|| ShareRestrictionError::InvalidFormat(field.clone()))?;
                    if !spec.is_empty() && (clauses(spec).len() != spec.split('|').count()
                        || clauses(spec).iter().any(|(field, _, _)| !matches!(field.as_str(), "contentRating" | "label")))
                    { return Err(ShareRestrictionError::InvalidFormat(field.clone())); }
                }
                _ => return Err(ShareRestrictionError::InvalidFormat(field.clone())),
            }
        }
        let mut all = self.prefs.get("_rexShareRestrictions").and_then(Value::as_object).cloned().unwrap_or_default();
        all.insert(account.to_string(), value.clone());
        self.prefs.insert("_rexShareRestrictions".into(), Value::Object(all));
        self.dirty = true;
        Ok(())
    }

    pub(crate) fn remove_share_restrictions(&mut self, account: i64) -> Result<(), ShareRestrictionError> {
        if account <= OWNER { return Err(ShareRestrictionError::InvalidAccount); }
        if let Some(all) = self.prefs.get_mut("_rexShareRestrictions").and_then(Value::as_object_mut) {
            all.remove(account.to_string().as_str());
            self.dirty = true;
        }
        Ok(())
    }

    pub(super) fn download_allowed(&self) -> bool {
        self.viewer.is_owner() || (self.viewer.allow_sync
            && self.share_restrictions(self.viewer.id).get("allowSync").and_then(Value::as_bool) != Some(false))
    }

    fn local_share_passes(&self, row: &(impl Row + ?Sized), field: &str) -> bool {
        passes(row, self.share_restrictions(self.viewer.id).get(field).and_then(Value::as_str).unwrap_or(""))
    }
}

/// A share filter's clauses, as plex.tv hands them to the server: `field=v1,v2` keeps only
/// rows holding one of the values, `field!=v1,v2` drops rows holding any; clauses are joined
/// by `|` and values may be percent-encoded (`contentRating=G%2CPG`, `label!=Rexhide`).
fn clauses(spec: &str) -> Vec<(String, bool, Vec<String>)> {
    spec.split('|')
        .filter_map(|clause| {
            let (field, negate, values) = if let Some((field, values)) = clause.split_once("!=") {
                (field, true, values)
            } else {
                let (field, values) = clause.split_once('=')?;
                (field, false, values)
            };
            let values = percent_decode(values)
                .split(',')
                .map(|value| value.trim().to_lowercase())
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>();
            (!field.is_empty() && !values.is_empty()).then(|| (field.to_string(), negate, values))
        })
        .collect()
}

/// Whether `row` passes a share filter. A row with no content rating fails an allow-list
/// of ratings: plexlab hid Cosmos Laundromat, which has none, from `contentRating=G,PG`.
fn passes(row: &(impl Row + ?Sized), spec: &str) -> bool {
    clauses(spec).into_iter().all(|(field, negate, values)| {
        let held: Vec<String> = match field.as_str() {
            "contentRating" => row
                .get("contentRating")
                .map(string_value)
                .filter(|rating| !rating.is_empty())
                .into_iter()
                .map(|rating| rating.to_lowercase())
                .collect(),
            "label" => row
                .get("Label")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|label| label.get("tag").map(string_value))
                .map(|tag| tag.to_lowercase())
                .collect(),
            // ponytail: only contentRating and label are offered by plex.tv's share dialog.
            _ => return true,
        };
        let holds = held.iter().any(|value| values.contains(value));
        holds != negate
    })
}

/// One account's state beside the rows.
#[derive(Debug, Default, Clone, PartialEq)]
pub(crate) struct AccountState {
    /// By ratingKey: the view fields this account holds on that row.
    pub views: HashMap<String, Value>,
    pub hidden_continue: HashSet<String>,
    pub viewed_in_session: HashSet<String>,
    /// By part id: the streams this account chose by hand (`PUT /library/parts`), by
    /// stream type.
    pub picks: HashMap<String, Value>,
}

/// What `enter_account` moved aside, put back by `leave_account`.
#[derive(Debug, Default)]
pub(crate) struct Overlay {
    account: i64,
    hidden_continue: HashSet<String>,
    viewed_in_session: HashSet<String>,
    /// By ratingKey: the `Media` of each row the request names, before its tracks were
    /// selected for this account.
    media: HashMap<String, Value>,
}

/// The routes a claimed 1.43.4 PMS refused a shared user with 403. Section refresh
/// has its own method and section-permission ordering below.
fn admin_only(method: &str, path: &str) -> bool {
    let read = matches!(method, "GET" | "HEAD");
    let exact = [
        "/accounts", "/myplex/account", "/status/sessions", "/status/sessions/background",
        "/butler", "/devices", "/system", "/diagnostics/logs", "/updater/status",
        "/media/grabbers", "/livetv/dvrs", "/channels/all",
    ];
    let prefix = [
        "/accounts/", "/butler/", "/devices/", "/system/", "/diagnostics/", "/updater/",
        "/media/grabbers/", "/livetv/", "/status/sessions/terminate",
    ];
    if exact.contains(&path) || prefix.iter().any(|prefix| path.starts_with(prefix)) {
        return true;
    }
    if path.starts_with("/library/metadata/") && path.ends_with("/matches") {
        return true;
    }
    // ponytail: a friend's write to Manage Recommendations is unmeasured; the list is the
    // owner's (Plex Web shows it only on the owner's server settings), so it is refused.
    if !read && path.starts_with("/hubs/sections/") && path.split('/').nth(4) == Some("manage") {
        return true;
    }
    // PUT /:/prefs and an edit of a row were 403. ponytail: the other library and server
    // writes are refused by the same rule, unmeasured one by one.
    !read
        && (path == "/:/prefs"
            || path.starts_with("/library/metadata/")
            || (path.starts_with("/library/sections") && !path.ends_with("/refresh"))
            || path.starts_with("/library/collections")
            || path.starts_with("/library/streams/")
            || path.starts_with("/library/parts/") && path.matches('/').count() > 3)
}

impl Runtime {
    /// The account the request's token belongs to: the owner for `--token`, the claim's own
    /// token, the owner's devices and an unclaimed server's local callers.
    pub(super) fn grant_for(&self, request: &Request) -> Grant {
        let (_, raw_query) = split_target(&request.target);
        let token = request
            .headers
            .get("x-plex-token")
            .filter(|value| !value.is_empty())
            .cloned()
            .or_else(|| {
                // The gate reads the query as a map, where a repeated name keeps its last
                // value; the first one here let `?X-Plex-Token=zz&X-Plex-Token=<share>` in as
                // the owner (audit 2026-10-20).
                parse_query(&raw_query)
                    .into_iter()
                    .rev()
                    .find(|(name, _)| name == "X-Plex-Token")
                    .map(|(_, value)| value)
            })
            .unwrap_or_default();
        if token.is_empty() || self.token.as_deref() == Some(token.as_str()) {
            return Grant::owner();
        }
        self.plextv.access.grant(&token).unwrap_or_else(Grant::owner)
    }

    /// The accounts this server knows: the owner and every share plex.tv listed, in id order
    /// (`GET /accounts` on plexlab listed the shared user by its plex.tv id).
    pub(super) fn known_accounts(&self) -> Vec<Grant> {
        let mut grants = self.plextv.access.grants();
        grants.retain(|grant| !grant.is_owner());
        grants.sort_by_key(|grant| grant.id);
        grants.dedup_by_key(|grant| grant.id);
        grants
    }

    /// An account's name and avatar as sessions and history show them.
    pub(super) fn account_user(&self, id: i64) -> (String, String) {
        if id == OWNER {
            return (self.owner_name(), String::new());
        }
        self.known_accounts()
            .into_iter()
            .find(|grant| grant.id == id)
            .map(|grant| (grant.title, grant.thumb))
            .unwrap_or_default()
    }

    pub(super) fn section_granted(&self, section: &str) -> bool {
        self.viewer.sections.as_ref().is_none_or(|sections| sections.contains(section))
    }

    /// Whether the requesting account may see `row`: its library is shared, and it passes
    /// the share's filter -- a film by the film filter, a show, season or episode by the
    /// television filter, read off the show.
    pub(super) fn visible(&self, row: &(impl Row + ?Sized)) -> bool {
        if self.viewer.is_owner() {
            return true;
        }
        let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
        if !section.is_empty() && !self.section_granted(&section) {
            return false;
        }
        match row.get("type").and_then(Value::as_str) {
            Some("movie") => passes(row, &self.viewer.filter_movies) && self.local_share_passes(row, "filterMovies"),
            Some("show") => passes(row, &self.viewer.filter_television) && self.local_share_passes(row, "filterTelevision"),
            Some("season") | Some("episode") => {
                let show_key = if row.get("type").and_then(Value::as_str) == Some("season") {
                    row.get("parentRatingKey")
                } else {
                    row.get("grandparentRatingKey")
                };
                match show_key.map(string_value).and_then(|key| self.catalog.get(&key)) {
                    Some(show) => passes(show, &self.viewer.filter_television) && self.local_share_passes(show, "filterTelevision"),
                    None => passes(row, &self.viewer.filter_television) && self.local_share_passes(row, "filterTelevision"),
                }
            }
            _ => true,
        }
    }

    /// What a real PMS refuses another account before routing: the owner's routes (403), a
    /// library the share leaves out (`/library/sections/{id}...` 403, its hubs 404) and a row
    /// the share hides (404).
    pub(super) fn share_refusal(&self, request: &Request, path: &str) -> Option<Response> {
        if self.viewer.is_owner() {
            return None;
        }
        let method = request.method.as_str();
        // A write to a row that does not exist is the route's own 404 (`DELETE
        // /library/metadata/999999` was a 404 for the shared user too).
        let missing_row = path
            .strip_prefix("/library/metadata/")
            .map(|rest| rest.split('/').next().unwrap_or_default())
            // A comma list is the multi-row edit and a collection is no catalog row: both
            // skipped the owner's 403 as "missing" (audit 2026-10-20).
            .is_some_and(|keys| {
                keys.split(',').all(|key| self.catalog.get(key).is_none() && !self.collections.contains_key(key))
            });
        // Live TV is the share's `allowTuners` (plexav 1.43.4 with a DVR,
        // `tests/fixtures/livetv/shared_tuners_s1434.json`): at 1 the DVR's and sessions' reads, the
        // provider's and the tune answer as the owner's; at 0 each is 403 but What's On, which
        // still lists its hubs. ponytail: a share's other writes under `/livetv/` (DVR edits,
        // prefs, reloadGuide, a session's DELETE) were not measured and stay the owner's; nor were
        // the provider's other keys at 0 (What's On's shelves, metadata), refused with the rest, or a
        // share at 1 on a server with no DVR, which gets the owner's tunerless answers.
        let tuners = path.starts_with(super::livetv::PREFIX)
            || path.starts_with("/livetv/")
                && (matches!(method, "GET" | "HEAD") || method == "POST" && path.starts_with("/livetv/dvrs/") && path.ends_with("/tune"));
        if tuners && !self.viewer.allow_tuners && path != format!("{}/hubs/discover", super::livetv::PREFIX) {
            return Some(Response::error(403, "rex: the owner's route"));
        }
        if admin_only(method, path) && !missing_row && !tuners {
            return Some(Response::error(403, "rex: the owner's route"));
        }
        // The download queue: the owner's list is 403 to everyone else, and a queue of its own
        // only with allowSync=1 (`POST /downloadQueue` 403 at allowSync=0, 200 at 1).
        if path == "/downloadQueue" && method == "GET" || path.starts_with("/downloadQueue") && !self.download_allowed() {
            return Some(Response::error(403, "rex: downloads are not shared"));
        }
        let segments = path.split('/').collect::<Vec<_>>();
        if let ["", "library", "sections", section, ..] = segments.as_slice() {
            if !section.is_empty() && section.bytes().all(|byte| byte.is_ascii_digit()) && !self.section_granted(section) {
                return Some(Response::error(403, "rex: not shared"));
            }
        }
        // The disposable shared PMS refused GET/HEAD/POST refresh even for a granted
        // section or `all`; PUT was a route 404, except an unshared section wins with 403.
        if path.starts_with("/library/sections/") && path.ends_with("/refresh") {
            let status = if matches!(method, "GET" | "HEAD" | "POST") { 403 } else { 404 };
            return Some(Response::error(status, "rex: the owner's route"));
        }
        if let ["", "hubs", "sections", section, ..] = segments.as_slice() {
            if !self.section_granted(section) {
                return Some(Response::error(404, "rex: not shared"));
            }
        }
        // The row a path, a play's `key` or `path`, or a part's bytes name.
        if self.viewer.restricted()
            && self
                .named_rows(request)
                .iter()
                .filter_map(|key| self.catalog.get(key))
                .any(|row| !self.visible(row))
        {
            return Some(Response::error(404, "rex: not shared"));
        }
        None
    }

    /// Swap the requesting account's state onto the rows: its view fields, its Continue
    /// Watching hides and in-session views, and its own track selection on the rows the
    /// request names. Nothing is marked written; `leave_account` undoes it.
    pub(super) fn enter_account(&mut self, request: &Request) {
        if self.viewer.is_owner() || self.overlay.is_some() {
            return;
        }
        let account = self.viewer.id;
        let mut state = self.account_states.remove(&account).unwrap_or_default();
        let mut overlay = Overlay {
            account,
            ..Overlay::default()
        };
        // Which rows hold the owner's view fields, kept current from the rows written since
        // the last request (`Catalog::take_writes`): a pass over every row cost each of another
        // account's requests 47 ms on fun's library copy.
        let writes = self.catalog.take_writes();
        let held = match self.owner_views.take() {
            Some(mut held) if !writes.all => {
                for key in &writes.keys {
                    if self.catalog.get(key).is_some_and(holds_view) {
                        held.insert(key.clone());
                    } else {
                        held.remove(key);
                    }
                }
                held
            }
            _ => self.catalog.hot().lazy().filter(|row| holds_view(row)).filter_map(|row| key_of(&row)).collect(),
        };
        // Read through, not written onto every row: a row read shows the account's fields, and
        // only a row written to is changed (`rows::Overlay`).
        self.catalog.overlay_views(&VIEW_FIELDS, &held, &state.views);
        self.owner_views = Some(held);
        overlay.hidden_continue = std::mem::replace(&mut self.hidden_continue, std::mem::take(&mut state.hidden_continue));
        overlay.viewed_in_session =
            std::mem::replace(&mut self.viewed_in_session, std::mem::take(&mut state.viewed_in_session));
        let prefs = self.viewer.prefs.clone().unwrap_or(crate::mediainfo::TrackPrefs {
            auto_audio: true,
            audio: None,
            subtitles: 0,
            subtitle: None,
        });
        for key in self.named_rows(request) {
            let Some(row) = self.catalog.get_unmarked_mut(&key) else {
                continue;
            };
            let Some(media) = row.get("Media").cloned() else {
                continue;
            };
            crate::catalog::unpack_streams(row);
            if let Some(rows) = row.get_mut("Media").and_then(Value::as_array_mut) {
                for media in rows.iter_mut() {
                    for part in media.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
                        let id = part.get("id").map(string_value).unwrap_or_default();
                        if let Some(object) = part.as_object_mut() {
                            object.shift_remove("_trackChosen");
                        }
                        match state.picks.get(&id) {
                            Some(pick) => apply_pick(part, pick),
                            None => {
                                crate::mediainfo::select_tracks(part, &prefs);
                            }
                        }
                    }
                }
            }
            overlay.media.insert(key, media);
        }
        self.overlay = Some(overlay);
        self.account_states.insert(account, state);
    }

    /// Undo `enter_account`, keeping what the request changed as the account's own.
    pub(super) fn leave_account(&mut self) {
        let Some(mut overlay) = self.overlay.take() else {
            return;
        };
        let account = overlay.account;
        let mut state = self.account_states.remove(&account).unwrap_or_default();
        let before = state.clone();
        for (key, media) in std::mem::take(&mut overlay.media) {
            let Some(row) = self.catalog.get_unmarked_mut(&key) else {
                continue;
            };
            for part in row
                .get("Media")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .flat_map(|media| media.get("Part").and_then(Value::as_array).into_iter().flatten())
            {
                if part.get("_trackChosen").and_then(Value::as_bool) == Some(true) {
                    let id = part.get("id").map(string_value).unwrap_or_default();
                    state.picks.insert(id, chosen_streams(part));
                }
            }
            if let Some(object) = row.as_object_mut() {
                object.insert("Media".to_string(), media);
            }
        }
        // The account's fields now: those it held, and those of the rows the request changed,
        // read off them before the owner's go back (`Catalog::end_view_overlay`).
        let writes = self.catalog.take_writes();
        if writes.all {
            self.owner_views = None;
        }
        let mut views = state.views.clone();
        for (key, owner) in self.catalog.end_view_overlay() {
            let Some(object) = self.catalog.get_unmarked_mut(&key).and_then(Value::as_object_mut) else {
                continue;
            };
            let mut fields = Map::new();
            for name in VIEW_FIELDS {
                if let Some(value) = object.shift_remove(name) {
                    fields.insert(name.to_string(), value);
                }
            }
            if fields.is_empty() {
                views.remove(&key);
            } else {
                views.insert(key.clone(), Value::Object(fields));
            }
            for (index, name, value) in owner {
                let index = index.min(object.len());
                object.shift_insert(index, name, value);
            }
            self.catalog.refile(&key);
        }
        for key in views.keys().chain(state.views.keys()) {
            if views.get(key) != state.views.get(key) {
                self.account_views_changed.insert(format!("{account}/{key}"));
            }
        }
        state.views = views;
        state.hidden_continue = std::mem::replace(&mut self.hidden_continue, overlay.hidden_continue);
        state.viewed_in_session = std::mem::replace(&mut self.viewed_in_session, overlay.viewed_in_session);
        if state != before {
            self.dirty = true;
        }
        self.account_states.insert(account, state);
    }

    /// The rows a request names -- its path's `/library/metadata/{key}`, the `key`,
    /// `ratingKey`, `path` and `uri` it carries, and the row of a `/library/parts/{id}` --
    /// whose tracks are selected for the account while it runs.
    fn named_rows(&self, request: &Request) -> Vec<String> {
        let (path, raw_query) = split_target(&request.target);
        fn from(text: &str, keys: &mut Vec<String>) {
            for (index, _) in text.match_indices("/library/metadata/") {
                let key = route_id(&text[index + "/library/metadata/".len()..]);
                if !key.is_empty() {
                    keys.push(key);
                }
            }
        }
        let mut keys = Vec::new();
        from(&path, &mut keys);
        for (name, value) in parse_query(&raw_query) {
            match name.as_str() {
                "key" | "ratingKey" if value.bytes().all(|byte| byte.is_ascii_digit()) => keys.push(value),
                "key" | "path" | "uri" => from(&value, &mut keys),
                _ => {}
            }
        }
        if let Some(part) = path.strip_prefix("/library/parts/") {
            let part = route_id(part);
            let part = part.as_str();
            if let Some(key) = self.catalog.rows_by(crate::catalog::By::Part, part).into_iter().find_map(|row| {
                row.get("Media")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .flat_map(|media| media.get("Part").and_then(Value::as_array).into_iter().flatten())
                    .any(|candidate| candidate.get("id").map(string_value).as_deref() == Some(part))
                    .then(|| key_of(row))
                    .flatten()
            }) {
                keys.push(key);
            }
        }
        keys.sort();
        keys.dedup();
        keys
    }

    /// Another account's answer as plexlab gave its shared user one: the libraries and rows
    /// the share hides pruned, and the server's own flags those of the share.
    pub(super) fn share_response(&self, path: &str, response: &mut Response) {
        // The router pops a trailing slash (and folds the measured `//` forms), so the share's
        // pass must too: `/media/providers/` answered the owner's libraries and flags.
        let path = match path.trim_end_matches('/') {
            "" => "/",
            trimmed => trimmed,
        };
        if self.viewer.is_owner() || response.status != 200 || !response.content_type.starts_with("application/json") {
            return;
        }
        // An answer the share neither prunes nor flags keeps its bytes.
        if !self.viewer.restricted() && self.share_restrictions(self.viewer.id).is_null() && !matches!(path, "/" | "/media/providers" | "/library/sections") {
            return;
        }
        let Some(mut document) = response.take_document() else {
            return;
        };
        let Some(container) = document.get_mut("MediaContainer").and_then(Value::as_object_mut) else {
            { response.restore_document(document); return; }
        };
        if matches!(path, "/" | "/media/providers") {
            container.shift_remove("allowMediaDeletion");
            for (name, allowed) in [
                ("allowCameraUpload", self.viewer.allow_camera_upload),
                ("allowChannelAccess", self.viewer.allow_channels),
                ("allowSync", self.download_allowed()),
                ("allowTuners", self.viewer.allow_tuners),
            ] {
                // A share lowers a flag the server has; it never raises one rex answers 0.
                if let Some(own) = container.get(name).and_then(Value::as_bool) {
                    container.insert(name.to_string(), json!(own && allowed));
                }
            }
        }
        if path == "/media/providers" {
            for provider in container.get_mut("MediaProvider").and_then(Value::as_array_mut).into_iter().flatten() {
                // A local restriction may lower, but never raise, the cloud grant.
                if let Some(protocols) = provider.get("protocols").and_then(Value::as_str).filter(|_| !self.download_allowed()) {
                    let kept = protocols.split(',').filter(|protocol| *protocol != "download").collect::<Vec<_>>().join(",");
                    provider["protocols"] = json!(kept);
                }
                if let Some(features) = provider.get_mut("Feature").and_then(Value::as_array_mut) {
                    let sync = self.download_allowed();
                    features.retain(|feature| {
                        let kind = feature.get("type").and_then(Value::as_str);
                        kind != Some("manage")
                            && !(kind == Some("subscribe") && feature.get("flavor").and_then(Value::as_str) == Some("download") && !sync)
                    });
                    for feature in features.iter_mut() {
                        if let Some(directories) = feature.get_mut("Directory").and_then(Value::as_array_mut) {
                            directories.retain(|directory| self.directory_visible(directory));
                        }
                    }
                }
            }
        }
        if path == "/library/sections" {
            for directory in container.get_mut("Directory").and_then(Value::as_array_mut).into_iter().flatten() {
                if directory.get("allowSync").is_some() {
                    directory["allowSync"] = json!(self.download_allowed());
                }
            }
        }
        if self.viewer.restricted() {
            if let Some(container) = document.get_mut("MediaContainer") {
                self.prune(container);
            }
        }
        response.store_document(document);
    }

    fn directory_visible(&self, directory: &Value) -> bool {
        if directory.get("agent").is_none() && directory.get("uuid").is_none() {
            return true;
        }
        let id = directory
            .get("id")
            .map(string_value)
            .filter(|id| !id.is_empty())
            .or_else(|| directory.get("key").map(string_value).map(|key| key.rsplit('/').next().unwrap_or_default().to_string()))
            .unwrap_or_default();
        self.section_granted(&id)
    }

    fn element_visible(&self, element: &Value) -> bool {
        if let Some(row) = element.get("ratingKey").map(string_value).and_then(|key| self.catalog.get(&key)) {
            return self.visible(row);
        }
        if let Some(section) = element.get("librarySectionID").map(string_value).filter(|id| !id.is_empty()) {
            return self.section_granted(&section);
        }
        self.directory_visible(element)
    }

    /// Drop what the share hides from every `Metadata`, `Directory` and `Hub` list, with the
    /// counts and a hub's `hubKey` following; a hub left empty goes too (plexlab's search
    /// answered no Movies hub once its one film was hidden).
    fn prune(&self, node: &mut Value) {
        let Some(object) = node.as_object_mut() else {
            return;
        };
        let mut removed = 0_i64;
        for name in ["Metadata", "Directory", "Hub", "Video"] {
            let Some(list) = object.get_mut(name).and_then(Value::as_array_mut) else {
                continue;
            };
            let before = list.len();
            list.retain(|element| self.element_visible(element));
            for element in list.iter_mut() {
                let had = element.get("Metadata").and_then(Value::as_array).map_or(0, Vec::len);
                self.prune(element);
                if name == "Hub" && had > 0 {
                    let left = element.get("Metadata").and_then(Value::as_array).map_or(0, Vec::len);
                    if left == 0 {
                        element["_drop"] = json!(true);
                    }
                }
            }
            list.retain(|element| element.get("_drop").is_none());
            removed += (before - list.len()) as i64;
        }
        if removed > 0 {
            for name in ["size", "totalSize"] {
                if let Some(count) = object.get(name).and_then(Value::as_i64) {
                    object.insert(name.to_string(), json!((count - removed).max(0)));
                }
            }
        }
        if let Some(keys) = object
            .get("hubKey")
            .and_then(Value::as_str)
            .and_then(|key| key.strip_prefix("/library/metadata/"))
            .map(str::to_string)
        {
            let kept = keys
                .split(',')
                .filter(|key| self.catalog.get(key).is_none_or(|row| self.visible(row)))
                .collect::<Vec<_>>();
            object.insert("hubKey".to_string(), json!(format!("/library/metadata/{}", kept.join(","))));
        }
    }

    /// `row` with `account`'s view fields in place of the owner's.
    pub(super) fn with_account_views(&self, row: &Value, account: i64) -> Value {
        let mut row = row.clone();
        if account == OWNER || self.overlay.as_ref().is_some_and(|overlay| overlay.account == account) {
            return row;
        }
        let key = key_of(&row).unwrap_or_default();
        if let Some(object) = row.as_object_mut() {
            for name in VIEW_FIELDS {
                object.shift_remove(name);
            }
            if let Some(Value::Object(fields)) = self.account_states.get(&account).and_then(|state| state.views.get(&key)) {
                for (name, value) in fields {
                    object.insert(name.clone(), value.clone());
                }
            }
        }
        row
    }

    /// Out of sight for this request: the playlists another account made.
    pub(super) fn hide_other_playlists(&mut self) {
        let viewer = self.viewer.id;
        if self.playlists.values().all(|playlist| playlist.account == viewer) {
            return;
        }
        let (mine, others): (HashMap<_, _>, HashMap<_, _>) =
            std::mem::take(&mut self.playlists).into_iter().partition(|(_, playlist)| playlist.account == viewer);
        self.playlists = mine;
        // Added to, never assigned: after a request that never restored them (a handler's
        // panic), assigning dropped the owner's playlists from memory and the next save.
        self.hidden_playlists.extend(others);
    }

    pub(super) fn restore_playlists(&mut self) {
        self.playlists.extend(self.hidden_playlists.drain());
    }

    /// The state document's per-account fields: the view fields by `<account>/<ratingKey>`
    /// (diffed by member, `accountViews`) and the rest by account (`accountSets`).
    pub(super) fn account_sets_document(&self) -> Value {
        let mut out = Map::new();
        for (account, state) in &self.account_states {
            let mut hidden = state.hidden_continue.iter().cloned().collect::<Vec<_>>();
            hidden.sort();
            let mut viewed = state.viewed_in_session.iter().cloned().collect::<Vec<_>>();
            viewed.sort();
            out.insert(
                account.to_string(),
                json!({"hiddenContinue": hidden, "viewedInSession": viewed, "picks": state.picks}),
            );
        }
        Value::Object(out)
    }

    pub(super) fn account_views_all(&self) -> Map<String, Value> {
        account_views_all(&self.account_states)
    }

    pub(super) fn restore_accounts(&mut self, views: Option<&Value>, sets: Option<&Value>) {
        for (flat, value) in views.and_then(Value::as_object).into_iter().flatten() {
            let Some((account, key)) = flat.split_once('/') else {
                continue;
            };
            let Ok(account) = account.parse::<i64>() else {
                continue;
            };
            self.account_states.entry(account).or_default().views.insert(key.to_string(), value.clone());
        }
        for (account, value) in sets.and_then(Value::as_object).into_iter().flatten() {
            let Ok(account) = account.parse::<i64>() else {
                continue;
            };
            let state = self.account_states.entry(account).or_default();
            let set = |name: &str| -> HashSet<String> {
                value.get(name).and_then(Value::as_array).into_iter().flatten().map(string_value).collect()
            };
            state.hidden_continue = set("hiddenContinue");
            state.viewed_in_session = set("viewedInSession");
            state.picks = value
                .get("picks")
                .and_then(Value::as_object)
                .map(|picks| picks.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
                .unwrap_or_default();
        }
    }

    /// `/accounts`' rows for the shares, after the owner's: a real PMS lists every account
    /// it has seen, named, with its track settings (`GET /accounts/873552796` on plexlab).
    pub(super) fn shared_account_rows(&self) -> Vec<Value> {
        self.known_accounts()
            .into_iter()
            .map(|grant| {
                let prefs = grant.prefs.clone();
                let language = |value: Option<String>| value.filter(|code| !code.is_empty()).unwrap_or_else(|| "C.".to_string());
                json!({
                    "id": grant.id,
                    "key": format!("/accounts/{}", grant.id),
                    "name": grant.title,
                    "defaultAudioLanguage": language(prefs.as_ref().and_then(|prefs| prefs.audio.clone())),
                    "autoSelectAudio": prefs.as_ref().is_none_or(|prefs| prefs.auto_audio),
                    "defaultSubtitleLanguage": language(prefs.as_ref().and_then(|prefs| prefs.subtitle.clone())),
                    "subtitleMode": prefs.as_ref().map_or(1, |prefs| prefs.subtitles),
                    "thumb": "",
                })
            })
            .collect()
    }
}

fn holds_view(row: &(impl Row + ?Sized)) -> bool {
    VIEW_FIELDS.iter().any(|name| row.get(name).is_some())
}

/// One `accountViews` member, by `<account>/<ratingKey>`.
pub(super) fn account_view<'a>(states: &'a HashMap<i64, AccountState>, flat: &str) -> Option<&'a Value> {
    let (account, key) = flat.split_once('/')?;
    states.get(&account.parse::<i64>().ok()?)?.views.get(key)
}

pub(super) fn account_views_all(states: &HashMap<i64, AccountState>) -> Map<String, Value> {
    let mut out = Map::new();
    for (account, state) in states {
        for (key, value) in &state.views {
            out.insert(format!("{account}/{key}"), value.clone());
        }
    }
    out
}

/// The audio and subtitle streams a Part has selected, by stream type.
fn chosen_streams(part: &Value) -> Value {
    let mut out = Map::new();
    for stream in part.get("Stream").and_then(Value::as_array).into_iter().flatten() {
        if stream.get("selected").and_then(Value::as_bool) == Some(true) {
            if let Some(kind) = stream.get("streamType").and_then(Value::as_i64).filter(|kind| matches!(kind, 2 | 3)) {
                out.insert(kind.to_string(), stream.get("id").cloned().unwrap_or(Value::Null));
            }
        }
    }
    for kind in ["2", "3"] {
        out.entry(kind.to_string()).or_insert(Value::Null);
    }
    Value::Object(out)
}

/// Select on `part` what an account chose by hand.
fn apply_pick(part: &mut Value, pick: &Value) {
    if let Some(object) = part.as_object_mut() {
        object.insert("_trackChosen".to_string(), json!(true));
    }
    for stream in part.get_mut("Stream").and_then(Value::as_array_mut).into_iter().flatten() {
        let Some(kind) = stream.get("streamType").and_then(Value::as_i64).filter(|kind| matches!(kind, 2 | 3)) else {
            continue;
        };
        let chosen = pick.get(kind.to_string()).filter(|id| !id.is_null()) == stream.get("id") && stream.get("id").is_some();
        if let Some(object) = stream.as_object_mut() {
            if chosen {
                object.insert("selected".to_string(), Value::Bool(true));
            } else {
                object.shift_remove("selected");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filters_as_plex_tv_hands_them_over() {
        let sintel = json!({"type": "movie", "contentRating": "PG", "Label": [{"tag": "Rexhide"}]});
        let cosmos = json!({"type": "movie"});
        let bunny = json!({"type": "movie", "contentRating": "G"});
        assert!(passes(&sintel, "contentRating=G%2CPG"));
        assert!(passes(&bunny, "contentRating=G%2CPG"));
        assert!(!passes(&cosmos, "contentRating=G%2CPG"));
        assert!(!passes(&sintel, "label!=Rexhide"));
        assert!(passes(&cosmos, "label!=Rexhide"));
        assert!(passes(&sintel, "label=rexhide"));
        assert!(!passes(&sintel, "contentRating=G|label!=Rexhide"));
        assert!(passes(&sintel, ""));
    }
}

/// The id a path segment names as the router reads it (`request_route`, cycle 36): an
/// optional `+`, then digits with their leading zeros dropped. A padded id read raw named no
/// row here while the route served the row (audit 2026-10-20).
fn route_id(text: &str) -> String {
    let digits: String = text.strip_prefix('+').unwrap_or(text).chars().take_while(char::is_ascii_digit).collect();
    match digits.trim_start_matches('0') {
        "" if !digits.is_empty() => "0".to_string(),
        trimmed => trimmed.to_string(),
    }
}
