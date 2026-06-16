# Episeerr Custom — Claude Code Project Context

## What This Is

Joe's personal production build of Episeerr. Includes everything in `dev` plus the Xadarr TV app integration. Running as Docker container `episeerr` on port 5002.

## Branches

| Branch | Directory | Purpose |
|--------|-----------|---------|
| `custom` | `~/projects/episeerr_custom` | **This** — production, includes Xadarr |
| `dev` | `~/projects/episeerr_dev` | Upstream features, no Xadarr |
| `main` | merged from dev | Public Docker Hub releases |

General improvements go to both `dev` and `custom`. Xadarr-specific code stays in `custom` only.

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
| `integrations/xadarr.py` | Xadarr TV app — playback webhooks + settings sync + embedded web UI (custom only) |
| `integrations/dispatcharr.py` | Dispatcharr IPTV — stream widget, webhook handler, maintenance trigger, failover toast |
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

## Xadarr Integration (custom only)

`integrations/xadarr.py` — webhook receiver + settings sync + embedded web UI.

**In Joe's setup, Episeerr is the Xadarr sync server.** The TV app connects directly to Episeerr — no separate xadarr-server in this path. (xadarr-server remains a valid alternative for other users.)

- Webhook fires Sonarr rule processing when `progress_percent >= completion_threshold`
- History only logs events at/above threshold + watchlist events
- SSE stream at `/xadarr/api/player/events` — the TV app holds this connection for live player state and toast notifications
- `broadcast_episeerr_event(entry: dict)` — module-level function; pushes a named `event: episeerr` SSE frame to all connected Xadarr clients, triggering `_pushEpiseerrToast` in the TV app. Used by `dispatcharr.py` to deliver the channel failover toast.
- Toast event types handled by TV app: `episode.grabbed`, `episode.ready`, `rule.triggered`, `rule.assigned`, `watchlist.requested`, `channel.failover` (orange)

## Xadarr Embedded Web UI

Xadarr's sections are embedded inside Episeerr's sidebar layout (no iframe).

| File | Purpose |
|------|---------|
| `templates/xadarr_embed.html` | Extends `base.html`; loads `/xadarr/style.css` + Alpine.js; calls `embeddedInit(section)` |
| `integrations/xadarr_static/app.js` | `embeddedInit()` — parallel load of settings + catalogues + section data; `window.xadarrGoto` exposed after Alpine init for SPA tab switching |
| `integrations/xadarr_static/style.css` | `.xadarr-embedded` rules hide Xadarr's own sidebar/chrome |
| `templates/base.html` | Sidebar has expandable Xadarr section with 4 sub-links (`data-xadarr-section` attr); JS intercepts clicks and calls `window.xadarrGoto` to switch tabs client-side without page reload |

Routes in `integrations/xadarr.py`:
- `/xadarr/discover`, `/xadarr/search`, `/xadarr/cameras`, `/xadarr/settings` → `render_template("xadarr_embed.html", section=<name>)`

**Navigation:** First visit to any `/xadarr/*` URL does a full load. Subsequent sidebar clicks call `switchTab()` on the Alpine component + `history.pushState()` — no reload.

**`{% block extra_head %}` must stay OUTSIDE the `<style>` block in `base.html`** — placing it inside means `<link>` tags are treated as CSS text and never load.

## Setup Page

- All service cards start collapsed on load
- `sonarr_enabled`, `tmdb_enabled` come from raw DB query in setup route
- `integration_configs[name]['enabled']` also from raw DB query
- `toggleServiceEnabled(service, checked)` JS → `POST /api/toggle-service/<service>`

## Dispatcharr Integration

`integrations/dispatcharr.py` — live IPTV stream monitoring widget + webhook handler.

**Webhook events handled** (`POST /api/integration/dispatcharr/webhook`):
| Event | Action |
|-------|--------|
| `channel_start` / `channel_started` | Add stream to `_active_streams`, bg API sync after 1.5s |
| `channel_stop` / `channel_stopped` | Remove stream from `_active_streams` |
| `channel_failover` | Set `failover=True` on stream, push Episeerr web toast (SSE), push Xadarr TV toast via `broadcast_episeerr_event` |
| `m3u_refreshed` | Run `scripts/dispatcharr/maintenance.sql` via `docker exec dispatcharr psql` |

**Notification SSE** (`GET /api/integration/dispatcharr/notification/events`): Pushes `channel_failover` events to Episeerr web UI. `base.html` listens on this stream and shows an orange Bootstrap toast on any page.

**Xadarr TV toast**: Dispatcharr's `channel_failover` handler imports and calls `broadcast_episeerr_event({"event": "channel.failover", "title": <channel_name>})` from `integrations.xadarr` — fires an `episeerr` SSE event to the TV app which shows the orange "⚠ Failover" toast.

**Maintenance script**: `dispatcharr.py` reads `scripts/dispatcharr/maintenance.sql` from the host project tree and pipes it to `docker exec dispatcharr psql -U postgres -d dispatcharr`. Live source is `~/projects/episeerr_custom/scripts/dispatcharr/maintenance.sql`. Runs in ~270ms. (A copy also exists at `/home/joe/config/dispatcharr/scripts/maintenance.sql` but is **not** what runs.)

## Project Separation Policy

Three separate projects — never mix code between them without explicit instruction:

**episeerr_dev** (`~/projects/episeerr_dev`)
- Community/upstream Episeerr only
- Bug fixes, general features, integrations useful to all users
- No Xadarr-specific code
- Promote to production via `./promote_dev.sh <version>`
- Changes here eventually become `vansmak/episeerr:latest` on Docker Hub

**episeerr_custom** (`~/projects/episeerr_custom`)
- Joe's personal build only
- Includes everything in dev PLUS Xadarr integration
- Xadarr is Joe's renamed fork of the Arvio Android TV app; Episeerr is its sync server
- `integrations/xadarr.py` lives here only — never in episeerr_dev
- Deploy via `docker cp` to running `episeerr` container
- Bake permanently via `./release_custom.sh`

**Xadarr** (`~/projects/xadarr`)
- Joe's personal Android TV app fork (Kotlin/Jetpack Compose)
- Joe uses Episeerr as his sync server; xadarr-server (`~/projects/xadarr/xadarr-server/`) is a valid alternative for other users
- Never include Episeerr-specific hardcoding — webhook system is generic
