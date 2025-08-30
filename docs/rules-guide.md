# Rule-Based Episode Management

Automatic "next episode ready when you watch" system. **Assumes linear viewing** (S1E1 → S1E2 → S1E3).

## Required Setup

### 1. Media Server Webhook

**Tautulli (Plex)**:

1. Tautulli → Settings → Notification Agents → Webhook
2. **URL**: `http://your-episeerr:5002/webhook`  
3. **Triggers**: Watched
4. **JSON Data**:

```json
{
  "plex_title": "{show_name}",
  "plex_season_num": "{season_num}",
  "plex_ep_num": "{episode_num}",
  "thetvdb_id": "{thetvdb_id}",
  "themoviedb_id": "{themoviedb_id}"
}
```

**Jellyfin**:

1. Dashboard → Plugins → Webhooks → Add New
2. **URL**: `http://your-episeerr:5002/jellyfin-webhook`
3. **Events**: Playback Progress (automatically triggers at 50%)

### 2. Optional: Sonarr Webhook for Auto-Assignment

1. **Sonarr** → Settings → Connect → Webhook
2. **URL**: `http://your-episeerr:5002/sonarr-webhook`
3. **Triggers**: On Series Add

## Create Rules

1. **Episeerr** → Rules → Create Rule
2. **Configure:**

### Get Episodes (What to prepare next)

- **Episodes**: Get X individual episodes (e.g., 3)
- **Seasons**: Get X full seasons (e.g., 1)
- **All**: Get everything available

### Keep Episodes (What to retain)

- **Episodes**: Keep X individual episodes (e.g., 1)
- **Seasons**: Keep X full seasons (e.g., 1)
- **All**: Keep everything forever

### Action

- **Monitor**: Just mark for monitoring
- **Search**: Monitor and start searching immediately

```log
**Save**
```

## Assign Series to Rules

### Method 1: Manual Assignment

1. **Episeerr** → Series Management
2. Select series → Choose rule → Assign

### Method 2: Sonarr Tags (with webhook)

- Add series with `episeerr_default` tag → Auto-assigned to default rule

## How It Works

```log
Watch S1E2 → Webhook → Episeerr processes → Sonarr gets S1E3 ready
```

**Linear viewing example:**

- Rule: Get 2, Keep 1
- Watch S1E3 → Get S1E4+S1E5, Keep S1E3, Delete S1E1+S1E2

## Common Rules

### Binge Watcher

```log
Get: 3 episodes
Keep: 1 episode  
Action: Search
```

### Current Shows

```log
Get: 1 episode
Keep: 3 episodes
Action: Monitor
```

### Season Watcher

```log
Get: 1 season
Keep: 1 season
Action: Search
```

## Troubleshooting

**Episodes not updating**: Check webhook setup and logs  
**Wrong episodes**: Verify rule settings match your viewing pattern  
**Series not managed**: Ensure series is assigned to a rule
