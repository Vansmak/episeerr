# Episeerr

Smart episode management for Sonarr - Get episodes as you watch, clean up automatically when storage gets low.

**Perfect for:**
- Limited storage setups (seedboxes, VPS, budget home servers)
- Preventing runaway downloads that fill your disk
- Automated cleanup based on viewing activity
- Per-season tracking for shows with multiple active seasons
- Granular episode-level control
- Custom rules for different types of shows

This project started as scratching my own itch - I wanted more granular series management and couldn't find exactly what I wanted. I'm not a programmer by trade, but I had a clear vision for the solution I needed. I used AI as a development tool to help implement my ideas faster, just like any other tool. The creativity, problem-solving, architecture decisions, and feature design are all mine - AI helped with code, syntax and implementation details. Although I run everything in my own production environment first, it is catered to my environment and is use at your own risk. All code is open source for anyone to review and audit. The tool has been useful for me, and I shared it in case others can benefit from it too - but I absolutely understand if some prefer to stick with established solutions.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/vansmak)

---

## 🚀 Quick Start

**New to Episeerr?** Get running in 5 minutes: **[Quick Start Guide](./docs/getting-started/quick-start.md)**

**Want to understand first?** Read: **[How Episeerr Works](./docs/core-concepts/deletion-system.md)**

---

## What It Does

Episeerr automates your TV library with three independent features:

🎯 **[Episode Selection](./docs/features/episode-selection.md)** - Choose exactly which episodes you want  
⚡ **[Viewing Automation](./docs/features/viewing-automation.md)** - Next episode ready when you watch  
💾 **[Storage Management](./docs/features/storage-management.md)** - Automatic cleanup when storage gets low

**Use one, some, or all** - they work independently!

---

## Installation

### Docker Compose (Recommended)

```yaml
services:
  episeerr:
    image: vansmak/episeerr:latest
    environment:
      # Required
      - SONARR_URL=http://your-sonarr:8989
      - SONARR_API_KEY=your_sonarr_api_key
      - TMDB_API_KEY=your_tmdb_api_key
      
      # Optional - For viewing automation
      - TAUTULLI_URL=http://your-tautulli:8181
      - TAUTULLI_API_KEY=your_tautulli_key
      # OR
      - JELLYFIN_URL=http://your-jellyfin:8096
      - JELLYFIN_API_KEY=your_api_key
      - JELLYFIN_USER_ID=your_username
      
      # Optional - For request integration
      - JELLYSEERR_URL=http://your-jellyseerr:5055
      - JELLYSEERR_API_KEY=your_jellyseerr_key
      
      # Optional - Quick links
      - CUSTOMAPP_URL=http://192.168.1.100:8080
      - CUSTOMAPP_NAME=My Custom App
      - CUSTOMAPP_ICON=fas fa-cog

    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
      - ./data:/app/data
      - ./temp:/app/temp
    ports:
      - "5002:5002"
    restart: unless-stopped
```

**After starting:** Open `http://your-server:5002`

**Full installation guide:** [Installation Documentation](./docs/getting-started/installation.md)

---

## How It Works

### Smart Rules System

Create rules with intuitive dropdowns:

**GET:** What to prepare next (e.g., "3 episodes", "1 season", "all")  
**KEEP:** What to retain (e.g., "1 episode", "1 season", "all")  
**ACTION:** Monitor or search immediately

### Grace Periods (Time-Based Cleanup)

**Grace Watched:** Deletes OLD watched episodes after X days, keeps last watched as bookmark  
**Grace Unwatched:** Deletes unwatched episodes after X days, keeps first unwatched as bookmark  
**Dormant:** Deletes EVERYTHING after X days of no activity (abandoned shows)

### Storage Gate

- Set one global threshold: "Keep 20GB free"
- Cleanup only runs when below threshold
- Stops immediately when back above threshold

**Learn more:** [Deletion System Guide](./docs/core-concepts/deletion-system.md)

---

## Example Configuration

### Binge Watcher Rule

```yaml
Get: 3 episodes
Keep: 1 episode
Grace Watched: 7 days
Grace Unwatched: 14 days
Dormant: 30 days
```

**What happens:**

1. **Watch E10** → Next 3 episodes (E11-E13) monitored/searched
2. **Keep E10** → Delete E1-E9 (outside keep window)
3. **After 7 days inactive** → Delete E10 (grace expired, bookmark kept)
4. **After 30 days inactive** → Delete entire show (abandoned)

---

## 📚 Documentation

### For New Users

1. **[Installation Guide](./docs/getting-started/installation.md)** - Get Episeerr running
2. **[Quick Start](./docs/getting-started/quick-start.md)** - Configure in 5 minutes
3. **[Add Your First Series](./docs/getting-started/first-series.md)** - Step-by-step tutorial

### Essential Reading

- **[Deletion System Guide](./docs/core-concepts/deletion-system.md)** ⭐ - How Keep/Grace/Dormant work
- **[Tags & Auto-Assign](./docs/core-concepts/tags-and-auto-assign.md)** - How series get managed
- **[Rules Explained](./docs/core-concepts/rules-explained.md)** - Understanding rule settings
- **[Webhooks Explained](./docs/core-concepts/webhooks-explained.md)** - Why webhooks matter

### Features

- **[Episode Selection](./docs/features/episode-selection.md)** - Manual episode picking
- **[Viewing Automation](./docs/features/viewing-automation.md)** - Rule-based management
- **[Storage Management](./docs/features/storage-management.md)** - Automatic cleanup

### Configuration

- **[Webhook Setup](./docs/configuration/webhook-setup.md)** - Connect Tautulli/Jellyfin/Sonarr
- **[Rules Guide](./docs/configuration/rules-guide.md)** - Create and manage rules
- **[Rule Examples](./docs/configuration/rule-examples.md)** - Copy/paste configs

### Help

- **[Common Issues](./docs/troubleshooting/common-issues.md)** - Quick fixes
- **[Debugging Guide](./docs/troubleshooting/debugging.md)** - Log analysis

**[📖 Full Documentation Index](./docs/)**

---

## Key Benefits

✅ **Intuitive**: Dropdown system makes rules easy to understand  
✅ **Safe**: Dry run mode + approval queue for testing  
✅ **Flexible**: Use only the features you need  
✅ **Storage-Aware**: Cleanup respects your storage limits  
✅ **Bookmark System**: Never lose your viewing position

---

## Screenshots

<img width="1869" height="1040" alt="image" src="https://github.com/user-attachments/assets/4c17baab-b9a7-4ace-aa8a-65e5594cf228" />

---

## Support

- **Issues**: [GitHub Issues](https://github.com/Vansmak/episeerr/issues)
- **Discussions**: [GitHub Discussions](https://github.com/Vansmak/episeerr/discussions)
- **Buy Me Coffee**: [☕ Support Development](https://buymeacoffee.com/vansmak)

---

## What's New

**v2.2 Highlights:**
- Complete documentation restructure
- Comprehensive deletion system guide
- Tag behavior explained
- New quick start and tutorial
- Improved rule explanations

**[Full Changelog](./docs/reference/changelog.md)**

---

*Simple, smart episode management that just works.*
