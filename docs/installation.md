# Installation

Get Episeerr running in 5 minutes.

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
