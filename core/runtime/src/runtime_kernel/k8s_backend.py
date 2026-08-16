"""K8sBackend — runs a workload as a real Kubernetes Pod (the cluster substrate). Uses the kubectl CLI
via subprocess (no client lib), matching the DockerBackend approach. Implements the same Backend port,
so the kernel's runtime.v1 lifecycle is identical to process/docker. A workload is a bare Pod with
restart=Never; the kernel owns restart policy, so the Pod must not resurrect itself."""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import time
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from .backend import (
    SPEC_HASH_LABEL,
    WORKLOAD_CLAIM_ENV,
    WORKLOAD_CLAIM_LABEL,
    WorkloadHandle,
    launch_spec_hash,
)
from .mounts import k8s_volume_mounts
from .profiles import Runnable

MANAGED_LABEL = "runtime.managed"
WORKLOAD_ID_LABEL = "runtime.workload_id"

# The runtime's OWN scheduling constraints, serialized as JSON by the chart from
# global.tolerations / global.nodeSelector (see deployment-runtime.yaml). A spawned workload is a bare
# `kubectl run` Pod — NOT a Deployment child — so it inherits none of the runtime Deployment's
# scheduling directives; on an all-tainted pool it sits Pending forever and the meeting silently fails.
# These knobs let the spawn override carry the runtime's own constraints so the Pod schedules wherever
# the runtime itself is allowed to run.
TOLERATIONS_ENV = "RUNTIME_K8S_TOLERATIONS"      # JSON array of toleration objects
NODE_SELECTOR_ENV = "RUNTIME_K8S_NODE_SELECTOR"  # JSON object of node-label selectors


def _scheduling_json(env: dict[str, str], key: str, expected: type) -> Optional[object]:
    """Parse one scheduling knob (``key``) from ``env`` as JSON of ``expected`` shape. Unset or empty
    (the chart's default ``[]`` / ``{}`` serialize to ``"[]"`` / ``"{}"``) ⇒ None (no constraint,
    today's behaviour). Malformed JSON or a wrong shape is FATAL (raise) — a scheduling constraint
    silently dropped is exactly the bug this fixes (a stranded Pending Pod, a silent meeting failure),
    so it must fail loud at spawn, never fail open like the workspace mount set."""
    raw = env.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{key} is not valid JSON: {exc}") from exc
    if not isinstance(value, expected):
        raise ValueError(
            f"{key} must be a JSON {expected.__name__}, got {type(value).__name__}: {raw!r}"
        )
    return value or None                                 # empty [] / {} ⇒ treat as unset


def _runtime_scheduling_env() -> dict[str, str]:
    """The runtime's own scheduling knobs from its PROCESS env (set by the chart on the runtime
    Deployment). Overlaid onto the per-workload spawn env for ``pod_overrides`` — spec.env cannot
    carry these: it is built per-workload by different producers (meeting-api for a bot, agent-api for
    an agent worker), whereas the scheduling constraints are a property of the runtime/backend."""
    return {k: os.environ[k] for k in (TOLERATIONS_ENV, NODE_SELECTOR_ENV) if os.environ.get(k)}


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        # Args contain --env=VEXA_BOT_CONFIG=... and therefore credentials.  Keep both argv and
        # stderr out of propagated/logged exceptions; callers only need the bounded operation name.
        operation = args[0] if args else "operation"
        raise RuntimeError(f"kubectl {operation} failed (exit {r.returncode})")
    return r


def _stop_grace_sec() -> int:
    """Graceful-delete window (SIGTERM → SIGKILL). Same env knob as the Docker backend
    (RUNTIME_STOP_GRACE_SEC, default 30) so a live meeting bot can honour SIGTERM — leave the
    meeting, flush, POST its terminal callback (<25s by its own watchdog) — before the kubelet
    SIGKILLs it."""
    try:
        return max(1, int(float(os.getenv("RUNTIME_STOP_GRACE_SEC", "30"))))
    except ValueError:
        return 30


def _pod_started_at(body: dict) -> Optional[str]:
    for status in (body.get("status") or {}).get("containerStatuses") or []:
        state = status.get("state") or {}
        for kind in ("running", "terminated"):
            started_at = (state.get(kind) or {}).get("startedAt")
            if started_at:
                return str(started_at)
    return None


def pod_overrides(env: dict[str, str], *, container_name: str) -> Optional[dict]:
    """The ``kubectl run --overrides`` spec for a spawned Pod, built from the SAME env. It carries two
    independent seams:

      * the workspace store mount set (WP-A1.1): the store PVC (``VEXA_WORKSPACE_MOUNT_SOURCE`` = the
        claim name on k8s) exposes every in-store workspace via per-mount subPath volumeMounts;
      * the runtime's scheduling constraints (``RUNTIME_K8S_TOLERATIONS`` / ``RUNTIME_K8S_NODE_SELECTOR``)
        so the bare ``kubectl run`` Pod — which inherits none of the runtime Deployment's scheduling —
        lands where the runtime itself is allowed to run instead of stranding Pending on a tainted pool.

    The spec is built whenever EITHER seam is present; returns None only when neither is (no override
    needed). Building it for scheduling alone is load-bearing: a plain meeting bot has no workspace PVC,
    so a volumes-only early return would silently drop its tolerations and re-create the bug. Pure/
    env-driven → unit-tested offline (no kubectl)."""
    pvc = env.get("VEXA_WORKSPACE_MOUNT_SOURCE")
    root = env.get("VEXA_WORKSPACE_MOUNT_TARGET")
    volumes, volume_mounts = k8s_volume_mounts(env, pvc_name=pvc or "", store_target=root or "")
    tolerations = _scheduling_json(env, TOLERATIONS_ENV, list)
    node_selector = _scheduling_json(env, NODE_SELECTOR_ENV, dict)
    if not volumes and not tolerations and not node_selector:
        return None
    # ``kubectl run --overrides`` merges the containers LIST by replacement (json-merge, not
    # strategic), so a containers entry here wipes the generated container — image, env, command —
    # and the API server rejects the Pod (`spec.containers[0].image: Required value`), killing the
    # spawn instantly. Emit ``containers`` ONLY when volumeMounts force it (the workspace-store
    # seam); pod-level fields (tolerations/nodeSelector) merge fine without touching the list.
    spec: dict = {}
    if volume_mounts:
        spec["containers"] = [{"name": container_name, "volumeMounts": volume_mounts}]
    if volumes:
        spec["volumes"] = volumes
    if tolerations:
        spec["tolerations"] = tolerations
    if node_selector:
        spec["nodeSelector"] = node_selector
    return {"spec": spec}


class K8sBackend:
    name = "k8s"

    def __init__(self, name_prefix: str = "vexa-", namespace: Optional[str] = None) -> None:
        self._prefix = name_prefix
        self._ns = namespace

    def _pname(self, workload_id: str) -> str:
        return f"{self._prefix}{workload_id}"            # must be DNS-1123 (lowercase alnum + '-')

    def _ns_args(self) -> list[str]:
        return ["-n", self._ns] if self._ns else []

    def _pod_namespace(self) -> str:
        if self._ns:
            return self._ns
        path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
        try:
            namespace = open(path, encoding="utf-8").read().strip()
        except OSError as exc:
            raise RuntimeError("kubernetes namespace unavailable for UID-fenced delete") from exc
        if not namespace:
            raise RuntimeError("kubernetes namespace unavailable for UID-fenced delete")
        return namespace

    def _delete_pod_uid(self, name: str, uid: str) -> int:
        """DELETE one exact Pod UID through the Kubernetes API DeleteOptions precondition."""
        host = os.getenv("KUBERNETES_SERVICE_HOST")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        if not host:
            raise RuntimeError("kubernetes API unavailable for UID-fenced delete")
        try:
            token = open(token_path, encoding="utf-8").read().strip()
        except OSError as exc:
            raise RuntimeError("kubernetes service-account token unavailable") from exc
        namespace = self._pod_namespace()
        url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods/{name}"
        body = json.dumps({
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "gracePeriodSeconds": 0,
            "propagationPolicy": "Background",
            "preconditions": {"uid": uid},
        }).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=body,
            method="DELETE",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        context = ssl.create_default_context(cafile=ca_path)
        try:
            with urllib_request.urlopen(req, context=context, timeout=10) as response:
                return int(response.status)
        except urllib_error.HTTPError as exc:
            return int(exc.code)

    @staticmethod
    def _handle_name(h: WorkloadHandle) -> str:
        impl = h._impl  # type: ignore[attr-defined]
        return str(impl["name"] if isinstance(impl, dict) else impl)

    def _confirmed_pod(self, name: str, initial: Optional[dict] = None) -> tuple[dict, str]:
        deadline = time.monotonic() + max(
            0.1, float(os.getenv("RUNTIME_K8S_START_CONFIRM_TIMEOUT_SEC", "30")),
        )
        body = initial
        while True:
            if body is None:
                current = _kubectl("get", "pod", name, "-o", "json", *self._ns_args(), check=False)
                if current.returncode == 0:
                    try:
                        body = json.loads(current.stdout)
                    except (TypeError, json.JSONDecodeError):
                        body = None
            if body is not None:
                started_at = _pod_started_at(body)
                if started_at:
                    return body, started_at
                if (body.get("status") or {}).get("phase") in ("Failed", "Succeeded"):
                    raise RuntimeError("kubernetes workload became terminal without start proof")
            if time.monotonic() >= deadline:
                raise RuntimeError("kubernetes workload start timestamp is unconfirmed")
            body = None
            time.sleep(0.05)

    def start(self, workload_id: str, runnable: Runnable, env: dict[str, str]) -> WorkloadHandle:
        if not runnable.image:
            raise ValueError("k8s backend requires an image")
        name = self._pname(workload_id)
        args = [
            "run", name, f"--image={runnable.image}", "--restart=Never",
            # Adoption labels (the orphaned-live-bot fix): a recreated runtime re-discovers its
            # still-running Pods by this label pair and re-registers them (see the kernel's adopt()).
            f"--labels={MANAGED_LABEL}=true,{WORKLOAD_ID_LABEL}={workload_id}",
            *self._ns_args(),
        ]
        for k, v in env.items():
            args += [f"--env={k}={v}"]
        # The --overrides spec carries the workspace mount set (WP-A1.1: the store PVC bound per-mount,
        # container name = Pod name for a `run` Pod) AND the runtime's own scheduling constraints. The
        # latter live in the runtime's PROCESS env (the chart sets them on the runtime Deployment), not
        # in the per-workload spec.env, so overlay them here; the workload's own --env (above) is left
        # untouched — scheduling shapes the Pod, it is not container config.
        overrides = pod_overrides({**env, **_runtime_scheduling_env()}, container_name=name)
        if overrides:
            args += ["--overrides", json.dumps(overrides)]
        if runnable.command:
            args += ["--command", "--", *runnable.command]
        spec_hash = launch_spec_hash(
            runnable,
            env,
            substrate={"overrides": overrides or {}},
        )
        label_index = next(i for i, value in enumerate(args) if value.startswith("--labels="))
        args[label_index] += f",{SPEC_HASH_LABEL}={spec_hash}"
        if WORKLOAD_CLAIM_ENV in env:
            args[label_index] += f",{WORKLOAD_CLAIM_LABEL}={env[WORKLOAD_CLAIM_ENV][:52]}"
        attested_identity: Optional[dict] = None
        created = _kubectl(*args, check=False)
        if created.returncode != 0:
            existing = _kubectl("get", "pod", name, "-o", "json", *self._ns_args(), check=False)
            if existing.returncode != 0:
                raise RuntimeError("kubectl run conflicted and ownership is unverifiable")
            try:
                body = json.loads(existing.stdout)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("kubectl run conflicted and ownership is unverifiable") from exc
            labels = (body.get("metadata") or {}).get("labels") or {}
            phase = (body.get("status") or {}).get("phase")
            if (
                labels.get(MANAGED_LABEL) != "true"
                or labels.get(WORKLOAD_ID_LABEL) != workload_id
                or labels.get(SPEC_HASH_LABEL) != spec_hash
                or phase not in ("Pending", "Running")
            ):
                raise RuntimeError("kubectl run conflicted with a foreign or terminal workload")
            attested_identity = body
        attested_identity, started_at = self._confirmed_pod(name, initial=attested_identity)
        if WORKLOAD_CLAIM_ENV in env:
            metadata = attested_identity.get("metadata") or {}
            labels = metadata.get("labels") or {}
            uid = metadata.get("uid")
            if (
                not uid
                or labels.get(MANAGED_LABEL) != "true"
                or labels.get(WORKLOAD_ID_LABEL) != workload_id
                or labels.get(SPEC_HASH_LABEL) != spec_hash
                or labels.get(WORKLOAD_CLAIM_LABEL) != env[WORKLOAD_CLAIM_ENV][:52]
            ):
                raise RuntimeError("kubectl run succeeded but pod identity is unverifiable")
            return WorkloadHandle(
                id=workload_id, impl={"name": name, "uid": uid}, started_at=started_at,
            )
        return WorkloadHandle(id=workload_id, impl=name, started_at=started_at)

    def find(
        self, workload_id: str, *, claim_hash: Optional[str] = None,
    ) -> Optional[WorkloadHandle]:
        """Re-derive a handle for a workload whose in-process handle was lost (restart): the Pod
        name is deterministic (``prefix + workload_id``); an existing Pod (any phase) is found."""
        name = self._pname(workload_id)
        r = _kubectl("get", "pod", name, "-o", "json", *self._ns_args(), check=False)
        if r.returncode != 0:
            error = str(getattr(r, "stderr", "")).lower()
            if "notfound" in error or "not found" in error:
                return None
            raise RuntimeError("kubernetes workload identity lookup failed")
        if claim_hash is not None:
            try:
                labels = (json.loads(r.stdout).get("metadata") or {}).get("labels") or {}
            except (TypeError, json.JSONDecodeError):
                return None
            if (
                labels.get(MANAGED_LABEL) != "true"
                or labels.get(WORKLOAD_ID_LABEL) != workload_id
                or labels.get(WORKLOAD_CLAIM_LABEL) != claim_hash[:52]
            ):
                return None
        if claim_hash is not None:
            uid = (json.loads(r.stdout).get("metadata") or {}).get("uid")
            if not uid:
                return None
            return WorkloadHandle(id=workload_id, impl={"name": name, "uid": uid})
        return WorkloadHandle(id=workload_id, impl=name)

    def probe_claimed(self, workload_id: str, claim_hash: str) -> dict:
        """Inspect the deterministic Pod without relying on the runtime registry."""
        name = self._pname(workload_id)
        r = _kubectl("get", "pod", name, "-o", "json", *self._ns_args(), check=False)
        if r.returncode != 0:
            error = str(getattr(r, "stderr", "")).lower()
            if "notfound" in error or "not found" in error:
                return {"neverStarted": True, "backend": self.name}
            raise RuntimeError("kubernetes claimed workload probe failed")
        try:
            body = json.loads(r.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("kubernetes claimed workload probe is invalid") from exc
        metadata = body.get("metadata") or {}
        labels = metadata.get("labels") or {}
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(WORKLOAD_ID_LABEL) != workload_id
            or labels.get(WORKLOAD_CLAIM_LABEL) != claim_hash[:52]
        ):
            raise RuntimeError("kubernetes deterministic name is occupied by a foreign workload")
        uid = metadata.get("uid")
        if not uid:
            raise RuntimeError("kubernetes claimed workload identity is unavailable")
        phase = (body.get("status") or {}).get("phase")
        return {
            "backend": self.name, "identity": str(uid),
            "state": "starting" if phase == "Pending" else "running" if phase == "Running" else "stopped",
            "startedAt": _pod_started_at(body),
        }

    def list_workload_containers(self) -> list[dict]:
        """Discover the workload Pods THIS backend spawned — for boot re-adoption. Label-selected
        only (``runtime.managed=true``): a name-prefix fallback is unsafe in a shared namespace
        (the chart's own service Pods can share the prefix), so Pods spawned by a pre-label runtime
        are not re-adopted. Never raises."""
        try:
            r = _kubectl(
                "get", "pods", "-l", f"{MANAGED_LABEL}=true", "-o", "json",
                *self._ns_args(), check=False,
            )
            if r.returncode != 0:
                return []
            out = []
            for pod in json.loads(r.stdout).get("items", []):
                meta = pod.get("metadata", {})
                wid = (meta.get("labels") or {}).get(WORKLOAD_ID_LABEL)
                if not wid:
                    continue
                phase = pod.get("status", {}).get("phase")
                running = phase in ("Pending", "Running")
                exit_code: Optional[int] = None
                if not running:
                    exit_code = 0 if phase == "Succeeded" else 1
                    for cs in pod.get("status", {}).get("containerStatuses", []):
                        term = cs.get("state", {}).get("terminated")
                        if term and "exitCode" in term:
                            exit_code = int(term["exitCode"])
                out.append({
                    "workload_id": wid,
                    "name": meta.get("name", self._pname(wid)),
                    "handle": (
                        WorkloadHandle(
                            id=wid,
                            impl={"name": meta.get("name", self._pname(wid)), "uid": meta["uid"]},
                        )
                        if meta.get("uid") else None
                    ),
                    "claim_hash": (meta.get("labels") or {}).get(WORKLOAD_CLAIM_LABEL),
                    "running": running,
                    "exit_code": exit_code,
                    "started_at": _pod_started_at(pod),
                })
            return out
        except Exception:  # noqa: BLE001 — discovery is a boot aid; it must never crash the boot
            return []

    def exit_code(self, h: WorkloadHandle) -> Optional[int]:
        r = _kubectl("get", "pod", self._handle_name(h), "-o", "json", *self._ns_args(), check=False)
        if r.returncode != 0:
            return 0                                     # gone (deleted/never-found) → no longer running
        status = json.loads(r.stdout).get("status", {})
        phase = status.get("phase")
        if phase in ("Pending", "Running"):
            return None                                  # still scheduling / running
        if phase == "Succeeded":
            return 0
        if phase == "Failed":
            for cs in status.get("containerStatuses", []):
                term = cs.get("state", {}).get("terminated")
                if term and "exitCode" in term:
                    return int(term["exitCode"])
            return 1
        return None

    def terminate(self, h: WorkloadHandle) -> None:      # graceful: SIGTERM + grace, then SIGKILL
        _kubectl("delete", "pod", self._handle_name(h), f"--grace-period={_stop_grace_sec()}", "--wait=false",
                 *self._ns_args(), check=False)

    def kill(self, h: WorkloadHandle) -> None:           # force: immediate SIGKILL + drop the object
        _kubectl("delete", "pod", self._handle_name(h), "--grace-period=0", "--force", "--wait=false",
                 *self._ns_args(), check=False)

    def cleanup(self, h: WorkloadHandle) -> None:
        impl = h._impl  # type: ignore[attr-defined]
        name = self._handle_name(h)
        if isinstance(impl, dict):
            uid = impl["uid"]
            status = self._delete_pod_uid(name, uid)
            # 409 can mean the name now belongs to a different UID. It is not success by itself:
            # the bounded GET loop below must observe absence or a different UID.
            if status not in (200, 202, 404, 409):
                raise RuntimeError(f"kubernetes UID-fenced delete failed ({status})")
        else:
            deleted = _kubectl(
                "delete", "pod", name, "--ignore-not-found", "--grace-period=0", "--force",
                "--wait=false", *self._ns_args(), check=False,
            )
            if deleted.returncode != 0:
                raise RuntimeError("kubectl delete failed")
        deadline = time.monotonic() + max(
            0.1, float(os.getenv("RUNTIME_K8S_DELETE_CONFIRM_TIMEOUT_SEC", "10")),
        )
        while True:
            remaining = _kubectl("get", "pod", name, "-o", "json", *self._ns_args(), check=False)
            if remaining.returncode != 0:
                error = str(getattr(remaining, "stderr", "")).lower()
                if "notfound" in error or "not found" in error:
                    return
                raise RuntimeError("kubernetes deletion absence is unconfirmed")
            if isinstance(impl, dict):
                try:
                    remaining_uid = (json.loads(remaining.stdout).get("metadata") or {}).get("uid")
                except (TypeError, json.JSONDecodeError):
                    remaining_uid = None
                if remaining_uid and remaining_uid != impl["uid"]:
                    return  # old UID is gone; never touch the replacement
            if time.monotonic() >= deadline:
                raise RuntimeError("kubectl delete did not remove the attested pod before timeout")
            time.sleep(0.05)

    def teardown_identity(self, h: WorkloadHandle) -> str:
        impl = h._impl  # type: ignore[attr-defined]
        if not isinstance(impl, dict) or not impl.get("uid"):
            raise RuntimeError("kubernetes immutable Pod UID is unavailable")
        return str(impl["uid"])

    def cleanup_identity(self, workload_id: str, identity: str, claim_hash: str) -> None:
        name = self._pname(workload_id)
        current = _kubectl("get", "pod", name, "-o", "json", *self._ns_args(), check=False)
        if current.returncode != 0:
            error = str(getattr(current, "stderr", "")).lower()
            if "notfound" in error or "not found" in error:
                return
            raise RuntimeError("kubernetes attested identity is unverifiable")
        try:
            body = json.loads(current.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("kubernetes attested identity is unverifiable") from exc
        metadata = body.get("metadata") or {}
        if metadata.get("uid") != identity:
            return  # the persisted UID is absent; never touch its replacement
        labels = metadata.get("labels") or {}
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(WORKLOAD_ID_LABEL) != workload_id
            or labels.get(WORKLOAD_CLAIM_LABEL) != claim_hash[:52]
        ):
            raise RuntimeError("kubernetes attested identity does not belong to this workload")
        self.cleanup(WorkloadHandle(
            id=workload_id,
            impl={"name": name, "uid": identity},
        ))
