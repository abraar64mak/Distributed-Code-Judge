#!/usr/bin/env bash
# Runs the whole stack on THIS machine and records what actually happened, verbatim, in
# results/run-<UTC timestamp>/REPORT.md (plus the raw outputs next to it).
#
#   scripts/record_run.sh              # start the stack, run demo + examples + sandbox probes, write the report
#   PUSH=1 scripts/record_run.sh       # ...and also commit and push results/ to origin/main with your git login
#   SKIP_UP=1 scripts/record_run.sh    # the stack is already running; just run and record
#   DOWN=1 scripts/record_run.sh       # stop the stack afterwards (data kept; add -v yourself to wipe)
#
# Nothing in the report is written by hand: every section is the captured output of a command, and the
# header records the OS, Docker version and git commit so the provenance is visible.
set -uo pipefail   # no -e: a failing step must be recorded, not abort the recording

cd "$(dirname "$0")/.."
API="${API:-http://localhost:8000}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="results/run-$STAMP"
mkdir -p "$OUT"

say() { printf '\n==> %s\n' "$*"; }

# first free host port for Postgres, so a local Postgres on 5432 doesn't break the run
if [ -z "${POSTGRES_PORT:-}" ]; then
  POSTGRES_PORT="$(python3 - <<'PY'
import socket
for port in (5432, 5433, 5434, 5435):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        print(port)
        break
    except OSError:
        pass
    finally:
        s.close()
PY
)"
fi
export POSTGRES_PORT

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop, wait until 'docker info' works, then re-run." >&2
  exit 1
fi

if [ -z "${SKIP_UP:-}" ]; then
  say "Starting the stack (POSTGRES_PORT=$POSTGRES_PORT)"
  docker compose up --build -d > "$OUT/compose-up.txt" 2>&1
  echo "compose up exit code: $?" | tee -a "$OUT/compose-up.txt"
fi

say "Waiting for the API at $API (up to 3 minutes)"
healthy=no
for _ in $(seq 1 90); do
  if curl -fsS "$API/health" >/dev/null 2>&1; then healthy=yes; break; fi
  sleep 2
done
echo "api healthy: $healthy"
if [ "$healthy" != "yes" ]; then
  docker compose ps > "$OUT/compose-ps.txt" 2>&1 || true
  docker compose logs --no-color --tail 200 > "$OUT/compose-logs.txt" 2>&1 || true
  echo "The API never became healthy. See $OUT/compose-logs.txt" >&2
fi

if [ "$healthy" = "yes" ]; then
  say "Waiting for the sandbox runner to join Kafka (it retries until the broker is ready)"
  sleep 20

  say "demo (scripts/demo.sh)"
  ./scripts/demo.sh > "$OUT/demo.txt" 2>&1; echo "exit code: $?" >> "$OUT/demo.txt"; tail -n 4 "$OUT/demo.txt"

  say "examples (13 solutions, python/javascript/java + failure modes)"
  python3 scripts/try_examples.py > "$OUT/examples.txt" 2>&1; echo "exit code: $?" >> "$OUT/examples.txt"
  cat "$OUT/examples.txt"

  say "sandbox probes (network, memory hog, fork bomb, write to /)"
  python3 scripts/try_examples.py security-manifest.json > "$OUT/security.txt" 2>&1; echo "exit code: $?" >> "$OUT/security.txt"
  cat "$OUT/security.txt"

  say "policy proxy: attacks the runner could attempt must be refused"
  {
    runner_id="$(docker compose ps -q sandbox-runner 2>/dev/null | head -n 1)"
    if [ -n "$runner_id" ]; then
      docker compose exec -T sandbox-runner python - <<'PY'
import json, urllib.error, urllib.request

BASE = "http://docker-proxy:2375"
GOOD_IMAGE = "python:3.11-slim"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


attempts = [
    ("privileged container + host bind mount", "POST", "/containers/create",
     {"Image": GOOD_IMAGE, "User": "65534:65534", "NetworkDisabled": True, "WorkingDir": "/tmp",
      "Labels": {"judge.sandbox": "1"}, "HostConfig": {"Privileged": True, "Binds": ["/:/host"]}}),
    ("run as root", "POST", "/containers/create",
     {"Image": GOOD_IMAGE, "User": "root", "NetworkDisabled": True, "WorkingDir": "/tmp",
      "Labels": {"judge.sandbox": "1"}, "HostConfig": {}}),
    ("unapproved image (alpine)", "POST", "/containers/create", {"Image": "alpine"}),
    ("list all containers", "GET", "/containers/json", None),
    ("list volumes", "GET", "/volumes", None),
    ("host info", "GET", "/info", None),
    ("pull unapproved image", "POST", "/images/create?fromImage=alpine&tag=latest", {}),
    ("API health ping (allowed)", "GET", "/_ping", None),
]
for name, method, path, body in attempts:
    status = call(method, path, body)
    expected = 200 if "allowed" in name else 403
    print(f"{'ok  ' if status == expected else 'FAIL'} {name:42} -> HTTP {status} (expected {expected})")
PY
    else
      echo "sandbox-runner container not found; skipped"
    fi
  } > "$OUT/proxy-attacks.txt" 2>&1
  cat "$OUT/proxy-attacks.txt"
fi

say "Collecting environment and logs"
docker compose ps > "$OUT/compose-ps.txt" 2>&1 || true
docker compose logs --no-color --tail 300 docker-proxy > "$OUT/proxy-log.txt" 2>&1 || true
docker compose logs --no-color --tail 300 sandbox-runner > "$OUT/runner-log.txt" 2>&1 || true
docker compose logs --no-color --tail 100 api > "$OUT/api-log.txt" 2>&1 || true
docker ps -a --filter label=judge.sandbox=1 --format '{{.ID}} {{.Status}}' > "$OUT/leftover-sandboxes.txt" 2>&1 || true

{
  echo "# Recorded run $STAMP"
  echo
  echo "Every section below is the verbatim output of a command, captured by \`scripts/record_run.sh\`."
  echo
  echo "## Environment"
  echo '```'
  echo "git commit : $(git rev-parse HEAD 2>/dev/null)  ($(git rev-parse --abbrev-ref HEAD 2>/dev/null))"
  echo "uname      : $(uname -a)"
  command -v sw_vers >/dev/null 2>&1 && sw_vers
  echo "cpu        : $(uname -m)"
  echo "docker     : $(docker version --format 'client {{.Client.Version}}, server {{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})' 2>&1)"
  echo "compose    : $(docker compose version 2>&1)"
  echo "python3    : $(python3 --version 2>&1)"
  echo "postgres host port: $POSTGRES_PORT"
  echo '```'
  for f in compose-ps demo examples security proxy-attacks; do
    [ -f "$OUT/$f.txt" ] || continue
    echo
    echo "## $f"
    echo '```'
    cat "$OUT/$f.txt"
    echo '```'
  done
  echo
  echo "## Sandbox containers left behind (should be empty)"
  echo '```'
  cat "$OUT/leftover-sandboxes.txt"
  echo '```'
  echo
  echo "## Policy proxy log (denials)"
  echo '```'
  grep -E "DENY|policy proxy|allowed images" "$OUT/proxy-log.txt" | head -n 60
  echo '```'
  echo
  echo "Raw logs: runner-log.txt, api-log.txt, proxy-log.txt in this directory."
} > "$OUT/REPORT.md"

say "Done. Report: $OUT/REPORT.md"
if [ -f "$OUT/examples.txt" ]; then
  echo "examples : $(tail -n 2 "$OUT/examples.txt" | tr '\n' ' ')"
  echo "security : $(tail -n 2 "$OUT/security.txt" | tr '\n' ' ')"
fi

if [ -n "${DOWN:-}" ]; then docker compose down; fi

if [ -n "${PUSH:-}" ]; then
  say "Committing and pushing results to origin/main with your git credentials"
  git add "$OUT" && git commit -m "Add recorded run results ($STAMP, $(uname -s) $(uname -m))" \
    && git pull --rebase origin main && git push origin HEAD:main
else
  echo
  echo "To publish these results to main:  git add $OUT && git commit -m 'Add recorded run results' && git push origin main"
  echo "(or re-run with PUSH=1)"
fi
