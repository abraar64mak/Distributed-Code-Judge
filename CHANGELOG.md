# Changelog

## [1.1.2] - 2026-10-18

### Added
- `examples/`: sample problems and solutions in Python, JavaScript and Java, plus deliberate failures (wrong answer,
  timeout, runtime error, compile error) with expected outcomes in `manifest.json`.
- `make examples` / `scripts/try_examples.py` verifies them against a running stack; `tests/test_examples.py`
  verifies them against the grader using local interpreters.
- `docs/WALKTHROUGH.md`: architecture tour, threat model, known weaknesses and experiments for the lab.

### Fixed
- Postgres host port configurable via `POSTGRES_PORT` (avoids clashes with a local Postgres).
- `scripts/demo.sh` works with macOS's bash 3.2.

## [1.1.1] - 2026-10-17

### Security
- Runner no longer mounts the Docker socket; it uses a `docker-socket-proxy` sidecar that allows only
  container/image calls. Postgres is bound to localhost; `POSTGRES_PASSWORD` is configurable.
- Per-user submission rate limiting (`SUBMIT_RATE_LIMIT`), optional read protection (`PROTECT_READS`),
  optional alternative sandbox runtime (`SANDBOX_RUNTIME`, e.g. gVisor).
- Tests guard the compose security posture.

## [1.1.0] - 2026-10-17

### Added
- Problems with graded test cases (`problems`, `test_cases` tables; `PUT /problems`, `GET /problems[/{id}]`).
  Expected outputs are read by the runner straight from the database and never exposed by the API.
- Real grading: each test case runs in its own container with stdin; score is the percentage of matching cases,
  and per-case results are stored in `results` (`GET /submissions/{id}/results`, `/audit`).
- `running` status and full audit trail (`queued -> running -> terminal`).
- `GET /users/{user_id}/submissions`, ranks on `/leaderboard/{problem_id}`, optional `X-API-Key` auth on writes.
- Sandbox hardening: all capabilities dropped, `no-new-privileges`, non-root user, PID limit, swap disabled,
  output truncation; user code is passed via environment variables, never interpolated into a shell string.
- Seed problem (`sum-two`), `make` targets, `scripts/demo.sh`, CI with Postgres, unit + integration tests.

### Fixed
- Runner is genuinely idempotent on Kafka redelivery; offsets are committed only after results are stored.
- API returns 503 and marks the submission `failed` if it cannot be enqueued, instead of leaving it `queued`.
- Java submissions no longer break on quotes or shell metacharacters.
- Repository restructured into the documented layout; added missing API Dockerfile and requirements.

## [1.0.0] - 2026-07-01

### Added
- Initial public release of the distributed code judge.
- FastAPI submission service with `/submissions`, `/submissions/{id}`, `/leaderboard/{problem_id}` endpoints.
- Kafka-based event streaming between submission API and sandbox runner workers.
- Docker sandboxing: isolated, network-disabled, resource-limited execution per submission.
- PostgreSQL schema with audit logging and indexing strategy for fast leaderboard queries under concurrent load.
- Docker Compose setup for local development (API, Kafka, Postgres, sandbox runner).
- CI/CD pipeline via GitHub Actions: lint, test, and versioned image builds on merge to `main`.

## [0.1.0] - 2026-05-01

### Added
- Initial prototype: synchronous single-process code execution (pre-Kafka, pre-Docker isolation).
