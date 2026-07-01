"""Add jobs.required_label — the runtime worker-placement column.

Revision ID: 0013_runtime_worker_placement
Revises: 0012_observability_telemetry
Create Date: 2026-07-01

The Increment-4 runtime's **worker-placement slice** turns "run this pipeline in a
specific place" into a claim-time filter. A worker advertises a label
(``carve worker --label onprem-dbt``); a pipeline whose referenced components set
``worker_label`` enqueues a job carrying a single derived ``required_label``; and
the ``claim_next`` query filters so a labeled job is only picked up by a matching
worker (unlabeled jobs still run anywhere — the flat pool). This migration adds
the one net-new column that carries the derivation onto the queue.

``required_label`` is declared ``sa.String()`` (nullable) to mirror its sibling
columns in 0008 — ``pipeline``/``target``/``error_message`` are all ``sa.String``,
and the ORM ``Job.required_label`` is a plain ``Mapped[str | None]`` (which renders
as ``String``). Postgres treats an unbounded ``VARCHAR`` and ``TEXT`` identically,
so this matches the capability spec's ``TEXT`` sketch while keeping the column
shaped exactly like the columns beside it.

**The column is added to BOTH ``jobs`` and ``jobs_archive``.** The archiver's
verify-then-delete does a column-list-agnostic ``INSERT INTO jobs_archive SELECT *
FROM jobs`` (``runtime/archiver.py``), which only round-trips while the archive
clone stays column-parallel with its active table. ``jobs_archive`` was created in
0011 as a ``LIKE jobs`` clone — a one-time copy that does NOT track later ``ALTER``s
— so adding ``required_label`` to ``jobs`` alone would leave the archive one column
short and break every ``jobs`` archive batch. Adding it to both keeps the clone
invariant the archiver relies on. (There are no ``*Archive`` ORM models, per the
archiver slice — the archive is raw ``SELECT *`` DDL, so this is a DDL-only edit.)

**No new index (deliberate).** The claim's inner ``SELECT`` already rides
``ix_jobs_claim_order`` (partial, ``WHERE status='queued'``) and ``required_label``
is a low-cardinality filter over the already-tiny queued set, so the existing
partial index bounds the scan at the target scale (100s of jobs/min). Forward
option: fold ``required_label`` into the claim index only if a large multi-label
backlog ever shows scan cost.

Downgrade drops the column from both tables, restoring 0012's schema exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_runtime_worker_placement"
down_revision: str | None = "0012_observability_telemetry"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable ``required_label`` column to ``jobs`` + its archive clone."""
    op.add_column("jobs", sa.Column("required_label", sa.String(), nullable=True))
    # Keep the archive clone column-parallel so the archiver's ``INSERT INTO
    # jobs_archive SELECT * FROM jobs`` still round-trips (see the module docstring).
    op.add_column("jobs_archive", sa.Column("required_label", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop ``required_label`` from both ``jobs`` and ``jobs_archive``."""
    op.drop_column("jobs_archive", "required_label")
    op.drop_column("jobs", "required_label")


__all__ = ["downgrade", "upgrade"]
