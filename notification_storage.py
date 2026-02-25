"""
Notification storage helpers
Manages pending search notifications in a simple JSON file
"""

import os
import json
import logging
from datetime import datetime, timedelta

from logging_config import main_logger as logger

NOTIFICATION_STORAGE = '/config/pending_notifications.json'
AIRED_NOTIFICATION_STORAGE = '/data/aired_notifications.json'


def store_notification(episode_id, message_id):
    """
    Store Discord message ID for an episode search
    
    Args:
        episode_id: Sonarr episode ID
        message_id: Discord message ID
    """
    try:
        notifications = {}
        if os.path.exists(NOTIFICATION_STORAGE):
            with open(NOTIFICATION_STORAGE, 'r') as f:
                notifications = json.load(f)
        
        notifications[str(episode_id)] = {
            'message_id': message_id,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        os.makedirs(os.path.dirname(NOTIFICATION_STORAGE), exist_ok=True)
        with open(NOTIFICATION_STORAGE, 'w') as f:
            json.dump(notifications, f, indent=2)
            
        logger.info(f"💾 Stored notification for episode {episode_id}: {message_id}")
        
    except Exception as e:
        logger.error(f"Failed to store notification: {e}")


def get_and_remove_notification(episode_id):
    """
    Get and remove notification for an episode
    
    Args:
        episode_id: Sonarr episode ID
        
    Returns:
        Discord message ID if found, None otherwise
    """
    try:
        if not os.path.exists(NOTIFICATION_STORAGE):
            return None
            
        with open(NOTIFICATION_STORAGE, 'r') as f:
            notifications = json.load(f)
        
        notification = notifications.pop(str(episode_id), None)
        
        with open(NOTIFICATION_STORAGE, 'w') as f:
            json.dump(notifications, f, indent=2)
        
        if notification:
            message_id = notification.get('message_id')
            logger.info(f"📋 Retrieved notification for episode {episode_id}: {message_id}")
            return message_id
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to get notification: {e}")
        return None
def notification_exists(episode_id):
    """Check if a notification already exists for an episode"""
    try:
        if not os.path.exists(NOTIFICATION_STORAGE):
            return False

        with open(NOTIFICATION_STORAGE, 'r') as f:
            notifications = json.load(f)

        return str(episode_id) in notifications

    except Exception as e:
        logger.error(f"Failed to check notification existence: {e}")
        return False


# --- Aired-but-not-downloaded notification tracking ---

def aired_notification_exists(episode_id):
    """Check if an aired-not-downloaded notification has already been sent for an episode"""
    try:
        if not os.path.exists(AIRED_NOTIFICATION_STORAGE):
            return False

        with open(AIRED_NOTIFICATION_STORAGE, 'r') as f:
            notified = json.load(f)

        return str(episode_id) in notified

    except Exception as e:
        logger.error(f"Failed to check aired notification existence: {e}")
        return False


def store_aired_notification(episode_id):
    """Record that an aired-not-downloaded notification was sent for an episode"""
    try:
        notified = {}
        if os.path.exists(AIRED_NOTIFICATION_STORAGE):
            with open(AIRED_NOTIFICATION_STORAGE, 'r') as f:
                notified = json.load(f)

        notified[str(episode_id)] = datetime.utcnow().isoformat()

        os.makedirs(os.path.dirname(AIRED_NOTIFICATION_STORAGE), exist_ok=True)
        with open(AIRED_NOTIFICATION_STORAGE, 'w') as f:
            json.dump(notified, f, indent=2)

        logger.debug(f"Stored aired notification for episode {episode_id}")

    except Exception as e:
        logger.error(f"Failed to store aired notification: {e}")


def cleanup_old_aired_notifications():
    """Remove entries older than 30 days from the aired notifications file"""
    try:
        if not os.path.exists(AIRED_NOTIFICATION_STORAGE):
            return

        with open(AIRED_NOTIFICATION_STORAGE, 'r') as f:
            notified = json.load(f)

        cutoff = datetime.utcnow() - timedelta(days=30)
        pruned = {
            ep_id: ts for ep_id, ts in notified.items()
            if datetime.fromisoformat(ts) > cutoff
        }

        removed = len(notified) - len(pruned)
        if removed:
            with open(AIRED_NOTIFICATION_STORAGE, 'w') as f:
                json.dump(pruned, f, indent=2)
            logger.info(f"Cleaned up {removed} old aired notification entries")

    except Exception as e:
        logger.error(f"Failed to cleanup aired notifications: {e}")