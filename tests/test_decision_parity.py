#!/usr/bin/env python3
"""Compare native decision output with the Python oracle on semantic probes.

The probes exercise JSON number whitespace and streamType typing directly. They are
not claimed PMS measurements and are intentionally kept out of the measured corpus.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import decision  # noqa: E402


def native_command():
    configured = os.environ.get(
        "PLEXSIM_NATIVE_BIN", os.path.join(ROOT, "rust", "target", "debug", "plexsim")
    )
    command = shlex.split(configured)
    if not command:
        raise AssertionError("PLEXSIM_NATIVE_BIN must name the native binary")
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
            os.path.join(ROOT, "client_profiles.json"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def main():
    probes = [
        {
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
        },
        {
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
        },
    ]
    command = native_command()
    with tempfile.TemporaryDirectory(prefix="plexsim-decision-parity-") as directory:
        for media in probes:
            expected_outcome, expected_text = decision.decide("Android", media)
            path = os.path.join(directory, "media.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(media, handle)
            actual = run_native(command, path)
            assert actual["outcome"] == expected_outcome, (media, actual, expected_outcome)
            assert actual["text"] == expected_text, (media, actual, expected_text)
    print("PASS native/Python semantic decision parity for numeric and string streamType")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
