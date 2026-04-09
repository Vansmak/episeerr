
# Add Your First Series

Step-by-step walkthrough of adding a series and seeing Episeerr in action.

## Method 0: Search within Episeerr (Simplest)

The easiest path — nothing touches Sonarr until you confirm.

### Step 1: Search

1. Open Episeerr: `http://your-server:5002`
2. Use the **search bar** at the top to find a TV show by name
3. Click **Add** on the result

### Step 2: Make your selections

The season/rule selection screen opens immediately.

- Pick a **rule** from the dropdown (or select specific seasons/episodes)
- Click **Apply Rule** or **Submit**

### Step 3: Done

Episeerr adds the series to Sonarr, applies the rule, and updates your Plex watchlist. If you cancel, nothing is written anywhere.

---

## Method 1: Auto-Assign (Recommended)

### Step 1: Enable Auto-Assign

1. Open Episeerr: `http://your-server:5002`
2. Click **Scheduler** → **Global Settings**
3. Enable: **"Auto-assign new series to default rule"**
4. Click **Save**

### Step 2: Add Series in Sonarr

1. Open Sonarr
2. Add any TV series normally
3. **Don't add any tags** - just add it normally

### Step 3: Verify Assignment

1. Go to Episeerr → **Series Management**
2. Your series should appear under "default" rule
3. ✅ Success!

### Step 4: Watch an Episode

1. Watch any episode to 50%+
2. Check Sonarr - next episodes now monitored!

---

## Method 2: Using Tags

### Step 1: Create Tag in Sonarr

1. **Sonarr** → Settings → Tags
2. **Add tag:** `episeerr_default`
3. **Save**

### Step 2: Add Series with Tag

1. Search for series
2. Before clicking "Add":
   - Select **episeerr_default** tag
3. Click **Add**

### Step 3: Verify Processing

1. Check Sonarr - first episodes should be monitored immediately!
2. Tag will disappear (this is normal!)
3. Series appears in Episeerr Series Management

---

## How to Know It's Working

### Check Episeerr

**Series Management** should show:
```
Rule: default
└─ Your Series Name
```

### Check Sonarr

After watching Episode 1:
```
Episode 1: Monitored ✅ 
Episode 2: Monitored ✅
Episode 3: Monitored ✅
Episode 4: Unmonitored ⬜
```

### Check Logs

`/app/logs/app.log` should show:
```
✓ Auto-assigned [Series] to default rule
✓ Monitored 2 episodes
✓ Started search for [Series]
```

---

## Troubleshooting

**Series not in Series Management?**
- Check auto-assign is enabled
- Check Sonarr webhook is configured
- Manually add in Series Management

**Episodes not monitoring?**
- Check series is in a rule
- Watch another episode to trigger

**Tag disappeared but nothing happened?**
- Tag disappearing is normal!
- Check Series Management
- Check Sonarr episodes
- Check logs

---

## Next Steps

- [Set up viewing webhooks](../configuration/webhook-setup.md)
- [Configure storage cleanup](../features/storage-management.md)
- [Understand rules](../core-concepts/rules-explained.md)
