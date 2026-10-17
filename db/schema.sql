-- Schema for distributed-code-judge
-- Indexing strategy is designed around the two hottest read paths:
--   1. Leaderboard queries: best score per user per problem, tie-broken by earliest submission.
--   2. Per-user submission history lookups.

CREATE TABLE IF NOT EXISTS submissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    problem_id      TEXT NOT NULL,
    language        TEXT NOT NULL,
    code            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'timed_out')),
    score           NUMERIC(5, 2),
    execution_ms    INTEGER,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

-- Speeds up per-user submission history without a full table scan.
CREATE INDEX IF NOT EXISTS idx_submissions_user_problem
    ON submissions (user_id, problem_id, submitted_at DESC);

-- Speeds up leaderboard aggregation: best score per problem, earliest submission wins ties.
CREATE INDEX IF NOT EXISTS idx_submissions_problem_score
    ON submissions (problem_id, score DESC, submitted_at ASC)
    WHERE status = 'completed';

CREATE TABLE IF NOT EXISTS results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id   UUID NOT NULL REFERENCES submissions (id) ON DELETE CASCADE,
    test_case_id    TEXT NOT NULL,
    passed          BOOLEAN NOT NULL,
    stdout          TEXT,
    stderr          TEXT,
    runtime_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_results_submission
    ON results (submission_id);

-- Materialized-style cache table, incrementally updated by the sandbox runner
-- on each completed submission, so leaderboard reads never scan `submissions` directly.
CREATE TABLE IF NOT EXISTS leaderboard_cache (
    problem_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    best_score      NUMERIC(5, 2) NOT NULL,
    best_submission_id UUID NOT NULL REFERENCES submissions (id),
    achieved_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (problem_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_leaderboard_ranking
    ON leaderboard_cache (problem_id, best_score DESC, achieved_at ASC);

-- Audit log: append-only record of every status transition for compliance/debugging.
CREATE TABLE IF NOT EXISTS submission_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    submission_id   UUID NOT NULL REFERENCES submissions (id) ON DELETE CASCADE,
    previous_status TEXT,
    new_status      TEXT NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_submission
    ON submission_audit_log (submission_id, changed_at);

-- Problems and their graded test cases. The runner loads these directly from the database, so expected
-- outputs never travel over Kafka or reach the API's public read endpoints.
CREATE TABLE IF NOT EXISTS problems (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    time_limit_s    INTEGER NOT NULL DEFAULT 10 CHECK (time_limit_s BETWEEN 1 AND 60),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS test_cases (
    id              BIGSERIAL PRIMARY KEY,
    problem_id      TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    stdin           TEXT NOT NULL DEFAULT '',
    expected_stdout TEXT NOT NULL,
    UNIQUE (problem_id, ordinal)
);
