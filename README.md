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

A sample problem (`sum-two`: read two integers, print their sum) is seeded automatically. Run
`make images` once to pre-pull the sandbox images, then `make demo` to submit a correct and a wrong
solution and watch them get graded. By hand:

```bash
curl -X POST http://localhost:8000/submissions -H "Content-Type: application/json" \
  -d '{"user_id":"user_123","problem_id":"sum-two","language":"python",
       "code":"a, b = map(int, input().split())\nprint(a + b)"}'

curl http://localhost:8000/submissions/<submission_id>           # status + score
curl http://localhost:8000/submissions/<submission_id>/results   # per-test-case outcome
curl http://localhost:8000/leaderboard/sum-two
```

## API

| Method & path | Purpose |
|---|---|
| `PUT /problems` | create/replace a problem and its test cases (`id`, `title`, `time_limit_s`, `test_cases[{stdin, expected_stdout}]`) |
| `GET /problems`, `GET /problems/{id}` | list / view problems (expected outputs are never returned) |
| `POST /submissions` | submit `{user_id, problem_id, language, code}`; languages: `python`, `javascript`, `java` |
| `GET /submissions/{id}` | status (`queued`/`running`/`completed`/`failed`/`timed_out`), score, runtime |
| `GET /submissions/{id}/results` | per-test-case pass/fail with stdout/stderr |
| `GET /submissions/{id}/audit` | status transition history |
| `GET /users/{user_id}/submissions` | a user's history (`?problem_id=`) |
| `GET /leaderboard/{problem_id}` | ranked best scores |

Set `API_KEY` in the environment (e.g. `API_KEY=secret docker compose up`) to require `X-API-Key` on writes; see [docs/SETUP.md](docs/SETUP.md) for all options (read protection, rate limit, DB password, gVisor).

**Grading.** Each test case runs in a fresh container with the case's stdin; output is compared to the expected
output ignoring trailing whitespace. Score = % of cases passed. Status is `timed_out` if any case timed out,
`failed` if any crashed (non-zero exit, compile error), else `completed`. Only `completed` submissions with a
score above 0 enter the leaderboard.

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
│   ├── schema.sql           # PostgreSQL schema + indexing strategy
│   └── seed.sql             # sample problem
├── docs/                    # architecture.md, SETUP.md
├── tests/                   # grading unit tests + Postgres integration tests
├── scripts/demo.sh
├── Makefile
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
