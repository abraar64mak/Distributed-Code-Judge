"""
Sandbox runner: consumes submission events from Kafka, executes the
submitted code inside an isolated, resource-limited Docker container,
and persists results back to PostgreSQL.
"""

from __future__ import annotations

import json
import os
import time
import uuid

try:
    import docker
except ImportError:  # pragma: no cover
    docker = None

try:
    from kafka import KafkaConsumer, KafkaProducer
except ImportError:  # pragma: no cover
    KafkaConsumer = None
    KafkaProducer = None

import asyncpg
import asyncio

KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://judge:judge@localhost:5432/judge")

LANGUAGE_IMAGES = {
    "python": "python:3.11-slim",
    "javascript": "node:20-slim",
    "java": "eclipse-temurin:21-jdk",
}

DEFAULT_TIMEOUT_SECONDS = 10
MEMORY_LIMIT = "256m"
CPU_LIMIT = 1.0


def run_in_sandbox(language: str, code: str) -> dict:
    """Executes `code` inside an isolated, network-disabled Docker container."""
    if docker is None:
        raise RuntimeError("docker SDK is not installed")

    client = docker.from_env()
    image = LANGUAGE_IMAGES.get(language)
    if image is None:
        return {"status": "failed", "stderr": f"unsupported language: {language}"}

    run_cmd = {
        "python": ["python3", "-c", code],
        "javascript": ["node", "-e", code],
        "java": ["sh", "-c", f'echo "{code}" > Main.java && javac Main.java && java Main'],
    }[language]

    start = time.time()
    try:
        container = client.containers.run(
            image,
            run_cmd,
            detach=True,
            network_disabled=True,
            mem_limit=MEMORY_LIMIT,
            nano_cpus=int(CPU_LIMIT * 1e9),
            read_only=True,
            tmpfs={"/tmp": "size=64m"},
        )
        try:
            result = container.wait(timeout=DEFAULT_TIMEOUT_SECONDS)
            exit_code = result.get("StatusCode", 1)
            logs = container.logs().decode("utf-8", errors="replace")
            status = "completed" if exit_code == 0 else "failed"
        except Exception:
            container.kill()
            status = "timed_out"
            logs = ""
            exit_code = -1
        finally:
            container.remove(force=True)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "stderr": str(exc), "execution_ms": 0}

    execution_ms = int((time.time() - start) * 1000)
    return {
        "status": status,
        "stdout": logs if exit_code == 0 else "",
        "stderr": logs if exit_code != 0 else "",
        "execution_ms": execution_ms,
    }


async def persist_result(pool: asyncpg.Pool, submission_id: str, result: dict) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE submissions
                SET status = $2, execution_ms = $3, completed_at = now()
                WHERE id = $1
                """,
                submission_id,
                result["status"],
                result.get("execution_ms", 0),
            )
            await conn.execute(
                """
                INSERT INTO submission_audit_log (submission_id, previous_status, new_status)
                VALUES ($1, 'queued', $2)
                """,
                submission_id,
                result["status"],
            )

            if result["status"] == "completed":
                await conn.execute(
                    """
                    INSERT INTO leaderboard_cache (problem_id, user_id, best_score, best_submission_id, achieved_at)
                    SELECT problem_id, user_id, 100.0, id, now()
                    FROM submissions WHERE id = $1
                    ON CONFLICT (problem_id, user_id)
                    DO UPDATE SET
                        best_score = GREATEST(leaderboard_cache.best_score, EXCLUDED.best_score),
                        best_submission_id = EXCLUDED.best_submission_id,
                        achieved_at = EXCLUDED.achieved_at
                    WHERE EXCLUDED.best_score >= leaderboard_cache.best_score
                    """,
                    submission_id,
                )


async def consume_loop() -> None:
    if KafkaConsumer is None:
        raise RuntimeError("kafka-python is not installed")

    pool = await asyncpg.create_pool(DATABASE_URL)
    consumer = KafkaConsumer(
        "submissions",
        bootstrap_servers=KAFKA_BROKER,
        group_id="sandbox-runner",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=True,
    )

    for message in consumer:
        payload = message.value
        submission_id = payload["submission_id"]
        result = run_in_sandbox(payload["language"], payload["code"])
        await persist_result(pool, submission_id, result)


if __name__ == "__main__":
    asyncio.run(consume_loop())
