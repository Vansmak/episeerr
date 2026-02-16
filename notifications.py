"""
Episeerr Notification System
Handles Discord notifications for pending searches and selection requests
"""

import requests
import logging
from datetime import datetime

from logging_config import main_logger as logger

# Config will be passed in from episeerr.py
NOTIFICATIONS_ENABLED = False
DISCORD_WEBHOOK_URL = ''
EPISEERR_URL = 'http://localhost:5002'
SONARR_URL = 'http://localhost:8989'

def init_notifications(notifications_enabled, discord_webhook_url, episeerr_url, sonarr_url):
    """Initialize notification config - called from episeerr.py on startup"""
    global NOTIFICATIONS_ENABLED, DISCORD_WEBHOOK_URL, EPISEERR_URL, SONARR_URL
    NOTIFICATIONS_ENABLED = notifications_enabled
    DISCORD_WEBHOOK_URL = discord_webhook_url
    EPISEERR_URL = episeerr_url
    SONARR_URL = sonarr_url


def send_notification(notification_type, **data):
    """
    Central notification dispatcher
    
    Args:
        notification_type: Type of notification to send
        **data: Context data for the notification
        
    Supported types:
        - episode_search_pending: Search requested for episode
        - selection_pending: New episeerr_select request
    
    Returns:
        Discord message ID if sent successfully, None otherwise
    """
    if not NOTIFICATIONS_ENABLED:
        logger.debug(f"Notifications disabled, skipping {notification_type}")
        return None
    
    if not DISCORD_WEBHOOK_URL:
        logger.warning("Discord webhook URL not configured")
        return None
    
    try:
        # Build message based on type
        if notification_type == "episode_search_pending":
            message = build_search_pending_message(
                series=data['series'],
                season=data['season'],
                episode=data['episode'],
                air_date=data.get('air_date'),
                series_id=data.get('series_id')
            )
        
        elif notification_type == "selection_pending":
            message = build_selection_pending_message(
                series=data['series'],
                series_id=data.get('series_id')
            )
        
        else:
            logger.warning(f"Unknown notification type: {notification_type}")
            return None
        
        # Send notification and get message ID
        message_id = send_discord_webhook(message)
        logger.info(f"Sent {notification_type} notification, message_id: {message_id}")
        return message_id
        
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        return None


def build_search_pending_message(series, season, episode, air_date=None, series_id=None):
    """Build Discord embed for pending episode search"""
    
    # Format air date
    air_date_str = "Unknown"
    if air_date:
        try:
            dt = datetime.fromisoformat(air_date.replace('Z', '+00:00'))
            air_date_str = dt.strftime("%B %d, %Y")
        except:
            air_date_str = str(air_date)
    
    # Build Sonarr link
    sonarr_link = ""
    if series_id and SONARR_URL:
        series_slug = series.lower().replace(' ', '-').replace("'", "")
        sonarr_link = f"{SONARR_URL}/series/{series_slug}/season-{season}"
    
    fields = [
        {
            "name": "Episode",
            "value": f"{series} S{season:02d}E{episode:02d}",
            "inline": False
        },
        {
            "name": "Air Date",
            "value": air_date_str,
            "inline": True
        },
        {
            "name": "ℹ️ Status",
            "value": "Waiting for Sonarr to find and grab this episode. This message will disappear if found.",
            "inline": False
        }
    ]
    
    if sonarr_link:
        fields.append({
            "name": "🔍 Manual Search",
            "value": f"[Open in Sonarr]({sonarr_link})",
            "inline": False
        })
    
    return {
        "embeds": [{
            "title": "🔍 Episode Search Pending",
            "description": "Sonarr is searching for this episode",
            "color": 3447003,  # Blue - informational
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat()
        }]
    }


def build_selection_pending_message(series, series_id=None):
    """Build Discord embed for pending episode selection"""
    
    episeerr_link = ""
    if EPISEERR_URL:
        episeerr_link = f"{EPISEERR_URL}/episeerr"
    
    fields = [
        {
            "name": "Series",
            "value": series,
            "inline": False
        }
    ]
    
    if episeerr_link:
        fields.append({
            "name": "🔗 Select Episodes",
            "value": f"[Open Episeerr]({episeerr_link})",
            "inline": False
        })
    
    return {
        "embeds": [{
            "title": "📋 New Episode Selection Request",
            "description": "A show is waiting for episode selection",
            "color": 3447003,  # Blue
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat()
        }]
    }


def send_discord_webhook(message):
    """Send notification to Discord webhook and return message ID"""
    if not DISCORD_WEBHOOK_URL:
        return None
    
    try:
        # Add ?wait=true to get the message back
        url = DISCORD_WEBHOOK_URL
        if '?' not in url:
            url += '?wait=true'
        else:
            url += '&wait=true'
        
        response = requests.post(url, json=message, timeout=10)
        response.raise_for_status()
        
        # Get message ID from response
        message_data = response.json()
        message_id = message_data.get('id')
        
        logger.info(f"📤 Sent Discord message ID: {message_id}")
        return message_id
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Discord webhook failed: {e}")
        return None


def delete_discord_message(message_id):
    """Delete a Discord webhook message"""
    if not DISCORD_WEBHOOK_URL or not message_id:
        return False
    
    try:
        # Extract webhook ID and token from URL
        # Format: https://discord.com/api/webhooks/{webhook_id}/{webhook_token}
        parts = DISCORD_WEBHOOK_URL.rstrip('/').split('/')
        webhook_id = parts[-2]
        webhook_token = parts[-1].split('?')[0]  # Remove query params if present
        
        delete_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"
        
        response = requests.delete(delete_url)
        response.raise_for_status()
        
        logger.info(f"🗑️ Deleted Discord message {message_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to delete Discord message: {e}")
        return False