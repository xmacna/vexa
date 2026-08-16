"""The Backend port — the kernel's one dependency on HOW a workload runs. The kernel orchestrates the
lifecycle; a backend just starts/observes/stops a workload. docker/k8s implement this same Protocol."""
from __future__ import annotations

import hashlib
import json
from typing import Optional, Protocol

from .profiles import Runnable


SPEC_HASH_LABEL = "runtime.spec_hash"
WORKLOAD_CLAIM_ENV = "VEXA_WORKLOAD_CLAIM_HASH"
WORKLOAD_CLAIM_LABEL = "runtime.claim_hash"


def launch_spec_hash(
    runnable: Runnable,
    env: dict[str, str],
    *,
    substrate: Optional[dict] = None,
) -> str:
    """Return a label-safe commitment to the exact launch inputs.

    The digest, rather than the launch document, is stored on the substrate because workload env can
    contain short-lived credentials.  Sorting both the environment and JSON keys makes independent
    runtime replicas derive the same value.  Fifty-two hex characters fit Kubernetes' 63-character
    label-value limit while retaining 208 bits of collision resistance.
    """
    # Assignment retries mint fresh JWT/STT credentials.  Their public request commitment is
    # injected by meeting-api and is the durable equivalence class; hashing ephemeral env would make
    # a legitimate response-loss retry look foreign.  Other workload kinds retain exact-env hashing.
    committed_env = (
        [[WORKLOAD_CLAIM_ENV, env[WORKLOAD_CLAIM_ENV]]]
        if WORKLOAD_CLAIM_ENV in env
        else sorted(env.items())
    )
    document = {
        "image": runnable.image,
        "command": list(runnable.command or []),
        "env": committed_env,
        "substrate": substrate or {},
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:52]


class WorkloadHandle:
    """An opaque, backend-specific handle to a started workload."""
    __slots__ = ("id", "_impl", "started_at")

    def __init__(self, id: str, impl: object, started_at: Optional[str] = None) -> None:
        self.id = id
        self._impl = impl
        self.started_at = started_at


class Backend(Protocol):
    name: str

    def start(self, workload_id: str, runnable: Runnable, env: dict[str, str]) -> WorkloadHandle: ...
    def exit_code(self, h: WorkloadHandle) -> Optional[int]:
        """None while running; the exit code once exited."""
        ...
    def terminate(self, h: WorkloadHandle) -> None:
        """Graceful stop (SIGTERM)."""
        ...
    def kill(self, h: WorkloadHandle) -> None:
        """Force stop (SIGKILL)."""
        ...
    def cleanup(self, h: WorkloadHandle) -> None:
        """Reclaim resources (destroy)."""
        ...
