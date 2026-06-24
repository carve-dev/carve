"""The backend-uniform dbt run result — the single normalization seam.

Every dbt-execution backend (``local`` here; the managed backends later) returns
the *same* :class:`DbtRunResult` regardless of how the run was produced. The
``dbt`` step type (deferred) and the dbt-engineer agent loop read this uniform
shape and never branch on which backend ran the build.

The normalization rides the **already-shipped** substrate
(:func:`carve.integrations.dbt.verify.read_run_results`): that function reads
``target/run_results.json`` (optionally manifest-assisted for readable names)
into a :class:`~carve.integrations.dbt.verify.DbtRunReport`, applying dbt's
fail-closed verdict (a clean exit with no readable artifact is **not** green).
:meth:`DbtRunResult.from_report` adapts that report + the finished process into
the uniform model — it does **not** re-parse ``run_results.json``; the
per-node detail it surfaces comes from the report's ``nodes`` tuple.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from carve.integrations.dbt.verify import DbtRunReport

# The three terminal states a dbt run normalizes to.
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"


class PerModelResult(BaseModel):
    """One node's outcome, normalized from dbt's ``run_results.json``.

    ``status`` is dbt's raw per-node status (``success``/``error``/``skipped``
    for build nodes, ``pass``/``fail`` for tests). ``failures`` is the count a
    failing test reports (``None`` for build nodes / clean tests).
    """

    model_config = ConfigDict(extra="forbid")

    unique_id: str
    name: str
    status: str
    message: str | None = None
    failures: int | None = None


class DbtRunResult(BaseModel):
    """The backend-uniform result of one dbt invocation.

    ``status`` is the run-level verdict: ``"success"`` (every node ok),
    ``"failed"`` (a node failed per the on-disk artifact), or ``"error"`` (the
    run never produced a readable artifact — fail-closed). ``manifest_ref`` /
    ``run_results_ref`` point at the on-disk artifacts (for lineage + audit);
    ``logs`` is the captured combined stdout/stderr; ``cost`` is reserved for
    backends that meter spend (the managed ones), ``None`` for local.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    per_model: list[PerModelResult]
    manifest_ref: str | None = None
    run_results_ref: str | None = None
    logs: str = ""
    duration_ms: int = 0
    cost: float | None = None

    @classmethod
    def from_report(
        cls,
        report: DbtRunReport | None,
        *,
        returncode: int,
        logs: str = "",
        duration_ms: int = 0,
        manifest_ref: str | None = None,
        run_results_ref: str | None = None,
        cost: float | None = None,
    ) -> DbtRunResult:
        """Normalize a finished dbt run into the uniform :class:`DbtRunResult`.

        ``report`` is :func:`carve.integrations.dbt.verify.read_run_results`'s
        output (``None`` when the artifact was absent/malformed). This is the
        single normalization seam — it rides the shipped report and never
        re-parses ``run_results.json``.

        Verdict (fail-closed, inherited from the substrate):

        * ``report is None`` → ``"error"`` — no readable artifact. A clean exit
          (``returncode == 0``) with no artifact is **not** trusted as green.
        * ``report.failed_nodes`` non-empty, or a non-zero ``returncode`` → ``"failed"``.
        * else → ``"success"``.
        """
        if report is None:
            return cls(
                status=STATUS_ERROR,
                per_model=[],
                manifest_ref=manifest_ref,
                run_results_ref=run_results_ref,
                logs=logs,
                duration_ms=duration_ms,
                cost=cost,
            )

        per_model = [
            PerModelResult(
                unique_id=node.unique_id,
                name=node.name,
                status=node.status,
                message=node.message,
                failures=node.failures,
            )
            for node in report.nodes
        ]
        if report.failed_nodes or returncode != 0:
            status = STATUS_FAILED
        else:
            status = STATUS_SUCCESS
        return cls(
            status=status,
            per_model=per_model,
            manifest_ref=manifest_ref,
            run_results_ref=run_results_ref,
            logs=logs,
            duration_ms=duration_ms,
            cost=cost,
        )


__all__ = [
    "STATUS_ERROR",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "DbtRunResult",
    "PerModelResult",
]
