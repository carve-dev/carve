"""Carve ↔ dbt integration: readers the DLT engineer runs on (Increment 3).

So far this package ships ``dbt_source_lookup`` — a callable
:class:`~carve.core.agents.tools.Tool` that reads the user's dbt project
``sources.yml`` declarations so the engineer can target an existing dbt
source schema/table when authoring a dlt destination. It resolves the dbt
project via the shipped ``component_locator`` (root + one-level-down
detection), never a new locator.
"""

from __future__ import annotations

from carve.integrations.dbt.sources import make_dbt_source_lookup_tool

__all__ = ["make_dbt_source_lookup_tool"]
