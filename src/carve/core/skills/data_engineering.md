# Skill: data_engineering

Universal extract-load patterns. Load this skill when the source side
of the task is non-trivial — paginated APIs, retries, watermark logic,
streaming, type coercion. Trivial single-page reads do not need it.

## Pagination patterns

### Offset / limit

```python
offset = 0
while True:
    rows = fetch(url, offset=offset, limit=PAGE_SIZE)
    if not rows:
        break
    yield from rows
    offset += len(rows)
    if len(rows) < PAGE_SIZE:
        break
```

### Cursor

```python
cursor = None
while True:
    payload = fetch(url, cursor=cursor)
    yield from payload["items"]
    cursor = payload.get("next_cursor")
    if not cursor:
        break
```

### Link-header

```python
url = first_url
while url:
    response = session.get(url)
    response.raise_for_status()
    yield from response.json()
    url = _parse_link_header(response.headers.get("Link", "")).get("next")
```

## Retry with exponential backoff

Idempotent retry on 5xx, network errors, and 429 (rate limit). Honor
`Retry-After` when the server provides it.

```python
import time
import random

def _with_retries(fn, *, max_attempts=5, base_delay=1.0):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except _Retryable as exc:
            last_exc = exc
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                delay = float(retry_after)
            else:
                delay = base_delay * (2 ** attempt) * (1 + random.random() * 0.1)
            time.sleep(delay)
    raise last_exc
```

`_Retryable` is the union of HTTP 5xx / 429 and network errors. Do
not retry 4xx (other than 429) — they will not succeed without a
code change.

## Watermark / incremental extraction

Read the max watermark from the destination, pull source rows after
it, then write the inserted rows and the new watermark in the same
transaction.

```python
with conn.cursor() as cur:
    cur.execute(f"SELECT COALESCE(MAX({wm_col}), '1970-01-01') FROM {fqn}")
    last_watermark = cur.fetchone()[0]

new_rows = list(extract_after(last_watermark))
if not new_rows:
    print(f"[load] no new rows since {last_watermark}; exiting")
    return

with conn.cursor() as cur:
    cur.executemany(insert_sql, new_rows)
    conn.commit()
```

The COALESCE on first run is the cold-start case; the source query
filters to `event_time > <last_watermark>` server-side when the API
supports it.

## Idempotent writes

Decision tree:

- Source has a stable PK and late-arriving updates can replace prior
  values → `MERGE` upsert (`merge_upsert`).
- Source has a stable PK but no late updates → `INSERT … IF NOT
  EXISTS` (Snowflake: emulate via `MERGE … WHEN NOT MATCHED THEN
  INSERT`).
- Source is monotonic and append-friendly → append-only (`append_only`).
- Source is small and we want a fresh snapshot each run → wipe-and-
  reload (`truncate_load`) inside a single transaction.

The choice is fixed by the design's `transformation.strategy`. Do
not deviate without `submit_step(error=True)`.

## Memory-bounded streaming

Generator pipelines beat materialized lists for large extracts:

```python
def extract():
    for page in paginated_api():
        for row in page:
            yield row

def chunked(it, n):
    chunk = []
    for item in it:
        chunk.append(item)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk

for batch in chunked(extract(), 1000):
    cursor.executemany(insert_sql, batch)
```

`executemany` batches of 1k–10k rows are a good default for
Snowflake row-level loads; bigger is rarely faster and risks
exceeding statement limits.

## Type coercion for JSON-ish nested data

Snowflake's `executemany` rejects Python `dict` and `list` bindings
with an opaque error. Two options:

### Option 1 — stringify, store as VARCHAR / VARIANT

```python
import json

def _coerce(row):
    return tuple(
        json.dumps(v) if isinstance(v, (dict, list)) else v
        for v in row
    )

cursor.executemany(insert_sql, [_coerce(r) for r in rows])
```

Destination column declared as `VARIANT` (preferred — Snowflake parses
the JSON automatically) or `VARCHAR(N)`.

### Option 2 — VARIANT with `PARSE_JSON`

When the column is `VARIANT` and the source guarantees valid JSON
strings:

```sql
INSERT INTO {fqn} (id, payload)
SELECT column1, PARSE_JSON(column2)
FROM VALUES (?, ?)
```

This is the canonical fix for the Iowa-liquor regression: the
Socrata `location` column comes back as a `dict`, and binding it
raw blows up with the executemany error. Either option above
resolves it.

## Structured logging

The Carve runner parses progress lines. Use these formats:

- `print(f"[extract] page={i} rows={n}")` — once per page.
- `print(f"[load] inserted={n} table={t}")` — once per batch.
- `print(f"[done] total_rows={n} duration_ms={d}")` — at the end.

Match the bracketed-tag form. Do not use `logging.info` in the
script body; the runner streams stdout and the bracketed prefix
makes filtering trivial for the observer.

## Connection management

Read every credential from the environment with `os.environ['X']`
(no `.get` defaults). Pass `role=` explicitly to
`snowflake.connector.connect(...)`. Never rely on the connector's
env auto-discovery (Carve disables it).

```python
import os
import snowflake.connector

target = os.environ["CARVE_ACTIVE_TARGET"]  # injected by the runner

conn = snowflake.connector.connect(
    user=os.environ[f"{target.upper()}_SNOWFLAKE_USER"],
    password=os.environ[f"{target.upper()}_SNOWFLAKE_PASSWORD"],
    account=os.environ[f"{target.upper()}_SNOWFLAKE_ACCOUNT"],
    role=os.environ[f"{target.upper()}_SNOWFLAKE_ROLE"],
    warehouse=os.environ[f"{target.upper()}_SNOWFLAKE_WAREHOUSE"],
    database=os.environ[f"{target.upper()}_SNOWFLAKE_DATABASE"],
    schema=os.environ[f"{target.upper()}_SNOWFLAKE_SCHEMA"],
    paramstyle="qmark",
)
```

Use `try / finally` to close the connection on every code path.
Do not catch `Exception` and swallow — let failures propagate so
the runner records the failure and surfaces it in `carve runs`.
