/* ==========================================================================
   Tab: Log — Real-time log viewer with filters
   ========================================================================== */

const TabLog = {
    _autoScroll: true,
    _filters: { info: true, success: true, error: true, warning: true },
    _container: null,

    init() {
        this._buildUI();
        // Render any logs already buffered
        for (const entry of App.state.logs) {
            this.appendEntry(entry);
        }
    },

    _buildUI() {
        const container = document.getElementById('tab-log');
        container.innerHTML = '';

        // Filter toolbar
        const toolbar = el('div', { className: 'toolbar' },
            ...['info', 'success', 'error', 'warning'].map(level => {
                const btn = el('button', {
                    className: `btn btn-sm ${this._filters[level] ? 'btn-primary' : ''}`,
                    onClick: () => this._toggleFilter(level, btn),
                }, level.charAt(0).toUpperCase() + level.slice(1));
                return btn;
            }),
            el('div', { className: 'toolbar-spacer' }),
            el('button', { className: 'btn btn-sm', onClick: () => this.clear() }, 'Clear'),
        );
        container.appendChild(toolbar);

        // Log output
        this._container = el('div', {
            className: 'log-container',
            id: 'log-output',
        });
        this._container.addEventListener('scroll', () => {
            const el = this._container;
            this._autoScroll = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
        });
        container.appendChild(this._container);
    },

    _toggleFilter(level, btn) {
        this._filters[level] = !this._filters[level];
        btn.className = `btn btn-sm ${this._filters[level] ? 'btn-primary' : ''}`;
        this._rerender();
    },

    _rerender() {
        if (!this._container) return;
        this._container.innerHTML = '';
        for (const entry of App.state.logs) {
            this._addLine(entry);
        }
    },

    appendEntry(entry) {
        if (!this._container) return;
        this._addLine(entry);
        if (this._container.childNodes.length > App.MAX_LOGS) {
            this._container.removeChild(this._container.firstChild);
        }
        if (this._autoScroll) {
            this._container.scrollTop = this._container.scrollHeight;
        }
    },

    _addLine(entry) {
        const level = entry.level === 'critical' ? 'error' : (entry.level || 'info');
        if (!this._filters[level]) return;

        const colorClass = {
            info: 'log-info',
            success: 'log-success',
            error: 'log-error',
            warning: 'log-warning',
        }[level] || 'log-info';

        const line = el('div', { className: `log-entry ${colorClass}` },
            el('span', { className: 'timestamp' },
                entry.timestamp || new Date().toLocaleTimeString()),
            document.createTextNode(` ${entry.message || ''}`),
        );
        this._container.appendChild(line);
    },

    clear() {
        App.state.logs = [];
        if (this._container) this._container.innerHTML = '';
    },
};
