"""Session-wide pytest setup.

Disable the CLI's ``.env`` auto-loader for the duration of the test run.
A stray ``.env`` in the repo root or a CI checkout dir would otherwise leak
values into the process environment and produce hard-to-debug, environment-
dependent test failures. Tests that exercise the auto-loader directly clear
this flag in a fixture and restore it on teardown.
"""

from __future__ import annotations

import os

os.environ["CARVE_NO_DOTENV"] = "1"
