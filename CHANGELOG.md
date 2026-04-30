# Changelog

All notable changes to Carve are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `carve init` now writes commented-but-complete templates for
  `connections.toml`, `models.toml`, `runner.toml`, and `.env.example`.
  A new user can fill in values without consulting Carve's source.
- `ModelsConfig.anthropic_api_key` is now optional at load-time; commands
  that need it (`plan`, `build`) raise a `ConfigError` pointing at
  `carve/models.toml` when the key is unset.
