"""Real-PostgreSQL proofs for the confirmed-chat outbox, claim fence and tombstone."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("VEXA_TEST_DATABASE_URL"),
    reason="VEXA_TEST_DATABASE_URL is required for the real-Postgres gate",
)


class CapturePublisher:
    def __init__(self, *, subscribers=1):
        self.subscribers = subscribers
        self.published = []

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        return self.subscribers


async def _fixture(factory):
    from meeting_api.sessions.models import BotStartRequest, Meeting, MeetingSession

    assignment_id = str(uuid.uuid4())
    native_id = f"chat-pg-{uuid.uuid4().hex}"
    async with factory() as db:
        meeting = Meeting(
            user_id=7,
            platform="google_meet",
            platform_specific_id=native_id,
            status="active",
            data={},
        )
        db.add(meeting)
        await db.flush()
        db.add(MeetingSession(meeting_id=meeting.id, session_uid=assignment_id))
        db.add(BotStartRequest(
            assignment_id=assignment_id,
            user_id=7,
            request_hash="a" * 64,
            meeting_id=meeting.id,
            connection_id=assignment_id,
            workload_id=f"mtg-{meeting.id}-{assignment_id[:8]}",
            phase="started",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
        await db.commit()
        return meeting.id, assignment_id, native_id


async def _cleanup(factory, *, meeting_id):
    from sqlalchemy import delete
    from meeting_api.sessions.models import BotChatCommand, BotStartRequest, Meeting, MeetingSession

    async with factory() as db:
        await db.execute(delete(BotChatCommand).where(BotChatCommand.meeting_id == meeting_id))
        await db.execute(delete(MeetingSession).where(MeetingSession.meeting_id == meeting_id))
        await db.execute(delete(BotStartRequest).where(BotStartRequest.meeting_id == meeting_id))
        await db.execute(delete(Meeting).where(Meeting.id == meeting_id))
        await db.commit()


@pytest.mark.asyncio
async def test_chat_schema_migration_is_idempotent_complete_and_secret_free():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from meeting_api.database import ensure_assignment_schema, verify_chat_command_schema
    from meeting_api.sessions.models import Base, BotChatCommand

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(lambda sync: BotChatCommand.__table__.drop(sync, checkfirst=True))
    with pytest.raises(RuntimeError, match="bot_chat_commands schema is missing"):
        await verify_chat_command_schema(engine)
    await ensure_assignment_schema(engine)
    await ensure_assignment_schema(engine)
    await verify_chat_command_schema(engine)
    async with engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("bot_chat_commands")}
        )
    assert not ({"token", "claim_token", "passcode", "bearer", "jwt"} & columns)
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE bot_chat_commands DROP CONSTRAINT ck_bot_chat_command_publish_lease"
        ))
    with pytest.raises(RuntimeError, match="bot_chat_commands schema is incomplete"):
        await verify_chat_command_schema(engine)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: BotChatCommand.__table__.drop(sync, checkfirst=True))
    await ensure_assignment_schema(engine)
    await verify_chat_command_schema(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_twenty_puts_and_claims_converge_and_ack_loss_replays_without_second_publish():
    pytest.importorskip("sqlalchemy")
    httpx = pytest.importorskip("httpx")
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
    from meeting_api.bot_spawn.invocation import mint_meeting_token
    from meeting_api.database import ensure_assignment_schema
    from meeting_api.lifecycle.chat_commands import SqlAlchemyChatCommandLedger
    from meeting_api.lifecycle.chat_router import build_chat_router

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    from meeting_api.sessions.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_assignment_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    meeting_id, assignment_id, native_id = await _fixture(factory)
    command_id = str(uuid.uuid4())
    publisher = CapturePublisher()
    apps = []
    for _ in range(20):
        app = FastAPI()
        app.include_router(build_chat_router(
            SqlAlchemyMeetingRepo(factory),
            publisher,
            chat_commands=SqlAlchemyChatCommandLedger(factory),
            token_secret="secret",
        ))
        apps.append(app)

    async def put(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.put(
                f"/bots/google_meet/{native_id}/chat/{command_id}",
                headers={"x-user-id": "7"}, json={"text": "resposta"},
            )

    puts = await asyncio.gather(*(put(app) for app in apps))
    assert all(response.status_code == 202 for response in puts)
    assert 1 <= len(publisher.published) <= 20
    issued = puts[0].json()
    assert all(response.json() == issued for response in puts)

    token = mint_meeting_token(
        meeting_id,
        7,
        "google_meet",
        native_id,
        secret="secret",
        session_uid=assignment_id,
    )
    binding = {
        "meetingId": meeting_id,
        "assignmentId": assignment_id,
        "payloadHash": issued["payloadHash"],
    }

    async def claim(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.post(
                f"/internal/chat-commands/{command_id}/claim",
                headers={"authorization": f"Bearer {token}"},
                json={**binding, "claimantId": str(uuid.uuid4())},
            )

    claims = await asyncio.gather(*(claim(app) for app in apps))
    winners = [response for response in claims if response.status_code == 200]
    assert len(winners) == 1
    assert sum(response.status_code == 409 for response in claims) == 19
    claim_token = winners[0].json()["claimToken"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps[0]), base_url="http://test",
    ) as client:
        body = {**binding, "claimToken": claim_token, "status": "confirmed"}
        first = await client.post(
            f"/internal/chat-commands/{command_id}/result",
            headers={"authorization": f"Bearer {token}"}, json=body,
        )
        lost_ack_retry = await client.post(
            f"/internal/chat-commands/{command_id}/result",
            headers={"authorization": f"Bearer {token}"}, json=body,
        )
        public = await client.get(
            f"/bots/google_meet/{native_id}/chat/{command_id}",
            headers={"x-user-id": "7"},
        )
    assert first.status_code == 200
    assert lost_ack_retry.json() == first.json()
    assert public.json()["status"] == "confirmed"
    published_before_claim = len(publisher.published)
    assert published_before_claim >= 1

    await _cleanup(factory, meeting_id=meeting_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_committed_pending_republishes_after_restart_and_zero_subscribers():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.database import ensure_assignment_schema
    from meeting_api.lifecycle.chat_commands import (
        SqlAlchemyChatCommandLedger,
        new_chat_command,
        republish_chat_commands,
    )
    from meeting_api.sessions.models import BotChatCommand

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    from meeting_api.sessions.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_assignment_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    meeting_id, assignment_id, native_id = await _fixture(factory)
    command = new_chat_command(
        command_id=str(uuid.uuid4()), user_id=7, assignment_id=assignment_id,
        meeting_id=meeting_id, platform="google_meet", native_meeting_id=native_id, text="x",
    )
    first_process = SqlAlchemyChatCommandLedger(factory)
    await first_process.reserve(command=command)

    no_subscriber = CapturePublisher(subscribers=0)
    assert await republish_chat_commands(first_process, no_subscriber) == (0, 1)
    assert len(no_subscriber.published) == 1

    async with factory() as db:
        await db.execute(
            update(BotChatCommand)
            .where(BotChatCommand.command_id == command.command_id)
            .values(publish_lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await db.commit()
    restarted_process = SqlAlchemyChatCommandLedger(factory)
    recovered = CapturePublisher(subscribers=1)
    assert await republish_chat_commands(restarted_process, recovered) == (1, 0)
    assert len(recovered.published) == 1

    await _cleanup(factory, meeting_id=meeting_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_reserve_rechecks_active_meeting_under_lock_and_never_commits_after_stop():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.database import ensure_assignment_schema
    from meeting_api.lifecycle.chat_commands import (
        ChatCommandPayloadConflict,
        SqlAlchemyChatCommandLedger,
        new_chat_command,
    )
    from meeting_api.sessions.models import Base, BotChatCommand, Meeting

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_assignment_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    meeting_id, assignment_id, native_id = await _fixture(factory)
    command = new_chat_command(
        command_id=str(uuid.uuid4()), user_id=7, assignment_id=assignment_id,
        meeting_id=meeting_id, platform="google_meet", native_meeting_id=native_id, text="x",
    )
    ledger = SqlAlchemyChatCommandLedger(factory)

    async with factory() as stopper:
        async with stopper.begin():
            meeting = (
                await stopper.execute(
                    select(Meeting).where(Meeting.id == meeting_id).with_for_update()
                )
            ).scalar_one()
            meeting.status = "stopping"
            await stopper.flush()
            reserve = asyncio.create_task(ledger.reserve(command=command))
            await asyncio.sleep(0.05)
            assert not reserve.done()
        with pytest.raises(ChatCommandPayloadConflict):
            await reserve

    async with factory() as db:
        assert (
            await db.execute(
                select(BotChatCommand).where(BotChatCommand.command_id == command.command_id)
            )
        ).scalar_one_or_none() is None
    await _cleanup(factory, meeting_id=meeting_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_sweep_settles_claimed_timeout_and_nonactive_meeting_without_get():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.database import ensure_assignment_schema
    from meeting_api.lifecycle.chat_commands import (
        SqlAlchemyChatCommandLedger,
        new_chat_command,
        republish_chat_commands,
    )
    from meeting_api.sessions.models import Base, BotChatCommand, Meeting

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_assignment_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    meeting_id, assignment_id, native_id = await _fixture(factory)
    ledger = SqlAlchemyChatCommandLedger(factory)
    command = new_chat_command(
        command_id=str(uuid.uuid4()), user_id=7, assignment_id=assignment_id,
        meeting_id=meeting_id, platform="google_meet", native_meeting_id=native_id, text="x",
    )
    await ledger.reserve(command=command)
    await ledger.claim(
        command_id=command.command_id,
        meeting_id=meeting_id,
        assignment_id=assignment_id,
        payload_hash=command.payload_hash,
        claim_token_hash="b" * 64,
        claimant_id_hash="c" * 64,
    )
    async with factory() as db:
        await db.execute(
            update(BotChatCommand)
            .where(BotChatCommand.command_id == command.command_id)
            .values(claim_deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await db.commit()

    assert await republish_chat_commands(ledger, CapturePublisher()) == (0, 0)
    timed_out = await ledger.get(command_id=command.command_id)
    assert timed_out is not None
    assert (timed_out.phase, timed_out.result_reason) == ("indeterminate", "claim_timed_out")

    second = new_chat_command(
        command_id=str(uuid.uuid4()), user_id=7, assignment_id=assignment_id,
        meeting_id=meeting_id, platform="google_meet", native_meeting_id=native_id, text="y",
    )
    await ledger.reserve(command=second)
    await ledger.claim(
        command_id=second.command_id,
        meeting_id=meeting_id,
        assignment_id=assignment_id,
        payload_hash=second.payload_hash,
        claim_token_hash="d" * 64,
        claimant_id_hash="e" * 64,
    )
    async with factory() as db:
        await db.execute(update(Meeting).where(Meeting.id == meeting_id).values(status="stopping"))
        await db.commit()
    assert await republish_chat_commands(ledger, CapturePublisher()) == (0, 0)
    stopped = await ledger.get(command_id=second.command_id)
    assert stopped is not None
    assert (stopped.phase, stopped.result_reason) == (
        "indeterminate", "meeting_not_active_after_claim",
    )

    await _cleanup(factory, meeting_id=meeting_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_limit_is_fair_after_failed_first_batch():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.database import ensure_assignment_schema
    from meeting_api.lifecycle.chat_commands import (
        SqlAlchemyChatCommandLedger,
        new_chat_command,
        republish_chat_commands,
    )
    from meeting_api.sessions.models import Base, BotChatCommand

    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_assignment_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    meeting_id, assignment_id, native_id = await _fixture(factory)
    ledger = SqlAlchemyChatCommandLedger(factory)
    commands = [
        new_chat_command(
            command_id=str(uuid.uuid4()), user_id=7, assignment_id=assignment_id,
            meeting_id=meeting_id, platform="google_meet", native_meeting_id=native_id,
            text=f"message-{index}",
        )
        for index in range(51)
    ]
    for command in commands:
        await ledger.reserve(command=command)

    class FailedPublisher:
        def __init__(self):
            self.command_ids = []

        async def publish(self, _channel, payload):
            self.command_ids.append(__import__("json").loads(payload)["commandId"])
            raise RuntimeError("redis unavailable")

    first = FailedPublisher()
    assert await republish_chat_commands(ledger, first, limit=50) == (0, 50)
    assert len(set(first.command_ids)) == 50
    missing = ({command.command_id for command in commands} - set(first.command_ids)).pop()

    async with factory() as db:
        await db.execute(
            update(BotChatCommand)
            .where(BotChatCommand.publish_lease_token.is_not(None))
            .values(
                publish_lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
        await db.commit()
    second = FailedPublisher()
    await republish_chat_commands(ledger, second, limit=50)
    assert second.command_ids[0] == missing

    await _cleanup(factory, meeting_id=meeting_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_assignment_put_is_the_canonical_binding_for_chat_v2(monkeypatch):
    pytest.importorskip("sqlalchemy")
    httpx = pytest.importorskip("httpx")
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meeting_api.bot_spawn import build_router
    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.database import ensure_assignment_schema
    from meeting_api.lifecycle.chat_commands import SqlAlchemyChatCommandLedger
    from meeting_api.lifecycle.chat_router import build_chat_router
    from meeting_api.sessions.models import Base

    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_URL", "https://stt.example")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_TOKEN", "stt-secret")
    engine = create_async_engine(os.environ["VEXA_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_assignment_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = SqlAlchemyMeetingRepo(factory)
    publisher = CapturePublisher()
    app = FastAPI()
    app.include_router(build_router(repo, FakeRuntimeClient()))
    app.include_router(build_chat_router(
        repo, publisher,
        chat_commands=SqlAlchemyChatCommandLedger(factory),
        token_secret="secret",
    ))
    assignment_id = str(uuid.uuid4())
    command_id = str(uuid.uuid4())
    suffix = uuid.uuid4().hex
    letters = "".join(chr(ord("a") + int(ch, 16) % 10) for ch in suffix[:10])
    native_id = f"{letters[:3]}-{letters[3:7]}-{letters[7:10]}"
    meeting_url = f"https://meet.google.com/{native_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        started = await client.put(
            f"/bots/assignments/{assignment_id}", headers={"x-user-id": "7"},
            json={
                "platform": "google_meet", "meeting_url": meeting_url,
                "bot_name": "Xmacna", "recording_enabled": True,
                "transcribe_enabled": True, "language": "pt",
            },
            )
        assert started.status_code == 201, started.text
        async with factory() as db:
            from sqlalchemy import update
            from meeting_api.sessions.models import Meeting
            await db.execute(
                update(Meeting).where(Meeting.id == started.json()["id"]).values(status="active")
            )
            await db.commit()
        issued = await client.put(
            f"/bots/google_meet/{native_id}/chat/{command_id}",
            headers={"x-user-id": "7"}, json={"text": "resposta"},
        )
    assert issued.status_code == 202, issued.text
    assert issued.json()["assignmentId"] == assignment_id
    assert __import__("json").loads(publisher.published[-1][1])["assignmentId"] == assignment_id

    await _cleanup(factory, meeting_id=started.json()["id"])
    await engine.dispose()
