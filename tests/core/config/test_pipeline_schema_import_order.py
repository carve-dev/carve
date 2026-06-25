"""Regression test for the `core.config <-> integrations` import cycle.

`pipeline_schema.py` used to import `component_locator` at module top.
Because `component_locator` imports `core.config.schema` (whose package
``__init__`` re-exports `pipeline_schema`), a bare
``import carve.integrations.component_locator`` in a fresh interpreter —
and isolated pytest collection of modules that import the locator first
(`test_code_emitter.py` / `test_backend_interface.py`) — raised an
ImportError on the partially-initialised module. The fix defers the
locator import into the function body that uses it.

These tests run the imports in a **fresh subprocess** so the cycle is
exercised from a clean module table, regardless of what the parent
pytest process already imported.
"""

from __future__ import annotations

import subprocess
import sys


def _import_in_fresh_interpreter(statement: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )


def test_bare_component_locator_import_succeeds() -> None:
    """`import carve.integrations.component_locator` works in isolation."""
    result = _import_in_fresh_interpreter(
        "import carve.integrations.component_locator as cl; "
        "assert hasattr(cl, 'resolve_component'); "
        "assert hasattr(cl, 'ComponentResolutionError')"
    )
    assert result.returncode == 0, f"locator import failed in a fresh interpreter:\n{result.stderr}"


def test_integrations_package_import_succeeds() -> None:
    """Importing the `integrations` package (which re-exports the locator) works."""
    result = _import_in_fresh_interpreter("import carve.integrations")
    assert result.returncode == 0, result.stderr


def test_core_config_still_imports_first() -> None:
    """`import carve.core.config` first still works (the other cycle order)."""
    result = _import_in_fresh_interpreter(
        "import carve.core.config; import carve.integrations.component_locator"
    )
    assert result.returncode == 0, result.stderr


def test_pipeline_schema_locator_symbols_resolve_lazily() -> None:
    """The deferred symbols are reachable when the validation pass runs.

    Importing `pipeline_schema` and reaching into its module namespace
    must not pull `resolve_component` to module scope (that's the cycle);
    but the locator must still be importable on demand.
    """
    result = _import_in_fresh_interpreter(
        "import carve.core.config.pipeline_schema as ps; "
        "assert not hasattr(ps, 'resolve_component'), "
        "'resolve_component must stay a deferred import, not module-level'; "
        "from carve.integrations.component_locator import resolve_component"
    )
    assert result.returncode == 0, result.stderr
