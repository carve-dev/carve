"""Engine resolution + pin — *decide + record*, never install.

Carve picks the best dbt engine the target warehouse supports, then **pins** it
into the component config so the choice is reproducible and never silently
drifts. This module owns the *decision* and the *config write-back*; the
**install** of the chosen engine is connect's deferred half (the engine binary
path is injected into the backend, never installed here).

License guardrail (read before touching the ``fusion`` branch): the bundled
engine Carve resolves to for warehouses that support it is **dbt Core v2.0,
which is Apache-2.0** — the OSS relicensing of dbt's engine. It is **NOT** the
ELv2-licensed commercial "dbt Fusion" build, whose license forbids
managed-service use and would taint both the OSS Carve and any hosted Carve. The
``"fusion"`` engine identifier here names the *Apache-2.0 dbt Core v2.0 engine*,
nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import tomlkit
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from carve.core.config.schema import ComponentConfig

# Engine identifiers.
ENGINE_FUSION = "fusion"  # the Apache-2.0 dbt Core v2.0 engine (NOT ELv2 Fusion)
ENGINE_DBT_CORE = "dbt-core"

# Warehouse dialects whose adapters the dbt Core v2.0 ("fusion") engine supports.
# Everything else (DuckDB, Postgres, and the long tail) falls back to dbt-core.
_FUSION_DIALECTS = frozenset({"snowflake", "bigquery", "databricks", "redshift"})

# Default pinned versions per engine. These are the reproducibility anchors the
# pin records; connect's installer reads them to fetch the matching engine.
_DEFAULT_VERSIONS = {
    ENGINE_FUSION: "2.0.0",
    ENGINE_DBT_CORE: "1.8.0",
}


class EnginePin(BaseModel):
    """The resolved engine + version recorded into the component config."""

    model_config = ConfigDict(extra="forbid")

    dbt_engine: str
    dbt_version: str


def resolve_engine(dialect: str) -> EnginePin:
    """Pick the best engine for ``dialect`` — Fusion where supported, else dbt-core.

    ``snowflake``/``bigquery``/``databricks``/``redshift`` → ``fusion`` (the
    Apache-2.0 dbt Core v2.0 engine — see the module license note). DuckDB,
    Postgres, and every other dialect → the ``dbt-core`` fallback. The dialect
    comes from the resolved connection (the ``sql`` dialect axis).
    """
    name = dialect.strip().lower()
    # NOTE (license): the `fusion` engine here is dbt Core v2.0 (Apache-2.0),
    # the OSS relicensing — NOT the ELv2 commercial "dbt Fusion" build.
    engine = ENGINE_FUSION if name in _FUSION_DIALECTS else ENGINE_DBT_CORE
    return EnginePin(dbt_engine=engine, dbt_version=_DEFAULT_VERSIONS[engine])


def resolve_or_reuse(component: ComponentConfig, dialect: str) -> EnginePin:
    """Return the component's existing pin if it has one, else resolve fresh.

    A component that already carries ``dbt_engine`` **and** ``dbt_version`` is
    **reused, not re-resolved** (the "pinned and reused, not re-resolved"
    invariant). Otherwise the engine is resolved from ``dialect`` — the caller
    is responsible for persisting it via :func:`pin_engine`.
    """
    if component.dbt_engine is not None and component.dbt_version is not None:
        return EnginePin(dbt_engine=component.dbt_engine, dbt_version=component.dbt_version)
    return resolve_engine(dialect)


def pin_engine(component_name: str, pin: EnginePin, *, config_path: Path) -> None:
    """Write ``pin`` into the ``[components.<name>]`` block of ``config_path``.

    Uses ``tomlkit`` for a comment-preserving round-trip so the pin reads as a
    lockfile edit, not a rewrite. The ``[components.<name>]`` table must already
    exist (a convention-mode component with no block is pinned by first
    materializing its block — out of this slice's scope; here we require the
    block).
    """
    text = config_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)

    components = doc.get("components")
    if not isinstance(components, dict) or component_name not in components:
        raise KeyError(
            f"[components.{component_name}] not found in {config_path}; "
            "cannot pin engine into a non-existent component block."
        )
    block = components[component_name]
    if not isinstance(block, dict):
        raise ValueError(
            f"[components.{component_name}] in {config_path} is not a table; cannot pin."
        )

    block["dbt_engine"] = pin.dbt_engine
    block["dbt_version"] = pin.dbt_version

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


__all__ = [
    "ENGINE_DBT_CORE",
    "ENGINE_FUSION",
    "EnginePin",
    "pin_engine",
    "resolve_engine",
    "resolve_or_reuse",
]
