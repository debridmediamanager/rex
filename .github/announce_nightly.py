"""Post a published nightly to Discord, the way zurg's nightlies are announced in #general.

    DISCORD_WEBHOOK=... python3 .github/announce_nightly.py TAG URL COMMIT notes.md
    python3 .github/announce_nightly.py --dry-run TAG URL COMMIT notes.md

The notes are the release's own: the Docker image line, then one `- subject` line per commit.
--dry-run prints the payload.
"""
import json
import os
import sys
import urllib.request

MAX_DESCRIPTION = 4096  # Discord's embed description limit


def describe(lines):
    lines = [line for line in lines if line.strip()]
    for keep in range(len(lines), 0, -1):
        text = "\n".join(lines[:keep])
        if keep < len(lines):
            text += f"\n\n…and {len(lines) - keep} more in the release notes."
        if len(text) <= MAX_DESCRIPTION:
            return text
    return "No changes were listed for this build."


def message(tag, url, commit, notes):
    return {
        "content": "New rex nightly is out",
        "username": "rex nightlies",
        "embeds": [{
            "title": f"rex {tag}",
            "url": url,
            "description": describe(notes.splitlines()),
            "color": 0xFF9A62,
            "footer": {"text": f"nightly build · commit {commit[:7]}"},
        }],
        "allowed_mentions": {"parse": []},
    }


def main(argv):
    dry_run = argv[:1] == ["--dry-run"]
    tag, url, commit, notes_path = argv[1:] if dry_run else argv
    with open(notes_path, encoding="utf-8") as notes:
        payload = message(tag, url, commit, notes.read())
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    # From the environment, not an argument: the URL is a credential.
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        sys.exit("DISCORD_WEBHOOK is empty, so the nightly went out unannounced")
    request = urllib.request.Request(
        webhook, data=json.dumps(payload).encode(), method="POST",
        # Discord's Cloudflare refuses urllib's default User-Agent.
        headers={"Content-Type": "application/json", "User-Agent": "rex-release (github.com/debridmediamanager/rex)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        print(f"announced {tag} ({response.status})")


if __name__ == "__main__":
    main(sys.argv[1:])
