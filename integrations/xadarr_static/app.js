const BASE = (typeof window._XB !== "undefined") ? window._XB : "";
function xadarr() {
  return {

    // ── State ─────────────────────────────────────────────────────────────
    activeTab: 'home',
    theme: 'midnight',
    embedded: false,

    // Home
    homeLoaded: false,
    cwItems: [],
    watchlistItems: [],
    trendingItems: [],
    serverItems: [],
    heroItem: null,

    // Discover
    discoverLoaded: false,
    trendingMovies: [],
    trendingShows: [],
    popularMovies: [],
    popularShows: [],
    upcomingMovies: [],
    searchQuery: '',
    searchResults: [],
    searchLoading: false,
    searchTypeFilter: 'all',   // 'all' | 'movies' | 'tv'
    searchGenreId: null,
    searchDiscoverData: { trending: [], popular_this_year: [], top_rated: [], new_releases: [], hidden_gems: [] },
    searchDiscoverLoading: false,
    browseMode: false,
    browseTitle: '',
    browseMovies: [],
    browseShows: [],
    browseLoading: false,

    // Cameras
    cameras: [],
    camerasFrigateConfigured: false,
    cameraFullscreen: null,
    cameraNonce: Date.now(),
    _cameraTimer: null,
    _hlsInstance: null,

    // History
    history: [],

    // Player (SSE)
    playerState: {
      isPlaying: false, isPaused: false, title: '',
      episodeTitle: '', overview: '', positionMs: 0,
      durationMs: 0, streamUrl: '', isLive: false,
    },
    _eventSource: null,

    // Sidebar
    mobileSidebarOpen: false,
    sidebarSec: { player: true, history: false, theme: false },

    // Detail sheet
    detailItem: null,
    detailLoading: false,

    // Expanded row grid
    expandedRow: null,

    // Swipe navigation
    swipeStartX: 0,
    swipeStartY: 0,

    // Settings
    settings: {},
    webhookUrlRaw: '',
    servers: [],
    iptv: { m3uUrl: '', epgUrl: '' },
    addons: [],
    addonUrlInput: '',
    catalogues: [],
    showServerForm: false,
    editingServerId: null,
    editingServerName: '',

    // Known service/genre titles — used to classify COLLECTION items under COLLECTION_RAIL rows
    _SERVICE_TITLES: new Set([
      'netflix','hbo max','disney+','prime video','apple tv+','hulu',
      'paramount+','peacock','starz','shudder','mgm+','discovery+',
      'crunchyroll','adult swim','mubi',
    ]),
    _GENRE_TITLES: new Set([
      'action','adventure','animation','comedy','crime','documentary',
      'drama','family','fantasy','history','horror','mystery','romance',
      'sci-fi','thriller','war & military','western','superhero',
    ]),
    _PROVIDER_IDS: {
      'netflix': 8, 'hbo max': 1899, 'disney+': 337, 'prime video': 9,
      'apple tv+': 350, 'hulu': 15, 'paramount+': 2303, 'peacock': 386,
      'starz': 43, 'shudder': 99, 'mgm+': 268, 'discovery+': 584,
      'crunchyroll': 283, 'mubi': 11,
    },
    _GENRE_IDS: {
      'action': 28, 'adventure': 12, 'animation': 16, 'comedy': 35,
      'crime': 80, 'documentary': 99, 'drama': 18, 'family': 10751,
      'fantasy': 14, 'history': 36, 'horror': 27, 'mystery': 9648,
      'romance': 10749, 'sci-fi': 878, 'thriller': 53,
      'war & military': 10752, 'western': 37,
    },
    _TILE_IMAGES: {
      // Services
      'netflix':     'https://raw.githubusercontent.com/mrtxiv/networks-video-collection/3486fc9a3d0efe59d1929e75f66021dc4e15bcb7/networks%20collection/netflix.png',
      'prime video': 'https://raw.githubusercontent.com/mrtxiv/networks-video-collection/3486fc9a3d0efe59d1929e75f66021dc4e15bcb7/networks%20collection/amazonprime.png',
      'apple tv+':   'https://raw.githubusercontent.com/mrtxiv/networks-video-collection/3486fc9a3d0efe59d1929e75f66021dc4e15bcb7/networks%20collection/appletvplus.png',
      'disney+':     'https://raw.githubusercontent.com/mrtxiv/networks-video-collection/3486fc9a3d0efe59d1929e75f66021dc4e15bcb7/networks%20collection/disneyplus.png',
      'hbo max':     'https://raw.githubusercontent.com/mrtxiv/networks-video-collection/3486fc9a3d0efe59d1929e75f66021dc4e15bcb7/networks%20collection/hbomax.png',
      'hulu':        'https://raw.githubusercontent.com/mrtxiv/networks-video-collection/3486fc9a3d0efe59d1929e75f66021dc4e15bcb7/networks%20collection/hulu.png',
      'paramount+':  'https://raw.githubusercontent.com/mrtxiv/networks-video-collection/3486fc9a3d0efe59d1929e75f66021dc4e15bcb7/networks%20collection/paramount.png',
      // Genres
      'action':      'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/action-wide.png',
      'sci-fi':      'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/sci-fi-wide.png',
      'horror':      'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/horror-wide.png',
      'thriller':    'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/thriller-wide.png',
      'fantasy':     'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/fantasy-wide.png',
      'animation':   'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/animation-wide.png',
      'adventure':   'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/adventure-wide.png',
      'comedy':      'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/comedy-wide.png',
      'family':      'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/family-movie-night-wide.png',
      'crime':       'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/crime-wide.png',
      'superhero':   'https://raw.githubusercontent.com/itsrenoria/fusion-starter-kit/refs/heads/main/resources/widgets/genres/wide/dannyrutledge/superheroes-wide.png',
    },
    serverForm: { kind: 'JELLYFIN', url: '', username: '', password: '', token: '' },
    serverConnecting: false,
    serverError: '',

    // Trakt
    traktStatus: { connected: false, hasClientId: false },

    // Episeerr
    toasts: [],
    _toastId: 0,
    pendingIds: new Set(),
    rules: [],
    showRulePicker: false,
    rulePickerItem: null,
    selectedRuleId: null,

    // ── init ──────────────────────────────────────────────────────────────
    async init() {
      // Restore theme from localStorage first (instant, before API load)
      const savedTheme = localStorage.getItem('xadarr_theme') || 'midnight';
      this.theme = savedTheme;

      await this.loadSettings();
      await this.loadCatalogues();   // must know visibility before home renders
      this.connectSSE();
      this.loadHome();
      this.loadCameras();
      this.loadHistory();
      this.loadEpiseerrPending();
      this.loadServers();
      this.loadAddons();
      this.loadIptv();
      this.loadTraktStatus();
    },

    // ── Embedded init (called when hosted inside Episeerr's chrome) ───────
    async embeddedInit(section) {
      this.embedded = true;
      this.activeTab = section;
      const savedTheme = localStorage.getItem('xadarr_theme') || 'midnight';
      this.theme = savedTheme;
      const sectionLoad =
        section === 'discover' ? this.loadDiscover() :
        section === 'search'   ? this.loadSearchDiscover() :
        section === 'cameras'  ? this.loadCameras() :
        Promise.resolve();
      await Promise.all([this.loadSettings(), this.loadCatalogues(), sectionLoad]);
      this.connectSSE();
      this.loadHistory();
      this.loadEpiseerrPending();
      this.loadServers();
      this.loadAddons();
      this.loadIptv();
      this.loadTraktStatus();
    },

    // ── Tab switching ─────────────────────────────────────────────────────
    switchTab(tab) {
      this.activeTab = tab;

      if (tab === 'cameras') {
        this._cameraTimer = setInterval(() => { this.cameraNonce = Date.now(); }, 2000);
        if (this.cameras.length === 0) this.loadCameras();
      } else {
        clearInterval(this._cameraTimer);
        this._cameraTimer = null;
      }

      if (tab === 'search') {
        this.$nextTick(() => this.$refs.searchInput?.focus());
        if (this.searchDiscoverData.trending.length === 0) {
          this.loadSearchDiscover();
        }
      }
      if (tab === 'discover' && !this.discoverLoaded) {
        this.loadDiscover();
      }
      if (tab === 'tv' && !this.iptv.m3uUrl) {
        this.loadIptv();
      }
      if (tab === 'settings') {
        if (this.servers.length === 0) {
          this.loadServers();
          this.loadAddons();
          this.loadCatalogues();
        }
        this.loadIptv();
      }
    },

    // ── Home ──────────────────────────────────────────────────────────────
    async loadHome() {
      this.homeLoaded = false;

      const [cwResp, wlResp, trendResp, srvResp] = await Promise.allSettled([
        fetch(BASE+'/api/media/continue-watching').then(r => r.json()),
        fetch(BASE+'/api/media/watchlist').then(r => r.json()),
        fetch(BASE+'/api/media/trending').then(r => r.json()),
        fetch(BASE+'/api/media/server-items').then(r => r.json()).catch(() => []),
      ]);

      if (cwResp.status === 'fulfilled' && Array.isArray(cwResp.value)) {
        this.cwItems = cwResp.value;
      }
      if (wlResp.status === 'fulfilled' && Array.isArray(wlResp.value)) {
        this.watchlistItems = wlResp.value;
        this._markPending(this.watchlistItems);
      }
      if (trendResp.status === 'fulfilled' && Array.isArray(trendResp.value)) {
        this.trendingItems = trendResp.value;
        this._markWatchlistFlag(this.trendingItems);
      }
      if (srvResp.status === 'fulfilled' && Array.isArray(srvResp.value)) {
        this.serverItems = srvResp.value;
        this._markWatchlistFlag(this.serverItems);
      }

      // Hero from user's library: prefer watchlist → server items (both need backdropUrl)
      const heroPool = [
        ...this.watchlistItems.filter(i => i.backdropUrl || i.backdropPath),
        ...this.serverItems.filter(i => i.backdropUrl),
      ];
      if (heroPool.length > 0) {
        const pick = heroPool[Math.floor(Math.random() * Math.min(8, heroPool.length))];
        this.heroItem = { ...pick, backdropUrl: pick.backdropUrl || pick.backdropPath };
      }

      this.homeLoaded = true;
    },

    // ── Discover ──────────────────────────────────────────────────────────
    async loadDiscover() {
      if (this.discoverLoaded) return;
      const [trendRes, popRes, upRes] = await Promise.allSettled([
        this.trendingItems.length > 0
          ? Promise.resolve(this.trendingItems)
          : fetch(BASE+'/api/media/trending').then(r => r.json()),
        fetch(BASE+'/api/media/popular').then(r => r.json()),
        fetch(BASE+'/api/media/upcoming').then(r => r.json()),
      ]);
      const trending  = trendRes.status === 'fulfilled' ? trendRes.value : [];
      const popular   = popRes.status === 'fulfilled'   ? popRes.value   : {};
      const upcoming  = upRes.status === 'fulfilled'    ? upRes.value    : [];
      this.trendingMovies  = trending.filter(i => i.mediaType === 'movie');
      this.trendingShows   = trending.filter(i => i.mediaType === 'show');
      this.popularMovies   = Array.isArray(popular.movies) ? popular.movies : [];
      this.popularShows    = Array.isArray(popular.shows)  ? popular.shows  : [];
      this.upcomingMovies  = Array.isArray(upcoming) ? upcoming : [];
      [this.trendingMovies, this.trendingShows, this.popularMovies,
       this.popularShows, this.upcomingMovies].forEach(a => this._markWatchlistFlag(a));
      this.discoverLoaded = true;
    },

    discoverRowItems(cat) {
      const title = (cat.title || '').toLowerCase();
      const id    = (cat.id    || '').toLowerCase();
      const has   = (...kw) => kw.some(k => id.includes(k) || title.includes(k));
      if (has('trending')) {
        if (has('movie', 'film'))   return this.trendingMovies;
        if (has('tv', 'show'))      return this.trendingShows;
        return [...this.trendingMovies, ...this.trendingShows].slice(0, 20);
      }
      if (has('popular', 'top10', 'top_10', 'top 10')) {
        if (has('movie', 'film'))   return this.popularMovies;
        if (has('tv', 'show'))      return this.popularShows;
      }
      if (has('top_movies', 'top movies')) return this.popularMovies;
      if (has('top_shows', 'top shows'))   return this.popularShows;
      if (has('coming', 'upcoming'))       return this.upcomingMovies;
      return [];
    },

    // ── Search ────────────────────────────────────────────────────────────

    searchGenresForType() {
      const all = [
        {id: 28,    name: 'Action'},      {id: 12,    name: 'Adventure'},
        {id: 16,    name: 'Animation'},   {id: 35,    name: 'Comedy'},
        {id: 80,    name: 'Crime'},       {id: 99,    name: 'Documentary'},
        {id: 18,    name: 'Drama'},       {id: 10751, name: 'Family'},
        {id: 14,    name: 'Fantasy'},     {id: 27,    name: 'Horror'},
        {id: 9648,  name: 'Mystery'},     {id: 10749, name: 'Romance'},
        {id: 878,   name: 'Sci-Fi'},      {id: 53,    name: 'Thriller'},
        {id: 10752, name: 'War'},         {id: 37,    name: 'Western'},
      ];
      const tv = [
        {id: 10759, name: 'Action & Adventure'}, {id: 16,    name: 'Animation'},
        {id: 35,    name: 'Comedy'},             {id: 80,    name: 'Crime'},
        {id: 99,    name: 'Documentary'},        {id: 18,    name: 'Drama'},
        {id: 10751, name: 'Family'},             {id: 10762, name: 'Kids'},
        {id: 9648,  name: 'Mystery'},            {id: 10765, name: 'Sci-Fi & Fantasy'},
        {id: 10768, name: 'War & Politics'},     {id: 37,    name: 'Western'},
      ];
      return this.searchTypeFilter === 'tv' ? tv : all;
    },

    setSearchType(type) {
      this.searchTypeFilter = type;
      this.searchGenreId = null;
      if (this.searchQuery.length < 2) this.loadSearchDiscover();
    },

    setSearchGenre(id) {
      this.searchGenreId = (this.searchGenreId === id) ? null : id;
      this.loadSearchDiscover();
    },

    async loadSearchDiscover() {
      this.searchDiscoverLoading = true;
      const params = new URLSearchParams({ type: this.searchTypeFilter });
      if (this.searchGenreId) params.set('genre_id', String(this.searchGenreId));
      const data = await fetch(BASE+`/api/media/search-discover?${params}`)
        .then(r => r.json()).catch(() => ({}));
      this.searchDiscoverData = {
        trending:          Array.isArray(data.trending)          ? data.trending          : [],
        popular_this_year: Array.isArray(data.popular_this_year) ? data.popular_this_year : [],
        top_rated:         Array.isArray(data.top_rated)         ? data.top_rated         : [],
        new_releases:      Array.isArray(data.new_releases)      ? data.new_releases      : [],
        hidden_gems:       Array.isArray(data.hidden_gems)       ? data.hidden_gems       : [],
      };
      Object.values(this.searchDiscoverData).forEach(arr => this._markWatchlistFlag(arr));
      this.searchDiscoverLoading = false;
    },

    searchDiscoverRows() {
      const d = this.searchDiscoverData;
      return [
        { id: 'sd-trend', title: 'Trending',          items: d.trending },
        { id: 'sd-popy',  title: 'Popular This Year', items: d.popular_this_year },
        { id: 'sd-top',   title: 'Top Rated',         items: d.top_rated },
        { id: 'sd-new',   title: 'New Releases',      items: d.new_releases },
        { id: 'sd-hgems', title: 'Hidden Gems',       items: d.hidden_gems },
      ].filter(r => r.items.length > 0);
    },

    async doSearch() {
      const q = this.searchQuery.trim();
      if (q.length < 2) { this.searchResults = []; return; }
      this.searchLoading = true;
      const data = await fetch(BASE+`/api/media/search?q=${encodeURIComponent(q)}`).then(r => r.json()).catch(() => []);
      if (Array.isArray(data)) {
        let results = data;
        if (this.searchTypeFilter === 'movies') results = data.filter(i => i.mediaType === 'movie');
        else if (this.searchTypeFilter === 'tv')  results = data.filter(i => i.mediaType === 'show');
        this.searchResults = results;
        this._markWatchlistFlag(this.searchResults);
      }
      this.searchLoading = false;
    },

    async browseByTile(cat, opt) {
      const t = (cat.title || '').toLowerCase();
      let qs = '';
      if (t.includes('service')) {
        const pid = this._PROVIDER_IDS[opt.name.toLowerCase()];
        if (!pid) return;
        qs = `provider_id=${pid}`;
      } else if (t.includes('genre')) {
        const gid = this._GENRE_IDS[opt.name.toLowerCase()];
        if (!gid) return;
        qs = `genre_id=${gid}`;
      } else { return; }
      this.browseMode = true;
      this.browseTitle = opt.name;
      this.browseLoading = true;
      this.browseMovies = [];
      this.browseShows = [];
      const data = await fetch(BASE+`/api/media/discover?${qs}`).then(r => r.json()).catch(() => ({}));
      const f = this.searchTypeFilter;
      this.browseMovies = f === 'tv'     ? [] : (Array.isArray(data.movies) ? data.movies : []);
      this.browseShows  = f === 'movies' ? [] : (Array.isArray(data.shows)  ? data.shows  : []);
      this._markWatchlistFlag(this.browseMovies);
      this._markWatchlistFlag(this.browseShows);
      this.browseLoading = false;
    },

    clearBrowse() {
      this.browseMode = false;
      this.browseTitle = '';
      this.browseMovies = [];
      this.browseShows = [];
    },

    // ── Cameras ───────────────────────────────────────────────────────────
    async loadCameras() {
      const data = await fetch(BASE+'/api/cameras/list').then(r => r.json()).catch(() => []);
      if (Array.isArray(data)) {
        this.cameras = data;
        this.camerasFrigateConfigured = this.settings.frigate_url
          ? true
          : data.length > 0;
      } else {
        this.camerasFrigateConfigured = false;
      }
    },

    openCamera(cam) {
      this.cameraFullscreen = cam;
      this.cameraNonce = Date.now();
      if (this._cameraSnapTimer) clearInterval(this._cameraSnapTimer);
      // Refresh poster snapshot every 3s while HLS loads / as fallback
      this._cameraSnapTimer = setInterval(() => { this.cameraNonce = Date.now(); }, 3000);
      this.$nextTick(() => {
        const video = document.getElementById('cameraVideo');
        if (!video) return;
        const frigateBase = (this.settings.frigate_url || '').replace(/\/+$/, '');
        let go2rtcBase = frigateBase;
        try { const u = new URL(frigateBase); u.port = '1984'; go2rtcBase = u.origin; } catch (_) {}
        const hlsUrl = `${go2rtcBase}/api/${cam.name}/index.m3u8`;
        if (window.Hls && Hls.isSupported()) {
          if (this._hlsInstance) this._hlsInstance.destroy();
          const hls = new Hls({ lowLatencyMode: true });
          hls.loadSource(hlsUrl);
          hls.attachMedia(video);
          this._hlsInstance = hls;
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
          video.src = hlsUrl;
        }
        video.play().catch(() => {});
      });
    },

    closeCamera() {
      if (this._hlsInstance) { this._hlsInstance.destroy(); this._hlsInstance = null; }
      if (this._cameraSnapTimer) { clearInterval(this._cameraSnapTimer); this._cameraSnapTimer = null; }
      const video = document.getElementById('cameraVideo');
      if (video) { video.pause(); video.removeAttribute('src'); video.load(); }
      this.cameraFullscreen = null;
    },

    // ── History ───────────────────────────────────────────────────────────
    async loadHistory() {
      const data = await fetch(BASE+'/api/media/history?limit=100').then(r => r.json()).catch(() => []);
      this.history = Array.isArray(data) ? data : [];
    },

    async clearHistory() {
      if (!confirm('Clear all activity history?')) return;
      await fetch(BASE+'/api/media/history', { method: 'DELETE' });
      this.history = [];
    },

    historyChipClass(event) {
      if (!event) return 'chip-play';
      if (event.includes('grabbed')) return 'chip-grabbed';
      if (event.includes('ready'))   return 'chip-ready';
      if (event.includes('rule'))    return 'chip-rule';
      if (event.includes('watchlist')) return 'chip-watchlist';
      return 'chip-play';
    },

    historyChipLabel(event) {
      const map = {
        'episode.grabbed':   'grabbed',
        'episode.ready':     'ready',
        'rule.triggered':    'rule',
        'rule.assigned':     'rule',
        'watchlist.requested': 'watchlist',
        'start':  'play',
        'stop':   'stop',
        'pause':  'pause',
        'resume': 'resume',
        'finish': 'done',
      };
      return map[event] || event || '?';
    },

    historySubtitle(item) {
      const parts = [];
      if (item.episodeTitle) parts.push(item.episodeTitle);
      if (item.season && item.episode) parts.push(`S${item.season}E${item.episode}`);
      if (item.rule) parts.push(`Rule: ${item.rule}`);
      if (item.mediaType) parts.push(item.mediaType);
      return parts.join(' · ');
    },

    // ── Player / SSE ──────────────────────────────────────────────────────
    connectSSE() {
      if (this._eventSource) this._eventSource.close();
      const es = new EventSource(BASE+'/api/player/events');
      this._eventSource = es;

      es.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          this.playerState = { ...this.playerState, ...data };
        } catch (_) {}
      };

      es.addEventListener('episeerr', (e) => {
        try {
          const entry = JSON.parse(e.data);
          this._pushEpiseerrToast(entry);
          // Refresh pending state
          if (entry.event === 'rule.assigned' || entry.event === 'watchlist.requested') {
            this.loadEpiseerrPending();
          }
        } catch (_) {}
      });

      es.addEventListener('watchlist', (e) => {
        // Another client changed the watchlist — reload
        this._reloadWatchlist();
      });

      es.onerror = () => {
        // Auto-reconnect after 5s
        setTimeout(() => this.connectSSE(), 5000);
        es.close();
      };
    },

    // ── Settings ──────────────────────────────────────────────────────────
    async loadSettings() {
      const data = await fetch(BASE+'/api/settings').then(r => r.json()).catch(() => ({}));
      this.settings = data;

      // Sync theme from blob (server saved it)
      if (data.web_theme) {
        this.theme = data.web_theme;
        localStorage.setItem('xadarr_theme', data.web_theme);
      }

      // Build webhookUrlRaw from webhook_urls array
      const urls = data.webhook_urls || [];
      if (urls.length > 0) {
        this.webhookUrlRaw = typeof urls[0] === 'string' ? urls[0] : urls[0].url || '';
      } else if (data.webhook_url) {
        this.webhookUrlRaw = data.webhook_url;
      }

      // Check frigate
      if (data.frigate_url) {
        this.camerasFrigateConfigured = true;
      }
    },

    async saveSetting(key, value) {
      const payload = {};
      payload[key] = value;
      await fetch(BASE+'/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    },

    async saveWebhookUrl() {
      const url = this.webhookUrlRaw.trim();
      const payload = { webhook_urls: url ? [url] : [] };
      await fetch(BASE+'/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    },

    async saveFrigateUrl() {
      const url = (this.settings.frigate_url || '').trim();
      await this.saveSetting('frigate_url', url);
      this.camerasFrigateConfigured = !!url;
      if (url) {
        this.loadCameras();
      }
    },

    async testWebhook() {
      const url = this.webhookUrlRaw.trim();
      if (!url) { alert('Enter a webhook URL first'); return; }
      const resp = await fetch(BASE+'/api/webhook/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, events: ['start'] }),
      }).then(r => r.json()).catch(() => ({ ok: false, error: 'Network error' }));
      alert(resp.ok ? '✓ Webhook test sent successfully' : '✗ ' + (resp.error || 'Failed'));
    },

    setTheme(t) {
      this.theme = t;
      localStorage.setItem('xadarr_theme', t);
      this.saveSetting('web_theme', t);
    },

    // ── Servers ───────────────────────────────────────────────────────────
    serverFriendlyName() {
      const s = this.servers[0];
      if (!s) return 'Server';
      if (s.displayName) return s.displayName;
      const sn = s.serverName || '';
      if (sn && !/^[0-9a-f]{6,}$/i.test(sn)) return sn;
      const k = (s.serverKind || '').toLowerCase();
      return k === 'plex' ? 'Plex' : k === 'emby' ? 'Emby' : k === 'jellyfin' ? 'Jellyfin' : 'Server';
    },

    async loadServers() {
      const data = await fetch(BASE+'/api/setup/servers').then(r => r.json()).catch(() => []);
      this.servers = Array.isArray(data) ? data : [];
    },

    async connectServer() {
      this.serverConnecting = true;
      this.serverError = '';
      const body = {
        kind: this.serverForm.kind,
        url: this.serverForm.url,
        username: this.serverForm.username,
        password: this.serverForm.password,
        token: this.serverForm.token,
      };
      const resp = await fetch(BASE+'/api/setup/servers/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(r => r.json()).catch(e => ({ error: e.message }));
      this.serverConnecting = false;
      if (resp.ok) {
        this.showServerForm = false;
        this.serverForm = { kind: 'JELLYFIN', url: '', username: '', password: '', token: '' };
        await this.loadServers();
        this.loadHome(); // refresh server items
      } else {
        this.serverError = resp.error || 'Connection failed';
      }
    },

    async removeServer(connectionId) {
      if (!confirm('Remove this server?')) return;
      await fetch(BASE+`/api/setup/servers/${encodeURIComponent(connectionId)}`, { method: 'DELETE' });
      await this.loadServers();
    },

    reconnectServer(srv) {
      this.serverForm.kind = srv.serverKind || 'JELLYFIN';
      this.serverForm.url = srv.serverUrl || '';
      this.serverForm.username = srv.userName || '';
      this.serverForm.password = '';
      this.serverForm.token = '';
      this.showServerForm = true;
    },

    startRenameServer(srv) {
      this.editingServerId = srv.connectionId;
      this.editingServerName = srv.displayName || srv.serverName || '';
      this.$nextTick(() => {
        const el = document.getElementById('server-rename-' + srv.connectionId);
        if (el) { el.focus(); el.select(); }
      });
    },

    async commitRenameServer() {
      if (!this.editingServerId) return;
      await fetch(BASE+`/api/setup/servers/${encodeURIComponent(this.editingServerId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ displayName: this.editingServerName }),
      });
      this.editingServerId = null;
      await this.loadServers();
    },

    // ── IPTV ──────────────────────────────────────────────────────────────
    async loadIptv() {
      const data = await fetch(BASE+'/api/setup/iptv').then(r => r.json()).catch(() => ({}));
      this.iptv = { m3uUrl: data.m3uUrl || '', epgUrl: data.epgUrl || '' };
    },

    async saveIptv() {
      await fetch(BASE+'/api/setup/iptv', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.iptv),
      });
      this._showToast('IPTV settings saved', '', 'success');
    },

    // ── Addons ────────────────────────────────────────────────────────────
    async loadAddons() {
      const data = await fetch(BASE+'/api/setup/addons').then(r => r.json()).catch(() => []);
      this.addons = Array.isArray(data) ? data : [];
    },

    async addAddon() {
      const url = this.addonUrlInput.trim();
      if (!url) return;
      const resp = await fetch(BASE+'/api/setup/addons', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      }).then(r => r.json()).catch(() => ({ error: 'Network error' }));
      if (resp.ok) {
        this.addonUrlInput = '';
        await this.loadAddons();
      } else {
        alert(resp.error || 'Failed to install addon');
      }
    },

    async removeAddon(addonId) {
      await fetch(BASE+`/api/setup/addons/${encodeURIComponent(addonId)}`, { method: 'DELETE' });
      await this.loadAddons();
    },

    // ── Catalogues ────────────────────────────────────────────────────────
    async loadCatalogues() {
      const data = await fetch(BASE+'/api/catalogues').then(r => r.json()).catch(() => []);
      if (Array.isArray(data)) {
        this.catalogues = data.map((c, i) => ({
          ...c,
          placement: ['HOME','DISCOVER','SEARCH','HIDDEN'].includes(c.placement) ? c.placement : (c.isHidden ? 'HIDDEN' : 'HOME'),
          sortOrder: c.sortOrder ?? i,
        }));
      }
    },

    async saveCatalogues() {
      const toSave = this.catalogues.map((c, i) => ({
        ...c,
        isHidden: c.placement === 'HIDDEN',
        sortOrder: i,
      }));
      await fetch(BASE+'/api/catalogues', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(toSave),
      });
    },

    // All COLLECTION items are sub-catalogues managed via COLLECTION_RAIL chip pickers;
    // they never appear as standalone rows in the management list.
    isSubItem(cat) {
      return cat.kind === 'COLLECTION';
    },

    _visibleCats() {
      return this.catalogues.filter(c => !this.isSubItem(c));
    },

    isFirstVisible(catId) {
      const v = this._visibleCats();
      return v.length === 0 || v[0].id === catId;
    },

    isLastVisible(catId) {
      const v = this._visibleCats();
      return v.length === 0 || v[v.length - 1].id === catId;
    },

    moveCatalogueUp(catId) {
      const vis = this._visibleCats();
      const vi = vis.findIndex(c => c.id === catId);
      if (vi <= 0) return;
      const ai = this.catalogues.findIndex(c => c.id === catId);
      const bi = this.catalogues.findIndex(c => c.id === vis[vi - 1].id);
      const cats = [...this.catalogues];
      [cats[ai], cats[bi]] = [cats[bi], cats[ai]];
      this.catalogues = cats;
      this.saveCatalogues();
    },

    moveCatalogueDown(catId) {
      const vis = this._visibleCats();
      const vi = vis.findIndex(c => c.id === catId);
      if (vi < 0 || vi >= vis.length - 1) return;
      const ai = this.catalogues.findIndex(c => c.id === catId);
      const bi = this.catalogues.findIndex(c => c.id === vis[vi + 1].id);
      const cats = [...this.catalogues];
      [cats[ai], cats[bi]] = [cats[bi], cats[ai]];
      this.catalogues = cats;
      this.saveCatalogues();
    },

    isCatPicker(cat) {
      return cat.kind === 'COLLECTION_RAIL';
    },

    // Return COLLECTION catalogue items that belong to this COLLECTION_RAIL row.
    // Uses title-matching against known service/genre sets to classify them.
    availableItemsForCat(cat) {
      if (cat.kind !== 'COLLECTION_RAIL') return [];
      const t = (cat.title || '').toLowerCase();
      let matchSet;
      if (t.includes('service')) matchSet = this._SERVICE_TITLES;
      else if (t.includes('genre')) matchSet = this._GENRE_TITLES;
      else return [];

      return this.catalogues
        .filter(c => c.kind === 'COLLECTION' && matchSet.has((c.title || '').toLowerCase()))
        .map(c => ({
          id: c.id,
          name: c.title,
          coverImage: c.collectionCoverImageUrl || this._TILE_IMAGES[(c.title || '').toLowerCase()] || null,
        }));
    },

    // A sub-item is "selected" (visible) when its catalogue is not hidden.
    catItemSelected(cat, itemId) {
      const item = this.catalogues.find(c => c.id === itemId);
      return item ? (item.placement !== 'HIDDEN' && !item.isHidden) : false;
    },

    // Toggle a COLLECTION sub-item's visibility (HOME ↔ HIDDEN).
    toggleCatalogueItem(cat, itemId) {
      const item = this.catalogues.find(c => c.id === itemId);
      if (!item) return;
      const visible = item.placement !== 'HIDDEN' && !item.isHidden;
      item.placement = visible ? 'HIDDEN' : 'HOME';
      item.isHidden  = visible;
      this.saveCatalogues();
    },

    // Items to show in search suggestions: only the visible (selected) sub-items.
    // Falls back to all available if none are visible (shouldn't happen in practice).
    catDisplayItems(cat) {
      const all = this.availableItemsForCat(cat);
      if (!all.length) return [];
      const visible = all.filter(i => this.catItemSelected(cat, i.id));
      return visible.length > 0 ? visible : all;
    },

    searchSuggestionCats() {
      return this.catalogues.filter(c =>
        c.placement === 'SEARCH' && this.availableItemsForCat(c).length > 0
      );
    },

    catalogueIsVisible(id) {
      if (this.catalogues.length === 0) return false;  // not loaded yet
      const cat = this.catalogues.find(c => c.id === id);
      if (!cat) return true;
      return cat.placement !== 'HIDDEN' && !cat.isHidden;
    },

    catOrder(id) {
      const cat = this.catalogues.find(c => c.id === id);
      return cat ? (cat.sortOrder ?? 999) : 999;
    },

    serverCatOrder(mediaType) {
      const kw = mediaType === 'show' ? ['show', 'series'] : ['movie'];
      const cat = this.catalogues.find(c =>
        c.id.startsWith('home_server_') &&
        kw.some(k => (c.title || '').toLowerCase().includes(k))
      );
      return cat ? (cat.sortOrder ?? 999) : 999;
    },

    // ── Trakt ─────────────────────────────────────────────────────────────
    async loadTraktStatus() {
      const data = await fetch(BASE+'/api/trakt/status').then(r => r.json()).catch(() => ({}));
      this.traktStatus = data;
    },
    async disconnectTrakt() {
      await fetch(BASE+'/api/trakt/disconnect', { method: 'POST' });
      this.traktStatus = { connected: false, hasClientId: this.traktStatus.hasClientId };
    },

    // ── Watchlist ─────────────────────────────────────────────────────────
    async toggleWatchlist(item) {
      if (!item) return;
      const inWl = item.inWatchlist;
      // Optimistic update across all item lists
      this._setWatchlistFlag(item.id, item.mediaType, !inWl);

      if (inWl) {
        await fetch(BASE+`/api/media/watchlist/${item.mediaType}/${item.id}`, { method: 'DELETE' });
      } else {
        await fetch(BASE+'/api/media/watchlist', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: item.id,
            tmdbId: item.id,
            title: item.title,
            mediaType: item.mediaType,
            posterPath: item.image || item.posterPath || '',
            backdropPath: item.backdropUrl || item.backdropPath || '',
          }),
        });
      }
      // Reload watchlist row
      await this._reloadWatchlist();
    },

    async _reloadWatchlist() {
      const data = await fetch(BASE+'/api/media/watchlist').then(r => r.json()).catch(() => []);
      if (Array.isArray(data)) {
        this.watchlistItems = data;
        this._markPending(this.watchlistItems);
        this._markWatchlistFlag(this.trendingItems);
        this._markWatchlistFlag(this.trendingMovies);
        this._markWatchlistFlag(this.trendingShows);
        this._markWatchlistFlag(this.searchResults);
        this._markWatchlistFlag(this.serverItems);
        // Update hero if it's in the watchlist
        if (this.heroItem) {
          const found = [...this.watchlistItems, ...this.trendingItems].find(
            i => String(i.id) === String(this.heroItem.id)
          );
          if (found) this.heroItem = { ...this.heroItem, inWatchlist: found.inWatchlist };
        }
      }
    },

    _setWatchlistFlag(id, mediaType, value) {
      const lists = [
        this.watchlistItems, this.trendingItems,
        this.trendingMovies, this.trendingShows,
        this.searchResults, this.serverItems
      ];
      for (const list of lists) {
        const item = list.find(i => String(i.id) === String(id));
        if (item) item.inWatchlist = value;
      }
      if (this.heroItem && String(this.heroItem.id) === String(id)) {
        this.heroItem = { ...this.heroItem, inWatchlist: value };
      }
    },

    _markWatchlistFlag(items) {
      const wlIds = new Set(this.watchlistItems.map(w => String(w.id)));
      for (const item of items) {
        item.inWatchlist = wlIds.has(String(item.id));
      }
    },

    // ── Episeerr ──────────────────────────────────────────────────────────
    async loadEpiseerrPending() {
      const data = await fetch(BASE+'/api/episeerr/pending').then(r => r.json()).catch(() => []);
      if (Array.isArray(data)) {
        this.pendingIds = new Set(data.map(i => String(i.tmdbId || i.id)));
        this._markPending(this.watchlistItems);
      }
    },

    _markPending(items) {
      for (const item of items) {
        item.isPending = this.pendingIds.has(String(item.id || item.tmdbId));
      }
    },

    async openRulePicker(item) {
      this.rulePickerItem = item;
      this.selectedRuleId = null;
      if (this.rules.length === 0) {
        const data = await fetch(BASE+'/api/episeerr/rules').then(r => r.json()).catch(() => []);
        this.rules = Array.isArray(data) ? data : [];
      }
      this.showRulePicker = true;
    },

    async assignRule() {
      if (!this.selectedRuleId || !this.rulePickerItem) return;
      await fetch(BASE+'/api/episeerr/assign', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tmdb_id: this.rulePickerItem.id,
          rule_id: this.selectedRuleId,
          media_type: this.rulePickerItem.mediaType,
        }),
      });
      this.showRulePicker = false;
      this._showToast('Rule assigned', this.rulePickerItem.title, 'rule');
    },

    _pushEpiseerrToast(entry) {
      const typeMap = {
        'episode.grabbed':    { cls: 'grab',  title: '📥 Grabbed' },
        'episode.ready':      { cls: 'ready', title: '✅ Ready' },
        'rule.triggered':     { cls: 'rule',  title: '⚡ Rule Triggered' },
        'rule.assigned':      { cls: 'rule',  title: '✓ Rule Assigned' },
        'watchlist.requested':{ cls: 'watch', title: '➕ Watchlist' },
      };
      const mapped = typeMap[entry.event] || { cls: '', title: entry.event };
      this._showToast(mapped.title, entry.title, mapped.cls);
    },

    _showToast(title, body, cls) {
      const id = ++this._toastId;
      this.toasts.push({ id, title, body: body || '', cls: cls || '' });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, 4000);
    },

    // ── Hero ──────────────────────────────────────────────────────────────
    hoverCard(item) {
      if (item?.backdropUrl) {
        this.heroItem = item;
      }
    },

    // ── Detail sheet ──────────────────────────────────────────────────────
    openDetail(item) {
      this.detailItem = { ...item };
      this.detailLoading = false;
      // If no overview yet and item has a TMDB id, fetch full details
      const id = item.id || item.tmdbId;
      if (id && !item.overview) {
        this.detailLoading = true;
        const mt = item.mediaType === 'show' ? 'tv' : 'movie';
        fetch(BASE+`/api/media/detail?id=${id}&type=${mt}`)
          .then(r => r.ok ? r.json() : null)
          .then(data => {
            if (data && this.detailItem) Object.assign(this.detailItem, data);
          })
          .finally(() => { this.detailLoading = false; });
      } else if (id && item.overview && !item.genres) {
        // Has overview but no genres — fetch silently in background
        const mt = item.mediaType === 'show' ? 'tv' : 'movie';
        fetch(BASE+`/api/media/detail?id=${id}&type=${mt}`)
          .then(r => r.ok ? r.json() : null)
          .then(data => {
            if (data && this.detailItem) Object.assign(this.detailItem, data);
          });
      }
    },

    closeDetail() {
      this.detailItem = null;
      this.detailLoading = false;
    },

    // ── Expanded row grid ─────────────────────────────────────────────────
    openExpandedRow(title, items) {
      this.expandedRow = { title, items: [...items] };
    },

    closeExpandedRow() {
      this.expandedRow = null;
    },

    // ── CW subtitle ───────────────────────────────────────────────────────
    cwSubtitle(item) {
      if (item.episode) {
        return `S${item.season || 1}E${item.episode}` + (item.episodeTitle ? ` · ${item.episodeTitle}` : '');
      }
      return item.mediaType || '';
    },

    // ── Tab title & swipe navigation ─────────────────────────────────────
    tabTitle() {
      return { home:'Home', search:'Search', discover:'Discover', tv:'TV', cameras:'Cameras', settings:'Settings', history:'History', player:'Now Playing' }[this.activeTab] || '';
    },

    swipeStart(e) {
      this.swipeStartX = e.touches[0].clientX;
      this.swipeStartY = e.touches[0].clientY;
    },

    swipeEnd(e) {
      const dx = e.changedTouches[0].clientX - this.swipeStartX;
      const dy = e.changedTouches[0].clientY - this.swipeStartY;
      if (Math.abs(dx) < 80 || Math.abs(dx) < Math.abs(dy) * 2) return;
      const tabs = ['search', 'home', 'discover', ...(this.camerasFrigateConfigured ? ['cameras'] : []), 'settings'];
      const i = tabs.indexOf(this.activeTab);
      if (i === -1) return;
      if (dx < 0 && i < tabs.length - 1) this.switchTab(tabs[i + 1]);
      else if (dx > 0 && i > 0) this.switchTab(tabs[i - 1]);
    },

    // ── Time helpers ──────────────────────────────────────────────────────
    msToTime(ms) {
      if (!ms) return '0:00';
      const s = Math.floor(ms / 1000);
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      const mm = String(m % 60).padStart(2, '0');
      const ss = String(s % 60).padStart(2, '0');
      return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
    },

    relativeTime(ts) {
      if (!ts) return '';
      const diff = Date.now() - new Date(ts).getTime();
      const mins  = Math.floor(diff / 60000);
      const hours = Math.floor(mins / 60);
      const days  = Math.floor(hours / 24);
      if (days > 0)  return `${days}d ago`;
      if (hours > 0) return `${hours}h ago`;
      if (mins > 0)  return `${mins}m ago`;
      return 'just now';
    },
  };
}
