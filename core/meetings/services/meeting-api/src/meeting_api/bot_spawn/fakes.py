"""In-process fakes for the bot-spawn ports — for this module's tests (drive the SAME shipped
``request_bot`` / ``build_router`` offline, no DB, no runtime kernel).

  * ``InMemoryMeetingRepo`` — a dict-backed ``MeetingRepo``: ``create_meeting`` assigns ids and
    timestamps, ``create_session`` records (meeting_id, session_uid), ``set_bot_container`` writes
    the workload id back. N sessions accumulate per meeting; ``continue_meeting`` reuses a terminal
    row + appends a session; ``count_active_bots`` powers the max-bots quota (browser_session
    excluded). ``sessions`` is exposed so a test asserts sessions were created. A test can flip a
    meeting's ``status`` directly to simulate the bot reaching active / a session going terminal.
  * ``FakeRuntimeClient`` — a ``RuntimeClient`` that records the spec it was asked to spawn and
    returns a synthetic ``workloadId``. Construct with ``quota_exceeded=True`` / ``fail=True`` to
    exercise the 429 / spawn-failed seams.

NO production logic — they only stand in for Postgres + the runtime kernel so the spawn flow runs
fully in-process.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import uuid

from .ports import (
    AssignmentOwnerConflict,
    AssignmentPayloadConflict,
    AssignmentInProgress,
    AssignmentLifecycleUnconfirmed,
    AssignmentLeaseLost,
    AssignmentTerminalConflict,
    DuplicateMeeting,
    MaxBotsExceeded,
    QuotaExceeded,
    SpawnFailed,
    WorkloadUnknown,
    reconcile_grace_for_status,
)

_ACTIVE_STATUSES = ("requested", "joining", "awaiting_admission", "active")
_TERMINAL_STATUSES = ("completed", "failed")


class InMemoryMeetingRepo:
    """A dict-backed ``MeetingRepo`` keyed by the synthetic meeting id."""

    def __init__(self):
        self._meetings: dict[int, dict] = {}
        self._next_id = 1
        self.sessions: list[dict] = []  # exposed for assertions (all sessions, all meetings)
        self.reopened: list[int] = []   # meeting ids continue_meeting reused
        self.assignment_starts: dict[str, dict] = {}

    async def find_active(self, user_id, platform, native_meeting_id) -> Optional[dict]:
        for m in self._meetings.values():
            if (
                m["user_id"] == user_id
                and m["platform"] == platform
                and m["native_meeting_id"] == native_meeting_id
                and m["status"] in _ACTIVE_STATUSES
            ):
                return dict(m)
        return None

    async def find_active_by_userdata(self, userdata_s3_path) -> Optional[dict]:
        for m in self._meetings.values():
            if (
                m["status"] in _ACTIVE_STATUSES
                and m.get("data", {}).get("auth_userdata_path") == userdata_s3_path
            ):
                return dict(m)
        return None

    async def find_latest(self, user_id, platform, native_meeting_id) -> Optional[dict]:
        rows = [
            m for m in self._meetings.values()
            if m["user_id"] == user_id
            and m["platform"] == platform
            and m["native_meeting_id"] == native_meeting_id
        ]
        if not rows:
            return None
        return dict(max(rows, key=lambda m: m["id"]))  # id is monotonic → most recent

    async def create_meeting(self, *, user_id, platform, native_meeting_id, data) -> dict:
        mid = self._next_id
        self._next_id += 1
        row = {
            "id": mid,
            "user_id": user_id,
            "platform": platform,
            "native_meeting_id": native_meeting_id,
            "platform_specific_id": native_meeting_id,
            "status": "requested",
            "bot_container_id": None,
            "start_time": None,
            "end_time": None,
            "data": dict(data or {}),
            "created_at": "2026-06-20T09:00:00Z",
            "updated_at": "2026-06-20T09:00:00Z",
        }
        self._meetings[mid] = row
        return dict(row)

    async def create_meeting_guarded(
        self, *, user_id, platform, native_meeting_id, data, max_concurrent=None,
        exclude_meeting_id=None,
    ) -> dict:
        """ATOMIC dedup + cap + insert (ROB1/ROB2). The check and the insert run with NO ``await``
        between them, so even ``SlowRepo`` (which adds ``await asyncio.sleep(0)`` inside the SEPARATE
        ``count_active_bots`` / ``create_meeting`` methods) cannot interleave concurrent spawns here —
        modelling the real adapter's single-transaction guard (advisory lock + unique partial index)."""
        # 0. depleted — a cap <= 0 means NO bots allowed (0 is "depleted", never "unlimited");
        #    only ``None`` (no cap provided) skips the gate. Mirrors the real adapter.
        if max_concurrent is not None and max_concurrent <= 0:
            raise MaxBotsExceeded(user_id, max_concurrent)
        # 1. dedup — an ACTIVE row for (user, platform, native) blocks the spawn (409).
        for m in self._meetings.values():
            if (
                m["user_id"] == user_id
                and m["platform"] == platform
                and m["native_meeting_id"] == native_meeting_id
                and m["status"] in _ACTIVE_STATUSES
            ):
                raise DuplicateMeeting(
                    f"An active meeting already exists for {platform}/{native_meeting_id}"
                )
        # 2. cap — count the user's ACTIVE bots (browser_session excluded); reject the N+1th (429).
        if max_concurrent is not None:
            active = sum(
                1 for m in self._meetings.values()
                if m["user_id"] == user_id
                and m["status"] in _ACTIVE_STATUSES
                and m["platform"] != "browser_session"
                and m["id"] != exclude_meeting_id
            )
            if active >= max_concurrent:
                raise MaxBotsExceeded(user_id, max_concurrent)
        # 2b. claim — a PLANNED row (intent status) for the same (user, platform, native) is
        #     UPGRADED in place, mirroring the real adapter: spawn keys merge OVER the planned
        #     data (title / scheduled_at / workspace_id / auto_join / calendar_uid survive).
        planned_rows = [
            m for m in self._meetings.values()
            if m["user_id"] == user_id
            and m["platform"] == platform
            and m["native_meeting_id"] == native_meeting_id
            and m["status"] in ("idle", "scheduled")
        ]
        if planned_rows:
            row = max(planned_rows, key=lambda m: m["id"])  # newest, like the real adapter
            row["status"] = "requested"
            row["end_time"] = None
            row["bot_container_id"] = None
            row["data"] = {**row["data"], **dict(data or {})}
            return dict(row)
        # 3. insert — NO await before this point since the dedup read, so the check+insert is atomic.
        mid = self._next_id
        self._next_id += 1
        row = {
            "id": mid,
            "user_id": user_id,
            "platform": platform,
            "native_meeting_id": native_meeting_id,
            "platform_specific_id": native_meeting_id,
            "status": "requested",
            "bot_container_id": None,
            "start_time": None,
            "end_time": None,
            "data": dict(data or {}),
            "created_at": "2026-06-20T09:00:00Z",
            "updated_at": "2026-06-20T09:00:00Z",
        }
        self._meetings[mid] = row
        return dict(row)

    async def reserve_assignment_start(
        self, *, assignment_id, user_id, request_hash, platform, native_meeting_id, data,
        max_concurrent=None,
    ) -> dict:
        existing = self.assignment_starts.get(assignment_id)
        if existing is not None:
            if existing["user_id"] != user_id:
                raise AssignmentOwnerConflict(assignment_id)
            if existing["request_hash"] != request_hash:
                raise AssignmentPayloadConflict(assignment_id)
            return {
                **dict(existing),
                "meeting": dict(self._meetings[existing["meeting_id"]]),
                "replay": True,
            }

        row = await self.create_meeting_guarded(
            user_id=user_id, platform=platform, native_meeting_id=native_meeting_id,
            data=data, max_concurrent=max_concurrent,
        )
        connection_id = assignment_id
        workload_id = f"mtg-{row['id']}-{assignment_id.replace('-', '')[:12]}"
        # Publish the deterministic substrate identity in the same reservation boundary. The
        # existing stale-nonterminal reconciler can now probe it: accepted work is protected by
        # liveness, while a crash before runtime is bounded by its untracked-expiry window.
        self._meetings[row["id"]]["bot_container_id"] = workload_id
        row["bot_container_id"] = workload_id
        start = {
            "assignment_id": assignment_id,
            "user_id": user_id,
            "request_hash": request_hash,
            "meeting_id": row["id"],
            "connection_id": connection_id,
            "workload_id": workload_id,
            "phase": "reserved",
            "phase_updated_at": datetime.now(timezone.utc),
            "lease_token": None,
            "lease_until": None,
            "launch_attempt": 0,
            "started_at": None,
            "teardown_backend": None,
            "teardown_identity": None,
            "teardown_confirmed_at": None,
            "last_error_code": None,
        }
        self.assignment_starts[assignment_id] = start
        self.sessions.append({"meeting_id": row["id"], "session_uid": connection_id})
        return {**dict(start), "meeting": dict(row), "replay": False}

    async def get_assignment_start(
        self, *, assignment_id, user_id, request_hash,
    ) -> Optional[dict]:
        existing = self.assignment_starts.get(assignment_id)
        if existing is None:
            return None
        if existing["user_id"] != user_id:
            raise AssignmentOwnerConflict(assignment_id)
        if existing["request_hash"] != request_hash:
            raise AssignmentPayloadConflict(assignment_id)
        return {
            **dict(existing),
            "meeting": dict(self._meetings[existing["meeting_id"]]),
            "replay": True,
        }

    async def get_assignment_for_meeting(self, *, meeting_id):
        assignments = [
            assignment_id
            for assignment_id, start in self.assignment_starts.items()
            if start["meeting_id"] == meeting_id and start["phase"] == "started"
        ]
        return assignments[0] if len(assignments) == 1 else None

    async def mark_assignment_started(
        self, *, assignment_id, user_id, workload_id, lease_token, started_at,
    ) -> dict:
        start = self.assignment_starts[assignment_id]
        if start["user_id"] != user_id:
            raise AssignmentOwnerConflict(assignment_id)
        if start["workload_id"] != workload_id:
            raise AssignmentPayloadConflict(assignment_id)
        if start["phase"] != "launching" or start["lease_token"] != lease_token:
            raise AssignmentLeaseLost(assignment_id)
        row = self._meetings[start["meeting_id"]]
        if start["phase"] == "cancelled" or row["status"] in _TERMINAL_STATUSES:
            raise AssignmentTerminalConflict(assignment_id)
        row["bot_container_id"] = workload_id
        start["phase"] = "started"
        start["phase_updated_at"] = datetime.now(timezone.utc)
        start["started_at"] = started_at
        start["lease_token"] = None
        start["lease_until"] = None
        start["last_error_code"] = None
        return dict(row)

    async def claim_assignment_launch(
        self, *, assignment_id, user_id, request_hash,
    ) -> dict:
        start = self.assignment_starts[assignment_id]
        if start["user_id"] != user_id:
            raise AssignmentOwnerConflict(assignment_id)
        if start["request_hash"] != request_hash:
            raise AssignmentPayloadConflict(assignment_id)
        now = datetime.now(timezone.utc)
        # Only a never-authorized/released reservation may enter create.  An expired launching
        # lease is recovery work: runtime absence is not substrate absence and cannot authorize a
        # blind second start.
        if start["phase"] != "reserved":
            raise AssignmentInProgress(assignment_id)
        start["phase"] = "launching"
        start["phase_updated_at"] = now
        start["lease_token"] = str(uuid.uuid4())
        start["lease_until"] = now + timedelta(seconds=45)
        start["launch_attempt"] += 1
        start["teardown_backend"] = None
        start["teardown_identity"] = None
        start["teardown_confirmed_at"] = None
        return {
            **dict(start),
            "meeting": dict(self._meetings[start["meeting_id"]]),
            "replay": start["launch_attempt"] > 1,
        }

    async def release_assignment_launch(
        self, *, assignment_id, user_id, lease_token, error_code, never_started=False,
    ) -> None:
        start = self.assignment_starts[assignment_id]
        if (
            start["user_id"] != user_id
            or start["phase"] != "launching"
            or start["lease_token"] != lease_token
        ):
            raise AssignmentLeaseLost(assignment_id)
        start["phase"] = "reserved"
        start["phase_updated_at"] = datetime.now(timezone.utc)
        start["lease_token"] = None
        start["lease_until"] = None
        start["last_error_code"] = "runtime_never_started" if never_started else error_code[:64]
        if never_started:
            start["teardown_confirmed_at"] = datetime.now(timezone.utc)

    async def record_assignment_teardown_identity(
        self, *, assignment_id, lease_token, error_code, teardown_backend, teardown_identity,
    ) -> dict:
        start = self.assignment_starts[assignment_id]
        if start["phase"] != "launching" or start["lease_token"] != lease_token:
            raise AssignmentLeaseLost(assignment_id)
        if not teardown_backend or not teardown_identity:
            raise AssignmentLeaseLost(assignment_id)
        start["teardown_backend"] = teardown_backend
        start["teardown_identity"] = teardown_identity
        start["last_error_code"] = error_code[:64]
        return {**dict(start), "meeting": dict(self._meetings[start["meeting_id"]])}

    async def claim_assignment_reconcile_candidates(self, *, limit=20) -> list[dict]:
        now = datetime.now(timezone.utc)
        out = []
        for start in self.assignment_starts.values():
            expired_reserved = (
                start["phase"] == "reserved"
                and start["phase_updated_at"] <= now - timedelta(seconds=120)
            )
            expired_lease = (
                start["phase"] in ("launching", "cancel_pending")
                and (start["lease_until"] is None or start["lease_until"] <= now)
            )
            if not (expired_reserved or expired_lease):
                continue
            if expired_reserved:
                start["phase"] = "cancel_pending"
                if start["last_error_code"] != "runtime_never_started":
                    start["last_error_code"] = "reservation_expired"
            start["phase_updated_at"] = now
            start["lease_token"] = str(uuid.uuid4())
            start["lease_until"] = now + timedelta(seconds=45)
            out.append({
                **dict(start),
                "meeting": dict(self._meetings[start["meeting_id"]]),
                "replay": True,
            })
            if len(out) >= limit:
                break
        return out

    async def begin_assignment_cancel(
        self, *, assignment_id, lease_token, error_code, teardown_backend, teardown_identity,
    ) -> dict:
        start = self.assignment_starts[assignment_id]
        if start["phase"] != "launching" or start["lease_token"] != lease_token:
            raise AssignmentLeaseLost(assignment_id)
        if not teardown_backend or not teardown_identity:
            raise AssignmentLeaseLost(assignment_id)
        start["phase"] = "cancel_pending"
        start["teardown_backend"] = teardown_backend
        start["teardown_identity"] = teardown_identity
        start["phase_updated_at"] = datetime.now(timezone.utc)
        start["last_error_code"] = error_code[:64]
        return {**dict(start), "meeting": dict(self._meetings[start["meeting_id"]])}

    async def complete_assignment_cancel(self, *, assignment_id, lease_token) -> None:
        start = self.assignment_starts[assignment_id]
        if start["phase"] != "cancel_pending" or start["lease_token"] != lease_token:
            raise AssignmentLeaseLost(assignment_id)
        if start["teardown_confirmed_at"] is None:
            raise AssignmentLifecycleUnconfirmed(assignment_id)
        if self._meetings[start["meeting_id"]]["status"] not in _TERMINAL_STATUSES:
            raise AssignmentLifecycleUnconfirmed(assignment_id)
        now = datetime.now(timezone.utc)
        start["phase"] = "cancelled"
        start["phase_updated_at"] = now
        start["lease_token"] = None
        start["lease_until"] = None

    async def record_assignment_teardown(self, *, assignment_id, lease_token) -> None:
        start = self.assignment_starts[assignment_id]
        if start["phase"] != "cancel_pending" or start["lease_token"] != lease_token:
            raise AssignmentLeaseLost(assignment_id)
        start["teardown_confirmed_at"] = datetime.now(timezone.utc)

    async def record_assignment_error(
        self, *, assignment_id, user_id, error_code,
    ) -> None:
        start = self.assignment_starts.get(assignment_id)
        if start is not None and start["user_id"] == user_id:
            start["last_error_code"] = error_code[:64]

    async def list_scheduled_meetings(self) -> list:
        return [
            dict(m) for m in self._meetings.values()
            if m["status"] == "scheduled"
            and m["native_meeting_id"] is not None
            and m["platform"] not in (None, "", "unknown")
        ]

    async def merge_meeting_data(self, meeting_id, patch) -> None:
        m = self._meetings.get(meeting_id)
        if m is None:
            return
        for k, v in patch.items():
            if v is None:
                m["data"].pop(k, None)
            else:
                m["data"][k] = v

    async def reopen_meeting(self, *, meeting_id, data_patch=None) -> dict:
        row = self._meetings[meeting_id]
        row["status"] = "requested"
        row["end_time"] = None
        row["bot_container_id"] = None
        # Clear the prior terminal attribution but KEEP the row + its transcripts/recordings.
        for k in ("completion_reason", "failure_stage"):
            row["data"].pop(k, None)
        for key, value in (data_patch or {}).items():
            if value is None:
                row["data"].pop(key, None)
            else:
                row["data"][key] = value
        self.reopened.append(meeting_id)
        return dict(row)

    async def create_session(self, *, meeting_id, session_uid) -> None:
        self.sessions.append({"meeting_id": meeting_id, "session_uid": session_uid})

    async def list_sessions(self, *, meeting_id) -> list:
        return [s["session_uid"] for s in self.sessions if s["meeting_id"] == meeting_id]

    async def set_bot_container(self, *, meeting_id, bot_container_id) -> dict:
        row = self._meetings[meeting_id]
        row["bot_container_id"] = bot_container_id
        return dict(row)

    async def fail_meeting(self, *, meeting_id, reason, failure_stage="requested") -> Optional[dict]:
        row = self._meetings.get(meeting_id)
        if row is None:
            return None
        row["status"] = "failed"
        row["data"]["failure_stage"] = failure_stage
        row["data"]["failure_reason"] = reason
        row["data"]["completion_reason"] = "start_failed"
        return dict(row)

    async def get_status_by_session(self, *, session_uid) -> Optional[str]:
        sess = next((s for s in self.sessions if s["session_uid"] == session_uid), None)
        if sess is None:
            return None
        row = self._meetings.get(sess["meeting_id"])
        return row["status"] if row else None

    async def get_lifecycle_state_by_session(self, *, session_uid) -> Optional[dict]:
        sess = next((s for s in self.sessions if s["session_uid"] == session_uid), None)
        if sess is None:
            return None
        row = self._meetings.get(sess["meeting_id"])
        if row is None:
            return None
        return {
            "status": row["status"],
            "data": dict(row.get("data") or {}),
        }

    async def find_by_container(self, *, bot_container_id) -> Optional[dict]:
        row = next(
            (m for m in self._meetings.values() if m.get("bot_container_id") == bot_container_id), None
        )
        if row is None:
            return None
        sid = next(
            (s["session_uid"] for s in reversed(self.sessions) if s["meeting_id"] == row["id"]), None
        )
        return {
            "meeting_id": row["id"],
            "status": row["status"],
            "session_uid": sid,
            "stop_requested": bool((row.get("data") or {}).get("stop_requested")),
        }

    async def update_meeting_status(
        self, *, session_uid, status, completion_reason=None, failure_stage=None, data=None
    ) -> None:
        sess = next((s for s in self.sessions if s["session_uid"] == session_uid), None)
        if sess is None:
            return  # unknown session — no-op (mirrors the SQL adapter)
        row = self._meetings.get(sess["meeting_id"])
        if row is None:
            return
        row["status"] = status
        if completion_reason is not None:
            row["data"]["completion_reason"] = completion_reason
        if failure_stage is not None:
            row["data"]["failure_stage"] = failure_stage
        for k, v in (data or {}).items():
            row["data"][k] = v
        return dict(row)

    async def count_active_bots(self, *, user_id, exclude_meeting_id=None) -> int:
        return sum(
            1 for m in self._meetings.values()
            if m["user_id"] == user_id
            and m["status"] in _ACTIVE_STATUSES
            and m["platform"] != "browser_session"   # infra excluded (parent meetings.py:1091)
            and m["id"] != exclude_meeting_id
        )

    async def list_service_authority_sessions(self) -> list[dict]:
        """Active rows carrying a per-run authority identity."""
        return [
            dict(m)
            for m in self._meetings.values()
            if m["status"] in ("active", "needs_help")
            and isinstance(m.get("data", {}).get("service_authority"), dict)
            and m["data"]["service_authority"].get("mode")
            in ("enforce", "observe")
        ]

    async def record_service_authority_decision(
        self,
        *,
        meeting_id,
        request,
        decision,
    ) -> bool:
        row = self._meetings.get(meeting_id)
        if row is None:
            return False
        metadata = row.get("data", {}).get("service_authority")
        if (
            not isinstance(metadata, dict)
            or metadata.get("service_identity")
            != request.service_identity
        ):
            return False
        boundary = request.boundary_at.isoformat()
        if metadata.get("last_boundary_at") == boundary:
            if metadata.get("last_decision_id") != decision.decision_id:
                raise ValueError(
                    "service-authority boundary decision conflicts"
                )
            return False
        if metadata.get("last_boundary_at"):
            from datetime import datetime

            previous = datetime.fromisoformat(
                metadata["last_boundary_at"].replace("Z", "+00:00")
            )
            if previous >= request.boundary_at:
                return False
        metadata.update(decision.to_record())
        metadata["last_boundary_at"] = boundary
        metadata["last_decision_id"] = decision.decision_id
        if (
            decision.enforced
            and not decision.allow
            and decision.stop_scope == "billable_service"
        ):
            metadata["teardown_confirmed"] = False
            row["data"]["stop_requested"] = True
            row["status"] = "stopping"
        return True

    async def list_service_authority_teardowns(self) -> list[dict]:
        out = []
        for row in self._meetings.values():
            metadata = row.get("data", {}).get("service_authority")
            if (
                isinstance(metadata, dict)
                and metadata.get("enforced") is True
                and metadata.get("allow") is False
                and metadata.get("stop_scope") == "billable_service"
                and metadata.get("teardown_confirmed") is not True
            ):
                out.append({
                    "id": row["id"],
                    "bot_container_id": row.get("bot_container_id"),
                    "decision_id": metadata.get("decision_id"),
                })
        return out

    async def claim_service_authority_teardown(
        self,
        *,
        meeting_id,
        claim_id,
        claimed_at,
        lease_seconds,
    ) -> Optional[dict]:
        from datetime import datetime, timezone

        row = self._meetings.get(meeting_id)
        metadata = (
            row.get("data", {}).get("service_authority")
            if row is not None
            else None
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("enforced") is not True
            or metadata.get("allow") is not False
            or metadata.get("stop_scope") != "billable_service"
            or metadata.get("teardown_confirmed") is True
        ):
            return None
        prior_claim = metadata.get("teardown_claim_id")
        prior_at = metadata.get("teardown_claimed_at")
        if prior_claim and prior_at:
            try:
                prior_time = datetime.fromisoformat(
                    prior_at.replace("Z", "+00:00"),
                ).astimezone(timezone.utc)
            except (TypeError, ValueError):
                return None
            if (claimed_at - prior_time).total_seconds() < lease_seconds:
                return None
        metadata["teardown_claim_id"] = claim_id
        metadata["teardown_claimed_at"] = claimed_at.isoformat()
        return {
            "id": row["id"],
            "bot_container_id": row.get("bot_container_id"),
            "decision_id": metadata.get("decision_id"),
            "claim_id": claim_id,
        }

    async def confirm_service_authority_teardown(
        self,
        *,
        meeting_id,
        decision_id,
        claim_id,
    ) -> bool:
        row = self._meetings.get(meeting_id)
        metadata = (
            row.get("data", {}).get("service_authority")
            if row is not None
            else None
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("decision_id") != decision_id
            or metadata.get("teardown_claim_id") != claim_id
            or metadata.get("teardown_confirmed") is True
        ):
            return False
        metadata["teardown_confirmed"] = True
        metadata["teardown_claim_id"] = None
        metadata["teardown_claimed_at"] = None
        return True

    async def list_stale_nonterminal(
        self, *, stop_grace: float, active_grace: float, preactive_grace: Optional[float] = None
    ) -> list:
        """In-memory mirror of the SQL adapter's general reconcile query. A row is stale once its age
        (now - ``updated_at``) passes its per-status grace (``reconcile_grace_for_status`` — the SAME
        policy the SQL adapter reads, so the two listings cannot drift). Rows carry a static created/
        updated timestamp, so a test sets ``updated_at`` (or leaves it in the past) to mark a row
        stale; a row whose ``updated_at`` is recent is NOT listed."""
        from datetime import datetime, timezone

        non_terminal = {
            "requested", "joining", "awaiting_admission", "needs_help", "active", "stopping",
        }
        now = datetime.now(timezone.utc)
        out: dict = {}
        # latest session per meeting (mirror the SQL adapter's MeetingSession.id desc)
        for s in reversed(self.sessions):
            mid = s["meeting_id"]
            if mid in out:
                continue
            row = self._meetings.get(mid)
            if row is None or row["status"] not in non_terminal:
                continue
            if any(
                start["meeting_id"] == mid
                and start["phase"] in ("reserved", "launching", "cancel_pending")
                for start in self.assignment_starts.values()
            ):
                continue
            upd = row.get("updated_at")
            try:
                u = datetime.fromisoformat(str(upd).replace("Z", "+00:00")) if upd else None
            except ValueError:
                u = None
            if u is None:
                continue
            if u.tzinfo is None:
                u = u.replace(tzinfo=timezone.utc)
            grace = reconcile_grace_for_status(
                row["status"], stop_grace, active_grace, preactive_grace
            )
            if (now - u).total_seconds() < grace:
                continue
            stop_req = bool(row.get("data", {}).get("stop_requested"))
            out[mid] = (row["status"], s["session_uid"], row.get("bot_container_id"), stop_req)
        return [(mid, st, sid, bcid, sr) for mid, (st, sid, bcid, sr) in out.items()]

    # ── test affordances (not part of the port) ──────────────────────────────────────────────────
    def set_status(self, meeting_id: int, status: str) -> None:
        """Flip a meeting's status (simulate the bot reaching active / a session going terminal)."""
        self._meetings[meeting_id]["status"] = status


class FakeRuntimeClient:
    """A ``RuntimeClient`` that records the spec and returns a synthetic ``workloadId``."""

    def __init__(self, *, quota_exceeded: bool = False, fail: bool = False,
                 dead_on_arrival: bool = False,
                 workloads: Optional[dict[str, dict]] = None):
        self._quota_exceeded = quota_exceeded
        self._fail = fail
        # dead_on_arrival models a kernel that (against #718 C1) still answers 201 but with a workload
        # that never started (state=stopped/start_failed). The HTTP adapter's body-state check (C2)
        # must catch it, so the FAKE returns that shape verbatim rather than raising — the belt of the
        # belt-and-suspenders defense the adapter owns.
        self._dead_on_arrival = dead_on_arrival
        self.specs: list[dict] = []  # every spawned spec, for assertions
        self.deleted: list[str] = []  # workload ids torn down (ROB3 compensation), for assertions
        self.attested_deleted: list[tuple[str, str, str]] = []
        # Liveness map for the reconcile sweep: workload_id -> status dict ({"state": ...}). A workload
        # ABSENT from this map is treated as GONE (404 → None) by ``get_workload``. ``None`` defaults to
        # "every workload is alive and running" (back-compat for tests that don't care about liveness).
        self._workloads: Optional[dict[str, dict]] = workloads

    async def create_workload(self, spec: dict) -> dict[str, Any]:
        self.specs.append(spec)
        if self._quota_exceeded:
            raise QuotaExceeded("owner quota exceeded")
        if self._fail:
            raise SpawnFailed("kernel could not start the workload")
        if self._dead_on_arrival:
            return {"workloadId": spec["workloadId"], "state": "stopped", "stopReason": "start_failed"}
        return {
            "workloadId": spec["workloadId"],
            "state": "running",
            "startedAt": "2026-06-20T09:00:01Z",
        }

    async def delete_workload(self, workload_id: str) -> None:
        # Mirrors the HTTP adapter: an id the kernel doesn't track raises WorkloadUnknown (404).
        # Absence is NOT evidence the underlying workload stopped; every caller must preserve that
        # distinction until it has a positive destroyed/teardown observation.
        if self._workloads is not None and workload_id not in self._workloads:
            raise WorkloadUnknown(workload_id)
        # Record the teardown so the partial-spawn test asserts the orphaned workload was torn down.
        self.deleted.append(workload_id)
        if self._workloads is not None:
            self._workloads.pop(workload_id, None)

    async def get_workload(self, workload_id: str) -> Optional[dict[str, Any]]:
        # Default (no map injected): every workload reports alive+running, so liveness gating defers to
        # the time window only when there is NO container id. A test exercising the liveness gate injects
        # ``workloads={...}`` — a workload absent from the map is GONE (None), present is alive.
        if self._workloads is None:
            return {"workloadId": workload_id, "state": "running"}
        return self._workloads.get(workload_id)

    async def get_teardown_identity(self, workload_id: str) -> dict:
        return {"backend": "docker", "identity": f"immutable:{workload_id}"}

    async def probe_claimed_workload(self, workload_id: str, *, claim_hash: str) -> dict:
        workload = None if self._workloads is None else self._workloads.get(workload_id)
        if workload is None:
            return {"backend": "docker", "neverStarted": True}
        return {
            "backend": "docker", "identity": f"immutable:{workload_id}",
            "state": workload.get("state"), "startedAt": workload.get("startedAt"),
        }

    async def delete_workload_attested(
        self, workload_id: str, *, backend: str, identity: str, claim_hash: str,
    ) -> None:
        self.attested_deleted.append((workload_id, backend, identity))
        if self._workloads is not None:
            self._workloads.pop(workload_id, None)
