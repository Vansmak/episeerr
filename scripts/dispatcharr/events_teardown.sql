-- =============================================================================
-- Game-Day Events Teardown
-- =============================================================================
-- Standalone script, run on its own cadence (every 30-60 min from
-- integrations/events.py's background tick) -- NOT appended to or run as
-- part of maintenance.sql. maintenance.sql's Parts 1-9 were designed around
-- a post-M3U-sync context and haven't been audited for safety on a 30-min
-- timer outside that context, so teardown gets its own file instead.
--
-- events.py stages the current whitelist via `docker cp` into this
-- container at /events_whitelist.txt immediately before invoking this
-- script. `docker cp` either fully lands the file or fails outright (never
-- a partial/truncated file), and events.py skips running this script
-- entirely if staging fails -- so by the time this runs, the file is either
-- complete and current, or genuinely doesn't exist yet (no tick has ever
-- succeeded, in which case no event channels exist either and the DELETE
-- below is a no-op).
--
-- Deletes any channel in the "Events Today" group whose tvg_id (set to
-- "events:<league>:<espn_event_id>" at creation time) is not in the fresh
-- whitelist. Not gated on auto_created -- channels created via the
-- from-stream API are not flagged auto_created (that flag is specific to
-- Dispatcharr's own M3U auto-channel-sync), so this group is exclusively
-- populated by events.py and is safe to sweep by group membership alone.
-- Do not put manually-created channels in "Events Today" -- they will be
-- deleted the next time their tvg_id isn't in the whitelist.
--
-- Usage:  docker exec -i dispatcharr psql -U postgres -d dispatcharr \
--             < events_teardown.sql
-- Idempotent -- safe to run every tick.
-- =============================================================================

\echo '── Events teardown ──'

CREATE TEMP TABLE _events_whitelist (event_key text);
\copy _events_whitelist (event_key) FROM PROGRAM 'cat /events_whitelist.txt 2>/dev/null || true'

CREATE TEMP TABLE _expired_events AS
SELECT c.id, c.name, c.tvg_id
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name = 'Events Today'
  AND (c.tvg_id IS NULL OR c.tvg_id NOT IN (SELECT event_key FROM _events_whitelist));

\echo 'Events expiring:'
SELECT COUNT(*) AS events_expiring FROM _expired_events;
SELECT id, name, tvg_id FROM _expired_events ORDER BY id;

DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id IN (SELECT id FROM _expired_events);
DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id IN (SELECT id FROM _expired_events);
DELETE FROM dispatcharr_channels_channelstream           WHERE channel_id IN (SELECT id FROM _expired_events);
DELETE FROM dispatcharr_channels_channel WHERE id IN (SELECT id FROM _expired_events);

SELECT COUNT(*) AS events_deleted FROM _expired_events;

DROP TABLE _events_whitelist;
DROP TABLE _expired_events;

\echo ''
\echo 'Events teardown complete.'
