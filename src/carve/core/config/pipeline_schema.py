"""Pydantic schema + loader for ``pipelines/<name>.toml``.

This is the binding contract for a pipeline definition: metadata, an
optional ``[seed_schedule]`` *seed*, and the ordered ``[[steps]]`` tables
that form a DAG. Steps reference dlt/dbt components **by name**
(``component = "<name>"``); ``sql`` steps stay inline (``file`` +
``connection``). The same TOML is identical across simple and multi mode
(see :mod:`carve.integrations.component_locator`).

The public entry point is :func:`load_pipeline`, which mirrors the
``core/config/loader.py`` pattern: ``tomllib.load`` -> ``model_validate``
-> structured error, then a cross-field validation pass (unique step ids,
``depends_on`` integrity, no cycles, valid cron, resolvable component
names). It raises :class:`PipelineError` for every user-facing failure so
the future ``carve pipelines validate`` surfaces one consistent shape.

Step typing
-----------
Each step type is a concrete pydantic model carrying the common step
fields (``id``/``depends_on``/``failure_mode``/``jinja_vars``) plus its
type-specific config; the three are a ``type``-discriminated union
(``PipelineStep``). The spec's ``DltStepConfig``/``DbtStepConfig``/
``SqlStepConfig`` names are the executor-facing aliases for those step
models — a ``dlt`` step exposes ``step.component`` directly, so the
Unit-2 executors read ``step.component`` / ``step.file`` off the step.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from carve.core.config.schema import ComponentType

if TYPE_CHECKING:
    from carve.core.config.paths import ProjectPaths
    from carve.core.config.schema import ComponentConfig


class PipelineError(Exception):
    """Raised for any user-facing pipeline-definition failure.

    Carries a one-line ``message`` plus an optional ``file``/``field``/
    ``hint`` so the loader can render an actionable, multi-line error —
    the same shape ``ConfigError`` uses, kept distinct because a pipeline
    failure is a per-pipeline concern (a bad TOML, a cycle, an
    unresolvable component) rather than a project-config one.
    """

    def __init__(
        self,
        message: str,
        *,
        file: Path | str | None = None,
        field: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.message = message
        self.file = Path(file) if file is not None else None
        self.field = field
        self.hint = hint
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [f"PipelineError: {self.message}"]
        if self.file is not None:
            lines.append(f"  File: {self.file}")
        if self.field is not None:
            lines.append(f"  Field: {self.field}")
        if self.hint is not None:
            lines.append(f"  Hint: {self.hint}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self._render()


class ConflictingWorkerLabelsError(PipelineError):
    """Raised when a pipeline's steps require ≥2 distinct worker labels.

    One job = one whole-DAG run on **one** worker, so a pipeline whose
    referenced components carry different ``worker_label``s cannot be
    placed on any single worker. A subclass of :class:`PipelineError` so
    :func:`load_pipeline` rejects it at author time with the loader's
    consistent error shape, and so the scheduler's enqueue resolver can
    catch the specific type without matching every pipeline failure.
    """


# ---------------------------------------------------------------------------
# Metadata / schedule seed / failure mode
# ---------------------------------------------------------------------------


class PipelineMeta(BaseModel):
    """``[pipeline]`` — free-form pipeline metadata."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    owner: str = ""


class SeedSchedule(BaseModel):
    """``[seed_schedule]`` — the schedule *seed*, not the live schedule.

    Applied to the ``schedules`` table at first registration only (the
    runtime owns the live schedule as data). ``paused``/``enabled`` are
    deliberately rejected (``extra="forbid"``): pause/resume is live data
    set via CLI/API/UI, never seeded from code. ``cron`` is validated via
    ``croniter`` in :func:`load_pipeline`.
    """

    model_config = ConfigDict(extra="forbid")

    cron: str
    timezone: str = "UTC"
    target: str = "prod"


class FailureMode(BaseModel):
    """``[steps.failure_mode]`` — how a step's failure affects the run.

    ``max_attempts``/``backoff``/``initial_delay_s``/``max_delay_s`` are
    only consulted when ``mode == "retry"``; they round-trip harmlessly
    otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fail", "warn", "continue", "retry", "skip_downstream"] = "fail"
    max_attempts: int = 1
    backoff: Literal["exponential", "linear", "fixed"] = "exponential"
    initial_delay_s: float = 5.0
    max_delay_s: float = 300.0


# ---------------------------------------------------------------------------
# Steps (a `type`-discriminated union)
# ---------------------------------------------------------------------------


class _StepBase(BaseModel):
    """Fields common to every step type.

    Concrete step models add their ``type`` discriminator + type-specific
    config. ``extra="forbid"`` is what rejects the old ``artifact`` key on
    a dlt step (and any stray field) at parse time — the migration-pointing
    message is layered on in :func:`load_pipeline`.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    depends_on: list[str] = Field(default_factory=list)
    failure_mode: FailureMode = Field(default_factory=FailureMode)
    jinja_vars: dict[str, str] = Field(default_factory=dict)


class DltStepConfig(_StepBase):
    """A ``dlt`` step: references a dlt component **by name** (required)."""

    type: Literal["dlt"] = "dlt"
    component: str
    write_disposition: Literal["append", "replace", "merge"] | None = None
    resource_select: list[str] | None = None


class DbtStepConfig(_StepBase):
    """A ``dbt`` step: references a dbt component by name (optional).

    Omitting ``component`` means "the single detected dbt project" in
    simple mode; graduation backfills the name. ``command`` defaults to
    ``build``.
    """

    type: Literal["dbt"] = "dbt"
    component: str | None = None
    command: Literal["build", "run", "test", "snapshot", "seed"] = "build"
    select: str | None = None
    exclude: str | None = None
    vars: dict[str, Any] = Field(default_factory=dict)
    full_refresh: bool = False


class SqlStepConfig(_StepBase):
    """A ``sql`` step: inline ``file`` + ``connection`` (no named component)."""

    type: Literal["sql"] = "sql"
    file: str
    connection: str


# The discriminated union: pydantic picks the variant off the `type` tag.
PipelineStep = Annotated[
    DltStepConfig | DbtStepConfig | SqlStepConfig,
    Field(discriminator="type"),
]


class Pipeline(BaseModel):
    """A fully-parsed, validated pipeline definition.

    ``name`` is derived from the TOML *filename*, never the file body —
    :func:`load_pipeline` injects it. The steps are a validated DAG by the
    time a ``Pipeline`` exists (see :func:`load_pipeline`).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    pipeline: PipelineMeta = Field(default_factory=PipelineMeta)
    seed_schedule: SeedSchedule | None = None
    steps: list[PipelineStep] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_pipeline(
    path: Path,
    *,
    components: dict[str, ComponentConfig],
    paths: ProjectPaths,
) -> Pipeline:
    """Load and fully validate ``pipelines/<name>.toml``.

    Parsing mirrors ``core/config/loader.py``: ``tomllib.load`` ->
    ``Pipeline.model_validate`` -> the cross-field validation pass. The
    pipeline ``name`` is derived from the filename (``stripe.toml`` ->
    ``stripe``), never read from the TOML body.

    Cross-field validation covers: unique step ids; ``depends_on`` refs
    all exist; no cycles; valid cron (when ``[seed_schedule]`` is present);
    and component-name resolvability for ``dlt``/``dbt`` steps via the
    shipped locator (an omitted dbt ``component`` resolves to the single
    detected dbt project). The old ``artifact`` key on a ``dlt`` step is
    rejected with a migration-pointing message.

    Raises:
        PipelineError: For any user-facing failure (bad TOML, schema
            violation, duplicate ids, dangling ``depends_on``, cycle, bad
            cron, unresolvable component name).
    """
    name = path.stem
    raw = _parse_toml(path)

    # Surface the `artifact`-key migration before the generic
    # `extra_forbidden` error so the user gets the pointed message.
    _reject_artifact_key(raw, path)

    raw_for_model = {**raw, "name": name}
    try:
        pipeline = Pipeline.model_validate(raw_for_model)
    except ValidationError as exc:
        raise _validation_error_to_pipeline_error(exc, path) from exc

    _validate_unique_step_ids(pipeline, path)
    _validate_depends_on_refs(pipeline, path)
    _validate_no_cycles(pipeline, path)
    _validate_cron(pipeline, path)
    _validate_components_resolve(pipeline, path, components=components, paths=paths)
    _validate_worker_labels(pipeline, path, components=components)

    return pipeline


def resolve_required_label(
    pipeline: Pipeline,
    components: dict[str, ComponentConfig],
) -> str | None:
    """Reduce a pipeline's referenced components' ``worker_label``s to one label.

    Placement is **per-pipeline-run**: a job is one whole-DAG run on one
    worker, so a job's ``required_label`` is the *single* label its steps'
    components all agree on. This pure helper is the single source of that
    reduction — called from both :func:`load_pipeline` (author-time reject)
    and the scheduler's enqueue resolver (stamp-time read), so the two can
    never disagree.

    The reduction over the non-``None`` labels of every dlt/dbt step that
    names a component:

    * **0** labeled components → ``None`` (runs anywhere — the flat pool).
    * **exactly 1** distinct label → that label.
    * **≥ 2** distinct labels → :class:`ConflictingWorkerLabelsError` (the
      pipeline can't be placed on one worker).

    ``worker_label`` lives only on :class:`ComponentConfig` today, so the
    mechanism is **general** (it serves ``dlt``/``dbt``/``sql`` alike — a
    label on *any* referenced component counts) even though the config
    source is component-scoped. Fenced follow-ups (deliberately not read
    here): a step-level ``worker_label`` (the ``extra="forbid"`` step
    models), a label on a ``sql`` step (it references no component), and a
    label on a dlt/sql component config (the field is on the shared
    ``ComponentConfig`` only). A component-less dbt step (single detected
    project, no ``ComponentConfig`` entry) contributes no label.
    """
    labels: set[str] = set()
    for step in pipeline.steps:
        if isinstance(step, SqlStepConfig):
            # A sql step references no component — no label to source (fenced).
            continue
        # dlt (component required) / dbt (component optional).
        component_name = step.component
        if component_name is None:
            continue
        component = components.get(component_name)
        if component is None or component.worker_label is None:
            # Convention-resolved component (no ``ComponentConfig`` entry) or an
            # unlabeled one contributes no label — general to dlt/dbt/sql.
            continue
        labels.add(component.worker_label)

    if not labels:
        return None
    if len(labels) == 1:
        return next(iter(labels))
    raise ConflictingWorkerLabelsError(
        f"Pipeline {pipeline.name!r} steps require different worker labels "
        f"{sorted(labels)}; a pipeline runs on one worker, so it cannot be placed.",
        field="steps.*.component.worker_label",
        hint="Give the conflicting components a single shared `worker_label`, or "
        "split the pipeline so each label's steps run in their own pipeline.",
    )


# ---------------------------------------------------------------------------
# Parsing + error translation
# ---------------------------------------------------------------------------


def _parse_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise PipelineError(
            f"Failed to parse pipeline TOML: {exc}",
            file=path,
            hint="Check the file for syntax errors (unbalanced quotes, bad escapes, etc.).",
        ) from exc
    except OSError as exc:
        raise PipelineError(
            f"Failed to read pipeline file: {exc}",
            file=path,
        ) from exc


def _reject_artifact_key(raw: dict[str, Any], path: Path) -> None:
    """Reject the retired ``artifact`` key on a step with a migration hint.

    ``artifact`` lived only on dlt steps; it was renamed to ``component``
    (unifying dlt + dbt step references under one name-based key). A stray
    ``artifact`` would otherwise surface as a generic ``extra_forbidden``
    error; intercept it so the user gets the rename instruction.
    """
    steps = raw.get("steps")
    if not isinstance(steps, list):
        return
    for index, step in enumerate(steps):
        if isinstance(step, dict) and "artifact" in step:
            step_id = step.get("id", f"#{index}")
            raise PipelineError(
                f"Step {step_id!r} uses the removed `artifact` key.",
                file=path,
                field=f"steps.{index}.artifact",
                hint="`artifact` was renamed to `component`: replace "
                '`artifact = "<name>"` with `component = "<name>"`.',
            )


def _validation_error_to_pipeline_error(exc: ValidationError, path: Path) -> PipelineError:
    """Render the first pydantic error as a structured ``PipelineError``."""
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always returns at least one
        return PipelineError(str(exc), file=path)

    err = errors[0]
    loc = tuple(str(part) for part in err.get("loc", ()))
    field = ".".join(loc) if loc else None
    err_type = err.get("type", "")
    msg = err.get("msg", "validation failed")

    if err_type == "missing":
        message = f"Required field '{field}' is missing"
        hint = "Add this field to the step/section in the file above."
    elif err_type == "extra_forbidden":
        message = f"Unknown field '{field}'"
        hint = "Remove the field, or check for a typo against the pipeline schema."
    elif err_type == "union_tag_invalid":
        message = f"Unknown step type at '{field}': {msg}"
        hint = "A step `type` must be one of: dlt, dbt, sql."
    else:
        message = f"Invalid value for '{field}': {msg}"
        hint = None

    return PipelineError(message, file=path, field=field, hint=hint)


# ---------------------------------------------------------------------------
# Cross-field validation
# ---------------------------------------------------------------------------


def _validate_unique_step_ids(pipeline: Pipeline, path: Path) -> None:
    seen: set[str] = set()
    for step in pipeline.steps:
        if step.id in seen:
            raise PipelineError(
                f"Duplicate step id: {step.id!r}",
                file=path,
                hint="Each step `id` must be unique within a pipeline.",
            )
        seen.add(step.id)


def _validate_depends_on_refs(pipeline: Pipeline, path: Path) -> None:
    ids = {step.id for step in pipeline.steps}
    for step in pipeline.steps:
        for dep in step.depends_on:
            if dep not in ids:
                raise PipelineError(
                    f"Step {step.id!r} depends on unknown step {dep!r}",
                    file=path,
                    hint="Every `depends_on` entry must name an existing step id.",
                )


def _validate_no_cycles(pipeline: Pipeline, path: Path) -> None:
    """Detect a dependency cycle via DFS over the ``depends_on`` edges.

    Kept self-contained (rather than constructing a ``PipelineDAG``) so
    ``load_pipeline`` has no import dependency on the ``runtime`` package;
    ``PipelineDAG`` re-runs the same check at construction as a belt-and-
    braces guard for callers that build a DAG directly.
    """
    adjacency = {step.id: list(step.depends_on) for step in pipeline.steps}
    # States: 0 = unvisited, 1 = on the current DFS stack, 2 = done.
    state: dict[str, int] = dict.fromkeys(adjacency, 0)

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for dep in adjacency[node]:
            if state[dep] == 1:
                cycle = [*stack[stack.index(dep) :], dep]
                raise PipelineError(
                    f"Dependency cycle detected: {' -> '.join(cycle)}",
                    file=path,
                    hint="Steps must form a DAG; remove the circular `depends_on`.",
                )
            if state[dep] == 0:
                visit(dep, stack)
        stack.pop()
        state[node] = 2

    for step_id in adjacency:
        if state[step_id] == 0:
            visit(step_id, [])


def _validate_cron(pipeline: Pipeline, path: Path) -> None:
    if pipeline.seed_schedule is None:
        return
    from croniter import croniter

    expr = pipeline.seed_schedule.cron
    if not croniter.is_valid(expr):
        raise PipelineError(
            f"Invalid cron expression in [seed_schedule]: {expr!r}",
            file=path,
            field="seed_schedule.cron",
            hint="Use a 5-field cron expression, e.g. `0 2 * * *` (2am daily).",
        )


def _validate_components_resolve(
    pipeline: Pipeline,
    path: Path,
    *,
    components: dict[str, ComponentConfig],
    paths: ProjectPaths,
) -> None:
    """Confirm each ``dlt``/``dbt`` step's ``component`` name resolves.

    ``sql`` steps reference a file + connection inline and have no
    component to resolve. A ``dlt`` step's ``component`` is required (the
    schema enforces presence); a ``dbt`` step's omitted ``component``
    resolves to the single detected dbt project. Resolution delegates to
    the shipped locator — an unresolvable name is a validation error here
    (the logic the future ``carve pipelines validate`` calls).

    The locator imports are deferred to this function body: importing them
    at module top creates a ``core.config <-> integrations.component_locator``
    import cycle (the locator imports ``core.config.schema``, whose package
    ``__init__`` re-exports this module), which broke a bare
    ``import carve.integrations.component_locator`` in a fresh interpreter.
    Deferring the import here keeps the cycle from forming at import time.
    """
    from carve.integrations.component_locator import (
        ComponentResolutionError,
        _detect_dbt_project,
        resolve_component,
    )

    for step in pipeline.steps:
        if isinstance(step, SqlStepConfig):
            continue
        if isinstance(step, DbtStepConfig) and step.component is None:
            # Omitted dbt component -> the single detected dbt project.
            try:
                _detect_dbt_project(paths, required=True)
            except ComponentResolutionError as exc:
                raise PipelineError(
                    f"Step {step.id!r} (dbt) omits `component` and no single dbt "
                    f"project could be detected: {exc.message}",
                    file=path,
                    field=f"steps.{step.id}.component",
                    hint=exc.hint,
                ) from exc
            continue

        component_name = step.component
        assert component_name is not None  # dlt always; named dbt here
        try:
            resolved = resolve_component(component_name, components=components, paths=paths)
        except ComponentResolutionError as exc:
            raise PipelineError(
                f"Step {step.id!r} references component {component_name!r} "
                f"which does not resolve: {exc.message}",
                file=path,
                field=f"steps.{step.id}.component",
                hint=exc.hint,
            ) from exc
        # Enforce step-type / component-type agreement: a `dbt` step must not
        # reference a `dlt` component (and vice versa), or the executor would
        # dispatch the wrong engine at run time.
        expected = ComponentType.DLT if isinstance(step, DltStepConfig) else ComponentType.DBT
        if resolved.type is not expected:
            raise PipelineError(
                f"Step {step.id!r} is a {step.type} step but component "
                f"{component_name!r} is a {resolved.type.value} component.",
                file=path,
                field=f"steps.{step.id}.component",
                hint=f"Reference a {step.type} component, or change the step's type.",
            )


def _validate_worker_labels(
    pipeline: Pipeline,
    path: Path,
    *,
    components: dict[str, ComponentConfig],
) -> None:
    """Reject a pipeline whose components require ≥2 distinct worker labels.

    Delegates the reduction to :func:`resolve_required_label` (the same helper
    the scheduler's enqueue resolver calls, so author-time and stamp-time never
    disagree) and re-raises its typed :class:`ConflictingWorkerLabelsError` with
    the file attached — an actionable author-time error, the same shape
    :func:`_validate_components_resolve` produces.
    """
    try:
        resolve_required_label(pipeline, components)
    except ConflictingWorkerLabelsError as exc:
        raise ConflictingWorkerLabelsError(
            exc.message, file=path, field=exc.field, hint=exc.hint
        ) from exc


__all__ = [
    "ConflictingWorkerLabelsError",
    "DbtStepConfig",
    "DltStepConfig",
    "FailureMode",
    "Pipeline",
    "PipelineError",
    "PipelineMeta",
    "PipelineStep",
    "SeedSchedule",
    "SqlStepConfig",
    "load_pipeline",
    "resolve_required_label",
]
