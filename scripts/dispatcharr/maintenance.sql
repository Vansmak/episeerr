-- =============================================================================
-- Dispatcharr Maintenance Script
-- =============================================================================
-- Covers:
--   3. Source group → target group mapping cleanup
--   6. Whitelist enforcement — delete auto_created not in approved tvg_id list
--   7. Foreign/junk name pattern cleanup
--   9. Stream merging — consolidate Titan+Direct into single channels,
--      stack OTA on LA locals
--   8. Channel numbering — runs last after all merges and deletes
--
-- Provider architecture:
--   Titan  (XC, pxlsystems.cx) → target groups: Entertainment, Movies, News,
--          Sports, Documentary, Locals, 4K, PPV
--          Source prefixes: USA |, Live Pay-Per View
--   Direct (XC)                → target groups: same as Titan (merged in)
--          Source prefixes: US:
--   HDHR   (OTA, 192.168.254.30) → Favorites group; LA streams stacked manually
--
-- Channel structure after merge:
--   Each network = one channel, all Titan+Direct streams stacked
--   LA locals: OTA first, then Titan, then Direct
--   Timezone locals: Titan only (variety)
--   PPV: hidden when no event (Event Channel Managarr plugin)
--
-- Guide blocks:
--   2/4/5/7/9/11     LA locals
--   2.1-11.x         Timezone locals (Denver .1, Chicago .2, NY .3, Miami .4)
--   101+             News
--   201+             Sports
--   301+             Documentary
--   401+             Entertainment
--   501+             Movies
--   601+             4K
--   900+             PPV
--
-- Safety: Only touches auto_created = true channels.
-- Usage:  docker exec -i dispatcharr psql -U postgres -d dispatcharr \
--             < /home/joe/config/dispatcharr/scripts/maintenance.sql
-- Idempotent — safe to run repeatedly after each provider sync.
-- =============================================================================

\echo '============================================================'
\echo ' Dispatcharr Maintenance'
\echo '============================================================'

-- ═══════════════════════════════════════════════════════════════════════════
-- PART 3: Source-group → target-group migration
-- Maps provider source groups to clean target groups after each sync.
-- ═══════════════════════════════════════════════════════════════════════════

\echo ''
\echo '── Part 3: Source-group → target-group migration ──'

WITH mapping AS (
  SELECT g_src.id AS src_id, g_tgt.id AS tgt_id
  FROM dispatcharr_channels_channelgroup g_src
  JOIN dispatcharr_channels_channelgroup g_tgt ON g_tgt.name =
    CASE
      -- Titan source groups → clean target groups
      WHEN g_src.name LIKE 'USA | Entertainment%'  THEN 'Entertainment'
      WHEN g_src.name LIKE 'USA | Movies%'          THEN 'Movies'
      WHEN g_src.name LIKE 'USA | News%'            THEN 'News'
      WHEN g_src.name LIKE 'USA | Sports%'          THEN 'Sports'
      WHEN g_src.name LIKE 'USA | Documentary%'     THEN 'Documentary'
      WHEN g_src.name LIKE 'USA | Local%'           THEN 'Locals'
      WHEN g_src.name LIKE '4K / UHD%'              THEN '4K'
      WHEN g_src.name LIKE 'Live Pay-Per View%'     THEN 'PPV'
      WHEN g_src.name LIKE 'PPV%'                   THEN 'PPV'
      -- Direct source groups → clean target groups
      WHEN g_src.name ILIKE 'US: Entertainment%'    THEN 'Entertainment'
      WHEN g_src.name ILIKE 'US: Movie%'            THEN 'Movies'
      WHEN g_src.name ILIKE 'US: News%'             THEN 'News'
      WHEN g_src.name ILIKE 'US: Sports%'           THEN 'Sports'
      WHEN g_src.name ILIKE 'US: Regional Sports%'  THEN 'Sports'
      WHEN g_src.name ILIKE 'US: Factual%'          THEN 'Documentary'
      WHEN g_src.name ILIKE 'US: LOCALS%'           THEN 'Locals'
    END
  WHERE g_src.name LIKE 'USA |%'
     OR g_src.name ILIKE 'US:%'
     OR g_src.name LIKE '4K / UHD%'
     OR g_src.name LIKE 'Live Pay-Per View%'
     OR g_src.name LIKE 'PPV%'
)
UPDATE dispatcharr_channels_channel c
SET channel_group_id = m.tgt_id
FROM mapping m
WHERE c.channel_group_id = m.src_id
  AND c.auto_created = true;

SELECT COUNT(*) AS part3_channels_in_target_groups
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name IN (
  'Entertainment','Movies','News','Sports','Documentary','Locals','4K','PPV'
)
AND c.auto_created = true;

-- ═══════════════════════════════════════════════════════════════════════════
-- PART 6: Whitelist enforcement
-- Deletes auto_created channels whose tvg_id is not approved.
-- ═══════════════════════════════════════════════════════════════════════════

\echo ''
\echo '── Part 6: Whitelist enforcement ──'

CREATE TEMP TABLE _approved_tvgids (tvg_id text);
INSERT INTO _approved_tvgids (tvg_id) VALUES
  -- 4K
  ('DirecTV4KLive2_106.us'),
  ('DirecTV4KLive_105.us'),
  ('ESPN4K.us'),
  ('FOX4K.us'),
  -- LA Locals
  ('ABCKABC.us'),
  ('CBSKCBS.us'),
  ('CWKTLA.us'),
  ('FOXKTTV.us'),
  ('NBCKNBC.us'),
  -- Denver Locals
  ('ABCKMGH.us'),
  ('CBSKCNC.us'),
  ('FOXKDVR.us'),
  ('NBCKUSA.us'),
  -- Chicago Locals
  ('ABCWLS.us'),
  ('CBSWBBM.us'),
  ('FOXWFLD.us'),
  ('NBCWMAQ.us'),
  -- New York Locals
  ('ABCWABC.us'),
  ('CBSWCBS.us'),
  ('CWWPIX.us'),
  ('FOXWNYW.us'),
  ('NBCWNBC.us'),
  -- Miami Locals
  ('ABCWPLG.us'),
  ('CBSWFOR.us'),
  ('FOXWSVN.us'),
  ('NBCWTVJ.us'),
  -- Other locals
  ('ABCKVUE.us'),
  ('ABCWAAY.us'),
  ('ABCWABMDT2.us'),
  ('ABCWEAR.us'),
  ('ABCWFTV.us'),
  ('ABCWJXX.us'),
  ('CBSKEYE.us'),
  ('CBSWHNT.us'),
  ('CBSWIAT.us'),
  ('CBSWJAX.us'),
  ('CBSWKMG.us'),
  ('CBSWKRG.us'),
  ('CBSWTSP.us'),
  ('CWWCWJ.us'),
  ('CWWKCF.us'),
  ('CWWTOG.us'),
  ('CWWTTO.us'),
  ('FOXKTBC.us'),
  ('FOXWALA.us'),
  ('FOXWBRC.us'),
  ('FOXWFOX.us'),
  ('FOXWOFL.us'),
  ('FOXWTVT.us'),
  ('FOXWZDX.us'),
  ('NBCKXAN.us'),
  ('NBCWAFF.us'),
  ('NBCWESH.us'),
  ('NBCWFLA.us'),
  ('NBCWPMI.us'),
  ('NBCWSFA.us'),
  ('NBCWTLV.us'),
  ('NBCWTVJ.us'),
  ('NBCWVTM.us'),
  -- News
  ('ABCNewsLive.us'),
  ('Accuweather.us'),
  ('BBCAmerica.us'),
  ('BBCWorldNews.us'),
  ('BlazeTV.us'),
  ('CNBC.us'),
  ('CNBCWorld.us'),
  ('CNN.us'),
  ('CNNinternational.us'),
  ('CSPAN.us'),
  ('FOXWeather.us'),
  ('FoxBusiness.us'),
  ('FoxNewsChannel.us'),
  ('FreeSpeechTV.us'),
  ('HLN.us'),
  ('LiveNOWfromFOX.us'),
  ('MSNOW.us'),
  ('NBCLX.us'),
  ('NewsNation.us'),
  ('Newsmax2.us'),
  ('NewsmaxTV.us'),
  ('OneAmericaNewsNetwork.us'),
  ('TheWeatherChannel.us'),
  -- Sports
  ('ESPN.us'),
  ('ESPN2.us'),
  ('FoxSports1.us'),
  ('FoxSports2.us'),
  ('NFLNetwork.us'),
  ('NHLNetwork.us'),
  ('SpectrumSportsNetLADodgers.us'),
  -- Documentary
  ('AmericanHeroesChannel.us'),
  ('AnimalPlanet.us'),
  ('AnimalPlanetPacific.us'),
  ('CuriousityStream.us'),
  ('DRCTV'),
  ('DestinationAmerica.us'),
  ('DiscoveryChannel.us'),
  ('DiscoveryChannelPacific.us'),
  ('DiscoveryFamilyChannel.us'),
  ('DiscoveryLifeChannel.us'),
  ('DiscoveryTurbo.us'),
  ('DogTV.us'),
  ('EarthxTV.us'),
  ('INFAST.us'),
  ('InvestigationDiscovery.us'),
  ('InvestigationDiscoveryPacific.us'),
  ('LoveNature.ca'),
  ('MilitaryHeroesByHistory.us'),
  ('MilitaryHistoryChannel.us'),
  ('NationalGeographic.us'),
  ('NationalGeographicPacific.us'),
  ('NationalGeographicWild.us'),
  ('OutdoorChannel.us'),
  ('OutsideTelevision.us'),
  ('RFDTV.us'),
  ('ScienceChannel.us'),
  ('SmithsonianNetwork.us'),
  ('TheCowboyChannel.us'),
  ('TravelChannel.us'),
  ('TheTravelChannelPacific.us'),
  ('Vice.us'),
  -- Entertainment
  ('50CentAction.us'),
  ('ALTER.us'),
  ('AMC.us'),
  ('AMCPacific.us'),
  ('AMCPlus.us'),
  ('AMCThrillers.us'),
  ('ASPiRE.us'),
  ('AWE.us'),
  ('AWEPlus.us'),
  ('AXSTV.us'),
  ('AXSTVNOW.us'),
  ('AandENetwork.us'),
  ('AandENetworkPacific.us'),
  ('AntennaTV.us'),
  ('AtHomewithFamilyHandyman.us'),
  ('BUZZR.us'),
  ('BounceTV.us'),
  ('Bravo.us'),
  ('BravoPacific.us'),
  ('CMT.us'),
  ('CMTPacific.us'),
  ('CartoonNetwork.us'),
  ('CarsTV.us'),
  ('CatchyComedy.us'),
  ('Charge!.us'),
  ('Choppertown.us'),
  ('CleoTV.us'),
  ('ComedyCentral.us'),
  ('ComedyCentralPacific.us'),
  ('ComedyDynamics.us'),
  ('Comet.us'),
  ('CookingChannel.us'),
  ('CourtTV.us'),
  ('CoziTV.us'),
  ('CrimeandInvestigationNetwork.us'),
  ('Dabl.us'),
  ('EEntertainmentTelevision.us'),
  ('EEntertainmentTelevisionPacific.us'),
  ('EbonyTV.us'),
  ('FETV.us'),
  ('FOXSOUL.us'),
  ('FX.us'),
  ('FXPacific.us'),
  ('FXX.us'),
  ('FXXPacific.us'),
  ('FYIChannel.us'),
  ('FYIPacific.us'),
  ('FailArmy.us'),
  ('Freeform.us'),
  ('FreeformPacific.us'),
  ('Fuse.us'),
  ('GameShowNetwork.us'),
  ('GameShowNetworkPacific.us'),
  ('GreatAmericanAdventures.us'),
  ('GreatAmericanFamily.us'),
  ('GreatEntertainmentTelevision.us'),
  ('Grit.us'),
  ('HGTV.us'),
  ('HGTVPacific.us'),
  ('HSN.us'),
  ('HSN2.us'),
  ('HallmarkChannel.us'),
  ('HallmarkChannelPacific.us'),
  ('HallmarkFamily.us'),
  ('HallmarkMystery.us'),
  ('HerSphere.us'),
  ('HeroesAndIconsKPRCDT3.us'),
  ('History.us'),
  ('HistoryChannelPacific.us'),
  ('IFC.us'),
  ('INSP.us'),
  ('IONPlus.us'),
  ('IonMystery.us'),
  ('IonTV.us'),
  ('JewelryTelevision.us'),
  ('JusticeCentral.us'),
  ('Laff.us'),
  ('Lifetime.us'),
  ('LifetimeMovieNetworkPacific.us'),
  ('LifetimeMoviesNetwork.us'),
  ('LifetimePacific.us'),
  ('LifetimeRealWomen.us'),
  ('Logo.us'),
  ('MTV.us'),
  ('MTV2.us'),
  ('MTV2Pacific.us'),
  ('MTVClassic.us'),
  ('MTVClassicPacific.us'),
  ('MTVLive.us'),
  ('MTVPacific.us'),
  ('MagnoliaNetwork.us'),
  ('MeTV+.us'),
  ('MeTV.us'),
  ('MOVIESPHERE.us'),
  ('Nosey.us'),
  ('OWN.us'),
  ('OWNPacific.us'),
  ('OvationTV.us'),
  ('OxygenPacific.us'),
  ('POP.us'),
  ('POPPacific.us'),
  ('POWERNATION.us'),
  ('ParamountNetwork.us'),
  ('ParamountNetworkPacific.us'),
  ('PixL.us'),
  ('PureFlixTV.us'),
  ('PursuitChannel.us'),
  ('QVC.us'),
  ('QVC2.us'),
  ('QVC3.us'),
  ('ReelzChannel.us'),
  ('Revolt.us'),
  ('Roar.us'),
  ('ShortsTV.us'),
  ('StartTV.us'),
  ('StoriesbyAMC.us'),
  ('SundanceTV.us'),
  ('Syfy.us'),
  ('SyfyPacific.us'),
  ('TBS.us'),
  ('TBSPacific.us'),
  ('TLC.us'),
  ('TLCPacific.us'),
  ('TNT.us'),
  ('TNTPacific.us'),
  ('TVLand.us'),
  ('TVLandPacific.us'),
  ('TVOne.us'),
  ('Tastemade.us'),
  ('TastemadeHome.us'),
  ('TastemadeTravel.us'),
  ('TheBobRossChannel.us'),
  ('TheDesignNetwork.us'),
  ('TheFilipinoChannel.us'),
  ('ThePetCollective.us'),
  ('TrueCrimeNetwork.us'),
  ('UP.us'),
  ('USANetwork.us'),
  ('USANetworkPacific.us'),
  ('VH1.us'),
  ('VH1Pacific.us'),
  ('VictoryChannel.us'),
  ('WAPAAmerica.us'),
  ('WeTV.us'),
  -- Movies
  ('CINEVAULTClassics.us'),
  ('CINEVAULTWesterns.us'),
  ('CineMAX.us'),
  ('CineMAXPacific.us'),
  ('CinemaxAction.us'),
  ('CinemaxClassics.us'),
  ('CinemaxHits.us'),
  ('Cinevault.us'),
  ('FLIX.us'),
  ('FlixPacific.us'),
  ('FXMovies.us'),
  ('HBO.us'),
  ('HBOComedy.us'),
  ('HBOComedyPacific.us'),
  ('HBODrama.us'),
  ('HBOHits.us'),
  ('HBOHitsPacific.us'),
  ('HBOMovies.us'),
  ('HBOMoviesPacific.us'),
  ('HBOPacific.us'),
  ('HDNetMovies.us'),
  ('IndiePlex.us'),
  ('MGM+.us'),
  ('MGM+DriveIn.us'),
  ('MGM+Hits.us'),
  ('MGM+Marquee.us'),
  ('MoviePlex.us'),
  ('Movies!.us'),
  ('Paramount+withShowtime.us'),
  ('Paramount+withShowtimePacific.us'),
  ('RetroPlex.us'),
  ('RetroPlexPacific.us'),
  ('ScreenPix.us'),
  ('ScreenPixAction.us'),
  ('ScreenPixVoices.us'),
  ('ScreenPixWesterns.us'),
  ('Showtime2.us'),
  ('Showtime2Pacific.us'),
  ('ShowtimeExtreme.us'),
  ('ShowtimeExtremePacific.us'),
  ('ShowtimeFamilyzone.us'),
  ('ShowtimeFamilyzonePacific.us'),
  ('ShowtimeNext.us'),
  ('ShowtimeShowcase.us'),
  ('ShowtimeShowcasePacific.us'),
  ('ShowtimeWomen.us'),
  ('SonyMovieChannel.us'),
  ('Starz.us'),
  ('StarzCinema.us'),
  ('StarzCinemaPacific.us'),
  ('StarzComedy.us'),
  ('StarzComedyPacific.us'),
  ('StarzEdge.us'),
  ('StarzEdgePacific.us'),
  ('StarzEncore.us'),
  ('StarzEncoreAction.us'),
  ('StarzEncoreBlack.us'),
  ('StarzEncoreClassic.us'),
  ('StarzEncoreClassicPacific.us'),
  ('StarzEncoreFamily.us'),
  ('StarzEncorePacific.us'),
  ('StarzEncoreSuspense.us'),
  ('StarzEncoreWesterns.us'),
  ('StarzInBlack.us'),
  ('StarzKids.us'),
  ('StarzKidsPacific.us'),
  ('StarzPacific.us'),
  ('StarzinBlackPacific.us'),
  ('TCMPacific.us'),
  ('TheMovieChannel.us'),
  ('TheMovieChannelExtra.us'),
  ('TheMovieChannelExtraWest.us'),
  ('TheMovieChannelWest.us');

-- PPV channels have no tvg_id — they are handled by name pattern, not whitelist
-- So we only delete channels WITH a tvg_id that isn't approved

CREATE TEMP TABLE _whitelist_violators AS
  SELECT c.id, c.name, c.tvg_id
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE c.auto_created = true
    AND c.tvg_id IS NOT NULL AND c.tvg_id <> ''
    AND LOWER(c.tvg_id) NOT IN (SELECT LOWER(tvg_id) FROM _approved_tvgids)
    -- Never delete PPV channels via whitelist
    AND g.name != 'PPV';

SELECT COUNT(*) AS channels_not_in_whitelist FROM _whitelist_violators;

SELECT id, name, tvg_id AS deleted_tvg_id
FROM _whitelist_violators
ORDER BY tvg_id, id;

DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id IN (SELECT id FROM _whitelist_violators);
DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id IN (SELECT id FROM _whitelist_violators);
DELETE FROM dispatcharr_channels_channelstream           WHERE channel_id IN (SELECT id FROM _whitelist_violators);

DELETE FROM dispatcharr_channels_channel
WHERE id IN (SELECT id FROM _whitelist_violators);

SELECT COUNT(*) AS part6_channels_deleted FROM _whitelist_violators;

-- ═══════════════════════════════════════════════════════════════════════════
-- PART 7: Name-pattern deletions for channels with no tvg_id
-- ═══════════════════════════════════════════════════════════════════════════

\echo ''
\echo '── Part 7: Name-pattern deletions ──'

CREATE TEMP TABLE _pattern_deletions AS
  SELECT
    c.id, c.name, c.tvg_id, c.epg_data_id,
    CASE
      WHEN c.name ~* '^(FR|BG|NL|CZ|PL|HK|CY|TH|RU|DE|AR|PT|IT|TR|AL|Alb|AT|CH|ES|FI|RO|MY|AU|Astro):'
           THEN 'foreign prefix'
      WHEN c.name ILIKE '%4K UHD%' AND c.epg_data_id IS NULL
           THEN '4K UHD no EPG'
      WHEN c.name ILIKE '%CineMania%'    THEN 'CineMania'
      WHEN c.name LIKE '%*%'             THEN 'asterisk in name'
      WHEN c.name ILIKE '%group-title=%' THEN 'malformed (group-title=)'
      WHEN c.name ILIKE 'LBW:%'          THEN 'LBW: low-bandwidth dupe'
      WHEN c.name ILIKE '%Latino%'       THEN 'Latino channel'
      WHEN c.name ILIKE '%24/7%'         THEN '24/7 loop channel'
    END AS reason
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE c.auto_created = true
    -- Never delete PPV channels
    AND g.name != 'PPV'
    AND (
      c.name ~* '^(FR|BG|NL|CZ|PL|HK|CY|TH|RU|DE|AR|PT|IT|TR|AL|Alb|AT|CH|ES|FI|RO|MY|AU|Astro):'
      OR (c.name ILIKE '%4K UHD%' AND c.epg_data_id IS NULL)
      OR c.name ILIKE '%CineMania%'
      OR c.name LIKE '%*%'
      OR c.name ILIKE '%group-title=%'
      OR c.name ILIKE 'LBW:%'
      OR c.name ILIKE '%Latino%'
      OR c.name ILIKE '%24/7%'
    );

\echo 'Part 7 — channels matched by reason:'
SELECT reason, COUNT(*) AS channel_count
FROM _pattern_deletions
GROUP BY reason ORDER BY reason;

SELECT COUNT(*) AS part7_total_to_delete FROM _pattern_deletions;

SELECT COUNT(*) AS manual_channels_accidentally_matched
FROM dispatcharr_channels_channel c
JOIN _pattern_deletions d ON d.id = c.id
WHERE c.auto_created = false;

DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id IN (SELECT id FROM _pattern_deletions);
DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id IN (SELECT id FROM _pattern_deletions);
DELETE FROM dispatcharr_channels_channelstream           WHERE channel_id IN (SELECT id FROM _pattern_deletions);

DELETE FROM dispatcharr_channels_channel
WHERE id IN (SELECT id FROM _pattern_deletions);

SELECT COUNT(*) AS part7_channels_deleted FROM _pattern_deletions;

-- ═══════════════════════════════════════════════════════════════════════════
-- PART 9: Stream merging
--
-- Step A: Consolidate all channels with same tvg_id into one entry.
--         Move all streams to survivor, strip dupes, delete empties.
-- Step B: LA locals — merge Direct into Titan, stack OTA at order 0.
-- Step C: Timezone locals — Titan only, drop Direct, dedup.
-- Step D: Stack OTA streams on LA locals (hardcoded HDHR stream IDs).
-- ═══════════════════════════════════════════════════════════════════════════

\echo ''
\echo '── Part 9: Stream merging ──'

-- ── Step A: Consolidate Titan+Direct into single channel per tvg_id ───────

\echo 'Step A: consolidating all streams into single channel per tvg_id...'

CREATE TEMP TABLE _merge_groups AS
SELECT id FROM dispatcharr_channels_channelgroup
WHERE name IN (
  'News','Sports','Documentary','Entertainment','Movies',
  'Direct - News','Direct - Sports','Direct - Factual','Direct - Entmt','Direct - Movies'
);

CREATE TEMP TABLE _survivors AS
SELECT LOWER(tvg_id) AS tvg_id_lower, MIN(id) AS survivor_id
FROM dispatcharr_channels_channel
WHERE auto_created = true
  AND tvg_id IS NOT NULL AND tvg_id != ''
  AND channel_group_id IN (SELECT id FROM _merge_groups)
GROUP BY LOWER(tvg_id);

-- Move unique streams from non-survivors to survivor
WITH to_move AS (
  SELECT cs.id AS stream_id, s.survivor_id AS target_channel_id
  FROM dispatcharr_channels_channel c
  JOIN _survivors s ON LOWER(c.tvg_id) = s.tvg_id_lower AND c.id != s.survivor_id
  JOIN dispatcharr_channels_channelstream cs ON cs.channel_id = c.id
  WHERE c.auto_created = true
    AND c.channel_group_id IN (SELECT id FROM _merge_groups)
    AND NOT EXISTS (
      SELECT 1 FROM dispatcharr_channels_channelstream
      WHERE channel_id = s.survivor_id AND stream_id = cs.stream_id
    )
)
UPDATE dispatcharr_channels_channelstream cs
SET channel_id = m.target_channel_id
FROM to_move m
WHERE cs.id = m.stream_id;

-- Strip remaining streams from non-survivors (already on survivor)
DELETE FROM dispatcharr_channels_channelstream
WHERE channel_id IN (
  SELECT c.id FROM dispatcharr_channels_channel c
  JOIN _survivors s ON LOWER(c.tvg_id) = s.tvg_id_lower AND c.id != s.survivor_id
  WHERE c.auto_created = true
    AND c.channel_group_id IN (SELECT id FROM _merge_groups)
);

-- Move survivors in Direct groups to correct Titan group
UPDATE dispatcharr_channels_channel c
SET channel_group_id = (
  SELECT id FROM dispatcharr_channels_channelgroup
  WHERE name = CASE
    WHEN (SELECT name FROM dispatcharr_channels_channelgroup WHERE id = c.channel_group_id) = 'Direct - News'    THEN 'News'
    WHEN (SELECT name FROM dispatcharr_channels_channelgroup WHERE id = c.channel_group_id) = 'Direct - Sports'  THEN 'Sports'
    WHEN (SELECT name FROM dispatcharr_channels_channelgroup WHERE id = c.channel_group_id) = 'Direct - Factual' THEN 'Documentary'
    WHEN (SELECT name FROM dispatcharr_channels_channelgroup WHERE id = c.channel_group_id) = 'Direct - Entmt'   THEN 'Entertainment'
    WHEN (SELECT name FROM dispatcharr_channels_channelgroup WHERE id = c.channel_group_id) = 'Direct - Movies'  THEN 'Movies'
  END
)
WHERE c.auto_created = true
  AND c.channel_group_id IN (
    SELECT id FROM dispatcharr_channels_channelgroup
    WHERE name IN ('Direct - News','Direct - Sports','Direct - Factual','Direct - Entmt','Direct - Movies')
  )
  AND c.id IN (SELECT survivor_id FROM _survivors);

-- Delete now-streamless duplicate channels
DO $$
DECLARE _dead int[];
BEGIN
  SELECT ARRAY(
    SELECT id FROM dispatcharr_channels_channel
    WHERE auto_created = true
      AND channel_group_id IN (SELECT id FROM _merge_groups)
      AND id NOT IN (SELECT DISTINCT channel_id FROM dispatcharr_channels_channelstream)
  ) INTO _dead;
  DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channel                 WHERE id         = ANY(_dead);
END $$;

SELECT COUNT(*) AS part9a_channels_after_consolidation
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name IN ('News','Sports','Documentary','Entertainment','Movies')
AND c.auto_created = true;

-- ── Step B: LA locals — Direct into Titan ────────────────────────────────

\echo 'Step B: merging LA local streams...'

CREATE TEMP TABLE _la_tvgids (tvg_id text);
INSERT INTO _la_tvgids VALUES
  ('CBSKCBS.us'),('NBCKNBC.us'),('CWKTLA.us'),
  ('ABCKABC.us'),('FOXKTTV.us');

-- Pick survivor (min id) per LA tvg_id across Locals and Direct-Locals
CREATE TEMP TABLE _la_survivors AS
SELECT LOWER(la.tvg_id) AS tvg_id_lower, MIN(c.id) AS survivor_id
FROM _la_tvgids la
JOIN dispatcharr_channels_channel c ON LOWER(c.tvg_id) = LOWER(la.tvg_id)
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name IN ('Locals','Direct - Locals') AND c.auto_created = true
GROUP BY LOWER(la.tvg_id);

-- Move streams from non-survivors to survivor
WITH to_move AS (
  SELECT cs.id AS stream_id, s.survivor_id AS target_channel_id
  FROM dispatcharr_channels_channel c
  JOIN _la_survivors s ON LOWER(c.tvg_id) = s.tvg_id_lower AND c.id != s.survivor_id
  JOIN dispatcharr_channels_channelstream cs ON cs.channel_id = c.id
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name IN ('Locals','Direct - Locals') AND c.auto_created = true
    AND NOT EXISTS (
      SELECT 1 FROM dispatcharr_channels_channelstream
      WHERE channel_id = s.survivor_id AND stream_id = cs.stream_id
    )
)
UPDATE dispatcharr_channels_channelstream cs
SET channel_id = m.target_channel_id
FROM to_move m
WHERE cs.id = m.stream_id;

-- Ensure each survivor lands in Locals (in case it started in Direct-Locals)
UPDATE dispatcharr_channels_channel
SET channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals')
WHERE id IN (SELECT survivor_id FROM _la_survivors)
  AND channel_group_id != (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

-- Force-remove all channelstream entries from non-survivors, then delete them
DO $$
DECLARE _dead int[];
BEGIN
  SELECT ARRAY(
    SELECT c.id FROM dispatcharr_channels_channel c
    JOIN _la_survivors s ON LOWER(c.tvg_id) = s.tvg_id_lower AND c.id != s.survivor_id
    JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
    WHERE g.name IN ('Locals','Direct - Locals') AND c.auto_created = true
  ) INTO _dead;
  DELETE FROM dispatcharr_channels_channelstream           WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channel                 WHERE id         = ANY(_dead);
END $$;

\echo 'LA locals merged.'

-- ── Step C: Timezone locals — Titan only, drop Direct, dedup ─────────────

\echo 'Step C: cleaning timezone locals...'

CREATE TEMP TABLE _tz_tvgids (tvg_id text);
INSERT INTO _tz_tvgids VALUES
  ('CBSKCNC.us'),('NBCKUSA.us'),('ABCKMGH.us'),('FOXKDVR.us'),
  ('CBSWBBM.us'),('NBCWMAQ.us'),('ABCWLS.us'),('FOXWFLD.us'),
  ('CBSWCBS.us'),('NBCWNBC.us'),('ABCWABC.us'),('FOXWNYW.us'),('CWWPIX.us'),
  ('CBSWFOR.us'),('NBCWTVJ.us'),('ABCWPLG.us'),('FOXWSVN.us');

DELETE FROM dispatcharr_channels_channelstream
WHERE channel_id IN (
  SELECT c.id FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name IN ('Direct - Locals','Locals')
    AND LOWER(c.tvg_id) IN (SELECT LOWER(tvg_id) FROM _tz_tvgids)
    AND c.auto_created = true
    AND c.id NOT IN (
      SELECT MIN(id) FROM dispatcharr_channels_channel
      WHERE auto_created = true
        AND channel_group_id IN (
          SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals'
        )
        AND LOWER(tvg_id) IN (SELECT LOWER(tvg_id) FROM _tz_tvgids)
      GROUP BY tvg_id
    )
);

DO $$
DECLARE _dead int[];
BEGIN
  SELECT ARRAY(
    SELECT c.id FROM dispatcharr_channels_channel c
    JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
    WHERE g.name IN ('Locals','Direct - Locals')
      AND LOWER(c.tvg_id) IN (SELECT LOWER(tvg_id) FROM _tz_tvgids)
      AND c.auto_created = true
      AND c.id NOT IN (
        SELECT MIN(id) FROM dispatcharr_channels_channel
        WHERE auto_created = true
          AND channel_group_id IN (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals')
          AND LOWER(tvg_id) IN (SELECT LOWER(tvg_id) FROM _tz_tvgids)
        GROUP BY tvg_id
      )
  ) INTO _dead;
  DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channel                 WHERE id         = ANY(_dead);
END $$;

-- Drop any remaining Direct - Locals
DO $$
DECLARE _dead int[];
BEGIN
  SELECT ARRAY(
    SELECT c.id FROM dispatcharr_channels_channel c
    JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
    WHERE g.name = 'Direct - Locals' AND c.auto_created = true
  ) INTO _dead;
  DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channelstream           WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channel                 WHERE id         = ANY(_dead);
END $$;

SELECT COUNT(*) AS part9_remaining_direct_channels
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name LIKE 'Direct -%' AND c.auto_created = true;

-- ── Step D: Stack OTA streams under LA Titan channels ────────────────────
-- OTA stream IDs are fixed HDHR streams:
--   183985=KCBS-HD, 184151=KNBC NX, 184152=KTLA HD, 183999=KABC DT, 184009=KTTV-DT

\echo 'Step D: stacking OTA streams on LA locals...'

WITH ota_map (tvg_id, stream_id) AS (
  VALUES
    ('CBSKCBS.us', 183985),
    ('NBCKNBC.us', 184151),
    ('CWKTLA.us',  184152),
    ('ABCKABC.us', 183999),
    ('FOXKTTV.us', 184009)
),
la_channels AS (
  SELECT c.id AS channel_id, o.stream_id
  FROM ota_map o
  JOIN dispatcharr_channels_channel c ON LOWER(c.tvg_id) = LOWER(o.tvg_id)
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'Locals'
    AND c.auto_created = true
)
UPDATE dispatcharr_channels_channelstream cs
SET "order" = "order" + 1
FROM la_channels lc
WHERE cs.channel_id = lc.channel_id
  AND NOT EXISTS (
    SELECT 1 FROM dispatcharr_channels_channelstream
    WHERE channel_id = lc.channel_id
      AND stream_id = lc.stream_id
      AND "order" = 0
  );

INSERT INTO dispatcharr_channels_channelstream (channel_id, stream_id, "order")
SELECT lc.channel_id, lc.stream_id, 0
FROM (
  SELECT c.id AS channel_id, o.stream_id
  FROM (VALUES
    ('CBSKCBS.us', 183985),
    ('NBCKNBC.us', 184151),
    ('CWKTLA.us',  184152),
    ('ABCKABC.us', 183999),
    ('FOXKTTV.us', 184009)
  ) AS o(tvg_id, stream_id)
  JOIN dispatcharr_channels_channel c ON LOWER(c.tvg_id) = LOWER(o.tvg_id)
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'Locals'
    AND c.auto_created = true
) lc
WHERE NOT EXISTS (
  SELECT 1 FROM dispatcharr_channels_channelstream
  WHERE channel_id = lc.channel_id AND stream_id = lc.stream_id
);

SELECT COUNT(*) AS part9d_ota_streams_verified;

-- ═══════════════════════════════════════════════════════════════════════════
-- PART 8: Channel numbering
-- Runs last — after all merges and deletes.
-- ═══════════════════════════════════════════════════════════════════════════

\echo ''
\echo '── Part 8: Channel numbering ──'

-- ── LA Locals ─────────────────────────────────────────────────────────────

UPDATE dispatcharr_channels_channel SET channel_number = 2
WHERE LOWER(tvg_id) = 'cbskcbs.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 4
WHERE LOWER(tvg_id) = 'nbcknbc.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 5
WHERE LOWER(tvg_id) = 'cwktla.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 7
WHERE LOWER(tvg_id) = 'abckabc.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 9
WHERE name ILIKE '%KCAL%'
  AND channel_group_id IN (
    SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals'
  );

UPDATE dispatcharr_channels_channel SET channel_number = 11
WHERE LOWER(tvg_id) = 'foxkttv.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

-- ── Denver (.1) ───────────────────────────────────────────────────────────

UPDATE dispatcharr_channels_channel SET channel_number = 2.1
WHERE LOWER(tvg_id) = 'cbskcnc.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 4.1
WHERE LOWER(tvg_id) = 'nbckusa.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 7.1
WHERE LOWER(tvg_id) = 'abckmgh.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 11.1
WHERE LOWER(tvg_id) = 'foxkdvr.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

-- ── Chicago (.2) ──────────────────────────────────────────────────────────

UPDATE dispatcharr_channels_channel SET channel_number = 2.2
WHERE LOWER(tvg_id) = 'cbswbbm.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 4.2
WHERE LOWER(tvg_id) = 'nbcwmaq.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 7.2
WHERE LOWER(tvg_id) = 'abcwls.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 11.2
WHERE LOWER(tvg_id) = 'foxwfld.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

-- ── New York (.3) ─────────────────────────────────────────────────────────

UPDATE dispatcharr_channels_channel SET channel_number = 2.3
WHERE LOWER(tvg_id) = 'cbswcbs.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 4.3
WHERE LOWER(tvg_id) = 'nbcwnbc.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 5.3
WHERE LOWER(tvg_id) = 'cwwpix.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 7.3
WHERE LOWER(tvg_id) = 'abcwabc.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 11.3
WHERE LOWER(tvg_id) = 'foxwnyw.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

-- ── Miami (.4) ────────────────────────────────────────────────────────────

UPDATE dispatcharr_channels_channel SET channel_number = 2.4
WHERE LOWER(tvg_id) = 'cbswfor.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 4.4
WHERE LOWER(tvg_id) = 'nbcwtvj.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 7.4
WHERE LOWER(tvg_id) = 'abcwplg.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

UPDATE dispatcharr_channels_channel SET channel_number = 11.4
WHERE LOWER(tvg_id) = 'foxwsvn.us'
  AND channel_group_id = (SELECT id FROM dispatcharr_channels_channelgroup WHERE name = 'Locals');

\echo 'All locals numbered.'

-- ── News 101+ ─────────────────────────────────────────────────────────────

WITH news_ranked AS (
  SELECT c.id, DENSE_RANK() OVER (ORDER BY c.tvg_id) AS tvg_rank
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'News' AND c.auto_created = true
)
UPDATE dispatcharr_channels_channel c
SET channel_number = 100 + n.tvg_rank
FROM news_ranked n WHERE c.id = n.id;

\echo 'News numbered (101+).'

-- ── Sports 201+ ───────────────────────────────────────────────────────────

WITH sports_ranked AS (
  SELECT c.id, DENSE_RANK() OVER (ORDER BY c.tvg_id) AS tvg_rank
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'Sports' AND c.auto_created = true
)
UPDATE dispatcharr_channels_channel c
SET channel_number = 200 + n.tvg_rank
FROM sports_ranked n WHERE c.id = n.id;

\echo 'Sports numbered (201+).'

-- ── Documentary 301+ ──────────────────────────────────────────────────────

WITH docu_ranked AS (
  SELECT c.id, DENSE_RANK() OVER (ORDER BY c.tvg_id) AS tvg_rank
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'Documentary' AND c.auto_created = true
)
UPDATE dispatcharr_channels_channel c
SET channel_number = 300 + n.tvg_rank
FROM docu_ranked n WHERE c.id = n.id;

\echo 'Documentary numbered (301+).'

-- ── Entertainment 401+ ────────────────────────────────────────────────────

WITH entmt_ranked AS (
  SELECT c.id, DENSE_RANK() OVER (ORDER BY c.tvg_id) AS tvg_rank
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'Entertainment' AND c.auto_created = true
)
UPDATE dispatcharr_channels_channel c
SET channel_number = 400 + n.tvg_rank
FROM entmt_ranked n WHERE c.id = n.id;

\echo 'Entertainment numbered (401+).'

-- ── Movies 501+ ───────────────────────────────────────────────────────────

WITH movies_ranked AS (
  SELECT c.id, DENSE_RANK() OVER (ORDER BY c.tvg_id) AS tvg_rank
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'Movies' AND c.auto_created = true
)
UPDATE dispatcharr_channels_channel c
SET channel_number = 500 + n.tvg_rank
FROM movies_ranked n WHERE c.id = n.id;

\echo 'Movies numbered (501+).'

-- ── 4K 601+ ───────────────────────────────────────────────────────────────

WITH uhd_ranked AS (
  SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.tvg_id, c.name) AS rn
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = '4K' AND c.auto_created = true
)
UPDATE dispatcharr_channels_channel c
SET channel_number = 600 + u.rn
FROM uhd_ranked u WHERE c.id = u.id;

\echo '4K numbered (601+).'

-- ── PPV 901+ ──────────────────────────────────────────────────────────────

WITH ppv_ranked AS (
  SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.name) AS rn
  FROM dispatcharr_channels_channel c
  JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
  WHERE g.name = 'PPV' AND c.auto_created = true
)
UPDATE dispatcharr_channels_channel c
SET channel_number = 900 + u.rn
FROM ppv_ranked u WHERE c.id = u.id;

\echo 'PPV numbered (901+).'

-- ── Verify locals ─────────────────────────────────────────────────────────

\echo ''
\echo 'Part 8 — locals with stream counts:'
SELECT c.channel_number, c.name, c.tvg_id,
  (SELECT COUNT(*) FROM dispatcharr_channels_channelstream WHERE channel_id = c.id) AS stream_count
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name = 'Locals'
ORDER BY c.channel_number;

-- ═══════════════════════════════════════════════════════════════════════════
-- PART 6B: Locals-specific whitelist enforcement
--
-- Runs AFTER Part 9 to catch any channels the provider sync may have added
-- after Part 6 already ran (sync race condition).  Also catches Locals
-- channels with NULL tvg_id, which Part 6's IS NOT NULL guard misses.
--
-- Keep only auto_created Locals channels whose tvg_id is in the approved
-- list; delete everything else (null tvg_id or unrecognised tvg_id).
-- ═══════════════════════════════════════════════════════════════════════════

\echo ''
\echo '── Part 6B: Locals-specific whitelist enforcement ──'

CREATE TEMP TABLE _approved_locals (tvg_id text);
INSERT INTO _approved_locals VALUES
  -- LA Locals
  ('CBSKCBS.us'),('NBCKNBC.us'),('CWKTLA.us'),('ABCKABC.us'),('FOXKTTV.us'),
  -- Denver
  ('CBSKCNC.us'),('NBCKUSA.us'),('ABCKMGH.us'),('FOXKDVR.us'),
  -- Chicago
  ('CBSWBBM.us'),('NBCWMAQ.us'),('ABCWLS.us'),('FOXWFLD.us'),
  -- New York
  ('CBSWCBS.us'),('NBCWNBC.us'),('CWWPIX.us'),('ABCWABC.us'),('FOXWNYW.us'),
  -- Miami
  ('CBSWFOR.us'),('NBCWTVJ.us'),('ABCWPLG.us'),('FOXWSVN.us'),
  -- Other locals (approved extras)
  ('ABCKVUE.us'),('ABCWAAY.us'),('ABCWABMDT2.us'),('ABCWEAR.us'),('ABCWFTV.us'),('ABCWJXX.us'),
  ('CBSKEYE.us'),('CBSWHNT.us'),('CBSWIAT.us'),('CBSWJAX.us'),('CBSWKMG.us'),('CBSWKRG.us'),('CBSWTSP.us'),
  ('CWWCWJ.us'),('CWWKCF.us'),('CWWTOG.us'),('CWWTTO.us'),
  ('FOXKTBC.us'),('FOXWALA.us'),('FOXWBRC.us'),('FOXWFOX.us'),('FOXWOFL.us'),('FOXWTVT.us'),('FOXWZDX.us'),
  ('NBCKXAN.us'),('NBCWAFF.us'),('NBCWESH.us'),('NBCWFLA.us'),('NBCWPMI.us'),('NBCWSFA.us'),('NBCWTLV.us'),('NBCWTVJ.us'),('NBCWVTM.us');

SELECT COUNT(*) AS part6b_locals_to_delete
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name = 'Locals'
  AND c.auto_created = true
  AND (c.tvg_id IS NULL OR c.tvg_id = '' OR LOWER(c.tvg_id) NOT IN (SELECT LOWER(tvg_id) FROM _approved_locals));

DO $$
DECLARE _bad_locals int[];
BEGIN
  SELECT ARRAY(
    SELECT c.id FROM dispatcharr_channels_channel c
    JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
    WHERE g.name = 'Locals' AND c.auto_created = true
      AND (c.tvg_id IS NULL OR c.tvg_id = '' OR LOWER(c.tvg_id) NOT IN (SELECT LOWER(tvg_id) FROM _approved_locals))
  ) INTO _bad_locals;

  DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id = ANY(_bad_locals);
  DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id = ANY(_bad_locals);
  DELETE FROM dispatcharr_channels_channelstream           WHERE channel_id = ANY(_bad_locals);
  DELETE FROM dispatcharr_channels_channel                 WHERE id         = ANY(_bad_locals);
END $$;

-- Deduplicate: for each tvg_id keep MIN(id), merge all streams onto it
CREATE TEMP TABLE _locals_survivors AS
SELECT LOWER(c.tvg_id) AS tvg_id_lower, MIN(c.id) AS survivor_id
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name = 'Locals' AND c.auto_created = true
  AND c.tvg_id IS NOT NULL AND c.tvg_id != ''
GROUP BY LOWER(c.tvg_id);

-- Move unique streams from non-survivors to survivor
UPDATE dispatcharr_channels_channelstream cs
SET channel_id = s.survivor_id
FROM dispatcharr_channels_channel c
JOIN _locals_survivors s ON LOWER(c.tvg_id) = s.tvg_id_lower AND c.id != s.survivor_id
WHERE cs.channel_id = c.id
  AND NOT EXISTS (
    SELECT 1 FROM dispatcharr_channels_channelstream
    WHERE channel_id = s.survivor_id AND stream_id = cs.stream_id
  );

-- Force-remove remaining channelstream entries from non-survivors, then delete them
DO $$
DECLARE _dead int[];
BEGIN
  SELECT ARRAY(
    SELECT c.id FROM dispatcharr_channels_channel c
    JOIN _locals_survivors s ON LOWER(c.tvg_id) = s.tvg_id_lower AND c.id != s.survivor_id
    JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
    WHERE g.name = 'Locals' AND c.auto_created = true
  ) INTO _dead;
  DELETE FROM dispatcharr_channels_channelstream           WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channelprofilemembership WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channeloverride         WHERE channel_id = ANY(_dead);
  DELETE FROM dispatcharr_channels_channel                 WHERE id         = ANY(_dead);
END $$;

SELECT COUNT(*) AS part6b_locals_remaining
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name = 'Locals' AND c.auto_created = true;

-- ═══════════════════════════════════════════════════════════════════════════
-- FINAL SUMMARY
-- ═══════════════════════════════════════════════════════════════════════════

\echo ''
\echo '── Channel counts by group ──'
SELECT g.name, COUNT(c.id) AS channel_count
FROM dispatcharr_channels_channelgroup g
LEFT JOIN dispatcharr_channels_channel c ON c.channel_group_id = g.id
WHERE g.name IN (
  'Entertainment','Movies','News','Sports','Documentary','Locals','4K','PPV',
  'Direct - Entmt','Direct - Movies','Direct - News','Direct - Sports',
  'Direct - Factual','Direct - Locals','Favorites'
)
GROUP BY g.id, g.name
ORDER BY g.name;

\echo ''
\echo '── Channel totals ──'
SELECT
  COUNT(*)                                     AS total,
  COUNT(*) FILTER (WHERE auto_created = true)  AS auto_created,
  COUNT(*) FILTER (WHERE auto_created = false) AS manual
FROM dispatcharr_channels_channel;

\echo ''
\echo '── Remaining Direct channels (should be 0) ──'
SELECT COUNT(*) AS direct_channels_remaining
FROM dispatcharr_channels_channel c
JOIN dispatcharr_channels_channelgroup g ON g.id = c.channel_group_id
WHERE g.name LIKE 'Direct -%' AND c.auto_created = true;

\echo ''
\echo '============================================================'
\echo ' Maintenance complete.'
\echo '============================================================'
