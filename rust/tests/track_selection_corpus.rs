//! Which audio and subtitle tracks a Part starts on, against what a real PMS selected
//! for the account in `tests/fixtures/track_selection/real_pms_1422.json`: 1,500 Parts
//! of a real library, the account on `autoSelectAudio` English and subtitles always on
//! in English. Before `chosen_tracks`, rex kept ffprobe's first audio track and no
//! subtitle on every Part, which this corpus disagrees with hundreds of times.
use rex::mediainfo::{chosen_tracks, TrackPrefs};
use serde_json::Value;

fn corpus() -> (TrackPrefs, Vec<Vec<Value>>) {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/fixtures/track_selection/real_pms_1422.json");
    let doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let account = &doc["account"];
    let prefs = TrackPrefs {
        auto_audio: account["autoSelectAudio"].as_bool().unwrap(),
        audio: account["defaultAudioLanguage"].as_str().map(str::to_string),
        subtitles: account["autoSelectSubtitle"].as_i64().unwrap(),
        subtitle: account["defaultSubtitleLanguage"].as_str().map(str::to_string),
    };
    let parts = doc["parts"].as_array().unwrap().iter().map(|part| part.as_array().unwrap().clone()).collect();
    (prefs, parts)
}

fn selected(part: &[Value], kind: i64) -> Option<i64> {
    part.iter()
        .find(|stream| stream["streamType"].as_i64() == Some(kind) && stream["selected"].as_bool() == Some(true))
        .and_then(|stream| stream["id"].as_i64())
}

fn first(part: &[Value], kind: i64) -> Option<i64> {
    part.iter().find(|stream| stream["streamType"].as_i64() == Some(kind)).and_then(|stream| stream["id"].as_i64())
}

#[test]
fn the_account_settings_pick_what_a_real_pms_picked() {
    let (prefs, parts) = corpus();
    let mut audio_misses = Vec::new();
    let mut subtitle_misses = Vec::new();
    let mut old_misses = 0;
    for (index, part) in parts.iter().enumerate() {
        let (audio, subtitle) = chosen_tracks(part, &prefs);
        if audio != selected(part, 2) {
            audio_misses.push(index);
        }
        if subtitle != selected(part, 3) {
            subtitle_misses.push(index);
        }
        if first(part, 2) != selected(part, 2) || selected(part, 3).is_some() {
            old_misses += 1;
        }
    }
    // The two Parts that real PMS answered differently from its own rule elsewhere:
    // each is the second version of a two-version title.
    let known: Vec<usize> = parts
        .iter()
        .enumerate()
        .filter(|(_, part)| {
            let titles: Vec<&str> = part.iter().filter_map(|stream| stream["title"].as_str()).collect();
            titles.contains(&"English-FORCED-PGS") || titles.contains(&"English forced")
        })
        .map(|(index, _)| index)
        .collect();
    assert!(audio_misses.is_empty(), "audio differs from the real PMS on Parts {audio_misses:?}");
    assert!(
        subtitle_misses.iter().all(|index| known.contains(index)) && subtitle_misses.len() <= 2,
        "subtitles differ from the real PMS on Parts {subtitle_misses:?} (known: {known:?})"
    );
    assert!(old_misses > 1000, "the old first-audio, no-subtitle rule should miss most of the corpus, missed {old_misses}");
}

#[test]
fn a_track_chosen_by_hand_is_kept() {
    let (prefs, parts) = corpus();
    let part = parts.iter().find(|part| first(part, 2) != selected(part, 2)).unwrap();
    let mut row = serde_json::json!({"_trackChosen": true, "Stream": part});
    for stream in row["Stream"].as_array_mut().unwrap() {
        stream.as_object_mut().unwrap().remove("selected");
    }
    assert!(!rex::mediainfo::select_tracks(&mut row, &prefs));
    row.as_object_mut().unwrap().remove("_trackChosen");
    assert!(rex::mediainfo::select_tracks(&mut row, &prefs));
    assert_eq!(selected(row["Stream"].as_array().unwrap(), 2), selected(part, 2));
}
