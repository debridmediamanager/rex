#!/usr/bin/env python3
"""Unit checks for the black-box server executable selector."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from server_command import server_command


def main():
    old = os.environ.pop("SIMPLEX_SERVER", None)
    try:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="repo with spaces ") as repo:
            try:
                server_command("--port", 32500, root=repo)
            except FileNotFoundError as error:
                assert "cargo build" in str(error) and "SIMPLEX_SERVER=python" in str(error)
            else:
                raise AssertionError("an unbuilt native default must fail clearly")
            native = os.path.join(repo, "rust", "target", "release", "simplex")
            os.makedirs(os.path.dirname(native))
            open(native, "w").close()
            default = server_command("--port", 32500, root=repo)
            # A state file is the server's default; a test server keeps none unless asked.
            assert default == [native, "--port", "32500", "--no-state"]
            assert server_command("--state", "s", root=repo) == [native, "--state", "s"]
            assert server_command("--state=s", root=repo) == [native, "--state=s"]
            assert server_command("--no-state", root=repo) == [native, "--no-state"]

        os.environ["SIMPLEX_SERVER"] = "python"
        oracle = server_command("--port", 32500, root="/repo with spaces")
        assert oracle == [sys.executable, "/repo with spaces/server.py", "--port", "32500",
                          "--no-state"]

        os.environ["SIMPLEX_SERVER"] = '"/tmp/native build/simplex" --profile "test mode"'
        selected = server_command("--port", 32501, "--catalog", "/tmp/catalog.json",
                                  root="/unused")
        assert selected == ["/tmp/native build/simplex", "--profile", "test mode",
                            "--port", "32501", "--catalog", "/tmp/catalog.json", "--no-state"]

        os.environ["SIMPLEX_SERVER"] = "   "
        try:
            server_command("--port", 32502)
        except ValueError as error:
            assert "must name" in str(error)
        else:
            raise AssertionError("an empty SIMPLEX_SERVER must fail clearly")

        os.environ["SIMPLEX_SERVER"] = '"unterminated'
        try:
            server_command("--port", 32503)
        except ValueError as error:
            assert "valid shell syntax" in str(error)
        else:
            raise AssertionError("malformed SIMPLEX_SERVER must fail clearly")
    finally:
        if old is None:
            os.environ.pop("SIMPLEX_SERVER", None)
        else:
            os.environ["SIMPLEX_SERVER"] = old

    print("PASS native default, python oracle and quoted executable override")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
