"""What this distribution is called: the only product-specific file in the engine.

Every other module in ``harness_fleet`` is shared byte for byte across the
public distributions. One surface, built once and branded at the edges, so a
fix or a connector lands everywhere at the same time instead of being ported.
A repo edits this file and nothing else.
"""
from __future__ import annotations

#: The command this distribution installs.
CLI_NAME = "harness-fleet"
#: The distribution name used in install hints (``pip install <DIST_NAME>[js]``).
DIST_NAME = "harness-fleet"
#: Entry-point names this CLI may run under, most preferred first. The running
#: binary's own name is always preferred, so two fleets in one environment
#: still report themselves correctly.
CLI_NAMES = ("harness-fleet", "account-fleet", "career-fleet")
#: Pre-rename entry points that still resolve to this distribution.
LEGACY_CLI_NAMES = ("career-lanes",)
#: The SQLite control plane this CLI opens when no path is given.
DEFAULT_DB = "harness-fleet.db"
#: The product noun used in the CLI's own help and errors.
PROFILE_NOUN = "Ideal Company Profile"
#: The bundled engine skill's directory name.
SKILL_NAME = "harness-fleet"
#: Project home, used in the discover user agent.
HOMEPAGE = "https://github.com/NatesVibeCode/harness-fleet"
