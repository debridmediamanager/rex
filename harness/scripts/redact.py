#!/usr/bin/env python3
"""Reduce a captured corpus to a checked-in fixture.

Keeps only the server-directed exchanges -- the ones plexsim has to answer --
and removes every credential. Tokens appear in three places and all three matter:
the X-Plex-Token query parameter, the X-Plex-Token header, and inside response
bodies, where a server hands out accessToken and similar per-user values.
"""
import argparse, json, re, sys

TOKEN_KEYS = {"x-plex-token"}
BODY_PATTERNS = [
    (re.compile(r'((?:accessToken|authToken|token|X-Plex-Token)="?)([A-Za-z0-9_\-]{15,})'), r"\1REDACTED"),
    (re.compile(r'("(?:accessToken|authToken|token)"\s*:\s*")([^"]{15,})'), r"\1REDACTED"),
]


def scrub_headers(headers):
    out = []
    for k, v in headers:
        if k.lower() in TOKEN_KEYS:
            v = "REDACTED"
        if k.lower() in ("cookie", "set-cookie", "authorization"):
            v = "REDACTED"
        out.append([k, v])
    return out


def scrub_body(body):
    if not body:
        return body
    for pat, rep in BODY_PATTERNS:
        body = pat.sub(rep, body)
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host-suffix", default=".plex.direct")
    a = ap.parse_args()

    kept = 0
    with open(a.out, "w") as fh:
        for line in open(a.corpus):
            r = json.loads(line)
            if r.get("error") or not r.get("host", "").endswith(a.host_suffix):
                continue
            if any(k.lower() == "x-plexlab-blocked" for k, _ in r.get("res_headers") or []):
                continue
            # A token also rides inside nested URLs: /photo/:/transcode carries
            # the whole upstream request, token included, in its url parameter.
            r["query"] = [[k, ("REDACTED" if k.lower() in TOKEN_KEYS else scrub_body(v))]
                          for k, v in r.get("query") or []]
            r["req_headers"] = scrub_headers(r.get("req_headers") or [])
            r["res_headers"] = scrub_headers(r.get("res_headers") or [])
            r["req_body"] = scrub_body(r.get("req_body"))
            r["res_body"] = scrub_body(r.get("res_body"))
            # The host embeds the server's certificate hash; the fixture should
            # not pin one install's identity.
            r["host"] = "reference.plex.direct"
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            kept += 1
    print(f"{kept} server exchanges -> {a.out}")


if __name__ == "__main__":
    sys.exit(main())
