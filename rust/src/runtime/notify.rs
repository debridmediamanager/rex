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

    /// Shape measured on the production server mid-refresh: uuid, type, cancellable,
    /// userID, title, subtitle, progress. Pushes the `started` frame.
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
