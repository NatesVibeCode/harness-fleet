-- Durable, content-addressed profile revisions shared by every fleet.
--
-- `profile_kind` is deliberately an open TEXT with no CHECK. The engine does not
-- enumerate the objects a product may be about: a kind is validated as a slug and
-- decoded by the contract registered for it (harness_fleet/profiles.py). A new
-- object — a supplier, an investor, a candidate — is registered, not added to a
-- list here and in three other places.
CREATE TABLE IF NOT EXISTS profile_revisions (
    revision_id TEXT PRIMARY KEY,
    profile_kind TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_profiles (
    profile_kind TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES profile_revisions(revision_id),
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_revisions_kind
    ON profile_revisions(profile_kind, created_at);
