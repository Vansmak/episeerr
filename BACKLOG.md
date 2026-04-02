# Episeerr Improvement Backlog

## Medium effort, good value

- **Retry/backoff on external API calls** — if Sonarr hiccups, webhooks fail silently. Add simple retry with exponential backoff on all `requests.get/post` calls to Sonarr, Jellyseerr, etc.

- ~~**Consolidate drift detection**~~ — done in v3.5.1. `reconcile_series_drift()` in `episeerr_utils.py` is now the single canonical implementation used by all callers.

- **N+1 in cleanup** — `media_processor.py` fetches all series once but then calls individual Sonarr series endpoints in a loop. Batch these or use the data already fetched.

- **Pending requests: SQLite instead of files** — a crash loses pending requests. Move to the existing `settings.db` SQLite database for consistency and durability.

## Bigger refactors

- **Extract webhook handlers** — the Sonarr and Tautulli webhook handlers are 800+ lines combined in `episeerr.py`. Move to a dedicated `webhooks.py` module.

- **`plex.py` is 2400 lines** — likely has unused/dead code. Audit and split.

- **Standardize API error responses** — currently inconsistent across routes (some use `{"status": "error"}`, others use `{"success": false}`, etc.). Pick one format and apply it everywhere.

- **Bulk Sonarr API operations** — some cleanup loops call individual series endpoints. Use batch endpoints where Sonarr supports them.
