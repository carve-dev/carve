"""Unit tests for `CachedSkillExecutor` cache behavior + target wiring."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from carve.core.config.schema import (
    Config,
    ConnectionsConfig,
    ModelsConfig,
    ProjectConfig,
)
from carve.core.config.schema import SnowflakeConnection as ConnConfig
from carve.core.connectors.snowflake import SnowflakePool
from carve.core.skills.context import SkillContext
from carve.core.skills.decorator import skill
from carve.core.skills.executor import CachedSkillExecutor, SkillNotFound
from carve.core.skills.registry import SkillRegistry
from carve.core.skills.result import SkillResult


def _config(targets: dict[str, ConnConfig] | None = None) -> Config:
    return Config(
        project=ProjectConfig(name="t"),
        connections=ConnectionsConfig(snowflake=targets or {}),
        models=ModelsConfig(anthropic_api_key="x"),
    )


def _ctx(target: str = "dev", pool: SnowflakePool | None = None) -> SkillContext:
    config = _config()
    return SkillContext(
        config=config,
        repo=MagicMock(),
        run_id=None,
        target=target,
        snowflake_pool=pool if pool is not None else SnowflakePool(config),
    )


def _make_counting_skill() -> tuple[Any, list[int]]:
    """Build a skill that records each invocation in a counter list."""
    counter: list[int] = []

    @skill(
        name="counter",
        description="Counts calls.",
        inputs={"x": {"type": "string", "required": True}},
    )
    def counter_fn(ctx: SkillContext, x: str) -> SkillResult:
        counter.append(len(counter) + 1)
        return SkillResult(data={"value": x, "n": len(counter)})

    return counter_fn, counter


def test_executor_caches_within_invocation() -> None:
    """Second call with same args hits the cache; the skill body runs once."""
    fn, counter = _make_counting_skill()
    registry = SkillRegistry()
    registry.register(fn)
    executor = CachedSkillExecutor(registry)
    ctx = _ctx()

    a = executor.execute("counter", {"x": "hi"}, ctx)
    b = executor.execute("counter", {"x": "hi"}, ctx)

    assert a is b  # same SkillResult instance
    assert len(counter) == 1


def test_executor_does_not_cache_across_invocations() -> None:
    """A new executor (one per agent invocation) gets a fresh cache."""
    fn, counter = _make_counting_skill()
    registry = SkillRegistry()
    registry.register(fn)
    ctx = _ctx()

    executor1 = CachedSkillExecutor(registry)
    executor1.execute("counter", {"x": "hi"}, ctx)
    executor2 = CachedSkillExecutor(registry)
    executor2.execute("counter", {"x": "hi"}, ctx)

    assert len(counter) == 2


def test_executor_caches_per_argument_combination() -> None:
    """Different kwargs yield different cache entries."""
    fn, counter = _make_counting_skill()
    registry = SkillRegistry()
    registry.register(fn)
    executor = CachedSkillExecutor(registry)
    ctx = _ctx()

    executor.execute("counter", {"x": "a"}, ctx)
    executor.execute("counter", {"x": "b"}, ctx)
    executor.execute("counter", {"x": "a"}, ctx)

    assert len(counter) == 2  # second "a" hits the cache


def test_executor_caches_with_structured_inputs() -> None:
    """The cache key handles list/dict/None values without raising.

    Pillar 2's dbt-manifest skills are likely to declare object/array
    inputs (filter dicts, model-name lists). The cache must not fail or
    silently miss when those arrive — the JSON-key serialization makes
    structured kwargs first-class.
    """
    counter: list[int] = []

    @skill(
        name="structured",
        description="Counts calls; accepts structured inputs.",
        inputs={
            "filters": {"type": "object", "required": False},
            "names": {"type": "array", "required": False},
        },
    )
    def structured(
        ctx: SkillContext,
        filters: dict[str, Any] | None = None,
        names: list[str] | None = None,
    ) -> SkillResult:
        counter.append(1)
        return SkillResult(data={"count": len(counter)})

    registry = SkillRegistry()
    registry.register(structured)
    executor = CachedSkillExecutor(registry)
    ctx = _ctx()

    args1 = {"filters": {"a": 1, "b": [2, 3]}, "names": ["x", "y"]}
    args2 = {"filters": {"b": [2, 3], "a": 1}, "names": ["x", "y"]}  # reordered keys
    args3 = {"filters": {"a": 1, "b": [2, 3]}, "names": ["y", "x"]}  # different list order

    executor.execute("structured", args1, ctx)
    executor.execute("structured", args2, ctx)  # same content → cache hit
    executor.execute("structured", args3, ctx)  # different list order → distinct entry

    assert len(counter) == 2


def test_executor_raises_on_unknown_skill() -> None:
    """Unknown skill name surfaces as a `SkillNotFound`."""
    registry = SkillRegistry()
    executor = CachedSkillExecutor(registry)
    with pytest.raises(SkillNotFound):
        executor.execute("missing", {}, _ctx())


def test_skill_uses_active_target() -> None:
    """`ctx.target` selects which `[snowflake.<target>]` block is used.

    We give the pool two connections (dev + prod) with distinct
    accounts, run the same skill with `ctx.target = "prod"`, and assert
    the pool returned the prod connection.
    """
    dev = ConnConfig(
        account="dev.acct",
        user="u",
        password="p",
        role="R",
        warehouse="W",
        database="DB",
    )
    prod = ConnConfig(
        account="prod.acct",
        user="u",
        password="p",
        role="R",
        warehouse="W",
        database="DB",
    )
    config = _config({"dev": dev, "prod": prod})
    pool = SnowflakePool(config)

    captured: dict[str, str] = {}

    @skill(
        name="probe",
        description="Records the connection account.",
        inputs={},
    )
    def probe(ctx: SkillContext) -> SkillResult:
        sf = ctx.snowflake_pool.get(ctx.target)
        captured["account"] = sf.config.account
        return SkillResult(data={"ok": True})

    registry = SkillRegistry()
    registry.register(probe)
    executor = CachedSkillExecutor(registry)
    ctx = SkillContext(
        config=config,
        repo=MagicMock(),
        run_id=None,
        target="prod",
        snowflake_pool=pool,
    )
    executor.execute("probe", {}, ctx)
    assert captured["account"] == "prod.acct"


def test_skill_must_return_skill_result() -> None:
    """A skill that returns a non-SkillResult raises a TypeError."""

    @skill(name="bad", description="Returns wrong shape.", inputs={})
    def bad(ctx: SkillContext) -> Any:
        return {"raw": True}

    registry = SkillRegistry()
    registry.register(bad)
    executor = CachedSkillExecutor(registry)
    with pytest.raises(TypeError, match="must return a SkillResult"):
        executor.execute("bad", {}, _ctx())
