from __future__ import annotations

import json
import unittest
from urllib.request import Request

import _plugin  # noqa: F401 - registers the standalone package under test
from tern_plugin_under_test.client import (
    RunnerClient,
    RunnerProtocolError,
    TransportResponse,
    normalize_base_url,
)


def response(status: int, payload: object, **headers: str) -> TransportResponse:
    return TransportResponse(status, headers, json.dumps(payload).encode("utf-8"))


class RunnerClientTests(unittest.TestCase):
    def test_rejects_unsafe_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            normalize_base_url("http://gettern.app/api/runner/v1")
        self.assertEqual(
            normalize_base_url("http://127.0.0.1:8787/api/runner/v1/"),
            "http://127.0.0.1:8787/api/runner/v1",
        )
        with self.assertRaisesRegex(ValueError, "credentials"):
            normalize_base_url("https://user:pass@gettern.app/api/runner/v1")

    def test_list_uses_bearer_without_exposing_it(self) -> None:
        seen: list[Request] = []

        def transport(request: Request, timeout: float, max_bytes: int) -> TransportResponse:
            del timeout, max_bytes
            seen.append(request)
            return response(200, {"data": {"operation": "list", "runs": [], "nextCursor": None}})

        client = RunnerClient(
            "https://gettern.app/api/runner/v1",
            "credential-sentinel",
            transport=transport,
        )
        self.assertEqual(client.list_ready_runs()["runs"], [])
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer credential-sentinel")
        self.assertNotIn("credential-sentinel", repr(client))

    def test_list_can_filter_one_exact_project_route(self) -> None:
        seen: list[Request] = []

        def transport(request: Request, timeout: float, max_bytes: int) -> TransportResponse:
            del timeout, max_bytes
            seen.append(request)
            return response(200, {"data": {"operation": "list", "runs": [], "nextCursor": None}})

        client = RunnerClient(
            "https://gettern.app/api/runner/v1",
            "credential-sentinel",
            transport=transport,
        )
        client.list_ready_runs(organization_id="org-a", project_id="project-a")
        self.assertIn("organizationId=org-a", seen[0].full_url)
        self.assertIn("projectId=project-a", seen[0].full_url)

    def test_mutation_retries_with_identical_idempotency_key(self) -> None:
        requests: list[tuple[str | None, bytes | None]] = []

        def transport(request: Request, timeout: float, max_bytes: int) -> TransportResponse:
            del timeout, max_bytes
            requests.append((request.get_header("Idempotency-key"), request.data))
            if len(requests) == 1:
                return response(
                    503,
                    {"error": {"code": "temporarily_unavailable", "message": "later", "retryable": True}},
                )
            return response(
                200,
                {
                    "data": {
                        "attempt": {"id": "attempt-id"},
                        "lease": {"leaseToken": "lease-token-long-enough"},
                    }
                },
            )

        sleeps: list[float] = []
        client = RunnerClient(
            "https://gettern.app/api/runner/v1",
            "secret",
            transport=transport,
            sleep=sleeps.append,
        )
        client.claim("run-id", "claim-key")
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0][0], "claim-key")
        self.assertEqual(sleeps, [0.25])

    def test_non_retryable_error_is_bounded_and_sanitized(self) -> None:
        def transport(request: Request, timeout: float, max_bytes: int) -> TransportResponse:
            del request, timeout, max_bytes
            return response(401, {"error": {"code": "unauthorized", "message": "bad credential"}})

        client = RunnerClient("https://gettern.app/api/runner/v1", "credential-sentinel", transport=transport)
        with self.assertRaises(RunnerProtocolError) as caught:
            client.list_ready_runs()
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("credential-sentinel", str(caught.exception))

    def test_server_cannot_echo_runner_secrets_into_errors(self) -> None:
        def transport(request: Request, timeout: float, max_bytes: int) -> TransportResponse:
            del request, timeout, max_bytes
            return response(
                400,
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "credential-sentinel lease-token-sentinel",
                    }
                },
            )

        client = RunnerClient("https://gettern.app/api/runner/v1", "credential-sentinel", transport=transport)
        with self.assertRaises(RunnerProtocolError) as caught:
            client.renew("run", "attempt", "lease-token-sentinel", "key", 30)
        self.assertNotIn("credential-sentinel", str(caught.exception))
        self.assertNotIn("lease-token-sentinel", str(caught.exception))

    def test_invalid_json_fails_without_retry(self) -> None:
        calls = 0

        def transport(request: Request, timeout: float, max_bytes: int) -> TransportResponse:
            nonlocal calls
            del request, timeout, max_bytes
            calls += 1
            return TransportResponse(200, {}, b"not-json")

        client = RunnerClient("https://gettern.app/api/runner/v1", "secret", transport=transport)
        with self.assertRaises(RunnerProtocolError) as caught:
            client.list_ready_runs()
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
