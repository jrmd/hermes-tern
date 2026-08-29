from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
import unittest
from unittest.mock import patch

import _plugin  # noqa: F401 - registers the standalone package under test
import tern_plugin_under_test.monitor as monitor_module
from tern_plugin_under_test.monitor import (
    MAX_RESPONSE_BYTES,
    ORGANIZATION_ID_ENV,
    PROJECT_ID_ENV,
    ROUTE_CONFIG_ENV,
    RouteBinding,
    RouteConfigurationError,
    bind_route,
    build_stable_output,
    endpoint_is_safe,
    render_route_config,
    render_route_environment,
    render_monitor_script,
    route_from_environment,
    select_ready_runs,
    stable_json,
)


class MonitorTests(unittest.TestCase):
    def test_rendered_route_is_secret_free_and_round_trips(self) -> None:
        route = bind_route("org-1", "project-2")
        encoded = render_route_config(route.organization_id, route.project_id)
        self.assertEqual(
            encoded,
            '{"organizationId":"org-1","projectId":"project-2"}',
        )
        self.assertEqual(route_from_environment({ROUTE_CONFIG_ENV: encoded}), route)

        dotenv = render_route_environment(route.organization_id, route.project_id)
        self.assertIn(f'{ORGANIZATION_ID_ENV}="org-1"', dotenv)
        self.assertIn(f'{PROJECT_ID_ENV}="project-2"', dotenv)
        self.assertNotIn("CREDENTIAL", dotenv)
        self.assertNotIn("Bearer", dotenv)

    def test_rendered_monitor_wrapper_binds_only_the_route(self) -> None:
        source = render_monitor_script("org-1", "project-2")
        self.assertIn("TERN_RUNNER_ROUTE", source)
        self.assertIn("org-1", source)
        self.assertIn("project-2", source)
        self.assertNotIn("TERN_RUNNER_CREDENTIAL", source)
        compile(source, "tern_ready_monitor_org_1.py", "exec")

    def test_explicit_route_environment_is_required_as_a_pair(self) -> None:
        self.assertIsNone(route_from_environment({}))
        self.assertEqual(
            route_from_environment(
                {ORGANIZATION_ID_ENV: " org-1 ", PROJECT_ID_ENV: " project-2 "}
            ),
            RouteBinding("org-1", "project-2"),
        )
        with self.assertRaisesRegex(RouteConfigurationError, "together"):
            route_from_environment({ORGANIZATION_ID_ENV: "org-1"})

    def test_conflicting_route_aliases_fail_closed(self) -> None:
        with self.assertRaisesRegex(RouteConfigurationError, "conflicting"):
            route_from_environment(
                {
                    ORGANIZATION_ID_ENV: "org-1",
                    "TERN_ORGANIZATION_ID": "org-2",
                    PROJECT_ID_ENV: "project-1",
                }
            )

    def test_selects_exact_route_and_emits_minimal_sorted_identifiers(self) -> None:
        route = RouteBinding("org-1", "project-1")
        runs = [
            {
                "runId": "run-z",
                "organizationId": "org-1",
                "projectId": "project-1",
                "profileId": "profile-z",
                "version": 2,
                "objective": "must never be emitted",
                "context": {"secret": "must never be emitted"},
            },
            {
                "runId": "run-a",
                "organizationId": "org-1",
                "projectId": "project-1",
                "profileId": "profile-a",
                "version": "3",
            },
            {
                "runId": "run-nope-org",
                "organizationId": "org-2",
                "projectId": "project-1",
                "profileId": "profile-leak",
                "version": 1,
            },
            {
                "runId": "run-nope-project",
                "organizationId": "org-1",
                "projectId": "project-2",
                "profileId": "profile-leak",
                "version": 1,
            },
            {
                "runId": "run-missing-version",
                "organizationId": "org-1",
                "projectId": "project-1",
                "profileId": "profile-invalid",
            },
            # Duplicate responses must not make the wake-up output flap.
            {
                "runId": "run-z",
                "organizationId": "org-1",
                "projectId": "project-1",
                "profileId": "profile-z",
                "version": 2,
            },
        ]
        expected = [
            {"runId": "run-a", "version": 3, "profileId": "profile-a"},
            {"runId": "run-z", "version": 2, "profileId": "profile-z"},
        ]
        selected = select_ready_runs(runs, route)
        self.assertEqual(selected, expected)
        self.assertTrue(all(set(item) == {"runId", "version", "profileId"} for item in selected))

        output = build_stable_output({"data": {"runs": list(reversed(runs))}}, route)
        self.assertEqual(output, {"state": "ready", "runs": expected})
        serialized = stable_json(output)
        self.assertNotIn("objective", serialized)
        self.assertNotIn("context", serialized)
        self.assertNotIn("profile-leak", serialized)

    def test_stable_output_is_same_for_different_input_order(self) -> None:
        route = RouteBinding("org-1", "project-1")
        first = {
            "runs": [
                {
                    "runId": "run-2",
                    "organizationId": "org-1",
                    "projectId": "project-1",
                    "profileId": "profile-2",
                    "version": 1,
                },
                {
                    "runId": "run-1",
                    "organizationId": "org-1",
                    "projectId": "project-1",
                    "profileId": "profile-1",
                    "version": 1,
                },
            ]
        }
        second = {"runs": list(reversed(first["runs"]))}
        self.assertEqual(
            stable_json(build_stable_output(first, route)),
            stable_json(build_stable_output(second, route)),
        )

    def test_malformed_payload_does_not_emit_any_run_context(self) -> None:
        route = RouteBinding("org-1", "project-1")
        self.assertEqual(
            build_stable_output({"data": {"notRuns": []}}, route),
            {"state": "invalid_response"},
        )
        self.assertEqual(
            build_stable_output({"context": "private"}, route),
            {"state": "invalid_response"},
        )
        self.assertEqual(
            stable_json({"state": "invalid_response"}), '{"state":"invalid_response"}'
        )

    def test_endpoint_safety_keeps_https_and_loopback_http(self) -> None:
        self.assertTrue(endpoint_is_safe("https://gettern.app/api/runner/v1"))
        self.assertTrue(endpoint_is_safe("http://127.0.0.1:8787/api/runner/v1"))
        self.assertTrue(endpoint_is_safe("http://[::1]:8787/api/runner/v1"))
        self.assertFalse(endpoint_is_safe("http://gettern.app/api/runner/v1"))
        self.assertFalse(endpoint_is_safe("https://user:pass@gettern.app/api/runner/v1"))
        self.assertFalse(endpoint_is_safe("https://gettern.app/api/runner/v1#fragment"))

    def test_response_limit_remains_bounded(self) -> None:
        self.assertEqual(MAX_RESPONSE_BYTES, 64_000)

        class Response:
            def __init__(self) -> None:
                self.read_limit: int | None = None

            def read(self, limit: int) -> bytes:
                self.read_limit = limit
                return b"x" * (MAX_RESPONSE_BYTES + 1)

        class Opener:
            def open(self, request: object, timeout: int) -> Response:
                del request, timeout
                return response

        response = Response()
        output = StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "TERN_RUNNER_CREDENTIAL": "credential-sentinel",
                    ORGANIZATION_ID_ENV: "org-1",
                    PROJECT_ID_ENV: "project-1",
                    "TERN_RUNNER_URL": "http://127.0.0.1:8787/api/runner/v1",
                },
                clear=True,
            ),
            patch.object(monitor_module, "build_opener", return_value=Opener()),
            redirect_stdout(output),
        ):
            monitor_module.main()
        self.assertEqual(response.read_limit, MAX_RESPONSE_BYTES + 1)
        self.assertEqual(output.getvalue(), '{"state":"response_too_large"}\n')

    def test_main_identifies_the_plugin_to_the_runner_endpoint(self) -> None:
        class Response:
            def read(self, limit: int) -> bytes:
                del limit
                return b'{"runs":[]}'

        class Opener:
            request: object | None = None

            def open(self, request: object, timeout: int) -> Response:
                del timeout
                self.request = request
                return Response()

        opener = Opener()
        output = StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "TERN_RUNNER_CREDENTIAL": "credential-sentinel",
                    ORGANIZATION_ID_ENV: "org-1",
                    PROJECT_ID_ENV: "project-1",
                    "TERN_RUNNER_URL": "http://127.0.0.1:8787/api/runner/v1",
                },
                clear=True,
            ),
            patch.object(monitor_module, "build_opener", return_value=opener),
            redirect_stdout(output),
        ):
            monitor_module.main()
        self.assertIsNotNone(opener.request)
        self.assertEqual(
            opener.request.get_header("User-agent"),
            "hermes-tern-plugin/0.2.4",
        )
        self.assertEqual(output.getvalue(), '{"runs":[],"state":"ready"}\n')


if __name__ == "__main__":
    unittest.main()
