"""What this distribution is called: the only product-specific file in the engine.

Everything product-specific that is not a name — the lanes, the presets, the
skills — lives in data the engine reads. This file is the residue: what to call
the binary, the distribution, the database, and the profile noun.
"""
from __future__ import annotations

#: The command this distribution installs.
CLI_NAME = "harness-fleet"
#: The distribution name used in install hints (``pip install <DIST_NAME>[js]``).
DIST_NAME = "harness-fleet"
#: Entry-point names this CLI may run under, most preferred first. There is one
#: distribution and one script; the list exists because a version lookup falls
#: back to it when the running code cannot say its own version.
CLI_NAMES = ("harness-fleet",)
#: Pre-rename entry points a stale install may still run this code under.
LEGACY_CLI_NAMES = ("career-lanes", "account-fleet", "career-fleet")
#: The SQLite control plane this CLI opens when no path is given.
DEFAULT_DB = "harness-fleet.db"
#: The product noun used in the CLI's own help and errors.
PROFILE_NOUN = "Ideal Company Profile"
#: The bundled engine skill's directory name.
SKILL_NAME = "harness-fleet"
#: Project home, used in the discover user agent.
HOMEPAGE = "https://github.com/NatesVibeCode/harness-fleet"
