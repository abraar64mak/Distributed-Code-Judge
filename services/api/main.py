"""
FastAPI submission service for the distributed code judge.

Accepts submissions, publishes them to Kafka for async execution by sandbox-runner workers, and exposes
problem management plus status/result/leaderboard read endpoints.

Set API_KEY to require an `X-API-Key` header on every write endpoint (submissions and problems), and
PROTECT_READS=1 to require it on read endpoints too (everything except /health).
SUBMIT_RATE_LIMIT caps submissions per user_id per minute (default 10, 0 disables).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

try:
    from kafka import KafkaProducer
except ImportError:  # pragma: no cover
    KafkaProducer = None

KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://judge:judge@localhost:5432/judge")
API_KEY = os.environ.get("API_KEY", "")
PROTECT_READS = os.environ.get("PROTECT_READS", "") not in ("", "0", "false")
SUBMIT_RATE_LIMIT = int(os.environ.get("SUBMIT_RATE_LIMIT", "10"))
RATE_WINDOW_S = 60

SUPPORTED_LANGUAGES = {"python", "javascript", "java"}
MAX_CODE_CHARS = 64_000

_producer: KafkaProducer | None = None
_db_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _db_pool
    _db_pool = await asyncpg.create_pool(DATABASE_URL)
    try:
        yield
    finally:
        await _db_pool.close()


app = FastAPI(title="Distributed Code Judge", version="1.1.0", lifespan=lifespan)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if API_KEY and not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def require_read_access(x_api_key: str = Header(default="")) -> None:
    if PROTECT_READS:
        require_api_key(x_api_key)


_recent_submissions: dict[str, deque] = defaultdict(deque)


def check_rate_limit(user_id: str) -> None:
    """Sliding-window limit per user_id. In-memory, so per API process; put a gateway in front for a cluster."""
    if SUBMIT_RATE_LIMIT <= 0:
        return
    now = time.monotonic()
    window = _recent_submissions[user_id]
    while window and now - window[0] > RATE_WINDOW_S:
        window.popleft()
    if len(window) >= SUBMIT_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="too many submissions; slow down",
            headers={"Retry-After": str(int(RATE_WINDOW_S - (now - window[0])) + 1)},
        )
    window.append(now)


class SubmissionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    problem_id: str = Field(min_length=1, max_length=128)
    language: str
    code: str = Field(max_length=MAX_CODE_CHARS)


class SubmissionResponse(BaseModel):
    submission_id: str
    status: str


class TestCaseIn(BaseModel):
    stdin: str = ""
    expected_stdout: str


class ProblemIn(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1, max_length=256)
    time_limit_s: int = Field(default=10, ge=1, le=60)
    test_cases: list[TestCaseIn] = Field(min_length=1, max_length=100)


def get_producer() -> KafkaProducer:
    global _producer
    if _producer is None:
        if KafkaProducer is None:
            raise RuntimeError("kafka-python is not installed")
        _producer = KafkaProducer(
            bootstrap_servers=KAFKA_BROKER,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
    return _producer


def _publish(event: dict) -> None:
    """Block until the broker has acknowledged the event, so a 200 means the submission is really enqueued."""
    get_producer().send("submissions", event).get(timeout=10)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- problems


@app.put("/problems", dependencies=[Depends(require_api_key)])
async def upsert_problem(problem: ProblemIn) -> dict:
    """Create or replace a problem and its test cases."""
    async with _db_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO problems (id, title, time_limit_s) VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, time_limit_s = EXCLUDED.time_limit_s
            """,
            problem.id,
            problem.title,
            problem.time_limit_s,
        )
        await conn.execute("DELETE FROM test_cases WHERE problem_id = $1", problem.id)
        await conn.executemany(
            "INSERT INTO test_cases (problem_id, ordinal, stdin, expected_stdout) VALUES ($1, $2, $3, $4)",
            [(problem.id, i, tc.stdin, tc.expected_stdout) for i, tc in enumerate(problem.test_cases, start=1)],
        )
    return {"id": problem.id, "test_cases": len(problem.test_cases)}


@app.get("/problems", dependencies=[Depends(require_read_access)])
async def list_problems() -> list[dict]:
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.title, p.time_limit_s, count(t.id) AS test_cases
            FROM problems p LEFT JOIN test_cases t ON t.problem_id = p.id
            GROUP BY p.id ORDER BY p.id
            """
        )
    return [dict(r) for r in rows]


@app.get("/problems/{problem_id}", dependencies=[Depends(require_read_access)])
async def get_problem(problem_id: str) -> dict:
    """Public view of a problem; expected outputs are deliberately not exposed."""
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, time_limit_s FROM problems WHERE id = $1", problem_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="problem not found")
        n = await conn.fetchval("SELECT count(*) FROM test_cases WHERE problem_id = $1", problem_id)
    return {**dict(row), "test_cases": n}


# ------------------------------------------------------------- submissions


@app.post("/submissions", response_model=SubmissionResponse, dependencies=[Depends(require_api_key)])
async def create_submission(req: SubmissionRequest) -> SubmissionResponse:
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="`code` must not be empty")
    if req.language not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400, detail=f"unsupported language; choose one of {sorted(SUPPORTED_LANGUAGES)}"
        )
    check_rate_limit(req.user_id)

    submission_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    async with _db_pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM problems WHERE id = $1", req.problem_id):
            raise HTTPException(status_code=404, detail="problem not found")
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO submissions (id, user_id, problem_id, language, code, status, submitted_at)
                VALUES ($1, $2, $3, $4, $5, 'queued', $6)
                """,
                submission_id,
                req.user_id,
                req.problem_id,
                req.language,
                req.code,
                now,
            )
            await conn.execute(
                "INSERT INTO submission_audit_log (submission_id, previous_status, new_status) "
                "VALUES ($1, NULL, 'queued')",
                submission_id,
            )

    event = {
        "submission_id": submission_id,
        "user_id": req.user_id,
        "problem_id": req.problem_id,
        "language": req.language,
        "code": req.code,
    }
    try:
        await asyncio.to_thread(_publish, event)
    except Exception as exc:  # noqa: BLE001
        # Never leave a submission stuck in `queued` if it could not be enqueued.
        async with _db_pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE submissions SET status = 'failed', completed_at = now() WHERE id = $1", submission_id
            )
            await conn.execute(
                "INSERT INTO submission_audit_log (submission_id, previous_status, new_status) "
                "VALUES ($1, 'queued', 'failed')",
                submission_id,
            )
        raise HTTPException(status_code=503, detail="could not enqueue submission") from exc

    return SubmissionResponse(submission_id=submission_id, status="queued")


@app.get("/submissions/{submission_id}", dependencies=[Depends(require_read_access)])
async def get_submission(submission_id: uuid.UUID) -> dict:
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, user_id, problem_id, language, status, score, execution_ms, submitted_at, completed_at "
            "FROM submissions WHERE id = $1",
            submission_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return dict(row)


@app.get("/submissions/{submission_id}/results", dependencies=[Depends(require_read_access)])
async def get_submission_results(submission_id: uuid.UUID) -> list[dict]:
    """Per-test-case outcome, plus the audit trail of status transitions."""
    async with _db_pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM submissions WHERE id = $1", submission_id):
            raise HTTPException(status_code=404, detail="submission not found")
        rows = await conn.fetch(
            "SELECT test_case_id, passed, stdout, stderr, runtime_ms FROM results "
            "WHERE submission_id = $1 ORDER BY test_case_id::int",
            submission_id,
        )
    return [dict(r) for r in rows]


@app.get("/submissions/{submission_id}/audit", dependencies=[Depends(require_read_access)])
async def get_submission_audit(submission_id: uuid.UUID) -> list[dict]:
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT previous_status, new_status, changed_at FROM submission_audit_log "
            "WHERE submission_id = $1 ORDER BY changed_at, id",
            submission_id,
        )
    if not rows:
        raise HTTPException(status_code=404, detail="submission not found")
    return [dict(r) for r in rows]


@app.get("/users/{user_id}/submissions", dependencies=[Depends(require_read_access)])
async def user_submissions(
    user_id: str,
    problem_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, problem_id, language, status, score, submitted_at
            FROM submissions
            WHERE user_id = $1 AND ($2::text IS NULL OR problem_id = $2)
            ORDER BY submitted_at DESC
            LIMIT $3
            """,
            user_id,
            problem_id,
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/leaderboard/{problem_id}", dependencies=[Depends(require_read_access)])
async def leaderboard(problem_id: str, limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, best_score, achieved_at,
                   RANK() OVER (ORDER BY best_score DESC, achieved_at ASC) AS rank
            FROM leaderboard_cache
            WHERE problem_id = $1
            ORDER BY best_score DESC, achieved_at ASC
            LIMIT $2
            """,
            problem_id,
            limit,
        )
    return [dict(r) for r in rows]
