# episeerr_dev — Community Dev Branch

This is the **community development branch** of Episeerr. It is promoted to production via `promote_dev.sh`.

## Purpose

Contains all features intended for public release. Personal/private integrations (e.g. Arvio) are intentionally excluded.

## What's included

- **Trakt integration** — Trakt.tv watchlist sync
- **Movie rules** — Radarr-based movie management rules
- **Expression modifiers** — e1+, e2+ style episode shorthand
- **Universal search** — cross-service search
- **JF/Emby favorites** — Jellyfin and Emby favorites watchlist
- All community-useful bug fixes and features

## What's excluded

- Arvio integration (personal, lives in `episeerr_custom` only)

## Release workflow

```bash
# Promote dev → production and release
~/projects/promote_dev.sh <version>

# e.g.
~/projects/promote_dev.sh 3.8.0
~/projects/promote_dev.sh 3.8.0-rc.1
```

`promote_dev.sh` copies this folder to `episeerr`, then runs `episeerr/release.sh <version>` which handles git tagging and Docker push.
