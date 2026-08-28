"""Load the plugin package from either a standalone clone or the Tern monorepo."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PACKAGE_NAME = "tern_plugin_under_test"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load() -> str:
    if PACKAGE_NAME in sys.modules:
        return PACKAGE_NAME
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the Tern plugin package for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return PACKAGE_NAME


load()
