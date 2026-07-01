"""CLI↔REST parity — every CLI command has a REST counterpart or a reviewed exemption.

The *mechanism* the rest-api spec requires (PRD §6 intro): enumerate the real
Typer command surface from ``carve.cli.main.app`` so a newly-added CLI command
automatically trips this test, and assert each command maps to a REST router tag
**or** appears in an explicit, commented exemption allow-list. Runs offline (the
app is built over a ``MagicMock`` state store — no Postgres needed).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from carve.api.main import create_app
from carve.cli.main import app as cli_app
from carve.core.config.schema import Config, ModelsConfig, ProjectConfig

# --- The reviewed mapping: CLI command → REST router tag ---------------------
#
# Keys are the exact invocation names ("group sub" for subcommands). Values are
# the router tag that provides the REST counterpart.
COMMAND_TO_TAG: dict[str, str] = {
    "plan": "plans",
    "build": "builds",
    "plan-and-build": "builds",
    "deploy": "deploys",
    "runs": "runs",
    "logs": "runs",  # logs are a run sub-resource: GET /runs/{id}/logs
    "component": "components",
    "auth rotate": "tokens",  # mints an API bearer token → /api/v1/tokens
    # target group → /api/v1/targets
    "target create": "targets",
    "target list": "targets",
    "target show": "targets",
    "target rename": "targets",
    "target delete": "targets",
    # el group → run history (/runs) + the deploy surface (/deploys)
    "el run": "runs",
    "el list": "deploys",
    "el deploy": "deploys",
    "el verify": "deploys",
    # pipelines group → /api/v1/pipelines
    "pipelines validate": "pipelines",
    "pipelines list": "pipelines",
    "pipelines show": "pipelines",
    "pipelines diff": "pipelines",
    # components group → /api/v1/components
    "components show": "components",
    # schedule group → /api/v1/schedules
    "schedule list": "schedules",
    "schedule show": "schedules",
    "schedule pause": "schedules",
    "schedule resume": "schedules",
    "schedule set-cron": "schedules",
    "schedule reseed": "schedules",
    # metrics group → /api/v1/metrics
    "metrics costs": "metrics",
    "metrics runs": "metrics",
    "metrics agents": "metrics",
    # memory group → /api/v1/memory
    "memory show": "memory",
    "memory edit": "memory",
    "memory append-decision": "memory",
    "memory refresh": "memory",
    # agents group → /api/v1/agents
    "agents list": "agents",
    "agents show": "agents",
    "agents create": "agents",
    # skills group → /api/v1/skills
    "skills list": "skills",
    "skills show": "skills",
    "skills test": "skills",
    # mcp-servers group → /api/v1/mcp-servers
    "mcp-servers add": "mcp-servers",
    "mcp-servers list": "mcp-servers",
    "mcp-servers remove": "mcp-servers",
}

# --- The reviewed exemption allow-list (plumbing-only / interactive) ----------
#
# Each entry is a command with NO control-plane REST counterpart, with a
# one-line justification. Silent gaps are NOT allowed — an unmapped, unexempted
# command fails the test.
EXEMPTIONS: dict[str, str] = {
    "init": "local project scaffolding; runs before the server/DB exist",
    "serve": "the process that launches this API server itself",
    "worker": "process launcher; live worker state is exposed via /api/v1/workers",
    "version": "build metadata; exposed via the OpenAPI info.version field",
    "connect": "local execution-engine install + connection validation (interactive setup)",
    "auth status": "local model-provider credential inspection (env/keychain, not server state)",
    "auth login": "interactive browser OAuth via `claude setup-token`",
}


def _enumerate_commands(typer_app: object, prefix: str = "") -> list[str]:
    """Walk a Typer app into ``["cmd", "group sub", ...]`` invocation names."""
    names: list[str] = []
    for cmd in typer_app.registered_commands:  # type: ignore[attr-defined]
        name = cmd.name or cmd.callback.__name__.replace("_", "-")
        names.append(f"{prefix}{name}".strip())
    for group in typer_app.registered_groups:  # type: ignore[attr-defined]
        names.extend(_enumerate_commands(group.typer_instance, prefix=f"{group.name} "))
    return names


def _app_tags() -> set[str]:
    """Every tag actually served, read from the generated OpenAPI schema."""
    config = Config(
        project=ProjectConfig(name="parity"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
    )
    schema = create_app(MagicMock(), config).openapi()
    tags: set[str] = set()
    for operations in schema["paths"].values():
        for operation in operations.values():
            if isinstance(operation, dict):
                tags.update(operation.get("tags", []))
    return tags


def test_every_cli_command_is_mapped_or_exempt() -> None:
    """A new CLI command with no mapping and no exemption fails CI here."""
    commands = _enumerate_commands(cli_app)
    unaccounted = [
        c for c in commands if c not in COMMAND_TO_TAG and c not in EXEMPTIONS
    ]
    assert not unaccounted, (
        "CLI commands lacking a REST counterpart or an explicit exemption: "
        f"{sorted(unaccounted)}. Add a router mapping to COMMAND_TO_TAG or a "
        "justified entry to EXEMPTIONS."
    )


def test_mapped_tags_are_served_by_the_app() -> None:
    """Every router tag referenced by the mapping is actually mounted."""
    served = _app_tags()
    missing = {tag for tag in COMMAND_TO_TAG.values() if tag not in served}
    assert not missing, f"mapped router tags with no routes in the app: {sorted(missing)}"


def test_exemptions_do_not_overlap_mappings() -> None:
    """An exemption and a mapping for the same command would be a review error."""
    overlap = set(COMMAND_TO_TAG) & set(EXEMPTIONS)
    assert not overlap, f"commands both mapped and exempted: {sorted(overlap)}"
