//! `/statistics/media`, ported from `sessions.py`'s play records and `server.py`'s
//! `statistics`.

use super::*;

impl Runtime {
    /// A stable small integer per client identifier, as a PMS assigns: the anonymous
    /// client (`""`) is 1 and every other one takes the next number the first time the
    /// server sees it, from the header or the query alike.
    pub(super) fn register_device(&mut self, identifier: &str) -> i64 {
        if let Some(index) = self
            .device_order
            .iter()
            .position(|known| known == identifier)
        {
            return index as i64 + 1;
        }
        self.device_order.push(identifier.to_string());
        self.device_first_seen.insert(identifier.to_string(), now());
        // The device table is persisted state (`persist.py` SESSION_FIELDS): cycle 74
        // measured a first-seen device surviving a SIGKILL 0.16 s after the answer.
        self.dirty = true;
        self.device_order.len() as i64
    }

    /// `record_play`: a stopped timeline on an item records one play for the rollups.
    pub(super) fn record_play(&mut self, row: &Value, seconds: i64, device: i64) {
        self.plays.push((
            now(),
            row.get("type")
                .and_then(Value::as_str)
                .unwrap_or("movie")
                .to_string(),
            seconds,
            device,
        ));
    }

    /// Rows for `/statistics/media`: a count and a duration per bucket, keyed by the
    /// device that PLAYED.  With no `timespan` every rollup at once (0 to 4).
    fn media_statistics_rows(&self, timespan: Option<i64>) -> Vec<Value> {
        let spans = match timespan {
            Some(span) => vec![span],
            None => vec![0, 1, 2, 3, 4],
        };
        let mut out = Vec::new();
        for span in spans {
            let mut rows: Vec<((i64, i64, i64), (i64, i64))> = Vec::new();
            for (at, kind, seconds, device) in &self.plays {
                let kind_number = match kind.as_str() {
                    "show" => 2,
                    "season" => 3,
                    "episode" => 4,
                    _ => 1,
                };
                let key = (statistics_bucket(*at, span), kind_number, *device);
                match rows.iter_mut().find(|(known, _)| *known == key) {
                    Some((_, (count, total))) => {
                        *count += 1;
                        *total += seconds;
                    }
                    None => rows.push((key, (1, *seconds))),
                }
            }
            rows.sort_by_key(|(key, _)| *key);
            for ((at, kind, device), (count, seconds)) in rows {
                out.push(json!({"accountID": 1, "deviceID": device, "timespan": span,
                                "at": at, "metadataType": kind, "count": count,
                                "duration": seconds}));
            }
        }
        out
    }

    /// The Device rows the Dashboard's graphs key by.
    pub(super) fn statistics_devices(&self) -> Vec<Value> {
        self.device_order
            .iter()
            .enumerate()
            .map(|(index, identifier)| {
                let known = self.devices.get(identifier);
                json!({
                    "id": index + 1,
                    "name": known.map(|device| device.name.clone()).unwrap_or_default(),
                    "platform": known.map(|device| device.platform.clone()).unwrap_or(json!("")),
                    "clientIdentifier": identifier,
                    "createdAt": self.device_first_seen.get(identifier).copied().unwrap_or(self.started_at),
                })
            })
            .collect()
    }

    pub(super) fn route_media_statistics_ported(
        &self,
        query: &HashMap<String, String>,
    ) -> Response {
        let bare_empty = || Response::json(200, json!({"MediaContainer": {"size": 0}}));
        // Cycle 26: a `timespan` the server does not KEEP is an empty 200.
        let timespan = match query.get("timespan").filter(|value| !value.is_empty()) {
            Some(value) => {
                let span = value.trim_start_matches('+').parse::<i64>().unwrap_or(-99);
                if !(0..=4).contains(&span) {
                    return bare_empty();
                }
                Some(span)
            }
            None => None,
        };
        let bound = |name: &str| query.get(name).and_then(|value| value.parse::<i64>().ok());
        let (since, until, exact) = (bound("at>"), bound("at<"), bound("at="));
        let mut rows = self.media_statistics_rows(timespan);
        let at = |row: &Value| row.get("at").and_then(Value::as_i64).unwrap_or(0);
        if let Some(since) = since {
            rows.retain(|row| at(row) >= since);
        }
        if let Some(until) = until {
            rows.retain(|row| at(row) <= until);
        }
        if let Some(exact) = exact {
            rows.retain(|row| at(row) == exact);
        }
        for (name, field) in [("accountID", "accountID"), ("deviceID", "deviceID")] {
            if let Some(value) = query.get(name) {
                let Some(number) = value.trim_start_matches('+').parse::<i64>().ok() else {
                    return Response::error(400, "simplex: bad request");
                };
                rows.retain(|row| row.get(field).and_then(Value::as_i64) == Some(number));
            }
        }
        // `_statistics_window`: sort= really orders, limit= caps (non-positive: none).
        if let Some(sort) = query.get("sort").filter(|sort| !sort.is_empty()) {
            let (key, direction) = sort.split_once(':').unwrap_or((sort.as_str(), ""));
            rows.sort_by(|left, right| {
                let value = |row: &Value| row.get(key).and_then(Value::as_i64);
                let order = (value(left).is_none(), value(left))
                    .cmp(&(value(right).is_none(), value(right)));
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
        if rows.is_empty() {
            // Measured: nothing but `size`; the Device and Account blocks only appear
            // with a series.
            return bare_empty();
        }
        let devices = self.statistics_devices();
        let account = json!({"id": 1, "key": "/accounts/1", "name": self.owner_username,
                             "defaultAudioLanguage": "en", "autoSelectAudio": true,
                             "defaultSubtitleLanguage": "en", "subtitleMode": 2, "thumb": ""});
        let size = rows.len() + devices.len() + 1;
        Response::json(
            200,
            json!({"MediaContainer": {"size": size, "Device": devices, "Account": [account],
                                      "StatisticsMedia": rows}}),
        )
    }
}
