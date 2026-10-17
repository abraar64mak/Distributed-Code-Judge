# distributed-code-judge

A customer-facing, distributed code execution platform for evaluating user-submitted code at scale — with Docker sandboxing, Kafka event streaming, PostgreSQL-backed audit logging, and automated CI/CD.

[![CI/CD](https://github.com/abraar64mak/distributed-code-judge/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/abraar64mak/distributed-code-judge/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Overview

This is a full-stack, production-shaped code judge: submissions come in over an API, get sandboxed and executed in isolated Docker containers, stream through Kafka for async processing, and get persisted to PostgreSQL for audit logging and leaderboard queries — all built to hold up under concurrent load.

## Architecture

```
                ┌────────────┐
   Submission   │  FastAPI    │
   ───────────► │  API Layer  │
                └──────┬─────┘
                       │ publish
                       ▼
                ┌────────────┐
                │   Kafka     │
                │  (events)   │
                └──────┬─────┘
                       │ consume
                       ▼
                ┌─────────────────┐
                │ Sandbox Runner    │
                │ (Docker, isolated)│
                └──────┬───────────┘
                       │ result
                       ▼
                ┌────────────┐        ┌──────────────┐
                │ PostgreSQL  │ <────► │ Leaderboard   │
                │ (audit log, │        │ Query Service │
                │ submissions)│        └──────────────┘
                └────────────┘
```

## Key design decisions

- **Docker sandboxing**: every submission runs in a freshly spawned, resource-limited container (CPU/memory/time capped) with no network access, so untrusted user code can't affect the host or other submissions.
- **Kafka event streaming**: submissions are published as events and consumed asynchronously by sandbox runner workers, which decouples the API's response time from actual execution time and lets us scale runners independently.
- **PostgreSQL audit logging**: every submission, execution result, and status transition is persisted with indexed columns on `(problem_id, user_id, submitted_at)` to keep leaderboard queries fast under concurrent load — see [`db/schema.sql`](db/schema.sql) for the indexing strategy.
- **CI/CD via GitHub Actions**: every PR runs lint + tests; merges to `main` build and push a versioned container image.

## Quickstart

```bash
git clone https://github.com/abraar64mak/distributed-code-judge.git
cd distributed-code-judge
docker-compose up --build
```

This brings up: the FastAPI submission service, a Kafka broker, one sandbox-runner worker, and PostgreSQL.

Submit code for judging:

```bash
curl -X POST http://localhost:8000/submissions \
  -H "Content-Type: application/json" \
  -d '{
        "user_id": "user_123",
        "problem_id": "two-sum",
        "language": "python",
        "code": "print(sum([1,2,3]))"
      }'
```

Check the leaderboard:

```bash
curl http://localhost:8000/leaderboard/two-sum
```

See [docs/SETUP.md](docs/SETUP.md) for setup instructions and security notes.

## Repository layout

```
distributed-code-judge/
├── services/
│   ├── api/                # FastAPI submission endpoints
│   │   └── main.py
│   ├── sandbox-runner/      # Kafka consumer + Docker execution worker
│   │   └── runner.py
│   └── leaderboard/         # indexed leaderboard query service
│       └── queries.py
├── db/
│   └── schema.sql           # PostgreSQL schema + indexing strategy
├── docs/
│   └── architecture.md
├── docker-compose.yml
├── CHANGELOG.md
└── LICENSE
```

## Reliability notes

- Sandbox containers are killed and reclaimed on timeout (default 10s wall clock, configurable per-problem).
- Submission state machine: `queued -> running -> completed | failed | timed_out`, all transitions logged to the audit table.
- Kafka consumer group is set up for at-least-once processing; the runner is idempotent on `submission_id` to safely handle redelivery.

## Releases

See [CHANGELOG.md](CHANGELOG.md) and [Releases](https://github.com/abraar64mak/distributed-code-judge/releases).

## License

MIT — see [LICENSE](LICENSE).
