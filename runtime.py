"""Deep lifecycle interface used by Hermes tools.

The model sees claim/progress/finish. Attempt ids, lease tokens, idempotency
keys, and heartbeat renewal stay inside this module.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .client import DEFAULT_CAPABILITIES, RunnerClient, RunnerProtocolError


LEASE_LOST_CODES = frozenset(
    {"lease_expired", "lease_superseded", "expired_lease", "superseded_lease"}
)
OUTCOMES = frozenset({"succeeded", "needs_review", "failed", "blocked"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class ActiveAttempt:
    client: RunnerClient
    run: dict[str, Any]
    context: dict[str, Any]
    reporting: dict[str, Any]
    attempt_id: str
    lease: dict[str, Any]
    capabilities: list[str]
    renewal_number: int = 0
    report_number: int = 0
    status_number: int = 0
    lease_error: RunnerProtocolError | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    heartbeat: threading.Thread | None = None

    @property
    def run_id(self) -> str:
        return str(self.run.get("runId") or self.run.get("id"))

    @property
    def lease_token(self) -> str:
        return str(self.lease["leaseToken"])


class RunnerSession:
    def __init__(
        self,
        client_factory: Callable[[], RunnerClient],
        *,
        route_guard: Callable[[str, str], Any] | None = None,
        lease_extension_seconds: int = 180,
        lease_renewal_seconds: float = 45.0,
    ) -> None:
        self._client_factory = client_factory
        self._route_guard = route_guard
        self._lease_extension_seconds = lease_extension_seconds
        self._lease_renewal_seconds = lease_renewal_seconds
        self._lock = threading.RLock()
        self._active: ActiveAttempt | None = None

    @staticmethod
    def _require_claim_shape(
        claim: dict[str, Any], run: dict[str, Any], client: RunnerClient
    ) -> ActiveAttempt:
        attempt = claim.get("attempt")
        lease = claim.get("lease")
        if not isinstance(attempt, dict) or not isinstance(attempt.get("id"), str):
            raise RunnerProtocolError("Tern claim response did not contain an attempt", code="invalid_response")
        if not isinstance(lease, dict) or not isinstance(lease.get("leaseToken"), str):
            raise RunnerProtocolError("Tern claim response did not contain a lease", code="invalid_response")
        context = claim.get("context") if isinstance(claim.get("context"), dict) else {}
        reporting = claim.get("reporting")
        if not isinstance(reporting, dict):
            raise RunnerProtocolError(
                "Tern claim response did not contain a reporting contract",
                code="invalid_response",
            )
        effective = reporting.get("capabilities")
        if not isinstance(effective, list):
            raise RunnerProtocolError(
                "Tern reporting contract did not contain capabilities",
                code="invalid_response",
            )
        capabilities = [value for value in DEFAULT_CAPABILITIES if value in effective]
        return ActiveAttempt(
            client=client,
            run=run,
            context=context,
            reporting=reporting,
            attempt_id=attempt["id"],
            lease=lease,
            capabilities=capabilities,
        )

    @staticmethod
    def _lease_lost(error: RunnerProtocolError) -> bool:
        # The canonical wire codes are lease_expired/lease_superseded. The
        # current service can surface the internal aliases, and one deployed
        # adapter revision maps those aliases to internal_error with HTTP 409.
        return error.code in LEASE_LOST_CODES or (
            error.status == 409 and error.code == "internal_error"
        )

    @staticmethod
    def _normalize_context(context: dict[str, Any]) -> dict[str, Any]:
        """Accept the canonical pack and the current richer domain projection."""
        normalized = dict(context)
        if "authority" not in normalized and isinstance(normalized.get("policy"), dict):
            normalized["authority"] = normalized["policy"]
        if "content" not in normalized and isinstance(normalized.get("untrustedContent"), list):
            normalized["content"] = normalized["untrustedContent"]
        if "redactedFields" not in normalized and isinstance(normalized.get("redaction"), list):
            normalized["redactedFields"] = normalized["redaction"]
        return normalized

    def _heartbeat(self, active: ActiveAttempt) -> None:
        while not active.stop_event.wait(self._lease_renewal_seconds):
            try:
                with self._lock:
                    if self._active is not active:
                        return
                    self._renew_locked(active)
            except RunnerProtocolError as error:
                with self._lock:
                    active.lease_error = error
                if self._lease_lost(error) or not error.retryable:
                    return

    def _renew_locked(self, active: ActiveAttempt) -> None:
        active.renewal_number += 1
        result = active.client.renew(
            active.run_id,
            active.attempt_id,
            active.lease_token,
            f"hermes-lease:{active.run_id}:{active.attempt_id}:{active.renewal_number}",
            self._lease_extension_seconds,
        )
        lease = result.get("lease") if isinstance(result, dict) else None
        if not isinstance(lease, dict) or not isinstance(lease.get("leaseToken"), str):
            raise RunnerProtocolError("Tern lease response did not contain a lease", code="invalid_response")
        active.lease = lease
        active.lease_error = None

    def _active_or_error(self) -> ActiveAttempt:
        active = self._active
        if active is None:
            raise RunnerProtocolError("No Tern run is active; call tern_claim_next first", code="no_active_run")
        if active.lease_error is not None:
            raise active.lease_error
        return active

    @staticmethod
    def _same_route(value: dict[str, Any], organization_id: str, project_id: str) -> bool:
        return value.get("organizationId") == organization_id and value.get("projectId") == project_id

    @staticmethod
    def _assert_claim_route(
        run: dict[str, Any], context: dict[str, Any], organization_id: str, project_id: str
    ) -> None:
        if not RunnerSession._same_route(run, organization_id, project_id):
            raise RunnerProtocolError(
                "Tern claim response did not match the configured project route",
                code="route_mismatch",
            )
        source = context.get("source") if isinstance(context.get("source"), dict) else {}
        policy = context.get("policy") if isinstance(context.get("policy"), dict) else {}
        authority = context.get("authority") if isinstance(context.get("authority"), dict) else {}
        for candidate in (source.get("projectId"), policy.get("projectId"), authority.get("projectId")):
            if candidate is not None and candidate != project_id:
                raise RunnerProtocolError(
                    "Tern context did not match the configured project route",
                    code="route_mismatch",
                )
        for candidate in (policy.get("organizationId"), authority.get("organizationId")):
            if candidate is not None and candidate != organization_id:
                raise RunnerProtocolError(
                    "Tern context did not match the configured organization route",
                    code="route_mismatch",
                )

    def claim_next(self, organization_id: str, project_id: str) -> dict[str, Any]:
        organization_id = organization_id.strip()
        project_id = project_id.strip()
        if not organization_id or not project_id:
            raise ValueError("Both organization_id and project_id are required")
        with self._lock:
            if self._active is not None:
                return {"status": "already_claimed", **self.status()}
            if self._route_guard is None:
                raise RunnerProtocolError(
                    "No local Tern project routes are configured; run `hermes tern projects add`",
                    code="route_not_configured",
                )
            self._route_guard(organization_id, project_id)
            client = self._client_factory()
            listing = client.list_ready_runs(
                limit=10,
                organization_id=organization_id,
                project_id=project_id,
            )
            runs = listing.get("runs", [])
            if not runs:
                return {
                    "status": "idle",
                    "organizationId": organization_id,
                    "projectId": project_id,
                }
            matching = [
                run
                for run in runs
                if isinstance(run, dict) and self._same_route(run, organization_id, project_id)
            ]
            if not matching:
                raise RunnerProtocolError(
                    "Tern returned ready work outside the requested project route",
                    code="route_mismatch",
                )
            run = sorted(
                matching,
                key=lambda value: (str(value.get("createdAt") or ""), str(value.get("runId") or value.get("id") or "")),
            )[0]
            run_id = str(run.get("runId") or run.get("id") or "")
            if not run_id:
                raise RunnerProtocolError("Tern listed a run without a run id", code="invalid_response")
            claim = client.claim(run_id, f"hermes-claim:{run_id}")
            claimed_run = claim.get("run") if isinstance(claim.get("run"), dict) else run
            active = self._require_claim_shape(claim, claimed_run, client)
            if not active.context:
                context_result = client.get_context(run_id, active.attempt_id, active.lease_token)
                context = context_result.get("context") if isinstance(context_result, dict) else None
                if not isinstance(context, dict):
                    raise RunnerProtocolError("Tern context response did not contain context", code="invalid_response")
                active.context = self._normalize_context(context)
            else:
                active.context = self._normalize_context(active.context)
            self._assert_claim_route(active.run, active.context, organization_id, project_id)
            self._active = active
            try:
                self._renew_locked(active)
            except Exception:
                self._clear_active(active)
                raise
            active.heartbeat = threading.Thread(
                target=self._heartbeat,
                args=(active,),
                name=f"tern-lease-{run_id[:8]}",
                daemon=True,
            )
            active.heartbeat.start()
            return {
                "status": "claimed",
                "organizationId": organization_id,
                "projectId": project_id,
                "run": active.run,
                "context": active.context,
                "reporting": active.reporting,
            }

    def progress(
        self,
        message: str,
        *,
        phase: str | None = None,
        percent: int | None = None,
        known_gap: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            active = self._active_or_error()
            clean_message = message.strip()
            if not clean_message:
                raise ValueError("Progress message is required")
            if len(clean_message) > 2_000:
                raise ValueError("Progress message must be 2,000 characters or fewer")
            self._renew_locked(active)
            active.report_number += 1
            progress: dict[str, Any] = {"message": clean_message}
            if phase:
                progress["phase"] = phase.strip()
            if percent is not None:
                progress["percent"] = max(0, min(int(percent), 100))
            if known_gap:
                progress["knownGap"] = known_gap.strip()
            result = active.client.report(
                active.run_id,
                active.attempt_id,
                active.lease_token,
                f"hermes-report:{active.run_id}:{active.attempt_id}:{active.report_number}",
                active.capabilities,
                {"kind": "progress", "progress": progress},
            )
            return {"status": "reported", "runId": active.run_id, "response": result}

    @staticmethod
    def _missing_required_artifacts(
        context: dict[str, Any], artifacts: list[dict[str, Any]],
    ) -> list[str]:
        workflow = context.get("workflow")
        if not isinstance(workflow, dict):
            return []
        required = workflow.get("requiredArtifactKinds")
        if not isinstance(required, list):
            return []
        reported = {
            str(artifact.get("type") or artifact.get("kind"))
            for artifact in artifacts
            if isinstance(artifact, dict) and (artifact.get("type") or artifact.get("kind"))
        }
        return [str(kind) for kind in required if isinstance(kind, str) and kind not in reported]

    def update_issue_status(
        self,
        status: str,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        clean_status = status.strip()
        if not clean_status or len(clean_status) > 48 or not all(
            character.islower() or character.isdigit() or character == "_"
            for character in clean_status
        ) or clean_status.startswith("_") or clean_status.endswith("_") or "__" in clean_status:
            raise ValueError("Status must be lowercase snake_case and 48 characters or fewer")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        with self._lock:
            active = self._active_or_error()
            authority = active.context.get("authority")
            if not isinstance(authority, dict):
                authority = active.context.get("policy")
            if not isinstance(authority, dict) or authority.get("actionMode") != "safe_automatic":
                raise RunnerProtocolError(
                    "This run does not permit safe automatic issue_update status changes",
                    code="capability_not_allowed",
                )
            allowed = authority.get("allowedActions")
            policy_capabilities = authority.get("capabilities")
            if not isinstance(allowed, list) or "issue_update" not in allowed:
                raise RunnerProtocolError(
                    "This run is not allowed to perform the issue_update status action",
                    code="capability_not_allowed",
                )
            if not isinstance(policy_capabilities, list) or "issue_updates" not in policy_capabilities:
                raise RunnerProtocolError(
                    "This run is not configured for issue status updates",
                    code="capability_not_allowed",
                )
            if "issue_updates" not in active.capabilities:
                raise RunnerProtocolError(
                    "This runner does not advertise issue_updates",
                    code="capability_not_allowed",
                )
            self._renew_locked(active)
            active.status_number += 1
            response = active.client.update_issue_status(
                active.run_id,
                active.attempt_id,
                active.lease_token,
                f"hermes-status:{active.run_id}:{active.attempt_id}:{active.status_number}",
                active.capabilities,
                clean_status,
                expected_version,
            )
            return {"status": "updated", "runId": active.run_id, "response": response}

    def finish(
        self,
        *,
        outcome: str,
        summary: str,
        work_performed: str,
        known_gaps: list[str] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"Unsupported Tern outcome: {outcome}")
        clean_summary = summary.strip()
        clean_work = work_performed.strip()
        if not clean_summary or len(clean_summary) > 4_000:
            raise ValueError("Summary is required and must be 4,000 characters or fewer")
        if len(clean_work) > 12_000:
            raise ValueError("Work performed must be 12,000 characters or fewer")
        clean_gaps = [str(value).strip() for value in (known_gaps or []) if str(value).strip()]
        if len(clean_gaps) > 20 or any(len(value) > 500 for value in clean_gaps):
            raise ValueError("Known gaps are limited to 20 entries of 500 characters each")
        clean_artifacts = artifacts or []
        if len(clean_artifacts) > 50:
            raise ValueError("Artifacts are limited to 50 entries")
        with self._lock:
            active = self._active_or_error()
            missing = self._missing_required_artifacts(active.context, clean_artifacts)
            if outcome == "succeeded" and missing:
                raise RunnerProtocolError(
                    "Cannot report succeeded; required artifacts are missing: " + ", ".join(missing),
                    code="completion_requirements_missing",
                )
            self._renew_locked(active)
            result = {
                "outcome": outcome,
                "summary": clean_summary,
                "workPerformed": clean_work,
                "knownGaps": clean_gaps,
                "artifacts": clean_artifacts,
                "runnerReceipt": f"hermes-receipt:{uuid.uuid4()}",
                "finishedAt": _utc_now(),
            }
            try:
                response = active.client.finish(
                    active.run_id,
                    active.attempt_id,
                    active.lease_token,
                    f"hermes-finish:{active.run_id}:{active.attempt_id}",
                    active.capabilities,
                    result,
                )
            except Exception:
                if active.lease_error and self._lease_lost(active.lease_error):
                    self._clear_active(active)
                raise
            self._clear_active(active)
            return {"status": "finished", "runId": active.run_id, "outcome": outcome, "response": response}

    def _clear_active(self, active: ActiveAttempt) -> None:
        active.stop_event.set()
        if self._active is active:
            self._active = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
            if active is None:
                return {"active": False}
            return {
                "active": True,
                "runId": active.run_id,
                "leaseUntil": active.lease.get("leaseUntil"),
                "leaseHealthy": active.lease_error is None,
                "leaseErrorCode": active.lease_error.code if active.lease_error else None,
            }
