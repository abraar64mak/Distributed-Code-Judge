# Architecture

## Components

### 1. API Layer (`services/api/main.py`)
A FastAPI service exposing:
- `POST /submissions` — accepts a code submission, validates it, publishes a `submission.created` event to Kafka, and returns a `submission_id` immediately (execution is async).
- `GET /submissions/{id}` — polls submission status/result.
- `GET /leaderboard/{problem_id}` — returns ranked results for a problem.

The API layer never executes user code directly — it only validates input and publishes events, keeping it fast and safe under load.

### 2. Kafka Event Bus
Submissions flow through a `submissions` topic. This decouples ingestion rate from execution throughput: the API can accept bursts of submissions without being blocked by sandbox execution time, and sandbox-runner workers can be scaled horizontally to consume from the topic in parallel.

### 3. Sandbox Runner (`services/sandbox-runner/runner.py`)
Consumes submission events and:
1. Spins up an isolated Docker container per submission (no network, capped CPU/memory, wall-clock timeout).
2. Executes the submitted code against the problem's test cases.
3. Captures stdout/stderr/exit code and execution time.
4. Publishes a `submission.completed` event and writes the result to PostgreSQL.

Isolation guarantees:
- No network access inside the sandbox.
- Read-only filesystem except for a scratch `/tmp` mount that's wiped after each run.
- Resource limits enforced via Docker `--memory`, `--cpus`, and a wrapper-level wall-clock timeout that kills the container if exceeded.

### 4. PostgreSQL (`db/schema.sql`)
Stores:
- `submissions` — one row per submission, full audit trail (status transitions, timestamps, resource usage).
- `results` — per-test-case results linked to a submission.
- `leaderboard_cache` — materialized ranking table, refreshed incrementally as results land, to keep `GET /leaderboard/{problem_id}` fast without scanning the full submissions table on every request.

Indexing strategy: composite index on `(problem_id, best_score DESC, submitted_at ASC)` supports the leaderboard's "best score, earliest submission wins ties" ordering directly, and `(user_id, problem_id)` supports user-history lookups without a full table scan. See inline comments in `db/schema.sql`.

### 5. Leaderboard Query Service (`services/leaderboard/queries.py`)
A thin query layer over the `leaderboard_cache` table, kept separate from the main API module so read-heavy leaderboard traffic can be scaled/cached independently from submission writes.

## Concurrency & reliability

- Kafka consumer group ensures each submission is processed by exactly one runner instance at a time, with at-least-once delivery semantics; the runner's DB writes are idempotent on `submission_id` (upsert), so redelivered messages don't produce duplicate results.
- Sandbox runner failures (container crash, OOM) are caught and recorded as a `failed` status rather than silently dropped — every submission always resolves to a terminal state.
- Database writes for audit logging happen synchronously with result processing, so the audit trail is never out of sync with the submission's actual outcome.

## CI/CD

GitHub Actions (`.github/workflows/ci-cd.yml`) runs on every PR (lint, unit tests, docker build check) and on merge to `main` builds and tags a versioned image, ready for deployment.

## Judging flow (v1.1)

1. `PUT /problems` stores a problem and its test cases (`problems`, `test_cases`).
2. `POST /submissions` validates, inserts `queued` + audit row, publishes to Kafka (waits for the broker ack; on
   failure the submission is marked `failed` and the API returns 503).
3. The runner moves it to `running`, loads time limit and test cases from PostgreSQL, runs one container per case,
   grades stdout, and in one transaction writes the terminal status, score, per-case `results`, audit row and
   leaderboard update. Offsets are committed only afterwards; redelivery of a finished submission is a no-op.
