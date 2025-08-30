# Webhook Setup

Webhooks enable viewing-based automation - Episeerr responds when you watch episodes to prepare next ones automatically.

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

### Plex (via Tautulli) Setup

1. **In Tautulli, go to Settings → Notification Agents**
2. **Click "Add a new notification agent" and select "Webhook"**
3. **Configure the webhook:**
   - **Webhook URL:** `http://your-episeerr-ip:5002/webhook`
   - **Trigger:** Episode Watched
   - **JSON Data:** Use exactly this template:

```json
{
  "plex_title": "{show_name}",
  "plex_season_num": "{season_num}",
  "plex_ep_num": "{episode_num}",
  "thetvdb_id": "{thetvdb_id}",
  "themoviedb_id": "{themoviedb_id}"
}
```

![webhook](https://github.com/Vansmak/OCDarr/assets/16037573/cf0db503-d730-4a9c-b83e-2d21a3430ece)![webhook2](https://github.com/Vansmak/OCDarr/assets/16037573/45be66c2-1869-49c1-8074-9081ed7c913b)
![webhook3](https://github.com/Vansmak/OCDarr/assets/16037573/24f02a75-2100-4b2a-9137-ce1e68803d1f)![webhook4](https://github.com/Vansmak/OCDarr/assets/16037573/f82198fc-e4c4-40ec-a9c7-551b2d8cdccd)

**Important:** In Settings → General, set **TV Episode Watched Percent** to control when webhooks trigger. Set the percentage for a TV episode to be considered as watched. Minimum 50%, Maximum 95%.

### Jellyfin Setup

**To configure Jellyfin to send playback information to Episeerr:**

1. **Navigate to Dashboard → Plugins → Webhooks**
   - If the Webhooks plugin is not installed, install it first from the Plugin Catalog

2. **Click + Add New Webhook and configure with these settings:**
   - **Webhook Name:** Episeerr Episode Tracking (or any name you prefer)
   - **Server URL:** Your Jellyfin base URL (for linking to content)
   - **Webhook URL:** `http://your-episeerr-ip:5002/jellyfin-webhook`
   - **Status:** Enabled
   - **Notification Type:** Select only "Playback Progress"
   - **User Filter (Optional):** Specific username(s) to track
   - **Item Type:** Episodes
   - **Send All Properties:** Enabled
   - **Content Type:** application/json

3. **Under Request Headers, add:**
   - **Key:** `Content-Type`
   - **Value:** `application/json`

4. **Click Save**

**Important Notes:**

- Episeerr processes playback events when progress is between 45-55% of the episode (mid-point)
- Make sure your server can reach your Episeerr instance on port 5002
- Episeerr will automatically manage episodes according to your configured rules when playback events are received

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

*This is used with the episeerr tags to cancel the request after it's added to Sonarr. If you want requests to stay in Jellyseerr/Overseerr, don't use the episeerr tags when requesting.*

1. **In Jellyseerr/Overseerr, go to Settings → Notifications**
2. **Add a new webhook notification**
3. **Set the webhook URL to** `http://your-episeerr-ip:5002/seerr-webhook`
4. **Enable notifications for "Request Approved"**
5. **Save the webhook configuration**

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

**Next:** [Rules System Guide](rules-guide.md) - Configure how Episeerr responds to
viewing events
