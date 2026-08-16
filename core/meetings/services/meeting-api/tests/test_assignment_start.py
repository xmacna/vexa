"""Idempotent assignment-scoped bot start over the public meeting-api boundary."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from meeting_api.bot_spawn import build_router
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo


SECRET = "test-admin-token"
HEADERS = {"x-user-id": "7"}
ASSIGNMENT = "11111111-1111-4111-8111-111111111111"
OTHER_ASSIGNMENT = "22222222-2222-4222-8222-222222222222"
BODY = {
    "platform": "google_meet",
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "bot_name": "Xmacna",
    "recording_enabled": True,
    "transcribe_enabled": True,
    "language": "pt",
}


def test_production_lifespan_fails_before_serving_when_assignment_schema_is_missing(monkeypatch):
    import asyncio
    import pytest
    from fastapi import FastAPI
    from meeting_api import database
    from meeting_api.__main__ import _attach_background_loops

    async def missing(_engine):
        raise RuntimeError("bot_start_requests schema is missing")

    monkeypatch.setattr(database, "verify_assignment_schema", missing)
    app = FastAPI()
    _attach_background_loops(
        app, transcript_store=None, segment_bus=None, redis_client=None, engine=object(),
    )

    async def start():
        async with app.router.lifespan_context(app):
            raise AssertionError("lifespan must not start")

    with pytest.raises(RuntimeError, match="schema is missing"):
        asyncio.run(start())


def _client(repo=None, runtime=None, authority=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(
        repo or InMemoryMeetingRepo(), runtime or FakeRuntimeClient(), authority=authority,
    ))
    return TestClient(app)


def _env(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_URL", "https://stt.vexa.ai")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_TOKEN", "tok-test")


def test_assignment_start_first_is_201_and_exact_replay_is_200(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = _client(repo, runtime)

    first = client.put(f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY)
    replay = client.put(f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["bot_container_id"] == first.json()["bot_container_id"]
    assert len(runtime.specs) == 1, "an exact replay must not ask the runtime to spawn again"
    invocation = __import__("json").loads(runtime.specs[0]["env"]["BOT_CONFIG"])
    assert invocation["connectionId"] == ASSIGNMENT
    assert len(repo.sessions) == 1


def test_assignment_start_rejects_conflicting_payload_without_side_effect(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = _client(repo, runtime)
    assert client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 201

    conflict = client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS,
        json={**BODY, "bot_name": "Different"},
    )

    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "assignment_payload_conflict"
    assert len(runtime.specs) == 1
    assert len(repo._meetings) == 1


def test_different_assignment_for_active_meeting_preserves_legacy_conflict(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = _client(repo, runtime)
    assert client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 201

    conflict = client.put(
        f"/bots/assignments/{OTHER_ASSIGNMENT}", headers=HEADERS, json=BODY,
    )

    assert conflict.status_code == 409, conflict.text
    assert len(runtime.specs) == 1
    assert len(repo._meetings) == 1


def test_assignment_id_is_canonical_uuid_and_rejected_before_spawn(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    response = _client(repo, runtime).put(
        "/bots/assignments/not-a-uuid", headers=HEADERS, json=BODY,
    )
    assert response.status_code == 422
    assert runtime.specs == []
    assert repo._meetings == {}


def test_legacy_post_contract_is_unchanged(monkeypatch):
    _env(monkeypatch)
    runtime = FakeRuntimeClient()
    response = _client(runtime=runtime).post("/bots", headers=HEADERS, json=BODY)
    assert response.status_code == 201, response.text
    invocation = __import__("json").loads(runtime.specs[0]["env"]["BOT_CONFIG"])
    assert invocation["connectionId"] != ASSIGNMENT


def test_canonical_hash_is_independent_of_json_key_order(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = _client(repo, runtime)
    assert client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 201
    reversed_body = dict(reversed(list(BODY.items())))
    replay = client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=reversed_body,
    )
    assert replay.status_code == 200, replay.text


def test_assignment_is_globally_bound_to_authenticated_user(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = _client(repo, runtime)
    assert client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 201
    stolen = client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers={"x-user-id": "8"}, json=BODY,
    )
    assert stolen.status_code == 409
    assert stolen.json()["detail"]["code"] == "assignment_owner_conflict"
    assert len(runtime.specs) == 1


def test_assignment_rejects_secret_bearing_fields_before_ledger(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    response = _client(repo, runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS,
        json={**BODY, "passcode": "do-not-store"},
    )
    assert response.status_code == 422
    assert repo.assignment_starts == {}
    assert runtime.specs == []


def test_assignment_rejects_continue_meeting_before_ledger(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    response = _client(repo, runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS,
        json={**BODY, "continue_meeting": True},
    )
    assert response.status_code == 422
    assert repo.assignment_starts == {}
    assert runtime.specs == []


def test_assignment_normalizes_bot_name_for_hash_and_spawn(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = _client(repo, runtime)

    first = client.put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS,
        json={**BODY, "bot_name": "  Xmacna  "},
    )
    replay = client.put(f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY)

    assert first.status_code == 201
    assert replay.status_code == 200
    invocation = __import__("json").loads(runtime.specs[0]["env"]["BOT_CONFIG"])
    assert invocation["botName"] == "Xmacna"


def test_reserved_assignment_with_terminal_workload_is_consumed_without_respawn(monkeypatch):
    _env(monkeypatch)

    class FailOnceFinalizeRepo(InMemoryMeetingRepo):
        failed = False

        async def mark_assignment_started(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("commit response lost")
            return await super().mark_assignment_started(**kwargs)

    repo = FailOnceFinalizeRepo()
    first_runtime = FakeRuntimeClient()
    assert _client(repo, first_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    workload_id = first_runtime.specs[0]["workloadId"]

    terminal_runtime = FakeRuntimeClient(workloads={
        workload_id: {
            "workloadId": workload_id,
            "state": "stopped",
            "exitCode": 0,
            "startedAt": "2026-06-20T09:00:01Z",
        },
    })
    replay = _client(repo, terminal_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )

    assert replay.status_code == 200, replay.text
    assert terminal_runtime.specs == []
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "started"


def test_terminal_meeting_without_accepted_workload_cancels_retry(monkeypatch):
    _env(monkeypatch)

    class FailingRuntime(FakeRuntimeClient):
        async def create_workload(self, spec):
            self.specs.append(spec)
            from meeting_api.bot_spawn.ports import SpawnFailed
            raise SpawnFailed("runtime unavailable")

        async def get_teardown_identity(self, workload_id):
            return {"backend": "docker", "neverStarted": True}

    repo = InMemoryMeetingRepo()
    first_runtime = FailingRuntime()
    assert _client(repo, first_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    meeting_id = repo.assignment_starts[ASSIGNMENT]["meeting_id"]
    repo._meetings[meeting_id]["status"] = "failed"  # stale-request reaper won before retry

    retry_runtime = FakeRuntimeClient(workloads={})
    retry = _client(repo, retry_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )

    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "assignment_terminal"
    assert retry_runtime.specs == []
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "reserved"
    assert repo.assignment_starts[ASSIGNMENT]["last_error_code"] == "runtime_never_started"
    assert repo.assignment_starts[ASSIGNMENT]["teardown_confirmed_at"] is not None


def test_start_failed_runtime_record_is_not_false_success(monkeypatch):
    _env(monkeypatch)

    class FailOnceFinalizeRepo(InMemoryMeetingRepo):
        failed = False

        async def mark_assignment_started(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("response lost")
            return await super().mark_assignment_started(**kwargs)

    repo = FailOnceFinalizeRepo()
    first_runtime = FakeRuntimeClient()
    assert _client(repo, first_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    workload_id = first_runtime.specs[0]["workloadId"]
    retry_runtime = FakeRuntimeClient(workloads={
        workload_id: {
            "workloadId": workload_id,
            "state": "stopped",
            "stopReason": "start_failed",
            "startedAt": None,
        },
    })

    retry = _client(repo, retry_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )

    assert retry.status_code == 200, retry.text
    assert [row[0] for row in retry_runtime.attested_deleted] == [workload_id]
    assert len(retry_runtime.specs) == 1


def test_replay_crash_after_attested_delete_recovers_from_persisted_identity(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    class CrashReleaseRepo(InMemoryMeetingRepo):
        crash_once = True
        finalize_once = True

        async def mark_assignment_started(self, **kwargs):
            if self.finalize_once:
                self.finalize_once = False
                raise RuntimeError("response lost")
            return await super().mark_assignment_started(**kwargs)

        async def release_assignment_launch(self, **kwargs):
            if self.crash_once and kwargs.get("error_code") == "runtime_start_failed":
                self.crash_once = False
                raise RuntimeError("SIGKILL after delete before release")
            return await super().release_assignment_launch(**kwargs)

    repo = CrashReleaseRepo()
    first_runtime = FakeRuntimeClient()
    assert _client(repo, first_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    reservation = repo.assignment_starts[ASSIGNMENT]
    runtime = FakeRuntimeClient(workloads={reservation["workload_id"]: {
        "workloadId": reservation["workload_id"], "state": "stopped",
        "stopReason": "start_failed", "startedAt": None,
    }})
    crashed = _client(repo, runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )
    assert crashed.status_code == 502
    start = repo.assignment_starts[ASSIGNMENT]
    assert start["phase"] == "launching"
    assert start["teardown_identity"].startswith("immutable:")
    assert len(runtime.attested_deleted) == 1

    start["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    restarted = FakeRuntimeClient(workloads={})
    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
    assert asyncio.run(reconcile_assignment_start_sweep(
        repo, restarted, lambda body: _append_async([], body), log=Log(),
    )) == 1
    assert start["phase"] == "reserved"
    assert len(restarted.attested_deleted) == 1


def test_never_started_proof_expires_without_any_substrate_delete(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.bot_spawn.ports import SpawnFailed
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    class NeverStartedRuntime(FakeRuntimeClient):
        async def create_workload(self, spec):
            self.specs.append(spec)
            raise SpawnFailed("kernel rejected before owning a substrate object")
        async def get_teardown_identity(self, workload_id):
            return {"backend": "docker", "neverStarted": True}
        async def delete_workload_attested(self, *args, **kwargs):
            raise AssertionError("never-started proof must not issue DELETE")

    repo, runtime = InMemoryMeetingRepo(), NeverStartedRuntime(workloads={})
    assert _client(repo, runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    start = repo.assignment_starts[ASSIGNMENT]
    assert start["last_error_code"] == "runtime_never_started"
    assert start["teardown_confirmed_at"] is not None
    start["phase_updated_at"] = datetime.now(timezone.utc) - timedelta(seconds=121)

    async def persist_terminal(body):
        repo._meetings[start["meeting_id"]]["status"] = "failed"
        return 200
    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
    assert asyncio.run(reconcile_assignment_start_sweep(
        repo, runtime, persist_terminal, log=Log(),
    )) == 1
    assert start["phase"] == "cancelled"
    assert start["last_error_code"] == "runtime_never_started"


def test_uncertain_spawn_failure_stays_launching_until_runtime_proves_never_started(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.bot_spawn.ports import SpawnFailed
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    class LostFirstProof(FakeRuntimeClient):
        proof_calls = 0
        async def create_workload(self, spec):
            self.specs.append(spec)
            raise SpawnFailed("ambiguous response")
        async def get_teardown_identity(self, workload_id):
            self.proof_calls += 1
            if self.proof_calls == 1:
                raise SpawnFailed("runtime unavailable")
            return {"backend": "docker", "neverStarted": True}
        async def get_workload(self, workload_id):
            return {"workloadId": workload_id, "state": "stopped", "stopReason": "start_failed"}

    repo, runtime = InMemoryMeetingRepo(), LostFirstProof(workloads={})
    assert _client(repo, runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    start = repo.assignment_starts[ASSIGNMENT]
    assert start["phase"] == "launching"
    assert start["teardown_confirmed_at"] is None
    start["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
    assert asyncio.run(reconcile_assignment_start_sweep(
        repo, runtime, lambda body: _append_async([], body), log=Log(),
    )) == 1
    assert start["phase"] == "reserved"
    assert start["last_error_code"] == "runtime_never_started"
    assert start["teardown_confirmed_at"] is not None


def test_never_started_proof_survives_db_failure_and_empty_runtime_restart(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.bot_spawn.ports import SpawnFailed
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    class FailProofPersistOnce(InMemoryMeetingRepo):
        fail_once = True
        async def release_assignment_launch(self, **kwargs):
            if self.fail_once and kwargs.get("never_started"):
                self.fail_once = False
                raise RuntimeError("SIGKILL after proof before DB commit")
            return await super().release_assignment_launch(**kwargs)

    class ProofRuntime(FakeRuntimeClient):
        async def create_workload(self, spec):
            self.specs.append(spec)
            raise SpawnFailed("start rejected")
        async def get_teardown_identity(self, workload_id):
            return {"backend": "docker", "neverStarted": True}

    repo = FailProofPersistOnce()
    with pytest.raises(RuntimeError, match="SIGKILL"):
        _client(repo, ProofRuntime(workloads={})).put(
            f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
        )
    start = repo.assignment_starts[ASSIGNMENT]
    assert start["phase"] == "launching"
    assert start["teardown_confirmed_at"] is None

    # Fresh runtime: empty process store/GET 404, but deterministic substrate probe proves absence.
    start["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    restarted = FakeRuntimeClient(workloads={})
    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
    assert asyncio.run(reconcile_assignment_start_sweep(
        repo, restarted, lambda body: _append_async([], body), log=Log(),
    )) == 1
    assert start["phase"] == "reserved"
    assert start["last_error_code"] == "runtime_never_started"
    assert start["teardown_confirmed_at"] is not None


def test_empty_runtime_restart_cancels_owned_pending_workload_without_respawn(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    repo = InMemoryMeetingRepo()
    reservation = asyncio.run(repo.reserve_assignment_start(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
        platform="google_meet", native_meeting_id="abc-defg-hij", data={},
    ))
    asyncio.run(repo.claim_assignment_launch(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
    ))
    start = repo.assignment_starts[ASSIGNMENT]
    start["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    class RestartedRuntime(FakeRuntimeClient):
        async def probe_claimed_workload(self, workload_id, *, claim_hash):
            return {
                "backend": "k8s", "identity": "owned-pod-uid",
                "state": "starting", "startedAt": None,
            }
    runtime = RestartedRuntime(workloads={})  # GET is 404; only stateless substrate proof remains
    async def persist_terminal(body):
        repo._meetings[reservation["meeting_id"]]["status"] = "failed"
        return 200
    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass

    assert asyncio.run(reconcile_assignment_start_sweep(
        repo, runtime, persist_terminal, log=Log(),
    )) == 1
    assert start["phase"] == "cancelled"
    assert start["teardown_backend"] == "k8s"
    assert start["teardown_identity"] == "owned-pod-uid"
    assert runtime.attested_deleted == [
        (reservation["workload_id"], "k8s", "owned-pod-uid"),
    ]
    assert runtime.specs == []


def test_running_without_started_at_is_not_acceptance_proof(monkeypatch):
    _env(monkeypatch)

    class FailOnceFinalizeRepo(InMemoryMeetingRepo):
        failed = False

        async def mark_assignment_started(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("response lost")
            return await super().mark_assignment_started(**kwargs)

    repo = FailOnceFinalizeRepo()
    first_runtime = FakeRuntimeClient()
    assert _client(repo, first_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    workload_id = first_runtime.specs[0]["workloadId"]
    unproven = FakeRuntimeClient(workloads={
        workload_id: {"workloadId": workload_id, "state": "running", "startedAt": None},
    })

    replay = _client(repo, unproven).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "assignment_in_progress"
    assert unproven.specs == []
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "launching"


def test_terminal_transition_winning_finalize_tears_down_before_cancellation(monkeypatch):
    _env(monkeypatch)

    class TerminalAtFinalizeRepo(InMemoryMeetingRepo):
        async def mark_assignment_started(self, **kwargs):
            start = self.assignment_starts[kwargs["assignment_id"]]
            self._meetings[start["meeting_id"]]["status"] = "failed"
            return await super().mark_assignment_started(**kwargs)

    class OrderedTerminalRepo(TerminalAtFinalizeRepo):
        events = []

        async def begin_assignment_cancel(self, **kwargs):
            self.events.append("fenced")
            return await super().begin_assignment_cancel(**kwargs)

        async def complete_assignment_cancel(self, **kwargs):
            self.events.append("completed")
            return await super().complete_assignment_cancel(**kwargs)

    class OrderedRuntime(FakeRuntimeClient):
        async def delete_workload_attested(self, workload_id, **identity):
            repo.events.append("teardown")
            return await super().delete_workload_attested(workload_id, **identity)

    repo = OrderedTerminalRepo()
    runtime = OrderedRuntime()
    response = _client(repo, runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )

    workload_id = runtime.specs[0]["workloadId"]
    assert response.status_code == 409, response.text
    assert runtime.attested_deleted == [
        (workload_id, "docker", f"immutable:{workload_id}"),
    ]
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "cancelled"
    assert repo.assignment_starts[ASSIGNMENT]["teardown_confirmed_at"] is not None
    assert repo.events == ["fenced", "teardown", "completed"]


def test_expired_launching_with_untracked_runtime_never_respawns(monkeypatch):
    _env(monkeypatch)
    from datetime import datetime, timedelta, timezone

    class FailOnceFinalizeRepo(InMemoryMeetingRepo):
        async def mark_assignment_started(self, **kwargs):
            raise RuntimeError("commit response lost")

    repo = FailOnceFinalizeRepo()
    first_runtime = FakeRuntimeClient()
    assert _client(repo, first_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    ).status_code == 502
    start = repo.assignment_starts[ASSIGNMENT]
    start["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    untracked_runtime = FakeRuntimeClient(workloads={})
    replay = _client(repo, untracked_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "assignment_in_progress"
    assert untracked_runtime.specs == []
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "launching"


def test_reserved_assignment_is_owned_by_bounded_assignment_reconciler(monkeypatch):
    _env(monkeypatch)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    __import__("asyncio").run(repo.reserve_assignment_start(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
        platform="google_meet", native_meeting_id="abc-defg-hij", data={},
    ))
    start = repo.assignment_starts[ASSIGNMENT]
    start["phase"] = "reserved"
    general = __import__("asyncio").run(repo.list_stale_nonterminal(
        stop_grace=0, active_grace=0, preactive_grace=0,
    ))
    assert general == []

    from datetime import datetime, timedelta, timezone
    start["phase_updated_at"] = datetime.now(timezone.utc) - timedelta(seconds=121)
    candidates = __import__("asyncio").run(
        repo.claim_assignment_reconcile_candidates(limit=20)
    )
    assert len(candidates) == 1
    assert candidates[0]["phase"] == "cancel_pending"
    assert candidates[0]["launch_attempt"] == 0


def test_reconciler_cancels_expired_never_launched_without_runtime_delete(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    repo = InMemoryMeetingRepo()
    runtime = FakeRuntimeClient()
    # Reserve directly to model crash after DB commit and before launch lease/runtime I/O.
    reservation = asyncio.run(repo.reserve_assignment_start(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
        platform="google_meet", native_meeting_id="abc-defg-hij", data={},
    ))
    repo.assignment_starts[ASSIGNMENT]["phase_updated_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=121)
    )
    lifecycle = []

    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass

    async def persist_lifecycle(body):
        lifecycle.append(body)
        repo._meetings[reservation["meeting_id"]]["status"] = "failed"
        return 200

    count = asyncio.run(reconcile_assignment_start_sweep(
        repo, runtime,
        persist_lifecycle,
        log=Log(),
    ))

    assert count == 1
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "cancelled"
    assert runtime.deleted == []
    assert lifecycle[0]["connection_id"] == reservation["connection_id"]


def test_cancel_retries_when_lifecycle_ack_did_not_persist_terminal_meeting(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    repo = InMemoryMeetingRepo()
    reservation = asyncio.run(repo.reserve_assignment_start(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
        platform="google_meet", native_meeting_id="abc-defg-hij", data={},
    ))
    launch = asyncio.run(repo.claim_assignment_launch(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
    ))
    repo.assignment_starts[ASSIGNMENT]["lease_until"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    runtime = FakeRuntimeClient(workloads={
        reservation["workload_id"]: {
            "workloadId": reservation["workload_id"],
            "state": "stopped",
            "stopReason": "start_failed",
            "startedAt": None,
        },
    })

    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass

    # Models response loss / swallowed DB failure: lifecycle returns success but meeting stayed requested.
    first = asyncio.run(reconcile_assignment_start_sweep(
        repo, runtime, lambda body: _append_async([], body), log=Log(),
    ))
    assert first == 0
    assert len(runtime.attested_deleted) == 1
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "cancel_pending"
    assert repo.assignment_starts[ASSIGNMENT]["teardown_confirmed_at"] is not None
    assert repo._meetings[reservation["meeting_id"]]["status"] == "requested"

    repo.assignment_starts[ASSIGNMENT]["lease_until"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    async def persist_on_retry(body):
        repo._meetings[reservation["meeting_id"]]["status"] = "failed"
        return 200

    second = asyncio.run(reconcile_assignment_start_sweep(
        repo, runtime, persist_on_retry, log=Log(),
    ))
    assert second == 1
    assert len(runtime.attested_deleted) == 1
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "cancelled"
    assert repo.assignment_starts[ASSIGNMENT]["teardown_confirmed_at"] is not None


def test_cancel_recovers_crash_after_delete_before_teardown_receipt(monkeypatch):
    _env(monkeypatch)
    import asyncio
    from datetime import datetime, timedelta, timezone
    from meeting_api.lifecycle.reconcile import reconcile_assignment_start_sweep

    class CrashAfterDeleteRepo(InMemoryMeetingRepo):
        crash_once = True

        async def record_assignment_teardown(self, **kwargs):
            if self.crash_once:
                self.crash_once = False
                raise RuntimeError("SIGKILL after substrate delete")
            return await super().record_assignment_teardown(**kwargs)

    repo = CrashAfterDeleteRepo()
    reservation = asyncio.run(repo.reserve_assignment_start(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
        platform="google_meet", native_meeting_id="abc-defg-hij", data={},
    ))
    asyncio.run(repo.claim_assignment_launch(
        assignment_id=ASSIGNMENT, user_id=7, request_hash="a" * 64,
    ))
    repo.assignment_starts[ASSIGNMENT]["lease_until"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    runtime_before = FakeRuntimeClient(workloads={
        reservation["workload_id"]: {
            "workloadId": reservation["workload_id"], "state": "stopped",
            "stopReason": "start_failed", "startedAt": None,
        },
    })

    class Log:
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass

    with pytest.raises(RuntimeError, match="SIGKILL"):
        asyncio.run(reconcile_assignment_start_sweep(
            repo, runtime_before, lambda body: _append_async([], body), log=Log(),
        ))
    start = repo.assignment_starts[ASSIGNMENT]
    assert start["phase"] == "cancel_pending"
    assert start["teardown_backend"] == "docker"
    assert start["teardown_identity"].startswith("immutable:")
    assert start["teardown_confirmed_at"] is None

    start["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    runtime_after = FakeRuntimeClient(workloads={})  # restarted runtime lost its in-memory tombstone

    async def persist_terminal(body):
        repo._meetings[reservation["meeting_id"]]["status"] = "failed"
        return 200

    assert asyncio.run(reconcile_assignment_start_sweep(
        repo, runtime_after, persist_terminal, log=Log(),
    )) == 1
    assert len(runtime_after.attested_deleted) == 1
    assert start["phase"] == "cancelled"


async def _append_async(target, value):
    target.append(value)
    return 200


def test_response_loss_reconciles_before_stt_or_authority(monkeypatch):
    _env(monkeypatch)

    class FailOnceFinalizeRepo(InMemoryMeetingRepo):
        failed = False

        async def mark_assignment_started(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("commit response lost")
            return await super().mark_assignment_started(**kwargs)

    class ExplodingAuthority:
        configured = True
        mode = "test"

        async def decide(self, request):
            raise AssertionError("exact replay must not call service authority")

    repo = FailOnceFinalizeRepo()
    first_runtime = FakeRuntimeClient()
    first = _client(repo, first_runtime).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )
    assert first.status_code == 502, first.text
    workload_id = first_runtime.specs[0]["workloadId"]
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "launching"
    assert first_runtime.deleted == [], "recoverable workload must not be compensated away"

    monkeypatch.delenv("TRANSCRIPTION_SERVICE_URL")
    monkeypatch.delenv("TRANSCRIPTION_SERVICE_TOKEN")
    restarted_runtime = FakeRuntimeClient(
        workloads={workload_id: {
            "workloadId": workload_id,
            "state": "running",
            "startedAt": "2026-06-20T09:00:01Z",
        }},
    )
    replay = _client(repo, restarted_runtime, authority=ExplodingAuthority()).put(
        f"/bots/assignments/{ASSIGNMENT}", headers=HEADERS, json=BODY,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["bot_container_id"] == workload_id
    assert restarted_runtime.specs == []
    assert repo.assignment_starts[ASSIGNMENT]["phase"] == "started"
    serialized_ledger = __import__("json").dumps(repo.assignment_starts, default=str)
    assert "tok-test" not in serialized_ledger
    assert "do-not-store" not in serialized_ledger
