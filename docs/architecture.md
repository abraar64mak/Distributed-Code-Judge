# Architecture

## Components

### 1. API Layer (`services/api/main.py`)
A FastAPI service exposing:
- `POST /submissions` — accepts a code submission, validates it, publishes the submission to the `submissions` Kafka topic, and returns a `submission_id` immediately (execution is async).
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
4. Writes the result (status, score, per-case results, audit row, leaderboard update) to PostgreSQL in one transaction. No completion event is published; clients poll `GET /submissions/{id}`.

Isolation guarantees:
- No network access inside the sandbox.
- Read-only filesystem except for a scratch `/tmp` mount that's wiped after each run.
- Resource limits enforced via Docker `--memory`, `--cpus`, and a wrapper-level wall-clock timeout that kills the container if exceeded.

### 4. PostgreSQL (`db/schema.sql`)
Stores:
- `submissions` — one row per submission, full audit trail (status transitions, timestamps, resource usage).
- `results` — per-test-case results linked to a submission.
- `leaderboard_cache` — materialized ranking table, refreshed incrementally as results land, to keep `GET /leaderboard/{problem_id}` fast without scanning the full submissions table on every request.

Indexing strategy: `leaderboard_cache` has a composite index on `(problem_id, best_score DESC, achieved_at ASC)`, which supports the leaderboard's "best score, earliest achieved wins ties" ordering directly, and `submissions` has `(user_id, problem_id, submitted_at DESC)` for user-history lookups without a full table scan. See inline comments in `db/schema.sql`.

### 4b. Docker policy proxy (`services/docker-proxy/proxy.py`)
The only component that touches the Docker socket. The runner's `DOCKER_HOST` points at it. It validates every
container-create request against a strict allow-list (see `docs/SETUP.md`), refuses all other endpoints, and
only allows operations on containers labelled `judge.sandbox=1`.

### 5. Leaderboard Query Service (`services/leaderboard/queries.py`)
A thin query layer over the `leaderboard_cache` table, kept separate from the main API module so read-heavy leaderboard traffic can be scaled/cached independently from submission writes.

## Concurrency & reliability

- Kafka consumer group gives each partition to one runner at a time, with at-least-once delivery. Runners scale only up to the topic's partition count (compose creates it with 4 via `KAFKA_NUM_PARTITIONS`; a topic created earlier keeps its old count). Redelivery is safe because the runner locks the submission row and ignores any submission that is already finished, so no duplicate results or audit rows are written.
- Sandbox runner failures (container crash, OOM) are caught and recorded as a `failed` status rather than silently dropped — every submission always resolves to a terminal state.
- Database writes for audit logging happen synchronously with result processing, so the audit trail is never out of sync with the submission's actual outcome.

## CI/CD

GitHub Actions (`.github/workflows/ci-cd.yml`) runs on every PR and every push to `main`: lint (`ruff`), the test suite against a PostgreSQL service container, and a `docker compose build` check. It does not publish images; adding a job that builds and pushes versioned images to a registry on merge is future work.

## Judging flow (v1.1)

1. `PUT /problems` stores a problem and its test cases (`problems`, `test_cases`).
2. `POST /submissions` validates, inserts `queued` + audit row, publishes to Kafka (waits for the broker ack; on
   failure the submission is marked `failed` and the API returns 503).
3. The runner moves it to `running`, loads time limit and test cases from PostgreSQL, runs one container per case,
   grades stdout, and in one transaction writes the terminal status, score, per-case `results`, audit row and
   leaderboard update. Offsets are committed only afterwards; redelivery of a finished submission is a no-op.
