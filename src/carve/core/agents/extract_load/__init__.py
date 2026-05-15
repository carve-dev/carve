"""Pillar 1 extract-load specialist agent (P1-04).

Public surface: `run_extract_load_agent` (the entrypoint the build
flow calls) and `ExtractLoadResult` (its return shape).

The agent consumes a `Task` from the plan task graph (one task per
build in Pillar 1), generates `main.py`, `requirements.txt`, and the
companion DDL file under `el/<artifact>/`, and terminates by calling
`submit_step(file_list, summary)`. The full system prompt lives in
`prompts/extract_load_agent.md`.
"""

from carve.core.agents.extract_load.agent import (
    ExtractLoadAgentError,
    ExtractLoadResult,
    load_extract_load_agent_prompt,
    run_extract_load_agent,
)

__all__ = [
    "ExtractLoadAgentError",
    "ExtractLoadResult",
    "load_extract_load_agent_prompt",
    "run_extract_load_agent",
]
