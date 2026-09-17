-- Widen the profile-kind constraint for databases created before a partner
-- profile had a kind of its own.
--
-- SQLite cannot alter a CHECK constraint, so the tables are rebuilt and their
-- rows carried across. The rebuild is applied only when the existing DDL does
-- not already name `ideal_partner` (see HarnessStore.migrate), so a fresh
-- database — built from the widened 003 — never pays for it and no migration
-- rewrites the tables on every command.
PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS profile_revisions_v2 (
    revision_id TEXT PRIMARY KEY,
    profile_kind TEXT NOT NULL CHECK(profile_kind IN ('ideal_company', 'ideal_partner', 'ideal_employer')),
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT OR IGNORE INTO profile_revisions_v2(
    revision_id, profile_kind, profile_name, profile_version, profile_json, created_at
)
SELECT revision_id, profile_kind, profile_name, profile_version, profile_json, created_at
FROM profile_revisions;

CREATE TABLE IF NOT EXISTS active_profiles_v2 (
    profile_kind TEXT PRIMARY KEY CHECK(profile_kind IN ('ideal_company', 'ideal_partner', 'ideal_employer')),
    revision_id TEXT NOT NULL REFERENCES profile_revisions(revision_id),
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO active_profiles_v2(profile_kind, revision_id, updated_at)
SELECT profile_kind, revision_id, updated_at FROM active_profiles;

DROP TABLE IF EXISTS active_profiles;
DROP TABLE IF EXISTS profile_revisions;

ALTER TABLE profile_revisions_v2 RENAME TO profile_revisions;
ALTER TABLE active_profiles_v2 RENAME TO active_profiles;

CREATE INDEX IF NOT EXISTS idx_profile_revisions_kind
    ON profile_revisions(profile_kind, created_at);

PRAGMA foreign_keys=ON;
