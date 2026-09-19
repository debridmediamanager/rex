#!/usr/bin/env python3
"""Exact regression for the measured regional `/:/prefs` catalogues."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PLEXSIM_TEST_ROOT", os.path.dirname(HERE))
CATALOGUE = os.environ.get("PLEXSIM_REGIONAL_PREFS_FIXTURE",
                           os.path.join(ROOT, "regional_server_prefs.json"))
BASE_PREFS = os.path.join(ROOT, "server_prefs.json")
VERSION_142_OMITS = {"AllowLegacyAgents", "CLDriver"}


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def fetch(port, language):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        # X-Plex-Language is the only request identity.  In particular, this test
        # carries no Product, Platform, Device, Device-Name or Client-Identifier.
        conn.request("GET", "/:/prefs", headers={"Accept": "application/json",
                                                  "Accept-Encoding": "identity",
                                                  "X-Plex-Language": language})
        response = conn.getresponse()
        body = response.read()
    finally:
        conn.close()
    return response.status, json.loads(body)


def main():
    fixture = json.load(open(CATALOGUE, encoding="utf-8"))
    catalogues = fixture["catalogues"]
    aliases = fixture["aliases"]
    base = json.load(open(BASE_PREFS, encoding="utf-8"))
    base_order = [row["id"] for row in base]
    if (set(catalogues) != {"pt-BR", "hu-HU"}
            or aliases != {"pt-PT": "pt-BR"}
            or any(len(rows) != 174 for rows in catalogues.values())
            or any([row["id"] for row in rows] != base_order
                   for rows in catalogues.values())):
        print("FAIL regional fixture is not two complete ordered 174-row catalogues")
        return 1

    expected_order = [identity for identity in base_order
                      if identity not in VERSION_142_OMITS]
    base_by_id = {row["id"]: row for row in base}
    languages = ("pt-BR", "pt-PT", "hu-HU")
    port = free_port()
    env = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), "--port", str(port),
         "--catalog", os.path.join(ROOT, "tests", "catalog_ui.json"), "--gdm", "off"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                probe = socket.create_connection(("127.0.0.1", port), timeout=1)
                probe.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            print("FAIL plexsim did not start")
            return 1

        failures = []
        cells = 0
        documents = {}
        for language in languages:
            status, document = fetch(port, language)
            documents[language] = document
            body = document.get("MediaContainer") or {}
            rows = body.get("Setting") or []
            wanted_language = aliases.get(language, language)
            wanted = {row["id"]: row for row in catalogues[wanted_language]}
            if status != 200:
                failures.append((language, "status", status, 200))
                continue
            if list(body) != ["size", "Setting"] or body.get("size") != 172:
                failures.append((language, "root", list(body), body.get("size")))
            if [row.get("id") for row in rows] != expected_order:
                failures.append((language, "row-order", len(rows), len(expected_order)))
            for row in rows:
                identity = row.get("id")
                if list(row) != list(base_by_id[identity]):
                    failures.append((language, identity, "attribute-order",
                                     list(row), list(base_by_id[identity])))
                for attribute in ("label", "summary", "enumValues"):
                    if attribute not in wanted[identity]:
                        continue
                    cells += 1
                    if row.get(attribute) != wanted[identity][attribute]:
                        failures.append((language, identity, attribute,
                                         row.get(attribute), wanted[identity][attribute]))

        if documents.get("pt-BR") != documents.get("pt-PT"):
            failures.append(("pt-BR/pt-PT", "documents differ"))
        if cells != 1122:
            failures.append(("denominator", cells, 1122))
        if failures:
            print("FAIL %d regional prefs differences across %d exact cells"
                  % (len(failures), cells))
            for failure in failures[:12]:
                print("  %r" % (failure,))
            return 1
        print("PASS 3/3 regional prefs documents: 174 captured rows each, "
              "172 version-1.42 rows served, 1,122 exact localisable cells")
        return 0
    finally:
        proc.kill()
        proc.wait()


if __name__ == "__main__":
    sys.exit(main())
