"""Carve ↔ dbt integration: readers + the verify/runner bridge (Increment 3).

This package ships the dbt-side substrate the engineers run on:

- ``dbt_source_lookup`` (``sources.py``) — a callable
  :class:`~carve.core.agents.tools.Tool` over the user's dbt ``sources.yml``, so an
  EL pipeline lands where downstream dbt models already expect a raw source.
- ``dbt_manifest`` (``manifest.py``) — a callable Tool over dbt's compiled
  ``target/manifest.json``: list models, a model's columns, a model's
  upstream/downstream dependencies, and the data tests attached to a model.
- the verify/runner bridge (``verify.py`` + ``runner.py``) — ``parse_dbt_run``
  turns a finished ``dbt build``/``dbt test`` (its on-disk ``run_results.json``)
  into the harness :class:`~carve.core.agents.verification.CheckResult`, and
  ``make_dbt_parse_fn`` binds it into the harness ``ParseFn`` contract. The LIVE
  ``dbt build``/``test`` execution this bridge would ride is deferred to the
  dbt-execution unit; this ships the parse side only.

All of it resolves the dbt project via the shipped ``component_locator`` (root +
one-level-down detection), never a new locator.
"""

from __future__ import annotations

from carve.integrations.dbt.manifest import make_dbt_manifest_tool
from carve.integrations.dbt.runner import (
    make_dbt_parse_fn,
    make_dbt_verification_loop,
    run_dbt_check,
)
from carve.integrations.dbt.sources import make_dbt_source_lookup_tool
from carve.integrations.dbt.verify import DbtRunReport, parse_dbt_run, read_run_results

__all__ = [
    "DbtRunReport",
    "make_dbt_manifest_tool",
    "make_dbt_parse_fn",
    "make_dbt_source_lookup_tool",
    "make_dbt_verification_loop",
    "parse_dbt_run",
    "read_run_results",
    "run_dbt_check",
]
