# Changelog

## [Unreleased / Dev]

### New Features
- **Series page selection** — list icon on every poster (grid) and table row launches the selection flow for any existing series; grab specific seasons/episodes or just change the rule without touching Sonarr tags
- **Plex integration** — watchlist sync, now-playing widget
  - TV shows added to Plex watchlist create a pending selection request
  - Movies go straight to Radarr
  - Optional movie cleanup: delete from Radarr after watched + grace period
- **Plex/Spotify now-playing widgets** on dashboard
- **Rule picker on selection page** — dropdown pre-selects the show's current rule; Apply to reassign only (no processing), or pick episodes manually and still assign a rule for ongoing management
- **Plex token helper script** (`get_plex_token.py`)

### Fixes
- Rule reassignment now removes series from old rule before adding to new one
- Duplicate pending request check — reuses existing request if series already queued
- Cancel on selection page deletes the pending request before navigating back
- Stat pills unassigned count no longer goes negative when stale config entries exist for deleted series
- After selection flow, lands on Rules page instead of index

## [Released]
v3.3.1 - 2025-02-06
cosmetic fixes
removed duplicate recently downloaded on dashboard

v3.3.0 - 2025-02-05
🔌 Plugin System **consider this feature beta***

Dashboard Integrations: Connect additional services to display stats on dashboard

Auto-discovery system - drop in integration file and restart
Integrated setup in Setup page with connection testing
Automatic quick links generation when services are configured


Initial Integrations: Radarr Sabnzbd and Prowlarr support included
Extensible: Template provided for creating custom integrations (integrations/_INTEGRATION_TEMPLATE.py)

✨ Improvements

Simplified setup flow - configure services through web UI with instant validation
Dashboard pills auto-generate from configured integrations
Quick links automatically appear in sidebar when services are connected

📝 Setup Notes

First-time setup: Restart container after configuring services for changes to take effect
Configuration persists across restarts once set
Existing configurations remain compatible

🔧 Technical

Plugin architecture with base class and standardized methods
Metadata-driven UI generation (no template editing required)
Database storage for all service configurations

v3.2.0 - 2025-02-04
🎉 New Features

Setup Page: Configure services via GUI at /setup - no more manual .env editing
Emby Support: Full webhook integration for viewing automation (joins Jellyfin, Plex/Tautulli)
Quick Links: Auto-populated sidebar links to configured services

✨ Improvements

Database Configuration: All service settings stored in database with .env fallback for backward compatibility
No .env Required: Fresh installations can configure everything through the web UI
Graceful Degradation: Dashboard adapts when services aren't configured - no crashes

🔧 Technical

Helper functions return empty strings instead of None when services unconfigured
API routes check service availability before making requests
Setup page validates URLs and API keys before saving

🔄 Migration

Existing .env files continue to work - no breaking changes
Database takes priority over .env when both present
Use /setup page to migrate from .env to database configuration

## [3.1.3] - 2026-01-31
fixed mobile layout
## [3.1.2] - 2026-01-30

### New Features
- **Series Management in Rules Tab:** Series table now available directly in Rules view for easier assignment

### Bug Fixes
- **Duplicate Search Bug:** Fixed by removing `episeerr_default` tag system
  - Series no longer trigger duplicate searches in Sonarr
  - Tag system simplified to direct rule tags only
- **UI Layout:** Fixed content being hidden behind sidebar at bottom of pages
  - Updated `.content-container` width calculation to account for sidebar
  - Mobile layout properly handles full width
- **Pending Deletions:** Fixed approval button error (imported wrong function)

### Documentation
- **Removed episeerr_default tag system:**
  - Updated in-app documentation to reflect direct rule tags
  - Removed "How Episeerr Works" section from home page
  - Clarified direct rule tags (`episeerr_[rule_name]`) vs auto-assign
- **Enhanced README.md:**
  - Comprehensive setup instructions with webhook guides
  - Detailed troubleshooting section
  - FAQ covering common questions
  - Screenshot placeholders for visual guides

### Technical
- **Delay Profile Simplified:** Now contains only 2 tags:
  - `episeerr_select` - Episode selection
  - `episeerr_delay` - Temporary processing block
- Tag system overhaul eliminates confusion and duplicate processing

---

## [3.1.1] - 2026-01-XX

### New Features
- **Pilot Protection:** Global and per-rule settings to always keep S01E01
  - Global setting applies to all series
  - Per-rule override for specific show types
  - Pilot episodes excluded from deletion regardless of Keep/Grace rules

### Minor Changes & Fixes
- Various UI improvements and bug fixes

---

## [3.1.0] - 2026-01-XX

### New Features
- **Dashboard Page:** Comprehensive dashboard with activity feed, stats cards, and 7-day episode calendar
  - Shows upcoming episodes from Sonarr
  - Displays recently downloaded episodes
  - Activity feed for recent actions
- **Rules Management Page:** Dedicated rules page with responsive design
  - List view for desktop
  - Card layout for mobile
  - Improved rule organization
- **Download Tracking:** Recently grabbed episodes display with green "Ready" badge
  - 7-day rolling window
  - Integrated into dashboard calendar
  - Stored in `data/recent_downloads.json` with auto-cleanup

### UI Improvements
- **Series Management Redesign:**
  - Grid view for browsing with poster cards
  - Manage (table) view for bulk operations
  - Toggle between views
- **Sidebar Navigation:**
  - Streamlined with collapsible sections
  - Removed individual rule listings for cleaner interface
  - Improved mobile responsiveness
- **Mobile Responsive:** Rules page adapts to card layout on mobile devices

### Bug Fixes
- **Scheduler Admin Page:** Fixed template block issues preventing page load
- **Edit Rule:** Corrected routing - now properly loads edit form instead of dashboard
- **Rules CRUD:** Fixed all redirect issues
  - Create/edit/delete operations stay within rules workflow
  - No more unexpected dashboard redirects
- **Orphaned Routes:** Removed duplicate/orphaned route decorators causing conflicts

### Technical
- Enhanced grab webhook handler with unified logging
- Calendar API merges Sonarr upcoming episodes with recent downloads
- Download tracking database with automatic cleanup

---

## [3.0.0] - 2026-01-25

### Breaking Changes
- **Tag System Overhaul:**
  - Removed `episeerr_default` tag
  - Direct rule tags now use format: `episeerr_[rule_name]`
  - Auto-assign setting separated from tag system
  - Delay profile simplified to 2 control tags only

### New Features
- **Tag Drift Detection:** Automatic detection and correction of tag mismatches
  - Checks on watch, cleanup, startup, and manual "Clean Config"
  - Series automatically moved to matching rule when tag changes in Sonarr
  - Orphaned tags detected and series auto-assigned
- **Auto-Cleanup:** Deleted series automatically removed from config
  - Runs on startup and during scheduled cleanup
  - No manual config editing needed

### Improvements
- **Jellyfin Integration:**
  - Smart mode detection (no manual disable needed)
  - Auto-detects Real-time, Polling, or On-stop based on env vars
  - Improved webhook handling
- **Webhook Processing:**
  - Better handling of string/integer tag IDs
  - 404 errors logged as DEBUG (reduced noise)
  - Improved error messages

### Bug Fixes
- Fixed pending deletions approval error (wrong function imported)
- Corrected episeerr.py tag removal logic
- Rules page server error fixed (added series data loading)

---

## [2.9.8] - 2025-XX-XX

### Features
- Auto-cleanup of deleted series from config
- Smart Jellyfin mode detection
- Webhook string/integer tag handling
- 404 errors logged as DEBUG
- Delay profile simplified to 3 tags

---

*For older versions, see [legacy changelog](CHANGELOG_OLD.md)*
Key additions in 3.1.2:

✅ UI layout fix (content behind sidebar)
✅ Documentation cleanup (episeerr_default removed)
✅ Enhanced README with setup guides

Want me to add anything else? 🎯delay profile just has delay and select tag not 3.  i already added the series mgmnt to rules. eliminating default tag solved to dupe search bug as well10:59 AMGot it! Let me fix those:
markdown# Changelog

## [Unreleased]

### Planned
- Additional UI improvements

---

## [Released]

## [3.1.2] - 2026-01-30

### New Features
- **Series Management in Rules Tab:** Series table now available directly in Rules view for easier assignment

### Bug Fixes
- **Duplicate Search Bug:** Fixed by removing `episeerr_default` tag system
  - Series no longer trigger duplicate searches in Sonarr
  - Tag system simplified to direct rule tags only
- **UI Layout:** Fixed content being hidden behind sidebar at bottom of pages
  - Updated `.content-container` width calculation to account for sidebar
  - Mobile layout properly handles full width
- **Pending Deletions:** Fixed approval button error (imported wrong function)

### Documentation
- **Removed episeerr_default tag system:**
  - Updated in-app documentation to reflect direct rule tags
  - Removed "How Episeerr Works" section from home page
  - Clarified direct rule tags (`episeerr_[rule_name]`) vs auto-assign
- **Enhanced README.md:**
  - Comprehensive setup instructions with webhook guides
  - Detailed troubleshooting section
  - FAQ covering common questions
  - Screenshot placeholders for visual guides

### Technical
- **Delay Profile Simplified:** Now contains only 2 tags:
  - `episeerr_select` - Episode selection
  - `episeerr_delay` - Temporary processing block
- Tag system overhaul eliminates confusion and duplicate processing

---

## [3.1.1] - 2026-01-XX

### New Features
- **Pilot Protection:** Global and per-rule settings to always keep S01E01
  - Global setting applies to all series
  - Per-rule override for specific show types
  - Pilot episodes excluded from deletion regardless of Keep/Grace rules

### Minor Changes & Fixes
- Various UI improvements and bug fixes

---

## [3.1.0] - 2026-01-XX

### New Features
- **Dashboard Page:** Comprehensive dashboard with activity feed, stats cards, and 7-day episode calendar
  - Shows upcoming episodes from Sonarr
  - Displays recently downloaded episodes
  - Activity feed for recent actions
- **Rules Management Page:** Dedicated rules page with responsive design
  - List view for desktop
  - Card layout for mobile
  - Improved rule organization
- **Download Tracking:** Recently grabbed episodes display with green "Ready" badge
  - 7-day rolling window
  - Integrated into dashboard calendar
  - Stored in `data/recent_downloads.json` with auto-cleanup

### UI Improvements
- **Series Management Redesign:**
  - Grid view for browsing with poster cards
  - Manage (table) view for bulk operations
  - Toggle between views
- **Sidebar Navigation:**
  - Streamlined with collapsible sections
  - Removed individual rule listings for cleaner interface
  - Improved mobile responsiveness
- **Mobile Responsive:** Rules page adapts to card layout on mobile devices

### Bug Fixes
- **Scheduler Admin Page:** Fixed template block issues preventing page load
- **Edit Rule:** Corrected routing - now properly loads edit form instead of dashboard
- **Rules CRUD:** Fixed all redirect issues
  - Create/edit/delete operations stay within rules workflow
  - No more unexpected dashboard redirects
- **Orphaned Routes:** Removed duplicate/orphaned route decorators causing conflicts

### Technical
- Enhanced grab webhook handler with unified logging
- Calendar API merges Sonarr upcoming episodes with recent downloads
- Download tracking database with automatic cleanup

---

## [3.0.0] - 2026-01-25

### Breaking Changes
- **Tag System Overhaul:**
  - Removed `episeerr_default` tag
  - Direct rule tags now use format: `episeerr_[rule_name]`
  - Auto-assign setting separated from tag system
  - Delay profile simplified to 2 control tags only

### New Features
- **Tag Drift Detection:** Automatic detection and correction of tag mismatches
  - Checks on watch, cleanup, startup, and manual "Clean Config"
  - Series automatically moved to matching rule when tag changes in Sonarr
  - Orphaned tags detected and series auto-assigned
- **Auto-Cleanup:** Deleted series automatically removed from config
  - Runs on startup and during scheduled cleanup
  - No manual config editing needed

### Improvements
- **Jellyfin Integration:**
  - Smart mode detection (no manual disable needed)
  - Auto-detects Real-time, Polling, or On-stop based on env vars
  - Improved webhook handling
- **Webhook Processing:**
  - Better handling of string/integer tag IDs
  - 404 errors logged as DEBUG (reduced noise)
  - Improved error messages

### Bug Fixes
- Fixed pending deletions approval error (wrong function imported)
- Corrected episeerr.py tag removal logic
- Rules page server error fixed (added series data loading)

---

## [2.9.8] - 2026-01-23

### Added
- **Auto-cleanup of deleted series**: Series removed from Sonarr are automatically cleaned from config during startup/drift detection
- **Smart Jellyfin mode detection**: Auto-detects Option 1/2/3 from environment variables - no manual `JELLYFIN_DISABLE_ACTIVE_POLLING` needed
- **Webhook string/integer tag handling**: SeriesAdd webhooks now handle both tag labels (strings) and IDs (integers) via reverse mapping
- **Enhanced documentation**: Dark mode support, reorganized sections (Examples under Rules, FAQ under Troubleshooting), improved navigation

### Changed
- **Delay profile simplification**: Now contains only 3 control tags (default, select, delay) - rule tags no longer added
- **404 error handling**: Series not found (404) logged as DEBUG instead of ERROR - reduces log noise
- **Log clarity improvements**: "already processed (no control tags)" replaces "no episeerr tags" for clearer messaging
- **Grace cleanup loop**: Series marked as `grace_cleaned` only re-enters loop after watch activity

### Fixed
- **Tag removal in webhooks**: Now correctly converts tag labels to IDs before API calls - prevents "tag not found" errors
- **Series safety check**: Always fetches full series object before updates to prevent race conditions
- **Jellyfin polling in Option 1**: Systems with TRIGGER_MIN/MAX and no POLL_INTERVAL no longer run unnecessary polling

### Technical Details
- Tag mapping: Maintains bidirectional label↔ID mapping for webhook compatibility
- Cleanup flags: `grace_cleaned` flag prevents redundant daily checks until next watch event
- 404 handling: Graceful removal from config with summary count in reconciliation logs
- Jellyfin detection: Inspects env vars to determine mode without manual override

### Migration Notes
- **Automatic**: No user action required
- **Sonarr delay profile**: May show extra rule tags briefly - these will be cleaned on next startup
- **Jellyfin Option 1 users**: Polling auto-disables after container restart
- **Documentation updates**: Restart container to see updated UI documentation

[2.9.7] - 2026-01-21
Added

Orphaned tag detection in watch webhooks: Series with episeerr tags but not in config are automatically added when watched

Enables fully tag-based workflow - tag shows in Sonarr, watch episodes, Episeerr picks them up
Complements existing startup/cleanup orphaned detection



Fixed

Case-sensitivity in drift detection: move_series_in_config() now uses case-insensitive rule lookups

Prevents errors when tag case doesn't match config rule case
Completes case-insensitive tag matching from 2.9.6


Jellyfin configuration detection: Moved load_dotenv() before environment variable reads in media_processor

Fixes "Jellyfin not configured" error when env vars are actually set
Ensures active polling starts correctly


Sonarr webhook rule assignment: New series with episeerr tags now use case-insensitive rule matching

Tag episeerr_One_At_A_Time correctly matches rule one_at_a_time



Technical Details

Watch webhooks now perform full tag reconciliation (drift + orphaned detection)
All rule name comparisons system-wide are now case-insensitive
Environment variables load before being accessed in media_processor.py

Migration Notes

Fully automatic - no user action required
Users with Jellyfin will need to restart for configuration fix to take effect

## [2.9.6] - 2026-01-20

### Added
- **Rule descriptions**: Optional description field for rules to document their purpose
  - Appears as tooltip when hovering over rule name
  - Helps organize and understand multiple rules
- **Orphaned tag detection**: Finds shows with episeerr tags but not in config, adds them automatically
  - Runs on startup
  - Runs during cleanup cycles  
  - Runs when "Clean Config" button clicked
  - Allows tag-based management entirely through Sonarr
- **Enhanced drift detection**: Now runs in multiple locations
  - Detects during watch webhooks (existing)
  - Detects during startup (new)
  - Detects during cleanup cycles (new)
  - Detects during "Clean Config" button (new)

### Changed
- **Case-insensitive tag matching**: Sonarr lowercases all tags internally
  - All tag comparisons now case-insensitive
  - Prevents false drift detection from case differences
  - Rule names can use any case (Get1keepseason vs get1keepseason)

### Fixed
- **False drift detection** from Sonarr's tag lowercasing behavior
- **Case-sensitivity bugs** in drift detection and rule lookups
- **Description field** now saves properly in create/edit rule forms
- **Notification import** errors in webhook processing

### Technical Details
- Comprehensive tag reconciliation runs on startup (create, migrate, drift, orphaned in single pass)
- Orphaned tag detection integrated into cleanup cycles
- All rule name lookups now case-insensitive
- Description field added to rule schema (optional, max 200 chars recommended)

### Migration Notes
- Fully automatic - no user action required
- Rule descriptions will be blank for existing rules (optional field)
- Orphaned shows will be automatically discovered and added to matching rules
- Case variations in rule names will be handled transparently

## [2.9.5] - 2026-01-19

### Added
- **Automatic tag system**: Rule-specific tags (`episeerr_<rulename>`) auto-created in Sonarr
- **Tag migration**: One-time bulk sync applies tags to all existing series on first startup
- **Drift detection**: Auto-corrects when tags manually changed in Sonarr
  - Detects tag changes during watch webhooks (Jellyfin/Tautulli)
  - Moves series to correct rule in config automatically
  - Syncs missing tags back to Sonarr
- **Delay profile integration**: Automatically manages episeerr tags in Sonarr delay profiles
  - Finds existing custom delay profile with episeerr tags
  - Adds/removes rule tags when rules created/deleted
  - Always preserves `episeerr_default` and `episeerr_select` tags

### Changed
- Rule creation/deletion now manages tags automatically in Sonarr and delay profiles
- Tags removed from series and Sonarr when rules deleted

### Fixed
- Config parameter passing in delay profile functions
- Import statements for tag functions across modules

### Technical Details
- Tag functions centralized in `episeerr_utils.py`
- Drift detection in `media_processor.py` webhook processing
- Migration controlled by `tag_migration_complete` flag in config
- Delay profile managed via `delay_profile_migrated` flag

### Migration Notes
- Fully automatic - no user action required
- First startup: creates all rule tags, syncs to series, updates delay profile
- Existing delay profile with episeerr tags will be detected and used
- Tags in Sonarr will match rules in Episeerr config automatically

*For older versions, see [legacy changelog](CHANGELOG_OLD.md)*

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
