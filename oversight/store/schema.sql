-- Jira Oversight snapshot schema (SQLite). Timestamps are UTC text, so text order is time order.

-- Raw, as synced from Jira --------------------------------------------------

CREATE TABLE IF NOT EXISTS issues (
  issue_key              TEXT PRIMARY KEY,
  issue_id               TEXT NOT NULL,
  project_key            TEXT NOT NULL,
  issue_type             TEXT,
  summary                TEXT,
  status                 TEXT,
  status_category        TEXT,      -- new | indeterminate | done
  assignee_id            TEXT,
  parent_key             TEXT,      -- added: direct parent (an epic for tasks, a task for subtasks)
  epic_key               TEXT,
  created_at             TEXT,
  updated_at             TEXT,
  resolved_at            TEXT,
  original_estimate_s    INTEGER,   -- timeoriginalestimate
  remaining_estimate_s   INTEGER,   -- timeestimate
  time_spent_s           INTEGER,   -- timespent
  aggregate_time_spent_s INTEGER,   -- added: aggregatetimespent (includes subtasks)
  story_points           REAL,
  sprint_ids             TEXT,      -- JSON array
  raw_json               TEXT       -- last payload, for audit
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_issues_issue_id ON issues (issue_id);
CREATE INDEX IF NOT EXISTS ix_issues_project ON issues (project_key);

CREATE TABLE IF NOT EXISTS status_changes (
  issue_key    TEXT NOT NULL,
  changelog_id TEXT NOT NULL,
  changed_at   TEXT NOT NULL,
  author_id    TEXT,
  from_status  TEXT,
  to_status    TEXT,
  PRIMARY KEY (issue_key, changelog_id)
);
CREATE INDEX IF NOT EXISTS ix_status_changes_time ON status_changes (issue_key, changed_at);

CREATE TABLE IF NOT EXISTS field_changes (   -- estimates, points, sprint, assignee, evidence fields
  issue_key    TEXT NOT NULL,
  changelog_id TEXT NOT NULL,
  field        TEXT NOT NULL,                -- logical name, e.g. story_points
  changed_at   TEXT NOT NULL,
  from_value   TEXT,
  to_value     TEXT,
  PRIMARY KEY (issue_key, changelog_id, field)
);

CREATE TABLE IF NOT EXISTS worklogs (
  worklog_id TEXT PRIMARY KEY,
  issue_key  TEXT NOT NULL,
  started_at TEXT,
  seconds    INTEGER,
  author_id  TEXT,                -- the Tempo app account in AIL
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_worklogs_issue ON worklogs (issue_key);

CREATE TABLE IF NOT EXISTS links (               -- issue links + remote links (PRs)
  issue_key TEXT NOT NULL,
  kind      TEXT NOT NULL,        -- issue_link | remote_link
  target    TEXT NOT NULL,        -- linked issue key, or the remote URL
  title     TEXT,                 -- link direction (e.g. "blocks"), or the remote title
  PRIMARY KEY (issue_key, kind, target)
);

CREATE TABLE IF NOT EXISTS statuses (            -- added: each project's statuses and their categories
  project_key TEXT NOT NULL,
  status_id   TEXT NOT NULL,
  name        TEXT NOT NULL,
  category    TEXT,               -- new | indeterminate | done
  PRIMARY KEY (project_key, status_id)
);

CREATE TABLE IF NOT EXISTS boards (              -- every Jira project the service account can browse
  project_key    TEXT PRIMARY KEY,
  project_id     TEXT,
  name           TEXT NOT NULL,                  -- Jira's name for it
  project_type   TEXT,                           -- Jira's projectTypeKey: software | business | service_desk | ...
  discovered_at  TEXT NOT NULL,                  -- when a search first listed it
  last_seen_at   TEXT NOT NULL,                  -- when a search last listed it
  disappeared_at TEXT                            -- when a search first no longer listed it; NULL while listed
);

CREATE TABLE IF NOT EXISTS sync_state (
  scope            TEXT PRIMARY KEY, -- a project key, e.g. "AIL"
  last_synced_at   TEXT,             -- UTC; the start of the last successful sync
  last_run_id      TEXT,
  account_timezone TEXT              -- added: zone JQL dates were read in (from /myself)
);

CREATE TABLE IF NOT EXISTS people (      -- added: the accounts on the synced boards, assignees and members, by display name and Jira picture (no email)
  account_id        TEXT PRIMARY KEY,
  display_name      TEXT,
  active            INTEGER,             -- 1, 0, or NULL when Jira does not say
  updated_at        TEXT NOT NULL,       -- when a sync last saw the account
  avatar_url        TEXT,                -- Jira's 48x48 picture URL (avatarUrls); internal, never sent
  avatar            BLOB,                -- the picture the last fetch stored (oversight/ingest/avatars.py); NULL: none
  avatar_type       TEXT,                -- its media type, from its bytes: image/png, image/jpeg, image/gif or image/webp
  avatar_hash       TEXT,                -- the first 12 hex characters of its SHA-256: the version the API sends
  avatar_source     TEXT,                -- the URL it came from (NULL: drawn by the demo, no fetch)
  avatar_fetched_at TEXT                 -- when it was fetched, or last found unchanged
);

CREATE TABLE IF NOT EXISTS board_members ( -- added: each synced board's members, the people Jira lets its tickets be assigned to
  project_key TEXT NOT NULL,
  account_id  TEXT NOT NULL,             -- a people row
  seen_at     TEXT NOT NULL,             -- when the board's last complete member fetch listed the account
  PRIMARY KEY (project_key, account_id)
);
CREATE INDEX IF NOT EXISTS ix_board_members_account ON board_members (account_id);

-- Derived --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS status_intervals (
  issue_key  TEXT NOT NULL,
  seq        INTEGER NOT NULL,    -- added: 0-based position in the issue's timeline
  status     TEXT NOT NULL,
  stage      TEXT,                -- James's stage, from process.yaml
  entered_at TEXT NOT NULL,
  left_at    TEXT,                -- NULL = still in this status
  hours      REAL,                -- NULL while still in this status
  PRIMARY KEY (issue_key, seq)    -- added
);
CREATE INDEX IF NOT EXISTS ix_status_intervals_status ON status_intervals (status);

CREATE TABLE IF NOT EXISTS issue_metrics (
  issue_key       TEXT PRIMARY KEY,
  started_at      TEXT,           -- first entry into development or a later work stage
  done_at         TEXT,
  cycle_time_h    REAL,
  reopen_count    INTEGER,
  skipped_stages  TEXT,           -- JSON list
  estimate_basis  TEXT,           -- hours | points | none
  estimate_set_at TEXT,
  effort_ratio    REAL,           -- actual / estimate, NULL if no estimate
  status_age_h    REAL,           -- time in the current status
  computed_at     TEXT
);

-- Rules, AI and audit ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  kind          TEXT,             -- sync | derive | baseline | rules | digest | insights | demo (seeding) | discover
  status        TEXT,             -- added: running | succeeded | failed
  started_at    TEXT,
  finished_at   TEXT,
  rules_version TEXT,
  stats_json    TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_kind ON runs (kind, started_at);

-- Phase 3: findings and their lifecycle.
CREATE TABLE IF NOT EXISTS findings (
  finding_id    TEXT PRIMARY KEY, -- hash(rule_id, rule_version, issue_key, evidence_key)
  rule_id       TEXT NOT NULL,
  rule_version  TEXT NOT NULL,
  issue_key     TEXT NOT NULL,    -- for sprint-scoped rules: "sprint:<id>"
  project_key   TEXT NOT NULL,    -- added
  kind          TEXT NOT NULL,    -- added: rule | warning (from the catalogue)
  severity      TEXT NOT NULL,    -- breach | warning
  evidence_key  TEXT NOT NULL,    -- added: what the finding is about (one per rule+issue+key)
  message       TEXT,
  evidence_json TEXT,
  first_seen_at TEXT,
  last_seen_at  TEXT,
  resolved_at   TEXT,             -- NULL = open
  resolved_why  TEXT,             -- cleared | superseded | aged_out | settings_changed
  run_id        TEXT,
  settings_hash TEXT              -- the rule's params fingerprint when last seen; NULL: the catalogue's
);
CREATE INDEX IF NOT EXISTS ix_findings_open ON findings (project_key, resolved_at, rule_id);
CREATE INDEX IF NOT EXISTS ix_findings_issue ON findings (issue_key);

-- Phase 3: what each rule could and could not measure in each run.
CREATE TABLE IF NOT EXISTS rule_status (
  run_id           TEXT NOT NULL,
  project_key      TEXT NOT NULL,
  rule_id          TEXT NOT NULL,
  rule_version     TEXT NOT NULL,
  measurable       INTEGER NOT NULL, -- 0 = "not measurable": itself a finding for James
  missing_json     TEXT,             -- JSON list of missing evidence sources or stages
  issues_checked   INTEGER,
  hits             INTEGER,
  note             TEXT,             -- e.g. "fewer than 20 closed intervals: no threshold"
  settings_hash    TEXT,             -- added: the params fingerprint the rule ran with; NULL: the catalogue's
  PRIMARY KEY (run_id, project_key, rule_id)
);

-- Phase 4: "warned on X, breached on Y".
CREATE TABLE IF NOT EXISTS warning_links (
  warning_finding_id TEXT NOT NULL,
  breach_finding_id  TEXT NOT NULL,
  linked_at          TEXT NOT NULL,
  PRIMARY KEY (warning_finding_id, breach_finding_id)
);

-- Phase 5: every digest that went out, with the findings it carried.
CREATE TABLE IF NOT EXISTS digests (
  digest_id        TEXT PRIMARY KEY,
  project_key      TEXT NOT NULL,
  channel          TEXT NOT NULL,   -- file | email | teams
  sent_at          TEXT NOT NULL,
  since            TEXT,            -- window start (the previous digest)
  subject          TEXT,
  body             TEXT,
  finding_ids_json TEXT,            -- JSON list: new and resolved findings included
  delivery_ref     TEXT,            -- path, message id or HTTP status
  run_id           TEXT
);

-- Phase 6: every question and answer.
CREATE TABLE IF NOT EXISTS qa_log (
  qa_id          TEXT PRIMARY KEY,
  asked_at       TEXT,
  asked_by       TEXT,
  project_key    TEXT,
  question       TEXT,
  plan_json      TEXT,
  facts_json     TEXT,
  answer         TEXT,
  citations_json TEXT,              -- added: bindings from answer text to fact rows
  outcome        TEXT,              -- answered | not_understood | no_data | not_measurable | not_verified
  missing_json   TEXT,              -- added: evidence a not_measurable answer lacked
  model          TEXT,
  prompt_hash    TEXT,
  latency_ms     INTEGER,           -- added
  level          TEXT NOT NULL DEFAULT 'team', -- added: team | people, the level the question was answered at
  mentions_json  TEXT               -- added: the person ids a people-level question mentioned (ids, never names)
);

-- Phase 6: one row per LLM call, for the audit trail.
CREATE TABLE IF NOT EXISTS llm_calls (
  call_id       TEXT PRIMARY KEY,
  called_at     TEXT NOT NULL,
  purpose       TEXT NOT NULL,      -- plan | compose | compose_strict | insights | insights_strict | rule_draft
  provider      TEXT NOT NULL,      -- scripted | anthropic
  model         TEXT,
  prompt_hash   TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  latency_ms    INTEGER,
  stop_reason   TEXT,
  outcome       TEXT,               -- ok | refused | error
  error         TEXT,
  qa_id         TEXT
);

-- Phase 7: the weekly "here's where the system thinks things are a problem".
CREATE TABLE IF NOT EXISTS insights (
  insight_id      TEXT PRIMARY KEY,
  project_key     TEXT NOT NULL,
  week_of         TEXT NOT NULL,    -- Monday, YYYY-MM-DD
  generated_at    TEXT NOT NULL,
  verified        INTEGER NOT NULL, -- 0: the narrative failed validation; the table was shipped instead
  narrative       TEXT,
  sentences_json  TEXT,             -- [{id, text, tone, candidate_ids, issue_keys, finding_ids}]
  candidates_json TEXT,             -- the ranked candidates the narrative was written from
  model           TEXT,
  prompt_hash     TEXT,
  run_id          TEXT
);
CREATE INDEX IF NOT EXISTS ix_insights_project ON insights (project_key, generated_at);

-- Who viewed whose person-level data, and when; one row per request, ids only.
CREATE TABLE IF NOT EXISTS access_log (
  id               TEXT PRIMARY KEY, -- uuid4 hex
  at               TEXT NOT NULL,    -- when it was read (UTC)
  viewer_hash      TEXT NOT NULL,    -- X-Oversight-Viewer: a keyed hash of the viewer's sign-in, or "local"
  viewer_name      TEXT NOT NULL,    -- X-Oversight-Viewer-Name, decoded: a display name, never an email address
  level            TEXT NOT NULL,    -- team | people
  project_key      TEXT,             -- the board read; NULL for the access log itself
  action           TEXT NOT NULL,    -- the view or export that read the data
  subject_ids_json TEXT NOT NULL,    -- JSON list of the person ids whose data was read (ids, never names)
  detail_json      TEXT              -- JSON object: days, qa_id, filters, counts
);
CREATE INDEX IF NOT EXISTS ix_access_log_at ON access_log (at);
-- the daily model budget and GET /usage count each UTC day's model calls and questions.
CREATE INDEX IF NOT EXISTS ix_llm_calls_time ON llm_calls (called_at);
CREATE INDEX IF NOT EXISTS ix_qa_log_time ON qa_log (asked_at);

-- a board's rule settings, as admins saved them (one row per board and rule that differs from rules.yaml).
CREATE TABLE IF NOT EXISTS rule_settings (
  project_key TEXT NOT NULL,
  rule_id     TEXT NOT NULL,
  enabled     INTEGER,        -- 0 | 1; NULL: as rules.yaml says
  severity    TEXT,           -- breach | warning; NULL: as rules.yaml says
  params_json TEXT,           -- JSON object of the params set for this board; NULL: none
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (project_key, rule_id)
);
-- every change to a board's rule settings, never purged: the audit trail and what undo restores.
CREATE TABLE IF NOT EXISTS rule_settings_changes (
  change_id    TEXT PRIMARY KEY,   -- uuid4 hex
  at           TEXT NOT NULL,      -- UTC
  project_key  TEXT NOT NULL,
  revision     INTEGER NOT NULL,   -- the board's settings revision this change made: 1, 2, 3 ...
  viewer_hash  TEXT NOT NULL,      -- X-Oversight-Viewer (as the access log keeps it)
  viewer_name  TEXT NOT NULL,      -- X-Oversight-Viewer-Name, read as the access log reads it (never an email)
  reason       TEXT,               -- optional, at most 500 characters
  changes_json TEXT NOT NULL,      -- enabled | severity | params.<name>, or a custom rule field
  undo_of      TEXT,               -- change_id this change undid, or NULL
  UNIQUE (project_key, revision)
);

-- Rules an admin added to a board; a deleted rule keeps its row (deleted_at) so undo can restore it.
CREATE TABLE IF NOT EXISTS custom_rules (
  rule_id          TEXT PRIMARY KEY,   -- R-CUS-01 / W-CUS-01 ..., unique across every board, never reused
  project_key      TEXT NOT NULL,
  title            TEXT NOT NULL,
  description      TEXT,
  kind             TEXT NOT NULL,      -- rule | warning (from the seriousness picked at creation)
  severity         TEXT NOT NULL,      -- breach | warning, as created
  check_name       TEXT NOT NULL,
  params_json      TEXT NOT NULL,      -- the params as created / last edited
  applies_json     TEXT NOT NULL,      -- {"status_category": [...], "issue_type": [...] | null}
  created_at       TEXT NOT NULL,
  created_revision INTEGER NOT NULL,   -- the board's settings revision that created it
  created_by       TEXT NOT NULL,      -- viewer name, as the history keeps it (never an email)
  updated_at       TEXT NOT NULL,
  deleted_at       TEXT                -- NULL while it exists
);
CREATE INDEX IF NOT EXISTS ix_custom_rules_project ON custom_rules (project_key);
