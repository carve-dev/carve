# M3-03 — Shell and HTTP step types

**Milestone:** 3 — Polish for adoption
**Estimated effort:** 0.5 day
**Dependencies:** M3-01 (multi-step pipelines)

## Purpose

Two step types that handle the long tail of integrations:

- `shell` — run an arbitrary shell command (use sparingly; here for escape-hatch flexibility)
- `http` — call an HTTP endpoint (webhooks, REST APIs, simple integrations)

## Shell step

```toml
[[steps]]
id = "rclone_sync"
type = "shell"
command = "rclone sync s3:bucket/data ./local-data"
timeout_seconds = 1800
env = { RCLONE_CONFIG = "/etc/rclone.conf" }
on_failure = "fail"
```

### Implementation

`src/carve/core/runners/shell.py`:

```python
class ShellRunner:
    def execute(self, step: ShellStep, context: RunContext) -> RunHandle:
        # Render Jinja in the command
        command = render_template(step.command, context)
        env = {**os.environ, **{k: render_template(v, context) for k, v in step.env.items()}}

        proc = subprocess.Popen(
            command,
            shell=True,  # users explicitly asking for shell
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=context.project_dir,
        )

        threading.Thread(
            target=self._stream_logs,
            args=(context.run_id, proc),
            daemon=True,
        ).start()

        return RunHandle(run_id=context.run_id, process_id=proc.pid)
```

### Safety considerations

`shell=True` is an obvious foot-gun. Carve's posture:

- Document clearly that shell steps run with the user's permissions and shell environment
- The command is part of the pipeline TOML (committed to git, reviewed in PRs)
- No secret interpolation other than via env vars
- Cap stdout capture at 10MB (avoid OOM from runaway commands)

The shell step is intentionally low-frills. If users need more (capture stdout, parse output, etc.), they should write a Python step instead.

## HTTP step

```toml
[[steps]]
id = "notify_slack"
type = "http"
method = "POST"
url = "{{ env.SLACK_WEBHOOK_URL }}"
headers = { "Content-Type" = "application/json" }
body_json = {
    text = "Pipeline {{ pipeline.name }} completed: {{ run.status }}"
}
timeout_seconds = 30
expected_status = [200]
on_failure = "warn"

# Or with a body file:
[[steps]]
id = "post_payload"
type = "http"
method = "POST"
url = "https://api.example.com/ingest"
headers = { Authorization = "Bearer {{ env.API_TOKEN }}" }
body_file = "pipelines/x/payload.json"
```

### Configuration

| Field | Required | Notes |
|---|---|---|
| `method` | yes | GET, POST, PUT, PATCH, DELETE |
| `url` | yes | full URL; templated |
| `headers` | no | dict of strings; templated |
| `body_json` | no | inline JSON body; templated |
| `body_file` | no | path to file containing body |
| `body_text` | no | inline text body |
| `query_params` | no | dict for query string |
| `timeout_seconds` | no | default 30 |
| `expected_status` | no | list of acceptable status codes; default `[200, 201, 202, 204]` |
| `retry_on_status` | no | status codes to retry; e.g., `[429, 500, 502, 503]` |

### Implementation

```python
class HttpRunner:
    def execute(self, step: HttpStep, context: RunContext) -> RunHandle:
        thread = threading.Thread(
            target=self._run,
            args=(step, context),
            daemon=True,
        )
        thread.start()
        return RunHandle(run_id=context.run_id, process_id=0)

    def _run(self, step: HttpStep, context: RunContext):
        run_id = context.run_id
        try:
            url = render_template(step.url, context)
            headers = {k: render_template(v, context) for k, v in step.headers.items()}
            body = self._build_body(step, context)

            response = httpx.request(
                method=step.method,
                url=url,
                headers=headers,
                content=body,
                timeout=step.timeout_seconds,
            )

            if response.status_code not in step.expected_status:
                raise StepError(f"HTTP {response.status_code}: {response.text[:500]}")

            self._set_outputs(run_id, step.id, {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": response.text[:10000],  # truncated
            })

            self.repo.update_step_status(run_id, step.id, "success")

        except Exception as e:
            self.repo.update_step_status(run_id, step.id, "failed", error=str(e))
```

### Output to downstream steps

The HTTP response is exposed as outputs:

```toml
[[steps]]
id = "fetch_token"
type = "http"
method = "POST"
url = "https://auth.example.com/oauth/token"
body_json = { grant_type = "client_credentials" }
expected_status = [200]

[[steps]]
id = "use_token"
type = "http"
method = "GET"
url = "https://api.example.com/data"
headers = {
    Authorization = "Bearer {{ steps.fetch_token.outputs.body | from_json | attr('access_token') }}"
}
depends_on = ["fetch_token"]
```

The `from_json` Jinja filter parses JSON; `attr` extracts an attribute. Both registered in the Jinja environment.

### Retries

If `retry_on_status` is set, the runner retries with exponential backoff:

- Up to N retries (default 3)
- Backoff: 1s, 2s, 4s, capped at 30s
- Configurable per step

### Auth helpers

Common auth patterns shouldn't require Jinja gymnastics:

```toml
[[steps]]
type = "http"
auth = { type = "bearer", token = "{{ env.API_TOKEN }}" }
# or
auth = { type = "basic", username = "user", password = "{{ env.PASSWORD }}" }
```

The runner translates these into headers automatically.

## Tests

### Shell

- Simple `echo` command produces expected output
- Non-zero exit code triggers failure
- Timeout cancels the process
- Env var injection works
- Jinja templating in command works

### HTTP

- Simple GET against a mock server returns expected outputs
- POST with JSON body works
- 4xx/5xx triggers failure unless in `expected_status`
- Retries work for transient failures
- Bearer auth helper produces correct header
- Timeout works

Use `httpx` mock helpers or `pytest-httpserver` for HTTP tests.

## Acceptance criteria

- Shell steps run arbitrary commands with logging and timeout
- HTTP steps make calls with full method/header/body/auth support
- Both step types integrate with the DAG executor's failure modes
- Output passing works (stdout for shell, response body for HTTP)

## Files

- `src/carve/core/steps/shell.py`
- `src/carve/core/steps/http.py`
- `src/carve/core/runners/shell.py`
- `src/carve/core/runners/http.py`
- `tests/core/runners/test_shell.py`
- `tests/core/runners/test_http.py`

## What this enables

- Slack/PagerDuty notifications via webhooks
- Calling external APIs in pipelines (Zendesk, HubSpot, GitHub)
- Escape hatch for "we just need to run this command"
- Composability without forcing every integration to be a Python script
