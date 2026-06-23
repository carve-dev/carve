---
name: _reference_hackernews
description: >-
  Hacker News API source for dlt. Loads stories, comments, and users from the
  public Firebase-backed Hacker News API (https://github.com/HackerNews/API) —
  no auth, no credentials. The reference curated source: proves the dlt_library
  copy/customize/provenance flow end to end against the creds-free DuckDB
  substrate.
supported_destinations:
  - duckdb
  - snowflake
  - bigquery
  - postgres
last_updated: "2026-06-23"
---

# Hacker News API source (reference)

This is Carve's **reference** curated dlt source. The Hacker News API is public
and unauthenticated, which makes it the cleanest possible target for proving the
`dlt_library` flow: copy the bundled source into `el/<component>/`, customize it
for a destination, and run it against the creds-free DuckDB substrate.

It is intentionally minimal. Its job is to prove the framework slots a curated
pack in — not to be a production HN connector.

## What the bundled source loads

The bundled `scripts/__init__.py` defines a `hacker_news()` dlt source with two
resources:

- `top_stories` — the current top stories (item objects: title, url, score, by,
  time, descendants, …), capped by `max_items`.
- `items` — arbitrary items fetched by id (stories, comments, jobs, polls).

The Hacker News API base is `https://hacker-news.firebaseio.com/v0`. No
credentials are required, so a copied pack runs immediately against DuckDB.

## Using it (copy + customize)

`dlt_library` copies `scripts/` into `el/<component>/` and applies any
`customization` you pass as `__UPPER__` placeholder substitutions, then stamps a
Carve provenance header recording this pack's name + commit. The bundled source
reads two customization points from module-level placeholders:

- `__DESTINATION__` — the dlt destination name (default `duckdb`).
- `__SCHEMA__` — the dataset/schema the pipeline writes into (default
  `hacker_news`).

There are **no credentials** for Hacker News, so the credential-env-var
customization point is a no-op for this reference pack (it is exercised by the
authenticated curated sources in the first wave). If you copy this pack and then
target an authenticated destination (e.g. Snowflake), set the destination's
credentials via the usual dlt env vars (e.g. `DESTINATION__SNOWFLAKE__*`) — those
belong to the destination, not to the HN source.

## Customization notes

- **Destination / schema:** override via the `destination` / `schema`
  customization keys at copy time (they substitute `__DESTINATION__` /
  `__SCHEMA__`). Edit below the provenance header to change resource selection or
  the item cap — edits below the header are preserved on regenerate.
- **Provenance:** the copied `__init__.py` carries the Carve header recording it
  was generated from `carve/sources/_reference_hackernews` at the library
  commit. Do not edit the header.
- **Requirements:** `scripts/requirements.txt` pins `dlt[duckdb]` to the Carve-
  pinned dlt so the copied pack runs against the DuckDB substrate with no extra
  setup.

## Per-source introspection

Hacker News has no auth-gated object catalog, so this reference pack ships a
small documented stub (`scripts/introspect.py`) listing the handful of HN
endpoints (top stories, item-by-id, user-by-id) rather than live
`*_list_objects` / `*_describe_object` tools. Real per-source introspection
tools land with the curated first wave; consult `scripts/introspect.py` for the
endpoint map when authoring against HN.
