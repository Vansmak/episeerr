# Episeerr Improvement Backlog

## Medium effort, good value

- ~~**Retry/backoff on external API calls**~~ — done in v3.5.2. Shared `http` session with 3-retry exponential backoff in `episeerr_utils.py`, applied across all files.

- ~~**Consolidate drift detection**~~ — done in v3.5.1. `reconcile_series_drift()` in `episeerr_utils.py` is now the single canonical implementation used by all callers.

- **N+1 in Phase 0 drift detection** — bulk reconciliation loops call `reconcile_series_drift` → `validate_series_tag` → `get_series_from_sonarr` (individual API call) per series, even though all series data was already fetched above. Episode fetches in the cleanup loops (`fetch_all_episodes`) are unavoidable — Sonarr has no batch episodes endpoint.

- **Pending requests: SQLite instead of files** — a crash loses pending requests. Move to the existing `settings.db` SQLite database for consistency and durability.

## Bigger refactors

- **Extract webhook handlers** — the Sonarr and Tautulli webhook handlers are 800+ lines combined in `episeerr.py`. Move to a dedicated `webhooks.py` module.

- **`plex.py` is 2400 lines** — likely has unused/dead code. Audit and split.

- **Standardize API error responses** — currently inconsistent across routes (some use `{"status": "error"}`, others use `{"success": false}`, etc.). Pick one format and apply it everywhere.

- **Bulk Sonarr API operations** — some cleanup loops call individual series endpoints. Use batch endpoints where Sonarr supports them.
