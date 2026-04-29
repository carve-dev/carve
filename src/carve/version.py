"""Single source of truth for the package version.

Read once at import time from the installed package metadata so we never drift
from `pyproject.toml`. When running from a source checkout that has not been
installed (e.g., very early dev), fall back to a hard-coded value matching
the current `pyproject.toml`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("carve")
except PackageNotFoundError:  # pragma: no cover - only hit before install
    __version__ = "0.0.1"
