"""The recording observer — persist each subagent invocation + skill call.

:class:`RecordingObserver` implements the :class:`~carve.core.agents.observer.AgentObserver`
protocol *and* an explicit ``begin_invocation`` / ``end_invocation`` lifecycle the
delegation call-site drives. It is the "instrumentation hook" the observability
capability wires onto a delegated subagent run: one ``agent_invocations`` row per
invocation (tokens / cost / duration / status) plus one ``skill_calls`` row per
tool call, correlated to the run/plan/build/ask that triggered it.

Two load-bearing properties (observability delivery spec §5):

**Best-effort, never a blocker.** Recording is telemetry, not the work. Every
write goes through a guarded call that *logs* a failure and moves on — a down DB
or a serialization hiccup must never propagate into (or fail) the delegated run.
The call-site only ever constructs a ``RecordingObserver`` when a session-factory
is present; otherwise the runner keeps its ``NullObserver`` default and behaviour
is byte-identical to before this wiring landed.

**Correlation rides the sync/sequential delegation invariant.** ``delegation.py``
guarantees one child loop at a time, so a single "current open invocation" cursor
— set by :meth:`begin_invocation`, cleared by :meth:`end_invocation` — correctly
attributes each :meth:`on_tool_result` ``skill_calls`` row to the right
invocation with no concurrency risk. A stray ``on_tool_result`` with no open
invocation is dropped-and-logged, never a crash.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from carve.core.agents.delegation import DelegationResult
    from carve.core.state.telemetry import TelemetryRepo


class RecordingObserver:
    """Persist a subagent run's invocation + skill calls, best-effort.

    Construct one per delegation session (the call-site threads it across the
    sequential sub-goals). The correlation ids (``run_id``/``plan_id``/
    ``build_id``/``ask_id``) are fixed for the session; the ``agent_name`` varies
    per invocation and is supplied to :meth:`begin_invocation`.
    """

    def __init__(
        self,
        telemetry: TelemetryRepo,
        *,
        run_id: str | None = None,
        plan_id: str | None = None,
        build_id: str | None = None,
        ask_id: str | None = None,
        model: str | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._run_id = run_id
        self._plan_id = plan_id
        self._build_id = build_id
        self._ask_id = ask_id
        self._model = model
        # The "current open invocation" cursor — set at begin_invocation,
        # cleared at end_invocation. Correct as a single scalar because
        # delegation is sync/sequential (one child loop at a time).
        self._open_invocation_id: str | None = None

    # ------------------------------------------------ Explicit call-site lifecycle

    def begin_invocation(self, *, agent_name: str) -> str | None:
        """Open an ``agent_invocations`` row; return + set the current-invocation id.

        Called by the delegation call-site immediately before ``delegate()``. The
        minted id is **returned** so the call-site can pass it back to
        :meth:`end_invocation` — the finalize no longer depends on the mutable
        cursor (which would silently corrupt the day nested delegation lands). The
        cursor is still set so ``on_tool_result`` can attribute skill calls to the
        current invocation.

        If the cursor is already non-``None`` (a prior invocation never closed —
        structurally impossible today, latent once nested ``delegate`` ships), a
        **fail-loud warning** is logged before the overwrite so the orphaned
        ``running`` row is a visible signal, not a silent corruption. Guarded: a
        failure logs and leaves the cursor ``None`` (so no skill call is
        mis-attributed) and returns ``None``, never raising into the delegated run.
        """
        if self._open_invocation_id is not None:
            logger.warning(
                "telemetry: begin_invocation with an already-open invocation %s; "
                "prior row may orphan",
                self._open_invocation_id,
            )
        try:
            self._open_invocation_id = self._telemetry.open_invocation(
                agent_name=agent_name,
                run_id=self._run_id,
                plan_id=self._plan_id,
                build_id=self._build_id,
                ask_id=self._ask_id,
                model=self._model,
            )
        except Exception:
            logger.warning(
                "telemetry: open_invocation failed for agent=%s; skipping recording",
                agent_name,
                exc_info=True,
            )
            self._open_invocation_id = None
        return self._open_invocation_id

    def end_invocation(
        self, invocation_id: str | None, result: DelegationResult | None, duration_ms: int
    ) -> None:
        """Finalize ``invocation_id`` from the ``DelegationResult`` + call-site duration.

        The id to finalize is **passed in** (the value :meth:`begin_invocation`
        returned) rather than read from the mutable cursor, so the lifecycle is
        symmetric and nested-delegation-safe. ``duration_ms`` is captured at the
        call-site (``DelegationResult`` carries no duration field). A ``None``
        result (the ``delegate()`` call raised) finalizes the row as ``failed``
        with zero tokens. Clears the cursor (if it still points at this id) so a
        subsequent invocation starts clean. Guarded — never raises.
        """
        if self._open_invocation_id == invocation_id:
            self._open_invocation_id = None
        if invocation_id is None:
            return
        if result is not None:
            tokens_input = result.usage.input_tokens
            tokens_output = result.usage.output_tokens
            cost_usd = result.cost_usd
            status = result.status
        else:
            tokens_input = 0
            tokens_output = 0
            cost_usd = 0.0
            status = "failed"
        try:
            self._telemetry.finalize_invocation(
                invocation_id,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                status=status,
            )
        except Exception:
            logger.warning(
                "telemetry: finalize_invocation failed for id=%s; skipping",
                invocation_id,
                exc_info=True,
            )

    # ------------------------------------------------------ AgentObserver protocol

    def on_turn_start(self, turn: int) -> None:
        return None

    def on_tool_call(self, name: str, input: dict[str, Any]) -> None:
        return None

    def on_tool_result(self, name: str, ok: bool, summary: str, duration_ms: int) -> None:
        """Record one ``skill_calls`` row against the currently-open invocation.

        The only telemetry the tool-use loop hands us is ``(name, ok, summary,
        duration_ms)`` — so ``output_size`` is the summary length (a proxy) and
        the §6.4 bounded-result signals (``result_too_large``/``pages_walked``)
        default off (they are not derivable from this callback). A stray call
        with no open invocation is dropped-and-logged, never a crash.
        """
        invocation_id = self._open_invocation_id
        if invocation_id is None:
            logger.warning(
                "telemetry: on_tool_result for %s with no open invocation; dropping",
                name,
            )
            return
        try:
            self._telemetry.record_skill_call(
                agent_invocation_id=invocation_id,
                skill_name=name,
                output_size=len(summary),
                duration_ms=duration_ms,
            )
        except Exception:
            logger.warning(
                "telemetry: record_skill_call failed for %s; skipping",
                name,
                exc_info=True,
            )

    def on_turn_complete(self, turn: int, input_tokens: int, output_tokens: int) -> None:
        return None

    def on_done(
        self,
        total_turns: int,
        total_tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        return None


__all__ = ["RecordingObserver"]
