# Installation

Get Episeerr running in 5 minutes.

- [Installation](#installation)
  - [Requirements](#requirements)
  - [Docker Setup](#docker-setup)
  - [Unraid Setup](#unraid-setup)
  - [Choose Your Features](#choose-your-features)
    - [1. Episode Selection Only](#1-episode-selection-only)
    - [2. Rule-Based Episode Management](#2-rule-based-episode-management)
    - [3. Storage Cleanup](#3-storage-cleanup)
    - [All Three Together](#all-three-together)
  - [Troubleshooting](#troubleshooting)

## Requirements

- **Sonarr v3+** (required)
- **Docker** (recommended)
- **TMDB API Key** - Free from [themoviedb.org](https://www.themoviedb.org/) → Settings → API → Developer → **API Read Access Token**

## Docker Setup

```yaml
services:
  episeerr:
    image: vansmak/episeerr:latest
    environment:
      # Required
      - SONARR_URL=http://your-sonarr:8989
      - SONARR_API_KEY=your_sonarr_api_key
      - TMDB_API_KEY=your_tmdb_read_access_token
      
      # Optional - Add as needed for features you want
      - TAUTULLI_URL=http://your-tautulli:8181
      - TAUTULLI_API_KEY=your_tautulli_key
      - JELLYSEERR_URL=http://your-jellyseerr:5055
      - JELLYSEERR_API_KEY=your_jellyseerr_key
      
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
      - ./data:/app/data
      - ./temp:/app/temp
    ports:
      - "5002:5002"
    restart: unless-stopped
```

Start: `docker-compose up -d`

Access: `http://your-server:5002`

## Unraid Setup

Create a file `my-episeer.xml` with the following content:

```xml
<Repository>vansmak/episeerr:latest</Repository>
  <Registry>https://hub.docker.com/r/vansmak/episeerr</Registry>
  <Network>bridge</Network>
  <MyIP/>
  <Shell>sh</Shell>
  <Privileged>false</Privileged>
  <Support>https://github.com/Vansmak/episeerr/issues</Support>
  <Project>https://github.com/Vansmak/episeerr</Project>
  <Overview>Smart episode management for Sonarr</Overview>
  <Category>Other:</Category>
  <WebUI>http://[IP]:[PORT:5002]</WebUI>
  <TemplateURL/>
  <Icon>https://raw.githubusercontent.com/Vansmak/episeerr/refs/heads/main/static/logo_icon.png</Icon>
  <ExtraParams/>
  <PostArgs/>
  <CPUset/>
  <DateInstalled/>
  <DonateText/>
  <DonateLink/>
  <Requires/>
  
  <!-- Ports -->
  <Config Name="WebUI Port" Target="5002" Default="5002" Mode="tcp" Type="Port" Display="always" Required="true" Description="Episeerr WebUI (Port 5002)"/>
  
  <!-- Paths -->
  <Config Name="Config" Target="/app/config" Default="/mnt/user/appdata/episeerr/config" Mode="rw" Type="Path" Display="always" Required="true" Description="App Configuration"/>
  <Config Name="Logs" Target="/app/logs" Default="/mnt/user/appdata/episeerr/logs" Mode="rw" Type="Path" Display="always" Required="true" Description="Logs"/>
  <Config Name="Data" Target="/app/data" Default="/mnt/user/appdata/episeerr/data" Mode="rw" Type="Path" Display="always" Required="true" Description="Application data files"/>
  <Config Name="Temp" Target="/app/temp" Default="/mnt/user/appdata/episeerr/temp" Mode="rw" Type="Path" Display="always" Required="true" Description="Temporary files location"/>
  
  <!-- Required Configuration -->
  <Config Name="SONARR_URL" Target="SONARR_URL" Default="" Type="Variable" Display="always" Required="true" Description="Sonarr Base URL"/>
  <Config Name="SONARR_API_KEY" Target="SONARR_API_KEY" Default="" Type="Variable" Display="always" Required="true" Description="Sonarr API Key"/>
  <Config Name="TMDB_API_KEY" Target="TMDB_API_KEY" Default="" Type="Variable" Display="always" Required="true" Description="TMDB API Key"/>
  
  <!-- Optional: Request Integration -->
  <Config Name="JELLYSEERR_URL" Target="JELLYSEERR_URL" Default="" Type="Variable" Display="always" Required="false" Description="Jellyseerr URL (optional)"/>
  <Config Name="JELLYSEERR_API_KEY" Target="JELLYSEERR_API_KEY" Default="" Type="Variable" Display="always" Required="false" Description="Jellyseerr API Key (optional)"/>
  <Config Name="OVERSEERR_URL" Target="OVERSEERR_URL" Default="" Type="Variable" Display="always" Required="false" Description="Overseerr URL (optional - alternative to Jellyseerr)"/>
  <Config Name="OVERSEERR_API_KEY" Target="OVERSEERR_API_KEY" Default="" Type="Variable" Display="always" Required="false" Description="Overseerr API Key (optional)"/>
  
  <!-- Optional: Viewing-based Rules -->
  <Config Name="TAUTULLI_URL" Target="TAUTULLI_URL" Default="" Type="Variable" Display="always" Required="false" Description="Tautulli URL (optional - for viewing-based rules)"/>
  <Config Name="TAUTULLI_API_KEY" Target="TAUTULLI_API_KEY" Default="" Type="Variable" Display="always" Required="false" Description="Tautulli API Key (optional)"/>
  <Config Name="JELLYFIN_URL" Target="JELLYFIN_URL" Default="" Type="Variable" Display="always" Required="false" Description="Jellyfin URL (optional - alternative to Tautulli)"/>
  <Config Name="JELLYFIN_API_KEY" Target="JELLYFIN_API_KEY" Default="" Type="Variable" Display="always" Required="false" Description="Jellyfin API Key (optional)"/>
  
  <!-- Optional: Additional Settings -->
  <Config Name="EPISEERR_AUTO_CREATE_TAGS" Target="EPISEERR_AUTO_CREATE_TAGS" Default="false" Type="Variable" Display="always" Required="false" Description="Automatically create tags in Sonarr (true/false)"/>
  <Config Name="LOG_PATH" Target="LOG_PATH" Default="logs/app.log" Type="Variable" Display="always" Required="false" Description="Log file path"/>
  <Config Name="FLASK_DEBUG" Target="FLASK_DEBUG" Default="false" Type="Variable" Display="always" Required="false" Description="Flask debug mode (true/false)"/>
  
  <TailscaleStateDir/>
</Container>
```

To make it available in the Unraid Apps section, do the following:

**Preparations:**

- In the Unraid Menu bar click on `Main`
- Click on <img width="18" height="22" alt="Image" src="https://github.com/user-attachments/assets/8aafb006-c407-4689-96da-7a4b1aa34be1" /> next to `Flash` in the `Boot Device` section
- Navigate to config -> plugins -> comunity.applications -> private
- Create a folder called `episeerr`
- Upload the `my-episeerr.xml` file you created before into that folder

**Installation:**

- In the Unraid Menu Bar click on `Apps`
- Search for `episeerr`
- Click on `Install`
- Fill out mandatory and if needed optional fields

## Choose Your Features

Episeerr has 3 independent features. Pick what you want:

### 1. Episode Selection Only

**What**: Choose specific episodes manually, try pilots
**Setup**: [Episode Selection Guide](episode-selection.md)

### 2. Rule-Based Episode Management

**What**: Next episode ready when you watch, automatic management
**Setup**: [Rule-Based Management Guide](rules-guide.md)

### 3. Storage Cleanup

**What**: Automatic cleanup based on time and viewing
**Setup**: [Storage Cleanup Guide](global_storage_gate_guide.md)

### All Three Together

Follow all three guides. Features work independently or together.

---

## Troubleshooting

**Container won't start**: Check environment variables  
**Can't connect to Sonarr**: Verify URL format (no trailing slash)  
**TMDB errors**: Use the Read Access Token, not the API key

**Next**: Choose your feature guide above
