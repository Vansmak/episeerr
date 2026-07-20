# Changelog

## v3.7.17

### 🐛 Bug Fixes

- **Approving a queued deletion hung the request forever — browser eventually threw "NetworkError when attempting to fetch resource," and the whole service went unresponsive until restarted** — regression from v3.7.15's fix. `approve_deletions()` holds `pending_lock` (a plain, non-reentrant `Lock`) for its whole body, and while still holding it, called `delete_episodes_immediately(..., rule_dry_run=False)` to force a real delete. But that function's dry-run check is `global_dry_run or rule_dry_run` — forcing only the rule-level flag doesn't help when `dry_run_mode` is on globally (the default for new installs), so it still routed into the queueing branch, which calls back into `add_to_pending_deletions()` — which itself takes `pending_lock`. Re-acquiring a lock the same thread already holds blocks forever, and once enough threads pile up waiting on it (1 worker/8 threads since v3.7.14), the whole app stops responding. Fixed: `delete_episodes_immediately()` now takes a `force=True` param that bypasses both global and rule dry-run outright, which `approve_deletions()` now passes; `approve_deletions()` also no longer holds `pending_lock` across the delete call, so this class of deadlock can't happen via any future path either. (`media_processor.py`, `pending_deletions.py`, #35)

---

## v3.7.16

### 🐛 Bug Fixes

- **A Tautulli "Playback Start" webhook sent to the legacy `/webhook` route was processed as a full watched event, which could delete the episode you just started playing** — the newer `/api/integration/tautulli/webhook` route already guarded playback-start events (only acting on them for a held `e1+`-style activation, ignoring them otherwise), but that guard lived in the route handler, not in the shared `process_watch_event()` it calls. The legacy `/webhook` route (still the one many existing Tautulli setups point at) calls `process_watch_event()` directly and had no such guard, so a playback-start on a non-held series ran the full watched pipeline — updating activity to the just-started episode, unmonitoring it, and running keep-window/finale-release cleanup on it. On a season finale with `release_keep_on_finale` enabled and no `grace_watched` set, this deleted the file mid-playback. Fixed: the guard now lives inside `process_watch_event()` itself, so every caller (including the legacy route) is protected; the integration route's duplicate copy of the same logic was removed. (`integrations/tautulli.py`, #62)

---

## v3.7.15

### 🐛 Bug Fixes

- **Approving a queued deletion did nothing — "Successfully deleted 0 episode(s)", episode reappears in the queue next cleanup run** — regression from v3.7.12's dry-run fix. `pending_deletions.approve_deletions()` still called `delete_episodes_immediately()` with its old positional shape (`episode_file_ids, False, series_title`), which doesn't match the new signature (`episodes, series_id, series_title, ...`). Since `episodes` was expected to be a list of dicts but got bare file-ID ints, the first `.get()` call raised immediately — silently swallowed by the batch's exception handler, so nothing was deleted and no real error surfaced. Fixed: `approve_deletions()` now builds proper episode dicts and calls the current signature, explicitly forcing `rule_dry_run=False` since approving from the queue is the explicit confirmation to delete regardless of the dry-run setting that queued it. (`pending_deletions.py`, #35)

---

## v3.7.14

### 🐛 Bug Fixes

- **Config changes (Sonarr URL/key, service toggles) only ever took effect for about half of requests, randomly, until a full restart** — the container ran 2 gunicorn worker processes, each with its own Python memory. `reload_module_configs()` (used by Save and by the service toggle) only reloads modules in whichever single worker handled that request; the other worker kept serving stale `SONARR_URL`/`SONARR_API_KEY` until it happened to reload independently, which without a second config change never happens. Since gunicorn round-robins requests across workers, this looked like intermittent failures (e.g. the Xadarr library browser's Sonarr-backed Shows tab going empty while Movies kept working) that a full restart always "fixed" by resetting both workers to the same state. Fixed: switched from 2 sync workers to 1 worker with 8 threads (`gthread`), so there's only one process/memory space — no more divergence — while still handling concurrent requests (including long-lived SSE streams) without blocking. (`Dockerfile`)

---

## v3.7.13

### 🐛 Bug Fixes

- **Service enable/disable toggles on the Setup page silently did nothing for most services** — `POST /api/toggle-service/<service>` ran a bare `UPDATE` that assumed a `services` row already existed. Anyone who configured a service (Sonarr, Radarr, Jellyfin, Plex, Tautulli, Emby) via env vars instead of saving it through the Setup UI had no row for the toggle to update, so it 404'd and the checkbox silently snapped back — looking exactly like the toggle didn't work. Fixed: the toggle now seeds a row from that service's existing env-var config when none exists yet. (`episeerr.py`, #82)
- **Disabling a service didn't actually stop it from running if env vars were also set** — `get_service()` returns `None` both when a row doesn't exist *and* when a row exists but is disabled, and every `get_<service>_config()` helper treated `None` as "fall back to env vars" either way. So toggling a service off only changed the Setup page's badge; anything reading its config directly (webhooks, dashboard, movie/media processors) kept using the env-var config regardless. Fixed: added `is_service_disabled()` and each getter now checks it before falling back to env vars, so an explicit disable is actually honored. (`settings_db.py`)
- **Sonarr toggle didn't take effect until clicking Save** — `SONARR_URL`/`SONARR_API_KEY` are loaded once into module-level globals at startup and were only refreshed by the existing Save flow's `reload_module_configs()` call. Toggling updated the DB but not the live in-memory connection. Fixed: the toggle route now calls `reload_module_configs()` too. (`episeerr.py`, #82)
- **Re-enabling a service left its badge stuck on "Disabled"** — `toggleServiceEnabled()` only had a code path to set the "Disabled" badge, none to restore it on re-enable. Fixed: a successful enable now reloads the page, which recomputes the real Connected/Not Connected/Disabled state server-side. (`templates/setup.html`)

---

## v3.7.12

### 🐛 Bug Fixes

- **Dry-run queueing for Keep Rule and Grace/Dormant cleanup silently dropped every episode** — after v3.7.10 taught `delete_episodes_immediately()` and `delete_episodes_in_sonarr_with_logging()` to check dry-run before deleting, both functions tried to resolve the episode info needed to queue an approval by looking up `episodeIds` on Sonarr's `episodefile/{id}` endpoint, which isn't reliably populated. Every episode was logged as "cannot resolve episode info — skipping" and never reached the pending-approval queue, so dry run stopped deletions but left nothing to review or approve. Fixed: both functions now take the episode data their callers already have on hand (from the same `/api/v3/episode?seriesId=` fetch that decided what to delete) instead of re-deriving it from Sonarr. (`media_processor.py`, #35)

---

## v3.7.10

### ✨ Configurable Quality Profiles

Sonarr and Radarr now have a **Preferred Quality Profile** dropdown in setup, replacing the old "enter the numeric ID" text field for Radarr and adding the option for Sonarr entirely.

- Setup page auto-populates the dropdown from the live Sonarr/Radarr API; no need to look up profile IDs
- Selected profile is used by all automatic adds: Trakt sync, Plex watchlist sync, Discover adds, and manual series prep
- Falls back to the first available profile if none is saved, with a log warning

### 🎨 Black & Gold Theme

New `data-theme="black-gold"` — pure black background with rich gold accents. Available in the sidebar theme switcher. Gold frame touches on cards, nav bar, modals, and sidebar border.

### 🐛 Bug Fixes

- **Trakt watchlist "Added" badge never progressed to "Available"** — items added to Sonarr/Radarr by Trakt sync were permanently stored as `added_to_sonarr`/`added_to_radarr` in the sync file, and `get_watchlist_with_status` used that stored status without ever rechecking. Fixed: when the stored status is `added_to_radarr`, a live Radarr check verifies `hasFile`; for `added_to_sonarr`, checks `episodeFileCount > 0`. Badge automatically promotes to "Available" on the next dashboard load after the file lands. (`integrations/trakt.py`)
- **Keep Rule real-time deletions ignored both global and rule-level dry-run** — `delete_episodes_immediately()`, used by the webhook-triggered "episodes leaving keep block" and season-finale cleanup paths, deleted episode files straight from Sonarr with no dry-run check at all (unlike the scheduled Grace/Dormant cleanup path, which already checked both flags). Enabling dry run no longer stopped these deletions when triggered by a watch webhook. Fixed: it now checks global `dry_run_mode` and the rule's `dry_run` flag and queues the deletion for approval instead of deleting live, matching the scheduled cleanup path. (`media_processor.py`)
- **Caught-up airing shows never received newly aired episodes** — `episeerr_default` was bound as one of the three "control tags" on the Sonarr delay profile alongside the genuinely transient `episeerr_select`/`episeerr_delay`, but it's actually the shipped `default` rule's own permanent tag, same as `episeerr_one_at_a_time` or any custom rule tag. Any series left on the `default` rule therefore had its automatic/RSS grabs held forever instead of just during initial processing: once a user caught up on an airing show, no watch event ever fired again to trigger Episeerr's own search, so newly aired episodes silently never downloaded. A coupled bug in `validate_series_tag()`/`reconcile_series_drift()` also misread a correctly-tagged `episeerr_default` series as having no tag at all, causing a redundant tag-restore loop on every reconciliation pass. Fixed: `default` is no longer special-cased and now behaves like every other rule (temporary hold only during initial select/processing, normal RSS afterward). Self-heals existing installs on next restart. (`episeerr_utils.py`)

---

## v3.7.7

### ✨ Trakt Integration

Episeerr can now sync your Trakt watchlist to Sonarr/Radarr.

- **OAuth device code flow** — no browser redirect needed; generate a code in setup, approve it on Trakt.tv, poll confirms and saves tokens automatically
- **Watchlist sync**: fetches shows and movies from Trakt watchlist and adds them to Sonarr/Radarr using the same rule-assignment logic as other integrations
- **Dashboard widget** — poster card strip on the dashboard showing your Trakt watchlist with Sonarr/Radarr status badges; click a card to remove it from Trakt
- **Token auto-refresh** — access token silently refreshed on expiry using the stored refresh token; no re-auth needed

### ✨ Service Enable/Disable Toggle

Integration cards on the setup page now have an enable/disable toggle that persists without clearing configuration.

- Toggle fires `POST /api/toggle-service/<service>` with `{"enabled": true/false}` and updates the `services` DB row without touching any other config fields
- `get_service()` already filters `WHERE enabled = 1`, so disabled services are automatically excluded from all polling, cleanup, and webhook dispatch

### 🐛 Bug Fixes

- **Grace period cleanup ignored global dry-run mode** — `delete_episodes_in_sonarr_with_logging` only checked the rule-level `dry_run` flag; the global `dry_run_mode` setting was not consulted. Fixed by loading `global_settings` and ORing both flags — if either is true the deletion is queued for approval rather than executed live. (`media_processor.py`)
- **`parse_date_fixed` date parsing failures on non-standard Sonarr timestamps** — added a multi-method parser that handles UTC `Z` suffix, fractional seconds (stripping milliseconds before re-parsing), and timezone-naive ISO strings. Prevents cleanup runs from silently skipping files with timestamps Sonarr emits in edge cases (e.g. items added during DST transitions). (`media_processor.py`)

---

## v3.7.6
fixed movie rule edit delete navigation
## v3.7.5

### ✨ Movie Rules

Episeerr now manages Radarr movies via a dedicated **Movie Rules** system, separate from series rules.

- **Movie Rules page** (`/movie-rules`): create rules that associate a Radarr tag (`episeerr-<rule>`) with cleanup behaviour
- **Grace Watched**: delete a movie N days after it was last watched (source: Plex, Jellyfin, Emby, or Tautulli)
- **Dormant**: delete a movie N days after it was added to Radarr when no watch history is found at all
- **Watch history cache** built once per cleanup run using a priority chain — Plex / Jellyfin / Emby (TMDB ID exact match) → Tautulli (title-normalized fallback); Radarr added-date is only used as the fallback for truly unwatched (dormant) movies
- **Radarr webhook** (`/radarr-webhook`): connect in Radarr Settings → Connect → Webhook; when a movie is added with no episeerr tag, Episeerr auto-applies the configured default movie rule tag
- **Default movie rule**: mark any rule as the default in the Movie Rules UI; auto-tagging on addition only fires when a default is set
- **Library movies tab**: movies tab in the library shows all Radarr movies with their assigned rule; rule can be assigned/changed from the drawer
- **Pending Deletions integration**: movies flagged by cleanup appear in Pending Deletions with approve/reject support; `require_approval` and `dry_run` flags work identically to series rules
- **Rules Summary** on the admin scheduler page now shows series and movie rule counts separately

### 🐛 Bug Fixes

- Fixed Radarr tag validation — tags now use hyphens (`episeerr-rule-name`) instead of underscores; Radarr enforces `^[a-z0-9-]+`
- Fixed gunicorn two-worker race condition on tag creation: tag-create now retries a GET on `UNIQUE constraint` failure and returns the existing tag
- Fixed series rule/status filter bars remaining visible when switching to the Movies tab (Bootstrap 5 `!important` specificity — now uses `style.cssText`)
- Fixed `/scheduler` page 500 — wrong `url_for('pending_deletions')` → `url_for('view_pending_deletions')`
- Fixed `loadRecentActivity` null guard — referenced `#recent-activity` div that was removed in a prior refactor
- Fixed movie rule Edit button — `tojson` in double-quoted HTML attribute terminated the attribute early; changed to single-quoted `onclick='...'`

---

## v3.7.2

### ✨ Universal Sidebar Search

A persistent search bar now lives in the sidebar, accessible from any page via the input or `Ctrl+K`.

- **Tier 1 (instant):** settings index, nav index, quick links, and watch history — resolved from local data with no network calls
- **Tier 2/3 (parallel):** fans out concurrently via `ThreadPoolExecutor` to Sonarr, Radarr, Plex, Jellyfin, Emby, TMDB, Jellyseerr, Tautulli, and Docker containers
- Results grouped by source with poster art, media-type badges, and direct action links
- Modal TMDB search (Ctrl+K / search icon) is now TMDB-only via `/api/discover/search`; full library/history search is handled by the new `/api/search` and `/search` page route
- `Ctrl+K` keyboard shortcut focuses the sidebar search input from anywhere (`episeerr.py`, `templates/base.html`)

### ✨ Emby & Jellyfin Now Playing widget

Both Emby and Jellyfin now show a **Now Playing** widget on the dashboard alongside the existing Plex widget.

- Pulls active sessions via `/Sessions`; shows poster thumbnail, series/episode title, progress bar, and player name
- Widget is hidden automatically when no session is active or the server is unreachable
- Favorites count pill displayed in the dashboard header pill row
- Poster art is proxied through Episeerr (`/api/integration/{emby,jellyfin}/art`) to avoid mixed-content/HTTPS errors when Episeerr is served over HTTPS but media servers are on HTTP (`emby.py`, `jellyfin.py`)

### 🐛 Bug Fixes

- **`process_always_have` ignores requested season for `e1+` rules** — in sequential mode, the function always started from the lowest-numbered season regardless of the season specified in the SeriesAdd webhook. Added `starting_season` parameter; sequential mode now uses the requested season and falls back to the lowest-numbered season only if the requested one isn't present. (`media_processor.py`, `webhooks.py`)
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
