# M2-11 — WebSocket streaming

**Milestone:** 2 — Real product
**Estimated effort:** 0.5 day
**Dependencies:** M2-10 (FastAPI server)

## Purpose

Stream live run events and log lines to clients (web UI and CLI) so the user can watch a run unfold in real time. WebSocket-based, multiplexed by run ID.

## URL surface

- `WS /api/v1/ws/runs/{run_id}` — subscribe to a single run's events and logs
- `WS /api/v1/ws/all` — subscribe to all events globally (UI workbench uses this for the active goal feed)

Auth via the same API key, passed in the connection URL: `ws://host/api/v1/ws/runs/{id}?api_key=xxx`. (WebSocket headers are awkward; query string is the practical choice for browsers.)

## Event types

Three kinds of message a client can receive:

```python
class WSMessage(BaseModel):
    type: str  # "event" | "log" | "status"
    run_id: str
    timestamp: datetime

class EventMessage(WSMessage):
    type: Literal["event"] = "event"
    event_name: str  # e.g. "step.started", "step.completed"
    payload: dict

class LogMessage(WSMessage):
    type: Literal["log"] = "log"
    level: str
    source: str
    message: str

class StatusMessage(WSMessage):
    type: Literal["status"] = "status"
    status: str  # current run status
```

## Implementation

`src/carve/server/websocket.py`:

```python
class ConnectionManager:
    def __init__(self):
        # run_id -> set of websockets
        self.run_subscribers: dict[str, set[WebSocket]] = defaultdict(set)
        # all global subscribers
        self.global_subscribers: set[WebSocket] = set()

    async def connect_to_run(self, ws: WebSocket, run_id: str):
        await ws.accept()
        self.run_subscribers[run_id].add(ws)

    async def connect_global(self, ws: WebSocket):
        await ws.accept()
        self.global_subscribers.add(ws)

    def disconnect(self, ws: WebSocket):
        for run_id, subs in self.run_subscribers.items():
            subs.discard(ws)
        self.global_subscribers.discard(ws)

    async def broadcast_to_run(self, run_id: str, message: WSMessage):
        dead = []
        for ws in self.run_subscribers.get(run_id, []):
            try:
                await ws.send_json(message.dict())
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

        # Also broadcast to global subscribers
        for ws in self.global_subscribers:
            try:
                await ws.send_json(message.dict())
            except Exception:
                self.disconnect(ws)
```

## Wiring to the event bus

The event bus from `ARCHITECTURE.md` is in-process for v0.1. Subscribe the WebSocket manager to relevant events:

```python
@event_bus.subscribe("step.started")
async def on_step_started(event):
    await ws_manager.broadcast_to_run(event["run_id"], EventMessage(
        run_id=event["run_id"],
        event_name="step.started",
        payload={"step_id": event["step_id"]},
    ))
```

Same for `step.completed`, `step.failed`, `run.completed`, `run.failed`, `agent.invoked`, etc.

## Wiring to logs

The state store's `append_log` triggers a WebSocket broadcast. Two implementation options:

**Option A — push from append_log:**

```python
def append_log(self, run_id, level, source, message):
    log_entry = self._save_to_db(run_id, level, source, message)
    asyncio.create_task(ws_manager.broadcast_to_run(run_id, LogMessage(
        run_id=run_id,
        level=level,
        source=source,
        message=message,
        timestamp=log_entry.timestamp,
    )))
```

But `append_log` is called from sync code in the runner. Need a thread-safe way to schedule the async broadcast. Use `asyncio.run_coroutine_threadsafe`:

```python
def append_log(self, run_id, level, source, message):
    log_entry = self._save_to_db(run_id, level, source, message)
    if event_loop is not None:
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast_to_run(run_id, ...),
            event_loop,
        )
```

**Option B — polling subscriber:**

A background coroutine polls the database for new log lines and broadcasts. Simpler but adds latency.

**Option A** is preferred for live feel. The threadsafe scheduling is straightforward.

## CLI integration

The CLI's `carve logs <run_id> --follow` opens a WebSocket:

```python
async def logs_follow(run_id: str, api_key: str):
    url = f"ws://localhost:8787/api/v1/ws/runs/{run_id}?api_key={api_key}"
    async with websockets.connect(url) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "log":
                print(format_log_line(msg))
            elif msg["type"] == "event":
                print(format_event(msg))
            elif msg["type"] == "status" and msg["status"] in ("success", "failed", "cancelled"):
                break
```

## Backpressure

WebSockets can be slow. If a client falls behind, don't let it stall the broadcast loop:

- Drop messages destined for slow clients (those whose send buffer is >100 messages backed up)
- Log a warning when this happens
- Keep healthy clients fast

## Initial state on connect

When a client connects to a run that's already running, send the missed history first:

```python
async def connect_to_run_with_history(ws, run_id):
    await ws_manager.connect_to_run(ws, run_id)

    # Send recent logs (last 100 lines)
    logs = repo.get_logs(run_id, limit=100)
    for log in logs:
        await ws.send_json(LogMessage.from_log(log).dict())

    # Send current status
    run = repo.get_run(run_id)
    await ws.send_json(StatusMessage(
        run_id=run_id,
        status=run.status,
    ).dict())
```

This makes the UI feel correct on reconnect or late connection.

## Tests

- A subscribing client receives events broadcast for its run
- A client subscribed to a different run does not receive events for the first
- Global subscribers receive all events
- Disconnected clients are cleanly removed
- History is sent on connect
- Slow clients don't block the broadcast

Use `httpx`'s WebSocket support (or `websockets` library) for tests.

## Acceptance criteria

- A WebSocket connection to a running run streams events and logs as they happen
- The CLI's `--follow` flag works end-to-end
- The web UI's workbench shows a live feed
- Connection drops are handled gracefully on both sides

## Files

- `src/carve/server/websocket.py`
- `src/carve/server/event_bridge.py` (event bus → WebSocket adapter)
- `src/carve/cli/commands/logs.py` (real impl, replaces M1 stub)
- `tests/server/test_websocket.py`

## What this enables

- The UI feels live, not polling-y
- Long runs can be observed in detail
- The CLI stops being a second-class citizen — it streams the same way the UI does
