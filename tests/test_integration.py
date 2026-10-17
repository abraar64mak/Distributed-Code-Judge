"""End-to-end checks against a real PostgreSQL (skipped if DATABASE_URL is unreachable).

Kafka and Docker are stubbed: the API's producer is replaced by a recorder and the sandbox result is faked.
"""

import asyncio
import importlib.util
import os
import pathlib

import asyncpg
import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
DSN = os.environ.get("DATABASE_URL", "postgresql://judge:judge@localhost:5432/judge")
os.environ["DATABASE_URL"] = DSN


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


class FakeProducer:
    def __init__(self):
        self.sent = []

    def send(self, topic, value):
        self.sent.append((topic, value))

    def flush(self):
        pass


def test_submit_run_leaderboard_flow():
    api = _load("api_main", "services/api/main.py")
    runner = _load("runner", "services/sandbox-runner/runner.py")
    producer = FakeProducer()
    api.get_producer = lambda: producer

    with TestClient(api.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post(
            "/submissions", json={"user_id": "u", "problem_id": "p", "language": "python", "code": "  "}
        ).status_code == 400

        r = client.post(
            "/submissions", json={"user_id": "u1", "problem_id": "p-flow", "language": "python", "code": "print(1)"}
        )
        assert r.status_code == 200
        sid = r.json()["submission_id"]
        assert producer.sent[0][1]["submission_id"] == sid
        assert client.get(f"/submissions/{sid}").json()["status"] == "queued"
        assert client.get("/submissions/00000000-0000-0000-0000-000000000000").status_code == 404

        async def finish():
            pool = await asyncpg.create_pool(DSN)
            await runner.persist_result(pool, sid, {"status": "completed", "execution_ms": 12})
            await runner.persist_result(pool, sid, {"status": "completed", "execution_ms": 12})  # redelivery
            await pool.close()

        asyncio.run(finish())

        assert client.get(f"/submissions/{sid}").json()["status"] == "completed"
        board = client.get("/leaderboard/p-flow").json()
        assert [e["user_id"] for e in board] == ["u1"]
        assert float(board[0]["best_score"]) == 100.0

        async def audit_and_rank():
            pool = await asyncpg.create_pool(DSN)
            queries = _load("queries", "services/leaderboard/queries.py")
            n = await pool.fetchval("SELECT count(*) FROM submission_audit_log WHERE submission_id = $1", sid)
            top = await queries.get_leaderboard(pool, "p-flow")
            me = await queries.get_user_rank(pool, "p-flow", "u1")
            nobody = await queries.get_user_rank(pool, "p-flow", "ghost")
            await pool.close()
            return n, top, me, nobody

        n, top, me, nobody = asyncio.run(audit_and_rank())
        assert n == 2  # queued + completed; redelivery added nothing
        assert top[0]["rank"] == 1 and me["rank"] == 1 and nobody is None
