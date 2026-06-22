"""Write a Carve project's files from a resolved :class:`InitPlan`.

Pure and idempotent: every write skips an existing file (so re-running
`carve init` never clobbers user edits), and the function returns a
:class:`ScaffoldResult` rather than printing — the CLI command renders it.
Connection-config (the dev target), the state-store migration, and git init
stay in the command layer (they print / connect / may exit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from carve.cli.commands.packaging import (
    bundled_env_block,
    external_env_block,
    render_compose,
)
from carve.init import templates
from carve.init.plan import InitPlan


@dataclass
class ScaffoldResult:
    """What :func:`scaffold` wrote vs. kept (existing files left untouched)."""

    written: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)
    dirs_created: list[Path] = field(default_factory=list)

    def _write(self, path: Path, content: str) -> None:
        # `is_symlink()` first: a *dangling* symlink reports exists()==False, so
        # writing would follow the link and clobber a file outside the project.
        # Treat any pre-existing symlink as "keep, never write through".
        if path.is_symlink() or path.exists():
            self.kept.append(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.written.append(path)

    def _ensure_dir(self, path: Path) -> None:
        if path.is_symlink() or path.exists():
            return
        path.mkdir(parents=True, exist_ok=True)
        self.dirs_created.append(path)


def scaffold(root: Path, plan: InitPlan) -> ScaffoldResult:
    """Write the project layout for ``plan`` under ``root`` (idempotently)."""
    r = ScaffoldResult()

    r._write(root / "carve.toml", templates.render_carve_toml(plan))
    r._write(root / "carve" / "runner.toml", templates.RUNNER_TOML_CONTENT)
    r._write(root / "carve" / "models.toml", templates.MODELS_TOML_CONTENT)
    r._write(
        root / "carve" / "connections.toml",
        templates.render_connections_toml(plan.default_target),
    )
    r._ensure_dir(root / "carve" / "agents")
    r._write(root / "carve" / "standards.md", templates.STANDARDS_MD_CONTENT)
    r._write(root / "carve" / "decisions.md", templates.DECISIONS_MD_CONTENT)
    r._write(root / "carve" / "conventions.md", templates.CONVENTIONS_MD_CONTENT)
    r._ensure_dir(root / "el")

    state_block = (
        external_env_block() if plan.external_postgres_url is not None else bundled_env_block()
    )
    r._write(root / ".env.example", templates.ENV_EXAMPLE_HEADER + state_block)
    r._write(root / ".gitignore", templates.GITIGNORE_CONTENT)

    # Bundled Postgres → drop the compose template; external → none (the URL is
    # printed for the user's gitignored .env by the command, never committed).
    if plan.external_postgres_url is None:
        r._write(root / "docker-compose.yml", render_compose(plan.project_name))

    # Greenfield scaffolds (same-repo; convention-discovered, no carve.toml block).
    if plan.scaffold_dbt:
        r._write(root / "dbt_project.yml", templates.render_dbt_project_yml(plan.project_name))
        r._ensure_dir(root / "models")
    if plan.scaffold_dlt:
        r._write(root / "el" / "sample" / "__init__.py", templates.DLT_SAMPLE_INIT_CONTENT)

    return r
