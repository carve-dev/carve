"""The pipeline step DAG: cycle check, topological order, readiness.

``PipelineDAG`` is pure data over a validated :class:`Pipeline` — no I/O,
no execution. It answers the two questions the DAG walk asks: *which steps
are ready to run now?* (:meth:`PipelineDAG.ready_steps`) and *what is
transitively downstream of a step?* (:meth:`PipelineDAG.downstream_of`,
used by ``skip_downstream`` and by ``carve run --resume``'s failed-subgraph
computation).

``load_pipeline`` already rejects cycles, but ``PipelineDAG`` re-validates
at construction so a caller that builds a DAG over a hand-constructed
``Pipeline`` (e.g. a test) still gets the guarantee.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from carve.core.config.pipeline_schema import PipelineError

if TYPE_CHECKING:
    from carve.core.config.pipeline_schema import Pipeline, PipelineStep


class PipelineDAG:
    """The dependency DAG over a pipeline's steps.

    Construction validates the graph is acyclic and computes a stable
    topological order. Readiness is evaluated against the live
    completed/failed/skipped sets the executor maintains.
    """

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self._steps_by_id: dict[str, PipelineStep] = {step.id: step for step in pipeline.steps}
        self._deps: dict[str, set[str]] = {step.id: set(step.depends_on) for step in pipeline.steps}
        self._validate_no_cycles()
        self._topological_order: list[str] = self._compute_topological_order()

    # -- construction-time validation ------------------------------------

    def _validate_no_cycles(self) -> None:
        """DFS three-colour cycle detection; raises on the first cycle."""
        # 0 = unvisited, 1 = on stack, 2 = done.
        state: dict[str, int] = dict.fromkeys(self._deps, 0)

        def visit(node: str, stack: list[str]) -> None:
            state[node] = 1
            stack.append(node)
            for dep in self._deps[node]:
                if dep not in state:
                    # A dangling ref; load_pipeline catches this earlier, but
                    # a hand-built Pipeline may not have run through it.
                    raise PipelineError(
                        f"Step {node!r} depends on unknown step {dep!r}",
                        hint="Every `depends_on` entry must name an existing step id.",
                    )
                if state[dep] == 1:
                    cycle = [*stack[stack.index(dep) :], dep]
                    raise PipelineError(
                        f"Dependency cycle detected: {' -> '.join(cycle)}",
                        hint="Steps must form a DAG; remove the circular `depends_on`.",
                    )
                if state[dep] == 0:
                    visit(dep, stack)
            stack.pop()
            state[node] = 2

        for step_id in self._deps:
            if state[step_id] == 0:
                visit(step_id, [])

    def _compute_topological_order(self) -> list[str]:
        """Kahn's algorithm over the dependency edges.

        Ties are broken by the steps' declaration order, so the order is
        stable and reproducible for a given pipeline.
        """
        declaration_index = {step.id: i for i, step in enumerate(self.pipeline.steps)}
        indegree: dict[str, int] = {sid: len(deps) for sid, deps in self._deps.items()}
        # dependents[x] = steps that depend on x (the reverse edges).
        dependents: dict[str, list[str]] = {sid: [] for sid in self._deps}
        for sid, deps in self._deps.items():
            for dep in deps:
                dependents[dep].append(sid)

        ready = sorted(
            (sid for sid, deg in indegree.items() if deg == 0),
            key=lambda s: declaration_index[s],
        )
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            newly_ready: list[str] = []
            for dependent in dependents[node]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    newly_ready.append(dependent)
            for sid in sorted(newly_ready, key=lambda s: declaration_index[s]):
                # Insert keeping declaration-order stability.
                _insert_sorted(ready, sid, declaration_index)
        return order

    # -- public API ------------------------------------------------------

    @property
    def topological_order(self) -> list[str]:
        """Step ids in a stable topological order (deps before dependents)."""
        return list(self._topological_order)

    def step(self, step_id: str) -> PipelineStep:
        """Return the step with ``step_id`` (raises ``KeyError`` if absent)."""
        return self._steps_by_id[step_id]

    def ready_steps(
        self,
        completed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> list[PipelineStep]:
        """Steps eligible to launch given the current run state.

        A step is ready when every dependency has *resolved* — completed,
        failed (a ``warn``/``continue`` fall-through), or skipped — AND the
        step is not itself already completed/failed/skipped. Steps are
        returned in topological order so the caller's slot-capped slice is
        deterministic.
        """
        resolved = completed | failed | skipped
        ready: list[PipelineStep] = []
        for step_id in self._topological_order:
            if step_id in resolved:
                continue
            if self._deps[step_id] <= resolved:
                ready.append(self._steps_by_id[step_id])
        return ready

    def downstream_of(self, step_id: str) -> set[str]:
        """All step ids transitively dependent on ``step_id`` (excludes it)."""
        dependents: dict[str, list[str]] = {sid: [] for sid in self._deps}
        for sid, deps in self._deps.items():
            for dep in deps:
                dependents[dep].append(sid)

        result: set[str] = set()
        frontier = list(dependents.get(step_id, []))
        while frontier:
            node = frontier.pop()
            if node in result:
                continue
            result.add(node)
            frontier.extend(dependents.get(node, []))
        return result


def _insert_sorted(
    queue: list[str],
    item: str,
    index: dict[str, int],
) -> None:
    """Insert ``item`` into ``queue`` keeping declaration-index order."""
    target = index[item]
    for i, existing in enumerate(queue):
        if index[existing] > target:
            queue.insert(i, item)
            return
    queue.append(item)


__all__ = ["PipelineDAG"]
