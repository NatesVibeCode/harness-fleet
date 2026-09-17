-- Durable, content-addressed profile revisions shared by every fleet.
--
-- Every onboarding kind is first-class here. A partner profile used to have no
-- kind of its own, so it was flattened into an ICP to be stored at all, and the
-- fields its own lane's questions need — the target ecosystem, the service
-- models, the delivery roles — were dropped on the way in.
CREATE TABLE IF NOT EXISTS profile_revisions (
    revision_id TEXT PRIMARY KEY,
    profile_kind TEXT NOT NULL CHECK(profile_kind IN ('ideal_company', 'ideal_partner', 'ideal_employer')),
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_profiles (
    profile_kind TEXT PRIMARY KEY CHECK(profile_kind IN ('ideal_company', 'ideal_partner', 'ideal_employer')),
    revision_id TEXT NOT NULL REFERENCES profile_revisions(revision_id),
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_revisions_kind
    ON profile_revisions(profile_kind, created_at);
