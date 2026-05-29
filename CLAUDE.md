# Episeerr Custom — Claude Code Project Context

## What This Is

Joe's personal production build of Episeerr. Includes everything in `dev` plus the Arvio TV app integration. Running as Docker container `episeerr` on port 5002.

## Branches

| Branch | Directory | Purpose |
|--------|-----------|---------|
| `custom` | `~/projects/episeerr_custom` | **This** — production, includes Arvio |
| `dev` | `~/projects/episeerr_dev` | Upstream features, no Arvio |
| `main` | merged from dev | Public Docker Hub releases |

General improvements go to both `dev` and `custom`. Arvio-specific code stays in `custom` only.

## Deploy

```bash
# Single file (survives docker restart, not container recreate)
docker cp ~/projects/episeerr_custom/<file> episeerr:/app/<file> && docker restart episeerr

# Bake into Docker image
cd ~/projects/episeerr_custom && ./release_dev.sh custom
```

## Key Files

| File | Purpose |
|------|---------|
| `episeerr.py` | Main Flask app — routes, setup page, service toggles |
| `dashboard.py` | Dashboard stats; uses `get_series_banners_bulk()` (one Sonarr call) |
| `webhooks.py` | Webhook handlers (Plex/Tautulli/Jellyfin/Emby) |
| `movie_processor.py` | Radarr movie rules engine |
| `media_processor.py` | Sonarr episode rule processing |
| `settings_db.py` | SQLite helpers — `get_service()`, `save_service()` |
| `integrations/arvio.py` | Arvio TV app — playback webhooks + settings sync (custom only) |
| `integrations/trakt.py` | Trakt — device code auth, watchlist poster cards, remove from Trakt |
| `integrations/plex.py` | Plex — watchlist sync, autosync enabled check |
| `integrations/__init__.py` | Auto-discovers integrations; `create_blueprint()` supports list return |
| `templates/setup.html` | Service setup — all collapsed by default, enable/disable toggles on all cards |
| `templates/dashboard.html` | Dashboard — Plex + Trakt watchlist widgets |
| `templates/movie_rules.html` | Movie rules — includes Clean Orphaned Tags button |

## Service Enable/Disable Pattern

Services live in the `services` SQLite table (`enabled BOOLEAN DEFAULT 1`).

- `get_service('name', 'default')` filters `WHERE enabled = 1` — returns `None` when disabled
- Toggle: `POST /api/toggle-service/<service>` with `{"enabled": true/false}`
- Setup route reads enabled state via raw SQL (no `enabled=1` filter) so toggle reflects actual DB value
- **Do NOT** gate widget display on `config is not None` — hides env-var services with no DB row

## Trakt

`integrations/trakt.py` — no browser OAuth redirect needed:

1. Save Client ID + Secret in setup, then click **Get Device Code**
2. `POST /auth/device` → `user_code` + `verification_url`; `POST /auth/poll` saves tokens to DB on approval
3. `preprocess_save_data` preserves stored tokens when form fields are blank
4. TMDB poster fetch: stored key is a v4 JWT → `Authorization: Bearer` header, not `?api_key=` param

## Arvio Integration (custom only)

`integrations/arvio.py` — webhook receiver + settings sync. No watchlist sync (removed 2.0.20).

- Webhook fires Sonarr rule processing when `progress_percent >= completion_threshold`
- History only logs events at/above threshold + watchlist events
- SSE player state at `/dashboard/player/events` for web UI live player

## Setup Page

- All service cards start collapsed on load
- `sonarr_enabled`, `tmdb_enabled` come from raw DB query in setup route
- `integration_configs[name]['enabled']` also from raw DB query
- `toggleServiceEnabled(service, checked)` JS → `POST /api/toggle-service/<service>`
