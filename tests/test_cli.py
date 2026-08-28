from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _plugin  # noqa: F401 - registers the standalone package under test
from tern_plugin_under_test.cli import (
    _job_name,
    _job_prompt,
    _projects_add,
    _routing,
    setup_parser,
)
from tern_plugin_under_test.routing import ProjectRoute


class FakeContext:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get_config(self, key: str, default=None):
        return self.values.get(key, default)

    def set_config(self, key: str, value) -> None:
        self.values[key] = value


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def route(self, organization: str = "org-1", project: str = "project-1") -> ProjectRoute:
        return ProjectRoute(organization, project, self.repo, "Example")

    def test_parser_exposes_explicit_multi_project_commands(self) -> None:
        parser = argparse.ArgumentParser()
        setup_parser(parser)
        parsed = parser.parse_args(
            [
                "projects",
                "add",
                "--organization-id",
                "org-1",
                "--project-id",
                "project-1",
                "--path",
                str(self.repo),
            ]
        )
        self.assertEqual(parsed.tern_action, "projects")
        self.assertEqual(parsed.tern_project_action, "add")

    def test_job_identity_uses_both_tenant_and_project(self) -> None:
        first = self.route("org-1", "project-1")
        second = self.route("org-2", "project-1")
        self.assertNotEqual(_job_name(first), _job_name(second))
        self.assertIn("organization_id='org-1'", _job_prompt(first))
        self.assertIn("project_id='project-1'", _job_prompt(first))

    def test_project_add_saves_versioned_exact_route_without_starting(self) -> None:
        context = FakeContext()
        args = SimpleNamespace(
            organization_id="org-1",
            project_id="project-1",
            path=str(self.repo),
            label="Example",
            no_start=True,
        )
        with patch("builtins.print"):
            self.assertEqual(_projects_add(context, args), 0)
        config = _routing(context)
        self.assertEqual(config.version, 1)
        self.assertEqual(config.routes[0].key, ("org-1", "project-1"))
        self.assertEqual(config.routes[0].checkout_path, self.repo.resolve())


if __name__ == "__main__":
    unittest.main()
