"""Docker deterministic-name conflicts are attach-or-fail, never delete."""
from __future__ import annotations

import json
from urllib.parse import unquote, urlparse

import pytest

from runtime_kernel import Runtime, StartFailed, WorkloadSpec
from runtime_kernel.backend import (
    SPEC_HASH_LABEL,
    WORKLOAD_CLAIM_ENV,
    launch_spec_hash,
)
from runtime_kernel.docker_backend import DockerBackend, MANAGED_LABEL, WORKLOAD_ID_LABEL
from runtime_kernel.profiles import Runnable


class _Resp:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _ConflictSession:

    def __init__(self, inspect_body: dict) -> None:
        self.calls: list[str] = []
        self.inspect_body = inspect_body

    def request(self, method: str, url: str, **kw):
        parsed = urlparse(url)
        path = unquote(parsed.path)
        path = path[path.index("/containers"):] if "/containers" in path else path
        q = f"?{parsed.query}" if parsed.query else ""
        self.calls.append(f"{method} {path}{q}")
        if method == "POST" and path == "/containers/create":
            return _Resp(409, {"message": "Conflict. The container name is already in use"})
        if method == "GET" and path in ("/containers/vexa-w9/json", "/containers/cid-winner/json"):
            return _Resp(200, self.inspect_body)
        if method == "DELETE" and path == "/containers/cid-winner":
            return _Resp(204)
        if method == "POST" and path.endswith("/start"):
            self.inspect_body["State"] = {
                "Status": "running",
                "Running": True,
                "StartedAt": "2026-06-20T09:00:01Z",
            }
            return _Resp(304)
        return _Resp(500, {"message": f"unhandled {method} {path}"})


def _attested_body(spec_hash: str, state: str = "running") -> dict:
    return {
        "Id": "cid-winner",
        "Config": {"Labels": {
            MANAGED_LABEL: "true",
            WORKLOAD_ID_LABEL: "w9",
            SPEC_HASH_LABEL: spec_hash,
        }},
        "HostConfig": {},
        "State": {
            "Status": state,
            "Running": state == "running",
            "StartedAt": (
                "2026-06-20T09:00:01Z" if state in ("running", "restarting", "exited")
                else "0001-01-01T00:00:00Z"
            ),
        },
    }


def test_start_attaches_only_to_exact_attested_winner_on_409():
    be = DockerBackend()
    runnable = Runnable(image="alpine", command=["true"])
    fake = _ConflictSession(_attested_body(launch_spec_hash(
        runnable, {}, substrate={"hostConfig": {"ShmSize": 2 * 1024**3}},
    )))
    be._session = fake  # inject the fake socket

    h = be.start("w9", runnable, env={})

    assert h.id == "w9" and h._impl == "cid-winner"
    assert fake.calls == [
        "POST /containers/create?name=vexa-w9",
        "GET /containers/vexa-w9/json",
        "GET /containers/cid-winner/json",
    ]


def test_start_only_starts_an_exact_attested_created_winner():
    runnable = Runnable(image="alpine", command=["true"])
    digest = launch_spec_hash(
        runnable, {}, substrate={"hostConfig": {"ShmSize": 2 * 1024**3}},
    )
    be = DockerBackend()
    fake = _ConflictSession(_attested_body(digest, state="created"))
    be._session = fake

    be.start("w9", runnable, env={})

    assert "POST /containers/cid-winner/start" in fake.calls


def test_start_never_restarts_an_exact_terminal_winner():
    runnable = Runnable(image="alpine", command=["true"])
    digest = launch_spec_hash(
        runnable, {}, substrate={"hostConfig": {"ShmSize": 2 * 1024**3}},
    )
    be = DockerBackend()
    fake = _ConflictSession(_attested_body(digest, state="exited"))
    be._session = fake

    with pytest.raises(RuntimeError, match="terminal"):
        be.start("w9", runnable, env={})

    assert not any(call.endswith("/start") for call in fake.calls)


def test_assignment_claim_hash_ignores_rotated_ephemeral_credentials():
    runnable = Runnable(image="bot")
    first = launch_spec_hash(
        runnable,
        {"VEXA_WORKLOAD_CLAIM_HASH": "a" * 64, "VEXA_BOT_CONFIG": "jwt-one"},
    )
    retry = launch_spec_hash(
        runnable,
        {"VEXA_BOT_CONFIG": "jwt-two", "VEXA_WORKLOAD_CLAIM_HASH": "a" * 64},
    )
    conflict = launch_spec_hash(
        runnable,
        {"VEXA_WORKLOAD_CLAIM_HASH": "b" * 64, "VEXA_BOT_CONFIG": "jwt-two"},
    )

    assert first == retry
    assert conflict != first


def test_foreign_409_then_retry_never_deletes_name_occupant():
    """A failed create persists start_failed, but destroy must re-attest the assignment claim.

    The foreign name occupant deliberately has plausible managed/workload labels; its absent claim
    is the fence that prevents the retry cleanup from reaching it.
    """
    body = {
        "Id": "foreign-cid",
        "Config": {"Labels": {
            MANAGED_LABEL: "true",
            WORKLOAD_ID_LABEL: "w9",
            SPEC_HASH_LABEL: "0" * 52,
        }},
        "HostConfig": {},
        "State": {"Status": "running"},
    }
    be = DockerBackend()
    fake = _ConflictSession(body)
    be._session = fake
    rt = Runtime(backend=be, profiles={"bot": Runnable(image="alpine")})
    spec = WorkloadSpec(
        workloadId="w9",
        profile="bot",
        env={WORKLOAD_CLAIM_ENV: "a" * 64},
    )

    with pytest.raises(StartFailed):
        rt.create(spec)
    rt.destroy("w9")
    with pytest.raises(StartFailed):
        rt.create(spec)

    assert not any(call.startswith("DELETE ") for call in fake.calls)


def test_exact_winner_cleanup_targets_immutable_container_id_not_reused_name():
    runnable = Runnable(image="alpine", command=["true"])
    digest = launch_spec_hash(
        runnable, {}, substrate={"hostConfig": {"ShmSize": 2 * 1024**3}},
    )
    be = DockerBackend()
    fake = _ConflictSession(_attested_body(digest, state="running"))
    be._session = fake
    handle = be.start("w9", runnable, env={})

    be.cleanup(handle)

    assert "DELETE /containers/cid-winner?force=true" in fake.calls
    assert "DELETE /containers/vexa-w9?force=true" not in fake.calls


@pytest.mark.parametrize("mutation", [
    lambda body: body["Config"]["Labels"].update({WORKLOAD_ID_LABEL: "foreign"}),
    lambda body: body["Config"]["Labels"].update({SPEC_HASH_LABEL: "0" * 52}),
    lambda body: body["Config"]["Labels"].pop(MANAGED_LABEL),
])
def test_start_never_deletes_foreign_or_different_spec_on_409(mutation):
    runnable = Runnable(image="alpine", command=["true"])
    body = _attested_body(launch_spec_hash(
        runnable, {}, substrate={"hostConfig": {"ShmSize": 2 * 1024**3}},
    ))
    mutation(body)
    be = DockerBackend()
    fake = _ConflictSession(body)
    be._session = fake

    with pytest.raises(RuntimeError, match="foreign"):
        be.start("w9", runnable, env={})

    assert not any(call.startswith("DELETE ") for call in fake.calls)
    assert fake.calls == ["POST /containers/create?name=vexa-w9", "GET /containers/vexa-w9/json"]
