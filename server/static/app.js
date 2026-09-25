/* ==========================================================================
   TTS Server — Main Application Controller
   ========================================================================== */

const App = {
    // Global state
    state: {
        workers: [],
        models: {},       // from /api/config -> models (MODEL_SETUP)
        modelsDir: null,
        defaults: {},     // from /api/config -> defaults
        fields: {},       // from /api/config -> non-slider model fields
        params: {},       // from /api/config -> params
        paramOverrides: {}, // from /api/config -> param_overrides (per-model min/max/step)
        tooltips: {},     // from /api/config -> tooltips
        profiles: {},     // from /api/config -> profiles (per-model audio profiles)
        overrideMap: {},  // from /api/config -> override_map
        setupStatus: {},  // from /api/setup/status
        setupActiveInstalls: {},  // from /api/setup/status -> active_installs
        devices: [],      // from /api/devices
        gatewayPort: null,
        bridgePort: null,
        jobs: [],         // from /api/jobs
        logs: [],         // ring buffer
        connected: false,
        configLoaded: false,
    },

    MAX_LOGS: 500,
    POLL_INTERVAL: 3000,
    // Flipped true after all TabX.init() complete (see init()). Render paths
    // invoked from polling gate on this so the first poll — which can resolve
    // before tabs are built — does not call into uninitialized tab DOM.
    tabsReady: false,

    // ---- API Client ----
    authHeaders(extra = {}) {
        const headers = { ...extra };
        if (window.__TTS_API_TOKEN__) {
            headers['X-TTS-API-Token'] = window.__TTS_API_TOKEN__;
        }
        return headers;
    },

    // Append the API token as a query param. ONLY use this for the SSE log
    // stream (EventSource cannot send custom headers). Putting the token in a
    // URL leaks it into browser history and access logs, so every other
    // authenticated request must use authHeaders() (X-TTS-API-Token) instead.
    // This is the one remaining query-token use, pending a stream-ticket
    // mechanism for the log stream.
    apiUrl(path) {
        if (!window.__TTS_API_TOKEN__ || !path.startsWith('/api/')) return path;
        const sep = path.includes('?') ? '&' : '?';
        return `${path}${sep}token=${encodeURIComponent(window.__TTS_API_TOKEN__)}`;
    },

    // Fetch an authenticated media/binary endpoint and return a blob: object
    // URL suitable for <audio src>, <a download>, etc. The token travels in the
    // X-TTS-API-Token header (not the URL), so it never lands in history/logs.
    // Callers own the returned URL and must URL.revokeObjectURL() it when done.
    async fetchBlobUrl(path) {
        const resp = await fetch(path, { headers: this.authHeaders() });
        if (!resp.ok) {
            const err = await resp.text().catch(() => resp.statusText);
            throw new Error(`${resp.status}: ${err}`);
        }
        const blob = await resp.blob();
        return URL.createObjectURL(blob);
    },

    async api(method, path, body) {
        const opts = { method, headers: this.authHeaders() };
        if (body) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(path, opts);
        if (!resp.ok) {
            const err = await resp.text();
            throw new Error(`${resp.status}: ${err}`);
        }
        const ct = resp.headers.get('content-type') || '';
        if (ct.includes('application/json')) return resp.json();
        return null;
    },

    // ---- Toast notifications ----
    toast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const el = document.createElement('div');
        el.className = `toast toast-${type}`;
        el.textContent = message;
        el.style.cursor = 'pointer';
        el.title = 'Click to dismiss';
        el.addEventListener('click', () => el.remove());
        container.appendChild(el);
        // Errors stay 12s so user can read them; info/success 4s
        const timeout = type === 'error' ? 12000 : 4000;
        setTimeout(() => el.remove(), timeout);
    },

    // ---- Shutdown button ----
    initShutdownButton() {
        const btn = document.getElementById('shutdown-btn');
        if (!btn) return;
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            btn.textContent = 'Shutting down...';
            // Stop background polling so the dying server doesn't spam errors.
            if (this._pollIntervalId) {
                clearInterval(this._pollIntervalId);
                this._pollIntervalId = null;
            }
            this.closeLogStream();
            // Let the backend unload workers and acknowledge the request before
            // terminating its WebView. Closing first can cancel the fetch and
            // leave the bridge supervisor running.
            try {
                await this.api('POST', '/api/shutdown');
            } catch (error) {
                console.error('Shutdown request failed:', error);
                this.toast(`Shutdown failed: ${error.message}`, 'error');
                btn.disabled = false;
                btn.textContent = 'Shutdown';
                this.startPolling();
                this.initLogStream();
                return;
            }

            // The native binding performs the cleanest window teardown. The
            // gateway also verifies and closes the launcher as a fallback.
            try {
                if (typeof window.closeApp === 'function') {
                    await Promise.resolve(window.closeApp());
                } else {
                    window.close();
                }
            } catch (error) {
                console.warn('Native window close unavailable:', error);
            }
        });
    },

    // ---- Tab switching ----
    initTabs() {
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
                btn.classList.add('active');
                document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
                document.body.dataset.activeTab = btn.dataset.tab;
                // Notify tabs that need to react to becoming visible (canvas
                // dimensions, focus, etc).
                if (btn.dataset.tab === 'jobs' && typeof TabJobs !== 'undefined'
                    && typeof TabJobs.onTabActivated === 'function') {
                    // Defer one frame so the layout settles after the active class is applied.
                    requestAnimationFrame(() => TabJobs.onTabActivated());
                }
            });
        });
        // Set initial body data-active-tab
        const initial = document.querySelector('.tab-btn.active');
        if (initial) document.body.dataset.activeTab = initial.dataset.tab;
    },

    // ---- Gateway badge ----
    updateGatewayBadge() {
        const badge = document.getElementById('gateway-badge');
        if (this.state.connected) {
            badge.textContent = `Connected (${this.state.workers.length} workers)`;
            badge.className = 'badge badge-green';
        } else {
            badge.textContent = 'Disconnected';
            badge.className = 'badge badge-red';
        }
    },

    // ---- SSE Log Stream ----
    initLogStream() {
        // NOTE: EventSource cannot send custom headers, so the log stream is the
        // one endpoint that still authenticates via a ?token= query param (see
        // apiUrl). Pending a short-lived stream-ticket mechanism, the server
        // accepts the token here as a query param. Do not use apiUrl() elsewhere.
        this._sse = new EventSource(this.apiUrl('/api/logs/stream'));
        this._sse.onmessage = (e) => {
            try {
                const entry = JSON.parse(e.data);
                this.state.logs.push(entry);
                if (this.state.logs.length > this.MAX_LOGS) {
                    this.state.logs.shift();
                }
                if (typeof TabLog !== 'undefined') TabLog.appendEntry(entry);
            } catch (err) {
                // A malformed/truncated SSE frame would otherwise vanish from
                // the log viewer with zero trace — surface it for diagnostics.
                console.debug('SSE log frame dropped (parse error):', err, e.data);
            }
        };
        this._sse.onerror = () => {
            // EventSource reconnects automatically, but notify the user
            const badge = document.getElementById('gateway-badge');
            if (badge && badge.className.includes('badge-green')) {
                badge.textContent = 'Reconnecting...';
                badge.className = 'badge badge-orange';
            }
        };
        this._sse.onopen = () => {
            // Restore badge on successful reconnect
            this.updateGatewayBadge();
        };
    },

    closeLogStream() {
        if (this._sse) {
            this._sse.close();
            this._sse = null;
        }
    },

    // ---- Polling loop ----
    _backoff: 0,  // consecutive failures — drives exponential backoff

    async poll() {
        if (this._polling) return;
        // Exponential backoff when disconnected: skip ticks (3s → 6s → 12s → 24s max)
        if (this._backoff > 0) {
            const skipTicks = Math.min(1 << this._backoff, 8); // 2, 4, 8
            if (this._pollTick % skipTicks !== 0) return;
        }
        this._polling = true;
        try {
            const data = await this.api('GET', '/api/workers');
            this.state.connected = true;
            this._backoff = 0;  // reset on success
            this.state.workers = data.workers || [];
            if (!this.state.configLoaded) await this.loadConfig();
            this.updateGatewayBadge();

            if (this.tabsReady) {
                if (typeof TabServer !== 'undefined') TabServer.render();
                if (typeof TabTesting !== 'undefined') TabTesting.updateWorkerList();
            }
        } catch {
            this.state.connected = false;
            this._backoff = Math.min(this._backoff + 1, 3); // cap at 3 (skip up to 8 ticks = 24s)
            this.updateGatewayBadge();
        } finally {
            this._polling = false;
        }
    },

    async pollJobs() {
        if (this._jobsPolling) return;
        this._jobsPolling = true;
        try {
            const jobs = [];
            while (true) {
                const page = await this.api('GET', '/api/jobs?limit=500&offset=' + jobs.length);
                jobs.push(...(page.jobs || []));
                if (!page.jobs?.length || jobs.length >= page.total) break;
            }
            this.state.jobs = [...new Map(jobs.map(j => [j.job_id, j])).values()];
            if (this.tabsReady && typeof TabJobs !== 'undefined') TabJobs.render();
        } catch {} finally { this._jobsPolling = false; }
    },

    async loadConfig() {
        try {
            const cfg = await this.api('GET', '/api/config');
            this.state.models = cfg.models || {};
            this.state.modelsDir = cfg.models_dir || null;
            this.state.defaults = cfg.defaults || {};
            this.state.fields = cfg.fields || {};
            this.state.params = cfg.params || {};
            this.state.paramOverrides = cfg.param_overrides || {};
            this.state.tooltips = cfg.tooltips || {};
            this.state.profiles = cfg.profiles || {};
            this.state.overrideMap = cfg.override_map || {};
            this.state.gatewayPort = cfg.gateway_port || null;
            this.state.bridgePort = cfg.bridge_port || null;
            this.state.configLoaded = true;
            if (this.tabsReady) {
                if (typeof TabSetup !== 'undefined') TabSetup.render();
                if (typeof TabServer !== 'undefined') TabServer.init();
                if (typeof TabTesting !== 'undefined') { TabTesting._selectedModel = null; TabTesting._onWorkerChange(); }
            }
        } catch (e) {
            console.error('Failed to load config:', e);
        }
    },

    async loadDevices() {
        try {
            const data = await this.api('GET', '/api/devices');
            this.state.devices = data.devices || [];
        } catch {
            this.state.devices = [{ id: 'cpu', name: 'CPU' }];
        }
    },

    async loadSetupStatus() {
        if (this._setupPolling) return;
        this._setupPolling = true;
        try {
            const data = await this.api('GET', '/api/setup/status');
            this.state.setupStatus = data.models || {};
            this.state.setupActiveInstalls = data.active_installs || {};
            if (this.tabsReady && typeof TabSetup !== 'undefined') TabSetup.render();
        } catch {} finally { this._setupPolling = false; }
    },

    // ---- Audio playback ----
    _audioEl: null,
    _audioUrl: null,

    playAudio(url) {
        this.stopAudio();
        if (typeof TabJobs !== 'undefined') TabJobs._pause();
        this._audioUrl = url;
        this._audioEl = new Audio(url);
        // play() returns a promise that rejects on autoplay-policy blocks,
        // decode errors, or aborted loads. Catch it so it doesn't surface as an
        // unhandled rejection, and give the user feedback when playback fails.
        const p = this._audioEl.play();
        if (p && typeof p.catch === 'function') {
            p.catch((err) => {
                console.warn('Audio play failed', err);
                if (typeof this.toast === 'function') this.toast('Playback failed', 'error');
            });
        }
    },

    stopAudio() {
        if (this._audioEl) {
            this._audioEl.pause();
            this._audioEl = null;
        }
        // Don't revoke blob URLs here — they may be replayed from history.
        // History cleanup handles revocation in _trimHistory().
        this._audioUrl = null;
    },

    // ---- Init ----
    async init() {
        this.initTabs();
        this.initShutdownButton();
        this.initLogStream();

        await this.loadConfig();
        await Promise.all([
            this.poll(),
            this.loadDevices(),
            this.loadSetupStatus(),
            this.pollJobs(),
        ]);

        // Initialize tabs
        if (typeof TabSetup !== 'undefined') TabSetup.init();
        if (typeof TabServer !== 'undefined') TabServer.init();
        if (typeof TabVoices !== 'undefined') TabVoices.init();
        if (typeof TabTesting !== 'undefined') TabTesting.init();
        if (typeof TabJobs !== 'undefined') TabJobs.init();
        if (typeof TabLog !== 'undefined') TabLog.init();
        // Tabs are now built; let render paths run on subsequent polls. The
        // first poll/setup-status above intentionally precedes this so device
        // and worker data are loaded before tab UI is built (the Server tab's
        // device dropdown is built once from App.state.devices). Until this
        // flag flips, the early render guards in poll()/pollJobs()/
        // loadSetupStatus() make those first renders explicit no-ops rather
        // than relying only on each tab's defensive checks.
        this.tabsReady = true;

        // Coordinated polling loop (single timer, staggered tasks)
        // Skip polling when the tab is hidden to save resources
        this.startPolling();
    },

    startPolling() {
        if (this._pollIntervalId) clearInterval(this._pollIntervalId);
        this._pollTick = 0;
        this._pollIntervalId = setInterval(() => {
            if (document.hidden) return;
            this._pollTick++;
            this.poll();                                     // every 3s
            if (this._pollTick % 2 === 0) this.pollJobs();  // every 6s
            if (this._pollTick % 4 === 0) this.loadSetupStatus(); // every 12s
        }, this.POLL_INTERVAL);
    },
};

// ---- Helpers ----
function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
        for (const [k, v] of Object.entries(attrs)) {
            if (k === 'className') e.className = v;
            else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
            else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
            else e.setAttribute(k, v);
        }
    }
    for (const c of children) {
        if (typeof c === 'string') e.appendChild(document.createTextNode(c));
        else if (c) e.appendChild(c);
    }
    return e;
}

function statusBadge(status) {
    const map = {
        ready: 'badge-green', running: 'badge-green', completed: 'badge-green',
        partial: 'badge-orange', loading: 'badge-orange', starting: 'badge-orange', busy: 'badge-orange', installing: 'badge-orange', packages_only: 'badge-orange',
        not_installed: 'badge-gray', dead: 'badge-red', failed: 'badge-red',
        error: 'badge-red',
    };
    return el('span', { className: `badge ${map[status] || 'badge-gray'}` }, status);
}

document.addEventListener('DOMContentLoaded', () => App.init());
