use std::env;
use std::fs;
use std::process::ExitCode;

use plexsim::decision;
use serde_json::json;

fn usage() -> &'static str {
    "Usage: plexsim decision --profile NAME --media FILE [--profiles FILE] [--protocol PROTOCOL] [--no-direct]"
}

#[derive(Debug, PartialEq, Eq)]
struct DecisionOptions {
    profile: String,
    media_path: String,
    profiles_path: String,
    protocol: String,
    direct: bool,
}

fn option_value(args: &[String], index: &mut usize, name: &str) -> Result<String, String> {
    let value = args
        .get(*index + 1)
        .ok_or_else(|| format!("{name} needs a value"))?;
    if value.starts_with('-') {
        return Err(format!("{name} needs a value"));
    }
    *index += 2;
    Ok(value.clone())
}

fn parse_decision_args(args: &[String]) -> Result<DecisionOptions, String> {
    let mut profile = None;
    let mut media_path = None;
    let mut profiles_path = None;
    let mut protocol = None;
    let mut direct = true;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--profile" => {
                if profile.is_some() {
                    return Err("option --profile was supplied more than once".to_string());
                }
                profile = Some(option_value(args, &mut index, "--profile")?);
            }
            "--media" => {
                if media_path.is_some() {
                    return Err("option --media was supplied more than once".to_string());
                }
                media_path = Some(option_value(args, &mut index, "--media")?);
            }
            "--profiles" => {
                if profiles_path.is_some() {
                    return Err("option --profiles was supplied more than once".to_string());
                }
                profiles_path = Some(option_value(args, &mut index, "--profiles")?);
            }
            "--protocol" => {
                if protocol.is_some() {
                    return Err("option --protocol was supplied more than once".to_string());
                }
                protocol = Some(option_value(args, &mut index, "--protocol")?);
            }
            "--no-direct" => {
                if !direct {
                    return Err("option --no-direct was supplied more than once".to_string());
                }
                direct = false;
                index += 1;
            }
            option if option.starts_with('-') => {
                return Err(format!("unknown option {option}"));
            }
            value => return Err(format!("unexpected argument {value}")),
        }
    }

    Ok(DecisionOptions {
        profile: profile.ok_or_else(|| "--profile is required".to_string())?,
        media_path: media_path.ok_or_else(|| "--media is required".to_string())?,
        profiles_path: profiles_path.unwrap_or_else(|| "client_profiles.json".to_string()),
        protocol: protocol.unwrap_or_else(|| "http".to_string()),
        direct,
    })
}

fn run_decision(args: &[String]) -> Result<(), String> {
    let options = parse_decision_args(args)?;

    let profiles_text = fs::read_to_string(&options.profiles_path)
        .map_err(|error| format!("cannot read profiles {}: {error}", options.profiles_path))?;
    let profiles = decision::parse_profiles(&profiles_text)
        .map_err(|error| format!("cannot parse profiles {}: {error}", options.profiles_path))?;
    let media_text = fs::read_to_string(&options.media_path)
        .map_err(|error| format!("cannot read media {}: {error}", options.media_path))?;
    let media: serde_json::Value = serde_json::from_str(&media_text)
        .map_err(|error| format!("cannot parse media {}: {error}", options.media_path))?;
    let answer = decision::decide(
        &profiles,
        &options.profile,
        &media,
        &options.protocol,
        options.direct,
    );
    let output = json!({
        "outcome": answer.outcome.as_str(),
        "text": answer.text,
    });
    println!(
        "{}",
        serde_json::to_string(&output).expect("decision JSON is serializable")
    );
    Ok(())
}

fn main() -> ExitCode {
    let mut args = env::args().skip(1).collect::<Vec<_>>();
    if args.is_empty() || args[0] == "--help" || args[0] == "-h" {
        println!("{}", usage());
        return ExitCode::SUCCESS;
    }
    let command = args.remove(0);
    let result = match command.as_str() {
        "decision" => run_decision(&args),
        _ => Err(format!("unknown command {command}\n{}", usage())),
    };
    if let Err(error) = result {
        eprintln!("plexsim: {error}");
        ExitCode::from(2)
    } else {
        ExitCode::SUCCESS
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    #[test]
    fn parses_options_sequentially() {
        let parsed = parse_decision_args(&args(&[
            "--profile",
            "Web",
            "--media",
            "media file.json",
            "--profiles",
            "profiles.json",
            "--protocol",
            "https",
            "--no-direct",
        ]))
        .expect("valid options parse");
        assert_eq!(
            parsed,
            DecisionOptions {
                profile: "Web".to_string(),
                media_path: "media file.json".to_string(),
                profiles_path: "profiles.json".to_string(),
                protocol: "https".to_string(),
                direct: false,
            }
        );
    }

    #[test]
    fn rejects_unconsumed_and_malformed_options() {
        let cases = [
            (
                &["--profile", "Web", "--media", "media.json", "stray"][..],
                "unexpected argument stray",
            ),
            (
                &["--profile", "Web", "--media"][..],
                "--media needs a value",
            ),
            (
                &["--profile", "Web", "--media", "media.json", "--bogus"][..],
                "unknown option --bogus",
            ),
            (
                &[
                    "--profile",
                    "Web",
                    "--media",
                    "media.json",
                    "--profile",
                    "Other",
                ][..],
                "option --profile was supplied more than once",
            ),
            (
                &[
                    "--profile",
                    "Web",
                    "--media",
                    "media.json",
                    "--no-direct",
                    "--no-direct",
                ][..],
                "option --no-direct was supplied more than once",
            ),
        ];
        for (values, expected) in cases {
            assert_eq!(
                parse_decision_args(&args(values)),
                Err(expected.to_string())
            );
        }
    }
}
