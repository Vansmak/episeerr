# Plex Watchlist Sync

Add something to your Plex watchlist and Episeerr handles the rest.

- [Plex Watchlist Sync](#plex-watchlist-sync)
  - [Prerequisites](#prerequisites)
  - [Setup](#setup)
  - [Getting Your Plex Token](#getting-your-plex-token)
  - [How It Works](#how-it-works)
  - [Movie Cleanup](#movie-cleanup)
  - [Use Cases](#use-cases)
  - [Troubleshooting](#troubleshooting)

## Prerequisites

- Plex server with a Plex account
- For TV shows: the `episeerr_select` delayed release profile in Sonarr (see [Episode Selection](episode-selection.md))
- For movies: Radarr configured in Episeerr

## Setup

1. Go to **Setup** (`/setup`) → scroll to **Plex** under Dashboard Integrations
2. Enter your **Plex URL** (e.g., `http://plex:32400`) and **Plex Token**
3. Click **Test Connection** to verify
4. In the **Watchlist Auto-Sync** section:
   - Toggle **Enable automatic sync**
   - Set **Sync Interval** (30 min – 24 hours, default 2 hours)
   - Optionally enable **Movie Cleanup** with a grace period
5. Click **Save**

## Getting Your Plex Token

**Script method (easiest):**

```bash
python get_plex_token.py
```

Requires `requests` (`pip install requests`). Enter your Plex **username** (not email) and password. The token works for both local server access and the Plex.tv watchlist API.

**Manual method:**

1. Sign in to [plex.tv](https://app.plex.tv) in a browser
2. Navigate to any media item
3. Click `···` → **Get Info** → look at the URL — copy the `X-Plex-Token=` value

> **Note:** Use your Plex username, not your email. Check it at plex.tv → Account.

## How It Works

| What You Do | What Episeerr Does |
|-------------|-------------------|
| Add TV show to Plex watchlist | Creates a pending selection request; tags the series in Sonarr with `episeerr_select` |
| Add movie to Plex watchlist | Sends directly to Radarr |
| Watch a movie (cleanup enabled) | Schedules Radarr deletion after grace period |

Sync runs on your configured interval. Already-tracked items are skipped — no duplicates.

**TV show flow:**

1. Show added to Plex watchlist
2. Next sync: Episeerr finds it via the Plex.tv API
3. Series sent to Sonarr with `episeerr_select` tag (all episodes unmonitored)
4. Pending request created → appears in **Pending Items**
5. You go to Pending Items → Select Seasons → choose a rule or pick episodes manually

**Movie flow:**

1. Movie added to Plex watchlist
2. Next sync: Episeerr sends it to Radarr
3. Radarr handles the download as normal

## Movie Cleanup

When **Delete movies after watched** is enabled:

- Episeerr checks your Plex library for watched movies on each sync
- Movies watched more than **Grace Period** days ago are removed from Radarr
- Configure grace period in the Plex section on the Setup page (default: 7 days)

## Use Cases

- **Discover browsing**: Browse Plex Discover, add anything interesting, let Episeerr queue it up
- **Shared households**: Anyone with Plex access can add to the watchlist; Episeerr picks it up
- **Movie auto-cleanup**: Watch a movie, forget about it — Radarr cleans up after the grace period
- **Zero-touch TV**: Watchlist → pending request → pick rule → done

## Troubleshooting

**Sync not running:**
- Check that sync is enabled and saved on the Setup page
- Verify the Plex token works: Test Connection button on Setup page
- Check logs: `docker logs episeerr | grep -i plex`

**TV shows not appearing in Pending Items:**
- Confirm the `episeerr_select` delayed release profile is set up in Sonarr
- Check that the series isn't already in Sonarr (already-present series are skipped)
- Logs: `docker logs episeerr | grep "watchlist"`

**Movies not going to Radarr:**
- Verify Radarr is configured in Episeerr (check Setup page → Radarr section)
- Check logs for Radarr errors

**Token not working:**
- Make sure you're using your Plex **username**, not your email
- Generate a fresh token with `get_plex_token.py`
