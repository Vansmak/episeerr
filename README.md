# Episeerr

**Smart episode management for Sonarr** - Get episodes as you watch, clean up automatically when storage gets low.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/vansmak)

## What It Does

Episeerr automates your TV library with three simple features:

🎯 **Episode Selection** - Choose exactly which episodes you want  
⚡ **Smart Rules** - Next episode ready when you watch, old episodes cleaned up  
💾 **Storage Gate** - Automatic cleanup when storage gets low

## Quick Start

### Minimal Setup (Just Episode Selection)
```yaml
version: '3.8'
services:
  episeerr:
    image: vansmak/episeerr:latest
    environment:
      # Only 3 required for basic episode selection
      - SONARR_URL=http://your-sonarr:8989
      - SONARR_API_KEY=your_sonarr_api_key
      - TMDB_API_KEY=your_tmdb_api_key
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
    ports:
      - "5002:5002"
    restart: unless-stopped
```

### Full Setup (All Features)
```yaml
version: '3.8'
services:
  episeerr:
    image: vansmak/episeerr:latest
    environment:
      # Required for all features
      - SONARR_URL=http://your-sonarr:8989
      - SONARR_API_KEY=your_sonarr_api_key
      - TMDB_API_KEY=your_tmdb_api_key
      
      # Add these ONLY if you want viewing automation
      - TAUTULLI_URL=http://your-tautulli:8181
      - TAUTULLI_API_KEY=your_tautulli_key
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
    ports:
      - "5002:5002"
    restart: unless-stopped
```

### Basic Setup (Works Immediately)
1. **Start container** and go to `http://your-server:5002`
2. **That's it!** You can now use episode selection

### Optional Additions (Add Only What You Want)
- **Storage cleanup**: Set threshold in Scheduler page
- **Smart rules**: Create rules for automatic management
- **Viewing automation**: Add webhooks for next episode ready

---

## How It Works

### Smart Rules (NEW!)
Create rules with the new dropdown system:

**Get Episodes:**
- Type: Episodes/Seasons/All + Count
- Example: "3 episodes" = next 3 episodes ready

**Keep Episodes:**  
- Type: Episodes/Seasons/All + Count
- Example: "1 season" = keep current season after watching

**Grace Period (NEW!):**
- Protects your previous episodes before ypur keep block for X days after watching
- Example: 7 days = keep watched episodes that preced the kept # for a week, then delete them

**Dormant Timer (NEW!):**
- Removes content from abandoned shows 
- Example: 30 days = if no activity for a month, clean up the show

### Example: Popular Show Rule
```
Get: 5 episodes (next 5 episodes ready)
Keep: 2 episodes (last 2 watched episodes)
Grace: 7 days (keep last 2 watched episodes, delete others after a week)
Dormant: 60 days (cleanup if abandoned for 2 months)
```

**What happens:**
1. Watch E10 → Get E11-E15, Keep E9-E10, Delete E1-E8
2. After 7 days → Delete E7-E8 (grace expired), Library: E9-E15
3. After 60 days no activity → Delete show (series abandoned)

### Storage Gate
- Set one global threshold: "Keep 20GB free"
- Cleanup only runs when below threshold
- Stops immediately when back above threshold
- Only affects shows with grace/dormant timers

---

## Three Ways to Use Episeerr (Pick What You Need)

### 🎯 **Just Episode Selection**
Good for picking specific episodes. Even across seasons
- **Setup**: Just the 3 required environment variables
- **create sonarr and optional seer webhooks**
- **No rules needed, no webhooks required**
- **Use**: Manual episode selection interface only

### ⚡ **Add Viewing Automation**
Next episode ready as you watch (optional upgrade).
- **Setup**: Add Tautulli/Jellyfin webhook + create rules  
- **No storage management required**
- **Use**: Episodes managed automatically as you watch, get this many, keep this many

### 💾 **Add Storage Management**  
Automatic cleanup when storage gets low (optional upgrade).
- **Setup**: Set storage threshold + add grace/dormant timers to rules
- **No viewing automation required**
- **Use**: Hands-off storage management

---

## Key Benefits

✅ **Intuitive**: New dropdown system makes rules easy to understand  
✅ **Smart**: Grace periods that actually make sense  
✅ **Safe**: Storage gate prevents unnecessary cleanup  
✅ **Flexible**: Use only the features you need  
✅ **Storage-Aware**: Cleanup respects your storage limits

---

## What's New in v2.2

🎯 **New Dropdown UI**: Replace confusing text fields with clear dropdowns  
   ** pick type, episodes or seasons and then the # of
🧠 **Intuitive Grace Logic**: Grace periods now protect recent watches  
🔧 **Fixed Season Bug**: Properly handles end-of-season transitions  
💾 **Storage-Aware Dormant**: Dormant cleanup respects storage gates

### Migration from v2.0
- Existing rules automatically converted to new format
- No manual changes needed
- Old behavior preserved with new interface

---

## Documentation

**[📚 Full Documentation](./docs/)** - Complete guides and setup

**Quick Links:**
- [Installation Guide](./docs/installation.md) - Docker setup and configuration
- [Rules Guide](./docs/rules-guide.md) - Creating and managing rules
- [Episode Selection](./docs/episode-selection.md) - Manual episode management
- [Storage Gate](./docs/global_storage_gate_guide.md) - Automatic cleanup system

---

## Support

- **Issues**: [GitHub Issues](https://github.com/Vansmak/episeerr/issues)
- **Discussions**: [GitHub Discussions](https://github.com/Vansmak/episeerr/discussions)
- **Coffee**: [Buy Me A Coffee](https://buymeacoffee.com/vansmak) ☕

*Simple, smart episode management that just works.*
