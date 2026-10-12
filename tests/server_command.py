"""Select the server executable used by black-box test processes.

The default is the native Rust server, `rust/target/release/rex` (build it with
`cargo build --release --manifest-path rust/Cargo.toml`). `REX_SERVER=python`
selects the Python server, which stays in the tree as the measured oracle the
native port is compared against. Any other value is an executable command rather
than only a path, so a binary can be selected with fixed arguments while retaining
the test's own server arguments. Shell quoting is interpreted once with
:func:`shlex.split`; no shell is introduced for the child process.
"""

import os
import shlex
import sys


def server_command(*args, root=None):
    """Return a subprocess argv for a test-owned rex server.

    With no environment override this is the native release binary under
    ``root``; ``REX_SERVER=python`` is the Python oracle. Any other override
    may contain a quoted executable path and additional fixed arguments, for
    example ``'/tmp/rust build/rex' --serve``. ``--no-state`` is added unless
    the arguments name ``--state`` or ``--no-state`` themselves.
    """

    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    configured = os.environ.get("REX_SERVER")
    if configured is None:
        native = os.path.join(root, "rust", "target", "release", "rex.exe" if os.name == "nt" else "rex")
        if not os.path.exists(native):
            raise FileNotFoundError(
                "%s is not built: run `cargo build --release --manifest-path "
                "rust/Cargo.toml`, or set REX_SERVER=python for the Python oracle"
                % native)
        command = [native]
    elif configured.strip() == "python":
        command = [sys.executable, os.path.join(root, "server.py")]
    else:
        try:
            command = shlex.split(configured)
        except ValueError as error:
            raise ValueError("REX_SERVER is not valid shell syntax: %s" % error) from error
        if not command:
            raise ValueError("REX_SERVER must name an executable command")

    args = [str(arg) for arg in args]
    # A state file is the server's default (`rex.db` beside the catalog), and the
    # suites rely on a restart reloading the fixture: a test server keeps nothing
    # unless the test asks for a state file itself.
    if not any(arg in ("--state", "--no-state") or arg.startswith("--state=") for arg in args):
        args.append("--no-state")
    return command + args
