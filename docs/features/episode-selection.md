# Episode Selection

Choose specific episodes manually across multiple seasons — or just pick a rule and let it decide.

- [Critical Sonarr Setup](#critical-sonarr-setup-external-paths-only)
- [How to Enter the Selection Flow](#how-to-enter-the-selection-flow)
- [The Rule Picker](#the-rule-picker)
- [What Happens on Confirm](#what-happens-on-confirm)
- [Use Cases](#use-cases)
- [Troubleshooting](#troubleshooting)

---

## Critical Sonarr Setup (external paths only)

**Only needed if you add series via Sonarr tags, Plex watchlist sync, or Seerr.** Skip this if you only add series by searching within Episeerr — Sonarr isn't touched until after you confirm.

Without this, episodes added with the `episeerr_select` tag will start downloading immediately before you make your selection.

1. **Sonarr** → Settings → Profiles → Release Profiles → **Add New**
2. **Settings:**
   - Name: `Episeerr Episode Selection Delay`
   - Delay: `10519200` (20 years)
   - Tags: `episeerr_select`
3. **Save**

---

## How to Enter the Selection Flow

### Search within Episeerr (nothing touches Sonarr until you confirm)

1. Use the search bar in Episeerr to find a TV show
2. Click **Add** on the result
3. Season/rule selection opens immediately
4. Pick a rule and/or select specific seasons
5. Confirm → Episeerr adds the series to Sonarr and updates Plex watchlist
6. Cancel → nothing is written anywhere

No tag or delay profile needed.

---

### Sonarr tag (external add)

1. Add series to Sonarr with `episeerr_select` tag
2. Episeerr webhook intercepts it → creates pending request, all episodes unmonitored
3. Go to **Episeerr → Pending Items** → click Select
4. Follow selection flow

Requires the delay profile above to hold downloads.

---

### Plex watchlist sync

1. Add a TV show to your Plex watchlist
2. On the next sync cycle, Episeerr creates a pending request automatically
3. Go to **Pending Items** → follow the selection flow

See [Plex Watchlist Sync](plex-watchlist-sync.md) for setup.

---

### Series already in Sonarr

1. Go to **Episeerr → Series** (grid or manage view)
2. Click the **list icon** on any poster or in the Actions column
3. You're taken directly to the season selection page

The rule dropdown pre-selects the show's current rule, making it easy to move a series to a different rule.

---

### Jellyseerr/Overseerr

1. Set up the Seerr webhook pointing to Episeerr
2. Request a series — it's added to Sonarr with `episeerr_select` tag
3. Pending request appears in Episeerr → follow selection flow

---

## The Rule Picker

Every entry into the selection flow shows a **rule dropdown** at the top of the season selection page.

**Apply Rule** — pick a rule and click Apply. The rule is assigned and its GET logic runs immediately (e.g., monitors first N episodes). Ongoing management from there.

**Select seasons/episodes manually** — check the seasons or episodes you want. The rule selected in the dropdown is still assigned for ongoing management, but you control what downloads now.

**Cancel** — deletes the pending request. For the in-Episeerr search path, nothing is added to Sonarr or Plex watchlist at all.

---

## What Happens on Confirm

| Path | Sonarr add | Plex watchlist |
|------|-----------|----------------|
| Search within Episeerr | Happens at confirm | Updated at confirm |
| Sonarr tag / Plex sync / Seerr | Already in Sonarr | Updated at confirm |
| Series page icon (already in Sonarr) | Already in Sonarr | Updated at confirm |

After confirm: only the selected/rule-specified episodes are monitored and searched. Everything else stays unmonitored.

---

## Use Cases

- **Try pilots** — select only S1E1 before committing
- **Skip seasons** — jump straight to S3
- **Specific arcs** — select exactly the episodes you want
- **Re-route a series** — one-click rule change from the selection page
- **Limited storage** — surgical control, nothing extra downloads

---

## Troubleshooting

**Episodes downloading immediately (external path):** Missing delayed release profile — create it in Sonarr as described above.

**Selection interface not appearing:** Check TMDB API key, check logs for errors.

**Wrong episodes monitored:** Check the selection summary before submitting.

**episeerr_default starting from Season 1:** Jellyseerr webhook not configured. See [Webhook Setup](../configuration/webhook-setup.md).
