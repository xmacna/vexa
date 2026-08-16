import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo
from meeting_api.lifecycle.chat_router import build_chat_router
from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher


def _client():
    repo = InMemoryMeetingRepo()
    repo._meetings[1] = {
        "id": 1, "user_id": 7, "platform": "google_meet", "native_meeting_id": "abc-defg-hij",
        "status": "active", "data": {},
    }
    publisher = InMemoryCommandPublisher()
    app = FastAPI()
    app.include_router(build_chat_router(repo, publisher))
    return TestClient(app), publisher


def test_chat_send_publishes_acts_v1_command_to_owned_active_meeting():
    client, publisher = _client()
    response = client.post(
        "/bots/google_meet/abc-defg-hij/chat", headers={"x-user-id": "7"}, json={"text": "  resposta  "},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "accepted", "meeting_id": 1}
    assert publisher.published[0][0] == "bot_commands:meeting:1"
    assert json.loads(publisher.published[0][1]) == {"action": "chat_send", "text": "resposta"}


def test_chat_send_enforces_owner_and_nonempty_text():
    client, publisher = _client()
    assert client.post("/bots/google_meet/abc-defg-hij/chat", headers={"x-user-id": "8"}, json={"text": "x"}).status_code == 404
    assert client.post("/bots/google_meet/abc-defg-hij/chat", headers={"x-user-id": "7"}, json={"text": " "}).status_code == 422
    assert publisher.published == []
