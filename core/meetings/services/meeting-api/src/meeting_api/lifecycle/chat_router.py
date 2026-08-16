"""Owner-scoped Google Meet chat command route over the existing acts.v1 Redis bus."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from ..bot_spawn.ports import MeetingRepo
from .stop import leave_command_channel
from .stop_router import CommandPublisher, _resolve_user_id


def build_chat_router(repo: MeetingRepo, publisher: CommandPublisher) -> APIRouter:
    router = APIRouter()

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
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
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

    return router
