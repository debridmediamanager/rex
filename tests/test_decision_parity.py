#!/usr/bin/env python3
"""Compare native decision output with the Python oracle's recorded answers on semantic probes.

The probes exercise JSON number whitespace and streamType typing directly. They are
not claimed PMS measurements and are intentionally kept out of the measured corpus. Each
expected (outcome, text) is what `oracle/decision.py`'s `decide("Android", media)` answered,
recorded from the oracle before it was removed.
"""

import json
import os
import shlex
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def native_command():
    configured = os.environ.get(
        "REX_NATIVE_BIN", os.path.join(ROOT, "rust", "target", "debug", "rex")
    )
    command = shlex.split(configured)
    if not command:
        raise AssertionError("REX_NATIVE_BIN must name the native binary")
    return command


def run_native(command, media_path):
    result = subprocess.run(
        command
        + [
            "decision",
            "--profile",
            "Android",
            "--media",
            media_path,
            "--profiles",
            os.path.join(ROOT, "data", "client_profiles.json"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def main():
    # (media, the oracle's recorded outcome and text)
    probes = [
        ({
            "container": "mp4",
            "videoCodec": "h264",
            "Part": [
                {
                    "Stream": [
                        {"streamType": 1, "codec": "h264"},
                        {"streamType": 2, "codec": "ac3", "selected": True},
                    ]
                }
            ],
        }, ("neither", "App cannot direct play this item. No direct play video profile exists "
                       "for protocol http, with container mp4, video codec h264, and audio codec ac3.")),
        ({
            "container": "mp4",
            "videoCodec": "h264",
            "Part": [
                {
                    "Stream": [
                        {"streamType": 1, "codec": "h264"},
                        {"streamType": "2", "codec": "ac3", "selected": True},
                    ]
                }
            ],
        }, ("neither", "This app cannot play this item. The reason is: "
                       "part.optimizedForStreaming limitation applies: 0 != 1.")),
    ]
    command = native_command()
    with tempfile.TemporaryDirectory(prefix="rex-decision-parity-") as directory:
        for media, (expected_outcome, expected_text) in probes:
            path = os.path.join(directory, "media.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(media, handle)
            actual = run_native(command, path)
            assert actual["outcome"] == expected_outcome, (media, actual, expected_outcome)
            assert actual["text"] == expected_text, (media, actual, expected_text)
    print("PASS native decision matches the oracle's recorded answers for numeric and string streamType")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
