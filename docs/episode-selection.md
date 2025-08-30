# Episode Selection

Choose specific episodes manually across multiple seasons.

## Critical Sonarr Setup (Do This First)

**Without this step, episodes download immediately instead of waiting for selection.**

1. **Sonarr** → Settings → Profiles → Release Profiles → **Add New**
2. **Settings:**
   - Name: `Episeerr Episode Selection Delay`
   - Delay: `10519200` (20 years)
   - Tags: `episeerr_select`
3. **Save**

## Sonarr Webhook (Optional but Recommended)

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

### Method 2: Jellyseerr/Overseerr Integration

1. Set up Jellyseerr webhook:
   - **URL**: `http://your-episeerr:5002/seerr-webhook`
   - **Triggers**: Request Approved
2. Request series in Jellyseerr/Overseerr
3. Add `episeerr_select` tag in Sonarr after it's added
4. Follow selection process above

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

## Special Behavior

**If you select only S1E1**: Tag removed, series assigned to default rule (becomes normal automation)  
**If you select multiple episodes**: Tag kept, manual management only

## Troubleshooting

**Episodes downloading immediately**: Missing delayed release profile  
**Selection interface not appearing**: Check TMDB API key, check logs  
**Wrong episodes monitored**: Verify selection summary before submitting
