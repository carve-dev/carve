"""Add the agent-telemetry tables: agents, agent_invocations, skill_calls.

Revision ID: 0012_observability_telemetry
Revises: 0011_runtime_archive
Create Date: 2026-06-30

The **observability** capability owns the previously-orphaned agent-telemetry
tables (every agent writes them; no migration created them). This slice creates
and owns all three, so a delegated subagent run can persist one
``agent_invocations`` row (tokens / cost / duration / status) + one ``skill_calls``
row per tool call, correlated to the run/plan/build that triggered it:

* ``agents`` — the registry projection of discovered agents (``name`` natural
  PK). The recording path upserts a minimal row so the ``agent_invocations``
  FK parent always exists; extensibility fills the richer JSONB projection.
* ``agent_invocations`` — one row per subagent invocation. ``id`` is an
  **app-generated String** minted at invocation-open (mirrors ``runs``/
  ``step_runs``) so ``skill_calls`` can reference it without a mid-recording
  flush. All four **external** correlation ids — ``run_id``/``plan_id``/
  ``build_id``/``ask_id`` — are **nullable plain columns with NO FK constraint**:
  they are "orphaned telemetry pointers" (the design this capability describes),
  recorded by value, never constraint-enforced. The reasons are uniform:

  - ``run_id`` — archiver-safety: an FK to ``runs`` would let an un-archived
    invocation child block the runtime archiver's aged-out ``runs`` DELETE and
    silently halt ``runs`` archival forever. The id value is kept (not
    ``ON DELETE SET NULL``) so correlation to ``runs_archive`` (same id) resolves.
  - ``plan_id`` — record-before-parent ordering: ``generate_plan`` mints the
    ``plan_id`` and runs the engineers *with recording live* **before** it
    inserts the ``plans`` row, so an FK would raise ``IntegrityError`` at
    ``open_invocation`` commit time and drop the whole plan-path invocation +
    its skill calls. The value is stamped now; the ``plans`` row lands moments
    later carrying the same id.
  - ``build_id`` — same record-before-parent ordering: the ``builds`` row is
    written only after the review fan-out passes, long after the engineers ran.
  - ``ask_id`` — the ``asks`` table does not exist until Increment 5.

  Only the two **internal** parents are enforced FKs (they always exist first):
  ``agent_name → agents.name`` here and ``skill_calls.agent_invocation_id →
  agent_invocations.id`` below.
* ``skill_calls`` — one row per skill/tool call, with the §6.4 bounded-result
  signals (``result_too_large``/``pages_walked``/``output_size``). ``id`` is
  ``BIGSERIAL`` (append-only child, no descendants — mirrors ``events``/
  ``logs``/``schedule_changes``).

Access-pattern indexes back the ``carve metrics`` rollups + per-run drill-downs:
``agent_invocations(agent_name, started_at DESC)``, ``agent_invocations(run_id)``,
``skill_calls(agent_invocation_id)``. Downgrade drops the indexes then the three
tables (children first), restoring 0011's schema exactly.

[DEFERRED — telemetry-table archival/retention]: ``agent_invocations`` /
``skill_calls`` are themselves *unarchived* and grow unbounded over time. A
future archiver-extension slice adds their ``*_archive`` clones + retention
windows, archiving these children *before* the ``runs`` pass. Fenced honestly
here, the way the runtime slices fenced their deferred work.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0012_observability_telemetry"
down_revision: str | None = "0011_runtime_archive"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``agents``, ``agent_invocations``, ``skill_calls`` + their indexes."""
    op.create_table(
        "agents",
        # ``name`` is the natural String PK — the discovered agent's name.
        sa.Column("name", sa.String(), primary_key=True, nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("system_prompt_path", sa.String(), nullable=True),
        sa.Column("allowed_skills", JSONB, nullable=True),
        sa.Column("guardrails", JSONB, nullable=True),
        sa.Column("specialization", JSONB, nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "agent_invocations",
        # App-generated String id, minted at open_invocation (mirrors runs/step_runs)
        # so skill_calls can reference it without a mid-recording flush.
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "agent_name",
            sa.String(),
            sa.ForeignKey("agents.name", name="fk_agent_invocations_agent_name"),
            nullable=False,
        ),
        # ``run_id`` is a nullable plain column with NO FK constraint. The
        # runtime archiver ages-out and DELETEs terminal ``runs`` rows; an FK
        # here would let an un-archived ``agent_invocations`` child block that
        # DELETE and silently halt ``runs`` archival forever (unbounded growth).
        # The id value is kept (NOT ``ON DELETE SET NULL``) so correlation to
        # ``runs_archive`` (same id) still resolves post-archival.
        sa.Column("run_id", sa.String(), nullable=True),
        # ``plan_id``/``build_id`` are nullable plain columns with NO FK
        # constraint. ``generate_plan`` records invocations BEFORE it inserts the
        # ``plans`` row (and the ``builds`` row only lands after the review
        # fan-out passes), so an FK here would raise IntegrityError at
        # open_invocation commit time and silently drop the plan/build-path
        # telemetry. The id value is stamped now; the parent row lands later
        # carrying the same id, so correlation still resolves.
        sa.Column("plan_id", sa.String(), nullable=True),
        sa.Column("build_id", sa.String(), nullable=True),
        # ``ask_id`` is a nullable plain column with NO FK constraint — the
        # ``asks`` table does not exist yet (Increment 5). The FK lands when
        # ask ships; until then the correlation is recorded, uncontrolled.
        sa.Column("ask_id", sa.String(), nullable=True),
        sa.Column("tokens_input", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # "recent invocations for agent X" — the per-agent drill-down + metrics scan.
    op.create_index(
        "ix_agent_invocations_agent_name_started_at",
        "agent_invocations",
        ["agent_name", sa.text("started_at DESC")],
    )
    # "invocations for run X" — the per-run correlation lookup.
    op.create_index(
        "ix_agent_invocations_run_id",
        "agent_invocations",
        ["run_id"],
    )

    op.create_table(
        "skill_calls",
        # BIGSERIAL: an append-only child log id, DB-generated — mirrors
        # ``events``/``logs``/``schedule_changes``, not the app-generated
        # String entity ids.
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "agent_invocation_id",
            sa.String(),
            sa.ForeignKey("agent_invocations.id", name="fk_skill_calls_agent_invocation_id"),
            nullable=False,
        ),
        sa.Column("skill_name", sa.String(), nullable=False),
        sa.Column("input_hash", sa.String(), nullable=True),
        sa.Column("output_size", sa.Integer(), nullable=True),
        # §6.4 bounded-result signals.
        sa.Column(
            "result_too_large",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("pages_walked", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # "skill calls for invocation X" — the per-invocation drill-down + the
    # metrics skill-call-mix join.
    op.create_index(
        "ix_skill_calls_agent_invocation_id",
        "skill_calls",
        ["agent_invocation_id"],
    )


def downgrade() -> None:
    """Drop ``skill_calls``, ``agent_invocations``, ``agents`` (children first)."""
    op.drop_index("ix_skill_calls_agent_invocation_id", table_name="skill_calls")
    op.drop_table("skill_calls")

    op.drop_index("ix_agent_invocations_run_id", table_name="agent_invocations")
    op.drop_index("ix_agent_invocations_agent_name_started_at", table_name="agent_invocations")
    op.drop_table("agent_invocations")

    op.drop_table("agents")


__all__ = ["downgrade", "upgrade"]
