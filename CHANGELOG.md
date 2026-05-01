# Changelog

All notable changes to Carve are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The CLI now auto-loads a project-local `.env` (defaulting to
  `<project-dir>/.env`, overridable with `--env-file`) before any command
  runs. Existing shell vars win — `.env` provides defaults only. Set
  `CARVE_NO_DOTENV=1` to disable for users managing env vars elsewhere
  (direnv, mise, 1Password CLI).

### Changed

- `carve init` now writes commented-but-complete templates for
  `connections.toml`, `models.toml`, `runner.toml`, and `.env.example`.
  A new user can fill in values without consulting Carve's source.
- `ModelsConfig.anthropic_api_key` is now optional at load-time; commands
  that need it (`plan`, `build`) raise a `ConfigError` pointing at
  `carve/models.toml` when the key is unset.
