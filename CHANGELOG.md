# Changelog

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
