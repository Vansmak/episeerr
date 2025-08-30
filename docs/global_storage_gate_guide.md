# Storage Cleanup

Automatic cleanup based on time and viewing activity. **Completely optional** - only runs when storage is actually low.

## Setup Storage Gate

1. **Episeerr** → Scheduler
2. **Storage Threshold**: Set GB limit (e.g., `20` for 20GB free)
3. **Cleanup Interval**: How often to check (e.g., `6` hours)
4. **Save**

**How it works**: Cleanup only runs when free space drops below threshold. When space is adequate, no cleanup happens.

## Add Grace Periods to Rules

**All optional - use any combination:**

### Grace Watched (Collection Rotation)

- **Days before kept episodes expire**
- Example: `7` = Your watched episodes expire after 7 days of no series activity
- Use for: Making room for new content

### Grace Unwatched (Watch Deadlines)

- **Days before unwatched episodes expire**
- Example: `14` = New episodes have 2 weeks to be watched or deleted
- Use for: Staying current, preventing backlog

### Dormant (Abandoned Series)

- **Days before complete series cleanup**
- Example: `60` = If no activity for 2 months, clean when storage is low
- Use for: Reclaiming space from shows you stopped watching
- **Set to empty/null**: Never clean up (protected series)

## Example Rule Configurations

### Standard Show

```log
Grace Watched: 7 days
Grace Unwatched: 14 days  
Dormant: 30 days
```

### Important Show (Protected)

```log
Grace Watched: empty
Grace Unwatched: empty
Dormant: empty
```

### Current Show (Pressure to Stay Current)

```log
Grace Watched: empty
Grace Unwatched: 7 days
Dormant: 60 days
```

## How Cleanup Works

**Three independent timers:**

1. **Grace Watched**: Series inactive for X days → Delete kept episodes
2. **Grace Unwatched**: Series inactive for X days → Delete unwatched episodes  
3. **Dormant**: Series inactive for X days + storage low → Delete all episodes

**Priority order**: Dormant (oldest shows) → Grace Watched → Grace Unwatched

**Stops automatically**: When storage goes back above threshold

## Testing (Important)

1. **Enable Global Dry Run** in Scheduler settings
2. **Test cleanup** with your settings
3. **Check logs** to see what would be deleted
4. **Disable Dry Run** when satisfied

## Series Assignment

**Only assigned series participate in cleanup:**

- Manual: Episeerr → Series Management
- Automatic: Add `episeerr_default` tag in Sonarr (with webhook)
- Protected: Create rules with empty Dormant timer

## Troubleshooting

**No cleanup happening**: Check storage threshold, ensure series are assigned to rules  
**Wrong shows deleted**: Review rule assignments and grace settings  
**Cleanup too aggressive**: Increase grace periods, enable dry run for testing
