"""
Sonarr and legacy Tautulli webhook handlers.

Extracted from episeerr.py to keep the main module manageable.
Circular-import risk: load_config / save_config / get_external_ids / search_tv_shows
live in episeerr.py, so they are imported inside each function rather than at the
top of the module (same pattern used by integrations/tautulli.py etc.).
"""

import os
import json
import time
import subprocess

from flask import Blueprint, request, jsonify, current_app

import episeerr_utils
import sonarr_utils
from episeerr_utils import http
from settings_db import add_pending_request

# Same value as episeerr.py:81
REQUESTS_DIR = os.path.join(os.getcwd(), 'data', 'pending_requests')

sonarr_webhooks_bp = Blueprint('sonarr_webhooks', __name__)


# ============================================================================
# SONARR WEBHOOK
# ============================================================================

@sonarr_webhooks_bp.route('/sonarr-webhook', methods=['POST'])
def process_sonarr_webhook():
    """Handle incoming Sonarr webhooks for series additions with enhanced tag-based assignment."""
    current_app.logger.info("Received Sonarr webhook")

    try:
        json_data = request.json

        event_type = json_data.get('eventType')
        current_app.logger.info(f"Sonarr webhook event type: {event_type}")

        if event_type == 'Grab':
            return handle_episode_grab(json_data)

        series = json_data.get('series', {})
        series_id = series.get('id')
        tvdb_id = series.get('tvdbId')
        tmdb_id = series.get('tmdbId')
        series_title = series.get('title')

        current_app.logger.info(f"Processing series addition: {series_title} (ID: {series_id}, TVDB: {tvdb_id})")

        # Sonarr connection setup
        sonarr_preferences = sonarr_utils.load_preferences()
        headers = {
            'X-Api-Key': sonarr_preferences['SONARR_API_KEY'],
            'Content-Type': 'application/json'
        }
        SONARR_URL = sonarr_preferences['SONARR_URL']

        # ────────────────────────────────────────────────────────────────
        # Jellyseerr request cleanup (moved up)
        # ────────────────────────────────────────────────────────────────
        jellyseerr_request_id = None
        jellyseerr_requested_seasons = None
        tvdb_id_str = str(tvdb_id) if tvdb_id else None

        if tvdb_id_str:
            request_file = os.path.join(REQUESTS_DIR, f"jellyseerr-{tvdb_id_str}.json")
            if os.path.exists(request_file):
                try:
                    with open(request_file, 'r') as f:
                        request_data = json.load(f)
                    jellyseerr_request_id = request_data.get('request_id')
                    jellyseerr_requested_seasons = request_data.get('requested_seasons')
                    current_app.logger.info(f"✓ Found Jellyseerr request file: {jellyseerr_request_id}")

                    try:
                        from activity_storage import save_request_event
                        save_request_event(request_data)
                    except Exception as e:
                        current_app.logger.error(f"Failed to log request to activity: {e}")

                    os.remove(request_file)
                    current_app.logger.info(f"✓ Removed Jellyseerr request file")
                except Exception as e:
                    current_app.logger.error(f"Error processing Jellyseerr request file: {str(e)}")

        # ────────────────────────────────────────────────────────────────
        # Enhanced tag detection - supports all rule tags
        # ────────────────────────────────────────────────────────────────
        tags_response = http.get(f"{SONARR_URL}/api/v3/tag", headers=headers)
        if not tags_response.ok:
            current_app.logger.error(f"Failed to get Sonarr tags: {tags_response.status_code}")
            return jsonify({"status": "error", "message": "Failed to get tags"}), 500

        tags = tags_response.json()
        tag_mapping = {tag['id']: tag['label'].lower() for tag in tags}

        series_tags = series.get('tags', [])
        current_app.logger.info(f"Series tags (IDs): {series_tags}")
        current_app.logger.info(f"Tag mapping: {tag_mapping}")

        assigned_rule = None
        is_select_request = False

        config = None  # lazy load

        # Create reverse mapping (label -> id) for webhook format compatibility
        reverse_tag_mapping = {label.lower(): tag_id for tag_id, label in tag_mapping.items()}
        current_app.logger.debug(f"Reverse tag mapping created: {len(reverse_tag_mapping)} tags")

        for tag_id in series_tags:
            # Handle both formats: integer IDs and string labels
            original_tag = tag_id
            tag_label = None

            if isinstance(tag_id, int):
                # Standard format: integer ID
                tag_label = tag_mapping.get(tag_id, '').lower()
            elif isinstance(tag_id, str):
                # Webhook format: string label
                tag_label = tag_id.lower()
                actual_tag_id = reverse_tag_mapping.get(tag_label)
                if actual_tag_id:
                    tag_id = actual_tag_id
                    current_app.logger.debug(f"Converted tag label '{original_tag}' to ID {tag_id}")
                else:
                    current_app.logger.warning(f"Tag label '{original_tag}' not found in Sonarr tags")
                    continue
            else:
                current_app.logger.error(f"Unexpected tag type: {original_tag} (type: {type(original_tag)})")
                continue

            if not tag_label:
                current_app.logger.warning(f"Could not determine label for tag: {original_tag}")
                continue

            if not tag_label.startswith('episeerr_'):
                continue

            rule_name = tag_label.replace('episeerr_', '')
            current_app.logger.info(f"Processing episeerr tag: {tag_label} (rule_name: {rule_name})")

            if rule_name == 'select':
                is_select_request = True
                current_app.logger.info("Detected episeerr_select tag → selection workflow")
                break

            else:
                # Direct rule tag - case-insensitive lookup
                if config is None:
                    from episeerr import load_config
                    config = load_config()

                actual_rule_name = None
                for rn in config.get('rules', {}).keys():
                    if rn.lower() == rule_name.lower():
                        actual_rule_name = rn
                        break

                if actual_rule_name:
                    assigned_rule = actual_rule_name
                    current_app.logger.info(f"✓ Detected direct rule tag: episeerr_{rule_name} → matched rule '{actual_rule_name}'")
                    break
                else:
                    current_app.logger.warning(f"Ignoring unknown rule tag: episeerr_{rule_name}")
                    if config:
                        current_app.logger.warning(f"Available rules: {list(config.get('rules', {}).keys())}")
                    else:
                        current_app.logger.warning("Config not yet loaded")

        # ────────────────────────────────────────────────────────────────
        # No episeerr tag → auto-assign fallback
        # ────────────────────────────────────────────────────────────────
        current_app.logger.info(f"=== TAG DETECTION SUMMARY ===")
        current_app.logger.info(f"assigned_rule: {assigned_rule}")
        current_app.logger.info(f"is_select_request: {is_select_request}")
        if not assigned_rule and not is_select_request:
            import media_processor
            global_settings = media_processor.load_global_settings()
            current_app.logger.info(f"Auto-assign check: enabled={global_settings.get('auto_assign_new_series', False)}")

            if global_settings.get('auto_assign_new_series', False):
                if config is None:
                    from episeerr import load_config
                    config = load_config()
                default_rule_name = config.get('default_rule', 'default')

                if default_rule_name not in config['rules']:
                    return jsonify({"status": "error", "message": f"Default rule '{default_rule_name}' missing"}), 500

                series_id_str = str(series_id)
                target_rule = config['rules'][default_rule_name]
                target_rule.setdefault('series', {})
                series_dict = target_rule['series']

                if series_id_str not in series_dict:
                    from episeerr import save_config
                    series_dict[series_id_str] = {'activity_date': None}
                    save_config(config)
                    try:
                        episeerr_utils.sync_rule_tag_to_sonarr(series_id, default_rule_name)
                    except Exception as e:
                        current_app.logger.error(f"Auto-assign tag sync failed: {e}")
                    current_app.logger.info(f"Auto-assigned to default rule '{default_rule_name}'")

                # Set assigned_rule and continue to processing
                assigned_rule = default_rule_name
            else:
                # ONLY return if auto-assign is OFF
                current_app.logger.info("No episeerr tags + auto-assign off → no action")
                return jsonify({"status": "success", "message": "No processing needed"}), 200

        # ────────────────────────────────────────────────────────────────
        # We have action → unmonitor + cleanup tags + cancel downloads
        # ────────────────────────────────────────────────────────────────
        current_app.logger.info(f"Unmonitoring episodes for {series_title}")
        episeerr_utils.unmonitor_series(series_id, headers)

        # Remove ONLY episeerr_select/episeerr_default/episeerr_delay (keep rule tags)
        updated_tags = []
        removed = []
        had_delay_tag = False

        # Create reverse mapping for string labels
        reverse_tag_mapping = {label.lower(): tag_id for tag_id, label in tag_mapping.items()}

        for tag_item in series_tags:
            # Handle both integer IDs and string labels
            if isinstance(tag_item, int):
                tag_id = tag_item
                label = tag_mapping.get(tag_id, '').lower()
            elif isinstance(tag_item, str):
                label = tag_item.lower()
                tag_id = reverse_tag_mapping.get(label)
                if not tag_id:
                    current_app.logger.warning(f"Unknown tag label in removal: {tag_item}")
                    continue
            else:
                current_app.logger.warning(f"Unexpected tag type in removal: {tag_item}")
                continue

            if label in ['episeerr_select', 'episeerr_delay']:
                removed.append(label)
                if label == 'episeerr_delay':
                    had_delay_tag = True
            else:
                # Keep this tag - use integer ID
                updated_tags.append(tag_id)

        if removed:
            # Get fresh series data from Sonarr
            series_resp = http.get(
                f"{SONARR_URL}/api/v3/series/{series_id}",
                headers=headers
            )

            if series_resp.ok:
                update_payload = series_resp.json()
                update_payload['tags'] = updated_tags

                resp = http.put(
                    f"{SONARR_URL}/api/v3/series",
                    headers=headers,
                    json=update_payload
                )

                if resp.ok:
                    current_app.logger.info(f"Removed control tag(s): {removed}")
                else:
                    current_app.logger.error(f"Tag removal failed: {resp.text}")
            else:
                current_app.logger.error(f"Failed to fetch series data: {series_resp.status_code}")

        try:
            episeerr_utils.check_and_cancel_unmonitored_downloads()
        except Exception as e:
            current_app.logger.error(f"Download cancel failed: {e}")

        # ────────────────────────────────────────────────────────────────
        # Branch: selection vs rule processing
        # ────────────────────────────────────────────────────────────────
        if is_select_request:
            current_app.logger.info(f"Processing {series_title} with episeerr_select tag - creating selection request")

            # Ensure we have a TMDB ID for the UI
            if not tmdb_id:
                try:
                    from episeerr import get_external_ids, search_tv_shows
                    external_ids = get_external_ids(tvdb_id, 'tv')
                    if external_ids and external_ids.get('tmdb_id'):
                        tmdb_id = external_ids['tmdb_id']
                    else:
                        search_results = search_tv_shows(series_title)
                        if search_results.get('results'):
                            tmdb_id = search_results['results'][0]['id']
                except Exception as e:
                    current_app.logger.error(f"Error finding TMDB ID: {str(e)}")

            # Create a selection request
            request_id = f"sonarr-select-{series_id}-{int(time.time())}"

            pending_request = {
                "id": request_id,
                "series_id": series_id,
                "title": series_title,
                "needs_season_selection": True,
                "tmdb_id": tmdb_id,
                "tvdb_id": tvdb_id,
                "source": "sonarr",
                "source_name": "Sonarr Episode Selection",
                "needs_attention": True,
                "jellyseerr_request_id": jellyseerr_request_id,
                "created_at": int(time.time())
            }

            add_pending_request(pending_request)
            current_app.logger.info(f"✓ Created episode selection request for {series_title}")

            try:
                from notifications import send_notification
                send_notification(
                    "selection_pending",
                    series=series_title,
                    series_id=series_id
                )
            except Exception as e:
                current_app.logger.error(f"Failed to send selection pending notification: {e}")

            return jsonify({"status": "success", "message": "Selection request created"}), 200

        # ─── Rule processing ─────────────────────────────────────────────
        if config is None:
            from episeerr import load_config
            config = load_config()

        current_app.logger.info(f"Applying rule: {assigned_rule}")

        # Determine starting season
        starting_season = 1
        if jellyseerr_requested_seasons:
            try:
                seasons = [int(s.strip()) for s in str(jellyseerr_requested_seasons).split(',')]
                if seasons:
                    starting_season = min(seasons)
                    current_app.logger.info(f"✓ Using requested season {starting_season} from Jellyseerr")
            except Exception as e:
                current_app.logger.warning(f"Could not parse requested seasons: {e}")

        # Add to config + sync tag
        series_id_str = str(series_id)
        target_rule = config['rules'][assigned_rule]
        target_rule.setdefault('series', {})
        series_dict = target_rule['series']

        if series_id_str not in series_dict:
            from episeerr import save_config
            series_dict[series_id_str] = {'activity_date': None}
            save_config(config)
            try:
                episeerr_utils.sync_rule_tag_to_sonarr(series_id, assigned_rule)
                current_app.logger.info(f"Synced tag episeerr_{assigned_rule}")
            except Exception as e:
                current_app.logger.error(f"Tag sync failed: {e}")

        # Execute rule logic
        try:
            import media_processor
            rule_config = config['rules'][assigned_rule]
            get_type = rule_config.get('get_type', 'episodes')
            get_count = rule_config.get('get_count', 1)
            action_option = rule_config.get('action_option', 'monitor')

            # Process always_have FIRST (additive on top of get_type, runs after unmonitor)
            always_have = rule_config.get('always_have', '')
            if always_have:
                try:
                    media_processor.process_always_have(series_id, always_have)
                except Exception as e:
                    current_app.logger.error(f"always_have processing failed for series {series_id}: {e}")

            current_app.logger.info(f"Executing rule '{assigned_rule}' with get_type '{get_type}', get_count '{get_count}' starting from Season {starting_season}")

            # Get all episodes for the series
            episodes_response = http.get(
                f"{SONARR_URL}/api/v3/episode?seriesId={series_id}",
                headers=headers
            )

            if episodes_response.ok:
                all_episodes = episodes_response.json()

                # Get episodes from the requested season
                requested_season_episodes = sorted(
                    [ep for ep in all_episodes if ep.get('seasonNumber') == starting_season],
                    key=lambda x: x.get('episodeNumber', 0)
                )

                if not requested_season_episodes:
                    current_app.logger.warning(f"No Season {starting_season} episodes found for {series_title}")
                else:
                    # Determine which episodes to monitor based on get settings
                    episodes_to_monitor = []

                    if get_type == 'all':
                        episodes_to_monitor = [
                            ep['id'] for ep in all_episodes
                            if ep.get('seasonNumber') >= starting_season
                        ]
                        current_app.logger.info(f"Monitoring all episodes from Season {starting_season} onward")

                    elif get_type == 'seasons':
                        num_seasons = get_count or 1
                        episodes_to_monitor = [
                            ep['id'] for ep in all_episodes
                            if starting_season <= ep.get('seasonNumber') < (starting_season + num_seasons)
                        ]
                        current_app.logger.info(f"Monitoring {num_seasons} season(s) starting from Season {starting_season} ({len(episodes_to_monitor)} episodes)")

                    else:  # episodes
                        try:
                            num_episodes = get_count or 1
                            episodes_to_monitor = [ep['id'] for ep in requested_season_episodes[:num_episodes]]
                            current_app.logger.info(f"Monitoring first {len(episodes_to_monitor)} episodes of Season {starting_season}")
                        except (ValueError, TypeError):
                            episodes_to_monitor = [requested_season_episodes[0]['id']] if requested_season_episodes else []
                            current_app.logger.warning(f"Invalid get_count, defaulting to first episode")

                    if episodes_to_monitor:
                        # Monitor the selected episodes
                        monitor_response = http.put(
                            f"{SONARR_URL}/api/v3/episode/monitor",
                            headers=headers,
                            json={"episodeIds": episodes_to_monitor, "monitored": True}
                        )

                        if monitor_response.ok:
                            current_app.logger.info(f"✓ Monitored {len(episodes_to_monitor)} episodes for {series_title}")

                            # Search for episodes if action_option is 'search'
                            if action_option == 'search':
                                if get_type == 'seasons':
                                    # Use SeasonSearch for season-based rules
                                    first_ep_response = http.get(
                                        f"{SONARR_URL}/api/v3/episode/{episodes_to_monitor[0]}",
                                        headers=headers
                                    )
                                    if first_ep_response.ok:
                                        first_ep = first_ep_response.json()
                                        season_number = first_ep.get('seasonNumber')

                                        current_app.logger.info(f"Searching for season pack for Season {season_number}")
                                        search_json = {
                                            "name": "SeasonSearch",
                                            "seriesId": series_id,
                                            "seasonNumber": season_number
                                        }
                                    else:
                                        search_json = {"name": "EpisodeSearch", "episodeIds": episodes_to_monitor}
                                else:
                                    # Individual episode search
                                    search_json = {"name": "EpisodeSearch", "episodeIds": episodes_to_monitor}

                                search_response = http.post(
                                    f"{SONARR_URL}/api/v3/command",
                                    headers=headers,
                                    json=search_json
                                )

                                if search_response.ok:
                                    search_type = "season pack" if get_type == 'seasons' else "episodes"
                                    current_app.logger.info(f"✓ Started search for {search_type}")
                                else:
                                    current_app.logger.error(f"Failed to search: {search_response.text}")
                        else:
                            current_app.logger.error(f"Failed to monitor episodes: {monitor_response.text}")
                    else:
                        current_app.logger.warning(f"No episodes to monitor for {series_title}")

                    # ────────────────────────────────────────────────────────────────
                    # Remove episeerr_delay tag to allow immediate downloads
                    # ────────────────────────────────────────────────────────────────
                    try:
                        delay_tag_id = episeerr_utils.get_or_create_rule_tag_id('delay')
                        if delay_tag_id:
                            # Get fresh series data
                            series_refresh_resp = http.get(
                                f"{SONARR_URL}/api/v3/series/{series_id}",
                                headers=headers
                            )

                            if series_refresh_resp.ok:
                                fresh_series = series_refresh_resp.json()
                                current_tags = fresh_series.get('tags', [])

                                if delay_tag_id in current_tags:
                                    # Remove delay tag
                                    current_tags.remove(delay_tag_id)
                                    fresh_series['tags'] = current_tags

                                    update_resp = http.put(
                                        f"{SONARR_URL}/api/v3/series",
                                        headers=headers,
                                        json=fresh_series
                                    )

                                    if update_resp.ok:
                                        current_app.logger.info(f"✓ Removed episeerr_delay tag - downloads can proceed immediately")
                                    else:
                                        current_app.logger.error(f"Failed to remove delay tag: {update_resp.text}")
                                else:
                                    current_app.logger.debug("episeerr_delay tag not present (already removed or never added)")
                            else:
                                current_app.logger.error(f"Failed to refresh series data: {series_refresh_resp.status_code}")
                        else:
                            current_app.logger.warning("Could not get delay tag ID")

                    except Exception as e:
                        current_app.logger.error(f"Error removing delay tag: {str(e)}")
            else:
                current_app.logger.error(f"Failed to get episodes: {episodes_response.text}")
        except Exception as e:
            current_app.logger.error(f"Error executing rule: {str(e)}", exc_info=True)

        return jsonify({"status": "success", "message": "Processing completed"}), 200

    except Exception as e:
        current_app.logger.error(f"Error processing Sonarr webhook: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================================
# GRAB HANDLER (called from process_sonarr_webhook, not a route)
# ============================================================================

def handle_episode_grab(json_data):
    """
    Handle episode grab:
    1. Mark series as cleaned (stops grace checking)
    2. Log download for dashboard
    3. Delete pending Discord notifications
    """
    try:
        series = json_data.get('series', {})
        series_id = series.get('id')
        series_title = series.get('title', 'Unknown')
        episodes = json_data.get('episodes', [])

        if not episodes:
            current_app.logger.warning(f"Grab webhook for {series_title} has no episodes")
            return jsonify({"status": "success", "message": "No episodes in grab"}), 200

        episode_info = episodes[0]
        season_num = episode_info.get('seasonNumber')
        episode_num = episode_info.get('episodeNumber')
        episode_id = episode_info.get('id')

        current_app.logger.info(f"✅ Episode grabbed: {series_title} S{season_num}E{episode_num}")

        # ──────────────────────────────────────────────────────
        # 1. MARK AS CLEANED (stops grace checking)
        # ──────────────────────────────────────────────────────
        from episeerr import load_config, save_config
        config = load_config()

        for rule_name, rule in config['rules'].items():
            if str(series_id) in rule.get('series', {}):
                series_data = rule['series'][str(series_id)]

                if isinstance(series_data, dict):
                    series_data['grace_cleaned'] = True
                    save_config(config)
                    current_app.logger.info(f"✓ Marked as cleaned in rule '{rule_name}'")
                break

        # ──────────────────────────────────────────────────────
        # 2. LOG DOWNLOAD FOR DASHBOARD (7-day rolling window)
        # ──────────────────────────────────────────────────────
        try:
            from datetime import datetime, timedelta

            download_event = {
                'series_title': series_title,
                'series_id': series_id,
                'season': season_num,
                'episode': episode_num,
                'episode_title': episode_info.get('title', ''),
                'timestamp': datetime.now().isoformat()
            }

            downloads_file = os.path.join(os.getcwd(), 'data', 'recent_downloads.json')
            os.makedirs(os.path.dirname(downloads_file), exist_ok=True)

            if os.path.exists(downloads_file):
                with open(downloads_file, 'r') as f:
                    downloads = json.load(f)
            else:
                downloads = []

            # Auto-cleanup: keep only last 7 days
            cutoff = datetime.now() - timedelta(days=7)
            downloads = [
                d for d in downloads
                if datetime.fromisoformat(d['timestamp']) > cutoff
            ]

            # Remove any existing entry for this episode before adding new one
            episode_key = (download_event['series_id'], download_event['season'], download_event['episode'])
            downloads = [
                d for d in downloads
                if (d['series_id'], d['season'], d['episode']) != episode_key
            ]

            # Add new download at the front
            downloads.insert(0, download_event)

            # Keep only 50 most recent (optional limit)
            downloads = downloads[:50]

            with open(downloads_file, 'w') as f:
                json.dump(downloads, f, indent=2)

            current_app.logger.info(f"📥 Logged download for dashboard: {series_title} S{season_num}E{episode_num}")

        except Exception as e:
            current_app.logger.error(f"Error logging download for dashboard: {e}")

        # ──────────────────────────────────────────────────────
        # 3. DELETE PENDING DISCORD NOTIFICATION
        # ──────────────────────────────────────────────────────
        try:
            from notification_storage import get_and_remove_notification
            from notifications import delete_discord_message

            message_id = get_and_remove_notification(episode_id)

            if message_id:
                current_app.logger.info(f"🗑️ Deleting pending search notification for episode {episode_id}")
                if delete_discord_message(message_id):
                    current_app.logger.info(f"✅ Successfully deleted notification message {message_id}")
                else:
                    current_app.logger.warning(f"⚠️ Failed to delete notification message {message_id}")
        except ImportError:
            # Notification modules not available, skip
            pass
        except Exception as e:
            current_app.logger.error(f"Error deleting notification: {e}")

        return jsonify({"status": "success", "message": "Grab processed"}), 200

    except Exception as e:
        current_app.logger.error(f"Error handling grab webhook: {str(e)}")
        import traceback
        current_app.logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================================
# LEGACY TAUTULLI WEBHOOK
# ============================================================================

@sonarr_webhooks_bp.route('/webhook', methods=['POST'])
def handle_server_webhook():
    """
    Legacy Tautulli webhook endpoint — kept for backward compatibility.
    New installs should use /api/integration/tautulli/webhook instead.
    """
    current_app.logger.info("Received webhook on legacy /webhook route — delegating to Tautulli integration")
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400
    try:
        from integrations.tautulli import process_watch_event
        result = process_watch_event(data)
        return jsonify(result), 200 if result['status'] == 'success' else 500
    except Exception as exc:
        current_app.logger.error(f"Legacy /webhook delegation error: {exc}")
        # Fall through to the original inline logic as a safety net
    current_app.logger.info("Received webhook from Tautulli (fallback path)")
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data received'}), 400

    try:
        # Extract identifiers (original)
        series_title = data.get('plex_title') or data.get('server_title') or 'Unknown'
        season_number = data.get('plex_season_num') or data.get('server_season_num')
        episode_number = data.get('plex_ep_num') or data.get('server_ep_num')
        thetvdb_id = data.get('thetvdb_id')
        themoviedb_id = data.get('themoviedb_id')

        # ─── Tag sync & drift correction BEFORE processing ───
        from media_processor import get_series_id
        from episeerr import load_config, save_config

        series_id = get_series_id(series_title, thetvdb_id, themoviedb_id)
        final_rule = None
        if not series_id:
            current_app.logger.warning(f"Could not find Sonarr series ID for '{series_title}'")
        else:
            config = load_config()
            final_rule, modified = episeerr_utils.reconcile_series_drift(series_id, config)
            if modified:
                save_config(config)

        # ─── Original temp file creation ───
        temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(temp_dir, exist_ok=True)

        plex_data = {
            "server_title": series_title,
            "server_season_num": season_number,
            "server_ep_num": episode_number,
            "thetvdb_id": thetvdb_id,
            "themoviedb_id": themoviedb_id,
            "sonarr_series_id": series_id,
            "rule": final_rule
        }

        temp_file_path = os.path.join(temp_dir, 'data_from_server.json')
        with open(temp_file_path, 'w') as f:
            json.dump(plex_data, f)

        # ─── Original subprocess call ───
        result = subprocess.run(
            ["python3", os.path.join(os.getcwd(), "media_processor.py")],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            current_app.logger.error(f"media_processor.py failed with return code {result.returncode}")
            if result.stderr:
                current_app.logger.error(f"Error output: {result.stderr}")
        else:
            current_app.logger.info("media_processor.py completed successfully")
            if result.stderr:
                current_app.logger.info(f"Processor output: {result.stderr}")

        current_app.logger.info("Webhook processing completed - activity tracked, next content processed")
        return jsonify({'status': 'success'}), 200

    except Exception as e:
        current_app.logger.error(f"Failed to process Tautulli webhook: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
