# M1-04 — Anthropic agent loop

**Milestone:** 1 — Walking skeleton
**Estimated effort:** 1 day
**Dependencies:** M1-02 (config), M1-03 (state store)

## Purpose

Wrap the Anthropic SDK with a tool-use loop that's reusable across all agents. For M1, define a single combined "code" agent with a small set of hardcoded tools (read file, write file, run Snowflake query). This is the heart of the demo — what turns natural language into executed code.

## Scope

### In scope

- Anthropic SDK client wrapper
- A generic tool-use turn-taking loop
- The combined M1 "code" agent with three hardcoded tools
- System prompt for the code agent
- Token usage tracking and cost computation
- Integration with the state store (logging tool calls and tokens)

### Out of scope

- The orchestration agent (M2)
- Specialist agents (dbt, Snowflake, quality) (M2)
- Skills as a discoverable concept (M3)
- Multiple model providers (Anthropic-only)
- Schema retrieval skills (M2)
- Per-agent guardrails (M2)

## Technical decisions

### SDK choice

Use the official `anthropic` Python SDK. Version 0.34+ for the latest tool-use API.

### Model choice

Default model: `claude-sonnet-4-5-20250929` (or the latest Sonnet at implementation time). Sonnet is the right default for the M1 code agent — Opus is overkill for this scope, Haiku undersized.

Make the model configurable via `carve/models.toml`:

```toml
[anthropic]
api_key = "${ANTHROPIC_API_KEY}"
default_model = "claude-sonnet-4-5"
```

### Async vs sync

Use **sync** for the agent loop. Reasoning:

- The user-facing CLI is sync; making the agent async adds complexity for no benefit
- The Anthropic SDK supports both; sync is simpler
- Tool execution (file IO, Snowflake queries) is naturally sync
- The future API server (M2) is async; it'll wrap the sync agent in `run_in_executor`

If async is needed later for parallelism, refactor at that point. Don't optimize prematurely.

## Architecture

### File: `src/carve/core/agents/loop.py`

The generic loop:

```python
class AgentLoop:
    def __init__(self, client, tools: list[Tool], system_prompt: str, model: str):
        self.client = client
        self.tools = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.model = model
        self.messages = []
        self.token_usage = TokenUsage()

    def run(self, user_message: str, max_turns: int = 30) -> str:
        self.messages.append({"role": "user", "content": user_message})

        for _ in range(max_turns):
            response = self.client.messages.create(
                model=self.model,
                system=self.system_prompt,
                max_tokens=4096,
                tools=[t.to_schema() for t in self.tools.values()],
                messages=self.messages,
            )
            self.token_usage.add(response.usage)
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                return self._extract_text(response)
            elif response.stop_reason == "tool_use":
                tool_results = self._execute_tool_calls(response)
                self.messages.append({"role": "user", "content": tool_results})
            else:
                raise AgentError(f"Unexpected stop_reason: {response.stop_reason}")

        raise AgentError("Max turns exceeded")
```

### File: `src/carve/core/agents/tools.py`

Tool definitions are simple — name, description, input schema, and an executor function:

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict  # JSON schema
    executor: Callable[[dict], dict]

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
```

### M1 tool set for the code agent

Three hardcoded tools, defined in `src/carve/core/agents/m1_tools.py`:

**`read_file`** — read a file from the project directory

```python
{
    "name": "read_file",
    "description": "Read the contents of a file in the project directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path from project root"}
        },
        "required": ["path"]
    }
}
```

Implementation guards against path traversal — only files under `project_dir` may be read.

**`write_file`** — write a file in the project directory

```python
{
    "name": "write_file",
    "description": "Write contents to a file in the project directory. Creates parent directories as needed. Overwrites if the file exists.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}
        },
        "required": ["path", "content"]
    }
}
```

Same path traversal guard. M1 has no diff-presentation; the file just gets written. M2 introduces the plan/diff view.

**`run_snowflake_query`** — run a SELECT against Snowflake to inspect data

```python
{
    "name": "run_snowflake_query",
    "description": "Execute a read-only SQL query against Snowflake. Used for exploring source data and schemas. Only SELECT, SHOW, and DESCRIBE statements are allowed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "limit": {"type": "integer", "default": 100}
        },
        "required": ["sql"]
    }
}
```

The executor validates that the SQL starts with SELECT, SHOW, or DESCRIBE (case-insensitive). Anything else raises an error returned to the agent.

### System prompt for the M1 code agent

Stored in `src/carve/core/agents/prompts/m1_code_agent.md`:

```markdown
You are Carve's code agent. Your job is to help users build data pipelines that
ingest source data into Snowflake.

When given a goal, you will:
1. Use `read_file` to understand the user's existing project structure if needed
2. Use `run_snowflake_query` to inspect existing schemas and tables
3. Generate a Python script that ingests the requested data
4. Use `write_file` to save the script

Conventions:
- Generated Python scripts go in `pipelines/<pipeline_name>/main.py`
- Each pipeline has its own directory under `pipelines/`
- Scripts use `snowflake-connector-python` for Snowflake access
- Scripts read connection details from environment variables, not hardcoded
- Scripts are idempotent — running them twice should not corrupt data

After writing the script, respond with a brief summary of what you built and
how the user should run it.
```

This prompt is intentionally short for M1. M2 will introduce per-agent prompts with conventions docs included.

### Token usage tracking

Anthropic returns usage in every response. Track cumulative usage across the loop:

```python
@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage):
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        # ... cache fields if present

    def cost_usd(self, model: str) -> float:
        # Lookup pricing table for the model and compute
        ...
```

Pricing table for known models lives in `src/carve/core/agents/pricing.py`. Update when prices change.

### Persisting agent activity

Every tool call should append a log line to the run via the repository:

```python
repo.append_log(
    run_id=run_id,
    level="info",
    source="agent",
    message=f"Calling tool: {tool_name} with input: {json.dumps(tool_input)}"
)
```

Token usage is persisted on the run row when the run completes.

## Error handling

A few specific failure modes:

**Anthropic rate limiting** — retry with exponential backoff, max 3 retries. Surface a clear error if all retries fail.

**Anthropic invalid request** — usually means a bad tool schema or oversized message. Don't retry; surface the error and exit.

**Tool execution failures** — return the error as the tool result so the agent can recover. Don't crash the loop.

**Path traversal attempts** — return a clear error to the agent: "Path X is outside the project directory and cannot be accessed."

**Forbidden SQL** — return a clear error: "Only SELECT, SHOW, and DESCRIBE statements are allowed via this tool."

## Tests

- A mocked Anthropic client returns a tool-use response → the loop executes the tool and continues
- A mocked client returns end-turn → the loop returns the text
- Tool executor errors are returned to the agent, not raised
- Path traversal is blocked
- Forbidden SQL is blocked
- Token usage accumulates correctly across turns
- Max-turns limit raises `AgentError`

Use the Anthropic SDK's mocking facilities or a `MagicMock`. Don't hit the real API in unit tests.

For integration tests (in CI, optional, gated on a secret API key), do hit the real API with simple goals to catch regressions.

## Acceptance criteria

- The M1 code agent loop runs end-to-end with a real Anthropic API key
- Tool calls are logged to the state store
- Token usage and cost are computed and stored on the run
- Path traversal and forbidden SQL are blocked at the tool-executor layer
- A failing tool returns its error to the agent rather than crashing the loop
- Tests pass with mocked Anthropic responses

## Files this spec produces

- `src/carve/core/agents/__init__.py`
- `src/carve/core/agents/loop.py`
- `src/carve/core/agents/tools.py`
- `src/carve/core/agents/m1_tools.py`
- `src/carve/core/agents/pricing.py`
- `src/carve/core/agents/prompts/m1_code_agent.md`
- `src/carve/core/agents/exceptions.py`
- `tests/core/agents/test_loop.py`
- `tests/core/agents/test_m1_tools.py`

## What this enables

- The full M1 demo flow: `carve plan "<goal>"` invokes the agent loop
- M2 specialists (dbt, Snowflake, etc.) reuse the same loop with different tools and prompts
- Token usage tracking is in place for cost reporting in plans (M2)
