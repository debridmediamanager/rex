//! The notification half of the item context menu and the library scans: the
//! timeline frames, activities and status rows a real PMS pushes, and the unmatch
//! cascade they announce. Ported from `server.py`'s `notify_*` family and
//! `library.py`'s `unmatch`, whose docstrings hold the measurements.

use super::*;

impl Runtime {
    /// A fresh activity uuid; the reference mints a random v4 per activity.
    pub(super) fn activity_uuid(&mut self) -> String {
        self.activity_serial += 1;
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        let hex = bytes_digest(format!("{nanos}/{}", self.activity_serial).as_bytes());
        format!(
            "{}-{}-4{}-a{}-{}",
            &hex[0..8],
            &hex[8..12],
            &hex[13..16],
            &hex[17..20],
            &hex[20..32]
        )
    }

    pub(super) fn push_activity(&mut self, activity: &Value, event: &str) {
        let uuid = activity.get("uuid").cloned().unwrap_or(Value::Null);
        self.push_event(
            "activity",
            json!({"ActivityNotification": [{"event": event, "uuid": uuid, "Activity": activity}]}),
        );
    }

    /// One activity from `started` to `ended`, with the measured `updated` steps --
    /// (progress, subtitle) pairs -- between; the end keeps the last subtitle.
    pub(super) fn push_activity_run(&mut self, kind: &str, title: &str, steps: &[(i64, &str)]) {
        let uuid = self.activity_uuid();
        let mut activity = json!({"uuid": uuid, "type": kind, "cancellable": false,
            "userID": 1, "title": title, "subtitle": "", "progress": 0});
        self.push_activity(&activity, "started");
        for (progress, subtitle) in steps {
            activity["progress"] = json!(progress);
            activity["subtitle"] = json!(subtitle);
            self.push_activity(&activity, "updated");
        }
        activity["progress"] = json!(100);
        self.push_activity(&activity, "ended");
    }

    // The timeline's state alphabet, read off the reference and pinned against the
    // client's own reducer: 0 created, 3 queued/processing/loading an edit, 4 loading,
    // 5 DONE (and it carries NO metadataState), 9 deleted. `5` is what makes the
    // client refetch the row.

    /// Plex's numeric type code as a TimelineEntry carries it. A playlist is 15 and a
    /// collection 18 on the wire, and the client builds the URL of the row it
    /// updates from this number.
    pub(super) fn timeline_type(kind: &str) -> i64 {
        match kind {
            "movie" => 1,
            "show" => 2,
            "season" => 3,
            "episode" => 4,
            "playlist" => 15,
            "collection" => 18,
            _ => 1,
        }
    }

    /// Shape measured on fun mid-refresh: uuid, type, cancellable, userID, title,
    /// subtitle, progress. Pushes the `started` frame.
    pub(super) fn add_activity(
        &mut self,
        subtitle: &str,
        title: &str,
        kind: &str,
        progress: i64,
    ) -> Value {
        let uuid = self.activity_uuid();
        let activity = json!({"uuid": uuid, "type": kind, "cancellable": false,
            "userID": 1, "title": title, "subtitle": subtitle, "progress": progress});
        self.push_activity(&activity, "started");
        activity
    }

    pub(super) fn add_metadata_activity(&mut self, subtitle: &str) -> Value {
        self.add_activity(
            subtitle,
            "Updating Metadata",
            "library.update.item.metadata",
            -1,
        )
    }

    /// End an activity the way a real refresh does: an `updated` at 49 unless the
    /// sequence already pushed it, then `ended` at 100.
    pub(super) fn finish_activity(&mut self, activity: &mut Value) {
        if activity.get("progress").and_then(Value::as_i64) != Some(49) {
            activity["progress"] = json!(49);
            self.push_activity(activity, "updated");
        }
        activity["progress"] = json!(100);
        self.push_activity(activity, "ended");
    }

    /// started -> ended with NO `updated` between (the chapter-thumbs activity, the
    /// season cascade).
    pub(super) fn end_activity_quietly(&mut self, activity: &mut Value) {
        self.push_activity(activity, "ended");
    }

    /// The `backgroundProcessingQueue` frame that follows a queue-touching write
    /// (cycle 15). The measured delay is not reproduced.
    pub(super) fn notify_queue_regenerated(&mut self) {
        self.push_event(
            "backgroundProcessingQueue",
            json!({"BackgroundProcessingQueueEventNotification":
                [{"queueID": 1, "event": "queueRegenerated"}]}),
        );
    }
}
