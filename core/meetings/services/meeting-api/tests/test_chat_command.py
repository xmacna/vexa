import json
from uuid import UUID
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo
from meeting_api.bot_spawn.invocation import mint_meeting_token
from meeting_api.lifecycle.chat_router import build_chat_router
from meeting_api.lifecycle.chat_commands import InMemoryChatCommandLedger
from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher


class DeliveringPublisher(InMemoryCommandPublisher):
    async def publish(self, channel: str, message: str):
        await super().publish(channel, message)
        return 1


def _client():
    repo = InMemoryMeetingRepo()
    repo._meetings[1] = {
        "id": 1, "user_id": 7, "platform": "google_meet", "native_meeting_id": "abc-defg-hij",
        "status": "active", "data": {},
    }
    publisher = DeliveringPublisher()
    app = FastAPI()
    app.include_router(build_chat_router(repo, publisher))
    return TestClient(app), publisher


COMMAND_ID = "33333333-3333-4333-8333-333333333333"
ASSIGNMENT_ID = "11111111-1111-4111-8111-111111111111"
CLAIMANT_ID = "77777777-7777-4777-8777-777777777777"


def _durable_client(publisher=None):
    repo = InMemoryMeetingRepo()
    repo._meetings[1] = {
        "id": 1, "user_id": 7, "platform": "google_meet", "native_meeting_id": "abc-defg-hij",
        "status": "active", "data": {},
    }
    repo.assignment_starts[ASSIGNMENT_ID] = {
        "assignment_id": ASSIGNMENT_ID,
        "meeting_id": 1,
        "phase": "started",
    }
    publisher = publisher or DeliveringPublisher()
    ledger = InMemoryChatCommandLedger()
    app = FastAPI()
    app.include_router(build_chat_router(repo, publisher, chat_commands=ledger, token_secret="secret"))
    return TestClient(app), repo, publisher, ledger


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


def test_confirmed_chat_put_is_durable_pending_and_exact_replay_does_not_redeliver():
    client, _repo, publisher, _ledger = _durable_client()

    first = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"},
        json={"text": "  resposta  "},
    )
    replay = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"},
        json={"text": "resposta"},
    )
    status = client.get(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"},
    )

    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert status.status_code == 202, status.text
    assert first.json() == replay.json() == status.json()
    assert first.json()["status"] == "pending"
    assert first.json()["commandId"] == COMMAND_ID
    assert first.json()["assignmentId"] == ASSIGNMENT_ID
    assert UUID(first.json()["commandId"])
    assert len(publisher.published) == 1
    assert first.headers["cache-control"] == "no-store"
    assert status.headers["cache-control"] == "no-store"
    _, wire = publisher.published[0]
    decoded = json.loads(wire)
    assert decoded == {
        "action": "chat_send_v2",
        "assignmentId": ASSIGNMENT_ID,
        "commandId": COMMAND_ID,
        "meetingId": 1,
        "payloadHash": first.json()["payloadHash"],
        "text": "resposta",
    }


def test_zero_subscribers_returns_durable_pending_and_keeps_outbox_retryable():
    publisher = InMemoryCommandPublisher()
    client, _repo, _publisher, ledger = _durable_client(publisher)
    response = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert ledger.rows[COMMAND_ID].published_at is None


def test_confirmed_chat_replay_survives_terminal_meeting_and_divergence_conflicts():
    client, repo, publisher, _ledger = _durable_client()
    assert client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    ).status_code == 202
    repo._meetings[1]["status"] = "completed"

    replay = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    )
    conflict = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "outra"},
    )

    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "expired"
    assert replay.json()["reason"] == "meeting_not_active_before_claim"
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "chat_command_payload_conflict"
    assert len(publisher.published) == 1


def test_confirmed_chat_requires_canonical_uuid_v4_on_every_wire_id():
    client, _repo, publisher, ledger = _durable_client()
    version_one = "11111111-1111-1111-8111-111111111111"
    response = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{version_one}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    )
    assert response.status_code == 422
    assert ledger.rows == {}
    assert publisher.published == []


def test_authenticated_claim_and_dom_result_are_exactly_replayable():
    client, _repo, publisher, _ledger = _durable_client()
    issued = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    ).json()
    bearer = mint_meeting_token(
        1, 7, "google_meet", "abc-defg-hij", secret="secret", session_uid=ASSIGNMENT_ID,
    )
    binding = {
        "meetingId": 1,
        "assignmentId": ASSIGNMENT_ID,
        "payloadHash": issued["payloadHash"],
    }
    claim_binding = {**binding, "claimantId": CLAIMANT_ID}

    first_claim = client.post(
        f"/internal/chat-commands/{COMMAND_ID}/claim",
        headers={"authorization": f"Bearer {bearer}"}, json=claim_binding,
    )
    lost_claim_retry = client.post(
        f"/internal/chat-commands/{COMMAND_ID}/claim",
        headers={"authorization": f"Bearer {bearer}"}, json=claim_binding,
    )
    assert first_claim.status_code == 200, first_claim.text
    assert lost_claim_retry.json() == first_claim.json()
    new_boot = client.post(
        f"/internal/chat-commands/{COMMAND_ID}/claim",
        headers={"authorization": f"Bearer {bearer}"},
        json={**binding, "claimantId": "88888888-8888-4888-8888-888888888888"},
    )
    assert new_boot.status_code == 409

    result = {
        **binding,
        "claimToken": first_claim.json()["claimToken"],
        "status": "confirmed",
    }
    first_result = client.post(
        f"/internal/chat-commands/{COMMAND_ID}/result",
        headers={"authorization": f"Bearer {bearer}"}, json=result,
    )
    lost_result_retry = client.post(
        f"/internal/chat-commands/{COMMAND_ID}/result",
        headers={"authorization": f"Bearer {bearer}"}, json=result,
    )
    public = client.get(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}", headers={"x-user-id": "7"},
    )

    assert first_result.status_code == 200, first_result.text
    assert lost_result_retry.json() == first_result.json()
    assert public.json()["status"] == "confirmed"
    assert len(publisher.published) == 1


def test_claim_is_fail_closed_on_token_or_payload_binding_and_pending_remains_drainable():
    client, _repo, _publisher, _ledger = _durable_client()
    issued = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    ).json()
    bearer = mint_meeting_token(
        1, 7, "google_meet", "abc-defg-hij", secret="secret", session_uid=ASSIGNMENT_ID,
    )
    binding = {
        "meetingId": 1,
        "assignmentId": ASSIGNMENT_ID,
        "payloadHash": issued["payloadHash"],
    }
    claim_binding = {**binding, "claimantId": CLAIMANT_ID}

    assert client.post(
        f"/internal/chat-commands/{COMMAND_ID}/claim",
        headers={"authorization": "Bearer invalid"}, json=claim_binding,
    ).status_code == 401
    assert client.post(
        f"/internal/chat-commands/{COMMAND_ID}/claim",
        headers={"authorization": f"Bearer {bearer}"},
        json={**claim_binding, "payloadHash": "0" * 64},
    ).status_code == 409
    pending = client.get(
        f"/internal/chat-commands/pending?meetingId=1&assignmentId={ASSIGNMENT_ID}",
        headers={"authorization": f"Bearer {bearer}"},
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["commands"] == [
        {
            "action": "chat_send_v2",
            "assignmentId": ASSIGNMENT_ID,
            "commandId": COMMAND_ID,
            "meetingId": 1,
            "payloadHash": issued["payloadHash"],
            "text": "resposta",
        }
    ]


@pytest.mark.parametrize("status", ["requested", "joining", "awaiting_admission"])
def test_confirmed_chat_requires_active_meeting_before_creating_ledger(status):
    client, repo, publisher, ledger = _durable_client()
    repo._meetings[1]["status"] = status

    response = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirmed_chat_meeting_not_active"
    assert ledger.rows == {}
    assert publisher.published == []


def test_stopping_meeting_fences_pending_claim_before_dom():
    client, repo, _publisher, _ledger = _durable_client()
    issued = client.put(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}",
        headers={"x-user-id": "7"}, json={"text": "resposta"},
    ).json()
    repo._meetings[1]["status"] = "stopping"
    bearer = mint_meeting_token(
        1, 7, "google_meet", "abc-defg-hij", secret="secret", session_uid=ASSIGNMENT_ID,
    )

    claim = client.post(
        f"/internal/chat-commands/{COMMAND_ID}/claim",
        headers={"authorization": f"Bearer {bearer}"},
        json={
            "meetingId": 1,
            "assignmentId": ASSIGNMENT_ID,
            "payloadHash": issued["payloadHash"],
            "claimantId": CLAIMANT_ID,
        },
    )
    public = client.get(
        f"/bots/google_meet/abc-defg-hij/chat/{COMMAND_ID}", headers={"x-user-id": "7"},
    )

    assert claim.status_code == 409
    assert public.json()["status"] == "expired"
    assert public.json()["reason"] == "meeting_not_active_before_claim"
