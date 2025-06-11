# Episeerr

**Smart episode management system for Sonarr** - Three independent automation solutions with intelligent storage management.

## What Episeerr Does

Episeerr gives you precise control over your TV episodes with three separate systems that work together or independently, plus a global storage gate that keeps your library size under control.
It's like the difference between a public library (keep everything for everyone) vs. a personal reading list (just what you actually want when you want it.

### 🎯 **Three Solutions + Smart Storage**

| Solution | Purpose | When to Use |
|----------|---------|-------------|
| **🎬 Granular Episode Requests** | Select exactly which episodes you want | Want specific episodes, not full seasons |
| **⚡ Viewing-Based Rules** | Auto-manage episodes when you watch | Want next episode ready, cleanup watched ones |
| **⏰ Time-Based Cleanup** | Clean up based on age and activity | Want automatic library maintenance |
| **💾 Global Storage Gate** | One threshold controls all cleanup | Want simple, safe storage management |


**Use any combination** - or just one solution that fits your needs.

---

## 💾 Global Storage Gate (NEW!)

**One simple setting controls all cleanup across your entire library.**

### How It Works
1. **Set one global threshold** (e.g., "20GB free space")
2. **Cleanup only runs when below threshold** (smart, not wasteful)
3. **Stops when back above threshold** (surgical precision)
4. **Only affects rules with grace/dormant settings** (safe by default)

### The "Chips" Philosophy
- 🟡 **Grace Period:** "Take my chips off the table" - Remove unwatched episodes while keeping your viewing context
- 🔴 **Dormant Timer:** "Remove chips from the bank" - Show abandoned, aggressive cleanup to reclaim storage
- 🛡️ **Protected Rules:** Rules without timers = never touched (your permanent collection)

---

## 🚀 Quick Start

### Docker Compose
```yaml
version: '3.8'
services:
  episeerr:
    image: vansmak/episeerr:latest
    environment:
      # Required
      - SONARR_URL=http://your-sonarr:8989
      - SONARR_API_KEY=your_sonarr_api_key
      - TMDB_API_KEY=your_tmdb_api_key
      
      # Optional - Viewing-based rules
      - TAUTULLI_URL=http://your-tautulli:8181
      - TAUTULLI_API_KEY=your_tautulli_key
      # OR
      - JELLYFIN_URL=http://your-jellyfin:8096
      - JELLYFIN_API_KEY=your_jellyfin_key
      
      # Optional - Request integration
      - JELLYSEERR_URL=http://your-jellyseerr:5055
      - JELLYSEERR_API_KEY=your_jellyseerr_key
      
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
    ports:
      - "5002:5002"
    restart: unless-stopped
```

### Setup
1. **Configure:** `http://your-server:5002` - Web interface for management
2. **Set global storage gate** - One threshold for smart cleanup
3. **Create rules** for automated episode management  
4. **Assign series** to rules (unassigned series are ignored)
5. **Optional:** Set up webhooks for viewing-based automation

---

## 🎛️ Example Configurations

### Global Storage Gate Only
Simple storage management without viewing automation.
```
Global Storage Gate: 30GB
Rules with time-based cleanup:
  - Grace: 14 days, Dormant: 90 days
Webhooks: Not needed
```

### Viewing Rules Only  
Next episode always ready, no time-based cleanup.
```
Rule: Get 1, Search, Keep 1
Grace: null, Dormant: null
Webhooks: Tautulli or Jellyfin
```

### Episode Requests Only
Perfect for trying new shows or specific episode management.
```
No rules needed - just use episode selection interface
Webhooks: Sonarr, Jellyseerr/Overseerr
```

### Full Smart System
Complete automation with intelligent storage management.
```
Global Storage Gate: 20GB
Viewing Rules:
  - Active Shows: Get 2, Search, Keep 2, Grace: 7 days
  - Archive Shows: Get all, Monitor, Keep all, Grace: null
  - Trial Shows: Get 1, Search, Keep 1, Dormant: 30 days
Webhooks: All enabled
```

---

## 🔧 Integration

### Sonarr Tags
When making requests:
- `episeerr_default`: Auto-assigns to default rule when added
- `episeerr_select`: Triggers episode selection workflow

### Webhooks *(Optional)*
- **Tautulli:** `http://your-episeerr:5002/webhook` 
- **Jellyfin:** `http://your-episeerr:5002/jellyfin-webhook` 
- **Sonarr:** `http://your-episeerr:5002/sonarr-webhook`
- **Jellyseerr/Overseerr:** `http://your-episeerr:5002/seerr-webhook`

---

## 🎯 Key Benefits

- **🔧 Modular:** Use only the features you need
- **🎯 Precise:** Episode-level control when you want it
- **⚡ Responsive:** Next episode ready when you need it  
- **🧹 Smart:** Global storage gate prevents waste
- **💡 Intuitive:** "Chips" philosophy makes cleanup predictable
- **🛡️ Safe:** Protected rules never touched by cleanup
- **🏠 Respectful:** Only manages assigned series

---

## 📚 Documentation

**[Complete Documentation →](./docs/)**

**Quick Links:**
- [Installation & Setup](./docs/installation.md)
- [Global Storage Gate Guide](./docs/global_storage_gate_guide.md) 
- [Rules System Guide](./docs/rules-guide.md) 
- [Episode Selection](./docs/episode-selection.md)
- [Webhook Setup](./docs/webhooks.md)

---

## 🆕 What's New in v2.0

- **🌍 Global Storage Gate:** One threshold controls all cleanup
- **🎯 Smart Priority:** Cleanup order based on dormant > grace, oldest first
- **🛡️ Rule Protection:** Rules without timers are never touched
- **🎮 Intuitive UI:** Visual storage status and gate indicators
- **🧠 "Chips" Philosophy:** Clear mental model for cleanup behavior

---

*Episeerr: Three solutions for episode management with intelligent storage control - use what you need, when you need it.*
