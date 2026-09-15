# Versioning: stop the churn

## The problem, stated plainly

`harness-fleet` is weeks old and carries **13 tags** (v0.2.0 … v0.5.0, patch
releases between them). Every tag is a public claim that "this build is worth
installing", and every tag we cut for a handful of internal changes makes the
project look like it ships constantly and breaks constantly — which is exactly
the criticism that started this: too many releases, each one buggy.

Semver churn is not a signal of progress. It is a liability we invented for
ourselves, and it costs real work every time: version strings to keep in sync
across three distributions, a tag to cut, a release to write, artifacts to
attach — all before anyone has asked for a build.

## Policy from here (forward)

1. **No version bump per change.** The version string stays put. It is an
   identity, not a progress bar.
2. **No tag without something to hand someone.** A release is cut only when
   (a) the public repo is ready, (b) a clean-venv `pip install` of the published
   artifact is verified, and (c) the install path is documented. Until then the
   work lives on commits, not releases.
3. **One deliberate milestone, not a ladder.** The next version is whatever we
   decide that milestone is — likely `1.0.0` when the three products (plus
   Partner Finder) are installable and proven, not `0.5.1`, `0.6.0`, `0.6.1`.
4. **Builds identify themselves without versions.** `--version` should report
   the identity *plus the commit* when running from a checkout (PEP 440 local
   version, e.g. `0.5.0+g2144863`), so a bug report is pinpointable without
   bumping anything. That replaces most of what version numbers were doing.
5. **No release notes for releases nobody asked for.** Changelogs are written
   when a release is cut, from the commits that earned it.

## What this does *not* change

- `pyproject.toml` and `__init__.py` still carry a valid PEP 440 version; a
  package must have one. It simply stops moving on its own.
- Tags already published stay valid history. See below.

## Open question (yours to call): the tags already public

There are 13 tags on `origin` for harness, 12 for career, plus matching GitHub
releases (the newest, `harness-fleet 0.5.0`, is marked "Latest"). Three options:

| Option | Effect |
| --- | --- |
| **Keep them, stop adding** (default) | History stays honest; the list still reads as churn to a newcomer |
| **Delete all pre-1.0 tags and releases** | Clean slate; removes the "so many releases" surface entirely, but rewrites nothing and loses nothing technically (the commits remain) |
| **Keep only the newest, delete the rest** | One installable "current" release, no ladder behind it |

Destructive and public, so it waits for an explicit instruction — and needs the
same treatment in all three repos, plus PyPI if anything was ever published
there (it was not; publishing needs a token neither repo has).

## Status

| Item | State |
| --- | --- |
| Policy recorded | yes (this file) |
| Version bumps stopped | yes — from here, no bump without a milestone |
| `--version` reports the commit in a checkout | not done |
| Existing tags/releases cleanup | awaiting your call |
