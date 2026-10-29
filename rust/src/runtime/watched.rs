//! `POST /rex/import-watched`: what an account watched, started and rated on another Plex
//! server, read off that server and kept here as the account's own.
//!
//! The body is the address Plex Web's View XML opens on the old server: its origin and the
//! account's token there, one paste, nothing typed. Whose state it becomes is the account of
//! the request's own token, as for a play: the owner's rows, or a share's view of them
//! (`accounts.rs`). rex asks plex.tv nothing with the pasted token, since rex reads no account
//! token (2026-10-19), and the old server's answer is all it trusts.
//!
//! What the old server says is `tests/fixtures/watch_import_s143.json` (plexscratch 1.43.4,
//! `probes/record_watch_import.py`): every film and episode of each library, paged, with
//! `viewCount`, `viewOffset`, `lastViewedAt`, `userRating` and `lastRatedAt` as the token's
//! account holds them. A row here is the same title when it has the same `plex://` guid,
//! which rex's rows and the old server both have from Plex's agent: on fun's old library that
//! was every watched film and episode but 74 trailers and `local://` items.
//! ponytail: a library on Plex's legacy agents (`com.plexapp.agents.*` guids) matches nothing;
//! match by its imdb/tmdb/tvdb ids if users bring such libraries.

use super::*;

/// Leaves asked per page. PMS pages any size it is asked (`totalSize`, `offset`), and the
/// page past the end is empty.
const PAGE: usize = 500;

/// The fields a leaf's state is read from, as PMS names them and rex keeps them.
const FIELDS: [&str; 5] = ["viewCount", "viewOffset", "lastViewedAt", "userRating", "lastRatedAt"];

/// The old server's origin and the token the pasted address carries.
fn old_server(body: &[u8]) -> Result<(String, String), &'static str> {
    let address = std::str::from_utf8(body).unwrap_or_default().trim();
    let Some((scheme, rest)) = address.split_once("://").filter(|(scheme, _)| matches!(*scheme, "http" | "https")) else {
        return Err("send the address View XML opens on your old Plex server, starting http:// or https://");
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.is_empty() || authority.contains('@') {
        return Err("that address names no server");
    }
    let token = rest
        .split_once('?')
        .map(|(_, query)| parse_query(query.split('#').next().unwrap_or_default()))
        .unwrap_or_default()
        .into_iter()
        .rev()
        .find(|(name, value)| name == "X-Plex-Token" && !value.is_empty())
        .map(|(_, value)| value);
    let Some(token) = token else {
        return Err("that address has no X-Plex-Token: copy it whole from the tab View XML opens");
    };
    Ok((format!("{scheme}://{authority}"), token))
}

fn ask(origin: &str, token: &str, path: &str) -> Result<Value, String> {
    let headers = [("Accept", "application/json".to_string()), ("X-Plex-Token", token.to_string())];
    let (status, _, body) = upstream::fetch("GET", &format!("{origin}{path}"), &headers, b"", std::time::Duration::from_secs(60))
        .map_err(|failure| format!("could not reach {origin}: {failure}"))?;
    match status {
        200 => serde_json::from_slice(&body).map_err(|_| format!("{origin} did not answer as a Plex server")),
        401 => Err(format!("{origin} refused the token: copy the address from View XML again")),
        status => Err(format!("{origin} answered HTTP {status}")),
    }
}

/// Every film and episode the token's account watched, started or rated on the old server,
/// each its `guid` and `FIELDS`; `{"error"}` when the server could not be read.
fn read(origin: &str, token: &str) -> Value {
    let mut items = Vec::new();
    let sections = match ask(origin, token, "/library/sections") {
        Ok(sections) => sections,
        Err(error) => return json!({"error": error}),
    };
    for directory in sections.pointer("/MediaContainer/Directory").and_then(Value::as_array).into_iter().flatten() {
        let kind = match directory.get("type").and_then(Value::as_str) {
            Some("movie") => 1,
            Some("show") => 4,
            _ => continue,
        };
        let key = directory.get("key").map(string_value).unwrap_or_default();
        if key.is_empty() || !key.bytes().all(|byte| byte.is_ascii_digit()) {
            continue;
        }
        let mut start = 0;
        loop {
            let path = format!(
                "/library/sections/{key}/all?type={kind}&X-Plex-Container-Start={start}&X-Plex-Container-Size={PAGE}"
            );
            let page = match ask(origin, token, &path) {
                Ok(page) => page,
                Err(error) => return json!({"error": error}),
            };
            let rows = page.pointer("/MediaContainer/Metadata").and_then(Value::as_array).cloned().unwrap_or_default();
            for row in &rows {
                let mut item = Map::new();
                for name in FIELDS {
                    if let Some(value) = row.get(name).filter(|value| !value.is_null()) {
                        item.insert(name.to_string(), value.clone());
                    }
                }
                let held = ["viewCount", "viewOffset", "userRating"].iter().any(|name| item.contains_key(*name));
                if let (true, Some(guid)) = (held, row.get("guid").and_then(Value::as_str)) {
                    item.insert("guid".to_string(), json!(guid));
                    items.push(Value::Object(item));
                }
            }
            if rows.len() < PAGE {
                break;
            }
            start += PAGE;
        }
    }
    json!({"items": items})
}

fn number(row: &Value, name: &str) -> Option<f64> {
    row.get(name)
        .and_then(|value| value.as_f64().or_else(|| value.as_str().and_then(|text| text.parse().ok())))
        .filter(|value| *value > 0.0)
}

impl Runtime {
    pub(super) fn route_import_watched(&mut self, request: &Request) -> Response {
        if request.method != "POST" {
            return Response::error(405, "rex: method not allowed");
        }
        // rex dials the address it is sent, the old server on the LAN as often as not: only a
        // caller on rex's own networks may name one, never one rex-direct lets in.
        if CONNECTION.with(std::cell::Cell::get).is_some_and(|(peer, _)| !feed::source_peer(peer)) {
            return Response::json(403, json!({"error": "run the import from your home network"}));
        }
        let (origin, token) = match old_server(&request.body) {
            Ok(server) => server,
            Err(error) => return Response::json(400, json!({"error": error})),
        };
        let call = format!("import-watched\0{}\0{origin}\0{token}", self.viewer.id);
        let Some(answer) = self.agent_answer(call, move || read(&origin, &token)) else {
            return Response::empty(200);
        };
        let Some(items) = answer.get("items").and_then(Value::as_array) else {
            let error = answer.get("error").and_then(Value::as_str).unwrap_or("the old server could not be read");
            return Response::json(502, json!({"error": error}));
        };
        Response::json(200, self.import_watched(items))
    }

    /// The old server's leaves onto the rows with their guid, for the account the request
    /// runs as. An import, as plex.tv's is (`import_account_state`): its dates and counts, no
    /// history row and nothing pushed back. What the account already holds here stays: a row
    /// it watched or started keeps that, a row it rated keeps its stars.
    fn import_watched(&mut self, items: &[Value]) -> Value {
        let mut keys: HashMap<String, Vec<String>> = HashMap::new();
        for row in self.catalog.hot().lazy() {
            if !matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode")) || !self.visible(&row) {
                continue;
            }
            if let (Some(guid), Some(key)) = (row.get("guid").and_then(Value::as_str), key_of(&row)) {
                if !self.deleted.contains(&key) {
                    keys.entry(guid.to_string()).or_default().push(key);
                }
            }
        }
        let (mut watched, mut started, mut rated, mut kept, mut missing) = (0, 0, 0, 0, 0);
        let mut stamped = Vec::new();
        for item in items {
            let Some(rows) = item.get("guid").and_then(Value::as_str).and_then(|guid| keys.get(guid)) else {
                missing += 1;
                continue;
            };
            let count = number(item, "viewCount").map(|count| count as i64);
            let offset = number(item, "viewOffset").map(|offset| offset as i64);
            let viewed = number(item, "lastViewedAt").map(|at| at as i64);
            let stars = number(item, "userRating");
            for key in rows.clone() {
                let Some(row) = self.catalog.get(&key).cloned() else { continue };
                let mut changed = false;
                if count.is_some() || offset.is_some() {
                    if number(&row, "viewCount").is_some() || number(&row, "viewOffset").is_some() {
                        kept += 1;
                    } else {
                        for (field, value) in [("viewCount", count), ("viewOffset", offset), ("lastViewedAt", viewed)] {
                            if let Some(value) = value {
                                self.update_view(&key, field, Some(json!(value)));
                            }
                        }
                        if count.is_some() { watched += 1 } else { started += 1 }
                        changed = true;
                        // A play bumps its season and show by one each (`scrobble_key_on`);
                        // these are the old server's plays.
                        if let (Some(count), Some("episode")) = (count, row.get("type").and_then(Value::as_str)) {
                            for parent in self.parent_keys(&key) {
                                let before = self.catalog.get(&parent).cloned().unwrap_or(Value::Null);
                                let plays = number(&before, "viewCount").unwrap_or(0.0) as i64 + count;
                                let last = (number(&before, "lastViewedAt").unwrap_or(0.0) as i64).max(viewed.unwrap_or(0));
                                self.update_view(&parent, "viewCount", Some(json!(plays)));
                                if last > 0 {
                                    self.update_view(&parent, "lastViewedAt", Some(json!(last)));
                                }
                                stamped.push(parent);
                            }
                        }
                    }
                }
                if let Some(stars) = stars.filter(|_| number(&row, "userRating").is_none()) {
                    self.update_view(&key, "userRating", Some(json!(stars)));
                    if let Some(at) = number(item, "lastRatedAt") {
                        self.update_view(&key, "lastRatedAt", Some(json!(at as i64)));
                    }
                    rated += 1;
                    changed = true;
                }
                if changed {
                    stamped.push(key);
                }
            }
        }
        stamped.sort();
        stamped.dedup();
        self.stamp_view(&stamped);
        self.save_if_dirty();
        info!(
            "import-watched for account {}: {watched} watched, {started} started, {rated} rated, {kept} already here, {missing} not in rex",
            self.viewer.id
        );
        json!({"watched": watched, "inProgress": started, "rated": rated, "alreadyHere": kept, "notInRex": missing})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// What View XML opens: the item's address with Plex Web's query and the token last.
    #[test]
    fn the_pasted_address_names_the_server_and_its_token() {
        let pasted = b" https://192-168-1-5.0123abcd.plex.direct:32400/library/metadata/18?checkFiles=1&includeAllConcerts=1&X-Plex-Token=abc-123\n";
        assert_eq!(old_server(pasted), Ok(("https://192-168-1-5.0123abcd.plex.direct:32400".to_string(), "abc-123".to_string())));
        assert_eq!(old_server(b"http://192.168.1.5:32400/?X-Plex-Token=t#x"), Ok(("http://192.168.1.5:32400".to_string(), "t".to_string())));
        for refused in [&b""[..], b"hello", b"ftp://h/?X-Plex-Token=t", b"http:///x?X-Plex-Token=t",
                        b"http://user@h/?X-Plex-Token=t", b"http://h/library/metadata/18", b"http://h/?X-Plex-Token="] {
            assert!(old_server(refused).is_err(), "{}", String::from_utf8_lossy(refused));
        }
    }

    /// rex-direct forwards rex's port from the internet: a public peer, even one with the
    /// owner's token, never makes rex dial an address.
    #[test]
    fn a_public_peer_cannot_name_a_server_to_dial() {
        let dir = std::env::temp_dir().join(format!("rex-watched-peer-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let catalog = dir.join("catalog.json");
        std::fs::write(&catalog, br#"{"format": 1, "machineIdentifier": "watched-test", "friendlyName": "t", "sections": [], "items": []}"#).unwrap();
        let mut runtime = Runtime::load(RuntimeOptions {
            catalog,
            state: Some(dir.join("no-state")),
            assert_claimed: true,
            ..RuntimeOptions::default()
        })
        .unwrap();
        runtime.token = Some("owner-token".to_string());
        let request = Request {
            method: "POST".to_string(),
            target: "/rex/import-watched?X-Plex-Token=owner-token".to_string(),
            version: "HTTP/1.1".to_string(),
            headers: HashMap::new(),
            body: b"http://192.168.1.5:32400/library/metadata/1?X-Plex-Token=old".to_vec(),
            body_complete: true,
        };
        let local: std::net::IpAddr = "192.168.1.2".parse().unwrap();
        CONNECTION.with(|cell| cell.set(Some(("203.0.113.7".parse().unwrap(), local))));
        let response = runtime.route(&request);
        assert_eq!((response.status, String::from_utf8_lossy(&response.body).contains("home network")), (403, true));
        assert!(runtime.pending_agent.is_none(), "a public peer's address was queued to be read");
        CONNECTION.with(|cell| cell.set(Some(("192.168.1.10".parse().unwrap(), local))));
        runtime.route(&request);
        assert!(runtime.pending_agent.is_some(), "a LAN peer's address was not read");
        CONNECTION.with(|cell| cell.set(None));
        let _ = std::fs::remove_dir_all(dir);
    }
}
