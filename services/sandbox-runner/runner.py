"""
Sandbox runner: consumes submission events from Kafka, executes the submitted code against the problem's
test cases inside isolated, resource-limited Docker containers, grades the output, and persists results
back to PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import asyncpg

try:
    import docker
except ImportError:  # pragma: no cover
    docker = None

try:
    from kafka import KafkaConsumer
except ImportError:  # pragma: no cover
    KafkaConsumer = None

log = logging.getLogger("sandbox-runner")

KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://judge:judge@localhost:5432/judge")

LANGUAGE_IMAGES = {
    "python": "python:3.11-slim",
    "javascript": "node:20-slim",
    "java": "eclipse-temurin:21-jdk",
}

# Source and stdin arrive as environment variables, never interpolated into a shell string.
SHELL_COMMANDS = {
    "python": 'printf "%s" "$JUDGE_INPUT" | python3 -B -c "$JUDGE_CODE"',
    "javascript": 'printf "%s" "$JUDGE_INPUT" | node -e "$JUDGE_CODE"',
    "java": (
        'printf "%s" "$JUDGE_CODE" > Main.java && javac Main.java '
        '&& printf "%s" "$JUDGE_INPUT" | java -Xmx128m Main'
    ),
}

SANDBOX_RUNTIME = os.environ.get("SANDBOX_RUNTIME") or None  # e.g. "runsc" for gVisor

DEFAULT_TIMEOUT_SECONDS = 10
MEMORY_LIMITS = {"python": "256m", "javascript": "256m", "java": "512m"}
CPU_LIMIT = 1.0
PIDS_LIMIT = 64
MAX_OUTPUT_BYTES = 64 * 1024


def _truncate(data: bytes) -> str:
    text = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return text + "\n[output truncated]" if len(data) > MAX_OUTPUT_BYTES else text


def run_case(language: str, code: str, stdin: str, timeout: int) -> dict:
    """Run `code` once with `stdin` in a fresh container. Never raises; infrastructure errors are reported."""
    if docker is None:
        raise RuntimeError("docker SDK is not installed")

    start = time.monotonic()
    container = None
    try:
        client = docker.from_env()
        mem = MEMORY_LIMITS[language]
        container = client.containers.run(
            LANGUAGE_IMAGES[language],
            ["sh", "-c", SHELL_COMMANDS[language]],
            detach=True,
            environment={"JUDGE_CODE": code, "JUDGE_INPUT": stdin},
            working_dir="/tmp",
            labels={"judge.sandbox": "1"},
            network_disabled=True,
            mem_limit=mem,
            memswap_limit=mem,
            nano_cpus=int(CPU_LIMIT * 1e9),
            pids_limit=PIDS_LIMIT,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            user="65534:65534",
            runtime=SANDBOX_RUNTIME,
            read_only=True,
            tmpfs={"/tmp": "size=64m,mode=1777,exec"},
        )
        timed_out = False
        try:
            exit_code = container.wait(timeout=timeout).get("StatusCode", 1)
        except Exception:  # noqa: BLE001 - docker-py surfaces wait timeouts as requests exceptions
            timed_out = True
            exit_code = -1
            container.kill()
        stdout = _truncate(container.logs(stdout=True, stderr=False))
        stderr = _truncate(container.logs(stdout=False, stderr=True))
        return {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "ms": int((time.monotonic() - start) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {"exit_code": -1, "stdout": "", "stderr": str(exc), "timed_out": False, "ms": 0, "infra_error": True}
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                log.warning("failed to remove container", exc_info=True)


def normalize(text: str) -> str:
    """Ignore trailing whitespace on each line and trailing blank lines when comparing output."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").rstrip().split("\n"))


def judge(language: str, code: str, cases: list[dict], timeout: int = DEFAULT_TIMEOUT_SECONDS, run=run_case) -> dict:
    """
    Run `code` against every case and grade it. Each case is {"id", "stdin", "expected"}; `expected` may be
    None for an ungraded run. Status is timed_out if any case timed out, failed if any crashed, otherwise
    completed. Score is the percentage of cases whose output matched.
    """
    if language not in LANGUAGE_IMAGES:
        error = f"unsupported language: {language}"
        return {"status": "failed", "score": 0.0, "execution_ms": 0, "cases": [], "error": error}

    cases = cases or [{"id": "run", "stdin": "", "expected": None}]
    graded = []
    for case in cases:
        r = run(language, code, case["stdin"], timeout)
        clean = not r["timed_out"] and r["exit_code"] == 0
        passed = clean and (case["expected"] is None or normalize(r["stdout"]) == normalize(case["expected"]))
        graded.append({"test_case_id": str(case["id"]), "passed": passed, **r})

    if any(g["timed_out"] for g in graded):
        status = "timed_out"
    elif any(g["exit_code"] != 0 for g in graded):
        status = "failed"
    else:
        status = "completed"
    score = round(100.0 * sum(g["passed"] for g in graded) / len(graded), 2)
    return {"status": status, "score": score, "execution_ms": sum(g["ms"] for g in graded), "cases": graded}


async def load_problem(pool: asyncpg.Pool, problem_id: str) -> tuple[int, list[dict]]:
    async with pool.acquire() as conn:
        problem = await conn.fetchrow("SELECT time_limit_s FROM problems WHERE id = $1", problem_id)
        rows = await conn.fetch(
            "SELECT ordinal, stdin, expected_stdout FROM test_cases WHERE problem_id = $1 ORDER BY ordinal",
            problem_id,
        )
    timeout = problem["time_limit_s"] if problem else DEFAULT_TIMEOUT_SECONDS
    return timeout, [{"id": r["ordinal"], "stdin": r["stdin"], "expected": r["expected_stdout"]} for r in rows]


async def mark_running(pool: asyncpg.Pool, submission_id: str) -> bool:
    """queued -> running. False if the submission is unknown or already finished (a redelivered message)."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT status FROM submissions WHERE id = $1 FOR UPDATE", submission_id)
        if row is None or row["status"] not in ("queued", "running"):
            return False
        if row["status"] == "queued":
            await conn.execute("UPDATE submissions SET status = 'running' WHERE id = $1", submission_id)
            await conn.execute(
                "INSERT INTO submission_audit_log (submission_id, previous_status, new_status) "
                "VALUES ($1, 'queued', 'running')",
                submission_id,
            )
        return True


async def persist_result(pool: asyncpg.Pool, submission_id: str, result: dict) -> None:
    """Record a terminal result. Idempotent: a redelivered message for an already-finished submission is a no-op."""
    score = result.get("score", 100.0 if result["status"] == "completed" else 0.0)
    async with pool.acquire() as conn, conn.transaction():
        prev = await conn.fetchrow("SELECT status FROM submissions WHERE id = $1 FOR UPDATE", submission_id)
        if prev is None or prev["status"] not in ("queued", "running"):
            return

        await conn.execute(
            """
            UPDATE submissions
            SET status = $2, score = $3, execution_ms = $4, completed_at = now()
            WHERE id = $1
            """,
            submission_id,
            result["status"],
            score,
            result.get("execution_ms", 0),
        )
        await conn.execute(
            "INSERT INTO submission_audit_log (submission_id, previous_status, new_status) VALUES ($1, $2, $3)",
            submission_id,
            prev["status"],
            result["status"],
        )
        for case in result.get("cases", []):
            await conn.execute(
                """
                INSERT INTO results (submission_id, test_case_id, passed, stdout, stderr, runtime_ms)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                submission_id,
                case["test_case_id"],
                case["passed"],
                case["stdout"],
                case["stderr"],
                case["ms"],
            )
        if result["status"] == "completed" and score > 0:
            await conn.execute(
                """
                INSERT INTO leaderboard_cache (problem_id, user_id, best_score, best_submission_id, achieved_at)
                SELECT problem_id, user_id, score, id, now()
                FROM submissions WHERE id = $1
                ON CONFLICT (problem_id, user_id)
                DO UPDATE SET
                    best_score = EXCLUDED.best_score,
                    best_submission_id = EXCLUDED.best_submission_id,
                    achieved_at = EXCLUDED.achieved_at
                WHERE EXCLUDED.best_score > leaderboard_cache.best_score
                """,
                submission_id,
            )


async def handle_message(pool: asyncpg.Pool, payload: dict, run=run_case) -> None:
    """Process one submission event end to end. Every submission reaches a terminal state, even on errors."""
    submission_id = payload["submission_id"]
    if not await mark_running(pool, submission_id):
        log.info("skipping %s (unknown or already finished)", submission_id)
        return
    try:
        timeout, cases = await load_problem(pool, payload["problem_id"])
        result = await asyncio.to_thread(judge, payload["language"], payload["code"], cases, timeout, run)
    except Exception as exc:  # noqa: BLE001
        log.exception("judging %s crashed", submission_id)
        result = {"status": "failed", "score": 0.0, "execution_ms": 0, "cases": [], "error": str(exc)}
    await persist_result(pool, submission_id, result)


async def consume_loop() -> None:
    if KafkaConsumer is None:
        raise RuntimeError("kafka-python is not installed")

    pool = await asyncpg.create_pool(DATABASE_URL)
    consumer = KafkaConsumer(
        "submissions",
        bootstrap_servers=KAFKA_BROKER,
        group_id="sandbox-runner",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=False,  # commit only after the result is stored => at-least-once
        auto_offset_reset="earliest",
    )
    log.info("consuming from %s", KAFKA_BROKER)
    while True:
        batches = await asyncio.to_thread(consumer.poll, 1000, 1)
        for messages in batches.values():
            for message in messages:
                await handle_message(pool, message.value)
        if batches:
            consumer.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(consume_loop())
