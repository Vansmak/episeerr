# Webhook Setup

Webhooks enable viewing-based automation - Episeerr responds when you watch episodes to prepare next ones automatically.

- [Webhook Setup](#webhook-setup)
  - [When You Need Webhooks](#when-you-need-webhooks)
    - [❌ **Skip Webhooks If:**](#-skip-webhooks-if)
    - [✅ **Use Webhooks If:**](#-use-webhooks-if)
  - [🔗 Media Server Integration](#-media-server-integration)
    - [Plex Setup](#plex-setup)
      - [Option A: Tautulli](#option-a-tautulli-recommended-if-you-already-use-tautulli)
      - [Option B: Plex Native Webhook](#option-b-plex-native-webhook-no-tautulli-required)
    - [Jellyfin Setup](#jellyfin-setup)
  - [Sonarr Setup (For Series Automation)](#sonarr-setup-for-series-automation)
  - [Jellyseerr/Overseerr Webhook Setup](#jellyseerroverseerr-webhook-setup)
  - [Testing Your Webhooks](#testing-your-webhooks)
    - [Verify Webhook Reception](#verify-webhook-reception)
    - [Verify Episode Processing](#verify-episode-processing)
  - [Troubleshooting](#troubleshooting)
    - [Webhook Not Received](#webhook-not-received)
    - [Webhook Received But Nothing Happens](#webhook-received-but-nothing-happens)
    - [Wrong Episodes Being Managed](#wrong-episodes-being-managed)
  - [Webhook Data Reference](#webhook-data-reference)
    - [What Episeerr Expects](#what-episeerr-expects)
    - [Supported Formats](#supported-formats)

## When You Need Webhooks

### ❌ **Skip Webhooks If:**

- You only want episode selection
- You prefer manual episode management
- You don't want viewing-based automation

### ✅ **Use Webhooks If:**

- You want next episode ready when you watch
- You want automatic episode management
- You want grace periods tied to viewing activity

---

## 🔗 Media Server Integration

### Plex Setup

Plex users have two options for watch detection. **Choose one — do not use both at the same time** as this will cause double-processing.

Both options handle **TV shows and movies**:
- **TV shows** → triggers rule automation (next episode, cleanup)
- **Movies** → updates Plex Watchlist sync status to Watched

---

#### Option A: Tautulli (Recommended if you already use Tautulli)

1. **In Tautulli, go to Settings → Notification Agents**
2. **Click "Add a new notification agent" and select "Webhook"**
3. **Configure the webhook:**
   - **Webhook URL:** `http://your-episeerr-ip:5002/webhook`
   - **Trigger:** Watched
   - **JSON Data:** Use exactly this template:

```json
{
  "plex_title": "{show_name}",
  "plex_movie_title": "{title}",
  "plex_season_num": "{season_num}",
  "plex_ep_num": "{episode_num}",
  "thetvdb_id": "{thetvdb_id}",
  "themoviedb_id": "{themoviedb_id}"
}
```

![webhook](https://github.com/Vansmak/OCDarr/assets/16037573/cf0db503-d730-4a9c-b83e-2d21a3430ece)![webhook2](https://github.com/Vansmak/OCDarr/assets/16037573/45be66c2-1869-49c1-8074-9081ed7c913b)
![webhook3](https://github.com/Vansmak/OCDarr/assets/16037573/24f02a75-2100-4b2a-9137-ce1e68803d1f)![webhook4](https://github.com/Vansmak/OCDarr/assets/16037573/f82198fc-e4c4-40ec-a9c7-551b2d8cdccd)

**Important:** In Tautulli Settings → General, set the **Watched Percent** threshold to control when the webhook fires (applies to both TV episodes and movies). Minimum 50%, Maximum 95%.

> ⚠️ **The screenshots above are outdated** — they show a condition filtering to episodes only. **Leave the Conditions section blank.** No media type filter is needed; Episeerr detects TV vs movie automatically from the payload.

---

#### Option B: Plex Native Webhook (No Tautulli required)

Configure watch detection directly in Episeerr's Plex integration settings (Integrations → Plex → Watch Detection).

**Detection Modes:**

| Mode | How It Works | Best For |
|------|-------------|----------|
| **Scrobble** | Fires at ~90% (Plex native scrobble event) | Simple setup, no extra config |
| **Stop + Threshold** | Fires when you stop playback, if progress ≥ your threshold | Control over what counts as "watched" |
| **Polling** | Checks progress every N minutes while playing, fires at threshold | Trigger mid-playback without stopping |

**Progress Threshold:** Set in Plex integration settings. Applies to both TV and movies.

**Allowed Users:** Optionally restrict to specific Plex usernames.

All modes handle TV and movies automatically — no additional configuration needed.

### Jellyfin Setup

**Episeerr supports two modes for Jellyfin integration - choose what works best for you:**

#### **Option 1: Real-Time Processing (Recommended)**

Processes episodes immediately when you hit 50-55% progress.

**Setup:**
1. **Navigate to:** Dashboard → Plugins → Webhooks
2. **Add New Webhook:**
   - **Webhook Name:** Episeerr Episode Tracking
   - **Server URL:** Your Jellyfin base URL
   - **Webhook URL:** `http://your-episeerr-ip:5002/jellyfin-webhook`
   - **Status:** Enabled
   - **Notification Type:** Select **"Playback Progress"** only
   - **User Filter:** Your username (optional but recommended)
   - **Item Type:** Episodes
   - **Send All Properties:** Enabled
   - **Content Type:** application/json

**Environment Variables:**
```env
JELLYFIN_URL=http://your-jellyfin:8096
JELLYFIN_API_KEY=your_api_key
JELLYFIN_USER_ID=your_username
JELLYFIN_TRIGGER_MIN=50.0
JELLYFIN_TRIGGER_MAX=55.0
```

**How it works:**
- Triggers between 50-55% progress
- Processes once, then ignores subsequent webhooks
- Real-time response

---

#### **Option 2: Polling Mode (Legacy)**

Checks progress every 15 minutes until threshold is reached.

**Setup:**
1. **Navigate to:** Dashboard → Plugins → Webhooks
2. **Add New Webhook:**
   - **Webhook Name:** Episeerr Session Tracking
   - **Webhook URL:** `http://your-episeerr-ip:5002/jellyfin-webhook`
   - **Notification Type:** Select **"Session Start"** and **"Playback Stop"**
   - **User Filter:** Your username
   - **Item Type:** Episodes

**Environment Variables:**
```env
JELLYFIN_URL=http://your-jellyfin:8096
JELLYFIN_API_KEY=your_api_key
JELLYFIN_USER_ID=your_username
JELLYFIN_TRIGGER_PERCENTAGE=50.0
JELLYFIN_POLL_INTERVAL=900  # 15 minutes
```

**How it works:**
- Starts polling when you begin watching
- Checks progress every 15 minutes
- Processes when >= 50%
- Stops polling when you stop watching

---

#### **Option 3: On-Stop Processing (Minimal)**

Processes when you finish/stop watching (if >= 50%).

**Setup:**
1. **Navigate to:** Dashboard → Plugins → Webhooks
2. **Add New Webhook:**
   - **Webhook Name:** Episeerr Playback Stop
   - **Webhook URL:** `http://your-episeerr-ip:5002/jellyfin-webhook`
   - **Notification Type:** Select **"Playback Stop"** only
   - **User Filter:** Your username
   - **Item Type:** Episodes

**Environment Variables:**
```env
JELLYFIN_URL=http://your-jellyfin:8096
JELLYFIN_API_KEY=your_api_key
JELLYFIN_USER_ID=your_username
JELLYFIN_TRIGGER_PERCENTAGE=50.0
```

**How it works:**
- Single webhook when playback stops
- Checks if you watched >= 50%
- Processes if threshold met
- Fewest webhooks, delayed processing

---

**Which should you use?**

| Mode | Best For | Pros | Cons |
|------|----------|------|------|
| **Real-Time** | Most users | Immediate, no polling | More webhooks |
| **Polling** | Unreliable webhooks | Works with any setup | Background threads, 15-min delay |
| **On-Stop** | Minimal webhook traffic | Simplest, fewest webhooks | Only processes when you stop |

**Recommendation:** Use **Real-Time (PlaybackProgress)** for best experience.

---

**Important Notes:**
- **JELLYFIN_USER_ID is required** - Set to your Jellyfin username
- All modes require the same Jellyfin API setup
- Modes are auto-detected based on which webhooks you enable
- You can enable multiple modes (Episeerr handles deduplication)

**Troubleshooting:**

- If webhooks aren't being received, check your server logs for any webhook delivery errors
- Verify the webhook URL is correctly pointing to your Episeerr instance
- Ensure Episeerr logs show webhook events being received at `/app/logs/app.log`

---

## Sonarr Setup (For Series Automation)

1. **Sonarr:** Settings → Connect → + → Webhook
2. **Configuration:**
   - **Name:** Episeerr Integration
   - **URL:** `http://your-episeerr-ip:5002/sonarr-webhook`
   - **Method:** POST
   - **Username/Password:** Leave empty
   - **Triggers:** Enable only "On Series Add"

3. **Test by adding a series** with `episeerr_default` or `episeerr_select` tag

---

## Jellyseerr/Overseerr Webhook Setup

**Required for `episeerr_default` tag with specific seasons.** Optional otherwise.

**What it does:**
- Captures which season(s) you requested from Jellyseerr
- Allows `episeerr_default` to start from the requested season instead of Season 1
- Automatically cancels the Jellyseerr request after Episeerr processes it

**Example use case:**
- Request Season 3 from Jellyseerr
- Series added to Sonarr with `episeerr_default` tag
- Episeerr starts from Season 3 (not Season 1) based on your Jellyseerr request
- Jellyseerr request is deleted (Episeerr manages the series now)

**Setup:**

1. **In Jellyseerr/Overseerr, go to Settings → Notifications**
2. **Add a new webhook notification**
3. **Set the webhook URL to** `http://your-episeerr-ip:5002/seerr-webhook`
4. **Enable notifications for "Request Approved"**
5. **Save the webhook configuration**

**Important Notes:**
- Jellyseerr requests are **automatically deleted** after Episeerr processes them
- This prevents Jellyseerr from conflicting with Episeerr's episode management
- If you want to keep requests in Jellyseerr for tracking purposes, use the "Auto-assign new series" setting (in Episeerr → Scheduler → Global Settings) instead of the `episeerr_default` tag
- Without this webhook, `episeerr_default` will always start from Season 1

---

## Testing Your Webhooks

### Verify Webhook Reception

1. **Watch an episode** (for Tautulli/Jellyfin)
2. **Check Episeerr logs:** `/logs/app.log`
3. **Look for:** "Received webhook" or "Processing webhook"

### Verify Episode Processing

1. **Ensure series is assigned** to a rule in Episeerr
2. **Watch logs** for rule application
3. **Check Sonarr** for episode changes (monitoring/searching)

---

## Troubleshooting

### Webhook Not Received

- **Check URL format:** Ensure no trailing slashes
- **Verify network:** Can Tautulli/Jellyfin reach Episeerr?
- **Check ports:** Is port 5002 accessible?
- **Review sender logs:** Any errors in Tautulli/Jellyfin?

### Webhook Received But Nothing Happens

- **Check series assignment:** Is the series assigned to a rule?
- **Verify rule configuration:** Does the rule have Get/Keep settings?
- **Check series name matching:** Do names match between webhook and Sonarr?
- **Review Episeerr logs:** Any processing errors?

### Wrong Episodes Being Managed

- **Check rule settings:** Verify Get/Keep values are correct
- **Review series assignment:** Ensure series is in the right rule
- **Check webhook data:** Verify correct season/episode numbers

---

## Webhook Data Reference

### What Episeerr Expects

| Field | Source | Purpose |
|-------|--------|---------|
| Series Name | `show_name`, `SeriesName` | Match to Sonarr series |
| Season Number | `season_num`, `SeasonNumber` | Current season |
| Episode Number | `episode_num`, `EpisodeNumber` | Current episode |

### Supported Formats

- **Tautulli:** `plex_title`, `plex_season_num`, `plex_ep_num`
- **Jellyfin:** `SeriesName`, `SeasonNumber`, `EpisodeNumber`
- **Custom:** Any JSON with series/season/episode data

---

**Next:** [Rules System Guide](rules-guide.md) - Configure how Episeerr responds to viewing events
