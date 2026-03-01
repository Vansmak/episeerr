"""
Emby Integration for Episeerr
Provides: Webhook-triggered polling for watch detection
"""

import os
import json
import requests
import logging
import threading
import time
import subprocess
from typing import Dict, Any, Optional, List
from flask import Blueprint, request, jsonify
from datetime import datetime
from integrations.base import ServiceIntegration

logger = logging.getLogger(__name__)

# ==========================================
# Session Tracking (Polling State)
# ==========================================

# Session polling state
active_emby_sessions = {}
emby_polling_threads = {}
emby_polling_lock = threading.Lock()

# Import shared tracking from media_processor
from media_processor import processed_jellyfin_episodes, get_episode_tracking_key


class EmbyIntegration(ServiceIntegration):
    """Emby integration handler"""

    # ==========================================
    # Integration Metadata
    # ==========================================

    @property
    def service_name(self) -> str:
        return 'emby'

    @property
    def display_name(self) -> str:
        return 'Emby'

    @property
    def description(self) -> str:
        return 'Webhook-triggered polling for watch detection'

    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/emby.png'

    @property
    def category(self) -> str:
        return 'media'

    @property
    def default_port(self) -> int:
        return 8096

    # ==========================================
    # Setup Fields
    # ==========================================

    def get_setup_fields(self) -> Optional[List[Dict]]:
        """Custom setup fields for Emby"""
        return [
            {
                'name': 'url',
                'label': 'Emby Server URL',
                'type': 'text',
                'placeholder': 'http://192.168.1.100:8096',
                'required': True,
                'help_text': 'Your Emby server URL'
            },
            {
                'name': 'api_key',
                'label': 'API Key',
                'type': 'text',
                'placeholder': 'Enter API Key',
                'required': True,
                'help_text': 'Settings → Advanced → API Keys'
            },
            {
                'name': 'user_id',
                'label': 'User ID (Optional)',
                'type': 'text',
                'placeholder': 'Leave blank to monitor all users',
                'required': False,  # Changed from True
                'help_text': 'Specific Emby username to monitor. Leave blank to process episodes from any user.'
            },
            {
                'name': 'poll_interval',
                'label': 'Poll Interval (seconds)',
                'type': 'number',
                'default': 900,
                'help_text': 'Check progress every X seconds after playback starts. Recommended: 900 (15 min). Min: 300 (5 min).'
            },
            {
                'name': 'trigger_percentage',
                'label': 'Trigger Percentage',
                'type': 'number',
                'default': 50.0,
                'help_text': 'Process episode when watch % >= this value (e.g., 50 = process at 50% watched).'
            }
        ]

    def get_dashboard_stats(self, url: str = None, api_key: str = None) -> Dict[str, Any]:
        """Get stats for dashboard"""
        return {'configured': True}

    # ==========================================
    # Config Loading
    # ==========================================

    def get_config(self) -> Optional[Dict[str, Any]]:
        """Load Emby configuration - always uses polling mode"""
        from settings_db import get_emby_config
        config = get_emby_config()
        if config:
            # Emby only supports polling, always set method
            config['method'] = 'polling'
        return config

    def check_user(self, username: str) -> bool:
        """Check if username matches configured user (or if monitoring all users)"""
        config = self.get_config()
        if not config:
            return False
        
        configured_user = config.get('user_id', '').strip()
        
        # If no user configured, accept all users
        if not configured_user:
            return True
        
        # Otherwise check if username matches
        return username.lower() == configured_user.lower()

    # ==========================================
    # Polling Functions
    # ==========================================

    def get_session_by_id(self, session_id: str) -> Optional[Dict]:
        """Get active Emby session by ID"""
        config = self.get_config()
        if not config:
            return None

        try:
            url = f"{config['url']}/Sessions"
            headers = {'X-Emby-Token': config['api_key']}

            response = requests.get(url, headers=headers, timeout=10)
            if response.ok:
                sessions = response.json()
                for session in sessions:
                    if session.get('Id') == session_id:
                        return session
        except Exception as e:
            logger.error(f"Error fetching Emby session {session_id}: {e}")

        return None

    def extract_episode_info(self, session: Dict) -> Optional[Dict]:
        """Extract episode info from Emby session"""
        try:
            now_playing = session.get('NowPlayingItem')
            if not now_playing or now_playing.get('Type') != 'Episode':
                return None

            play_state = session.get('PlayState', {})
            position_ticks = play_state.get('PositionTicks', 0)
            runtime_ticks = now_playing.get('RunTimeTicks', 1)
            progress = (position_ticks / runtime_ticks * 100) if runtime_ticks > 0 else 0

            return {
                'series_name': now_playing.get('SeriesName'),
                'season_number': now_playing.get('ParentIndexNumber'),
                'episode_number': now_playing.get('IndexNumber'),
                'progress_percent': progress,
                'is_paused': play_state.get('IsPaused', False),
                'user_name': session.get('UserName', 'Unknown')
            }
        except Exception as e:
            logger.error(f"Error extracting episode info: {e}")
            return None

    def should_trigger(self, progress: float, threshold: float) -> bool:
        """Check if progress meets trigger threshold"""
        return progress >= float(threshold)

    def poll_session(self, session_id: str, initial_episode_info: Dict):
        """Poll a specific Emby session until trigger percentage or session ends"""
        config = self.get_config()
        if not config:
            return

        poll_interval = int(config.get('poll_interval', 900))
        trigger_percentage = float(config.get('trigger_percentage', 50.0))

        logger.info(f"🔄 Starting Emby polling for session {session_id}")
        logger.info(f"   📺 {initial_episode_info['series_name']} S{initial_episode_info['season_number']}E{initial_episode_info['episode_number']}")
        logger.info(f"   🎯 Will trigger at {trigger_percentage}% progress")

        try:
            processed = False
            poll_count = 0

            while session_id in active_emby_sessions and not processed:
                poll_count += 1

                # Get current session state
                current_session = self.get_session_by_id(session_id)

                if not current_session:
                    logger.info(f"📺 Session {session_id} ended - stopping polling (poll #{poll_count})")
                    break

                # Extract current episode info
                current_episode_info = self.extract_episode_info(current_session)

                if not current_episode_info:
                    logger.info(f"⏭️ Session {session_id} no longer playing episode - stopping polling")
                    break

                # Check if we're still on the same episode
                if (current_episode_info['series_name'] != initial_episode_info['series_name'] or
                        current_episode_info['season_number'] != initial_episode_info['season_number'] or
                        current_episode_info['episode_number'] != initial_episode_info['episode_number']):
                    logger.info(f"📺 Episode changed in session {session_id} - stopping polling for original episode")
                    break

                current_progress = current_episode_info['progress_percent']
                is_paused = current_episode_info['is_paused']

                logger.info(f"📊 Poll #{poll_count}: {current_progress:.1f}% {'(PAUSED)' if is_paused else ''}")

                # Check if we should trigger processing
                if self.should_trigger(current_progress, trigger_percentage):
                    logger.info(f"🎯 Trigger threshold reached! Processing at {current_progress:.1f}%")

                    success = self.process_episode(current_episode_info)
                    if success:
                        processed = True
                        logger.info(f"✅ Successfully processed - stopping polling for session {session_id}")
                    else:
                        logger.warning(f"⚠️ Processing failed - continuing polling")

                # Wait before next poll (unless we just processed)
                if not processed:
                    time.sleep(poll_interval)

            if not processed and session_id not in active_emby_sessions:
                logger.info(f"🔄 Polling stopped for session {session_id} - session ended before trigger")

        except Exception as e:
            logger.error(f"❌ Error in Emby polling thread for session {session_id}: {str(e)}")

        finally:
            # Clean up
            with emby_polling_lock:
                if session_id in active_emby_sessions:
                    del active_emby_sessions[session_id]
                if session_id in emby_polling_threads:
                    del emby_polling_threads[session_id]

            logger.info(f"🧹 Cleaned up polling for session {session_id}")

    def start_polling(self, session_id: str, episode_info: Dict) -> bool:
        """Start polling for a specific Emby session"""
        with emby_polling_lock:
            # Don't start if already polling this session
            if session_id in active_emby_sessions:
                logger.info(f"⏭️ Already polling session {session_id} - skipping")
                return False

            # Store session info
            active_emby_sessions[session_id] = episode_info

            logger.info(f"🎬 Starting Emby polling for: {episode_info['series_name']} S{episode_info['season_number']}E{episode_info['episode_number']}")
            logger.info(f"   👤 User: {episode_info['user_name']}")
            logger.info(f"   🔄 Session ID: {session_id}")

            # Start polling thread
            thread = threading.Thread(
                target=self.poll_session,
                args=(session_id, episode_info),
                daemon=True,
                name=f"EmbyPoll-{session_id[:8]}"
            )
            thread.start()
            emby_polling_threads[session_id] = thread

            return True

    def stop_polling(self, session_id: str) -> bool:
        """Stop polling for a specific session"""
        with emby_polling_lock:
            if session_id in active_emby_sessions:
                logger.info(f"🛑 Stopping Emby polling for session {session_id}")
                del active_emby_sessions[session_id]
                return True
            return False

    # ==========================================
    # Episode Processing
    # ==========================================

    def process_episode(self, episode_info: Dict) -> bool:
        """Process episode for upgrade - writes temp file and calls media_processor"""
        try:
            series_name = episode_info['series_name']
            season = episode_info['season_number']
            episode = episode_info['episode_number']
            user_name = episode_info['user_name']
            progress = episode_info.get('progress_percent', 0)

            # Check if already processed
            tracking_key = get_episode_tracking_key(series_name, season, episode, user_name)
            if tracking_key in processed_jellyfin_episodes:
                logger.info(f"✅ Already processed - skipping")
                return False

            # Mark as processed
            processed_jellyfin_episodes.add(tracking_key)

            logger.info(f"🎯 Processing Emby episode at {progress:.1f}%")

            # Get Sonarr series ID
            from media_processor import get_series_id
            series_id = get_series_id(series_name)

            # Tag sync & drift correction
            if series_id:
                from episeerr_utils import validate_series_tag, sync_rule_tag_to_sonarr
                from episeerr import load_config
                from media_processor import move_series_in_config

                config = load_config()
                config_rule = None
                series_id_str = str(series_id)

                for rule_name, rule_details in config['rules'].items():
                    if series_id_str in rule_details.get('series', {}):
                        config_rule = rule_name
                        break

                if config_rule:
                    matches, actual_tag_rule = validate_series_tag(series_id, config_rule)
                    if not matches:
                        if actual_tag_rule:
                            logger.warning(f"EMBY DRIFT - config: {config_rule} → tag: {actual_tag_rule}")
                            move_series_in_config(series_id, config_rule, actual_tag_rule)
                        else:
                            logger.warning(f"No tag on {series_id} → restoring episeerr_{config_rule}")
                            sync_rule_tag_to_sonarr(series_id, config_rule)

            # Write temp file for media_processor
            temp_dir = os.path.join(os.getcwd(), 'temp')
            os.makedirs(temp_dir, exist_ok=True)

            episode_data = {
                'server_title': series_name,
                'server_season_num': int(season),
                'server_ep_num': int(episode),
                'thetvdb_id': None,
                'themoviedb_id': None,
                'sonarr_series_id': series_id,
                'source': 'emby'
            }

            temp_file_path = os.path.join(temp_dir, 'data_from_server.json')
            with open(temp_file_path, 'w') as f:
                json.dump(episode_data, f)

            # Run media_processor
            result = subprocess.run(
                ["python3", os.path.join(os.getcwd(), "media_processor.py")],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                logger.error(f"media_processor failed (rc={result.returncode}): {result.stderr}")
                return False
            else:
                logger.info(f"✅ Processed {series_name} S{season}E{episode}")
                return True

        except Exception as e:
            logger.error(f"Error processing Emby episode: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def get_dashboard_widget(self) -> Optional[Dict]:
        """Emby doesn't have a dashboard widget yet"""
        return None

    # ==========================================
    # Connection Test
    # ==========================================

    def test_connection(self, url: str, api_key: str, **kwargs) -> tuple:
        """Test connection to Emby server"""
        try:
            headers = {'X-Emby-Token': api_key}
            response = requests.get(f"{url}/System/Info", headers=headers, timeout=10)

            if response.ok:
                info = response.json()
                version = info.get('Version', 'unknown')
                server_name = info.get('ServerName', 'Emby')
                return True, f"Connected to {server_name} v{version}"
            else:
                return False, f"Emby returned status {response.status_code}"

        except requests.exceptions.Timeout:
            return False, "Connection timeout - check URL and network"
        except requests.exceptions.ConnectionError:
            return False, "Cannot connect - check URL and that Emby is running"
        except Exception as e:
            return False, f"Error: {str(e)}"

    # ==========================================
    # Flask Routes (Webhook Handler)
    # ==========================================

    def create_blueprint(self) -> Blueprint:
        """Create Flask blueprint with Emby-specific routes"""
        bp = Blueprint('emby_integration', __name__, url_prefix='/api/integration/emby')
        integration = self

        @bp.route('/webhook', methods=['POST'])
        def emby_webhook():
            """
            Handle Emby webhooks - Polling mode only (Emby doesn't support PlaybackProgress)
            
            Configure in Emby:
            Settings → Webhooks
            Enable: playback.start and playback.stop
            URL: http://<episeerr-ip>:5002/api/integration/emby/webhook
            """
            logger.info("Received webhook from Emby")
            data = request.json
            if not data:
                return jsonify({'status': 'error', 'message': 'No data received'}), 400
            
            try:
                event = data.get('Event')
                logger.info(f"Emby webhook event: {event}")
                
                config = integration.get_config()
                if not config:
                    return jsonify({'status': 'error', 'message': 'Emby not configured'}), 400
                
                # ============================================================================
                # PLAYBACK START - Start polling
                # ============================================================================
                if event in ['playback.start', 'SessionStart']:
                    item_type = data.get('Item', {}).get('Type')
                    if item_type == 'Episode':
                        item = data.get('Item', {})
                        series_name = item.get('SeriesName')
                        season = item.get('ParentIndexNumber')
                        episode = item.get('IndexNumber')
                        session_id = data.get('Session', {}).get('Id') or data.get('PlaySessionId')
                        user_name = data.get('User', {}).get('Name', 'Unknown')
                        
                        if all([series_name, season is not None, episode is not None, session_id]):
                            logger.info(f"📺 Emby session started: {series_name} S{season}E{episode} (User: {user_name})")
                            
                            if not integration.check_user(user_name):
                                return jsonify({'status': 'success', 'message': 'User not configured'}), 200
                            
                            episode_info = {
                                'user_name': user_name,
                                'series_name': series_name,
                                'season_number': int(season),
                                'episode_number': int(episode),
                                'progress_percent': 0.0,
                                'is_paused': False
                            }
                            
                            polling_started = integration.start_polling(session_id, episode_info)
                            
                            if polling_started:
                                logger.info(f"✅ Started polling for {series_name} S{season}E{episode}")
                                return jsonify({'status': 'success', 'message': 'Started polling'}), 200
                            else:
                                return jsonify({'status': 'warning', 'message': 'Already polling'}), 200
                
                # ============================================================================
                # PLAYBACK STOP - Stop polling and check final progress
                # ============================================================================
                elif event in ['playback.stop', 'PlaybackStop']:
                    item = data.get('Item', {})
                    series_name = item.get('SeriesName', 'Unknown')
                    season = item.get('ParentIndexNumber')
                    episode = item.get('IndexNumber')
                    session_id = data.get('Session', {}).get('Id') or data.get('PlaySessionId')
                    user_name = data.get('User', {}).get('Name', 'Unknown')
                    
                    logger.info(f"📺 Emby playback stopped: {series_name} S{season}E{episode} (User: {user_name})")
                    
                    # Stop polling if active
                    if session_id:
                        stopped = integration.stop_polling(session_id)
                        if stopped:
                            logger.info(f"🛑 Stopped polling for {series_name}")
                    
                    # Check final progress (safety net)
                    if all([series_name, season is not None, episode is not None]):
                        if not integration.check_user(user_name):
                            return jsonify({'status': 'success'}), 200
                        
                        position_ticks = data.get('PlaybackInfo', {}).get('PositionTicks', 0)
                        runtime_ticks = item.get('RunTimeTicks', 1)
                        progress_percent = (position_ticks / runtime_ticks * 100) if runtime_ticks > 0 else 0
                        
                        logger.info(f"Final progress: {progress_percent:.1f}%")
                        
                        tracking_key = get_episode_tracking_key(series_name, season, episode, user_name)
                        if tracking_key in processed_jellyfin_episodes:
                            logger.info(f"Already processed via polling")
                            return jsonify({'status': 'success'}), 200
                        
                        trigger_percentage = float(config.get('trigger_percentage', 50.0))
                        if progress_percent >= trigger_percentage:
                            logger.info(f"🎯 Processing on stop at {progress_percent:.1f}%")
                            
                            episode_info = {
                                'user_name': user_name,
                                'series_name': series_name,
                                'season_number': int(season),
                                'episode_number': int(episode),
                                'progress_percent': progress_percent
                            }
                            
                            integration.process_episode(episode_info)
                            return jsonify({'status': 'success', 'message': 'Processed on stop'}), 200
                        else:
                            logger.info(f"Skipped - only watched {progress_percent:.1f}%")
                
                return jsonify({'status': 'success'}), 200
                
            except Exception as e:
                logger.error(f"Error handling Emby webhook: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return jsonify({'status': 'error', 'message': str(e)}), 500

        

        @bp.route('/polling-status')
        def polling_status():
            """Get current Emby polling status for debugging"""
            try:
                with emby_polling_lock:
                    active_sessions = list(active_emby_sessions.keys())
                    thread_count = len(emby_polling_threads)

                    config = integration.get_config()
                    trigger_percentage = float(config.get('trigger_percentage', 50.0)) if config else 50.0
                    poll_interval = int(config.get('poll_interval', 900)) if config else 900

                    return jsonify({
                        'status': 'success',
                        'polling_status': {
                            'active_sessions': active_sessions,
                            'thread_count': thread_count,
                            'trigger_percentage': trigger_percentage,
                            'poll_interval_minutes': poll_interval // 60
                        }
                    })
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)}), 500

        return bp


# Auto-discovery registration
integration = EmbyIntegration()
