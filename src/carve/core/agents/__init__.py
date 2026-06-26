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

from carve.core.agents.discovery import (
    BUILTIN_AGENTS_DIR,
    AgentDiscovery,
    AgentRoot,
)
from carve.core.agents.exceptions import (
    AgentError,
    InvalidRequestError,
    MaxTurnsExceeded,
    RateLimitExhausted,
    UnexpectedStopReason,
)
from carve.core.agents.loader import (
    MAX_AGENT_FILE_BYTES,
    AgentFile,
    AgentLoadError,
    load_agent_file,
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
from carve.core.agents.routing import NoAgentMatch, select_agent
from carve.core.agents.subagent_registry import (
    AgentSpec,
    SubagentRegistry,
    spec_from_agent_file,
)
from carve.core.agents.tools import Tool, ToolExecutionError, ToolExecutor

# NOTE: the old hardcoded `AGENT_REGISTRY: dict[str, str]` (M1-era,
# zero external consumers) has been replaced by the declarative agent
# surface: `AgentDiscovery.build_registry()` produces a `SubagentRegistry`
# from the discovery roots, and `select_agent` (the classification router)
# turns a goal classification / explicit name into the agent name
# `delegate(agent, …)` consumes.

__all__ = [
    "BUILTIN_AGENTS_DIR",
    "MAX_AGENT_FILE_BYTES",
    "AgentDiscovery",
    "AgentError",
    "AgentFile",
    "AgentLoadError",
    "AgentLoop",
    "AgentObserver",
    "AgentResult",
    "AgentRoot",
    "AgentSpec",
    "InvalidRequestError",
    "MaxTurnsExceeded",
    "ModelPricing",
    "NoAgentMatch",
    "NullObserver",
    "RateLimitExhausted",
    "SnowflakeQueryRunner",
    "SubagentRegistry",
    "SubmitPlanCapture",
    "TokenUsage",
    "Tool",
    "ToolExecutionError",
    "ToolExecutor",
    "UnexpectedStopReason",
    "build_m1_tools",
    "compute_cost_usd",
    "load_agent_file",
    "load_m1_build_agent_prompt",
    "load_m1_plan_agent_prompt",
    "lookup_pricing",
    "make_read_file_tool",
    "make_run_snowflake_query_tool",
    "make_submit_plan_tool",
    "make_write_file_tool",
    "select_agent",
    "spec_from_agent_file",
]
