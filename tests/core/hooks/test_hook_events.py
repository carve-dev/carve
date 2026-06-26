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
    """The seam: pre_deploy/on_run_failed register without firing.

    `post_build` LEFT this set in plan-build Unit 2 — its emitter (the build
    flow) now exists — so only `pre_deploy`/`on_run_failed` remain deferred.
    """
    registry = HookRegistry()
    fired: list[str] = []
    # The remaining deferred events are exactly pre_deploy + on_run_failed.
    assert DEFERRED_EMITTER_EVENTS == frozenset({HookEvent.PRE_DEPLOY, HookEvent.ON_RUN_FAILED})
    for event in DEFERRED_EMITTER_EVENTS:
        registry.subscribe(event, lambda _p: fired.append("x"))
        # Subscription is accepted (no error).
        assert registry.has_handlers(event)
        # Emitting one this slice is refused (the emitter is a later increment).
        with pytest.raises(DeferredEmitterEvent):
            registry.emit(event, {})
    assert fired == []  # nothing fired


def test_post_build_is_no_longer_deferred() -> None:
    """post_build left DEFERRED_EMITTER_EVENTS — its emitter exists now."""
    assert HookEvent.POST_BUILD not in DEFERRED_EMITTER_EVENTS
    assert HookEvent.POST_BUILD in EMITTED_EVENTS


def test_emitting_post_build_fires_handlers_without_raising() -> None:
    """emit(POST_BUILD, payload) now fires handlers, not DeferredEmitterEvent."""
    registry = HookRegistry()
    seen: list[dict[str, object]] = []
    registry.subscribe(HookEvent.POST_BUILD, lambda payload: seen.append(payload))
    payload = {"pipeline_name": "stripe", "build_id": "build_x", "target": "dev"}
    # Must NOT raise DeferredEmitterEvent — the emitter is wired (plan-build).
    registry.emit(HookEvent.POST_BUILD, payload)
    assert seen == [payload]


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
