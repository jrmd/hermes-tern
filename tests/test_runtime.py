from __future__ import annotations

import unittest
from typing import Any

import _plugin  # noqa: F401 - registers the standalone package under test
from tern_plugin_under_test.runtime import RunnerSession


class FakeClient:
    def __init__(self, *, runs: list[dict[str, Any]] | None = None) -> None:
        self.runs = runs if runs is not None else [
            {
                "runId": "run-1",
                "organizationId": "org-1",
                "projectId": "project-1",
                "version": 1,
            }
        ]
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.lease_version = 0

    def list_ready_runs(
        self,
        *,
        limit: int,
        organization_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("list", (limit, organization_id, project_id)))
        return {"runs": self.runs, "nextCursor": None}

    def claim(self, run_id: str, key: str) -> dict[str, Any]:
        self.calls.append(("claim", (run_id, key)))
        return {
            "run": {
                "id": run_id,
                "organizationId": "org-1",
                "projectId": "project-1",
                "objective": "Implement the task",
            },
            "attempt": {"id": "attempt-1"},
            "lease": {"leaseToken": "lease-token-not-for-model", "leaseUntil": "later"},
            "context": {
                "runId": run_id,
                "source": {"projectId": "project-1"},
                "authority": {
                    "organizationId": "org-1",
                    "projectId": "project-1",
                    "actionMode": "proposal_only",
                    "allowedActions": ["read", "analysis"],
                },
                "content": [{"kind": "issue", "title": "Task", "untrusted": True}],
            },
            "reporting": {"capabilities": ["progress", "artifacts"]},
        }

    def renew(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("renew", args))
        self.lease_version += 1
        return {
            "lease": {
                "leaseToken": f"rotated-lease-token-{self.lease_version}",
                "leaseUntil": "later",
            }
        }

    def report(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("report", args))
        return {"operation": "report"}

    def finish(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("finish", args))
        return {"operation": "finish"}


class RunnerSessionTests(unittest.TestCase):
    @staticmethod
    def session(client: FakeClient) -> RunnerSession:
        return RunnerSession(
            lambda: client,
            route_guard=lambda organization_id, project_id: (organization_id, project_id),
            lease_renewal_seconds=3600,
        )

    def test_idle_does_not_claim(self) -> None:
        client = FakeClient(runs=[])
        session = self.session(client)
        self.assertEqual(
            session.claim_next("org-1", "project-1"),
            {"status": "idle", "organizationId": "org-1", "projectId": "project-1"},
        )
        self.assertEqual([name for name, _ in client.calls], ["list"])

    def test_claim_hides_attempt_and_lease_and_starts_renewal(self) -> None:
        client = FakeClient()
        session = self.session(client)
        result = session.claim_next("org-1", "project-1")
        encoded = repr(result)
        self.assertEqual(result["status"], "claimed")
        self.assertEqual(result["run"]["objective"], "Implement the task")
        self.assertNotIn("lease-token", encoded)
        self.assertNotIn("attempt-1", encoded)
        self.assertEqual([name for name, _ in client.calls], ["list", "claim", "renew"])
        self.assertTrue(session.status()["active"])

    def test_progress_renews_and_uses_distinct_keys(self) -> None:
        client = FakeClient()
        session = self.session(client)
        session.claim_next("org-1", "project-1")
        session.progress("first", phase="implement", percent=25)
        session.progress("second", percent=50)
        reports = [args for name, args in client.calls if name == "report"]
        self.assertEqual(len(reports), 2)
        self.assertNotEqual(reports[0][3], reports[1][3])
        self.assertEqual(reports[0][5]["progress"]["phase"], "implement")

    def test_finish_uses_canonical_result_and_clears_active(self) -> None:
        client = FakeClient()
        session = self.session(client)
        session.claim_next("org-1", "project-1")
        result = session.finish(
            outcome="needs_review",
            summary="Patch prepared",
            work_performed="Implemented and tested the requested change.",
            known_gaps=["PR not pushed"],
        )
        finish_args = [args for name, args in client.calls if name == "finish"][0]
        wire_result = finish_args[5]
        self.assertEqual(result["status"], "finished")
        self.assertEqual(wire_result["outcome"], "needs_review")
        self.assertEqual(wire_result["knownGaps"], ["PR not pushed"])
        self.assertTrue(wire_result["runnerReceipt"].startswith("hermes-receipt:"))
        self.assertFalse(session.status()["active"])

    def test_second_claim_does_not_claim_more_work(self) -> None:
        client = FakeClient()
        session = self.session(client)
        session.claim_next("org-1", "project-1")
        result = session.claim_next("org-1", "project-1")
        self.assertEqual(result["status"], "already_claimed")
        self.assertEqual([name for name, _ in client.calls].count("claim"), 1)

    def test_route_guard_runs_before_listing_or_claiming(self) -> None:
        client = FakeClient()

        def reject(organization_id: str, project_id: str) -> None:
            raise ValueError(f"unmapped {organization_id}/{project_id}")

        session = RunnerSession(lambda: client, route_guard=reject)
        with self.assertRaisesRegex(ValueError, "unmapped"):
            session.claim_next("org-x", "project-x")
        self.assertEqual(client.calls, [])

    def test_mismatched_ready_envelope_is_never_claimed(self) -> None:
        client = FakeClient(
            runs=[{"runId": "run-other", "organizationId": "org-2", "projectId": "project-2"}]
        )
        session = self.session(client)
        with self.assertRaisesRegex(Exception, "outside the requested project route"):
            session.claim_next("org-1", "project-1")
        self.assertEqual([name for name, _ in client.calls], ["list"])


if __name__ == "__main__":
    unittest.main()
