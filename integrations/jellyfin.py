"""
Jellyfin Integration for Episeerr
Provides: Webhook-triggered polling for watch detection, real-time session monitoring
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
active_jellyfin_sessions = {}
jellyfin_polling_threads = {}
jellyfin_polling_lock = threading.Lock()

# Processed episodes tracking (shared with Emby)
# This is imported from media_processor to maintain compatibility
from media_processor import processed_jellyfin_episodes, get_episode_tracking_key


class JellyfinIntegration(ServiceIntegration):
    """Jellyfin integration handler"""
    
    # ==========================================
    # Integration Metadata
    # ==========================================
    
    @property
    def service_name(self) -> str:
        return 'jellyfin'
    
    @property
    def display_name(self) -> str:
        return 'Jellyfin'
    
    @property
    def description(self) -> str:
        return 'Webhook-triggered polling with real-time session monitoring'
    
    @property
    def icon(self) -> str:
        return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/jellyfin.png'
    
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
        """Custom setup fields for Jellyfin"""
        return [
            {
                'name': 'url',
                'label': 'Jellyfin Server URL',
                'type': 'text',
                'placeholder': 'http://192.168.1.100:8096',
                'required': True,
                'help_text': 'Your Jellyfin server URL'
            },
            {
                'name': 'api_key',
                'label': 'API Key',
                'type': 'text',
                'placeholder': 'Enter API Key',
                'required': True,
                'help_text': 'Dashboard → API Keys → Create new key'
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
                'name': 'method',
                'label': 'Detection Method',
                'type': 'select',  # Use select dropdown instead of radio
                'options': [
                    {'value': 'polling', 'label': 'Webhook-Triggered Polling (Recommended)'},
                    {'value': 'progress', 'label': 'PlaybackProgress (Advanced)'}
                ],
                'default': 'polling',
                'help_text': 'Polling: webhook fires once, polls session. Progress: constant webhook spam.'
            },
            {
                'name': 'poll_interval',
                'label': 'Poll Interval (seconds)',
                'type': 'number',
                'default': 900,
                'help_text': 'For polling mode. Recommended: 900 (15 min)'
            },
            {
                'name': 'trigger_percentage',
                'label': 'Trigger %',
                'type': 'number',
                'default': 50.0,
                'help_text': 'For polling mode. Process when watch % >= this value'
            },
            {
                'name': 'trigger_min',
                'label': 'Min %',
                'type': 'number',
                'default': 50.0,
                'help_text': 'For progress mode. Minimum watch percentage'
            },
            {
                'name': 'trigger_max',
                'label': 'Max %',
                'type': 'number',
                'default': 55.0,
                'help_text': 'For progress mode. Maximum watch percentage'
            }
        ]
    def get_dashboard_stats(self, url: str = None, api_key: str = None) -> Dict[str, Any]:
        """Get stats for dashboard - Jellyfin doesn't provide stats yet"""
        return {
            'configured': True
        }
    # ==========================================
    # Config Loading
    # ==========================================
    
    def get_config(self) -> Optional[Dict[str, Any]]:
        """Load Jellyfin configuration"""
        from settings_db import get_jellyfin_config
        return get_jellyfin_config()
    
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
        """Get active Jellyfin session by ID"""
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
            logger.error(f"Error fetching Jellyfin session {session_id}: {e}")
        
        return None
    
    def extract_episode_info(self, session: Dict) -> Optional[Dict]:
        """Extract episode info from Jellyfin session"""
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
        """Poll a specific Jellyfin session until trigger percentage or session ends"""
        config = self.get_config()
        if not config:
            return
        
        poll_interval = int(config.get('poll_interval', 900))
        trigger_percentage = config.get('trigger_percentage', 50.0)
        
        logger.info(f"🔄 Starting Jellyfin polling for session {session_id}")
        logger.info(f"   📺 {initial_episode_info['series_name']} S{initial_episode_info['season_number']}E{initial_episode_info['episode_number']}")
        logger.info(f"   🎯 Will trigger at {trigger_percentage}% progress")
        
        try:
            processed = False
            poll_count = 0
            
            while session_id in active_jellyfin_sessions and not processed:
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
            
            if not processed and session_id not in active_jellyfin_sessions:
                logger.info(f"🔄 Polling stopped for session {session_id} - session ended before trigger")
            
        except Exception as e:
            logger.error(f"❌ Error in Jellyfin polling thread for session {session_id}: {str(e)}")
        
        finally:
            # Clean up
            with jellyfin_polling_lock:
                if session_id in active_jellyfin_sessions:
                    del active_jellyfin_sessions[session_id]
                if session_id in jellyfin_polling_threads:
                    del jellyfin_polling_threads[session_id]
            
            logger.info(f"🧹 Cleaned up polling for session {session_id}")
    
    def start_polling(self, session_id: str, episode_info: Dict) -> bool:
        """Start polling for a specific Jellyfin session"""
        with jellyfin_polling_lock:
            # Don't start if already polling this session
            if session_id in active_jellyfin_sessions:
                logger.info(f"⏭️ Already polling session {session_id} - skipping")
                return False
            
            # Store session info
            active_jellyfin_sessions[session_id] = episode_info
            
            logger.info(f"🎬 Starting Jellyfin polling for: {episode_info['series_name']} S{episode_info['season_number']}E{episode_info['episode_number']}")
            logger.info(f"   👤 User: {episode_info['user_name']}")
            logger.info(f"   🔄 Session ID: {session_id}")
            
            # Start polling thread
            thread = threading.Thread(
                target=self.poll_session,
                args=(session_id, episode_info),
                daemon=True,
                name=f"JellyfinPoll-{session_id[:8]}"
            )
            thread.start()
            jellyfin_polling_threads[session_id] = thread
            
            return True
    
    def stop_polling(self, session_id: str) -> bool:
        """Stop polling for a specific session"""
        with jellyfin_polling_lock:
            if session_id in active_jellyfin_sessions:
                logger.info(f"🛑 Stopping Jellyfin polling for session {session_id}")
                del active_jellyfin_sessions[session_id]
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
            
            logger.info(f"🎯 Processing Jellyfin episode at {progress:.1f}%")
            
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
                            logger.warning(f"JELLYFIN DRIFT - config: {config_rule} → tag: {actual_tag_rule}")
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
                'source': 'jellyfin'
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
            logger.error(f"Error processing Jellyfin episode: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    def get_dashboard_widget(self) -> Optional[Dict]:
        """Jellyfin doesn't have a dashboard widget yet"""
        return None
    # ==========================================
    # Flask Routes (Webhook Handler)
    # ==========================================
    def test_connection(self, url: str, api_key: str) -> tuple:
        """Test connection to Jellyfin server"""
        try:
            headers = {'X-Emby-Token': api_key}
            response = requests.get(f"{url}/System/Info", headers=headers, timeout=10)
            
            if response.ok:
                info = response.json()
                version = info.get('Version', 'unknown')
                return True, f"Connected to Jellyfin v{version}"
            else:
                return False, f"Jellyfin returned status {response.status_code}"
        
        except requests.exceptions.Timeout:
            return False, "Connection timeout - check URL and network"
        except requests.exceptions.ConnectionError:
            return False, "Cannot connect - check URL and that Jellyfin is running"
        except Exception as e:
            return False, f"Error: {str(e)}"
        
    def create_blueprint(self) -> Blueprint:
        """Create Flask blueprint with Jellyfin-specific routes"""
        bp = Blueprint('jellyfin_integration', __name__, url_prefix='/api/integration/jellyfin')
        integration = self
        
        @bp.route('/webhook', methods=['POST'])
        def jellyfin_webhook():
            """
            Handle Jellyfin webhooks - Supports polling (SessionStart) OR real-time (PlaybackProgress)
            
            Configure in Jellyfin:
            Dashboard → Plugins → Notifications → Webhook
            
            For Polling mode: Enable PlaybackStart and PlaybackStop
            For Progress mode: Enable PlaybackProgress
            
            URL: http://<episeerr-ip>:5002/api/integration/jellyfin/webhook
            """
            logger.info("Received webhook from Jellyfin")
            data = request.json
            if not data:
                return jsonify({'status': 'error', 'message': 'No data received'}), 400
            
            try:
                notification_type = data.get('NotificationType')
                logger.info(f"Jellyfin webhook type: {notification_type}")
                
                config = integration.get_config()
                if not config:
                    return jsonify({'status': 'error', 'message': 'Jellyfin not configured'}), 400
                
                method = config.get('method', 'polling')
                
                # ============================================================================
                # REAL-TIME MODE: PlaybackProgress
                # ============================================================================
                if notification_type == 'PlaybackProgress' and method == 'progress':
                    item_type = data.get('ItemType')
                    if item_type == 'Episode':
                        series_name = data.get('SeriesName')
                        season = data.get('SeasonNumber')
                        episode = data.get('EpisodeNumber')
                        
                        # Calculate progress percentage
                        progress_ticks = data.get('PlaybackPositionTicks', 0)
                        runtime_ticks = data.get('RunTimeTicks', 1)
                        progress_percent = (progress_ticks / runtime_ticks * 100) if runtime_ticks > 0 else 0
                        
                        session_id = data.get('SessionId') or data.get('PlaySessionId') or data.get('Id')
                        user_name = data.get('NotificationUsername', 'Unknown')
                        
                        if all([series_name, season is not None, episode is not None]):
                            if not integration.check_user(user_name):
                                return jsonify({'status': 'success', 'message': 'User not configured'}), 200
                            
                            trigger_min = float(config.get('trigger_min', 50.0))
                            trigger_max = float(config.get('trigger_max', 55.0))

                            if trigger_min <= progress_percent <= trigger_max:
                                # Check if already processed
                                tracking_key = get_episode_tracking_key(series_name, season, episode, user_name)
                                if tracking_key in processed_jellyfin_episodes:
                                    logger.debug(f"Already processed {series_name} S{season}E{episode} - skipping")
                                    return jsonify({'status': 'success', 'message': 'Already processed'}), 200
                                
                                # Mark as processed BEFORE processing (prevent race condition)
                                processed_jellyfin_episodes.add(tracking_key)
                                
                                episode_info = {
                                    'series_name': series_name,
                                    'season_number': season,
                                    'episode_number': episode,
                                    'progress_percent': progress_percent,
                                    'user_name': user_name
                                }
                                success = integration.process_episode(episode_info)
                                if success:
                                    logger.info(f"✅ Processed {series_name} S{season}E{episode}")
                            
                            return jsonify({'status': 'success'}), 200
                        else:
                            return jsonify({'status': 'error', 'message': 'Missing episode data'}), 400
                    else:
                        return jsonify({'status': 'success', 'message': 'Not an episode'}), 200
                
                # ============================================================================
                # POLLING MODE: SessionStart or PlaybackStart
                # ============================================================================
                elif notification_type in ['SessionStart', 'PlaybackStart'] and method == 'polling':
                    item_type = data.get('ItemType')
                    if item_type == 'Episode':
                        series_name = data.get('SeriesName')
                        season = data.get('SeasonNumber')
                        episode = data.get('EpisodeNumber')
                        webhook_id = data.get('Id')
                        user_name = data.get('NotificationUsername', 'Unknown')
                        
                        if all([series_name, season is not None, episode is not None]):
                            logger.info(f"📺 Jellyfin session started: {series_name} S{season}E{episode} (User: {user_name})")
                            
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
                            
                            polling_started = integration.start_polling(webhook_id, episode_info)
                            
                            if polling_started:
                                logger.info(f"✅ Started polling for {series_name} S{season}E{episode}")
                                return jsonify({'status': 'success', 'message': 'Started polling'}), 200
                            else:
                                return jsonify({'status': 'warning', 'message': 'Polling may already be active'}), 200
                        else:
                            return jsonify({'status': 'error', 'message': 'Missing fields'}), 400
                    else:
                        return jsonify({'status': 'success', 'message': 'Not an episode'}), 200
                
                # ============================================================================
                # CLEANUP: PlaybackStop
                # ============================================================================
                elif notification_type == 'PlaybackStop':
                    webhook_id = data.get('Id')
                    series_name = data.get('SeriesName', 'Unknown')
                    season = data.get('SeasonNumber')
                    episode = data.get('EpisodeNumber')
                    user_name = data.get('NotificationUsername', 'Unknown')
                    
                    logger.info(f"📺 Jellyfin playback stopped: {series_name} S{season}E{episode} (User: {user_name})")
                    
                    # Stop polling if active
                    stopped = integration.stop_polling(webhook_id)
                    if stopped:
                        logger.info(f"🛑 Stopped polling for {series_name}")
                    
                    # Fallback processing if watched enough
                    if all([series_name, season is not None, episode is not None]):
                        if not integration.check_user(user_name):
                            return jsonify({'status': 'success'}), 200
                        
                        progress_ticks = data.get('PlaybackPositionTicks', 0)
                        runtime_ticks = data.get('RunTimeTicks', 1)
                        progress_percent = (progress_ticks / runtime_ticks * 100) if runtime_ticks > 0 else 0
                        
                        logger.info(f"Final progress: {progress_percent:.1f}%")
                        
                        tracking_key = get_episode_tracking_key(series_name, season, episode, user_name)
                        if tracking_key in processed_jellyfin_episodes:
                            logger.info(f"Already processed via polling")
                            return jsonify({'status': 'success', 'message': 'Already processed'}), 200
                        
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
                    
                    return jsonify({'status': 'success', 'message': 'Playback stopped'}), 200
                
                else:
                    logger.info(f"Jellyfin notification type '{notification_type}' not handled for method '{method}'")
                    return jsonify({'status': 'success', 'message': 'Event not handled'}), 200
            
            except Exception as e:
                logger.error(f"Error handling Jellyfin webhook: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return jsonify({'status': 'error', 'message': str(e)}), 500
        @bp.route('/polling-status')
        def polling_status():
            """Get current Jellyfin polling status for debugging"""
            try:
                with jellyfin_polling_lock:
                    active_sessions = list(active_jellyfin_sessions.keys())
                    thread_count = len(jellyfin_polling_threads)
                    
                    config = integration.get_config()
                    trigger_percentage = config.get('trigger_percentage', 50.0) if config else 50.0
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
integration = JellyfinIntegration()