# episeerr_custom — Personal Custom Build

> **CRITICAL: This is episeerr_custom at `~/projects/episeerr_custom`. Never edit `episeerr_dev`. The running Docker container is `episeerr`, deployed from `/docker/media/compose`. Deploy changes with `docker cp` (not rebuild) unless dependencies changed:**
> ```bash
> docker cp ~/projects/episeerr_custom/integrations/arvio.py episeerr:/app/integrations/arvio.py && docker restart episeerr
> ```

This is the **permanent personal build** of Episeerr. It is never promoted to production.

## Purpose

Contains all personal and experimental features that are not intended for the community release:

- **Arvio integration** (`integrations/arvio.py`) — Android TV watchlist API over LAN
- **Trakt integration** — Trakt.tv watchlist sync
- **Movie rules** — Radarr-based movie management rules
- **Expression modifiers** — e1+, e2+ style episode shorthand
- **Universal search** — cross-service search
- All bug fixes and community-useful features from episeerr_dev

## Release

Built and released with `~/projects/release_custom.sh`:

```bash
# Docker only (no git)
./release_custom.sh

# Push to custom branch on GitHub + Docker tag :custom
./release_custom.sh --sync
```

Produces Docker tag: `vansmak/episeerr:custom`

## Workflow

This folder is **never** promoted via `promote_dev.sh`. It receives updates by manually syncing from `episeerr_dev` and re-adding personal features on top.

- Community-bound work → develop in `episeerr_dev`, then sync here
- Arvio-specific work → develop here only
