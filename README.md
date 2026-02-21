# Episeerr

**Smart episode management for Sonarr** - Get episodes as you watch, clean up automatically when storage gets low. Granular selection of seasons and episodes. Get what you want and only what you want.
This project started as scratching my own itch - I wanted more granular series management and couldn't find exactly what I wanted. I'm not a programmer by trade, but I had a clear vision for the solution I needed. I used AI as a development tool to help implement my ideas faster, just like any other tool. The creativity, problem-solving, architecture decisions, and feature design are all mine - AI helped with code, syntax and implementation details. Although I run everything in my own production environment first, it is catered to my environment and is use at your own risk. All code is open source for anyone to review and audit. The tool has been useful for me, and I shared it in case others can benefit from it too - but I absolutely understand if some prefer to stick with established solutions.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/vansmak)

---
[![Docker Pulls](https://img.shields.io/docker/pulls/vansmak/episeerr)](https://hub.docker.com/r/vansmak/episeerr)
[![GitHub Issues](https://img.shields.io/github/issues/vansmak/episeerr)](https://github.com/Vansmak/episeerr/issues)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Support-orange)](https://buymeacoffee.com/vansmak)

---
newest features will be the dev branch and can be pulled as :custom tag.  Includes the ability to make new season and episode requests straight from episeerr for existing shows. 
also for plex users can see your watchlist and auto sync it   https://github.com/Vansmak/episeerr/tree/dev
## 📋 Table of Contents

- [What It Does](#what-it-does)
- [Quick Start](#quick-start)
- [Installation](#installation)
  - [Docker Compose](#docker-compose-recommended)
  - [Unraid](#unraid)
  - [Environment Variables](#environment-variables)
- [Webhook Setup](#webhook-setup)
  - [Sonarr](#1-sonarr-webhook-required)
  - [Tautulli (Plex)](#2-tautulli-webhook-for-viewing-automation)
  - [Jellyfin](#3-jellyfin-webhook-for-viewing-automation)
  - [Jellyseerr/Overseerr](#4-jellyseerroverseerr-webhook-optional)
- [How to Use](#how-to-use)
  - [Create Your First Rule](#create-your-first-rule)
  - [Add Series](#add-series-three-ways)
  - [Episode Selection](#episode-selection)
- [Features Explained](#features-explained)
- [Configuration Examples](#configuration-examples)
- [Troubleshooting](#troubleshooting)
- [Screenshots](#screenshots)
- [FAQ](#faq)
- [Support](#support)

---

## What It Does

Episeerr gives you **three independent features** for TV episode management:

| Feature | What It Does | Use Case |
|---------|--------------|----------|
| 🎯 **Episode Selection** | Choose specific episodes to download | Try pilots, skip seasons, selective downloads |
| ⚡ **Viewing Automation** | Next episode ready when you watch | Binge watching, always-ready episodes |
| 💾 **Storage Management** | Automatic cleanup based on time/viewing | Limited storage, inactive show cleanup |

**Use one, some, or all** - they work independently!

---

## Quick Start

**Get running in 5 minutes:**

```bash
# 1. Create docker-compose.yml (minimal setup)
services:
  episeerr:
    image: vansmak/episeerr:latest
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
      - ./data:/app/data
    ports:
      - "5002:5002"
    restart: unless-stopped

# 2. Start container
docker-compose up -d

# 3. Open http://your-server:5002/setup
# 4. Configure Sonarr, TMDB, and optional services
# 5. Create a rule, add a series, start watching!
```
**Restart container for changes to take effect**
**That's it!** No `.env` file needed - configure everything via the GUI.

**For automation:** [Set up webhooks](#webhook-setup) ⬇️

---

## Installation

### Two Ways to Configure:

1. **GUI Setup (Recommended)** - Use `/setup` page, no `.env` file needed
2. **Environment Variables** - Traditional `.env` file (still supported)

---

### Docker Compose (Recommended)

**Create `docker-compose.yml`:**

```yaml
services:
  episeerr:
    image: vansmak/episeerr:latest
    container_name: episeerr
    environment:
      
      - LOG_LEVEL=INFO  # Optional - defaults to INFO if not set
      # ============================================
      # REQUIRED
      # ============================================
      - SONARR_URL=http://your-sonarr:8989
      - SONARR_API_KEY=your_sonarr_api_key
      - TMDB_API_KEY=your_tmdb_read_access_token
      
      # ============================================
      # OPTIONAL - For Viewing Automation
      # ============================================
      
      # Option 1: Tautulli (Plex)
      - TAUTULLI_URL=http://your-tautulli:8181
      - TAUTULLI_API_KEY=your_tautulli_key
      
      # Option 2: Jellyfin (choose one mode below)
      
      # --- Jellyfin: uncomment Option A OR Option B, not both ---
      #
      # Option A: Real-time (Jellyfin sends PlaybackProgress webhooks)
      #   Configure in Jellyfin: http://<episeerr>:5002/jellyfin-webhook
      #   Notification type: PlaybackProgress
      #
      # - JELLYFIN_URL=http://your-jellyfin:8096
      # - JELLYFIN_API_KEY=your_jellyfin_api_key
      # - JELLYFIN_USER_ID=your_username
      # - JELLYFIN_TRIGGER_MIN=50.0
      # - JELLYFIN_TRIGGER_MAX=55.0
      #
      # Option B: Polling (Jellyfin sends PlaybackStart, Episeerr polls /Sessions)
      #   Configure in Jellyfin: http://<episeerr>:5002/jellyfin-webhook
      #   Notification type: PlaybackStart
      #
      # - JELLYFIN_URL=http://your-jellyfin:8096
      # - JELLYFIN_API_KEY=your_jellyfin_api_key
      # - JELLYFIN_USER_ID=your_username
      # - JELLYFIN_TRIGGER_PERCENTAGE=50.0
      # - JELLYFIN_POLL_INTERVAL=900

      # --- Emby: uncomment to enable ---
      #   Configure in Emby: User Prefs → Notifications → Webhooks
      #   URL: http://<episeerr>:5002/emby-webhook
      #   Events: playback.start, playback.stop
      #
      # - EMBY_URL=http://your-emby:8096
      # - EMBY_API_KEY=your_emby_api_key
      # - EMBY_USER_ID=your_username
      # - EMBY_TRIGGER_PERCENTAGE=50.0
      # - EMBY_POLL_INTERVAL=900
      
      # ============================================
      # OPTIONAL - For Request Integration
      # ============================================
      - JELLYSEERR_URL=http://your-jellyseerr:5055
      - JELLYSEERR_API_KEY=your_jellyseerr_key
      # OR
      - OVERSEERR_URL=http://your-overseerr:5055
      - OVERSEERR_API_KEY=your_overseerr_key
      
      # ============================================
      # OPTIONAL - Quick Links in Sidebar
      # ============================================
      - CUSTOMAPP_URL=http://192.168.1.100:8080
      - CUSTOMAPP_NAME=My Custom App
      - CUSTOMAPP_ICON=fas fa-cog

    volumes:
      - ./config:/app/config     # Configuration files
      - ./logs:/app/logs         # Log files
      - ./data:/app/data         # Database and temp data
      - ./temp:/app/temp         # Temporary processing
    ports:
      - "5002:5002"
    restart: unless-stopped
```

**Start:**
```bash
docker-compose up -d
```

**Access:**
```
http://your-server:5002
```

---

### Unraid  Untested by me

**1. Add Custom Template**

Create `/boot/config/plugins/community.applications/private/episeerr/my-episeerr.xml`:

```xml
<?xml version="1.0"?>
<Container version="2">
  <Name>episeerr</Name>
  <Repository>vansmak/episeerr:latest</Repository>
  <Registry>https://hub.docker.com/r/vansmak/episeerr</Registry>
  <Network>bridge</Network>
  <Shell>sh</Shell>
  <Privileged>false</Privileged>
  <Support>https://github.com/Vansmak/episeerr/issues</Support>
  <Project>https://github.com/Vansmak/episeerr</Project>
  <Overview>Smart episode management for Sonarr</Overview>
  <Category>MediaApp:Video</Category>
  <WebUI>http://[IP]:[PORT:5002]</WebUI>
  <Icon>https://raw.githubusercontent.com/Vansmak/episeerr/main/static/logo_icon.png</Icon>
  
  <Config Name="WebUI Port" Target="5002" Default="5002" Mode="tcp" Description="Episeerr WebUI" Type="Port" Display="always" Required="true" Mask="false"/>
  
  <Config Name="Config" Target="/app/config" Default="/mnt/user/appdata/episeerr/config" Mode="rw" Description="Configuration files" Type="Path" Display="always" Required="true" Mask="false"/>
  <Config Name="Logs" Target="/app/logs" Default="/mnt/user/appdata/episeerr/logs" Mode="rw" Description="Log files" Type="Path" Display="always" Required="true" Mask="false"/>
  <Config Name="Data" Target="/app/data" Default="/mnt/user/appdata/episeerr/data" Mode="rw" Description="Database files" Type="Path" Display="always" Required="true" Mask="false"/>
  <Config Name="Temp" Target="/app/temp" Default="/mnt/user/appdata/episeerr/temp" Mode="rw" Description="Temporary files" Type="Path" Display="always" Required="false" Mask="false"/>
  
  <Config Name="SONARR_URL" Target="SONARR_URL" Default="" Description="Sonarr base URL (e.g., http://sonarr:8989)" Type="Variable" Display="always" Required="true" Mask="false"/>
  <Config Name="SONARR_API_KEY" Target="SONARR_API_KEY" Default="" Description="Sonarr API key" Type="Variable" Display="always" Required="true" Mask="true"/>
  <Config Name="TMDB_API_KEY" Target="TMDB_API_KEY" Default="" Description="TMDB Read Access Token (not API key)" Type="Variable" Display="always" Required="true" Mask="true"/>
  
  <Config Name="TAUTULLI_URL" Target="TAUTULLI_URL" Default="" Description="Tautulli URL (optional)" Type="Variable" Display="always" Required="false" Mask="false"/>
  <Config Name="TAUTULLI_API_KEY" Target="TAUTULLI_API_KEY" Default="" Description="Tautulli API Key (optional)" Type="Variable" Display="always" Required="false" Mask="true"/>
  
  <Config Name="JELLYFIN_URL" Target="JELLYFIN_URL" Default="" Description="Jellyfin URL (optional)" Type="Variable" Display="always" Required="false" Mask="false"/>
  <Config Name="JELLYFIN_API_KEY" Target="JELLYFIN_API_KEY" Default="" Description="Jellyfin API Key (optional)" Type="Variable" Display="always" Required="false" Mask="true"/>
  <Config Name="JELLYFIN_USER_ID" Target="JELLYFIN_USER_ID" Default="" Description="Jellyfin Username (required if using Jellyfin)" Type="Variable" Display="always" Required="false" Mask="false"/>
  
  <Config Name="JELLYSEERR_URL" Target="JELLYSEERR_URL" Default="" Description="Jellyseerr URL (optional)" Type="Variable" Display="always" Required="false" Mask="false"/>
  <Config Name="JELLYSEERR_API_KEY" Target="JELLYSEERR_API_KEY" Default="" Description="Jellyseerr API Key (optional)" Type="Variable" Display="always" Required="false" Mask="true"/>
</Container>
```

**2. Install from Apps**

1. Unraid → Apps
2. Search "episeerr"
3. Click Install
4. Fill in required fields

---

### GUI Setup Page (v3.2.0+)

**The easiest way to configure Episeerr** - no `.env` file needed!

**Access:** `http://your-server:5002/setup`

**Configure:**
1. **Sonarr** - URL and API key (required) **Initial setup Restart container for changes to take effect**
2. **TMDB** - API Read Access Token (required)
3. **Media Server** - Choose Jellyfin, Emby, or Plex/Tautulli (optional)
4. **Overseerr/Jellyseerr** - Request integration (optional)

**Features:**
- ✅ Test connections before saving
- ✅ Configuration stored in database
- ✅ Auto-populate Quick Links in sidebar
- ✅ No container restart needed
- ✅ Works alongside `.env` files (database takes priority)

**Migration from .env:**
1. Open `/setup` page
2. Your existing `.env` values appear as defaults
3. Save to migrate to database
4. Delete `.env` file when ready

---

### Environment Variables

**Note:** As of v3.2.0, environment variables are **optional**. You can configure everything via the `/setup` page GUI. Environment variables still work for backward compatibility and can be used alongside database configuration (database takes priority).

| Variable | Required | Description |
|----------|----------|-------------|
| `SONARR_URL` | ❌ Optional* | Sonarr base URL (e.g., `http://sonarr:8989`) |
| `SONARR_API_KEY` | ❌ Optional* | Sonarr API key (Settings → General) |
| `TMDB_API_KEY` | ❌ Optional* | TMDB **Read Access Token** ([Get one free](https://www.themoviedb.org/settings/api)) |
| `TAUTULLI_URL` | ❌ Optional | For Plex viewing automation |
| `TAUTULLI_API_KEY` | ❌ Optional | Tautulli API key |
| `JELLYFIN_URL` | ❌ Optional | For Jellyfin viewing automation |
| `JELLYFIN_API_KEY` | ❌ Optional | Jellyfin API key |
| `JELLYFIN_USER_ID` | ⚠️ Required if using Jellyfin | Your Jellyfin username |
| `JELLYSEERR_URL` | ❌ Optional | For request integration |
| `JELLYSEERR_API_KEY` | ❌ Optional | Jellyseerr API key |

| `EMBY_USER_ID` | ⚠️ Required if using emby | Your EMBY username |
| `EMBY_URL` | ❌ Optional | For request integration |
| `EMBY_API_KEY` | ❌ Optional | EMBY API key |
**⚠️ Important Notes:**
- TMDB requires the **Read Access Token**, not the API key v3
- Jellyfin **requires** `JELLYFIN_USER_ID` to be set to your username
- All URLs should NOT have trailing slashes

---
Dashboard Integrations
Overview
Episeerr's beta plugin system allows you to connect additional services that display statistics on your dashboard. Services are configured through the Setup page and automatically appear once configured.
Available Integrations

Example, Radarr: Movie library management

Displays total movies and storage usage
Shows monitored vs total counts



Setup Process

Navigate to Setup: Go to the Setup page (/setup)
Find Integration: Scroll to "Dashboard Integrations" section
Configure Service:

URL: Full service URL including http:// or https://
API Key: Found in service settings (usually under Settings > General)


Test Connection: Click "Test" button to verify
Save: Click "Save" to store configuration
Restart: Restart the Episeerr container
Verify: Check Dashboard for new statistics pill
Quick Link: Service link automatically appears in sidebar

Important Notes

Container restart required after initial configuration
Configuration persists across restarts
Services can be reconfigured at any time through Setup page
Invalid configurations won't crash the dashboard - they simply won't display

Creating Custom Integrations
Advanced users can create custom integrations for any service with an API:

Copy Template: Start with /integrations/_INTEGRATION_TEMPLATE.py
Customize: Fill in service details, API calls, and widget configuration
Save: Name file yourservice.py (no underscore prefix)
Restart: Restart container to load new integration
Configure: Service automatically appears in Setup page

The template includes extensive documentation and examples for:

Media library services (similar to Radarr)
Download clients (qBittorrent, Transmission, etc.)
Indexers and search services (Prowlarr, Jackett, etc.)
Custom services with unique requirements


## Webhook Setup

Webhooks let Episeerr respond to events automatically. **You only need the webhooks for features you want to use.**

### 1. Sonarr Webhook (Required)

**Enables:** Tag processing, auto-assignment, series addition detection

**Setup:**

1. **Sonarr** → Settings → Connect → Add → Webhook

2. **Configure:**
   - **Name:** Episeerr
   - **URL:** `http://your-episeerr:5002/sonarr-webhook`
   - **Method:** POST
   - **Triggers:** Enable ONLY "On Series Add" and "on Grab"

3. **Save**

```
[Sonarr webhook configuration screen]
- Shows URL field
- Shows "On Series Add" checkbox
- Shows Save button
```

**Test it:**
```bash
# Add a series in Sonarr and check logs
docker logs episeerr | grep "Received Sonarr webhook"
```

---

### 2. Tautulli Webhook (For Viewing Automation)

**Enables:** Next episode ready when you watch

**Configuration:**

**Option 1: Setup Page (Recommended) - v3.2.0+**
1. Go to `http://your-episeerr:5002/setup`
2. Scroll to **Tautulli** section
3. Enter Tautulli URL and API Key
4. Click **Test Connection** to verify
5. **Save**

**Option 2: Environment Variables**
```yaml
- TAUTULLI_URL=http://your-tautulli:8181
- TAUTULLI_API_KEY=your_tautulli_api_key
```

**Webhook Setup:**

1. **Tautulli** → Settings → Notification Agents → Add → Webhook

2. **Configure Webhook:**
   - **Webhook URL:** `http://your-episeerr:5002/webhook`
   - **Webhook Method:** POST

3. **Configure Triggers:**
   - **Triggers:** Enable ONLY "Watched"
   - **Conditions:** (Leave default)

4. **Configure Data:**

   **Text:**
   ```json
   {
     "plex_title": "{show_name}",
     "plex_season_num": "{season_num}",
     "plex_ep_num": "{episode_num}",
     "thetvdb_id": "{thetvdb_id}",
     "themoviedb_id": "{themoviedb_id}"
   }
   ```

5. **Save**

![webhook](https://github.com/Vansmak/OCDarr/assets/16037573/cf0db503-d730-4a9c-b83e-2d21a3430ece)![webhook2](https://github.com/Vansmak/OCDarr/assets/16037573/45be66c2-1869-49c1-8074-9081ed7c913b)
![webhook3](https://github.com/Vansmak/OCDarr/assets/16037573/24f02a75-2100-4b2a-9137-ce1e68803d1f)![webhook4](https://github.com/Vansmak/OCDarr/assets/16037573/f82198fc-e4c4-40ec-a9c7-551b2d8cdccd)
```
[Tautulli webhook configuration - Configuration tab]
[Tautulli webhook configuration - Triggers tab]
[Tautulli webhook configuration - Data tab with JSON]
```

**Important Settings:**

In Tautulli → Settings → General:
- **TV Episode Watched Percent:** Set between 50-95% (recommended: 80%)

**Test it:**
```bash
# Watch an episode to 80% and check logs
docker logs episeerr | grep "Received webhook"
```

---

### 3. Jellyfin Webhook (For Viewing Automation)

**Enables:** Next episode ready when you watch

Episeerr supports **two modes** for Jellyfin — pick one:

**Configuration (for both modes):**

**Option 1: Setup Page (Recommended) - v3.2.0+**
1. Go to `http://your-episeerr:5002/setup`
2. Scroll to **Jellyfin** section
3. Choose your mode and enter settings accordingly
4. Click **Test Connection** to verify
5. **Save**

**Option 2: Environment Variables** - See each mode below for specific variables

---

#### **Mode A: Real-Time (Recommended)**

Jellyfin sends a webhook on every progress update. Episeerr fires once when progress lands in the 50–55% window. No polling needed.

**Webhook Setup:**
1. **Jellyfin** → Dashboard → Plugins → Webhooks → Add Generic Destination
2. **Configure:**
   - **Webhook Name:** Episeerr Episode Tracking
   - **Webhook URL:** `http://your-episeerr:5002/jellyfin-webhook`
   - **Notification Type:** Select ONLY **"Playback Progress"**
   - **User Filter:** Your username (recommended)
   - **Item Type:** Episodes
   - **Send All Properties:** ✅ Enabled
3. **Save**

**Environment Variables (if not using Setup Page):**
```yaml
- JELLYFIN_URL=http://your-jellyfin:8096
- JELLYFIN_API_KEY=your_api_key
- JELLYFIN_USER_ID=your_username  # REQUIRED
- JELLYFIN_TRIGGER_MIN=50.0
- JELLYFIN_TRIGGER_MAX=55.0
```

<img width="738" height="1046" alt="image" src="https://github.com/user-attachments/assets/8fb610e8-ca62-4113-be7c-b7a1aedcae0c" />
<img width="794" height="879" alt="image" src="https://github.com/user-attachments/assets/7027be3a-3b75-407e-86e4-06a4c7280960" />

```
[Jellyfin webhook plugin configuration]
[Shows Playback Progress selected]
[Shows User Filter field]
```

---

#### **Mode B: Polling**

Jellyfin sends a webhook on session start. Episeerr then polls the Jellyfin `/Sessions` API every 15 minutes until the trigger percentage is hit. Useful if PlaybackProgress webhooks are unreliable on your setup.

**Webhook Setup:**
1. **Jellyfin** → Dashboard → Plugins → Webhooks → Add Generic Destination
2. **Configure:**
   - **Webhook URL:** `http://your-episeerr:5002/jellyfin-webhook`
   - **Notification Type:** Select **"Session Start"**
   - **User Filter:** Your username
   - **Item Type:** Episodes

**Environment Variables (if not using Setup Page):**
```yaml
- JELLYFIN_URL=http://your-jellyfin:8096
- JELLYFIN_API_KEY=your_api_key
- JELLYFIN_USER_ID=your_username  # REQUIRED
- JELLYFIN_TRIGGER_PERCENTAGE=50.0
- JELLYFIN_POLL_INTERVAL=900  # seconds (15 minutes)
```

---

**Which Jellyfin mode should you use?**

| Option | Best For | Processing | Jellyfin Webhooks Needed |
|--------|----------|------------|--------------------------|
| **A: Real-Time** | Most users | Immediate at 50–55% | PlaybackProgress (continuous) |
| **B: Polling** | Unreliable progress webhooks | Up to 15-min delay | Session Start (one-shot) |

**Test it:**
```bash
# Watch an episode past 50% and check logs
docker logs episeerr | grep "Processing Jellyfin"
```

---

### 4. Emby Webhook (For Viewing Automation)

**Enables:** Next episode ready when you watch

Emby doesn't send continuous progress webhooks like Jellyfin's PlaybackProgress, so Episeerr uses **polling only**. On `playback.start`, Episeerr spawns a background thread that queries the Emby `/Sessions` API every 15 minutes until your watch progress hits the trigger threshold. This handles autoplay correctly — if E1 finishes and E2 auto-starts without a `playback.stop` firing for E1, the poll already caught E1 at 50% and triggered the next episode search.

**Configuration:**

**Option 1: Setup Page (Recommended) - v3.2.0+**
1. Go to `http://your-episeerr:5002/setup`
2. Scroll to **Emby** section
3. Enter:
   - Emby URL (e.g., `http://emby:8096`)
   - API Key (from Emby → Settings → Advanced → Security)
   - Username (must match the user watching content)
   - Trigger Percentage (default: 50.0%)
   - Poll Interval (default: 900 seconds / 15 minutes)
4. Click **Test Connection** to verify
5. **Save**

**Option 2: Environment Variables**
```yaml
- EMBY_URL=http://your-emby:8096
- EMBY_API_KEY=your_emby_api_key
- EMBY_USER_ID=your_username  # REQUIRED — must match the Emby user
- EMBY_TRIGGER_PERCENTAGE=50.0
- EMBY_POLL_INTERVAL=900  # seconds (15 minutes)
```

**Webhook Setup:**
1. **Emby** → User Preferences (top-right avatar) → Notifications → Webhooks → Add Webhook
2. **Configure:**
   - **Webhook Name:** Episeerr Episode Tracking
   - **Webhook URL:** `http://your-episeerr:5002/emby-webhook`
   - **Events:** Enable **"playback.start"** and **"playback.stop"**
   - *(No item type filter in Emby — it sends all events; Episeerr filters to Episodes internally)*
3. **Save**

> **Note:** The webhook is configured per-user in Emby, not server-wide like Jellyfin's plugin. Make sure you're configuring it for the user account that watches content.

**Test it:**
```bash
# Watch an episode past 50% and check logs
docker logs episeerr | grep "Processing Emby"
```

**How it works:**

| Event | What Episeerr Does |
|-------|-------------------|
| `playback.start` | Starts polling `/Sessions` for this session every `POLL_INTERVAL` seconds |
| Poll hits `TRIGGER_PERCENTAGE` | Fires episode processing, marks session as handled |
| `playback.stop` | Stops the polling thread. If already processed by poll, skips. If not yet hit threshold, checks final position one last time. |

**Test it:**
```bash
# Watch an episode past 50% and check logs
docker logs episeerr | grep "Processing Emby"
```

---

### 4. Jellyseerr/Overseerr Webhook (Optional)

**Enables:** Season-specific automation with direct rule tags

**What it does:**
- Captures which season you requested
- Allows rules to start from that season (not Season 1)

**Setup:**

1. **Jellyseerr/Overseerr** → Settings → Notifications → Webhooks

2. **Add Webhook:**
   - **Webhook URL:** `http://your-episeerr:5002/seerr-webhook`
   - **Notification Types:** Enable "Request Approved"

3. **Save**

<img width="1869" height="1040" alt="image" src="https://github.com/user-attachments/assets/272caa3c-17d1-44c4-8990-ed459b73986e" />

```
[Jellyseerr webhook configuration]
[Shows URL field and Request Approved checkbox]
```

**Test it:**
```bash
# Request a series in Jellyseerr and check logs
docker logs episeerr | grep "Stored.*request"
```

---

## How to Use

### Create Your First Rule

**Rules control what happens when you watch episodes.**

1. **Open Episeerr:** `http://your-server:5002`

2. **Go to Rules** → Create New Rule

3. **Configure:**

   | Setting | What It Does | Example |
   |---------|--------------|---------|
   | **Name** | Rule identifier | "binge_watcher" |
   | **GET** | Episodes to prepare | "3 episodes" = next 3 ready |
   | **KEEP** | Episodes to retain | "1 episode" = keep only last watched |
   | **Action** | Monitor or Search | "Search" = actively download |

4. **Optional Time-Based Cleanup:**

   | Setting | What It Does | Example |
   |---------|--------------|---------|
   | **Grace Watched** | Delete old watched episodes after X days | "7 days" |
   | **Grace Unwatched** | Delete unwatched episodes after X days | "14 days" |
   | **Dormant** | Delete everything after X days inactive | "30 days" |

5. **Mark as Default Rule** (if this is your main rule)

6. **Save**

```
[Rule creation form showing all fields]
[Example configuration for binge watcher]
```

---

### Add Series (Three Ways)

#### **Method 1: Auto-Assign (Passive)**

Best for: Letting Sonarr/Jellyseerr control initial downloads

1. **Enable in Episeerr:**
   - Settings → Global Settings
   - Enable "Auto-assign new series to default rule"

2. **Add series normally in Sonarr** (no tags)

3. **Series automatically joins default rule**

4. **Waits for first watch** before processing

**Use case:** You request Season 3 from Jellyseerr → Let it download → Episeerr manages after first watch

---

#### **Method 2: Direct Rule Tags (Immediate)**

Best for: Immediate processing with specific rules

1. **In Sonarr**, add series with tag `episeerr_[rule_name]`
   - Example: `episeerr_binge_watcher`
   - Example: `episeerr_one_at_a_time`

2. **Episeerr processes immediately:**
   - Applies GET rule
   - Monitors/searches episodes
   - Removes tag

**Use case:** You want a specific rule applied right away

---

#### **Method 3: Manual Assignment**

Best for: Existing series or manual control

1. **Episeerr** → Series Management

2. **Select series** → Choose rule → Assign

**Use case:** Adding existing series to Episeerr

---

### Episode Selection

**Choose specific episodes manually across seasons.**

#### **Setup (One-time):**

1. **Sonarr** → Settings → Profiles → Release Profiles → Add

2. **Configure:**
   - **Name:** Episeerr Episode Selection Delay
   - **Delay:** `10519200` (20 years)
   - **Tags:** `episeerr_select`

3. **Save**

<img width="720" height="608" alt="image" src="https://github.com/user-attachments/assets/c33f6443-d00c-4446-8d00-fddb1b42fff7" />

```
[Sonarr release profile configuration]
[Shows delay field set to 10519200]
```

#### **Usage:**

1. **Add series to Sonarr** with `episeerr_select` tag

2. **Episeerr** → Pending Items → Select Seasons

3. **Choose specific episodes**

4. **Submit** → Only those episodes monitored

```
[Episode selection interface showing seasons and episodes]
[Checkboxes for each episode]
```

---

## Features Explained

### 🎯 Episode Selection

**Manual episode picking across multiple seasons.**

**Use cases:**
- Try pilots without downloading full seasons
- Skip filler episodes
- Download specific arcs
- Selective backlog management

**How it works:**
1. Add series with `episeerr_select` tag
2. All episodes unmonitored
3. Choose which ones you want
4. Only selected episodes download

---

### ⚡ Viewing Automation

**Next episode ready when you watch.**

**Use cases:**
- Binge watching (always 2-3 episodes ahead)
- Weekly shows (stay current)
- Automatic queue management

**How it works:**
1. Watch S1E5
2. Webhook fires to Episeerr
3. Rule applied: GET next 2 episodes
4. S1E6, S1E7 now monitored/searched
5. KEEP rule: Delete S1E1-S1E4 (outside keep window)

**Example flow:**
```
Watch E5 → Get E6, E7 → Keep E5 → Delete E1-E4
```

---

### 💾 Storage Management

**Automatic cleanup based on time and viewing activity.**

**Use cases:**
- Limited storage (seedboxes, budget servers)
- Inactive show cleanup
- Abandoned series removal

**How it works:**

| Cleanup Type | Trigger | What It Does |
|--------------|---------|--------------|
| **Grace Watched** | X days inactive | Deletes old watched episodes, keeps last as bookmark |
| **Grace Unwatched** | X days inactive | Deletes unwatched episodes, keeps first as bookmark |
| **Dormant** | X days + low storage | Deletes EVERYTHING from abandoned shows |

**Storage Gate:**
- Set threshold: "Keep 20GB free"
- Cleanup only runs when below threshold
- Stops when back above threshold

**Bookmarks:**
- Grace cleanup ALWAYS keeps at least 1 episode
- You never lose your viewing position

---

## Configuration Examples

### Binge Watcher

**Profile:** Always 3 episodes ahead, aggressive cleanup

```yaml
Rule Name: binge_watcher
GET: 3 episodes
KEEP: 1 episode
Action: Search
Grace Watched: 7 days
Grace Unwatched: 14 days
Dormant: 30 days
```

**What happens:**
- Watch E5 → E6, E7, E8 ready
- Keep E5, delete E1-E4
- After 7 days inactive → Delete E5 (keeps bookmark)
- After 30 days → Delete entire show

---

### Current Shows

**Profile:** Stay current, keep buffer

```yaml
Rule Name: weekly
GET: 1 episode
KEEP: 3 episodes
Action: Monitor
Grace Watched: 30 days
Grace Unwatched: null
Dormant: 90 days
```

**What happens:**
- Watch E5 → E6 monitored
- Keep E3, E4, E5
- After 30 days → Cleanup old episodes
- Unwatched episodes never auto-deleted

---

### Protected Series

**Profile:** Never delete, keep everything

```yaml
Rule Name: protected
GET: All
KEEP: All
Action: Search
Grace Watched: null
Grace Unwatched: null
Dormant: null
```

**What happens:**
- Watch E5 → All future episodes monitored
- Nothing ever deleted
- Perfect for rewatchable favorites

---

### Season Binger

**Profile:** Watch whole seasons, rotate

```yaml
Rule Name: season_binger
GET: 1 season
KEEP: 1 season
Action: Search
Grace Watched: 14 days
Grace Unwatched: null
Dormant: 90 days
```

**What happens:**
- Watch S2E1 → All of S3 monitored
- Keep all of S2, delete S1
- After 14 days → Cleanup S2
- Perfect for binging complete seasons

---

## Troubleshooting

### Container Won't Start

**Check:**
```bash
docker logs episeerr
```

**Common issues:**
- Missing required environment variables
- Invalid Sonarr URL format (remove trailing slash)
- Wrong TMDB key type (need Read Access Token, not API key)

**Fix:**
```yaml
# Correct format:
- SONARR_URL=http://sonarr:8989  # No trailing slash
- TMDB_API_KEY=eyJhbG...  # Read Access Token (long string)
```

---

### Webhooks Not Working

**Test webhook reception:**
```bash
# Watch logs live
docker logs -f episeerr | grep webhook

# Check recent webhook events
docker logs episeerr | grep "Received.*webhook" | tail -20
```

**Common issues:**

| Problem | Check | Solution |
|---------|-------|----------|
| No webhooks received | Network connectivity | Can webhook sender reach Episeerr? |
| Webhooks received but nothing happens | Series assignment | Is series in a rule? |
| Wrong episodes managed | Webhook data | Check logs for series name matching |

**Verify webhook URLs:**
- Sonarr: `http://episeerr:5002/sonarr-webhook`
- Tautulli: `http://episeerr:5002/webhook`
- Jellyfin: `http://episeerr:5002/jellyfin-webhook`
- Emby: `http://episeerr:5002/emby-webhook`
- Jellyseerr: `http://episeerr:5002/seerr-webhook`

> **Configuration:** Use the `/setup` page to configure services with URLs and API keys (recommended), or use environment variables.

---

### Episodes Not Monitoring

**Check series assignment:**
```
Episeerr → Series Management
```

**Verify:**
1. Series is listed under a rule
2. Rule has GET settings configured
3. Watch an episode to trigger

**Manual trigger:**
```bash
# Watch an episode, then check logs
docker logs episeerr | grep "Monitored.*episodes"
```

---

### Tags Not Working

**For direct rule tags (`episeerr_binge_watcher`):**

1. **Verify tag exists in Sonarr:**
   - Sonarr → Settings → Tags
   - Tag must match rule name exactly

2. **Check Sonarr webhook:**
   - Settings → Connect → Webhook
   - URL: `http://episeerr:5002/sonarr-webhook`
   - Trigger: "On Series Add" enabled

3. **Check logs:**
   ```bash
   docker logs episeerr | grep "Processing.*with tag"
   ```

---

### Jellyfin Not Working

**Most common issue: Missing JELLYFIN_USER_ID**

```yaml
# REQUIRED for Jellyfin
- JELLYFIN_USER_ID=your_username  # This is your Jellyfin login name
```

**Check webhook plugin:**
```
Jellyfin → Dashboard → Plugins → Webhooks
```

**Verify:**
- Webhook URL is correct
- Proper notification types selected
- User filter matches your username

**Test:**
```bash
# Watch episode past 50% and check logs
docker logs episeerr | grep "Jellyfin"
```

---

## Screenshots

<img width="1869" height="1040" alt="image" src="https://github.com/user-attachments/assets/b03ad3a3-c5eb-4805-a3ec-929a69469d82" />


## FAQ

### General

**Q: Do I need all the webhooks?**  
A: No! Only set up webhooks for features you want:
- Episode Selection only: Sonarr webhook
- Viewing Automation: Sonarr + Tautulli/Jellyfin webhooks
- Full automation: All webhooks

**Q: Can I use both Tautulli and Jellyfin?**  
A: No need - choose one based on your media server (Plex = Tautulli, Jellyfin = Jellyfin webhook)

**Q: What's the difference between tags and auto-assign?**  
A:
- **Tags** (`episeerr_[rule_name]`): Immediate processing with specific rules
- **Auto-assign**: Passive assignment, waits for first watch

**Q: Will this download my entire library?**  
A: No! Only series assigned to rules are managed. Use episode selection or auto-assign to control what gets managed.

---

### Tags & Assignment

**Q: Why did my tag disappear?**  
A: Tags are temporary signals. After processing, the tag is removed. Check Series Management to verify assignment succeeded.

**Q: How do I use tags for specific rules?**  
A: Tag format is `episeerr_[rule_name]`. If you have a rule named "binge_watcher", use tag `episeerr_binge_watcher`.

**Q: Can I change which rule a series uses?**  
A: Yes! Either:
- Change tag in Sonarr (tag drift detection will update Episeerr)
- Manually reassign in Series Management

---

### Viewing Automation

**Q: Episodes aren't updating when I watch?**  
A: Check:
1. Is viewing webhook configured? (Tautulli or Jellyfin)
2. Is series assigned to a rule?
3. Check logs: `docker logs episeerr | grep webhook`

**Q: How much do I need to watch for it to trigger?**  
A:
- Tautulli: Set in Tautulli settings (50-95%, recommended 80%)
- Jellyfin: 50% by default (configurable via `JELLYFIN_TRIGGER_PERCENTAGE`)

**Q: Can different people watch different seasons?**  
A: Yes! Enable "Grace Period Scope: Per Season" in rule settings for independent season tracking.

---

### Deletions

**Q: Will I lose my place if episodes get deleted?**  
A: No! Grace cleanup always keeps:
- Grace Watched: Last watched episode (bookmark)
- Grace Unwatched: First unwatched episode (resume point)

**Q: How do I test without deleting anything?**  
A: Enable "Global Dry Run Mode" in Settings. Review deletions in Pending Deletions before approving.

**Q: Why are episodes being deleted immediately?**  
A: KEEP rule deletes in real-time when watching. This is by design. To prevent this:
- Increase KEEP count
- Or disable KEEP entirely (set to "All")

**Q: What's the difference between Grace and Dormant?**  
A:
- **Grace:** Time-based cleanup of specific episode types (watched/unwatched)
- **Dormant:** Nuclear option - deletes EVERYTHING from completely abandoned shows

---

### Jellyfin Specific

**Q: Which Jellyfin mode should I use?**  
A: Real-time (Playback Progress) for most users. Use polling if you have webhook reliability issues.

**Q: Do I need to disable any modes?**  
A: No! System auto-detects based on environment variables. Just set the vars for your chosen mode.

**Q: JELLYFIN_USER_ID - What do I put here?**  
A: Your Jellyfin username (the name you use to log in). This is REQUIRED for Jellyfin integration.

---

### Storage Management

**Q: How do I set up storage cleanup?**  
A: Settings → Global Settings → Set "Storage Threshold" (e.g., 20GB). Cleanup only runs when below threshold.

**Q: Will it delete shows I'm actively watching?**  
A: No! Grace periods reset when you watch episodes. Only inactive shows are cleaned up.

**Q: Can I protect certain shows?**  
A: Yes! Create a rule with empty Grace and Dormant settings, assign those shows to it.

---

## Support

### Get Help

- 📖 **In-App Documentation:** `http://your-episeerr:5002/documentation`
- 🐛 **Report Issues:** [GitHub Issues](https://github.com/Vansmak/episeerr/issues)
- 💬 **Discussions:** [GitHub Discussions](https://github.com/Vansmak/episeerr/discussions)
- ☕ **Support Development:** [Buy Me A Coffee](https://buymeacoffee.com/vansmak)

### Logs Location

```bash
# Docker
docker logs episeerr

# Logs directory
./logs/app.log

# Live monitoring
docker logs -f episeerr
```

### Common Log Searches

```bash
# Check webhook reception
docker logs episeerr | grep "Received.*webhook"

# Check rule processing
docker logs episeerr | grep "Monitored.*episodes"

# Check errors
docker logs episeerr | grep "Error\|Failed"

# Check specific series
docker logs episeerr | grep "Breaking Bad"
```

---

## Contributing

Contributions welcome! Please open an issue or pull request on GitHub.

---

## License

[MIT License](LICENSE)

---

## Acknowledgments

Built with AI assistance as a development tool. All architecture, design decisions, and problem-solving are human-driven. Code is open source for transparency and community review.

---

**Ready to get started?** [Jump to Quick Start](#quick-start) ⬆️
