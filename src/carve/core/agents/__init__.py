"""Agent runtime: the Anthropic tool-use loop and the M1 code agent's tools.

Public surface:

- `AgentLoop` — the generic Anthropic tool-use loop.
- `AgentResult`, `TokenUsage` — dataclasses returned by `AgentLoop.run`.
- `Tool`, `ToolExecutionError` — primitives for defining tools.
- `build_m1_tools`, `SnowflakeQueryRunner` — the M1 code-agent toolkit.
- `load_m1_code_agent_prompt` — loads the system prompt from disk.
- `compute_cost_usd`, `lookup_pricing`, `ModelPricing` — pricing helpers.
- `AgentError` and friends — exceptions raised by the loop.
"""

from carve.core.agents.exceptions import (
    AgentError,
    InvalidRequestError,
    MaxTurnsExceeded,
    RateLimitExhausted,
    UnexpectedStopReason,
)
from carve.core.agents.extract_load import (
    ExtractLoadAgentError,
    ExtractLoadResult,
    load_extract_load_agent_prompt,
    run_extract_load_agent,
)
from carve.core.agents.loop import (
    AgentLoop,
    AgentResult,
    TokenUsage,
    load_m1_build_agent_prompt,
    load_m1_plan_agent_prompt,
)
from carve.core.agents.m1_tools import (
    SnowflakeQueryRunner,
    SubmitPlanCapture,
    build_m1_tools,
    make_read_file_tool,
    make_run_snowflake_query_tool,
    make_submit_plan_tool,
    make_write_file_tool,
)
from carve.core.agents.observer import AgentObserver, NullObserver
from carve.core.agents.pricing import ModelPricing, compute_cost_usd, lookup_pricing
from carve.core.agents.tools import Tool, ToolExecutionError, ToolExecutor

AGENT_REGISTRY: dict[str, str] = {
    # Maps task.agent values to a human-meaningful description. The build
    # flow's dispatch (Pillar 2 onward) consults this map to validate
    # task.agent before invoking the specialist. In Pillar 1 there's only
    # one entry — extract_load — and the build flow assumes it.
    "extract_load": "Pillar 1 extract-load specialist (P1-04).",
}

__all__ = [
    "AGENT_REGISTRY",
    "AgentError",
    "AgentLoop",
    "AgentObserver",
    "AgentResult",
    "ExtractLoadAgentError",
    "ExtractLoadResult",
    "InvalidRequestError",
    "MaxTurnsExceeded",
    "ModelPricing",
    "NullObserver",
    "RateLimitExhausted",
    "SnowflakeQueryRunner",
    "SubmitPlanCapture",
    "TokenUsage",
    "Tool",
    "ToolExecutionError",
    "ToolExecutor",
    "UnexpectedStopReason",
    "build_m1_tools",
    "compute_cost_usd",
    "load_extract_load_agent_prompt",
    "load_m1_build_agent_prompt",
    "load_m1_plan_agent_prompt",
    "lookup_pricing",
    "make_read_file_tool",
    "make_run_snowflake_query_tool",
    "make_submit_plan_tool",
    "make_write_file_tool",
    "run_extract_load_agent",
]
