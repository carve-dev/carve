"""Hook event-registry tests: subscription wires now; deferred emitters seam."""

from __future__ import annotations

import pytest

from carve.core.hooks.events import (
    DEFERRED_EMITTER_EVENTS,
    EMITTED_EVENTS,
    DeferredEmitterEvent,
    HookEvent,
    HookRegistry,
)


def test_emitted_events_fire_handlers() -> None:
    registry = HookRegistry()
    seen: list[dict[str, object]] = []
    registry.subscribe(HookEvent.PRE_TOOL, lambda payload: seen.append(payload))
    registry.emit(HookEvent.PRE_TOOL, {"tool": "bash"})
    assert seen == [{"tool": "bash"}]


def test_deferred_events_accept_subscriptions_but_do_not_fire() -> None:
    """The seam: pre_deploy/post_build/on_run_failed register without firing."""
    registry = HookRegistry()
    fired: list[str] = []
    for event in DEFERRED_EMITTER_EVENTS:
        registry.subscribe(event, lambda _p: fired.append("x"))
        # Subscription is accepted (no error).
        assert registry.has_handlers(event)
        # Emitting one this slice is refused (the emitter is a later increment).
        with pytest.raises(DeferredEmitterEvent):
            registry.emit(event, {})
    assert fired == []  # nothing fired


def test_emitted_and_deferred_partition_the_event_set() -> None:
    assert EMITTED_EVENTS | DEFERRED_EMITTER_EVENTS == set(HookEvent)
    assert not (EMITTED_EVENTS & DEFERRED_EMITTER_EVENTS)


def test_raising_handler_propagates_fail_closed() -> None:
    registry = HookRegistry()

    def _boom(_p: dict[str, object]) -> None:
        raise RuntimeError("blocked")

    registry.subscribe(HookEvent.PRE_TOOL, _boom)
    with pytest.raises(RuntimeError, match="blocked"):
        registry.emit(HookEvent.PRE_TOOL, {})
