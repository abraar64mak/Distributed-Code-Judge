"""End-to-end checks against a real PostgreSQL (skipped if DATABASE_URL is unreachable).

Kafka and Docker are stubbed: the API's producer is a recorder and the sandbox is a fake `run` function.
"""

import asyncio
import os
import uuid

import asyncpg
import pytest
from conftest import load_module
from fastapi.testclient import TestClient

DSN = os.environ.get("DATABASE_URL", "postgresql://judge:judge@localhost:5432/judge")
os.environ["DATABASE_URL"] = DSN


def _db_up() -> bool:
    async def ping():
        conn = await asyncpg.connect(DSN, timeout=2)
        await conn.close()

    try:
        asyncio.run(ping())
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="PostgreSQL not reachable")


class FakeFuture:
    def get(self, timeout=None):
        return None


class FakeProducer:
    def __init__(self):
        self.sent = []

    def send(self, topic, value):
        self.sent.append((topic, value))
        return FakeFuture()


def fake_sandbox(answers):
    """Sandbox whose stdout is `answers[code]` for every test case -- lets a test pick right/wrong solutions."""

    def run(language, code, stdin, timeout):
        a, b = map(int, stdin.split())
        out = str(a + b) if answers[code] == "right" else "0"
        return {"exit_code": 0, "stdout": out + "\n", "stderr": "", "timed_out": False, "ms": 3}

    return run


def test_full_flow():
    api = load_module("api_main", "services/api/main.py")
    runner = load_module("runner", "services/sandbox-runner/runner.py")
    producer = FakeProducer()
    api.get_producer = lambda: producer
    pid = f"sum-{uuid.uuid4().hex[:8]}"
    answers = {"right-code": "right", "wrong-code": "wrong"}

    with TestClient(api.app) as client:
        assert client.get("/health").json() == {"status": "ok"}

        # --- problem management
        put = client.put(
            "/problems",
            json={
                "id": pid,
                "title": "Sum",
                "time_limit_s": 5,
                "test_cases": [
                    {"stdin": "1 2\n", "expected_stdout": "3\n"},
                    {"stdin": "5 -5\n", "expected_stdout": "0\n"},
                ],
            },
        )
        assert put.status_code == 200 and put.json()["test_cases"] == 2
        assert client.get(f"/problems/{pid}").json() == {"id": pid, "title": "Sum", "time_limit_s": 5, "test_cases": 2}
        assert "expected_stdout" not in client.get(f"/problems/{pid}").text
        assert client.get("/problems/nope").status_code == 404

        # --- validation
        def submit(user, code, problem=pid, language="python"):
            return client.post(
                "/submissions", json={"user_id": user, "problem_id": problem, "language": language, "code": code}
            )

        assert submit("u", "  ").status_code == 400
        assert submit("u", "x", language="cobol").status_code == 400
        assert submit("u", "x", problem="nope").status_code == 404
        assert client.get("/submissions/not-a-uuid").status_code == 422
        assert client.get(f"/submissions/{uuid.uuid4()}").status_code == 404

        # --- submit a correct and an incorrect solution, process them as the runner would
        good = submit("alice", "right-code").json()["submission_id"]
        bad = submit("bob", "wrong-code").json()["submission_id"]
        assert [e["submission_id"] for _, e in producer.sent] == [good, bad]
        assert client.get(f"/submissions/{good}").json()["status"] == "queued"

        async def process():
            pool = await asyncpg.create_pool(DSN)
            for _, event in producer.sent:
                await runner.handle_message(pool, event, run=fake_sandbox(answers))
            for _, event in producer.sent:  # Kafka redelivery must change nothing
                await runner.handle_message(pool, event, run=fake_sandbox(answers))
            await pool.close()

        asyncio.run(process())

        g = client.get(f"/submissions/{good}").json()
        b = client.get(f"/submissions/{bad}").json()
        assert (g["status"], float(g["score"])) == ("completed", 100.0)
        assert (b["status"], float(b["score"])) == ("completed", 50.0)  # "0" is right for case 2 only

        results = client.get(f"/submissions/{good}/results").json()
        assert [r["passed"] for r in results] == [True, True]
        assert [r["passed"] for r in client.get(f"/submissions/{bad}/results").json()] == [False, True]

        audit = [(a["previous_status"], a["new_status"]) for a in client.get(f"/submissions/{good}/audit").json()]
        assert audit == [(None, "queued"), ("queued", "running"), ("running", "completed")]

        # --- leaderboard and history
        board = client.get(f"/leaderboard/{pid}").json()
        assert [(e["user_id"], e["rank"]) for e in board] == [("alice", 1), ("bob", 2)]
        assert [s["id"] for s in client.get("/users/alice/submissions", params={"problem_id": pid}).json()] == [
            good
        ]

        queries = load_module("queries", "services/leaderboard/queries.py")

        async def ranks():
            pool = await asyncpg.create_pool(DSN)
            me = await queries.get_user_rank(pool, pid, "bob")
            ghost = await queries.get_user_rank(pool, pid, "ghost")
            await pool.close()
            return me, ghost

        me, ghost = asyncio.run(ranks())
        assert me["rank"] == 2 and ghost is None


def test_enqueue_failure_marks_submission_failed():
    api = load_module("api_main", "services/api/main.py")

    class Broken:
        def send(self, *a):
            raise RuntimeError("broker down")

    api.get_producer = lambda: Broken()
    pid = f"p-{uuid.uuid4().hex[:8]}"
    with TestClient(api.app) as client:
        client.put("/problems", json={"id": pid, "title": "t", "test_cases": [{"expected_stdout": "x"}]})
        r = client.post(
            "/submissions", json={"user_id": "u", "problem_id": pid, "language": "python", "code": "print(1)"}
        )
        assert r.status_code == 503
        history = client.get("/users/u/submissions", params={"problem_id": pid}).json()
        assert [h["status"] for h in history] == ["failed"]


def test_api_key_enforced(monkeypatch):
    api = load_module("api_main", "services/api/main.py")
    monkeypatch.setattr(api, "API_KEY", "s3cret")
    body = {"id": "k", "title": "t", "test_cases": [{"expected_stdout": "x"}]}
    with TestClient(api.app) as client:
        assert client.put("/problems", json=body).status_code == 401
        assert client.put("/problems", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.put("/problems", json=body, headers={"X-API-Key": "s3cret"}).status_code == 200
        assert client.get("/problems").status_code == 200  # reads stay open


def test_rate_limit_and_protected_reads(monkeypatch):
    api = load_module("api_main", "services/api/main.py")
    producer = FakeProducer()
    api.get_producer = lambda: producer
    monkeypatch.setattr(api, "SUBMIT_RATE_LIMIT", 2)
    pid = f"rl-{uuid.uuid4().hex[:8]}"
    user = f"spammer-{uuid.uuid4().hex[:6]}"
    with TestClient(api.app) as client:
        client.put("/problems", json={"id": pid, "title": "t", "test_cases": [{"expected_stdout": "x"}]})
        body = {"user_id": user, "problem_id": pid, "language": "python", "code": "print(1)"}
        assert [client.post("/submissions", json=body).status_code for _ in range(3)] == [200, 200, 429]
        assert client.post("/submissions", json={**body, "user_id": "someone-else"}).status_code == 200
        assert "Retry-After" in client.post("/submissions", json=body).headers

        monkeypatch.setattr(api, "API_KEY", "k")
        monkeypatch.setattr(api, "PROTECT_READS", True)
        assert client.get("/problems").status_code == 401
        assert client.get("/problems", headers={"X-API-Key": "k"}).status_code == 200
        assert client.get("/health").status_code == 200
