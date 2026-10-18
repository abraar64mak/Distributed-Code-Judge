# Lab setup guide

## Prerequisites
- Docker 24+ with Compose v2 (`docker compose`)
- Python 3.11+ (only for running tests/lint locally)

## Run the stack
```bash
docker compose up --build
curl localhost:8000/health
curl -X POST localhost:8000/submissions -H 'Content-Type: application/json' \
  -d '{"user_id":"u1","problem_id":"two-sum","language":"python","code":"print(1+2)"}'
curl localhost:8000/submissions/<submission_id>
curl localhost:8000/leaderboard/two-sum
```
The runner pulls `python:3.11-slim`, `node:20-slim` and `eclipse-temurin:21-jdk` on first use; pre-pull them
(`docker pull ...`) to avoid first-submission timeouts.

## Develop
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && pytest -q
```

## Record a verified run
`scripts/record_run.sh` (needs Docker running) starts the stack, runs everything, and saves the captured output in
`results/run-<UTC timestamp>/`. Open `REPORT.md` there. Options: `PUSH=1` commits and pushes `results/` to
`origin/main` with your git credentials, `SKIP_UP=1` reuses a stack that is already running, `DOWN=1` stops it
afterwards. It picks a free Postgres host port automatically.

## Configuration (environment variables, set in your shell or a `.env` next to docker-compose.yml)
| Variable | Default | Effect |
|---|---|---|
| `POSTGRES_PASSWORD` | `judge` | database password (change it outside a throwaway lab) |
| `POSTGRES_PORT` | `5432` | host port for Postgres (use e.g. `5433` if a local Postgres already holds 5432) |
| `API_KEY` | unset | require `X-API-Key` on `PUT /problems` and `POST /submissions` |
| `PROTECT_READS` | unset | with `API_KEY`, require the key on read endpoints too (all but `/health`) |
| `SUBMIT_RATE_LIMIT` | `10` | max submissions per `user_id` per minute (`0` disables) |
| `SANDBOX_RUNTIME` | unset | OCI runtime for sandbox containers, e.g. `runsc` for gVisor (must be installed on the host) |

## Security notes (for the secure-computing discussion)
Applied per test case: `network_disabled`, memory + swap cap (256 MB; 512 MB for Java), 1 CPU, 64 PIDs, all Linux
capabilities dropped, `no-new-privileges`, non-root user (65534), read-only rootfs, 64 MB tmpfs, wall-clock
timeout with kill, 64 KB output cap, user code passed via environment variables (no shell interpolation).

Platform measures:
- **Docker socket isolation:** the runner does not mount `/var/run/docker.sock`. It talks to a small policy proxy
  (`services/docker-proxy/proxy.py`, standard library only) that holds the socket and is not published to the host.
  The proxy checks the *parameters* of every container create request: the user must be 65534, networking disabled,
  all capabilities dropped, `no-new-privileges`, read-only rootfs, only an `/tmp` tmpfs, bounded memory/PIDs/CPU,
  no swap, only the three approved images and the `judge.sandbox` label. Any other field (privileged, binds,
  mounts, devices, host namespaces...) is refused, as is every endpoint the runner doesn't need (exec, volumes,
  networks, build, listing containers...). The runner can only start, wait for, read logs from, kill and remove
  containers that carry its label, so it cannot touch Postgres, Kafka or other containers. Denials are logged as
  `DENY ...` in the proxy's output.
- Postgres is published on `127.0.0.1` only; password configurable. API has optional key auth, optional read
  protection and a per-user submission rate limit.

Remaining gaps (good lab exercises):
- **The proxy is the trust anchor.** It runs as root with the Docker socket, so a bug in its ~250 lines would be a
  host compromise. It is deliberately small and allow-list based (unknown JSON keys are rejected, so case or
  escape tricks cannot smuggle fields), but it has had no independent audit. A compromised runner can still run
  *allowed* sandboxes, e.g. send arbitrary commands to the approved images with the approved limits. Stronger
  options: rootless Docker for the sandbox daemon, gVisor/Kata via `SANDBOX_RUNTIME`, or Firecracker microVMs,
  ideally on a dedicated host.
- No custom seccomp/AppArmor profile; default Docker profile only.
- Env-var transport limits source size (API caps code at 64,000 characters) and exposes code in `docker inspect`.
- Rate limiting is in-memory per API process; auth is a single shared key. Submitted code is stored verbatim.
- Kafka is only exercised in the manual run (`make up && make demo`); the Docker sandbox and proxy are covered by automated tests when Docker and the sandbox images are available (always in CI).
