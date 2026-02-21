# ⚠️ Documentation Moved

**Main documentation is now in the root README.md**

👉 [**Go to Main README**](../README.md)

---

This folder is kept for backward compatibility with existing links.
For the latest documentation, always check the root README.md.
# Episeerr Documentation

**Smart episode management for Sonarr** - Automate downloads, manage storage, and keep only what you need.

---

## 🚀 New Users Start Here

**Never used Episeerr before?** Follow these guides in order:

1. **[Installation](getting-started/installation.md)** - Get Episeerr running (5 minutes)
2. **[Quick Start](getting-started/quick-start.md)** - Configure and test (5 minutes)
3. **[Add Your First Series](getting-started/first-series.md)** - Step-by-step walkthrough

**Total time:** 15 minutes from zero to working system

---

## 📚 Essential Reading

**Must-read guides to understand how Episeerr works:**

### [**Deletion System Guide**](core-concepts/deletion-system.md) ⭐

**The most important doc!** Explains how Keep rules, Grace cleanup, and Dormant deletion work together.

**Read this first if you're confused about:** When/why episodes get deleted, bookmarks, grace periods, dry run mode

---

### [**Tags & Auto-Assign**](core-concepts/tags-and-auto-assign.md)

How `episeerr_default` and `episeerr_select` tags work, and when to use auto-assign instead.

**Read this if:** Tags disappear from Sonarr, series aren't being managed, confused about immediate vs deferred processing

---

### [Rules Explained](core-concepts/rules-explained.md)

What rules are, how they work, and what GET/KEEP/Action settings do.

**Read this if:** Creating your first rule, don't understand rule settings

---

### [Webhooks Explained](core-concepts/webhooks-explained.md)

Why webhooks exist, what they do, and which ones you need.

**Read this if:** Don't understand webhook setup, wondering why you need Tautulli/Jellyfin

---

## 🎯 Features

**Choose the features you want to use:**

| Feature | What It Does | When To Use |
|---------|--------------|-------------|
| [**Episode Selection**](features/episode-selection.md) | Manually choose specific episodes, or apply a rule directly | Try pilots, skip seasons, re-route a series to a different rule |
| [**Viewing Automation**](features/viewing-automation.md) | Next episode ready when you watch | Binge watching, always-ready episodes |
| [**Storage Management**](features/storage-management.md) | Auto-cleanup based on time/viewing | Limited storage, inactive shows cleanup |
| [**Plex Watchlist Sync**](features/plex-watchlist-sync.md) | Add to Plex watchlist, Episeerr queues TV shows for selection and sends movies to Radarr | Zero-effort adding from Plex Discover |
| [**Pending Deletions**](features/pending-deletions.md) | Review deletions before they happen | Test settings safely, manual approval |

**All features work independently** - use one, some, or all!

---

## ⚙️ Configuration

**Set up and customize Episeerr:**

### Getting Connected

- [**Webhook Setup**](configuration/webhook-setup.md) - Connect Tautulli, Jellyfin, Sonarr, Jellyseerr
- [**Sonarr Integration**](configuration/sonarr-integration.md) - Tags, release profiles, delayed downloads

### Creating Rules

- [**Rules Guide**](configuration/rules-guide.md) - Step-by-step rule creation
- [**Rule Examples**](configuration/rule-examples.md) - Copy/paste common configurations

### System Settings

- [**Global Settings**](configuration/global-settings.md) - Storage gate, dry run mode, cleanup intervals

---

## 🔧 Troubleshooting

**Having problems?**

### [Common Issues](troubleshooting/common-issues.md)

Quick fixes for frequent problems:
- Tags not working
- Episodes not monitoring
- Webhooks not firing
- Deletions not happening
- Series not being managed

### [Debugging Guide](troubleshooting/debugging.md)

How to read logs, trace webhook flow, and diagnose issues.

### [Known Issues](troubleshooting/known-issues.md)

Current limitations and bugs we're working on.

---

## 📖 Guides & Best Practices

### For Everyone

- [**Best Practices**](guides/best-practices.md) - Recommended configurations and tips
- [**Migration Guide**](guides/migration.md) - Upgrading from older versions

### Advanced Users

- [**Advanced Scenarios**](guides/advanced-scenarios.md) - Multi-user setups, complex rules, custom workflows

---

## 📋 Reference

### Technical Documentation

- [**Configuration Schema**](reference/configuration-schema.md) - JSON structure reference
- [**API Reference**](reference/api.md) - Webhook endpoints and responses
- [**Changelog**](reference/changelog.md) - Version history and updates

---

## 🎓 Learning Paths

**Different paths for different needs:**

### Path 1: "Just Get It Working"

1. [Installation](getting-started/installation.md)
2. [Quick Start](getting-started/quick-start.md)
3. [Add First Series](getting-started/first-series.md)
4. Done! Come back later for advanced features

---

### Path 2: "I Want to Understand Everything"

1. [Installation](getting-started/installation.md)
2. [Deletion System Guide](core-concepts/deletion-system.md) ⭐
3. [Tags & Auto-Assign](core-concepts/tags-and-auto-assign.md)
4. [Rules Explained](core-concepts/rules-explained.md)
5. [Webhooks Explained](core-concepts/webhooks-explained.md)
6. [Rules Guide](configuration/rules-guide.md)
7. [Best Practices](guides/best-practices.md)

---

### Path 3: "I Need Specific Features"

**For Episode Selection:**
1. [Installation](getting-started/installation.md)
2. [Episode Selection](features/episode-selection.md)
3. [Sonarr Integration](configuration/sonarr-integration.md)

**For Viewing Automation:**
1. [Installation](getting-started/installation.md)
2. [Deletion System Guide](core-concepts/deletion-system.md)
3. [Viewing Automation](features/viewing-automation.md)
4. [Webhook Setup](configuration/webhook-setup.md)
5. [Rules Guide](configuration/rules-guide.md)

**For Storage Management:**
1. [Installation](getting-started/installation.md)
2. [Deletion System Guide](core-concepts/deletion-system.md) ⭐
3. [Storage Management](features/storage-management.md)
4. [Rules Guide](configuration/rules-guide.md)

---

## 💡 Quick Tips

**Before you dive in:**

- ✅ **Start with dry run mode enabled** - Review deletions before they happen
- ✅ **Use one rule for everything at first** - Add complexity later
- ✅ **Check logs frequently** - `/app/logs/app.log` shows everything
- ✅ **Read the Deletion System Guide** - Prevents 90% of confusion
- ✅ **Join the community** - [GitHub Discussions](https://github.com/Vansmak/episeerr/discussions)

---

## 🆘 Still Stuck?

1. **Check [Common Issues](troubleshooting/common-issues.md)** - Your problem might be there
2. **Read [Deletion System Guide](core-concepts/deletion-system.md)** - Solves most confusion
3. **Review logs** - See [Debugging Guide](troubleshooting/debugging.md)
4. **Ask for help** - [GitHub Discussions](https://github.com/Vansmak/episeerr/discussions) or [Issues](https://github.com/Vansmak/episeerr/issues)

---

## 📊 System Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Your Media Server                     │
│         (Plex/Jellyfin with Tautulli/Webhook)          │
└────────────────────┬────────────────────────────────────┘
                     │
                     │ Watch Episode (Webhook)
                     ↓
┌─────────────────────────────────────────────────────────┐
│                       Episeerr                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │  Keep Rule   │  │ Grace Cleanup│  │   Dormant    │ │
│  │  (Real-time) │  │  (Scheduled) │  │  (Storage)   │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
└────────────────────┬────────────────────────────────────┘
                     │
                     │ Monitor/Delete Episodes
                     ↓
┌─────────────────────────────────────────────────────────┐
│                        Sonarr                            │
│              (Monitors and downloads episodes)           │
└─────────────────────────────────────────────────────────┘
```

**[See detailed flow diagram](assets/flow.svg)**

---
## Recent Updates

- **Plex Watchlist Sync** — add to Plex watchlist, TV shows queue for selection, movies go straight to Radarr. Optional movie cleanup with grace period. See [Plex Watchlist Sync](features/plex-watchlist-sync.md)
- **Series page selection icon** — click the list icon on any poster or table row to send an existing series through the selection flow without needing Sonarr tags
- **Rule picker on selection page** — apply a rule directly from the season selection page (no manual episode picking needed), or choose episodes and assign a rule for ongoing management
- **Rule pre-selection** — the rule dropdown on the selection page defaults to the show's currently assigned rule
- Auto-cleanup of deleted series from config
- Smart Jellyfin mode detection (no manual disable)
- Webhook string/integer tag handling
- Delay profile simplified to 3 tags

## 📝 Documentation Status

**Last Updated:** January 2026  
**Version:** 2.0 (Restructured)

**Recent Changes:**
- ✅ Restructured all documentation for clarity
- ✅ Added comprehensive deletion system guide
- ✅ Added tags and auto-assign explanation
- ✅ Created learning paths for different user types
- ✅ Consolidated overlapping content

**Feedback?** [Open an issue](https://github.com/Vansmak/episeerr/issues) or [start a discussion](https://github.com/Vansmak/episeerr/discussions)

---

**Ready to get started?** → [Installation Guide](getting-started/installation.md)
