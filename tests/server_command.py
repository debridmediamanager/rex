"""Select the server executable used by black-box test processes.

The default is the historical Python invocation.  ``PLEXSIM_SERVER`` is an
executable command, rather than only a path, so a Rust binary can be selected
with fixed arguments while retaining the test's own server arguments.  Shell
quoting is interpreted once with :func:`shlex.split`; no shell is introduced
for the child process.
"""

import os
import shlex
import sys


def server_command(*args, root=None):
    """Return a subprocess argv for a test-owned plexsim server.

    With no environment override this is the same ``python server.py``
    command used by the suites before native-server selection was added.  An
    override may contain a quoted executable path and additional fixed
    arguments, for example ``'/tmp/rust build/plexsim' --serve``.
    """

    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    configured = os.environ.get("PLEXSIM_SERVER")
    if configured is None:
        command = [sys.executable, os.path.join(root, "server.py")]
    else:
        try:
            command = shlex.split(configured)
        except ValueError as error:
            raise ValueError("PLEXSIM_SERVER is not valid shell syntax: %s" % error) from error
        if not command:
            raise ValueError("PLEXSIM_SERVER must name an executable command")

    return command + [str(arg) for arg in args]
