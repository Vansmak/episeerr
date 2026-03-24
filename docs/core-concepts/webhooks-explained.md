# Webhooks Explained

Understanding why webhooks exist and what they do.

## What is a Webhook?

A **webhook** is a message one application sends to another when something happens.

**Think of it like:**
- 📞 A phone call: "Hey, something just happened!"
- 📬 A notification: "User just watched an episode"
- 🔔 A trigger: "New series was added"

---

## Why Episeerr Needs Webhooks

**Episeerr can't watch what you're doing.** It needs to be told when things happen.

### Without Webhooks:
```
You watch S1E5 → Nothing happens
Episeerr: 🤷 "I have no idea you watched anything"
Result: Episodes never update
```

### With Webhooks:
```
You watch S1E5 → Tautulli/Jellyfin sends webhook
Episeerr: ✅ "Got it! Processing..."
Result: Next episodes monitored, old ones deleted
```

---

## The Three Webhooks

### 1. Media Server Webhook (Required for Automation)

**From:** Tautulli, Plex native webhook, or Jellyfin
**To:** Episeerr
**When:** You watch something
**Message:** "User watched S1E5 of Breaking Bad" or "User watched Dune"

**What Episeerr Does:**
- **TV shows** → finds the series in your rules, applies GET rule (monitors next episodes), applies KEEP rule (deletes old episodes), updates activity date
- **Movies** → updates Plex Watchlist sync status to Watched (triggers cleanup grace period if enabled)

**Plex users:** Choose either Tautulli or the Plex native webhook — not both. See [Webhook Setup Guide](../configuration/webhook-setup.md) for the comparison.

**Without this:** Rules never trigger, watchlist statuses never update, manual management only

**Setup:** [Webhook Setup Guide](../configuration/webhook-setup.md)

---

### 2. Sonarr Webhook (Required for Tags/Auto-Assign)

**From:** Sonarr  
**To:** Episeerr  
**When:** Series added to Sonarr  
**Message:** "New series added: Breaking Bad (ID: 123, Tags: episeerr_default)"

**What Episeerr Does:**

**If has `episeerr_default` tag:**
1. Adds series to default rule
2. Applies GET rule immediately
3. Removes tag

**If has `episeerr_select` tag:**
1. Creates episode selection interface
2. Waits for your choices
3. Removes tag

**If no tag but auto-assign enabled:**
1. Adds series to default rule
2. Waits for first watch

**Without this:** Tags don't work, auto-assign doesn't work

**Setup:** [Sonarr Integration](../configuration/sonarr-integration.md)

---

### 3. Jellyseerr/Overseerr Webhook (Optional, for Season Requests)

**From:** Jellyseerr/Overseerr  
**To:** Episeerr  
**When:** Request approved  
**Message:** "User requested Season 3 of Breaking Bad"

**What Episeerr Does:**
1. Stores the season number
2. When series added with `episeerr_default` tag
3. Starts from requested season (not Season 1!)
4. Deletes request from Jellyseerr

**Without this:** `episeerr_default` always starts from Season 1

**Setup:** [Webhook Setup Guide](../configuration/webhook-setup.md)

---

## How They Work Together

### Example: Request Season 3 via Jellyseerr

```
1. You request Season 3 in Jellyseerr
   └─ Jellyseerr webhook → Episeerr: "Season 3 requested"

2. Jellyseerr adds series to Sonarr with episeerr_default tag
   └─ Sonarr webhook → Episeerr: "Series added with tag"

3. Episeerr processes:
   ├─ Remembers Season 3 from Jellyseerr webhook
   ├─ Applies GET rule starting from Season 3
   ├─ Monitors S3E1, S3E2, S3E3 (based on GET setting)
   ├─ Removes tag from Sonarr
   └─ Deletes request from Jellyseerr

4. You start watching Season 3
   └─ Tautulli webhook → Episeerr: "Watched S3E1"
   
5. Episeerr continues managing from there
```

---

## Webhook Flow Diagrams

### Watching Episodes (Core Automation)

```
┌──────────────┐
│ You Watch    │
│ Episode      │
└──────┬───────┘
       │
       │ 50%+ complete
       ↓
┌──────────────────────┐
│ Tautulli/Jellyfin    │
│ Detects Watch        │
└──────┬───────────────┘
       │
       │ Webhook: "S1E5 watched"
       ↓
┌──────────────────────┐
│ Episeerr             │
│ ├─ Find series rule  │
│ ├─ GET next episodes │
│ ├─ KEEP last watched │
│ └─ DELETE old ones   │
└──────┬───────────────┘
       │
       │ API calls
       ↓
┌──────────────────────┐
│ Sonarr               │
│ ├─ Monitor S1E6, S1E7│
│ └─ Delete S1E1-S1E4  │
└──────────────────────┘
```

---

### Adding Series (Tag Processing)

```
┌──────────────┐
│ Add Series   │
│ in Sonarr    │
│ with tag     │
└──────┬───────┘
       │
       ↓
┌──────────────────────┐
│ Sonarr               │
│ Series Added Event   │
└──────┬───────────────┘
       │
       │ Webhook: "Series added, tags: episeerr_default"
       ↓
┌──────────────────────┐
│ Episeerr             │
│ ├─ Detect tag        │
│ ├─ Add to rule       │
│ ├─ Apply GET rule    │
│ └─ Remove tag        │
└──────┬───────────────┘
       │
       │ API calls
       ↓
┌──────────────────────┐
│ Sonarr               │
│ ├─ Monitor episodes  │
│ ├─ Search episodes   │
│ └─ Tag removed       │
└──────────────────────┘
```

---

## What Gets Sent in Webhooks?

### Tautulli Webhook — TV Episode

```json
{
  "plex_title": "Breaking Bad",
  "plex_season_num": "1",
  "plex_ep_num": "5",
  "thetvdb_id": "81189",
  "themoviedb_id": "1396"
}
```

**Episeerr uses:** Series title, season, episode to find and run rule automation

### Tautulli Webhook — Movie

```json
{
  "plex_title": "Dune: Part Two",
  "plex_season_num": "",
  "plex_ep_num": "",
  "thetvdb_id": "",
  "themoviedb_id": "693134"
}
```

**Episeerr uses:** `themoviedb_id` (season/episode empty = movie) to update Plex Watchlist status to Watched

> Same template for both — Episeerr detects the type automatically.

---

### Jellyfin Webhook (Watch Event)

```json
{
  "SeriesName": "Breaking Bad",
  "SeasonNumber": 1,
  "EpisodeNumber": 5,
  "PlaybackPositionTicks": 24000000000
}
```

**Episeerr uses:** Series name, season, episode, and position to determine if ≥50% watched

---

### Sonarr Webhook (Series Added)

```json
{
  "eventType": "SeriesAdd",
  "series": {
    "id": 123,
    "title": "Breaking Bad",
    "tags": [5, 7]  // Tag IDs
  }
}
```

**Episeerr uses:** Series ID, title, and tags to determine processing

---

## Troubleshooting Webhooks

### "Nothing happens when I watch"

**Check:**
1. Is media server webhook configured?
2. Does it point to correct Episeerr URL?
3. Is series assigned to a rule in Episeerr?

**Test:** Check `/app/logs/app.log` for webhook receipts

---

### "Tag processing doesn't work"

**Check:**
1. Is Sonarr webhook configured?
2. Does tag exist in Sonarr (Settings → Tags)?
3. Is "On Series Add" trigger enabled?

**Test:** Add series, check logs for "Processing with episeerr_default"

---

### "Starts from Season 1 instead of requested season"

**Check:**
1. Is Jellyseerr/Overseerr webhook configured?
2. Did you request via Jellyseerr before adding to Sonarr?
3. Is `episeerr_default` tag used?

**Test:** Check logs for "Stored Jellyseerr request"

---

## Common Misconceptions

### ❌ "Episeerr watches my Plex/Jellyfin"

**No!** Episeerr can't see what you're doing. It relies entirely on webhooks.

---

### ❌ "I don't need webhooks for episode selection"

**Partially true.** Episode selection works without watch webhooks, but you still need Sonarr webhook for tag processing.

---

### ❌ "Webhooks are optional"

**Depends on features:**
- Episode selection: Sonarr webhook required
- Viewing automation: Media server webhook required
- Storage management: Can work with manual triggers only

---

### ❌ "Multiple webhooks will cause duplicates"

**No!** Episeerr deduplicates webhook events automatically.

---

## Security Note

**Webhooks are NOT authenticated by default.**

**This means:**
- Anyone who knows your Episeerr URL can send fake webhooks
- Usually not a concern on private networks
- Consider using reverse proxy with authentication for public access

---

## Next Steps

- **Set up webhooks:** [Webhook Setup Guide](../configuration/webhook-setup.md)
- **Test webhooks:** [First Series Tutorial](../getting-started/first-series.md)
- **Troubleshoot:** [Debugging Guide](../troubleshooting/debugging.md)
- **Understand rules:** [Rules Explained](rules-explained.md)
