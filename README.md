# Episeerr

**Smart episode management for Sonarr** - Get episodes as you watch, clean up automatically when storage gets low.

This project started as scratching my own itch - I wanted more granular series management and couldn't find exactly what I wanted. I'm not a programmer by trade, but I had a clear vision for the solution I needed. I used AI as a development tool to help implement my ideas faster, just like any other tool. The creativity, problem-solving, architecture decisions, and feature design are all mine - AI helped with code, syntax and implementation details. Although I run everything in my own production environment first, it is catered to my environment and is use at your own risk. All code is open source for anyone to review and audit. The tool has been useful for me, and I shared it in case others can benefit from it too - but I absolutely understand if some prefer to stick with established solutions.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/vansmak)

- [Episeerr](#episeerr)
  - [What It Does](#what-it-does)
  - [Quick Start](#quick-start)
    - [Full Setup (All Features)](#full-setup-all-features)
    - [Basic Setup (Works Immediately)](#basic-setup-works-immediately)
    - [Optional Additions (Add Only What You Want)](#optional-additions-add-only-what-you-want)
  - [How It Works](#how-it-works)
    - [Smart Rules (NEW!)](#smart-rules-new)
    - [Grace Periods (NEW!)](#grace-periods-new)
    - [Example: Popular Show Rule](#example-popular-show-rule)
    - [Storage Gate](#storage-gate)
  - [Three Ways to Use Episeerr (Pick What You Need)](#three-ways-to-use-episeerr-pick-what-you-need)
    - [🎯 **Just Episode Selection**](#-just-episode-selection)
    - [⚡ **Add Viewing Automation**](#-add-viewing-automation)
    - [💾 **Add Storage Management**](#-add-storage-management)
  - [Key Benefits](#key-benefits)
  - [What's New in v2.2](#whats-new-in-v22)
  - [Documentation](#documentation)
  - [Support](#support)

## What It Does

Episeerr automates your TV library with three simple features:

🎯 **Episode Selection** - Choose exactly which episodes you want  
⚡ **Smart Rules** - For example, next episode ready when you watch, old episodes cleaned up  
💾 **Smart Cleanup** - Automatic cleanup that can be based on when storage gets low

## Quick Start

### Full Setup (All Features)

```yaml
services:
  episeerr:
    image: vansmak/episeerr:latest
    environment:
      # Required for all features
      - SONARR_URL=http://your-sonarr:8989 # add webhook in sonarr
      - SONARR_API_KEY=your_sonarr_api_key
      - TMDB_API_KEY=your_tmdb_api_key
      # Add your seer info if you want to use episeer to manage by episode
      - JELLYSEERR_URL=http://your overseer or jellyseer url #leave field name as jellyseer even if you use overseerr
      - JELLYSEERR_API_KEY:
      # Add these ONLY if you want viewing automation #add webhook in tautulli
      - TAUTULLI_URL=http://your-tautulli:8181
      - TAUTULLI_API_KEY=your_tautulli_key
      # Or
      - JELLYFIN_URL=http://your-JF_URL  # ADD WEBHOOK IN JELLYFIN
      - JELLYFIN_API_KEY=your_jf_key
      - JELLYFIN_USER_ID=
     
      # Optional quicklinks
      - CUSTOMAPP_URL=http://192.168.254.205:8080 # example SABNZBD_URL=http...
      - CUSTOMAPP_NAME=My Custom App  # Optional
      - CUSTOMAPP_ICON=fas fa-cog    # Optional

    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
      - ./data:/app/data
      - ./temp:/app/temp
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
- **Add `watched` tag in Sonarr**: Removes these series from Episeer Series Management

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

### Grace Periods (NEW!)

Create rules with two independent grace timers:

**Grace Watched (Rotating Collection):**

- Your kept episodes expire after X days of inactivity
- Example: 14 days = watched rotate out after 2 weeks

**Grace Unwatched (Watch Deadlines):**

- New episodes get X days to be watched if no activity
- Example: 10 days = pressure to watch new content

**Dormant Timer (NEW!):**

- Removes content from abandoned shows
- Example: 30 days = if no activity for a month, clean up the show

### Example: Popular Show Rule

```log
Get: 5 episodes (next 5 episodes ready)
Keep: 2 episodes (last 2 watched episodes)
Grace: 7 days (keep last 2 watched episodes, delete after a week)
Dormant: 60 days (cleanup if abandoned for 2 months)
```

**What happens:**

1. Watch E10 → Get E11-E15, Keep E9-E10
2. After 7 days → Delete E9-10 (grace expired)
3. After 60 days no activity → Delete show (series abandoned)

### Storage Gate

- Set one global threshold: "Keep 20GB free"
- Cleanup only runs when below threshold
- Stops immediately when back above threshold
- Only affects shows with grace/dormant timers

---

## Three Ways to Use Episeerr (Pick What You Need)

### 🎯 **Just Episode Selection**

Good for picking specific episodes. Even across seasons.

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
