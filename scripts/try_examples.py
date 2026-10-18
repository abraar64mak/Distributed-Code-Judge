#!/usr/bin/env python3
"""Loads the example problems and submits every solution in examples/manifest.json to a running stack, then
checks each one ends with the expected status and score. Needs only the Python standard library.

    python3 scripts/try_examples.py                       # API at http://localhost:8000
    python3 scripts/try_examples.py security-manifest.json  # sandbox probes (network, memory, fork bomb, ...)
    API=http://host:8000 API_KEY=secret python3 scripts/try_examples.py
"""

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("API", "http://localhost:8000").rstrip("/")
HEADERS = {"Content-Type": "application/json"}
if os.environ.get("API_KEY"):
    HEADERS["X-API-Key"] = os.environ["API_KEY"]

ROOT = pathlib.Path(__file__).resolve().parent.parent / "examples"
POLL_SECONDS = 150  # cold starts, Java compilation and 3 timeouts of 10s+ can add up


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method, headers=HEADERS, data=json.dumps(body).encode() if body is not None else None
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"API not reachable at {API} ({e.reason}). Is the stack up? Run: make up && make logs")


def wait_for(submission_id):
    deadline = time.time() + POLL_SECONDS
    while time.time() < deadline:
        sub = call("GET", f"/submissions/{submission_id}")
        if sub["status"] not in ("queued", "running"):
            return sub
        time.sleep(1)
    return sub


def main():
    call("GET", "/health")
    for path in sorted((ROOT / "problems").glob("*.json")):
        call("PUT", "/problems", json.loads(path.read_text()))
    run_id = int(time.time())  # fresh user ids each run, so rate limits and leaderboards stay clean

    manifest_name = sys.argv[1] if len(sys.argv) > 1 else "manifest.json"
    manifest = json.loads((ROOT / manifest_name).read_text())
    pending = []
    for i, e in enumerate(manifest):
        source = ROOT / "solutions" / e["file"]
        code = (source if source.exists() else ROOT / e["file"]).read_text()  # probes live in examples/security/
        r = call(
            "POST",
            "/submissions",
            {"user_id": f"example-{run_id}-{i}", "problem_id": e["problem"], "language": e["language"], "code": code},
        )
        pending.append((e, r["submission_id"]))

    failures = 0
    print(f"{'solution':34} {'expected':16} {'actual':16} result")
    for e, sid in pending:
        sub = wait_for(sid)
        actual = (sub["status"], float(sub["score"] or 0))
        expected = (e["status"], float(e["score"]))
        ok = actual == expected
        failures += not ok
        print(
            f"{e['file']:34} {expected[0] + ' ' + str(expected[1]):16} "
            f"{actual[0] + ' ' + str(actual[1]):16} {'ok' if ok else 'MISMATCH'}"
        )
    print("\nall examples behaved as expected" if not failures else f"\n{failures} mismatch(es)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
