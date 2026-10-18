#!/usr/bin/env bash
# Submits a correct and a wrong solution to the seeded `sum-two` problem and prints the outcome.
set -euo pipefail
API="${API:-http://localhost:8000}"
# bash 3.2 (macOS default) treats an empty array as unbound under `set -u`, so build the args as a string.
KEY_ARGS=""
[ -n "${API_KEY:-}" ] && KEY_ARGS="-H X-API-Key:${API_KEY}"

curl -fsS "$API/health" >/dev/null 2>&1 || {
  echo "API not reachable at $API. Is the stack up? Run: make up && make logs" >&2
  exit 1
}

submit() {
  curl -fsS -X POST "$API/submissions" -H 'Content-Type: application/json' $KEY_ARGS \
    -d "{\"user_id\":\"$1\",\"problem_id\":\"sum-two\",\"language\":\"python\",\"code\":$2}" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["submission_id"])'
}

good=$(submit alice '"a, b = map(int, input().split())\nprint(a + b)"')
bad=$(submit bob '"print(42)"')

for id in "$good" "$bad"; do
  for _ in $(seq 1 60); do
    status=$(curl -fsS "$API/submissions/$id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
    case "$status" in queued|running) sleep 1 ;; *) break ;; esac
  done
  echo "== $id"; curl -fsS "$API/submissions/$id"; echo
  curl -fsS "$API/submissions/$id/results"; echo
done
echo "== leaderboard"; curl -fsS "$API/leaderboard/sum-two"; echo
