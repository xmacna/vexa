"""Real-Postgres assignment ledger concurrency and schema proof.

Set ``VEXA_TEST_DATABASE_URL`` to an isolated disposable database. The ordinary unit suite skips this
file rather than silently substituting SQLite, whose locking semantics cannot prove the contract.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("VEXA_TEST_DATABASE_URL"),
    reason="VEXA_TEST_DATABASE_URL is required for the real-Postgres gate",
)


@pytest.mark.asyncio
async def test_assignment_schema_upgrade_is_idempotent_and_missing_schema_fails_closed():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import create_async_engine

    from meeting_api.database import ensure_assignment_schema, verify_assignment_schema
    from meeting_api.sessions.models import BotStartRequest, Meeting

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: BotStartRequest.__table__.drop(sync, checkfirst=True))
        await conn.run_sync(lambda sync: Meeting.__table__.create(sync, checkfirst=True))

    with pytest.raises(RuntimeError, match="bot_start_requests schema is missing"):
        await verify_assignment_schema(engine)

    await ensure_assignment_schema(engine)
    await ensure_assignment_schema(engine)
    await verify_assignment_schema(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_twenty_replicas_claim_one_launch_lease_and_schema_has_no_secret_columns():
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy import inspect, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
    from meeting_api.bot_spawn.ports import AssignmentInProgress
    from meeting_api.sessions.models import Base, BotStartRequest, Meeting, MeetingSession

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        columns = await conn.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("bot_start_requests")}
        )
    assert not ({"passcode", "token", "stt_token", "request_body"} & columns)

    assignment_id = str(uuid.uuid4())
    native_id = f"pg-{uuid.uuid4().hex}"
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = SqlAlchemyMeetingRepo(factory)
    reservation = await owner.reserve_assignment_start(
        assignment_id=assignment_id,
        user_id=7,
        request_hash="a" * 64,
        platform="google_meet",
        native_meeting_id=native_id,
        data={},
    )

    replicas = [SqlAlchemyMeetingRepo(factory) for _ in range(20)]

    async def claim(repo):
        try:
            return await repo.claim_assignment_launch(
                assignment_id=assignment_id, user_id=7, request_hash="a" * 64,
            )
        except AssignmentInProgress:
            return None

    claims = await asyncio.gather(*(claim(repo) for repo in replicas))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]["phase"] == "launching"
    assert winners[0]["launch_attempt"] == 1
    cancel = await owner.begin_assignment_cancel(
        assignment_id=assignment_id,
        lease_token=winners[0]["lease_token"],
        error_code="runtime_start_failed",
        teardown_backend="docker",
        teardown_identity="immutable-container-id",
    )
    assert cancel["teardown_backend"] == "docker"
    assert cancel["teardown_identity"] == "immutable-container-id"
    assert await owner.list_stale_nonterminal(
        stop_grace=0, active_grace=0, preactive_grace=0,
    ) == [], "operational assignment phases belong exclusively to the assignment reconciler"

    replay = await owner.reserve_assignment_start(
        assignment_id=assignment_id,
        user_id=7,
        request_hash="a" * 64,
        platform="google_meet",
        native_meeting_id=native_id,
        data={},
    )
    assert replay["meeting_id"] == reservation["meeting_id"]
    assert replay["workload_id"] == reservation["workload_id"]

    async with factory() as db:
        start = (await db.execute(
            select(BotStartRequest).where(BotStartRequest.assignment_id == assignment_id)
        )).scalar_one()
        await db.execute(
            sqlalchemy.delete(MeetingSession).where(MeetingSession.meeting_id == start.meeting_id)
        )
        await db.delete(start)
        meeting = (await db.execute(
            select(Meeting).where(Meeting.id == start.meeting_id)
        )).scalar_one()
        await db.delete(meeting)
        await db.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_twenty_http_puts_across_replicas_converge_to_one_runtime_start(monkeypatch):
    pytest.importorskip("sqlalchemy")
    httpx = pytest.importorskip("httpx")
    from fastapi import FastAPI
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.bot_spawn import build_router
    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
    from meeting_api.sessions.models import Base, BotStartRequest, Meeting, MeetingSession

    monkeypatch.setenv("TRANSCRIPTION_SERVICE_URL", "https://stt.vexa.ai")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_TOKEN", "sentinel-not-persisted")
    monkeypatch.setenv("ADMIN_TOKEN", "test-only-meeting-token-secret")
    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class ConvergingRuntime:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.specs = []
            self.workloads = {}

        async def create_workload(self, spec):
            async with self.lock:
                if spec["workloadId"] not in self.workloads:
                    self.specs.append(spec)
                    await asyncio.sleep(0.03)
                    self.workloads[spec["workloadId"]] = {
                        "workloadId": spec["workloadId"],
                        "state": "running",
                        "startedAt": "2026-06-20T09:00:01Z",
                    }
                return dict(self.workloads[spec["workloadId"]])

        async def get_workload(self, workload_id):
            return self.workloads.get(workload_id)

        async def delete_workload(self, workload_id):
            self.workloads.pop(workload_id, None)

    runtime = ConvergingRuntime()
    apps = []
    for _ in range(20):
        app = FastAPI()
        app.include_router(build_router(SqlAlchemyMeetingRepo(factory), runtime))
        apps.append(app)

    assignment_id = str(uuid.uuid4())
    native_id = f"pg-http-{uuid.uuid4().hex}"
    body = {
        "platform": "google_meet",
        "native_meeting_id": native_id,
        "bot_name": "Xmacna",
        "transcribe_enabled": True,
    }

    async def put(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.put(
                f"/bots/assignments/{assignment_id}", headers={"x-user-id": "7"}, json=body,
            )

    first_wave = await asyncio.gather(*(put(app) for app in apps))
    assert sum(response.status_code == 201 for response in first_wave) == 1
    assert {response.status_code for response in first_wave} <= {200, 201, 409}
    retries = await asyncio.gather(*(put(app) for app in apps))
    assert all(response.status_code == 200 for response in retries)
    assert len(runtime.specs) == 1

    async with factory() as db:
        start = (await db.execute(
            select(BotStartRequest).where(BotStartRequest.assignment_id == assignment_id)
        )).scalar_one()
        serialized = repr({key: value for key, value in vars(start).items() if not key.startswith("_")})
        assert "sentinel-not-persisted" not in serialized
        meeting_id = start.meeting_id
        await db.execute(delete(MeetingSession).where(MeetingSession.meeting_id == meeting_id))
        await db.delete(start)
        meeting = (await db.execute(select(Meeting).where(Meeting.id == meeting_id))).scalar_one()
        await db.delete(meeting)
        await db.commit()
    await engine.dispose()
