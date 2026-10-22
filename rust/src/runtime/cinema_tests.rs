use super::*;

fn movie(key: &str, rating: &str) -> Value {
    json!({"ratingKey": key, "key": format!("/library/metadata/{key}"),
        "type":"movie", "title":key, "guid":format!("plex://movie/{key}"),
        "librarySectionID":1, "contentRating":rating,
        "Media":[{"id":key,"Part":[{"id":key,"file":format!("/cinema/{key}.mp4"),
            "key":format!("/library/parts/{key}/0/file.mp4"),"container":"mp4"}]}]})
}

fn clip(key: &str, parent: &str, index: i64) -> Value {
    let mut row = movie(key, "G");
    let object = row.as_object_mut().unwrap();
    object.insert("type".into(), json!("clip"));
    object.insert("extraType".into(), json!(1));
    object.insert("index".into(), json!(index));
    object.insert("_parentRatingKey".into(), json!(parent));
    object.insert("_librarySectionID".into(), json!(1));
    object.remove("librarySectionID");
    row
}

fn runtime() -> (Runtime, PathBuf) {
    let dir = std::env::temp_dir().join(format!("rex-cinema-{}-{}", std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
    fs::create_dir_all(&dir).unwrap();
    let catalog = dir.join("catalog.json");
    let mut watched = movie("30", "G");
    watched["viewCount"] = json!(1);
    let mut alternate = movie("40", "G");
    alternate["guid"] = json!("plex://movie/20");
    fs::write(&catalog, json!({"machineIdentifier":"cinema-test", "sections":[
        {"key":"1","type":"movie","title":"Cinema","uuid":"cinema-movies"}],
        "items":[movie("10","G"),movie("20","G"),watched,alternate,movie("50","R"),
            clip("101","10",1),clip("201","20",1),clip("202","20",2),
            clip("301","30",1),clip("401","40",1),clip("501","50",1),
            clip("601","20",3),clip("602","20",4)]}).to_string()).unwrap();
    let mut runtime = Runtime::load(RuntimeOptions { catalog, ..RuntimeOptions::default() }).unwrap();
    runtime.sources.push(Arc::new(source::Source::new("cinema", "http://127.0.0.1:9", None, Some("/cinema"))));
    runtime.prefs.insert("CinemaTrailersFromLibrary".into(), json!(true));
    runtime.prefs.insert("CinemaTrailersType".into(), json!(1));
    (runtime, dir)
}

fn query(count: &str) -> HashMap<String, String> {
    // Plex Web request shape recorded in playqueue_continuous.json; only the
    // fixture's episode URI/count are replaced with this source's movie/count.
    HashMap::from_iter([
        ("type".into(), "video".into()), ("extrasPrefixCount".into(), count.into()),
        ("uri".into(), "server://cinema-test/com.plexapp.plugins.library/library/metadata/10".into()),
        ("key".into(), "/library/metadata/10".into()), ("repeat".into(), "0".into()),
        ("shuffle".into(), "0".into()), ("continuous".into(), "1".into()),
    ])
}

fn post(runtime: &mut Runtime, query: &HashMap<String, String>) -> (u16, Value) {
    let request = Request { method:"POST".into(), target:"/playQueues".into(), version:"HTTP/1.1".into(),
        headers:HashMap::new(), body:Vec::new(), body_complete:true };
    let response = runtime.route_play_queue(&[], query, &request);
    (response.status, serde_json::from_slice(&response.body).unwrap_or(Value::Null))
}

fn keys(doc: &Value) -> Vec<String> {
    doc["MediaContainer"]["Metadata"].as_array().unwrap().iter().filter_map(key_of).collect()
}

#[test]
fn cinema_post_preserves_feature_identity_starts_at_prefix_and_does_not_repeat_insert() {
    let (mut runtime, dir) = runtime();
    let q = query("2");
    let (status, doc) = post(&mut runtime, &q);
    assert_eq!(status, 200);
    // One trailer/movie; no self trailer and no alternate version duplicate.
    assert_eq!(keys(&doc), ["201", "501", "10"]);
    let mc = &doc["MediaContainer"];
    assert_eq!(mc["playQueueSelectedItemOffset"], 0);
    assert_eq!(mc["playQueueSelectedItemID"], mc["Metadata"][0]["playQueueItemID"]);
    assert_eq!(mc["playQueueSelectedMetadataItemID"], "10");
    let queue_id = string_value(&mc["playQueueID"]);
    let stored = runtime.queues.get(&queue_id).unwrap();
    let reread = runtime.queue_container(stored, &q);
    assert_eq!(keys(&serde_json::from_slice::<Value>(&reread.body).unwrap()), ["201", "501", "10"]);
    let mut repeated = query("2");
    repeated.insert("repeat".into(), "1".into());
    assert_eq!(keys(&post(&mut runtime, &repeated).1), ["201", "501", "10"]);
    let mut multiple = query("1");
    multiple.insert("uri".into(), "server://cinema-test/com.plexapp.plugins.library/library/metadata/20,10".into());
    let (_, selected_feature) = post(&mut runtime, &multiple);
    assert_eq!(keys(&selected_feature), ["20", "501", "10"]);
    assert_eq!(selected_feature["MediaContainer"]["playQueueSelectedItemOffset"], 1);
    assert_eq!(selected_feature["MediaContainer"]["playQueueSelectedMetadataItemID"], "10");
    let inserted = &selected_feature["MediaContainer"]["Metadata"][1];
    assert_eq!(selected_feature["MediaContainer"]["playQueueSelectedItemID"], inserted["playQueueItemID"]);
    runtime.prefs.insert("CinemaTrailersType".into(), json!(0));
    assert_eq!(keys(&post(&mut runtime, &query("3")).1), ["201", "301", "501", "10"]);
    for count in ["0", "-1"] {
        assert_eq!(keys(&post(&mut runtime, &query(count)).1), ["10"]);
    }
    runtime.section_prefs.insert(("1".into(), "enableCinemaTrailers".into()), "false".into());
    assert_eq!(keys(&post(&mut runtime, &query("2")).1), ["10"]);
    drop(runtime);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn prerolls_obey_sequence_random_boundaries_source_registration_and_share_filters() {
    let (mut runtime, dir) = runtime();
    runtime.prefs.insert("CinemaTrailersFromLibrary".into(), json!(false));
    runtime.prefs.insert("CinemaTrailersPrerollID".into(), json!("/cinema/601.mp4,/cinema/602.mp4"));
    assert_eq!(keys(&post(&mut runtime, &query("1")).1), ["601", "602", "10"]);
    assert_eq!(preroll_paths("a;b;c", 0).unwrap(), ["a"]);
    assert_eq!(preroll_paths("a;b;c", 2).unwrap(), ["c"]);
    assert_eq!(preroll_paths("a;b;c", 3).unwrap(), ["a"]);
    let rows = vec![runtime.catalog.get("10").unwrap().clone()];
    runtime.prefs.insert("CinemaTrailersPrerollID".into(), json!("/cinema/601.mp4;/cinema/602.mp4"));
    for (entropy, expected) in [(0,"601"),(1,"602"),(2,"601")] {
        let prefix = runtime.cinema_prefix(&query("1"), &rows, entropy).unwrap_or_else(|_| panic!("valid registered pre-roll"));
        assert_eq!(prefix.rows.iter().filter_map(key_of).collect::<Vec<_>>(), [expected]);
        assert_eq!(prefix.main_key.as_deref(), Some("10"));
    }
    // A configured non-first version keeps the exact Part in the wire queue.
    let alternative = movie("603", "G")["Media"][0].clone();
    runtime.catalog.get_mut("601").unwrap()["Media"].as_array_mut().unwrap().push(alternative);
    runtime.prefs.insert("CinemaTrailersPrerollID".into(), json!("/cinema/603.mp4"));
    let (_, exact) = post(&mut runtime, &query("1"));
    assert_eq!(exact["MediaContainer"]["Metadata"][0]["Media"].as_array().unwrap().len(), 1);
    assert_eq!(exact["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0]["file"], "/cinema/603.mp4");
    assert_eq!(post(&mut runtime, &query("nonsense")).0, 400);
    let mut no_cinema = query("1");
    no_cinema.remove("extrasPrefixCount");
    assert_eq!(keys(&post(&mut runtime, &no_cinema).1), ["10"]);
    runtime.prefs.insert("CinemaTrailersPrerollID".into(), json!("/private/not-registered.mp4"));
    assert_eq!(post(&mut runtime, &query("1")).0, 409);
    runtime.prefs.insert("CinemaTrailersPrerollID".into(), json!("/cinema/601.mp4"));
    runtime.sources.clear();
    assert_eq!(post(&mut runtime, &query("1")).0, 409);
    runtime.sources.push(Arc::new(source::Source::new("cinema", "http://127.0.0.1:9", None, Some("/cinema"))));
    runtime.viewer = accounts::Grant { id:42, sections:Some(HashSet::from(["1".into()])),
        filter_movies:"contentRating=G".into(), ..accounts::Grant::owner() };
    runtime.prefs.insert("CinemaTrailersPrerollID".into(), json!("/cinema/501.mp4"));
    // A clip inherits its actual parent's rating restrictions, not its own G rating.
    assert_eq!(post(&mut runtime, &query("1")).0, 409);
    runtime.prefs.insert("CinemaTrailersPrerollID".into(), json!(""));
    runtime.prefs.insert("CinemaTrailersFromLibrary".into(), json!(true));
    assert_eq!(keys(&post(&mut runtime, &query("5")).1), ["201", "10"]);
    assert!(preroll_paths("a,b;c", 0).is_err());
    assert!(preroll_paths("a;", 0).is_err());
    drop(runtime);
    fs::remove_dir_all(dir).unwrap();
}
