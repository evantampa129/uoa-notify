"""Import helper shared by the test modules.

The scripts live in ``bin/`` and are executables rather than an installed
package, so there is no import path to rely on. Each test module imports this
first, which puts ``bin/`` on ``sys.path`` and re-exports the library under a
short name.
"""

import os
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import uoa_common as U  # noqa: E402  (path must be set before this import)


def default_config():
    """A config built only from the in-code defaults.

    ``U.load_config()`` layers the user's real ``config.ini`` on top of the
    defaults. That file exists on a developer machine and not in CI, so tests
    that relied on it would pass locally and fail in the runner. Building the
    parser straight from ``U.DEFAULTS`` keeps every run identical.
    """
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read_dict(U.DEFAULTS)
    return cfg
