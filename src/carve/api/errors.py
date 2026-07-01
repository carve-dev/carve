"""RFC 9457 ``application/problem+json`` error handling for the REST API.

Two responsibilities:

* :func:`problem` — build a problem+json ``JSONResponse`` with a stable
  ``https://carve.dev/errors/<slug>`` ``type`` URL, ``title``, ``status``,
  optional ``detail``/``instance``, and arbitrary custom fields.
* :class:`ProblemJsonExceptionHandler` + :func:`install_error_handlers` — turn
  the codebase's *scattered* domain exceptions (there is **no** unified
  ``CarveError`` base) into problem+json.

**Design decision (mapping table, not a base-class retrofit).** Rather than
introduce a ``CarveError`` base and retrofit every module in this slice, the
handler keys an explicit ``exception → (type-slug, status, title)`` table by the
exception's *fully-qualified class name* (walking the MRO). That means this
module imports **none** of the heavy orchestrator/agent modules the exceptions
live in — it matches by name and reads attributes with ``getattr``. An
unrecognized exception becomes 500 ``.../errors/internal`` with the stack trace
logged (never returned to the client). API-local signalling exceptions
(:class:`ResourceNotFound`/:class:`BadRequest`/:class:`Conflict`) carry their own
status/type and are handled by ``isinstance`` (same module, no import cost).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

#: Every problem ``type`` is this base + a stable slug. Documented + versioned;
#: clients switch on the slug, never on ``title``/``detail``.
_ERROR_BASE = "https://carve.dev/errors/"

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


def problem(
    status: int,
    type_slug: str,
    title: str,
    *,
    detail: str | None = None,
    instance: str | None = None,
    **extra: Any,
) -> JSONResponse:
    """Build an RFC 9457 problem+json response.

    ``type`` is ``_ERROR_BASE + type_slug``. ``detail``/``instance`` and any
    non-``None`` ``extra`` custom fields (e.g. ``plan_id``,
    ``expected_config_hash``) are merged into the body.
    """
    body: dict[str, Any] = {
        "type": _ERROR_BASE + type_slug,
        "title": title,
        "status": status,
    }
    if detail is not None:
        body["detail"] = detail
    if instance is not None:
        body["instance"] = instance
    for key, value in extra.items():
        if value is not None:
            body[key] = value
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON_MEDIA_TYPE)


# ---------------------------------------------------------------------------
# API-local signalling exceptions (raised by routers)
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """Base for router-raised problems that carry their own status/type/title.

    Subclasses set the class attributes; the instance carries an optional
    ``detail`` and arbitrary problem-body ``extra`` fields.
    """

    status: int = 500
    type_slug: str = "internal"
    title: str = "Internal server error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail
        self.extra = extra
        super().__init__(detail or self.title)


class Unauthorized(ApiError):
    """The request lacks valid authentication (→ 401)."""

    status = 401
    type_slug = "unauthorized"
    title = "Unauthorized"


class ResourceNotFound(ApiError):
    """A requested resource does not exist (→ 404)."""

    status = 404
    type_slug = "not-found"
    title = "Resource not found"


class BadRequest(ApiError):
    """The request was malformed or semantically invalid (→ 400)."""

    status = 400
    type_slug = "bad-request"
    title = "Bad request"


class Conflict(ApiError):
    """The request conflicts with current state (→ 409)."""

    status = 409
    type_slug = "conflict"
    title = "Conflict"


# ---------------------------------------------------------------------------
# Domain-exception mapping (by fully-qualified name — no heavy imports)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ErrorSpec:
    type_slug: str
    status: int
    title: str


#: Real, shipped exceptions → problem spec. Keyed by ``module.qualname`` so this
#: module imports none of them; the MRO walk matches a subclass at its nearest
#: registered ancestor (e.g. ``MaxTurnsExceeded`` → ``AgentError``).
_ERROR_MAP: dict[str, _ErrorSpec] = {
    "carve.cli.orchestrator.builder.ConfigDriftError": _ErrorSpec(
        "config-drift", 409, "Plan was generated against a different config"
    ),
    "carve.cli.orchestrator.builder.PlanExpiredError": _ErrorSpec(
        "plan-expired", 409, "Plan has expired"
    ),
    "carve.cli.orchestrator.builder.BuildError": _ErrorSpec(
        "build-failed", 400, "Build could not proceed"
    ),
    "carve.cli.orchestrator.planner.PlanGenerationError": _ErrorSpec(
        "plan-generation", 422, "Plan generation failed"
    ),
    "carve.core.memory.writer.DecisionAlreadyExists": _ErrorSpec(
        "decision-exists", 409, "Decision already exists"
    ),
    "carve.core.config.exceptions.ConfigError": _ErrorSpec(
        "config-invalid", 400, "Configuration error"
    ),
    "carve.core.config.pipeline_schema.PipelineError": _ErrorSpec(
        "pipeline-invalid", 400, "Pipeline definition error"
    ),
    "carve.core.connectors.exceptions.SnowflakeError": _ErrorSpec(
        "snowflake", 502, "Snowflake error"
    ),
    "carve.core.targets.resolution.TargetResolutionError": _ErrorSpec(
        "target-resolution", 400, "Target resolution error"
    ),
    "carve.core.agents.exceptions.AgentError": _ErrorSpec("agent", 500, "Agent run failed"),
    "carve.core.hooks.runner.HookExecutionError": _ErrorSpec(
        "hook-execution", 500, "Hook execution failed"
    ),
    "carve.core.hooks.config.HookConfigError": _ErrorSpec(
        "hook-config", 400, "Hook configuration error"
    ),
    "carve.core.deploy.ddl_applier.UnsafeDdlError": _ErrorSpec(
        "unsafe-ddl", 400, "Unsafe DDL rejected"
    ),
    "carve.core.mcp.config.McpConfigError": _ErrorSpec(
        "mcp-config", 400, "MCP configuration error"
    ),
    "carve.core.agents.loader.AgentLoadError": _ErrorSpec(
        "agent-load", 400, "Agent definition error"
    ),
    "carve.core.skills.packs.SkillPackError": _ErrorSpec("skill-pack", 400, "Skill pack error"),
    "carve.core.state.job_queue.PipelineAlreadyRunning": _ErrorSpec(
        "pipeline-already-running", 409, "Pipeline is already running"
    ),
    "carve.core.state.job_queue.QueuedJobAlreadyExists": _ErrorSpec(
        "job-already-queued", 409, "A job is already queued for this pipeline"
    ),
    "carve.core.state.schedules.ScheduleNotFound": _ErrorSpec(
        "schedule-not-found", 404, "Schedule not found"
    ),
    "carve.init.plan.InitError": _ErrorSpec("init-failed", 400, "Initialization error"),
}


def _fqname(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _lookup_spec(exc: BaseException) -> _ErrorSpec | None:
    """Return the nearest registered spec for ``exc`` (walking its MRO)."""
    for cls in type(exc).__mro__:
        spec = _ERROR_MAP.get(_fqname(cls))
        if spec is not None:
            return spec
    return None


def _extra_fields(exc: BaseException) -> dict[str, Any]:
    """Pull structured custom fields off known exceptions (by attribute).

    Read with ``getattr`` so this stays import-free. The ``ConfigDriftError``
    round-trip in the spec (``plan_id`` + ``expected_config_hash`` +
    ``actual_config_hash`` + ``recovery_hint``) is produced here.
    """
    extra: dict[str, Any] = {}
    if _fqname(type(exc)) == "carve.cli.orchestrator.builder.ConfigDriftError":
        extra["plan_id"] = getattr(exc, "plan_id", None)
        extra["expected_config_hash"] = getattr(exc, "plan_hash", None)
        extra["actual_config_hash"] = getattr(exc, "current_hash", None)
        extra["recovery_hint"] = (
            "Run `carve plan --refine` to regenerate against current config."
        )
    # ConfigError carries a dotted ``field`` and a remediation ``hint``.
    field = getattr(exc, "field", None)
    if field is not None:
        extra.setdefault("field", field)
    hint = getattr(exc, "hint", None)
    if hint is not None:
        extra.setdefault("recovery_hint", hint)
    return extra


def _detail_of(exc: BaseException) -> str | None:
    """Prefer a structured ``.message`` (e.g. ``ConfigError``) over ``str(exc)``."""
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    text = str(exc)
    return text or None


class ProblemJsonExceptionHandler:
    """Convert any exception to problem+json; unrecognized → 500 (stack logged)."""

    async def __call__(self, request: Request, exc: Exception) -> Response:
        instance = request.url.path
        if isinstance(exc, ApiError):
            return problem(
                exc.status,
                exc.type_slug,
                exc.title,
                detail=exc.detail,
                instance=instance,
                **exc.extra,
            )
        spec = _lookup_spec(exc)
        if spec is None:
            # Never leak internals: log the stack, return an opaque 500.
            logger.exception("unhandled exception serving %s", instance)
            return problem(
                500,
                "internal",
                "Internal server error",
                detail="An unexpected error occurred.",
                instance=instance,
            )
        return problem(
            spec.status,
            spec.type_slug,
            spec.title,
            detail=_detail_of(exc),
            instance=instance,
            **_extra_fields(exc),
        )


async def _validation_handler(request: Request, exc: Exception) -> Response:
    """FastAPI request-validation errors → 422 problem+json."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return problem(
        422,
        "validation",
        "Request validation failed",
        detail="One or more request parameters were invalid.",
        instance=request.url.path,
        errors=errors,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """Starlette ``HTTPException`` (e.g. 404 on an unknown route) → problem+json."""
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    detail = exc.detail if isinstance(exc, StarletteHTTPException) else None
    slug = {
        401: "unauthorized",
        403: "forbidden",
        404: "not-found",
        405: "method-not-allowed",
    }.get(status, "http-error")
    return problem(
        status,
        slug,
        str(detail) if detail else "HTTP error",
        instance=request.url.path,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the problem+json handlers on ``app`` (validation, HTTP, catch-all)."""
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, ProblemJsonExceptionHandler())


__all__ = [
    "PROBLEM_JSON_MEDIA_TYPE",
    "ApiError",
    "BadRequest",
    "Conflict",
    "ProblemJsonExceptionHandler",
    "ResourceNotFound",
    "Unauthorized",
    "install_error_handlers",
    "problem",
]
