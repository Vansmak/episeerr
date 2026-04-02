# Episeerr Improvement Backlog

## Medium effort, good value

- ~~**Retry/backoff on external API calls**~~ — done in v3.5.2. Shared `http` session with 3-retry exponential backoff in `episeerr_utils.py`, applied across all files.

- ~~**Consolidate drift detection**~~ — done in v3.5.1. `reconcile_series_drift()` in `episeerr_utils.py` is now the single canonical implementation used by all callers.

- ~~**N+1 in Phase 0 drift detection**~~ — done in v3.5.4. Both bulk Phase 0 loops now build a `series_lookup` dict from the already-fetched `all_series` / `existing_series` and pass it as `series_data=` to `reconcile_series_drift` → `validate_series_tag`, eliminating one Sonarr API call per series.

- ~~**Pending requests: SQLite instead of files**~~ — done in v3.5.3. All pending selection requests now stored in `settings.db` (`pending_requests` table). File-to-DB migration runs automatically on first startup for existing users. The Jellyseerr coordination file (`jellyseerr-{tvdb_id}.json`) remains file-based — it is cross-process state, not a UI-facing request.

## Bigger refactors

- ~~**Extract webhook handlers**~~ — done in v3.5.5. Moved to `webhooks.py` as `sonarr_webhooks_bp` Blueprint. Registered in `episeerr.py`; auth exemptions updated to blueprint-qualified names.

- **`plex.py` is 2400 lines** — likely has unused/dead code. Audit and split.

- **Standardize API error responses** — currently inconsistent across routes (some use `{"status": "error"}`, others use `{"success": false}`, etc.). Pick one format and apply it everywhere.

- ~~**Bulk Sonarr API operations**~~ — done in v3.5.6. Replaced O(n) linear series lookups in all three cleanup loops with dict lookups; added series title cache in dry-run delete loop; eliminated duplicate episode fetch in `trigger_episode_search_in_sonarr`.
