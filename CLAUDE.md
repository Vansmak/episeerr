# Episeerr Dev — Claude Code Project Context

## What This Is

The upstream community feature branch of Episeerr. All general improvements land here first, then get merged to `main` for public release. No Arvio-specific code.

## Branches

| Branch | Directory | Purpose |
|--------|-----------|---------|
| `dev` | `~/projects/episeerr_dev` | **This** — upstream features |
| `custom` | `~/projects/episeerr_custom` | Joe's personal build + Arvio |
| `main` | merged from dev | Public Docker Hub releases |

**Dev is always ahead of main. Custom mirrors dev plus Arvio additions.**

## Release

```bash
~/projects/promote_dev.sh <version>
# e.g. ~/projects/promote_dev.sh 3.8.0
```

Merges dev → main, tags, pushes to Docker Hub.

## Key Files

| File | Purpose |
|------|---------|
| `episeerr.py` | Main Flask app — routes, setup page, service toggles |
| `dashboard.py` | Dashboard stats; `get_series_banners_bulk()` (one Sonarr call vs N) |
| `webhooks.py` | Webhook handlers (Plex/Tautulli/Jellyfin/Emby) |
| `movie_processor.py` | Radarr movie rules engine |
| `media_processor.py` | Sonarr episode rule processing |
| `settings_db.py` | SQLite helpers — `get_service()`, `save_service()` |
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

## Setup Page

- All service cards start collapsed on load
- `sonarr_enabled`, `tmdb_enabled` come from raw DB query in setup route
- `integration_configs[name]['enabled']` also from raw DB query
- `toggleServiceEnabled(service, checked)` JS → `POST /api/toggle-service/<service>`

## What's Excluded

Arvio TV app integration — personal, lives in `episeerr_custom` only.
