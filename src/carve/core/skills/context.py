"""`SkillContext` — the per-invocation state every skill receives.

Every skill takes a `SkillContext` as its first positional argument plus
its declared inputs as keyword arguments. The context bundles four
pieces of state that the catalog skills (and Pillar 2's manifest /
lineage skills) need:

- `config`: the loaded `Config` (used to resolve target connection blocks).
- `repo`: state-store `Repository` (used by `log()` for skill audit logs).
- `run_id`: the run id the active agent invocation is logging under.
- `target`: the active Snowflake target name (e.g. ``"prod"``).

`snowflake_pool` is constructed eagerly from the config so each skill
call hits the same pool (and thus the same cached driver connection).
Tests can pass a pre-built pool to override.
"""

from __future__ import annotations

from carve.core.config.schema import Config
from carve.core.connectors.snowflake import SnowflakePool
from carve.core.state.repository import Repository


class SkillContext:
    """State injected into every skill call.

    The context is constructed once per agent invocation and reused
    across every skill call within that invocation (which is also the
    boundary at which the skill executor's cache resets).
    """

    def __init__(
        self,
        config: Config,
        repo: Repository,
        run_id: str | None,
        target: str,
        *,
        snowflake_pool: SnowflakePool | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.run_id = run_id
        self.target = target
        # Tests inject a pre-built pool with mocked connections; production
        # callers leave `snowflake_pool=None` and let the context build one.
        self.snowflake_pool = (
            snowflake_pool if snowflake_pool is not None else SnowflakePool(config)
        )

    # ---- audit logging -------------------------------------------------------

    def log(self, message: str, level: str = "info") -> None:
        """Append a `skill`-source log line for the current run.

        When `run_id` is `None` (the plan flow doesn't open a Run row)
        the log is dropped — there's no FK target to attach it to.
        """
        if self.run_id is None:
            return
        self.repo.append_log(self.run_id, level, "skill", message)

    def emit_event(self, event: str, payload: dict[str, object]) -> None:
        """Forward to `log()` for now.

        Pillar 4 introduces a real event bus; for Pillar 1 we keep
        events in the same log stream so the structure is captured even
        without dedicated infrastructure.
        """
        self.log(f"event: {event} {payload}")
