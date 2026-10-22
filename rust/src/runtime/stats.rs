//! `/statistics/media`, ported from `sessions.py`'s play records and `server.py`'s
//! `statistics`.

use super::*;
use std::collections::BTreeMap;

impl Runtime {
    /// Polling is at most once per epoch second; CPU deltas use monotonic time.
    /// A first/failed CPU sample omits CPU fields instead of claiming idle.
    pub(super) fn route_resource_statistics(&mut self, query: &HashMap<String, String>) -> Response {
        if query.get("timespan").and_then(|value| value.trim_start_matches('+').parse::<i64>().ok()) != Some(6) {
            return Response::json(200, json!({"MediaContainer": {"size": 0}}));
        }
        let at = now();
        if self.resource_series.last().and_then(|row| row["at"].as_i64()).is_none_or(|last| last < at) {
            let sample = crate::platform::resource_sample();
            let metrics = sample.utilization(self.resource_sample.as_ref());
            let mut row = json!({"timespan": 6, "at": at});
            for (name, value) in [
                "hostCpuUtilization", "processCpuUtilization",
                "hostMemoryUtilization", "processMemoryUtilization",
            ].into_iter().zip(metrics) {
                if let Some(value) = value.filter(|value| value.is_finite()) {
                    row[name] = json!((value * 1000.0).round() / 1000.0);
                }
            }
            self.resource_sample = Some(sample);
            self.resource_series.push(row);
            let excess = self.resource_series.len().saturating_sub(180);
            self.resource_series.drain(..excess);
        }
        let series = resource_window(self.resource_series.clone(), query);
        Response::json(200, json!({"MediaContainer": {
            "size": series.len(), "StatisticsResources": series
        }}))
    }

    pub(super) fn statistics_accounts(&self, rows: &[Value]) -> Vec<Value> {
        let ids: BTreeSet<i64> = rows.iter().filter_map(|row| row["accountID"].as_i64()).collect();
        let mut known = self.shared_account_rows();
        known.push(json!({"id": accounts::OWNER, "key": "/accounts/1", "name": self.owner_name(),
            "defaultAudioLanguage": "en", "autoSelectAudio": true,
            "defaultSubtitleLanguage": "en", "subtitleMode": 2, "thumb": ""}));
        ids.into_iter().map(|id| {
            known.iter().find(|row| row["id"].as_i64() == Some(id)).cloned()
                .unwrap_or_else(|| json!({"id": id, "key": format!("/accounts/{id}"), "name": ""}))
        }).collect()
    }

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
            self.viewer.id,
        ));
        self.dirty = true;
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
            let mut rows: BTreeMap<(i64, i64, i64, i64), (i64, i64)> = BTreeMap::new();
            for (at, kind, seconds, device, account) in &self.plays {
                let kind_number = match kind.as_str() {
                    "show" => 2,
                    "season" => 3,
                    "episode" => 4,
                    _ => 1,
                };
                let key = (statistics_bucket(*at, span), kind_number, *device, *account);
                let (count, total) = rows.entry(key).or_default();
                *count += 1;
                *total += seconds;
            }
            for ((at, kind, device, account), (count, seconds)) in rows {
                out.push(json!({"accountID": account, "deviceID": device, "timespan": span,
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
                    return Response::error(400, "rex: bad request");
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
        let accounts = self.statistics_accounts(&rows);
        let size = rows.len() + devices.len() + accounts.len();
        Response::json(
            200,
            json!({"MediaContainer": {"size": size, "Device": devices, "Account": accounts,
                                      "StatisticsMedia": rows}}),
        )
    }
}

/// Preserve the observed resource grammar: sort/limit precede the strict lower
/// bound, and both at> and at>= are strict here (unlike the media rollup).
fn resource_window(rows: Vec<Value>, query: &HashMap<String, String>) -> Vec<Value> {
    let mut series = sessions::statistics_window(rows, query);
    if let Some(since) = query.get("at>").or_else(|| query.get("at>="))
        .filter(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|value| value.parse::<i64>().ok())
    {
        series.retain(|row| row["at"].as_i64().unwrap_or(0) > since);
    }
    series
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resource_bounds_preserve_strict_alias_and_window_order() {
        let rows = vec![json!({"at": 10}), json!({"at": 11}), json!({"at": 12})];
        for name in ["at>", "at>="] {
            let query = HashMap::from([(name.to_string(), "11".to_string())]);
            assert_eq!(resource_window(rows.clone(), &query), vec![json!({"at": 12})]);
        }
        let query = HashMap::from([
            ("at>".to_string(), "11".to_string()),
            ("limit".to_string(), "1".to_string()),
            ("sort".to_string(), "at:asc".to_string()),
        ]);
        assert!(resource_window(rows.clone(), &query).is_empty());
        let query = HashMap::from([("at>".to_string(), "-1".to_string())]);
        assert_eq!(resource_window(rows.clone(), &query), rows);
    }
}
