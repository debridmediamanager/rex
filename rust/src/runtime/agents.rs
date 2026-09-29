//! The legacy agent chain, ported from `server.py`'s `legacy_agent_config`.
//!
//! Cycle 58, read out of `agentservice.py` and swept on zen (1.42.2) and kids
//! (1.43.4) over fifteen agents x six media types.  A LEGACY primary agent lists every
//! agent that serves the type: the ones in its saved (or default) order first with
//! `enabled="1"`, the primary appended if the order left it out, then the rest by
//! identifier.  The Agents settings page draws exactly that and saves a drag with
//! `PUT ?order=`, which answers the `<combine>` document it wrote.

use super::*;

/// `agentservice.py`'s `media_types`: the class each mediaType combines under.
pub(super) fn legacy_media_class(number: i64) -> Option<&'static str> {
    Some(match number {
        1 => "Movie",
        2 => "TV_Show",
        8 => "Artist",
        9 => "Album",
        13 => "Photo",
        20 => "Plex_Personal_Media",
        _ => return None,
    })
}

fn xml_text(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// The framework's container of `Agent` rows, as `agentservice.py` writes it.
fn legacy_agent_document(rows: &[Value]) -> String {
    let head = format!(
        "<?xml version='1.0' encoding='utf-8'?>\n<MediaContainer noHistory=\"0\" \
         replaceParent=\"0\" size=\"{}\" identifier=\"com.plexapp.system\"",
        rows.len()
    );
    if rows.is_empty() {
        return head + "/>\n";
    }
    let lines = rows
        .iter()
        .map(|row| {
            format!(
                "  <Agent name=\"{}\"{} hasPrefs=\"{}\" identifier=\"{}\"/>",
                xml_text(&row.get("name").map(string_value).unwrap_or_default()),
                if row.get("enabled").and_then(Value::as_bool).unwrap_or(false) {
                    " enabled=\"1\""
                } else {
                    ""
                },
                i32::from(row.get("hasPrefs").and_then(Value::as_bool).unwrap_or(false)),
                xml_text(&row.get("identifier").map(string_value).unwrap_or_default()),
            )
        })
        .collect::<Vec<_>>();
    format!("{head}>\n{}\n</MediaContainer>\n", lines.join("\n"))
}

pub(super) fn framework_traceback(sentence: &str) -> Vec<u8> {
    format!(
        "<?xml version='1.0' encoding='utf-8'?>\n\
         <Response code=\"2000\" status=\"TypeError: {sentence}\">\n\
         \x20 <Traceback>Traceback (most recent call last):\n\
         \x20 File \"/usr/lib/plexmediaserver/Resources/Plug-ins-f737b826c/Framework.bundle/\
         Contents/Resources/Versions/2/Python/Framework/components/runtime.py\", line 843, \
         in handle_request\n\
         \x20   result = f(**d)\n\
         TypeError: {sentence}\n\
         </Traceback>\n\
         </Response>\n"
    )
    .into_bytes()
}

/// A plug-in's own error document: `application/xml`, and the framework keeps the
/// connection alive (cycle 53).
fn plugin_xml(status: u16, body: Vec<u8>) -> Response {
    Response {
        status,
        content_type: "application/xml".to_string(),
        body,
        headers: Vec::new(),
        close: false,
    }
}

impl Runtime {
    fn legacy_agent_error(&self, key: &str, values: &[(&str, &str)]) -> Response {
        let mut body = load_json_sidecar(&self.catalog_path, "legacy_agent_config.json")
            .and_then(|document| {
                document
                    .get("errors")
                    .and_then(|errors| errors.get(key))
                    .map(string_value)
            })
            .unwrap_or_default();
        for (name, value) in values {
            body = body.replace(&format!("{{{name}}}"), &xml_text(value));
        }
        plugin_xml(500, body.into_bytes())
    }

    /// `/system/agents/{identifier}/config/{mediaType}`, GET and PUT, for an agent the
    /// legacy table knows.  `None` leaves the request to the modern-agent handler.
    pub(super) fn legacy_agent_config(
        &mut self,
        method: &str,
        identifier: &str,
        media_type: &str,
        query: &HashMap<String, String>,
    ) -> Option<Response> {
        let document = load_json_sidecar(&self.catalog_path, "legacy_agent_config.json")?;
        let Ok(number) = media_type.trim().parse::<i64>() else {
            // Python's `repr()` of the string the framework handed over.
            let shown = format!("'{}'", media_type.replace('\\', "\\\\").replace('\'', "\\'"));
            return Some(self.legacy_agent_error(
                if method == "PUT" {
                    "valueerror_put"
                } else {
                    "valueerror_config"
                },
                &[("value", &shown)],
            ));
        };
        let Some(media_class) = legacy_media_class(number) else {
            return Some(Response {
                status: 404,
                content_type: String::new(),
                body: Vec::new(),
                headers: Vec::new(),
                close: false,
            });
        };
        if method == "PUT" {
            // `set_agent_configuration(self, mediaType, identifier, order)` -- no
            // default, and the framework hands an EMPTY value over as None.
            let Some(order) = query.get("order") else {
                return Some(plugin_xml(
                    500,
                    framework_traceback(
                        "set_agent_configuration() takes exactly 4 arguments (3 given)",
                    ),
                ));
            };
            if order.is_empty() {
                return Some(self.legacy_agent_error("order_empty", &[]));
            }
            let names = order.split(',').map(str::to_string).collect::<Vec<_>>();
            let sources = names
                .iter()
                .map(|name| format!("    <agent>{}</agent>\n", xml_text(name)))
                .collect::<String>();
            self.prefs.insert(
                format!("_agentOrder:{identifier}|{media_class}"),
                Value::Array(names.iter().map(|name| json!(name)).collect()),
            );
            let body = format!(
                "<?xml version='1.0' encoding='utf-8'?>\n<combine class=\"{}\">\n  \
                 <sources>\n{sources}  </sources>\n</combine>\n",
                xml_text(media_class)
            );
            // Cycle 53/58: a framework STRING result carries its header spelled
            // literally `Content-type` -- the one lower-case `t` on the surface.
            return Some(Response {
                status: 200,
                content_type: String::new(),
                body: body.into_bytes(),
                headers: vec![("Content-type".to_string(), "application/xml".to_string())],
                close: false,
            });
        }
        if number == 20 {
            return Some(self.legacy_agent_error("personal_media", &[]));
        }
        let default = document
            .get("config")
            .and_then(|config| config.get(format!("{identifier}|{number}")))
            .and_then(Value::as_array)
            .cloned();
        let Some(default) = default else {
            if matches!(number, 8 | 9) && !identifier.starts_with("com.plexapp.agents.") {
                return Some(
                    self.legacy_agent_error(&format!("keyerror_{number}"), &[("identifier", identifier)]),
                );
            }
            return Some(plugin_xml(200, legacy_agent_document(&[]).into_bytes()));
        };
        let mut agents = default.clone();
        let mut order = self
            .prefs
            .get(&format!("_agentOrder:{identifier}|{media_class}"))
            .and_then(Value::as_array)
            .map(|names| names.iter().map(string_value).collect::<Vec<_>>())
            .unwrap_or_else(|| {
                default
                    .iter()
                    .filter(|row| row.get("enabled").and_then(Value::as_bool).unwrap_or(false))
                    .map(|row| row.get("identifier").map(string_value).unwrap_or_default())
                    .collect()
            });
        if !order.iter().any(|name| name == identifier) {
            order.push(identifier.to_string());
        }
        let mut rows = Vec::new();
        for ident in &order {
            if let Some(position) = agents
                .iter()
                .position(|row| row.get("identifier").map(string_value).as_deref() == Some(ident))
            {
                let mut row = agents.remove(position);
                if let Some(object) = row.as_object_mut() {
                    object.insert("enabled".to_string(), json!(true));
                }
                rows.push(row);
            }
        }
        agents.sort_by_key(|row| row.get("identifier").map(string_value).unwrap_or_default());
        for mut row in agents {
            if let Some(object) = row.as_object_mut() {
                object.remove("enabled");
            }
            rows.push(row);
        }
        Some(plugin_xml(200, legacy_agent_document(&rows).into_bytes()))
    }

    /// `/system/agents/contributors?mediaType=&primaryAgent=` -- cycle 58, zen.
    pub(super) fn legacy_agent_contributors(&self, media_type: &str, primary: &str) -> Option<Response> {
        let document = load_json_sidecar(&self.catalog_path, "legacy_agent_config.json")?;
        let Ok(number) = media_type.trim().parse::<i64>() else {
            let shown = format!("'{}'", media_type.replace('\\', "\\\\").replace('\'', "\\'"));
            return Some(self.legacy_agent_error("valueerror_contributors", &[("value", &shown)]));
        };
        legacy_media_class(number)?;
        let rows = document
            .get("contributors")
            .and_then(|contributors| contributors.get(format!("{primary}|{number}")))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Some(plugin_xml(200, legacy_agent_document(&rows).into_bytes()))
    }
}
