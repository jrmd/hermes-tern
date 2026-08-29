from __future__ import annotations

import json
import unittest
from typing import Any

import _plugin  # noqa: F401 - registers the standalone package under test
from tern_plugin_under_test.tools import handlers


class FakeSession:
    def update_issue_status(self, status: str, *, expected_version: int) -> dict[str, Any]:
        return {
            "status": "updated",
            "requested": {
                "status": status,
                "expected_version": expected_version,
            },
        }


class ToolHandlerTests(unittest.TestCase):
    def test_update_issue_status_exposes_a_model_facing_workflow_tool(self) -> None:
        tool = handlers(FakeSession())["tern_update_issue_status"]
        output = json.loads(
            tool({"status": "in_progress", "expected_version": 7})
        )
        self.assertEqual(output["status"], "updated")
        self.assertEqual(output["requested"]["status"], "in_progress")
        self.assertEqual(output["requested"]["expected_version"], 7)


if __name__ == "__main__":
    unittest.main()
