# Changelog

## [0.4.2] - 2026-11-27

### Fixed

- Raise uvicorn HTTP keep-alive timeout to 65 seconds so reverse proxies can reuse idle upstream connections across 60-second windows without hitting the 5-second default timeout.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.1.
