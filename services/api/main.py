"""
FastAPI submission service for the distributed code judge.

Accepts submissions, publishes them to Kafka for async execution by
sandbox-runner workers, and exposes status/leaderboard read endpoints.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    from kafka import KafkaProducer
except ImportError:  # pragma: no cover
    KafkaProducer = None

import asyncpg

app = FastAPI(title="Distributed Code Judge", version="1.0.0")

KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://judge:judge@localhost:5432/judge")

_producer: "KafkaProducer | None" = None
_db_pool = None


class SubmissionRequest(BaseModel):
    user_id: str
    problem_id: str
    language: str
    code: str


class SubmissionResponse(BaseModel):
    submission_id: str
    status: str


def get_producer() -> "KafkaProducer":
    global _producer
    if _producer is None:
        if KafkaProducer is None:
            raise RuntimeError("kafka-python is not installed")
        _producer = KafkaProducer(
            bootstrap_servers=KAFKA_BROKER,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
    return _producer


@app.on_event("startup")
async def startup() -> None:
    global _db_pool
    _db_pool = await asyncpg.create_pool(DATABASE_URL)


@app.on_event("shutdown")
async def shutdown() -> None:
    if _db_pool:
        await _db_pool.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/submissions", response_model=SubmissionResponse)
async def create_submission(req: SubmissionRequest) -> SubmissionResponse:
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="`code` must not be empty")

    submission_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    async with _db_pool.acquire() as conn:
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
            """
            INSERT INTO submission_audit_log (submission_id, previous_status, new_status)
            VALUES ($1, NULL, 'queued')
            """,
            submission_id,
        )

    producer = get_producer()
    producer.send(
        "submissions",
        {
            "submission_id": submission_id,
            "user_id": req.user_id,
            "problem_id": req.problem_id,
            "language": req.language,
            "code": req.code,
        },
    )
    producer.flush()

    return SubmissionResponse(submission_id=submission_id, status="queued")


@app.get("/submissions/{submission_id}")
async def get_submission(submission_id: str) -> dict:
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, status, score, execution_ms, submitted_at, completed_at "
            "FROM submissions WHERE id = $1",
            submission_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return dict(row)


@app.get("/leaderboard/{problem_id}")
async def leaderboard(problem_id: str, limit: int = 50) -> list[dict]:
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, best_score, achieved_at
            FROM leaderboard_cache
            WHERE problem_id = $1
            ORDER BY best_score DESC, achieved_at ASC
            LIMIT $2
            """,
            problem_id,
            limit,
        )
    return [dict(r) for r in rows]
