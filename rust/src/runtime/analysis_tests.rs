use super::*;

fn input() -> Input {
    Input { key: "11".into(), part_id: "41".into(), url: "http://127.0.0.1/source".into(), auth: Some("Bearer private".into()), duration_ms: 20000, audio_index: None, video_index: Some(0), chapters: vec![json!({"index":1,"startTimeOffset":0,"endTimeOffset":20000})], group: Some("10".into()) }
}

#[test]
fn source_part_fingerprint_ignores_our_cache_but_not_source_changes() {
    let input = input();
    let mut media = json!({"id":31,"_sid":"source-version-one","Part":[{"id":41,"file":"/source/a","size":400}]});
    let original = fingerprint(&input,&media);
    media["_analysis"] = json!({"41":{"fingerprint":"previous"}});
    media["Part"][0]["indexes"] = json!("sd");
    assert_eq!(original,fingerprint(&input,&media));
    media["Part"][0]["size"] = json!(401);
    assert_ne!(original,fingerprint(&input,&media));
    media["Part"][0]["size"] = json!(400);
    let mut changed = input.clone(); changed.url.push_str("?revision=2");
    assert_ne!(original,fingerprint(&changed,&media));
    changed = input.clone(); changed.auth = Some("Bearer replaced".into());
    assert_ne!(original,fingerprint(&changed,&media));
    changed = input.clone(); changed.chapters[0]["endTimeOffset"] = json!(19000);
    assert_ne!(original,fingerprint(&changed,&media));
    changed = input; changed.group = Some("different-authoritative-season".into());
    assert_ne!(original,fingerprint(&changed,&media));
}

#[test]
#[ignore = "generates real media with explicitly configured FFmpeg"]
fn generated_jpeg_restore_checks_hash_containment_corruption_and_removal() {
    let jobs = Jobs::new(None);
    let directory = jobs.root.join("v1-test");
    std::fs::create_dir_all(&directory).unwrap();
    let path = directory.join("chapter-1.jpg");
    let ffmpeg = std::env::var_os("REX_TRANSCODER").unwrap_or_else(|| "ffmpeg".into());
    assert!(std::process::Command::new(ffmpeg).args(["-v","error","-nostdin","-f","lavfi","-i","testsrc2=size=320x180:duration=1","-frames:v","1","-threads","1"]).arg(&path).status().unwrap().success());
    let artifact = json!({"path":path,"sha256":hash_file(&path).unwrap()});
    assert!(!jobs.known(&artifact));
    assert_eq!(jobs.resolve(&artifact),Some(std::fs::canonicalize(&path).unwrap()));
    assert!(jobs.known(&artifact));
    let restored = Jobs::new(Some(&jobs.root.with_extension("state")));
    // A different server's cache cannot read this server's artifact.
    assert!(restored.resolve(&artifact).is_none());
    std::fs::write(&path,b"not a JPEG").unwrap();
    assert!(!jobs.known(&artifact));
    assert!(jobs.resolve(&artifact).is_none());
    let corrupt = json!({"path":path,"sha256":hash_file(&path).unwrap()});
    assert!(jobs.resolve(&corrupt).is_none());
    std::fs::remove_file(path).unwrap();
    assert!(jobs.resolve(&artifact).is_none());
}

#[test]
fn persistent_cache_is_derived_from_state_and_ephemeral_cache_is_removed() {
    let state = std::env::temp_dir().join("rex-analysis-persistence-check.db");
    let first = Jobs::new(Some(&state)); let second = Jobs::new(Some(&state));
    assert_eq!(first.root,second.root);
    assert!(first.root.as_os_str().to_string_lossy().ends_with(".db.analysis"));
    let temporary = Jobs::new(None); let root = temporary.root.clone();
    std::fs::create_dir_all(&root).unwrap(); drop(temporary);
    assert!(!root.exists());
}

#[test]
fn season_marker_cache_is_target_independent_and_invalidated_by_any_dependency() {
    let dependencies = vec![("episode-one".into(),"part-one".into(),"source-one".into()),
                            ("episode-two".into(),"part-two".into(),"source-two".into())];
    let group = dependency_group(&dependencies,None);
    let reversed = dependencies.iter().rev().cloned().collect::<Vec<_>>();
    assert_eq!(group,dependency_group(&reversed,None));
    let first = json!({"version":1,"marker_group":group,"markers":[]});
    let second = first.clone();
    // Empty but successful analyses are reusable too; section Analyze must not
    // decode the complete season once again for every target's image job.
    assert!(reusable_markers(&first,&dependency_group(&dependencies,None)));
    assert!(reusable_markers(&second,&dependency_group(&reversed,None)));
    let mut changed = dependencies.clone(); changed[1].2 = "source-two-replaced".into();
    assert!(!reusable_markers(&first,&dependency_group(&changed,None)));
    assert!(!reusable_markers(&second,&dependency_group(&changed,None)));
    assert!(!reusable_markers(&first,&dependency_group(&dependencies,Some([true,false]))));
}

#[test]
fn source_invalidation_resolves_legacy_mount_and_preserves_other_provider_version() {
    let fixture = Jobs::new(None);
    std::fs::create_dir_all(&fixture.root).unwrap();
    let catalog = fixture.root.join("catalog.json");
    let first = json!({"version":1,"fingerprint":"one","markers":[],"chapters":{}});
    let second = json!({"version":1,"fingerprint":"two","markers":[],"chapters":{}});
    std::fs::write(&catalog,serde_json::to_vec(&json!({
        "sections":[{"key":"1","type":"movie","title":"Source placed"}],
        "items":[{"ratingKey":"1","type":"movie","librarySectionID":1,"Media":[
            {"id":1,"Part":[{"id":11,"file":"/first/one.mkv"}],"_analysis":{"11":first}},
            {"id":2,"_source":"second","_sid":"two","_path":"two.mkv","Part":[{"id":22,"file":"/second/two.mkv"}],"_analysis":{"22":second}}
        ]}]
    })).unwrap()).unwrap();
    let mut runtime = Runtime::load(RuntimeOptions { catalog, state:None, assert_claimed:true, ..RuntimeOptions::default() }).unwrap();
    runtime.sources = vec![
        Arc::new(source::Source::new("first","http://127.0.0.1:1/first",None,Some("/first"))),
        Arc::new(source::Source::new("second","http://127.0.0.1:2/second",None,Some("/second")))
    ];
    runtime.invalidate_source_analysis("first");
    let row = runtime.catalog.get("1").unwrap();
    assert!(row.pointer("/Media/0/_analysis/11").is_none());
    assert_eq!(row.pointer("/Media/1/_analysis/22"),Some(&second));
    runtime.invalidate_source_analysis("second");
    assert!(runtime.catalog.get("1").unwrap().pointer("/Media/1/_analysis/22").is_none());
}
