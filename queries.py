"""
Leaderboard query service: thin read layer over `leaderboard_cache`,
kept separate from the write-heavy submission API so read traffic can
be scaled and cached independently.
"""

from __future__ import annotations

import os

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://judge:judge@localhost:5432/judge")


async def get_leaderboard(pool: asyncpg.Pool, problem_id: str, limit: int = 50) -> list[dict]:
    """
    Returns the top `limit` entries for a problem, ordered by best score
    descending, earliest achieved_at as tiebreaker.

    Backed by the composite index on (problem_id, best_score DESC, achieved_at ASC)
    defined in db/schema.sql, so this stays fast even with millions of rows.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                user_id,
                best_score,
                achieved_at,
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


async def get_user_rank(pool: asyncpg.Pool, problem_id: str, user_id: str) -> dict | None:
    """Returns a single user's rank and score for a problem, or None if unranked."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH ranked AS (
                SELECT
                    user_id,
                    best_score,
                    achieved_at,
                    RANK() OVER (ORDER BY best_score DESC, achieved_at ASC) AS rank
                FROM leaderboard_cache
                WHERE problem_id = $1
            )
            SELECT * FROM ranked WHERE user_id = $2
            """,
            problem_id,
            user_id,
        )
    return dict(row) if row else None
