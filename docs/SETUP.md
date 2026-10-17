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

## Security notes (for the secure-computing discussion)
Applied per test case: `network_disabled`, memory + swap cap (256 MB; 512 MB for Java), 1 CPU, 64 PIDs, all Linux
capabilities dropped, `no-new-privileges`, non-root user (65534), read-only rootfs, 64 MB tmpfs, wall-clock
timeout with kill, 64 KB output cap, user code passed via environment variables (no shell interpolation).

Known gaps (good lab exercises):
- **Docker socket:** the runner mounts `/var/run/docker.sock`, so a compromised runner process is root-equivalent
  on the host. Explore rootless Docker, a socket proxy allowing only container create/start/wait/kill/remove,
  gVisor/Kata (`runtime="runsc"`), or Firecracker microVMs.
- No custom seccomp/AppArmor profile; default Docker profile only.
- Env-var transport limits source size (API caps code at 64,000 characters) and exposes code in `docker inspect`.
- API auth is a single shared `X-API-Key` on writes only; reads (including user ids) are open, and there is no
  rate limiting. Submitted code is stored verbatim.
- Compose uses default `judge/judge` credentials and publishes Postgres on 5432; change both outside a local lab.
- Not verified in CI: the Docker sandbox and Kafka paths need a Docker host (`make up && make demo`).
