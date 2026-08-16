"""Release-owned schema gate for the assignment start ledger.

The existing service has no Alembic history.  This module therefore owns the one additive table
introduced by assignment starts, serializes creation with PostgreSQL, and verifies the complete
shape before the HTTP process starts.  It never mutates the older meeting schema.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any


_MIGRATION_LOCK = 5_241_202_608_16
_REQUIRED_COLUMNS = {
    "assignment_id", "user_id", "request_hash", "meeting_id", "connection_id", "workload_id",
    "phase", "phase_updated_at", "lease_token", "lease_until", "launch_attempt", "started_at",
    "teardown_backend", "teardown_identity", "teardown_confirmed_at", "last_error_code",
    "created_at", "updated_at",
}
_REQUIRED_UNIQUES = {"meeting_id", "connection_id", "workload_id"}


async def ensure_assignment_schema(engine: Any) -> None:
    """Create only the additive assignment table/indexes, idempotently and replica-safe."""
    from sqlalchemy import text

    from .sessions.models import BotStartRequest

    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK})
        await connection.run_sync(
            lambda sync: BotStartRequest.__table__.create(sync, checkfirst=True)
        )
    await verify_assignment_schema(engine)


async def verify_assignment_schema(engine: Any) -> None:
    """Fail closed unless the deployed ledger has every correctness-bearing DB primitive."""
    from sqlalchemy import inspect

    def snapshot(connection):
        inspector = inspect(connection)
        if "bot_start_requests" not in inspector.get_table_names():
            return None
        return {
            "columns": {column["name"] for column in inspector.get_columns("bot_start_requests")},
            "pk": set(inspector.get_pk_constraint("bot_start_requests").get("constrained_columns") or []),
            "uniques": {
                tuple(item.get("column_names") or [])
                for item in inspector.get_unique_constraints("bot_start_requests")
            },
            "checks": [item.get("sqltext") or "" for item in inspector.get_check_constraints("bot_start_requests")],
            "indexes": [item for item in inspector.get_indexes("bot_start_requests")],
        }

    async with engine.connect() as connection:
        schema = await connection.run_sync(snapshot)
    if schema is None:
        raise RuntimeError("bot_start_requests schema is missing; run the release migration first")
    missing = _REQUIRED_COLUMNS - schema["columns"]
    unique_columns = {columns[0] for columns in schema["uniques"] if len(columns) == 1}
    reconcile_index = any(
        item.get("column_names") == ["phase", "lease_until", "phase_updated_at"]
        for item in schema["indexes"]
    )
    phase_check = any(
        all(phase in check for phase in ("reserved", "launching", "started", "cancel_pending", "cancelled"))
        for check in schema["checks"]
    )
    identity_check = any(
        "teardown_backend" in check and "teardown_identity" in check
        for check in schema["checks"]
    )
    if (
        missing
        or schema["pk"] != {"assignment_id"}
        or not _REQUIRED_UNIQUES.issubset(unique_columns)
        or not reconcile_index
        or not phase_check
        or not identity_check
    ):
        raise RuntimeError("bot_start_requests schema is incomplete; release migration did not converge")


def _database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    return (
        "postgresql+asyncpg://"
        f"{os.getenv('DB_USER', 'postgres')}:{os.getenv('DB_PASSWORD', 'postgres')}@"
        f"{os.getenv('DB_HOST', 'postgres')}:{os.getenv('DB_PORT', '5432')}/"
        f"{os.getenv('DB_NAME', 'vexa')}"
    )


async def init_db() -> None:
    """Container entrypoint used by the Helm pre-upgrade migration Job."""
    from .db import build_engine

    engine = build_engine(_database_url())
    try:
        await ensure_assignment_schema(engine)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(init_db())


if __name__ == "__main__":
    main()
