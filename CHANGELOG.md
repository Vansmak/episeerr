# Changelog

## v3.7.1

### ✨ Emby & Jellyfin Now Playing widget

Both Emby and Jellyfin now show a **Now Playing** widget on the dashboard alongside the existing Plex widget.

- Pulls active sessions via `/Sessions`; shows poster thumbnail, series/episode title, progress bar, and player name
- Widget is hidden automatically when no session is active or the server is unreachable
- Favorites count pill displayed in the dashboard header pill row
- Poster art is proxied through Episeerr (`/api/integration/{emby,jellyfin}/art`) to avoid mixed-content/HTTPS errors when Episeerr is served over HTTPS but media servers are on HTTP (`emby.py`, `jellyfin.py`)

### 🐛 Bug Fixes

- **SeriesAdd webhook does not respect held state from `+` modifier** — when a new series was added under a rule with an `always_have` `+` modifier (e.g. `e1+`), `process_always_have` correctly grabbed E1 and wrote `activation_seasons[1] = 'held'`, but the SeriesAdd handler continued into normal get-count processing (fetching E2, etc.) before the held gate was checked. Fixed by reloading config after `process_always_have` runs and skipping the entire episode-fetch/monitor/search block when any season is in held state — matching the activation gate that already existed in `process_episodes_for_webhook`. (`webhooks.py`)
- Dashboard widget containers are now hidden (`display: none`) when an integration returns no data or the fetch fails, instead of leaving a blank box. (`templates/dashboard.html`)

---

## v3.7.0

### ✨ Jellyfin & Emby favorites on the dashboard

Jellyfin and Emby users now have a favorites section on the dashboard, sitting alongside the existing Plex Watchlist row.

- Fetches favorited Series and Movies from `/Users/{id}/Items?Filters=IsFavorite` using the configured API key
- Displays poster art, title, year, and a media-type badge — scrollable horizontal strip, same layout as the Plex Watchlist
- **Click a poster** → opens the TMDB detail modal (overview, rating, genres) using the item's `ProviderIds.Tmdb` metadata
- **Click the TV/film badge** (top-left of poster) → removes the item from favorites in Jellyfin/Emby (`DELETE /Users/{id}/FavoriteItems/{itemId}`) and fades it out, matching the Plex remove-from-watchlist interaction
- Section is hidden automatically when no favorites exist; only appears when data is returned
- Collapsible with state persisted in `localStorage`
- No Sonarr/Radarr sync — Jellyfin/Emby favorites represent items already in the library, not a download queue, so syncing is intentionally omitted
- Jellyfin section uses a purple heart badge; Emby uses teal to visually distinguish them
- New routes: `GET /api/integration/jellyfin/favorites`, `POST /api/integration/jellyfin/favorites/remove` and equivalents for Emby

### 🐛 Bug Fixes

- Fixed `pending_deletions.json` entries being written with `episode_number: 0`, `episode_id: null`, and title `S1E0` when the dry-run deletion queue path encountered an episode file whose `episodes` array was empty. The root cause was relying on a `episodes` key that Sonarr does not reliably populate on `/api/v3/episodefile/{id}` responses. The fix reads `episodeIds` instead and resolves the real episode via `/api/v3/episode/{id}`, giving the correct season, episode number, and title. Files where `episodeIds` is empty or the lookup fails are now skipped with a warning log rather than queued with placeholder data. (`media_processor.py`)
- Fixed `get_sonarr_latest_file_date()` returning only a timestamp and causing the Sonarr file-date fallback path in `get_activity_date_with_hierarchy()` to hardcode `season=1, episode=1` for all series. The function now resolves the actual episode for the latest file via `episodeIds` → `/api/v3/episode/{id}` and returns `(timestamp, season, episode_number, episode_id)`. The caller uses the real values, so grace-period cleanup decisions are based on the correct last-file episode rather than always treating the series as if only S1E1 had been seen. (`media_processor.py`)

---

## v3.6.9

### 🐛 Bug Fixes

- Fixed cleanup job returning 401 from Sonarr `/api/v3/episodefile` for every series — `get_sonarr_latest_file_date()` was sending `X-Api` instead of `X-Api-Key` in the request header, so Sonarr never received the API key. All other Sonarr calls used the correct header name; only this function was affected. (`media_processor.py`)
- Fixed series lookup missing localized/alternate titles — `get_series_id()` now checks each series' `alternateTitles` array from Sonarr as a fallback when the primary title match fails. Titles are normalized (lowercase, punctuation stripped, whitespace collapsed) before comparison, so names like "Es - Welcome to Derry" correctly match Sonarr's "Es Welcome To Derry" alternate title. (`media_processor.py`)

---

## v3.6.8

### ✨ Playback start activation for `+` modifier

When any integration receives a **playback start** event for the activation episode of a series in held state (i.e. using the `+` modifier), the hold is released and the rule executes immediately — without waiting for the watch-completion threshold to be met.

- **Plex**: fires on `media.play` for all detection methods (polling, scrobble, stop+threshold). Marks the episode as processed so later threshold/scrobble events don't double-fire.
- **Jellyfin**: fires on `SessionStart` / `PlaybackStart` for all detection methods (polling and progress). Marks the tracking key so the stop handler and polling thread don't double-fire.
- **Emby**: fires on `playback.start` / `SessionStart`. Same tracking-key dedup as Jellyfin.
- **Tautulli**: fires on `Playback Start` notification type. Requires `"notification_type": "{notification_type}"` in the JSON template and a separate "Playback Start" notification agent (see setup below). Existing "Watched" webhook behaviour is completely unchanged.

All other series and rules are entirely unaffected — the check is a no-op for anything not in held state with a `+` modifier.

**New shared function:** `is_held_activation_episode(series_name, season, episode)` in `media_processor.py` — single source of truth for the held-activation check across all integrations.

---

## v3.6.7

### 🐛 Bug Fixes

- Fixed Approve All / Reject All buttons misalignment in Pending Deletions accordion headers — buttons are now outside the `accordion-button` element to avoid Bootstrap flexbox/chevron conflicts
- Jellyfin documentation fixes

---

## v3.6.6

### ✨ Release keep on season finale

- New rule flag: **Release Keep on Season Finale** (`release_keep_on_finale`)
- When the last episode of a season is watched and no next season exists in Sonarr with future or unscheduled episodes, episodes currently held in the keep window are released from keep protection
- If a grace period (`grace_watched`) is set on the rule, released episodes enter the grace countdown (timer starts from the finale watch); otherwise they are deleted immediately
- "No next season" = no episodes exist in a future season without a file and with a future or null air date — if any such episodes exist, the keep window is left unchanged
- Anchor-protected episodes (`always_have`, `keep_pilot`) are never released
- Fires only on the season finale (highest episode number in the season), not on mid-season watches
- Works independently of the `+` activation modifier system
- Checkbox added to Create Rule and Edit Rule forms below Keep Pilot

### 🎨 UI

- App version now displayed at the bottom of the sidebar (e.g. `v3.6.5`) — useful for users running the `latest` Docker tag

---

## v3.6.5

### ✨ Always Have expression modifiers (`+` and `-`)

Two independent modifier suffixes extend the `always_have` expression language.
Existing expressions without modifiers behave exactly as before.

| Expression | Behaviour |
|---|---|
| `s*e1` | Grab & permanently keep E1 of every season (unchanged) |
| `s*e1-` | Grab E1 of every season; follows grace/keep rules after watched |
| `s*e1+` | Grab E1 of every season; each season held until its E1 is watched |
| `s*e1+-` | Per-season gate + E1 removable after activation |
| `e1+` | Sequential: grab only current season's E1, advance on finale |
| `e1+-` | Sequential + removable |
| `s1e1+` | Activation on pilot only; full auto from S2 |
| `pilot+` | Alias for `e1+` |

- **`+` activation gate**: rule's get-count suppressed until the activation episode is watched; state is per-season, stored as `activation_seasons` in `config.json`
- **`-` removable**: activation episodes become subject to normal grace/keep rules after activation fires (rather than being permanent anchors)
- **Sequential mode** (`e1+`/`e1+-`): on season-finale watch, the next season's E1 is automatically grabbed and that season enters held state
- Series already in progress when a `+` rule is assigned are treated as active immediately (no retroactive hold)
- Ended series: sequential mode does not advance past the final season
- Expression validation added to Create Rule and Edit Rule forms with inline error display
- `get_count` now accepts 0 (keep-only mode)

### 🔄 Future season reconciliation (cleanup phase 0.5)

- During every scheduled cleanup run, Episeerr now scans all managed series for seasons where every episode is in the future (or has no air date yet) and none have been downloaded
- Such seasons are unmonitored — they were auto-monitored by Sonarr when announced but Episeerr should control them
- The rule's `always_have` expression is then re-applied: e.g. `s*e1+` will monitor E1 and set the season to held state
- Sequential mode (`e1+`) is skipped during reconciliation — those seasons are handled by the on-finale advance logic
- Seasons containing any past air date or any downloaded file are never touched
- Runs as Phase 0.5 in `run_unified_cleanup()`, between tag reconciliation and dormant cleanup

### 🐛 Fixes
- **Jellyfin**: watched episodes no longer appear in the "Ready to Watch" dashboard section — the calendar now supplements `watched.json` with a direct Jellyfin API query for played episodes, catching series that don't have Episeerr rules or whose watch events were missed

---

## v3.6.2

### 🎨 UI
- Dark theme applied consistently to Pending Deletions and Episeerr index pages — accordion, table, and alert-warning now use app CSS variables instead of Bootstrap light defaults

---

## v3.6.1

### 🔒 Deferred Sonarr add — nothing written until you confirm
- Searching within Episeerr no longer adds to Sonarr or Plex watchlist on click — Sonarr add and Plex watchlist update only happen after the user confirms rule/season selections
- Cancel at any point and nothing is touched

### 🗄️ Pending requests moved to SQLite
- Pending selection requests stored in `settings.db` instead of JSON files; auto-migrated on startup

### ⚡ Performance
- Config and Sonarr tag lookups cached in memory (30s / 60s); reduces API chatter on busy webhook paths
- Retry/backoff on all external API calls (3 retries, exponential backoff, covers 429/5xx)
- Eliminated N+1 Sonarr API calls in cleanup loops and drift detection

### 🏗️ Refactor
- Sonarr webhook handlers extracted to `webhooks.py` (~700 lines out of `episeerr.py`)
- Drift detection consolidated to a single canonical `reconcile_series_drift()` function

### 🐛 Fixes
- Sonarr webhook drift correction was running twice; second pass corrupted rule assignment
- Jellyfin/Emby: rule not passed to media_processor subprocess — drift corrections were lost
- Startup crash when `@app.before_request` decorator was left on the setup route
- Jellyseerr auto-delete removed — was firing before activity was saved, causing race conditions

### 📄 Docs
- README and in-app documentation updated to reflect the three add paths and correct delay profile scope

---

*For older versions, see [legacy changelog](CHANGELOG_OLD.md)*
