# Changelog

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
