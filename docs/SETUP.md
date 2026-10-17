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
- Isolation per submission: `network_disabled`, 256 MB memory, 1 CPU, read-only rootfs, 64 MB tmpfs, 10 s timeout.
- **Known gap:** the runner mounts `/var/run/docker.sock`, so a compromised runner process is root-equivalent on
  the host. Mitigations to explore: rootless Docker, a socket proxy that only allows `containers.*`,
  gVisor/Kata (`runtime="runsc"`), or Firecracker microVMs.
- Not yet set: `pids_limit`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges"]`, a non-root `user`, output-size caps,
  seccomp profile. These are natural lab exercises.
- The API stores raw submitted code and has no authentication or rate limiting.
- Compose uses default `judge/judge` credentials and exposes Postgres on 5432; change both outside a local lab.
