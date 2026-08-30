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

# Point the library at a config file that cannot exist, for the whole test
# session. Without this the tests read the developer's own
# ~/.config/check-uoa-mail/config.ini: results would differ between a laptop
# and CI, and a test exercising the "nothing configured" path would instead
# pick up the real assistant CLI and go on to spawn it.
U.CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "no-such-config.ini")


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
