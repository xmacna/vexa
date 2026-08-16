"""Owner-scoped Google Meet chat command route over the existing acts.v1 Redis bus."""
from __future__ import annotations

import json
import base64
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..bot_spawn.ports import MeetingRepo
from ..recordings.service import _verify_meeting_token
from .chat_commands import (
    ChatCommand,
    ChatCommandClaimConflict,
    ChatCommandLedger,
    ChatCommandNotFound,
    ChatCommandOwnerConflict,
    ChatCommandPayloadConflict,
    InMemoryChatCommandLedger,
    claim_token_for,
    hash_chat_command,
    new_chat_command,
    _token_hash,
)
from .stop import leave_command_channel
from .stop_router import CommandPublisher, _resolve_user_id


_RESULT_REASONS = {
    "failed": frozenset({
        "gmeet_chat_unavailable", "chat_destroyed", "composer_not_found", "empty_message",
    }),
    "indeterminate": frozenset({"message_not_observed_after_send"}),
}


def _canonical_uuid(value: str, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a canonical UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise HTTPException(status_code=422, detail=f"{field} must be a canonical UUIDv4")
    return value


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON body must be an object")
    return payload


def _chat_text(payload: dict) -> str:
    if set(payload) != {"text"}:
        raise HTTPException(status_code=422, detail="body must contain only 'text'")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="'text' must be a non-empty string")
    text = text.strip()
    if len(text) > 10_000:
        raise HTTPException(status_code=422, detail="'text' must be at most 10000 characters")
    return text


def _response(row: ChatCommand) -> JSONResponse:
    return JSONResponse(
        status_code=202 if row.phase in {"pending", "claimed"} else 200,
        content=row.public(),
        headers={"Cache-Control": "no-store"},
    )


def _internal_response(content: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"Cache-Control": "no-store"},
    )


def _bearer(authorization: Optional[str], *, token_secret: Optional[str]) -> dict:
    if not token_secret:
        raise HTTPException(status_code=503, detail="chat command authentication unavailable")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[7:]
    try:
        header_part = token.split(".", 1)[0]
        header = json.loads(base64.urlsafe_b64decode(header_part + "=" * (-len(header_part) % 4)))
        claims = _verify_meeting_token(token, secret=token_secret)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    if header != {"alg": "HS256", "typ": "JWT"}:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    if (
        claims.get("iss") != "meeting-api"
        or claims.get("aud") != "transcription-collector"
        or claims.get("scope") != "transcribe:write"
    ):
        raise HTTPException(status_code=401, detail="invalid bearer token")
    return claims


def _internal_binding(payload: dict, claims: dict) -> tuple[int, str, str]:
    allowed = {"meetingId", "assignmentId", "payloadHash"}
    if not allowed.issubset(payload):
        raise HTTPException(status_code=422, detail="missing chat command binding")
    meeting_id = payload.get("meetingId")
    assignment_id = payload.get("assignmentId")
    payload_hash = payload.get("payloadHash")
    if not isinstance(meeting_id, int) or claims.get("meeting_id") != meeting_id:
        raise HTTPException(status_code=403, detail="meeting token binding mismatch")
    if not isinstance(assignment_id, str) or not isinstance(payload_hash, str):
        raise HTTPException(status_code=422, detail="invalid chat command binding")
    _canonical_uuid(assignment_id, "assignmentId")
    if claims.get("session_uid") != assignment_id:
        raise HTTPException(status_code=403, detail="meeting session binding mismatch")
    if len(payload_hash) != 64 or any(ch not in "0123456789abcdef" for ch in payload_hash):
        raise HTTPException(status_code=422, detail="payloadHash must be lowercase SHA-256")
    return meeting_id, assignment_id, payload_hash


def _assert_token_binding(row: ChatCommand, claims: dict) -> None:
    if (
        claims.get("meeting_id") != row.meeting_id
        or claims.get("user_id") != row.user_id
        or claims.get("platform") != row.platform
        or claims.get("native_meeting_id") != row.native_meeting_id
        or claims.get("session_uid") != row.assignment_id
    ):
        raise HTTPException(status_code=403, detail="meeting token binding mismatch")


def build_chat_router(
    repo: MeetingRepo,
    publisher: CommandPublisher,
    *,
    chat_commands: Optional[ChatCommandLedger] = None,
    token_secret: Optional[str] = None,
) -> APIRouter:
    router = APIRouter()
    ledger = chat_commands or InMemoryChatCommandLedger()

    @router.post("/bots/{platform}/{native_meeting_id}/chat")
    async def send_meeting_chat(
        platform: str,
        native_meeting_id: str,
        request: Request,
        x_user_id: Optional[str] = Header(default=None),
    ):
        user_id = _resolve_user_id(x_user_id)
        if platform != "google_meet":
            raise HTTPException(status_code=422, detail="chat send is currently supported for google_meet")
        payload = await _json_object(request)
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="'text' must be a non-empty string")
        text = text.strip()
        if len(text) > 10_000:
            raise HTTPException(status_code=422, detail="'text' must be at most 10000 characters")

        meeting = await repo.find_active(user_id, platform, native_meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="No active meeting for this bot")
        meeting_id = meeting["id"]
        try:
            await publisher.publish(
                leave_command_channel(meeting_id),
                json.dumps({"action": "chat_send", "text": text}),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="chat command bus (redis) unavailable; retry the message",
            ) from exc
        return {"status": "accepted", "meeting_id": meeting_id}

    async def settle_against_meeting(row: ChatCommand) -> ChatCommand:
        latest = await repo.find_latest(row.user_id, row.platform, row.native_meeting_id)
        if latest and latest.get("id") == row.meeting_id and latest.get("status") != "active":
            return await ledger.expire_for_terminal_meeting(command_id=row.command_id)
        return row

    @router.put("/bots/{platform}/{native_meeting_id}/chat/{command_id}")
    async def put_confirmed_chat(
        platform: str,
        native_meeting_id: str,
        command_id: str,
        request: Request,
        x_user_id: Optional[str] = Header(default=None),
    ):
        user_id = _resolve_user_id(x_user_id)
        _canonical_uuid(command_id, "commandId")
        if platform != "google_meet":
            raise HTTPException(status_code=422, detail="chat send is currently supported for google_meet")
        text = _chat_text(await _json_object(request))

        existing = await ledger.get(command_id=command_id)
        if existing is not None:
            if existing.user_id != user_id:
                raise HTTPException(status_code=404, detail="chat command not found")
            if existing.platform != platform or existing.native_meeting_id != native_meeting_id:
                raise HTTPException(
                    status_code=409, detail={"code": "chat_command_payload_conflict"},
                )
            expected = hash_chat_command(
                user_id=user_id,
                assignment_id=existing.assignment_id,
                meeting_id=existing.meeting_id,
                platform=platform,
                native_meeting_id=native_meeting_id,
                command_id=command_id,
                text=text,
            )
            if expected != existing.payload_hash:
                raise HTTPException(
                    status_code=409, detail={"code": "chat_command_payload_conflict"},
                )
            return _response(await settle_against_meeting(existing))

        meeting = await repo.find_active(user_id, platform, native_meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="No active meeting for this bot")
        if meeting.get("status") != "active":
            raise HTTPException(
                status_code=409,
                detail={"code": "confirmed_chat_meeting_not_active"},
            )
        assignment_id = await repo.get_assignment_for_meeting(meeting_id=meeting["id"])
        if assignment_id is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "confirmed_chat_requires_assignment"},
            )
        _canonical_uuid(assignment_id, "assignmentId")
        command = new_chat_command(
            command_id=command_id,
            user_id=user_id,
            assignment_id=assignment_id,
            meeting_id=meeting["id"],
            platform=platform,
            native_meeting_id=native_meeting_id,
            text=text,
        )
        try:
            row, created = await ledger.reserve(command=command)
        except ChatCommandOwnerConflict as exc:
            raise HTTPException(status_code=404, detail="chat command not found") from exc
        except ChatCommandPayloadConflict as exc:
            raise HTTPException(
                status_code=409, detail={"code": "chat_command_payload_conflict"},
            ) from exc
        if created or row.published_at is None:
            try:
                delivered = await publisher.publish(
                    leave_command_channel(row.meeting_id),
                    json.dumps(row.act(), separators=(",", ":"), sort_keys=True),
                )
                if not isinstance(delivered, int) or delivered > 0:
                    row = await ledger.mark_published(command_id=command_id)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="chat command bus unavailable; command remains pending",
                ) from exc
        return _response(row)

    @router.get("/bots/{platform}/{native_meeting_id}/chat/{command_id}")
    async def get_confirmed_chat(
        platform: str,
        native_meeting_id: str,
        command_id: str,
        x_user_id: Optional[str] = Header(default=None),
    ):
        user_id = _resolve_user_id(x_user_id)
        _canonical_uuid(command_id, "commandId")
        row = await ledger.get(command_id=command_id)
        if (
            row is None
            or row.user_id != user_id
            or row.platform != platform
            or row.native_meeting_id != native_meeting_id
        ):
            raise HTTPException(status_code=404, detail="chat command not found")
        return _response(await settle_against_meeting(row))

    @router.get("/internal/chat-commands/pending")
    async def get_pending_chat(
        meeting_id: int = Query(alias="meetingId"),
        assignment_id: str = Query(alias="assignmentId"),
        authorization: Optional[str] = Header(default=None),
    ):
        claims = _bearer(authorization, token_secret=token_secret)
        if claims.get("meeting_id") != meeting_id:
            raise HTTPException(status_code=403, detail="meeting token binding mismatch")
        _canonical_uuid(assignment_id, "assignmentId")
        rows = await ledger.pending(meeting_id=meeting_id, assignment_id=assignment_id)
        pending_rows = []
        for row in rows:
            row = await settle_against_meeting(row)
            if row.phase != "pending":
                continue
            _assert_token_binding(row, claims)
            pending_rows.append(row)
        return _internal_response({
            "protocolVersion": 2,
            "commands": [row.act() for row in pending_rows],
        })

    @router.post("/internal/chat-commands/{command_id}/claim")
    async def claim_chat(
        command_id: str,
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ):
        _canonical_uuid(command_id, "commandId")
        claims = _bearer(authorization, token_secret=token_secret)
        payload = await _json_object(request)
        if set(payload) != {"meetingId", "assignmentId", "payloadHash", "claimantId"}:
            raise HTTPException(status_code=422, detail="invalid claim body")
        meeting_id, assignment_id, payload_hash = _internal_binding(payload, claims)
        claimant_id = payload.get("claimantId")
        if not isinstance(claimant_id, str):
            raise HTTPException(status_code=422, detail="claimantId must be a canonical UUID")
        _canonical_uuid(claimant_id, "claimantId")
        row = await ledger.get(command_id=command_id)
        if row is None:
            raise HTTPException(status_code=404, detail="chat command not found")
        _assert_token_binding(row, claims)
        row = await settle_against_meeting(row)
        if row.phase not in {"pending", "claimed"}:
            raise HTTPException(status_code=409, detail={"code": "chat_command_claim_conflict"})
        token = claim_token_for(
            secret=token_secret or "",
            command_id=command_id,
            payload_hash=payload_hash,
            claimant_id=claimant_id,
        )
        try:
            row = await ledger.claim(
                command_id=command_id,
                meeting_id=meeting_id,
                assignment_id=assignment_id,
                payload_hash=payload_hash,
                claim_token_hash=_token_hash(token),
                claimant_id_hash=_token_hash(claimant_id),
            )
        except ChatCommandNotFound as exc:
            raise HTTPException(status_code=404, detail="chat command not found") from exc
        except ChatCommandClaimConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "chat_command_claim_conflict"}) from exc
        return _internal_response({
            "protocolVersion": 2,
            "commandId": command_id,
            "status": row.phase,
            "claimToken": token,
            "claimDeadline": row.claim_deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "command": row.act(),
        })

    @router.post("/internal/chat-commands/{command_id}/result")
    async def complete_chat(
        command_id: str,
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ):
        _canonical_uuid(command_id, "commandId")
        claims = _bearer(authorization, token_secret=token_secret)
        payload = await _json_object(request)
        required = {"meetingId", "assignmentId", "payloadHash", "claimToken", "status"}
        if not required.issubset(payload) or set(payload) - (required | {"reason"}):
            raise HTTPException(status_code=422, detail="invalid result body")
        meeting_id, assignment_id, payload_hash = _internal_binding(payload, claims)
        row = await ledger.get(command_id=command_id)
        if row is None:
            raise HTTPException(status_code=404, detail="chat command not found")
        _assert_token_binding(row, claims)
        row = await settle_against_meeting(row)
        claim_token = payload.get("claimToken")
        status = payload.get("status")
        reason = payload.get("reason")
        if not isinstance(claim_token, str) or status not in {"confirmed", "failed", "indeterminate"}:
            raise HTTPException(status_code=422, detail="invalid chat result")
        if status == "confirmed":
            if reason is not None:
                raise HTTPException(status_code=422, detail="confirmed result cannot carry a reason")
        elif not isinstance(reason, str) or reason not in _RESULT_REASONS[status]:
            raise HTTPException(status_code=422, detail="invalid result reason")
        try:
            row = await ledger.complete(
                command_id=command_id,
                meeting_id=meeting_id,
                assignment_id=assignment_id,
                payload_hash=payload_hash,
                claim_token_hash=_token_hash(claim_token),
                phase=status,
                reason=reason,
            )
        except ChatCommandNotFound as exc:
            raise HTTPException(status_code=404, detail="chat command not found") from exc
        except ChatCommandClaimConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "chat_command_result_conflict"}) from exc
        return _internal_response(row.public())

    return router
