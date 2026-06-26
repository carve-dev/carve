"""Live wiring for the review fan-out — fill the dormant ``review_fan_out`` seam.

``review_fan_out`` (``core/agents/review_fan_out.py``) shipped as a *dormant*
driver: it routes an authored diff through a reviewer sequence via an **injected**
``DelegateFn`` and aggregates the structured findings, but it builds no live
runner and owns no goal-routing — that is the orchestrator's job. This module is
that orchestrator glue (mirroring how ``delegation_run.py`` /
``extensibility_wiring.py`` own the live wiring the ``core`` modules left
injectable). It owns three things B2 needs to run a build's quality gate:

1. **The live ``delegate_fn``** — a partial over a :class:`SubagentRunner`'s
   ``run`` at ``parent_mode=PermissionMode.READ_ONLY``. Every reviewer agent is
   ``max_mode: read_only``, so the clamp ``min(READ_ONLY, capability)`` is
   ``READ_ONLY``: the reviewers read the diff adversarially and *never* author.
2. **The authored diff** the reviewers see — rendered from the engineers'
   harness-tracked authored file set (the union of each
   :class:`DelegationResult.files_changed`), unified-diff style against a
   pre-build snapshot, or the authored file contents when there was no prior
   version (greenfield). The reviewers receive only ``{diff, goal}`` — the driver
   enforces that isolation; nothing privileged (no engineer transcript) flows.
3. **The reviewer-sequence selection** — derived from *which engines authored*.
   Each authored sub-goal's ``classification`` resolves to its engineer via the
   live registry (the same source ``select_agent`` uses), and that engineer's
   family maps to its reviewer sequence (dlt ⇒ ``(dlt-qa, dlt-security)``; dbt ⇒
   ``(dbt-qa,)``). The sequences **union, de-duped, order-stable** — a dlt+dbt
   build runs ``dlt-qa, dlt-security, dbt-qa``. Resolution is off the registry,
   never a hardcoded classification→family table, so a new classification added
   to an engineer's frontmatter extends this with no code change here.

The aggregate :class:`ReviewResult` the driver returns is the build's quality
gate (consumed in ``builder.py``): a ``blocker``/``major`` finding blocks the
build; lower-severity findings surface as warnings.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any

from carve.cli.orchestrator.delegation_run import _build_runner
from carve.cli.orchestrator.extensibility_wiring import (
    HookFactory,
    build_extensibility_hook_factory,
)
from carve.core.agents.delegation import SubagentRunner
from carve.core.agents.permissions.gate import Approver
from carve.core.agents.permissions.modes import PermissionMode
from carve.core.agents.review_fan_out import (
    _REVIEWER_SEQUENCE,
    DBT_REVIEWER_SEQUENCE,
    DelegateFn,
    ReviewResult,
    review_fan_out,
)
from carve.core.agents.routing import NoAgentMatch, select_agent
from carve.core.agents.subagent_registry import SubagentRegistry
from carve.core.config import Config

logger = logging.getLogger(__name__)

# Each engineer *family* (resolved off the live registry) maps to the reviewer
# sequence that vets its diff. Keyed by the authoring engineer's agent NAME — the
# name the registry/`select_agent` resolves a classification to — so the map
# never drifts from the agents on disk: a classification is mapped to a family by
# asking the registry which agent owns it, not by a hardcoded classification
# table. dlt gets qa+security; dbt gets qa only (no separate dbt-security
# reviewer — SQL/credential concerns are dbt-qa's remit).
_REVIEWERS_BY_ENGINE: dict[str, tuple[str, ...]] = {
    "dlt-engineer": _REVIEWER_SEQUENCE,
    "dbt-engineer": DBT_REVIEWER_SEQUENCE,
}


def select_reviewer_sequence(
    classifications: Sequence[str],
    *,
    registry: SubagentRegistry,
) -> list[str]:
    """Select the reviewer sequence for the engines that authored ``classifications``.

    Each authored sub-goal's ``classification`` resolves (via :func:`select_agent`
    against the live ``registry``) to the engineer that owns it; that engineer's
    family maps to its reviewer sequence (:data:`_REVIEWERS_BY_ENGINE`). The
    per-engine sequences are **unioned, de-duped, and order-stable**: the first
    time a reviewer is seen fixes its position, so a dlt-then-dbt build yields
    ``[dlt-qa, dlt-security, dbt-qa]`` and a dbt-then-dlt build yields
    ``[dbt-qa, dlt-qa, dlt-security]`` — each reviewer runs exactly once.

    A classification whose engineer has no mapped reviewer family (e.g. the
    pipeline-engineer, which has no review fan-out today) contributes nothing —
    it is skipped, not an error. A classification matching no engine raises
    :class:`NoAgentMatch` (the same clear no-route the planner already surfaces);
    the caller has already authored against these classifications, so a miss here
    is a wiring bug worth surfacing, not a silent empty review.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for classification in classifications:
        agent_name = select_agent(registry, classification=classification)
        reviewers = _REVIEWERS_BY_ENGINE.get(agent_name)
        if reviewers is None:
            logger.debug(
                "Authoring engine %r (classification %r) has no reviewer family; "
                "no reviewers added for it.",
                agent_name,
                classification,
            )
            continue
        for reviewer in reviewers:
            if reviewer not in seen:
                seen.add(reviewer)
                ordered.append(reviewer)
    return ordered


def render_authored_diff(
    files_changed: Sequence[str],
    *,
    project_dir: Path,
    pre_build: dict[Path, str] | None = None,
) -> str:
    """Render the authored file set as a unified diff the reviewers read.

    ``files_changed`` is the union of the engineers' harness-tracked
    ``DelegationResult.files_changed`` (the authored slices) — relative posix
    paths. For each, a unified diff is produced from its ``pre_build`` content
    (the file's bytes *before* the build, captured in a snapshot) to its current
    on-disk content. A file absent from ``pre_build`` is a **new** file
    (greenfield), rendered as a diff from empty. The per-file diffs concatenate
    in sorted path order so the rendered diff is deterministic.

    Reading the diff from ``files_changed`` (not a single ``el/<name>/`` dir
    snapshot) is the B2 design decision: dlt writes ``el/**`` and dbt writes
    ``models/**`` — one dir snapshot cannot see both, but the per-engine
    ``files_changed`` union does.
    """
    pre_build = pre_build or {}
    chunks: list[str] = []
    for rel in sorted(set(files_changed)):
        abs_path = (project_dir / rel).resolve()
        new_text = ""
        if abs_path.is_file():
            try:
                new_text = abs_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # A binary authored file (e.g. a dbt seed `.csv`, a fixture):
                # not text-diffable. Emit a git-style stub so the reviewer sees
                # the path changed without crashing the gate on a decode error.
                chunks.append(f"Binary files a/{rel} and b/{rel} differ\n")
                continue
        old_text = pre_build.get(abs_path, "")
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        rendered = "".join(diff)
        if rendered:
            chunks.append(rendered)
    return "".join(chunks)


def _build_reviewer_delegate_fn(
    *,
    registry: SubagentRegistry,
    config: Config,
    project_dir: Path,
    client: Any,
    model: str,
    approver: Approver | None,
    hook_factory: HookFactory | None,
    max_turns: int,
    runner: SubagentRunner | None,
) -> DelegateFn:
    """Build the live ``delegate_fn`` the driver injects — a partial over ``run``.

    The reviewers are read-only adversarial, so the runner delegates at
    ``parent_mode=PermissionMode.READ_ONLY`` (each clamps to
    ``min(READ_ONLY, capability) = READ_ONLY``). The runner is built ONCE (reusing
    ``delegation_run._build_runner`` — the same centralized construction the
    plan/build engine runs use, so the ``sql``/``dbt_manifest`` extra tools and
    the hook factory wire identically) and threaded across every reviewer. A
    pre-built ``runner`` (the test seam) is used as-is.

    The returned callable matches the driver's ``DelegateFn`` shape
    ``(agent, task, context) -> DelegationResult``: ``parent_mode`` is bound to
    ``READ_ONLY`` here so the driver never has to know the review mode.
    """
    if runner is None:
        runner = _build_runner(
            registry=registry,
            config=config,
            project_dir=project_dir,
            client=client,
            model=model,
            approver=approver,
            parent_mode=PermissionMode.READ_ONLY,
            max_turns=max_turns,
            hook_factory=hook_factory,
        )
    return partial(runner.run, parent_mode=PermissionMode.READ_ONLY)


def run_review_fan_out(
    *,
    classifications: Sequence[str],
    files_changed: Sequence[str],
    goal: str,
    config: Config,
    project_dir: Path,
    client: Any,
    model: str,
    registry: SubagentRegistry,
    pre_build: dict[Path, str] | None = None,
    approver: Approver | None = None,
    hook_factory: HookFactory | None = None,
    runner: SubagentRunner | None = None,
    max_turns: int = 30,
) -> ReviewResult:
    """Run the live review fan-out over an authored diff and return the verdict.

    The orchestrator-owned entry point ``build_plan`` calls after authoring:

    1. **Select** the reviewer sequence from the authoring engines'
       ``classifications`` (:func:`select_reviewer_sequence` — registry-driven,
       unioned + de-duped, order-stable).
    2. **Render** the authored diff from ``files_changed``
       (:func:`render_authored_diff`).
    3. **Delegate** each reviewer through a live ``delegate_fn`` at ``READ_ONLY``
       (:func:`_build_reviewer_delegate_fn`).
    4. **Aggregate** via :func:`review_fan_out`, whose ``ReviewResult.passed`` is
       the build's quality gate.

    The reviewers see only ``{diff, goal}`` (the driver enforces that isolation).
    ``runner`` is a test seam — a fake/stub :class:`SubagentRunner` short-circuits
    the live child loops; in production it is built once here and threaded across
    the sequence.

    Raises:
        NoAgentMatch: an authored classification matched no engine (a wiring
            bug — the caller already authored against it; surfaced, not masked).
    """
    reviewers = select_reviewer_sequence(classifications, registry=registry)
    diff = render_authored_diff(files_changed, project_dir=project_dir, pre_build=pre_build)

    if not reviewers and diff.strip():
        # The authoring engines have no reviewer family (e.g. a pipeline-only
        # build), so a non-empty authored diff would pass the gate UNREVIEWED.
        # That is spec-sanctioned per-engine (no fan-out for those engines yet),
        # but an entirely empty sequence over real changes is worth surfacing.
        logger.warning(
            "Review gate ran no reviewers for an authored diff (%d file(s), "
            "classifications=%r): the build ships ungated.",
            len(set(files_changed)),
            list(classifications),
        )

    # Build the hook factory only when we'll construct the runner ourselves (a
    # pre-built runner — the test seam — already carries its own hooks). Parse
    # carve/hooks.toml eagerly so a malformed file is fail-closed before any
    # reviewer delegates — the same boundary the plan/build flows promise.
    if runner is None and hook_factory is None:
        hook_factory = build_extensibility_hook_factory(
            project_dir=project_dir,
            paths=config.paths,
            approver=approver,
        )

    delegate_fn = _build_reviewer_delegate_fn(
        registry=registry,
        config=config,
        project_dir=project_dir,
        client=client,
        model=model,
        approver=approver,
        hook_factory=hook_factory,
        max_turns=max_turns,
        runner=runner,
    )
    return review_fan_out(diff, goal, delegate_fn, reviewers=reviewers)


__all__ = [
    "NoAgentMatch",
    "render_authored_diff",
    "run_review_fan_out",
    "select_reviewer_sequence",
]
