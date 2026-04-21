## v3.5.0

### 🔌 Dispatcharr Integration
- Dashboard stat pill shows active streams and queue count
- Streaming widget on dashboard — live view of active sessions with channel, quality, and user info
- Auto-generates sidebar quick link when configured
- Fixed widget response format to return JSON `{success, html}` (was returning raw HTML string)

### 🎬 Plex — Native Webhook Episode Detection (replaces Tautulli requirement)
- Plex now has its own standalone integration module (`integrations/plex.py`)
- **Three detection modes** — choose per your setup:
  - **Scrobble (90%)**: Plex's native watched event. Zero config, most reliable.
  - **Stop + Threshold**: Process when you stop at ≥ your threshold % (e.g. 50%). Scrobble fires automatically as a safety net — if the stop event is missed (crash, autoplay, network drop) processing happens at 90% instead. Episodes are never double-processed.
  - **Polling**: Background thread checks `/status/sessions` every N minutes at custom threshold.
- **Allowed Users** filter — comma-separated Plex usernames; blank = all users
- **Webhook URL:** `http://your-episeerr:5002/api/integration/plex/webhook` (Plex Pass required)
- Dashboard "Now Playing" widget driven by the same webhook (all modes)
- Stale-state fallback: widget falls back to polling when webhook state is > 5 min old

### 🔀 Tautulli — Standalone Optional Integration
- Tautulli moved to its own module (`integrations/tautulli.py`), no longer hardcoded
- **Tautulli is now optional** — only needed if you prefer its watch history over Plex's native API
- **Override Plex** toggle: when enabled, all watch-history lookups (grace period, dormant detection) use Tautulli instead of Plex
- Webhook URL updated: `http://your-episeerr:5002/api/integration/tautulli/webhook`
- Legacy URL `/webhook` still works — existing Tautulli setups continue without changes
- **⚠️ If you use Plex native webhooks for episode detection, do NOT also configure a Tautulli "Watched" webhook — use one or the other, not both**

### 🏗️ Integration System Improvements
- `setup.html` hardcoded Plex/Tautulli card removed — both now appear in auto-generated Integrations section
- `setup_complete` check updated: Sonarr + any media server (Plex, Jellyfin, Emby) satisfies setup
- `get_media_server()` endpoint now includes Plex directly (not via Tautulli)
- `get_optional_integrations()` excludes Plex and Tautulli from the optional sidebar section
- Auth bypass list updated for new integration webhook endpoints
- Watch history router `get_episode_watch_history(rating_key)` added — routes to Tautulli (if override enabled) or Plex
- `get_plex_series_watch_history()` added — queries max `lastViewedAt` across all episodes in a series
- `settings_db.py`: added `get_plex_config()` and `get_tautulli_config()` with env fallback

### 🐛 Fixes
- Plex Watchlist: TV shows stuck on "Requested" — `episodeFileCount` nested under `statistics` in Sonarr API, not top-level
- Plex Watchlist: TV shows never showed "Watched" — `mark_item_watched` now sets `watched=True`/`status='watched'` for TV
- Plex Polling: `session_key` now read from `Metadata.sessionKey` (was reading from `Player` which is empty/UUID)
- Plex Polling: session match now falls back to title/season/episode if key doesn't match
- Plex Polling: `session_key` always non-empty — content-based fallback prevents polling thread never starting
- Plex Polling: threshold checked on every play/pause/resume webhook event — triggers immediately without waiting for next poll interval
- Tautulli movie detection: `{season_num}` sends `"0"` for movies (not empty) — detection now treats `0`/`"0"` as absent
- Tautulli: movie detection no longer requires `themoviedb_id` to be present; logs a clear warning if missing
- Tautulli: `{show_name}` is TV-only — added `plex_movie_title` field using `{title}` for movie title

### 🎬 Jellyfin Fixes (v3.4.1)
- Detection Method field now renders as a dropdown in setup UI (was rendering as a text input)
- Fixed critical bug in PlaybackProgress mode: episode was marked as processed *before* `process_episode()` ran, and a duplicate dedup check inside `process_episode()` caused it to immediately return `False` — subprocess never executed despite logs claiming success
- Dedup check now happens before the "In trigger range" log so duplicate ticks are silent at debug level
- Per-tick progress % log demoted to debug — no more log spam during playback
- `process_episode()` now logs Sonarr series lookup result, assigned rule, and full media_processor output on failure
- Sonarr series not found returns `False` immediately with a clear warning instead of silently continuing

### 🗝️ Misc
- `use :custom tag` — alternate URL for configs (HTTP can open in iframe, HTTPS always opens externally)
- Alternate URL added to quick link configs

---

### v3.4.0

🔐 **Auth**
- Webhook endpoints for Jellyfin, Emby, and Jellyseerr integrations are now exempt from authentication — external services no longer receive `401 Unauthorized` when `REQUIRE_AUTH=true`

🛠️ **Fixes**
- Episeerr starts cleanly even when Sonarr is offline — tag reconciliation and delay profile sync log a warning instead of erroring out
- `/api/series-stats` returns `503 Sonarr unavailable` (instead of crashing) when Sonarr is unreachable at startup or during operation
- Series and Rules pages show an empty list instead of a 500 error when Sonarr is down
- Config cleanup scheduler skips silently when Sonarr is unreachable, preventing accidental series removal from config

### v3.3.9
- **Fix** — `get_rule_for_series` phantom import removed from `media_processor.py`; replaced with inline lookup over `config['rules']` (function never existed, caused import error during webhook processing)

### v3.3.8
⚠️ **BREAKING CHANGES**
- **Webhook URLs Changed** - Update webhook configurations:
  - Jellyfin: `/jellyfin-webhook` → `/api/integration/jellyfin/webhook`
  - Emby: `/emby-webhook` → `/api/integration/emby/webhook`
  - Jellyseerr: `/seerr-webhook` → `/api/integration/seerr/webhook`

🏗️ **Integrations Refactor**
- Jellyfin, Emby, and Jellyseerr migrated to modular integration system (~1500 lines refactored)
- Self-contained integration modules with auto-discovery
- Easier maintenance and future extensibility

🔐 **Security**
- Optional password authentication
- Session-based login with configurable timeout (default 24 hours)
- Localhost bypass for admin access
- Environment variables: `REQUIRE_AUTH`, `AUTH_USERNAME`, `AUTH_PASSWORD`

🎨 **UI/UX**
- Reorganized System Links with clearer categories (Required Services, Media Servers, Optional Integrations, Custom Links)
- Docker integration now supports optional web UI URL
- All configured media servers shown (Plex, Jellyfin, Emby)

### v3.3.6 - 2026-02-25
- Sidebar rearranged and cleaned up
- Docker integration added — select compose/stack or all containers for sidebar display

### v3.3.5 - 2026-02-24
- Notifications: Added notification option if episodes released but not in library
- New Cyber Neon Theme

### v3.3.4 - 2026-02-22
- **Always Have** — new rule parameter with expression syntax for episodes that should always be present and protected from cleanup
  - Expression examples: `s1` (full season), `s1e1` (pilot), `s1, s*e1` (showcase), `s1-3` (season range)
  - Processes on rule assignment — monitors and searches matching episodes
  - Protected from Grace and Keep cleanup; only Dormant overrides it
- **Series page selection** — list icon on every poster (grid) and table row launches the selection flow
- **Plex integration** — watchlist sync, now-playing widget; TV shows → pending selection, movies → Radarr
- **Plex/Spotify now-playing widgets** on dashboard
- **Rule picker on selection page** — dropdown pre-selects current rule; Apply to reassign or pick episodes manually

### v3.3.3 - 2026-02-15
- **Jellyfin Detection Method Overhaul**: Changed default to Webhook-Triggered Polling
- **Emby Polling Improvements**: Increased default poll interval to 900s
- **Database Optimization**: Form handler saves only method-specific fields

### v3.3.2 - 2025-02-15
- Centralized logging with `LOG_LEVEL` env var support
- Log rotation (10MB max, 5 backups)
- Reduced log spam by ~90%

### v3.3.1 - 2026-02-06
- Cosmetic fixes; removed duplicate "recently downloaded" on dashboard

### v3.3.0 - 2026-02-05
- Plugin System (beta): Dashboard integrations with auto-discovery
- Initial integrations: Radarr, Sabnzbd, Prowlarr

### v3.2.0 - 2026-02-04
- Setup Page: Configure services via GUI at /setup
- Emby Support: Full webhook integration
- Quick Links: Auto-populated sidebar links
- Database Configuration: All settings stored in DB with .env fallback

### v3.1.3 - 2026-01-31
- Fixed mobile layout

### v3.1.2 - 2026-01-30
- Series Management in Rules Tab
- Fixed Duplicate Search Bug (removed `episeerr_default` tag system)
- Fixed UI layout (content behind sidebar)
- Enhanced README with setup guides

### v3.1.1 - 2026-01-XX
- Pilot Protection: Global and per-rule settings to always keep S01E01

### v3.1.0 - 2026-01-XX
- Dashboard Page with activity feed, stats cards, 7-day episode calendar
- Rules Management Page
- Download Tracking with green "Ready" badge

### v3.0.0 - 2026-01-25
- Tag System Overhaul: Removed `episeerr_default` tag; direct rule tags `episeerr_[rule_name]`
- Tag Drift Detection: Auto-corrects tag mismatches
- Auto-Cleanup: Deleted series removed from config automatically

### v2.9.8
- Auto-cleanup of deleted series from config
- Smart Jellyfin mode detection
- Webhook string/integer tag handling

### v2.9.7 - 2026-01-21
- Orphaned tag detection in watch webhooks
- Case-sensitivity fix in drift detection
- Jellyfin configuration detection fix

### v2.9.6 - 2026-01-20
- Rule descriptions (optional tooltip field)
- Orphaned tag detection on startup, cleanup, and Clean Config
- Enhanced drift detection in multiple locations
- Case-insensitive tag matching

### v2.9.5 - 2026-01-19
- Automatic tag system: rule-specific tags auto-created in Sonarr
- Tag migration: one-time bulk sync on first startup
- Drift detection: auto-corrects when tags manually changed in Sonarr
- Delay profile integration: Episeerr tags managed in Sonarr delay profiles

---

## [2.9.2] - 2026-01-18

### Documentation

**Major documentation restructure v2.0** - Complete reorganization for clarity and discoverability

#### Added
- **New folder structure**: Organized into getting-started, core-concepts, features, configuration, guides, troubleshooting, reference
- **[Deletion System Guide](docs/core-concepts/deletion-system.md)**: Comprehensive explanation of Keep vs Grace vs Dormant with bookmark system
- **[Tags & Auto-Assign Guide](docs/core-concepts/tags-and-auto-assign.md)**: Clear explanation of `episeerr_default` and `episeerr_select` tag behavior
- **[Quick Start Guide](docs/getting-started/quick-start.md)**: Get running in 5 minutes
- **[First Series Tutorial](docs/getting-started/first-series.md)**: Step-by-step walkthrough for beginners
- **[Rules Explained](docs/core-concepts/rules-explained.md)**: Conceptual guide to GET/KEEP/Action settings
- **[Webhooks Explained](docs/core-concepts/webhooks-explained.md)**: Why webhooks exist and how they work
- **Learning paths**: Different guides for different user types (new users, power users, specific features)

#### Changed
- **Main README.md**: Streamlined with better navigation and links to full documentation
- **Documentation index**: `docs/README.md` now serves as comprehensive navigation hub
- **File organization**: Moved all docs into logical folders with clear naming
- **Content consolidation**: One authoritative source per topic, eliminated duplication

#### Fixed
- **Tag confusion**: Clearly documented that tags are temporary signals, not permanent labels
- **Deletion confusion**: Comprehensive guide explains bookmark system and when deletions happen
- **"Tag disappeared" issue**: Documentation now explains this is normal and expected behavior

#### Improved
- Clear separation between concepts and how-to guides
- Better cross-linking between related topics
- Consistent formatting across all documentation
- Improved discoverability with table of contents and quick links

### Bug Fixes
- Fixed duplicate dry run check in `delete_episodes_in_sonarr_with_logging()`

### Repository Cleanup
- Removed `data/activity/` files from git tracking
- Updated `.gitignore` to exclude data, logs, config, and temp folders
- Cleaned up accidentally committed runtime data

## [2.9.1] - 2026-01-17
### Changed - BREAKING
- **Grace Periods reimagined as Bookmark System** for inactive shows
  - Grace Watched: Keeps last watched episode as reference point, deletes older watched episodes
  - Grace Unwatched: Keeps first unwatched episode as bookmark, deletes extra unwatched episodes
  - Result: Maximum 2 episodes per inactive show (last watched + next unwatched)
  - Grace cleanup no longer applies Get rule - Get rule only runs on watch webhooks

### Added
- **grace_cleaned flag**: Prevents repeated processing of already-bookmarked shows
- **Grab webhook support**: Series marked as cleaned when new episodes grabbed
- **Smart cleanup loop**: Shows missing next episodes stay in cleanup until grab webhook fires
- **Master safety switch**: Global dry_run_mode always overrides rule-level settings
- **Sonarr Grab event handling**: Stops checking loop for incomplete series when content becomes available

### Fixed
- Grace cleanup no longer re-downloads same episodes every 5 days
- Incomplete series properly handled - keeps checking until content available
- Master dry_run_mode setting now cannot be overridden by individual rules
- Storage savings: Inactive shows reduced to 1-2 episodes instead of full seasons

### Technical Details
**Grace Bookmark Philosophy:**
- Watched: Delete all except last watched (keeps reference point for catch-up)
- Unwatched: Delete all except first unwatched (keeps bookmark for resume)
- Ignores Keep/Get rules during cleanup (those only apply during active watching)
- Marks series as "cleaned" when bookmarks established
- Stops processing until activity resumes

**Cleanup Loop Behavior:**
- Has bookmark → Mark grace_cleaned=true → Skip on future cleanups
- Missing bookmark → grace_cleaned=false → Check every cleanup cycle
- Watch webhook → Clear grace_cleaned=false → Re-enter grace period after inactivity
- Grab webhook → Set grace_cleaned=true → Exit checking loop

**Webhooks Required:**
- Watch webhook (Tautulli/Jellyfin): Updates activity_date, clears grace_cleaned flag
- Grab webhook (Sonarr): Sets grace_cleaned flag to stop checking loop

**Master Safety:**
- Global dry_run_mode=true: ALL deletions forced to pending queue regardless of rule settings
- Rule dry_run=true: That specific rule uses pending queue  
- Rule dry_run=false + Global=false: Direct deletion allowed

### Migration Notes
- Configure Sonarr webhook to include "On Grab" event in addition to "On Series Add"
- grace_cleaned field will be added automatically to config.json as series are processed
- Highly recommended: Set dry_run_mode: true in global settings during initial testing
- After grace runs: Each show has 1-2 episodes (reference + bookmark)
- Missing episodes: Series stays in cleanup loop until Sonarr grabs the content

### Example Scenarios
**Complete Series (Countdown):**
```
Initial: S1E1-S1E13 (13 episodes)
After Grace Cleanup:
  - S1E1 (last watched - your reference point)
  - S1E2 (first unwatched - your bookmark)
  - grace_cleaned = true
Result: Won't process again until you watch S1E2
Space Saved: 11 episodes
```

**Incomplete Series (Stranger Things):**
```
Initial: S5E1-S5E4 watched, no S5E5 available yet
After Grace Cleanup:
  - S5E4 (last watched - your reference point)
  - No unwatched exists
  - grace_cleaned = false
Result: Checks every cleanup cycle
When S5E5 grabbed: grace_cleaned = true, stops checking
Space Saved: 3 episodes
```

**Active Series Returns:**
```
You watch S1E2 (webhook fires)
  → grace_cleaned = false
  → Get rule applies: Gets S1E3
  → After 5 days inactive: Grace cleanup runs again
  → Bookmarks to S1E2 + S1E3
  → grace_cleaned = true
```

## [2.9.0] - 2026-01-16

### Changed - BREAKING
- **Grace Periods now override Keep/Get settings** for maximum flexibility
  - Grace Watched: Deletes ALL watched episodes after inactivity (keeps position in config only)
  - Grace Unwatched: Deletes unwatched episodes after deadline (keeps next episode file as resume point)
  - Both preserve your position so you can resume anytime

### Added
- **Position preservation**: Grace Watched keeps last_season/last_episode in config (no file kept)
- **Resume point file**: Grace Unwatched keeps the next unwatched episode file on disk
- **Auto-resume for mid-season breaks**: Grace Watched simulates watch event during cleanup to check for new episodes
- **Smart cleanup**: When cleanup runs, checks if new episodes exist and monitors them automatically

### Fixed
- Grace periods now truly independent of Keep/Get settings
- Mid-season breaks no longer lose your position
- Can resume shows months later without manual intervention

### Technical Details
- Grace Watched: Deletes all watched files, simulates last watched episode during cleanup
- Grace Unwatched: Sorts unwatched episodes, keeps first one, deletes rest
- Position data persists in config even when episode files are deleted
- Cleanup simulation triggers Get rule to check for new episodes in Sonarr

### Migration Notes
- Existing grace periods will use new behavior automatically
- No config changes needed
- Test with dry run first to see new deletion patterns
- After grace cleanup: watched files deleted (position kept), 1 unwatched file remains

## [2.8.0] - 2026-01-15

### Added
- **Pending Deletions System**: Comprehensive approval queue for all cleanup operations
  - All deletions now queue for review before execution when dry run is enabled
  - Hierarchical organization: Series → Season → Episode
  - Multiple approval levels: individual episodes, entire seasons, whole series, or bulk selection
  - Rejection cache: Rejected episodes won't reappear for 30 days
  - Enhanced logging with detailed deletion reports showing:
    - Reason for deletion (Grace Period, Keep Rule, Dormant Series, etc.)
    - Data source (Tautulli watch history vs Sonarr air date)
    - Date value used in decision
    - Rule name that triggered the deletion
    - File size to be reclaimed
  - Real-time notification badge showing combined pending requests + pending deletions
  - New `/pending-deletions` page with collapsible series/season view
  - API endpoints for programmatic access to pending deletions queue

### Changed
- **Dry Run Mode now enabled by default** for all new installations
  - Provides safety net against accidental mass deletions
  - Existing installations require manual migration (add `dry_run_mode: true` to `global_settings.json`)
  - Can be configured globally (Admin → Scheduler) or per-rule (Admin → Dry Run Settings)
- **Navigation updated**: "Pending Requests" renamed to "Pending" 
  - Now serves as unified entry point for both episode selection requests and pending deletions
  - Badge shows combined count of both pending items
- **Unified Pending Page**: Enhanced to show both episode selection requests and deletion queue
  - Tab-free design with separate cards for each type
  - Summary cards show quick stats and link to detailed views

### Fixed
- Improved deletion logic to prevent edge cases in multi-viewer households
- Enhanced error handling in cleanup operations
- Better context tracking for why episodes are targeted for deletion

### Technical Details
- New `pending_deletions.py` module for queue management
- New `pending_deletions.html` template with Bootstrap accordion interface
- Storage-efficient rejection cache using episode ID + expiry date pairs
- Migration system auto-adds `dry_run_mode` field to existing `global_settings.json`
- Backward compatible - existing cleanup behavior unchanged if dry run disabled

### Migration Notes
- **Existing users**: Dry run mode is NOT enabled automatically
  - To enable: Admin → Scheduler → Check "Global Dry Run Mode" → Save
  - Or manually add `"dry_run_mode": true` to `config/global_settings.json`
- **New users**: Dry run enabled by default, deletions require approval
- All pending deletions data stored in `data/pending_deletions.json`
- Rejection cache stored in `data/deletion_rejections.json`

### Documentation
- Added comprehensive "Pending Deletions" section to built-in documentation
- Updated FAQ with 5 new entries about the approval system
- Added troubleshooting guides for common pending deletion scenarios
- Updated installation guides to mention dry run default behavior
Version 2.7.9 - Season Pack Preference

### Features
- **Season Pack Preference**: Rules using "Get X seasons" now automatically prefer season packs over individual episodes when searching
  - Sonarr searches for season packs first, falls back to individual episodes if no pack available
  - Applies to both watched episodes (webhooks) and newly added series (Jellyseerr)
  - Episode-based rules continue to work exactly as before

### Fixes
- Fixed documentation: Tautulli webhook URL is `/webhook` (not `/tautulli-webhook`)

### Technical Details
- When `get_type: 'seasons'`, uses Sonarr's `SeasonSearch` command instead of `EpisodeSearch`
- Backward compatible - all existing rules continue to work unchanged

v2.7.8 
Cosmetic fixes
v2.7.7
Removed disk stats because its in ADMIN
Cleaner interface with 3 cards
🎬 Last Requested - Displays most recent series request from Jellyseerr/Overseerr with poster
🔍 Last Searched - Shows last episode search triggered by Episeerr with series poster
📺 Last Watched - Displays most recently watched episode with poster and user info

v2.7.6 is a pure performance/bug fix release:
🎯 One Issue Fixed:

Stopped backing up config files on every read (was happening 100+ times/minute)
Now only backs up when actually saving changes

📉 Impact:

Cleaner logs
Less disk I/O
Same backup protection

This is a "quality of life" release - no new features, just makes the existing system run cleaner! 🚀
Cards will show "No recent activity" until first events occur - this is normal!

Version 2.7.5 - Activity Dashboard Cards
🎨 New Features
Visual Activity Cards

Replaced static statistics cards with dynamic visual activity cards showing recent user activity
Four cards on dashboard:

💾 Disk Usage - Shows current storage usage with link to Sonarr system status
🎬 Last Requested - Displays most recent series request from Jellyseerr/Overseerr with poster
🔍 Last Searched - Shows last episode search triggered by Episeerr with series poster
📺 Last Watched - Displays most recently watched episode with poster and user info


Interactive Cards

All cards are clickable and link to their respective services:

Disk Usage → Sonarr System Status
Last Requested → Jellyseerr/Overseerr
Last Searched → Sonarr Activity Queue
Last Watched → Jellyfin/Tautulli/Plex


Hover effects provide visual feedback
Cards auto-refresh every 60 seconds

Activity Tracking System

New activity storage module (activity_storage.py) logs:

Watch events (series, season, episode, user, timestamp)
Search events (series, season, episode, timestamp)
Request events (title, TMDB ID, timestamp)


Activity data stored in /data/activity/:

watched.json - Last 10 watch events
searches.json - Last 10 search events
last_request.json - Most recent request


Persistent across restarts - activity history preserved

🔧 Technical Improvements
Poster Display

Cards display series posters fetched from:

Sonarr media cover API (watched/searched)
TMDB API (requested)


Automatic fallback to placeholder for missing posters
Optimized poster resolution (w200) for fast loading

Backend Enhancements

Added get_episode_details_by_id() function to fetch episode metadata
Request webhook now fetches TMDB poster path at request time
Activity logging integrated into existing webhook workflows
Watch events logged when episodes are marked as watched
Search events logged when Episeerr triggers Sonarr searches

Time Display

Human-readable timestamps: "5 minutes ago", "2 hours ago", "3 days ago"
Real-time updates as cards refresh

🐛 Bug Fixes

Fixed search event logging crash due to missing function
Fixed poster URL construction for TMDB requests
Improved error handling for missing activity data
Added graceful fallbacks when services are unconfigured

📝 Configuration

No new environment variables required
Uses existing service URLs (SONARR, JELLYSEERR, JELLYFIN, etc.)
Activity tracking works automatically with existing webhooks

💾 Data Storage

Activity data stored in persistent /data volume
Automatic directory creation on first use
No database required - simple JSON file storage
Configurable retention (default: last 10 events for watches/searches)

🎯 User Experience

At-a-glance visibility of recent activity
Quick access to services via clickable cards
Visual context with series posters
No configuration needed - works out of the box

🔄 Backwards Compatibility

Fully compatible with existing Episeerr installations
Old stat cards replaced seamlessly
No breaking changes to existing functionality
Works with all supported media servers (Jellyfin, Plex, Tautulli)

📊 Performance

Lightweight JSON storage
Minimal API calls (only when events occur)
Efficient poster caching via browser
Auto-cleanup of old events (keeps last 10)


Migration Notes

Activity tracking begins immediately after update
Previous activity (before update) will not be shown
Cards will show "No recent activity" until first events occur
No action required from users

Known Limitations

Activity history limited to last 10 events per type
Poster quality optimized for dashboard display (not full resolution)
Request poster requires TMDB API key (already required for Episeerr)

[2.7.4] - 2026-01-06
### Added
- Automatic backup system for configuration files - creates `config.json.bak` and `global_settings.json.bak` on every load
- Quick Links dropdown in navbar for quick access to Sonarr, Plex, and Jellyfin (moved from sidebar)

### Changed
- Moved Quick Links from sidebar card to navbar dropdown to save vertical space on rules index page
- Quick Links now populate from environment variables (SONARR_URL, PLEX_URL, JELLYFIN_URL)

[2.7.3] - 2026-01-05

    fixed duplicate discord notifications 

[2.7.2] - 2026-01-04

    Added 3 types of progress tracking using jellyfin, see documentation
    updated documentation

[2.7.1] - 2026-01-03
Added

    Discord webhook notifications for episode search status
        "Search Pending" notifications when episodes are requested
        Auto-delete notifications when Sonarr successfully grabs episodes
        "Selection Pending" notifications for episeerr_select tagged shows
    Notification storage system for tracking pending Discord messages
    Sonarr "Grab" webhook support for notification cleanup

Changed

    Sonarr webhook handler now processes both SeriesAdd and Grab events
    Streamlined notification approach - only shows failed/pending searches

Fixed

    Jellyfin webhook crash: stop_jellyfin_polling() function signature mismatch
        Function now accepts optional episode_info parameter
        Fixes "takes 1 positional argument but 2 were given" error
        Resolves automation breaking for Jellyfin users on PlaybackStop events

[2.7.0] - 2026-01-02
Added

    Comprehensive documentation page accessible from UI
    CHANGELOG.md for version tracking
    Automated CHANGELOG updates in release script
    Navigation links to docs in navbar and admin panel

Changed

    Cleaned up redundant help text from create/edit rule pages
    Removed large "How It Works" section from admin panel
    Streamlined UI with contextual help links to full docs

Fixed

    HTML layout issues in create_rule template

Added

    Per-season grace period tracking for multi-viewer households
    Grace period scope setting (per-series vs per-season) in rule editor
    Web-based log viewer with filtering, search, and download capabilities
    Auto-submit dropdowns in log viewer (instant filtering)
    Log rotation optimized for mobile viewing (2-5 MB files)
    "Clear Old Logs" feature to remove rotated logs older than 7 days

Changed

    Log rotation limits increased for better history retention
        app.log: 1MB → 2MB
        cleanup.log: 5MB → 2MB
        episeerr.log: 10MB → 5MB
    "View Cleanup Logs" button now opens advanced log viewer
    Migration system auto-adds grace_scope='series' to existing rules

Fixed

    Jellyseerr request files now properly deleted when no episeerr tags are used
    Per-season activity tracking correctly updates season-specific timestamps
    Grace period cleanup functions support both per-series and per-season modes

[2.6.5]
Added

    Sonarr webhook bug fix for Jellyseerr integration
    Improved Jellyseerr request file cleanup
    Better handling of series without episeerr tags

Fixed

    Jellyseerr cleanup moved to top of webhook processing
    Request files no longer orphaned when tags aren't used
    episeerr_default tag processing more reliable


---

## [2.6.0] 

### Added
- Initial stable release
- Episode selection system with multi-season support
- Viewing-based automation for episode management
- Time-based cleanup with dual timer system (grace watched/unwatched, dormant)
- Rule-based automation for different show types
- Webhook integration for Tautulli, Jellyfin, and Sonarr
- Tag-based workflow with Jellyseerr/Overseerr integration
- Scheduler admin panel for monitoring and control
- Global storage gate for cleanup threshold management
- Dry run mode for testing cleanup rules
- Multi-architecture Docker support (amd64, arm64, arm/v7)

### Key Features
- **Three Independent Solutions:**
  1. Manual episode selection across multiple seasons
  2. Viewing-based automation with webhook processing
  3. Time-based cleanup with configurable grace periods

- **Rule System:**
  - Get X episodes/seasons automatically
  - Keep Y episodes/seasons after watching
  - Grace periods for watched/unwatched content
  - Dormant timer for abandoned shows

- **Integration:**
  - Sonarr tag-based workflows (episeerr_default, episeerr_select)
  - Jellyseerr/Overseerr request handling
  - Tautulli/Jellyfin webhook support
  - TMDB API for series metadata

---

## Version Format

- **Stable releases:** `X.Y.Z` (e.g., 2.6.5)
- **Beta releases:** `X.Y.Z-beta.N` or `beta-X.Y.Z` (e.g., 2.6.6-beta.1)
- **Release candidates:** `X.Y.Z-rc.N` (e.g., 2.6.6-rc.1)

---

## Categories

- **Added** - New features
- **Changed** - Changes to existing functionality
- **Deprecated** - Soon-to-be removed features
- **Removed** - Removed features
- **Fixed** - Bug fixes
- **Security** - Vulnerability fixes

---

## Links

- [GitHub Repository](https://github.com/vansmak/episeerr)
- [Docker Hub](https://hub.docker.com/r/vansmak/episeerr)
- [Documentation](https://github.com/vansmak/episeerr#readme)