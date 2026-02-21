# Episode Selection

Choose specific episodes manually across multiple seasons — or just pick a rule and let it decide.

- [Episode Selection](#episode-selection)
  - [Critical Sonarr Setup (Do This First)](#critical-sonarr-setup-do-this-first)
  - [Sonarr Webhook (Optional but Recommended)](#sonarr-webhook-optional-but-recommended)
  - [How to Use](#how-to-use)
    - [Method 1: Sonarr Tags](#method-1-sonarr-tags)
    - [Method 2: Series Page Icon](#method-2-series-page-icon)
    - [Method 3: Plex Watchlist Sync](#method-3-plex-watchlist-sync)
    - [Method 4: Jellyseerr/Overseerr Integration](#method-4-jellyseerroverseerr-integration)
    - [Method 5: Jellyseerr with episeerr\_default Tag](#method-5-jellyseerr-with-episeerr_default-tag)
  - [The Rule Picker](#the-rule-picker)
  - [What Happens](#what-happens)
  - [Use Cases](#use-cases)
  - [Special Behavior](#special-behavior)
  - [Troubleshooting](#troubleshooting)

## Critical Sonarr Setup (Do This First)

**Without this step, episodes download immediately instead of waiting for selection.**

1. **Sonarr** → Settings → Profiles → Release Profiles → **Add New**
2. **Settings:**
   - Name: `Episeerr Episode Selection Delay`
   - Delay: `10519200` (20 years)
   - Tags: `episeerr_select`
3. **Save**

## Sonarr Webhook 

1. **Sonarr** → Settings → Connect → Webhook → **Add New**
2. **URL**: `http://your-episeerr:5002/sonarr-webhook`
3. **Triggers**: On Series Add only
4. **Save**

## How to Use

### Method 1: Sonarr Tags

1. Add series to Sonarr with `episeerr_select` tag
2. Go to Episeerr → Pending Requests
3. Click "Select Seasons" → Choose seasons
4. Click "Select Episodes" → Choose specific episodes
5. Submit

### Method 2: Series Page Icon

For series already in Sonarr — no tag needed.

1. Go to **Episeerr → Series** (grid or manage view)
2. Click the **list icon** on any poster (top-right corner in grid view) or in the **Actions** column (table/manage view)
3. You're taken directly to the season selection page for that show
4. Pick a rule or choose episodes manually

**The rule dropdown pre-selects the show's current rule** if it already has one, making it easy to move a series to a different rule.

### Method 3: Plex Watchlist Sync

1. Add a TV show to your Plex watchlist
2. On the next sync cycle, Episeerr creates a pending request automatically
3. Go to **Pending Items** → follow the selection flow

See [Plex Watchlist Sync](plex-watchlist-sync.md) for setup.

### Method 4: Jellyseerr/Overseerr Integration

1. Set up Jellyseerr webhook:
   - **URL**: `http://your-episeerr:5002/seerr-webhook`
   - **Triggers**: Request Approved
2. Request series in Jellyseerr/Overseerr
3. Add `episeerr_select` tag
4. Follow selection process above

### Method 5: Jellyseerr with episeerr_default Tag

**Best for:** Starting automated management from a specific season

1. Set up Jellyseerr webhook (see [Webhook Setup](webhooks.md))
2. Request specific season(s) in Jellyseerr (e.g., Season 3)
3. Series added to Sonarr with `episeerr_default` tag
4. Episeerr starts from your requested season automatically

**Example:**
- Request Season 3 from Jellyseerr
- Add series with `episeerr_default` tag  
- Default rule: "Get 2 episodes"
- Result: S03E01 and S03E02 monitored/searched

**Important Notes:**
- Jellyseerr request will be **automatically deleted** after processing
- Without Jellyseerr webhook, `episeerr_default` always starts from Season 1
- If you want to keep requests in Jellyseerr, use "Auto-assign new series" setting instead

## The Rule Picker

Every entry into the selection flow shows a **rule dropdown** at the top of the season selection page.

**Apply Rule** — pick a rule and click Apply. The rule is assigned to the series for ongoing management. No episode processing happens immediately — the rule governs future watch events (e.g., next episode queued after you watch one).

**Select seasons/episodes below** — ignore the Apply Rule button and check the seasons/episodes you want manually. The rule selected in the dropdown is still assigned for ongoing management (future watch events use it).

**Rule pre-selection:** If the series already has a rule assigned, the dropdown defaults to that rule. Useful for quickly moving a show to a different rule without going through Sonarr tags.

**Cancelling:** Hitting Cancel on the selection page deletes the pending request and goes back to the previous page — nothing is downloaded.

## What Happens

- **Series added** with `episeerr_select` tag
- **All episodes unmonitored** (prevents downloads)  
- **Selection interface appears** in Episeerr
- **Choose episodes** across any seasons
- **Only selected episodes monitored** and searched
- **Jellyseerr request cancelled** (if applicable)

## Use Cases

- **Try pilots**: Just episode 1 to test new shows
- **Specific episodes**: Get episodes you missed  
- **Limited storage**: Surgical control over downloads
- **Multi-season selection**: Episodes from seasons 1, 3, and 5
- **Season-specific automation**: Start automated management from a specific season

## Special Behavior

**If you select only S1E1**: Tag removed, series assigned to default rule (becomes normal automation)  
**If you select multiple episodes**: Tag kept, manual management only

## Troubleshooting

**Episodes downloading immediately**: Missing delayed release profile  
**Selection interface not appearing**: Check TMDB API key, check logs  
**Wrong episodes monitored**: Verify selection summary before submitting  
**episeerr_default starting from Season 1**: Jellyseerr webhook not configured (see [Webhook Setup](webhooks.md))
