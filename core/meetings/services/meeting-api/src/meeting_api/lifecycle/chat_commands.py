"""Durable, owner-bound chat command ledger.

Redis only wakes a bot.  This ledger owns command identity, the pre-DOM claim fence and the
terminal DOM-readback result, so a lost publish, callback or HTTP response never authorizes a
second browser side effect.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol


PENDING_TTL = timedelta(minutes=5)
CLAIM_TTL = timedelta(seconds=30)
_HASH_DOMAIN = b"vexa.chat-command.v2\0"
_CLAIM_DOMAIN = b"vexa.chat-command-claim.v2\0"
TERMINAL_PHASES = frozenset({"confirmed", "failed", "indeterminate", "expired"})


class ChatCommandConflict(Exception):
    pass


class ChatCommandOwnerConflict(ChatCommandConflict):
    pass


class ChatCommandPayloadConflict(ChatCommandConflict):
    pass


class ChatCommandNotFound(Exception):
    pass


class ChatCommandClaimConflict(ChatCommandConflict):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def hash_chat_command(
    *, user_id: int, assignment_id: str, meeting_id: int, platform: str,
    native_meeting_id: str, command_id: str, text: str,
) -> str:
    canonical = json.dumps(
        {
            "assignmentId": assignment_id,
            "commandId": command_id,
            "meetingId": meeting_id,
            "nativeMeetingId": native_meeting_id,
            "platform": platform,
            "text": text,
            "userId": user_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(_HASH_DOMAIN + canonical).hexdigest()


def claim_token_for(
    *, secret: str, command_id: str, payload_hash: str, claimant_id: str,
) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        _CLAIM_DOMAIN + command_id.encode("ascii") + b"\0" + payload_hash.encode("ascii")
        + b"\0" + claimant_id.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


@dataclass
class ChatCommand:
    command_id: str
    user_id: int
    assignment_id: str
    meeting_id: int
    platform: str
    native_meeting_id: str
    payload_hash: str
    text: str
    phase: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    published_at: Optional[datetime] = None
    publish_attempt: int = 0
    publish_lease_token: Optional[str] = None
    publish_lease_until: Optional[datetime] = None
    claimed_at: Optional[datetime] = None
    claim_deadline: Optional[datetime] = None
    claim_token_hash: Optional[str] = None
    claimant_id_hash: Optional[str] = None
    completed_at: Optional[datetime] = None
    result_reason: Optional[str] = None

    def public(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "protocolVersion": 2,
            "commandId": self.command_id,
            "assignmentId": self.assignment_id,
            "meetingId": self.meeting_id,
            "payloadHash": self.payload_hash,
            "status": self.phase,
            "expiresAt": _iso(self.expires_at),
        }
        if self.result_reason is not None:
            body["reason"] = self.result_reason
        if self.completed_at is not None:
            body["completedAt"] = _iso(self.completed_at)
        return body

    def act(self) -> dict[str, Any]:
        return {
            "action": "chat_send_v2",
            "assignmentId": self.assignment_id,
            "commandId": self.command_id,
            "meetingId": self.meeting_id,
            "payloadHash": self.payload_hash,
            "text": self.text,
        }


class ChatCommandLedger(Protocol):
    async def get(self, *, command_id: str) -> Optional[ChatCommand]: ...
    async def reserve(self, *, command: ChatCommand) -> tuple[ChatCommand, bool]: ...
    async def mark_published(self, *, command_id: str) -> ChatCommand: ...
    async def expire_for_terminal_meeting(self, *, command_id: str) -> ChatCommand: ...
    async def claim(
        self, *, command_id: str, meeting_id: int, assignment_id: str,
        payload_hash: str, claim_token_hash: str, claimant_id_hash: str,
    ) -> ChatCommand: ...
    async def complete(
        self, *, command_id: str, meeting_id: int, assignment_id: str,
        payload_hash: str, claim_token_hash: str, phase: str, reason: Optional[str],
    ) -> ChatCommand: ...
    async def pending(self, *, meeting_id: int, assignment_id: str) -> list[ChatCommand]: ...
    async def settle_due(self, *, limit: int = 50) -> int: ...
    async def due_for_publish(self, *, limit: int = 50) -> list[ChatCommand]: ...
    async def complete_publish(self, *, command_id: str, lease_token: str) -> None: ...


class InMemoryChatCommandLedger:
    """Behavioral fake with the same atomic transitions as the PostgreSQL adapter."""

    def __init__(self, *, now=_utcnow):
        self.rows: dict[str, ChatCommand] = {}
        self._lock = asyncio.Lock()
        self._now = now

    def _settle_timeouts(self, row: ChatCommand) -> None:
        now = self._now()
        if row.phase == "pending" and now >= row.expires_at:
            row.phase, row.result_reason, row.completed_at, row.updated_at = (
                "expired", "command_expired_before_claim", now, now,
            )
        elif row.phase == "claimed" and row.claim_deadline and now >= row.claim_deadline:
            row.phase, row.result_reason, row.completed_at, row.updated_at = (
                "indeterminate", "claim_timed_out", now, now,
            )

    async def get(self, *, command_id: str) -> Optional[ChatCommand]:
        async with self._lock:
            row = self.rows.get(command_id)
            if row:
                self._settle_timeouts(row)
            return row

    async def reserve(self, *, command: ChatCommand) -> tuple[ChatCommand, bool]:
        async with self._lock:
            existing = self.rows.get(command.command_id)
            if existing:
                self._settle_timeouts(existing)
                if existing.user_id != command.user_id:
                    raise ChatCommandOwnerConflict(command.command_id)
                if existing.payload_hash != command.payload_hash:
                    raise ChatCommandPayloadConflict(command.command_id)
                return existing, False
            self.rows[command.command_id] = command
            return command, True

    async def mark_published(self, *, command_id: str) -> ChatCommand:
        async with self._lock:
            row = self.rows[command_id]
            if row.published_at is None:
                row.published_at = row.updated_at = self._now()
            row.publish_attempt += 1
            row.publish_lease_token = row.publish_lease_until = None
            return row

    async def expire_for_terminal_meeting(self, *, command_id: str) -> ChatCommand:
        async with self._lock:
            row = self.rows[command_id]
            if row.phase == "pending":
                now = self._now()
                row.phase, row.result_reason, row.completed_at, row.updated_at = (
                    "expired", "meeting_not_active_before_claim", now, now,
                )
            elif row.phase == "claimed":
                now = self._now()
                row.phase, row.result_reason, row.completed_at, row.updated_at = (
                    "indeterminate", "meeting_not_active_after_claim", now, now,
                )
            return row

    def _check_binding(
        self, row: ChatCommand, *, meeting_id: int, assignment_id: str, payload_hash: str,
    ) -> None:
        if (
            row.meeting_id != meeting_id
            or row.assignment_id != assignment_id
            or not hmac.compare_digest(row.payload_hash, payload_hash)
        ):
            raise ChatCommandClaimConflict(row.command_id)

    async def claim(
        self, *, command_id: str, meeting_id: int, assignment_id: str,
        payload_hash: str, claim_token_hash: str, claimant_id_hash: str,
    ) -> ChatCommand:
        async with self._lock:
            row = self.rows.get(command_id)
            if row is None:
                raise ChatCommandNotFound(command_id)
            self._settle_timeouts(row)
            self._check_binding(
                row, meeting_id=meeting_id, assignment_id=assignment_id, payload_hash=payload_hash,
            )
            if row.phase == "pending":
                now = self._now()
                row.phase = "claimed"
                row.claimed_at = row.updated_at = now
                row.claim_deadline = now + CLAIM_TTL
                row.claim_token_hash = claim_token_hash
                row.claimant_id_hash = claimant_id_hash
            elif row.phase == "claimed" and hmac.compare_digest(
                row.claimant_id_hash or "", claimant_id_hash,
            ):
                if not hmac.compare_digest(row.claim_token_hash or "", claim_token_hash):
                    raise ChatCommandClaimConflict(command_id)
            else:
                raise ChatCommandClaimConflict(command_id)
            return row

    async def complete(
        self, *, command_id: str, meeting_id: int, assignment_id: str,
        payload_hash: str, claim_token_hash: str, phase: str, reason: Optional[str],
    ) -> ChatCommand:
        if phase not in {"confirmed", "failed", "indeterminate"}:
            raise ChatCommandClaimConflict(command_id)
        async with self._lock:
            row = self.rows.get(command_id)
            if row is None:
                raise ChatCommandNotFound(command_id)
            self._check_binding(
                row, meeting_id=meeting_id, assignment_id=assignment_id, payload_hash=payload_hash,
            )
            if not hmac.compare_digest(row.claim_token_hash or "", claim_token_hash):
                raise ChatCommandClaimConflict(command_id)
            if row.phase in TERMINAL_PHASES:
                if row.phase != phase or row.result_reason != reason:
                    raise ChatCommandClaimConflict(command_id)
                return row
            if row.phase != "claimed":
                raise ChatCommandClaimConflict(command_id)
            now = self._now()
            row.phase, row.result_reason, row.completed_at, row.updated_at = phase, reason, now, now
            return row

    async def pending(self, *, meeting_id: int, assignment_id: str) -> list[ChatCommand]:
        async with self._lock:
            result: list[ChatCommand] = []
            for row in self.rows.values():
                self._settle_timeouts(row)
                if (
                    row.meeting_id == meeting_id
                    and row.assignment_id == assignment_id
                    and row.phase == "pending"
                ):
                    result.append(row)
            return sorted(result, key=lambda row: (row.created_at, row.command_id))

    async def due_for_publish(self, *, limit: int = 50) -> list[ChatCommand]:
        async with self._lock:
            now = self._now()
            due: list[ChatCommand] = []
            for row in sorted(
                self.rows.values(),
                key=lambda item: (
                    item.publish_attempt,
                    item.published_at is not None,
                    item.published_at or item.created_at,
                    item.created_at,
                    item.command_id,
                ),
            ):
                self._settle_timeouts(row)
                if row.phase != "pending":
                    continue
                if row.published_at is not None and row.published_at > now - timedelta(seconds=2):
                    continue
                if row.publish_lease_until is not None and row.publish_lease_until > now:
                    continue
                row.publish_lease_token = str(uuid.uuid4())
                row.publish_lease_until = now + timedelta(seconds=10)
                row.updated_at = now
                row.publish_attempt += 1
                due.append(row)
                if len(due) >= limit:
                    break
            return due

    async def settle_due(self, *, limit: int = 50) -> int:
        async with self._lock:
            settled = 0
            for row in sorted(self.rows.values(), key=lambda item: (item.created_at, item.command_id)):
                before = row.phase
                self._settle_timeouts(row)
                if row.phase != before:
                    settled += 1
                    if settled >= limit:
                        break
            return settled

    async def complete_publish(self, *, command_id: str, lease_token: str) -> None:
        async with self._lock:
            row = self.rows[command_id]
            if row.publish_lease_token != lease_token:
                return
            now = self._now()
            row.published_at = row.updated_at = now
            row.publish_lease_token = row.publish_lease_until = None


def _model_to_command(row: Any) -> ChatCommand:
    return ChatCommand(
        command_id=row.command_id,
        user_id=row.user_id,
        assignment_id=row.assignment_id,
        meeting_id=row.meeting_id,
        platform=row.platform,
        native_meeting_id=row.native_meeting_id,
        payload_hash=row.payload_hash,
        text=row.text,
        phase=row.phase,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        published_at=row.last_publish_at,
        publish_attempt=row.publish_attempt,
        publish_lease_token=row.publish_lease_token,
        publish_lease_until=row.publish_lease_until,
        claimed_at=row.claimed_at,
        claim_deadline=row.claim_deadline,
        claim_token_hash=row.claim_token_hash,
        claimant_id_hash=row.claimant_id_hash,
        completed_at=row.completed_at,
        result_reason=row.result_reason,
    )


class SqlAlchemyChatCommandLedger:
    """PostgreSQL ledger; every phase decision uses the database clock under a row lock."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def _now(self, db) -> datetime:
        from sqlalchemy import func, select

        return (await db.execute(select(func.clock_timestamp()))).scalar_one()

    async def _locked(self, db, command_id: str):
        from sqlalchemy import select

        from ..sessions.models import BotChatCommand

        return (
            await db.execute(
                select(BotChatCommand)
                .where(BotChatCommand.command_id == command_id)
                .with_for_update()
            )
        ).scalars().first()

    async def _settle_locked(self, db, row, now: datetime) -> None:
        from sqlalchemy import select

        from ..sessions.models import Meeting

        if row.phase == "pending" and now >= row.expires_at:
            row.phase = "expired"
            row.result_reason = "command_expired_before_claim"
            row.completed_at = row.updated_at = now
            return
        if row.phase == "claimed" and row.claim_deadline is not None and now >= row.claim_deadline:
            row.phase = "indeterminate"
            row.result_reason = "claim_timed_out"
            row.completed_at = row.updated_at = now
            return
        if row.phase not in {"pending", "claimed"}:
            return
        status = (
            await db.execute(
                select(Meeting.status)
                .where(Meeting.id == row.meeting_id)
                .with_for_update()
            )
        ).scalars().first()
        if status != "active":
            row.phase = "expired" if row.phase == "pending" else "indeterminate"
            row.result_reason = (
                "meeting_not_active_before_claim"
                if row.phase == "expired"
                else "meeting_not_active_after_claim"
            )
            row.completed_at = row.updated_at = now

    async def get(self, *, command_id: str) -> Optional[ChatCommand]:
        async with self._session_factory() as db:
            async with db.begin():
                row = await self._locked(db, command_id)
                if row is None:
                    return None
                await self._settle_locked(db, row, await self._now(db))
            return _model_to_command(row)

    async def reserve(self, *, command: ChatCommand) -> tuple[ChatCommand, bool]:
        from sqlalchemy import select, text

        from ..sessions.models import BotChatCommand, BotStartRequest, Meeting

        async with self._session_factory() as db:
            async with db.begin():
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:command_id, 0))"),
                    {"command_id": command.command_id},
                )
                existing = await self._locked(db, command.command_id)
                if existing is not None:
                    await self._settle_locked(db, existing, await self._now(db))
                    if existing.user_id != command.user_id:
                        raise ChatCommandOwnerConflict(command.command_id)
                    if existing.payload_hash != command.payload_hash:
                        raise ChatCommandPayloadConflict(command.command_id)
                    return _model_to_command(existing), False
                # Match assignment launch/finalize lock order: durable assignment before meeting.
                assignment = (
                    await db.execute(
                        select(BotStartRequest).where(
                            BotStartRequest.assignment_id == command.assignment_id,
                            BotStartRequest.meeting_id == command.meeting_id,
                            BotStartRequest.user_id == command.user_id,
                            BotStartRequest.phase == "started",
                        ).with_for_update()
                    )
                ).scalars().one_or_none()
                if assignment is None:
                    raise ChatCommandPayloadConflict(command.command_id)
                meeting = (
                    await db.execute(
                        select(Meeting).where(
                            Meeting.id == command.meeting_id,
                            Meeting.user_id == command.user_id,
                            Meeting.platform == command.platform,
                            Meeting.platform_specific_id == command.native_meeting_id,
                        ).with_for_update()
                    )
                ).scalars().one_or_none()
                if meeting is None or meeting.status != "active":
                    raise ChatCommandPayloadConflict(command.command_id)
                now = await self._now(db)
                row = BotChatCommand(
                    command_id=command.command_id,
                    user_id=command.user_id,
                    assignment_id=command.assignment_id,
                    meeting_id=command.meeting_id,
                    platform=command.platform,
                    native_meeting_id=command.native_meeting_id,
                    payload_hash=command.payload_hash,
                    text=command.text,
                    phase="pending",
                    expires_at=now + PENDING_TTL,
                )
                db.add(row)
                await db.flush()
                await db.refresh(row)
                return _model_to_command(row), True

    async def mark_published(self, *, command_id: str) -> ChatCommand:
        async with self._session_factory() as db:
            async with db.begin():
                row = await self._locked(db, command_id)
                if row is None:
                    raise ChatCommandNotFound(command_id)
                now = await self._now(db)
                row.last_publish_at = row.updated_at = now
                row.publish_attempt += 1
                row.publish_lease_token = row.publish_lease_until = None
            return _model_to_command(row)

    async def expire_for_terminal_meeting(self, *, command_id: str) -> ChatCommand:
        async with self._session_factory() as db:
            async with db.begin():
                row = await self._locked(db, command_id)
                if row is None:
                    raise ChatCommandNotFound(command_id)
                now = await self._now(db)
                if row.phase == "pending":
                    row.phase, row.result_reason = "expired", "meeting_not_active_before_claim"
                    row.completed_at = row.updated_at = now
                elif row.phase == "claimed":
                    row.phase, row.result_reason = "indeterminate", "meeting_not_active_after_claim"
                    row.completed_at = row.updated_at = now
            return _model_to_command(row)

    @staticmethod
    def _check_binding(row, *, meeting_id: int, assignment_id: str, payload_hash: str) -> None:
        if (
            row.meeting_id != meeting_id
            or row.assignment_id != assignment_id
            or not hmac.compare_digest(row.payload_hash, payload_hash)
        ):
            raise ChatCommandClaimConflict(row.command_id)

    async def claim(
        self, *, command_id: str, meeting_id: int, assignment_id: str,
        payload_hash: str, claim_token_hash: str, claimant_id_hash: str,
    ) -> ChatCommand:
        async with self._session_factory() as db:
            async with db.begin():
                row = await self._locked(db, command_id)
                if row is None:
                    raise ChatCommandNotFound(command_id)
                now = await self._now(db)
                await self._settle_locked(db, row, now)
                self._check_binding(
                    row, meeting_id=meeting_id, assignment_id=assignment_id,
                    payload_hash=payload_hash,
                )
                if row.phase == "pending":
                    row.phase = "claimed"
                    row.claimed_at = row.updated_at = now
                    row.claim_deadline = now + CLAIM_TTL
                    row.claim_token_hash = claim_token_hash
                    row.claimant_id_hash = claimant_id_hash
                elif row.phase == "claimed" and hmac.compare_digest(
                    row.claimant_id_hash or "", claimant_id_hash,
                ):
                    if not hmac.compare_digest(row.claim_token_hash or "", claim_token_hash):
                        raise ChatCommandClaimConflict(command_id)
                else:
                    raise ChatCommandClaimConflict(command_id)
            return _model_to_command(row)

    async def complete(
        self, *, command_id: str, meeting_id: int, assignment_id: str,
        payload_hash: str, claim_token_hash: str, phase: str, reason: Optional[str],
    ) -> ChatCommand:
        if phase not in {"confirmed", "failed", "indeterminate"}:
            raise ChatCommandClaimConflict(command_id)
        async with self._session_factory() as db:
            async with db.begin():
                row = await self._locked(db, command_id)
                if row is None:
                    raise ChatCommandNotFound(command_id)
                now = await self._now(db)
                if row.phase not in TERMINAL_PHASES:
                    await self._settle_locked(db, row, now)
                self._check_binding(
                    row, meeting_id=meeting_id, assignment_id=assignment_id,
                    payload_hash=payload_hash,
                )
                if not hmac.compare_digest(row.claim_token_hash or "", claim_token_hash):
                    raise ChatCommandClaimConflict(command_id)
                if row.phase in TERMINAL_PHASES:
                    if row.phase != phase or row.result_reason != reason:
                        raise ChatCommandClaimConflict(command_id)
                elif row.phase == "claimed":
                    row.phase, row.result_reason = phase, reason
                    row.completed_at = row.updated_at = now
                else:
                    raise ChatCommandClaimConflict(command_id)
            return _model_to_command(row)

    async def pending(self, *, meeting_id: int, assignment_id: str) -> list[ChatCommand]:
        from sqlalchemy import select

        from ..sessions.models import BotChatCommand

        async with self._session_factory() as db:
            async with db.begin():
                rows = (
                    await db.execute(
                        select(BotChatCommand)
                        .where(
                            BotChatCommand.meeting_id == meeting_id,
                            BotChatCommand.assignment_id == assignment_id,
                            BotChatCommand.phase == "pending",
                        )
                        .order_by(BotChatCommand.created_at, BotChatCommand.command_id)
                        .with_for_update()
                    )
                ).scalars().all()
                now = await self._now(db)
                for row in rows:
                    await self._settle_locked(db, row, now)
                return [_model_to_command(row) for row in rows if row.phase == "pending"]

    async def settle_due(self, *, limit: int = 50) -> int:
        from sqlalchemy import or_, select

        from ..sessions.models import BotChatCommand, Meeting

        async with self._session_factory() as db:
            async with db.begin():
                now = await self._now(db)
                rows = (
                    await db.execute(
                        select(BotChatCommand)
                        .join(Meeting, Meeting.id == BotChatCommand.meeting_id)
                        .where(
                            BotChatCommand.phase.in_(("pending", "claimed")),
                            or_(
                                BotChatCommand.expires_at <= now,
                                BotChatCommand.claim_deadline <= now,
                                Meeting.status != "active",
                            ),
                        )
                        .order_by(BotChatCommand.created_at, BotChatCommand.command_id)
                        .limit(limit)
                        .with_for_update(of=BotChatCommand, skip_locked=True)
                    )
                ).scalars().all()
                settled = 0
                for row in rows:
                    before = row.phase
                    await self._settle_locked(db, row, now)
                    settled += int(row.phase != before)
                return settled

    async def due_for_publish(self, *, limit: int = 50) -> list[ChatCommand]:
        from sqlalchemy import or_, select

        from ..sessions.models import BotChatCommand

        async with self._session_factory() as db:
            async with db.begin():
                now = await self._now(db)
                rows = (
                    await db.execute(
                        select(BotChatCommand)
                        .where(
                            BotChatCommand.phase == "pending",
                            or_(
                                BotChatCommand.publish_lease_until.is_(None),
                                BotChatCommand.publish_lease_until <= now,
                            ),
                            or_(
                                BotChatCommand.last_publish_at.is_(None),
                                BotChatCommand.last_publish_at <= now - timedelta(seconds=2),
                            ),
                        )
                        .order_by(
                            BotChatCommand.publish_attempt,
                            BotChatCommand.last_publish_at.asc().nullsfirst(),
                            BotChatCommand.created_at,
                            BotChatCommand.command_id,
                        )
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
                due = []
                for row in rows:
                    await self._settle_locked(db, row, now)
                    if row.phase != "pending":
                        continue
                    row.publish_lease_token = str(uuid.uuid4())
                    row.publish_lease_until = now + timedelta(seconds=10)
                    row.updated_at = now
                    row.publish_attempt += 1
                    due.append(_model_to_command(row))
                return due

    async def complete_publish(self, *, command_id: str, lease_token: str) -> None:
        async with self._session_factory() as db:
            async with db.begin():
                row = await self._locked(db, command_id)
                if row is None or row.publish_lease_token != lease_token:
                    return
                now = await self._now(db)
                row.last_publish_at = row.updated_at = now
                row.publish_lease_token = row.publish_lease_until = None


async def republish_chat_commands(
    ledger: ChatCommandLedger, publisher: Any, *, limit: int = 50,
) -> tuple[int, int]:
    """One bounded durable-outbox tick. Duplicate publishes are safe; claim is the side-effect fence."""
    sent = failed = 0
    await ledger.settle_due(limit=limit)
    for row in await ledger.due_for_publish(limit=limit):
        try:
            delivered = await publisher.publish(
                f"bot_commands:meeting:{row.meeting_id}",
                json.dumps(row.act(), separators=(",", ":"), sort_keys=True),
            )
            if isinstance(delivered, int) and delivered < 1:
                raise RuntimeError("no bot subscriber accepted the wakeup")
            if row.publish_lease_token is not None:
                await ledger.complete_publish(
                    command_id=row.command_id,
                    lease_token=row.publish_lease_token,
                )
            sent += 1
        except Exception:
            failed += 1
    return sent, failed


def new_chat_command(
    *, command_id: str, user_id: int, assignment_id: str, meeting_id: int,
    platform: str, native_meeting_id: str, text: str, now: Optional[datetime] = None,
) -> ChatCommand:
    created = now or _utcnow()
    payload_hash = hash_chat_command(
        user_id=user_id,
        assignment_id=assignment_id,
        meeting_id=meeting_id,
        platform=platform,
        native_meeting_id=native_meeting_id,
        command_id=command_id,
        text=text,
    )
    return ChatCommand(
        command_id=command_id,
        user_id=user_id,
        assignment_id=assignment_id,
        meeting_id=meeting_id,
        platform=platform,
        native_meeting_id=native_meeting_id,
        payload_hash=payload_hash,
        text=text,
        phase="pending",
        created_at=created,
        updated_at=created,
        expires_at=created + PENDING_TTL,
    )
