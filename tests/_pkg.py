"""Load the plugin exactly as Hermes does: as a package, so relative imports resolve.

Hermes imports a directory plugin as ``hermes_plugins.<slug>`` with the plugin directory as
the package search location. The tests use the same mechanism under their own package name,
which also proves the plugin no longer depends on its directory being on ``sys.path``.
"""

import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keep every write out of the user's home; must happen before the package is imported.
if "DEGENERACY_GUARD_HOME" not in os.environ:
    os.environ["DEGENERACY_GUARD_HOME"] = tempfile.mkdtemp(prefix="degeneracy-guard-test-")

PKG = "degeneracy_guard_under_test"


def load():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(
        PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PKG
    module.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


def submodule(name: str):
    load()
    return importlib.import_module(f"{PKG}.{name}")
