from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import _plugin  # noqa: F401 - registers the standalone package under test
from tern_plugin_under_test.routing import (
    DuplicateRouteError,
    InvalidCheckoutError,
    InvalidConfigError,
    ProjectRoute,
    ProjectRouter,
    RouteNotFoundError,
    RoutingConfig,
    validate_checkout_path,
)


def _run_git(*args: str, cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _run_git("init", "-q", cwd=self.repo)
        _run_git(
            "-c",
            "user.name=Routing Test",
            "-c",
            "user.email=routing-test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
            cwd=self.repo,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def route(self, *, organization: str = "org-1", project: str = "project-1", path: Path | None = None, label: str | None = None) -> ProjectRoute:
        return ProjectRoute(organization, project, path or self.repo, label)

    def test_route_canonicalizes_absolute_symlink_and_serializes_label(self) -> None:
        link = self.root / "repo-link"
        link.symlink_to(self.repo, target_is_directory=True)
        route = self.route(path=link, label=" Trackit ")

        self.assertEqual(route.checkout_path, self.repo.resolve())
        self.assertTrue(route.checkout_path.is_absolute())
        self.assertEqual(route.label, "Trackit")
        self.assertEqual(
            route.to_dict(),
            {
                "organizationId": "org-1",
                "projectId": "project-1",
                "checkoutPath": os.fspath(self.repo.resolve()),
                "label": "Trackit",
            },
        )

    def test_linked_worktree_is_accepted_as_its_own_exact_root(self) -> None:
        worktree = self.root / "worktree"
        _run_git("worktree", "add", "-q", "--detach", os.fspath(worktree), cwd=self.repo)

        self.assertEqual(validate_checkout_path(worktree), worktree.resolve())
        self.assertEqual(self.route(path=worktree).checkout_path, worktree.resolve())

    def test_nested_directory_is_rejected_as_not_exact_root(self) -> None:
        nested = self.repo / "nested"
        nested.mkdir()

        with self.assertRaisesRegex(InvalidCheckoutError, "exact git repository root"):
            validate_checkout_path(nested)

    def test_non_repo_missing_file_root_and_home_are_rejected(self) -> None:
        non_repo = self.root / "not-repo"
        non_repo.mkdir()
        regular_file = self.root / "file"
        regular_file.write_text("not a directory", encoding="utf-8")

        cases = (non_repo, self.root / "missing", regular_file, Path("/"), Path.home())
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(InvalidCheckoutError):
                validate_checkout_path(candidate)

    def test_relative_checkout_is_rejected_even_if_it_resolves_from_cwd(self) -> None:
        with self.assertRaisesRegex(InvalidCheckoutError, "absolute"):
            validate_checkout_path(Path(os.path.relpath(self.repo)))

    def test_duplicate_exact_keys_are_rejected(self) -> None:
        first = self.route(label="first")
        second_path = self.root / "repo-two"
        second_path.mkdir()
        _run_git("init", "-q", cwd=second_path)
        second = self.route(label="second", path=second_path)

        with self.assertRaises(DuplicateRouteError):
            RoutingConfig((first, second))

    def test_json_round_trip_is_versioned_and_json_serializable(self) -> None:
        config = RoutingConfig((self.route(label="Trackit"),), version=1)
        document = config.to_json()
        payload = json.loads(document)
        self.assertEqual(payload["version"], 1)
        self.assertIsInstance(payload["routes"][0]["checkoutPath"], str)
        restored = RoutingConfig.from_json(document)
        self.assertEqual(restored.to_dict(), config.to_dict())
        self.assertEqual(ProjectRouter.from_dict(payload).lookup("org-1", "project-1"), config.routes[0])

    def test_unknown_config_version_and_malformed_json_fail_closed(self) -> None:
        with self.assertRaises(InvalidConfigError):
            RoutingConfig.from_dict({"version": 2, "routes": []})
        with self.assertRaises(InvalidConfigError):
            RoutingConfig.from_json("not json")
        with self.assertRaises(InvalidConfigError):
            RoutingConfig.from_dict({"version": 1, "routes": [{"projectId": "p"}]})

    def test_lookup_requires_the_exact_organization_and_project_pair(self) -> None:
        router = ProjectRouter((self.route(),))

        self.assertIsNotNone(router.lookup("org-1", "project-1"))
        self.assertIsNone(router.lookup("org-2", "project-1"))
        self.assertIsNone(router.lookup("org-1", "project-2"))
        self.assertIsNone(router.lookup("org-1", "project-1 "))
        self.assertIsNone(router.lookup("org-1", ""))
        with self.assertRaises(RouteNotFoundError):
            router.require("org-2", "project-1")

    def test_unknown_route_never_falls_back_to_a_label_or_checkout(self) -> None:
        labeled = self.route(label="default")
        router = ProjectRouter((labeled,))

        self.assertIsNone(router.lookup("", ""))
        self.assertIsNone(router.lookup("org-1", "unknown"))
        self.assertIsNone(router.lookup("unknown", "project-1"))


if __name__ == "__main__":
    unittest.main()
