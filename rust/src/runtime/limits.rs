//! WAN playback admission. Called under the Runtime lock, never around byte I/O.
//! Decisions only check; actual playback starts reserve until a timeline owns the slot.
use super::*;
use super::sessions::plex_value;

const PENDING_TTL: f64 = 30.0;
const SESSION_TTL: f64 = 180.0;
const MAX_PENDING: usize = 4096;

#[derive(Debug, Default)]
pub(super) struct StreamAdmissions {
    pending: HashMap<String, Reservation>,
}

#[derive(Debug)]
struct Reservation {
    account: i64,
    bitrate_kbps: i64,
    expires: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum AdmissionError {
    UnknownBitrate,
    PerStreamBitrate,
    PerUserStreams,
    TotalBitrate,
    PendingCapacity,
}

impl AdmissionError {
    pub(super) fn reason(self) -> &'static str {
        match self {
            Self::UnknownBitrate => "rex: WAN playback bitrate is unknown; a bounded conversion is required",
            Self::PerStreamBitrate => "rex: WAN per-stream bitrate limit exceeded",
            Self::PerUserStreams => "rex: WAN per-user stream limit reached",
            Self::TotalBitrate => "rex: WAN total upload bitrate limit reached",
            Self::PendingCapacity => "rex: playback admission queue is full",
        }
    }
}

fn raw_session_id(query: &HashMap<String, String>, headers: &HashMap<String, String>) -> String {
    plex_value(query, headers, "X-Plex-Session-Identifier")
        .or_else(|| plex_value(query, headers, "X-Plex-Client-Identifier").filter(|id| !id.is_empty()))
        .unwrap_or_else(|| "rex".into())
}

impl Runtime {
    fn wan_limit(&self, name: &str) -> i64 {
        self.server_pref(name).as_ref().and_then(value_as_i64).unwrap_or(0).max(0)
    }

    /// `lan` MUST come from the connection, not the client's `location` parameter.
    /// `bitrate_kbps` is the selected direct Media or actual conversion Plan total.
    pub(super) fn check_stream_admission(
        &self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
        lan: bool,
        bitrate_kbps: i64,
    ) -> Result<(), AdmissionError> {
        if lan { return Ok(()); }
        let per_stream = self.wan_limit("WanPerStreamMaxUploadRate");
        let total = self.wan_limit("WanTotalMaxUploadRate");
        let per_user = self.wan_limit("WanPerUserStreamCount");
        if bitrate_kbps <= 0 && (per_stream > 0 || total > 0) {
            return Err(AdmissionError::UnknownBitrate);
        }
        if per_stream > 0 && bitrate_kbps > per_stream {
            return Err(AdmissionError::PerStreamBitrate);
        }
        let now = now_seconds();
        let key = sessions::account_session_id(self.viewer.id, &raw_session_id(query, headers));
        let mut count = 0_i64;
        let mut used = 0_i64;
        // A pending start and its subsequent timeline are one stream. Ignore this
        // request's old slot: a seek/redecision atomically replaces, not adds to it.
        for (id, entry) in &self.sessions {
            if id == &key || !self.admission_session_live(id, entry, now) || entry.lan { continue; }
            count += i64::from(entry.account == self.viewer.id);
            let pending = self.stream_admissions.pending.get(id).filter(|r| r.expires > now);
            let bitrate = pending.map_or(entry.bitrate_kbps, |r| r.bitrate_kbps);
            // An unknown existing WAN stream consumes the full configured budget:
            // uncertainty must never silently create available WAN bandwidth.
            used = used.saturating_add(if bitrate > 0 { bitrate } else { total });
        }
        for (id, entry) in &self.stream_admissions.pending {
            if id == &key || entry.expires <= now { continue; }
            if self.sessions.get(id).is_some_and(|session| self.admission_session_live(id, session, now)) {
                continue;
            }
            count += i64::from(entry.account == self.viewer.id);
            used = used.saturating_add(if entry.bitrate_kbps > 0 { entry.bitrate_kbps } else { total });
        }
        if per_user > 0 && count >= per_user { return Err(AdmissionError::PerUserStreams); }
        if total > 0 && used.saturating_add(bitrate_kbps) > total { return Err(AdmissionError::TotalBitrate); }
        Ok(())
    }

    fn admission_session_live(&self, key: &str, entry: &sessions::SessionEntry, now: f64) -> bool {
        entry.seen >= now - SESSION_TTL
            && entry.player.get("state").and_then(Value::as_str) != Some("stopped")
            && !self.terminated_sessions.contains_key(key)
    }

    /// Check and reserve in one critical section before starting FFmpeg or a Part
    /// body. The caller MUST NOT unlock between a separate check and this call.
    pub(super) fn reserve_stream_admission(
        &mut self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
        lan: bool,
        bitrate_kbps: i64,
    ) -> Result<(), AdmissionError> {
        self.reap_stream_admissions();
        self.check_stream_admission(query, headers, lan, bitrate_kbps)?;
        let key = sessions::account_session_id(self.viewer.id, &raw_session_id(query, headers));
        if lan {
            self.stream_admissions.pending.remove(&key);
            return Ok(());
        }
        if !self.stream_admissions.pending.contains_key(&key) && self.stream_admissions.pending.len() >= MAX_PENDING {
            return Err(AdmissionError::PendingCapacity);
        }
        self.stream_admissions.pending.insert(key, Reservation {
            account: self.viewer.id,
            bitrate_kbps,
            expires: now_seconds() + PENDING_TTL,
        });
        Ok(())
    }

    /// Stop/termination must also remove or mark the corresponding SessionEntry
    /// stopped; removing only a pending slot cannot terminate a live timeline.
    pub(super) fn release_stream_admission(&mut self, account: i64, raw_id: &str) {
        self.stream_admissions.pending.remove(&sessions::account_session_id(account, raw_id));
    }

    pub(super) fn reserved_stream_bitrate(&self, account: i64, raw_id: &str) -> Option<i64> {
        self.stream_admissions.pending.get(&sessions::account_session_id(account, raw_id))
            .filter(|reservation| reservation.expires > now_seconds()).map(|reservation| reservation.bitrate_kbps)
    }

    pub(super) fn reap_stream_admissions(&mut self) {
        let now = now_seconds();
        self.stream_admissions.pending.retain(|_, entry| entry.expires > now);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runtime() -> Runtime {
        let built = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let crate_dir = if built.exists() { built } else { PathBuf::from(".") };
        Runtime::load(RuntimeOptions {
            catalog: crate_dir.join("../tests/catalog_ui.json"),
            state: Some(PathBuf::new()),
            ..RuntimeOptions::default()
        }).expect("captured UI catalog")
    }

    fn query(id: &str) -> HashMap<String, String> {
        HashMap::from([
            ("X-Plex-Session-Identifier".into(), id.into()),
            ("X-Plex-Client-Identifier".into(), "same-device".into()),
            ("location".into(), "lan".into()),
        ])
    }

    #[test]
    fn wan_stream_count_is_per_account_not_device_or_raw_session() {
        let mut rt = runtime();
        rt.prefs.insert("WanPerUserStreamCount".into(), json!(1));
        let headers = HashMap::new();
        rt.reserve_stream_admission(&query("one"), &headers, false, 1000).unwrap();
        assert_eq!(rt.reserve_stream_admission(&query("two"), &headers, false, 1000), Err(AdmissionError::PerUserStreams));
        rt.viewer.id = 42;
        rt.reserve_stream_admission(&query("one"), &headers, false, 1000).unwrap();
        assert_eq!(rt.stream_admissions.pending.len(), 2);
        rt.reserve_stream_admission(&query("lan"), &headers, true, 1000).unwrap();
    }

    #[test]
    fn start_reservations_serialize_the_total_budget_and_seek_replaces() {
        // Production requests hold the same Runtime mutex. These back-to-back
        // reserves model simultaneous starts after both decisions passed.
        let mut rt = runtime();
        rt.prefs.insert("WanTotalMaxUploadRate".into(), json!(1500));
        let headers = HashMap::new();
        for id in ["one", "two"] { rt.check_stream_admission(&query(id), &headers, false, 1000).unwrap(); }
        rt.reserve_stream_admission(&query("one"), &headers, false, 1000).unwrap();
        rt.check_stream_admission(&query("one"), &headers, false, 1000).unwrap();
        rt.reserve_stream_admission(&query("one"), &headers, false, 1100).unwrap();
        assert_eq!(rt.stream_admissions.pending.len(), 1);
        assert_eq!(rt.reserve_stream_admission(&query("two"), &headers, false, 1000), Err(AdmissionError::TotalBitrate));
        rt.release_stream_admission(rt.viewer.id, "one");
        rt.reserve_stream_admission(&query("two"), &headers, false, 1000).unwrap();
    }

    #[test]
    fn timeline_and_pending_are_counted_once_and_stop_reap_release() {
        let mut rt = runtime();
        rt.prefs.insert("WanPerUserStreamCount".into(), json!(1));
        let headers = HashMap::new();
        let account = rt.viewer.id;
        rt.reserve_stream_admission(&query("one"), &headers, false, 1000).unwrap();
        rt.touch_session("1007", false, 1000, &query("one"), &headers, "playing", 0, None);
        let key = sessions::account_session_id(account, "one");
        rt.sessions.get_mut(&key).unwrap().lan = false;
        rt.check_stream_admission(&query("one"), &headers, false, 1000).unwrap();
        assert_eq!(rt.check_stream_admission(&query("two"), &headers, false, 1000), Err(AdmissionError::PerUserStreams));
        rt.sessions.get_mut(&key).unwrap().player.insert("state".into(), json!("stopped"));
        rt.release_stream_admission(account, "one");
        rt.reserve_stream_admission(&query("two"), &headers, false, 1000).unwrap();
        let pending = sessions::account_session_id(account, "two");
        rt.stream_admissions.pending.get_mut(&pending).unwrap().expires = now_seconds() - 1.0;
        rt.reap_stream_admissions();
        rt.sessions.get_mut(&key).unwrap().player.insert("state".into(), json!("playing"));
        rt.sessions.get_mut(&key).unwrap().seen = now_seconds() - 181.0;
        rt.check_stream_admission(&query("three"), &headers, false, 1000).unwrap();
        assert!(rt.stream_admissions.pending.is_empty());
    }

    #[test]
    fn simultaneous_starts_cannot_overbook_total_bandwidth() {
        use std::sync::{Arc, Barrier, Mutex};
        let mut rt = runtime();
        rt.prefs.insert("WanTotalMaxUploadRate".into(), json!(1500));
        let runtime = Arc::new(Mutex::new(rt));
        let barrier = Arc::new(Barrier::new(2));
        let workers = ["first", "second"].map(|id| {
            let runtime = Arc::clone(&runtime);
            let barrier = Arc::clone(&barrier);
            std::thread::spawn(move || {
                barrier.wait();
                runtime.lock().unwrap().reserve_stream_admission(&query(id), &HashMap::new(), false, 1000)
            })
        });
        let results = workers.map(|worker| worker.join().unwrap());
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(results.iter().filter(|result| **result == Err(AdmissionError::TotalBitrate)).count(), 1);
    }

    #[test]
    fn spoofed_lan_does_not_bypass_bitrate_or_unknown_source_policy() {
        let mut rt = runtime();
        rt.prefs.insert("WanPerStreamMaxUploadRate".into(), json!(1000));
        let headers = HashMap::new();
        assert_eq!(rt.check_stream_admission(&query("one"), &headers, false, 1001), Err(AdmissionError::PerStreamBitrate));
        assert_eq!(rt.check_stream_admission(&query("one"), &headers, false, 0), Err(AdmissionError::UnknownBitrate));
        rt.check_stream_admission(&query("one"), &headers, true, 0).unwrap();
    }

    #[test]
    fn terminated_sessions_and_expired_pending_do_not_hold_capacity() {
        let mut rt = runtime();
        rt.prefs.insert("WanPerUserStreamCount".into(), json!(1));
        let headers = HashMap::new();
        rt.touch_session("1007", false, 1000, &query("one"), &headers, "playing", 0, None);
        let key = sessions::account_session_id(rt.viewer.id, "one");
        rt.sessions.get_mut(&key).unwrap().lan = false;
        rt.terminated_sessions.insert(key, "ended".into());
        rt.reserve_stream_admission(&query("two"), &headers, false, 1000).unwrap();
        let pending = sessions::account_session_id(rt.viewer.id, "two");
        rt.stream_admissions.pending.get_mut(&pending).unwrap().expires = now_seconds() - 1.0;
        rt.check_stream_admission(&query("three"), &headers, false, 1000).unwrap();
    }
}
