/* ==========================================================================
   Tab: Jobs (Audio Editor) — single-track editor + library + effects rack
   ==========================================================================

   Layout:
     [ Library | Editor (waveform + transport) | Effects rack ]

   Sources are pulled from the existing job system. Each render produces a
   new file under <job_dir>/edits/ that the user can continue editing.
   ========================================================================== */

const FX_DEFS = {
    gain: {
        label: 'Gain', category: 'Volume',
        params: { db: { label: 'Gain', min: -24, max: 24, step: 0.5, default: 0, unit: 'dB' } },
    },
    tempo: {
        label: 'Speed (Tempo)', category: 'Time / Pitch',
        params: { factor: { label: 'Factor', min: 0.5, max: 2.0, step: 0.05, default: 1.0, unit: 'x' } },
    },
    pitch: {
        label: 'Pitch Shift', category: 'Time / Pitch',
        params: { semitones: { label: 'Semitones', min: -12, max: 12, step: 0.5, default: 0, unit: 'st' } },
    },
    eq_highpass: {
        label: 'High-pass', category: 'EQ',
        params: { cutoff_hz: { label: 'Cutoff', min: 20, max: 1000, step: 10, default: 80, unit: 'Hz' } },
    },
    eq_lowpass: {
        label: 'Low-pass', category: 'EQ',
        params: { cutoff_hz: { label: 'Cutoff', min: 1000, max: 20000, step: 100, default: 12000, unit: 'Hz' } },
    },
    eq_shelf_low: {
        label: 'Bass Shelf', category: 'EQ',
        params: {
            cutoff_hz: { label: 'Frequency', min: 50, max: 500, step: 10, default: 200, unit: 'Hz' },
            gain_db:   { label: 'Gain',      min: -12, max: 12, step: 0.5, default: 0, unit: 'dB' },
        },
    },
    eq_shelf_high: {
        label: 'Treble Shelf', category: 'EQ',
        params: {
            cutoff_hz: { label: 'Frequency', min: 1000, max: 12000, step: 100, default: 4000, unit: 'Hz' },
            gain_db:   { label: 'Gain',      min: -12, max: 12, step: 0.5, default: 0, unit: 'dB' },
        },
    },
    reverb: {
        label: 'Reverb', category: 'Space',
        params: {
            room_size: { label: 'Room',    min: 0, max: 1, step: 0.05, default: 0.5 },
            damping:   { label: 'Damping', min: 0, max: 1, step: 0.05, default: 0.5 },
            wet:       { label: 'Wet',     min: 0, max: 1, step: 0.05, default: 0.3 },
        },
    },
    echo: {
        label: 'Echo / Delay', category: 'Space',
        params: {
            delay_sec: { label: 'Delay',    min: 0.05, max: 1, step: 0.01, default: 0.25, unit: 's' },
            feedback:  { label: 'Feedback', min: 0, max: 0.95, step: 0.05, default: 0.4 },
            wet:       { label: 'Wet',      min: 0, max: 1, step: 0.05, default: 0.4 },
        },
    },
    compressor: {
        label: 'Compressor', category: 'Dynamics',
        params: {
            threshold_db: { label: 'Threshold', min: -40, max: 0, step: 1, default: -20, unit: 'dB' },
            ratio:        { label: 'Ratio',     min: 1, max: 20, step: 0.5, default: 4, unit: ':1' },
            attack_ms:    { label: 'Attack',    min: 1, max: 200, step: 1, default: 10, unit: 'ms' },
            release_ms:   { label: 'Release',   min: 10, max: 1000, step: 10, default: 150, unit: 'ms' },
            makeup_db:    { label: 'Makeup',    min: 0, max: 24, step: 0.5, default: 0, unit: 'dB' },
        },
    },
    noise_gate: {
        label: 'Noise Gate', category: 'Dynamics',
        params: {
            threshold_db: { label: 'Threshold', min: -80, max: -20, step: 1, default: -50, unit: 'dB' },
            attack_ms:    { label: 'Attack',    min: 1, max: 50, step: 1, default: 5, unit: 'ms' },
            release_ms:   { label: 'Release',   min: 10, max: 500, step: 10, default: 100, unit: 'ms' },
        },
    },
    de_reverb: {
        label: 'De-reverb', category: 'Restoration',
        params: { strength: { label: 'Strength', min: 0, max: 1, step: 0.05, default: 0.5 } },
    },
    de_ess: {
        label: 'De-esser', category: 'Restoration',
        params: { strength: { label: 'Strength', min: 0, max: 1, step: 0.05, default: 0.5 } },
    },
    normalize_lufs: {
        label: 'LUFS Normalize', category: 'Mastering',
        params: { target_lufs: { label: 'Target', min: -36, max: -6, step: 0.5, default: -16, unit: 'LUFS' } },
    },
    normalize_peak: {
        label: 'Peak Normalize', category: 'Mastering',
        params: { target: { label: 'Target', min: 0.5, max: 1, step: 0.01, default: 0.95 } },
    },
};

// Range ops are added by the toolbar (selection-driven) — they don't appear in the +Add menu.
const RANGE_OPS = {
    cut:      { label: 'Cut' },
    trim:     { label: 'Trim to Selection' },
    silence:  { label: 'Silence' },
    fade_in:  { label: 'Fade In' },
    fade_out: { label: 'Fade Out' },
};


const TabJobs = {
    // ---- State ----
    _activeSource:    null,   // { kind, job_id, index?, name?, label }
    _peaks:           null,   // { peaks, rms, sample_rate, duration_sec, buckets }
    _peaksSig:        '',     // signature of last loaded source
    _chain:           [],     // [ { id, type, params, enabled } ]
    _chainSeq:        1,
    _selection:       null,   // { start, end } seconds
    _zoom:            1,      // >=1
    _viewOffset:      0,      // seconds
    _playheadSec:     0,
    _playing:         false,
    _audio:           null,   // own HTMLAudioElement
    _audioBlobUrl:    null,   // blob: URL backing _audio.src (revoked on switch)
    _expandedJobs:    new Set(),
    _editsByJob:      new Map(),  // job_id -> [{name, format, ...}]
    _libSearch:       '',
    _renderInProgress:false,
    _libSig:          '',
    _ctxMenu:         null,
    _addMenu:         null,
    _wfRafId:         null,
    _saveDialog:      null,
    _initialized:     false,

    init() {
        if (this._initialized) return;  // guard against double-init
        this._buildShell();
        this._initialized = true;
        this._refreshLibrary();
        this._redraw();
    },

    // Called by app.js when the user switches to this tab. Canvas dimensions
    // are zero while the tab is hidden, so we re-measure on activation.
    onTabActivated() {
        if (!this._initialized) return;
        this._resizeCanvas();
    },

    // App.pollJobs() calls this on every poll; refresh only when jobs change.
    render(force) {
        if (!this._initialized) return;
        // Only rebuild library when jobs change
        const sig = (App.state.jobs || [])
            .map(j => `${j.job_id}:${j.status}:${j.chunks_completed}:${j.final_file || ''}`)
            .join('|');
        if (force || sig !== this._libSig) {
            this._libSig = sig;
            this._renderLibrary();
        }
    },

    // ---- Top-level layout ----
    _buildShell() {
        const container = document.getElementById('tab-jobs');
        container.innerHTML = '';
        const shell = el('div', { className: 'editor-shell' });

        // -- Left: Library --
        const libPane = el('div', { className: 'editor-pane', id: 'lib-pane' });
        libPane.appendChild(el('div', { className: 'editor-pane-header' },
            el('span', null, 'Library'),
            el('span', { className: 'spacer' }),
            el('button', { className: 'btn btn-sm', onClick: () => App.pollJobs() }, 'Refresh'),
        ));
        const libBody = el('div', { className: 'editor-pane-body' });
        libBody.appendChild(el('input', {
            type: 'text',
            placeholder: 'Search jobs / text...',
            className: 'lib-search',
            id: 'lib-search',
            onInput: (e) => { this._libSearch = e.target.value.toLowerCase(); this._renderLibrary(); },
        }));
        libBody.appendChild(el('div', { id: 'lib-list' }));
        libPane.appendChild(libBody);
        shell.appendChild(libPane);

        // -- Center: Editor --
        const editPane = el('div', { className: 'editor-pane', id: 'edit-pane' });
        const tb = el('div', { className: 'editor-toolbar' });
        tb.appendChild(el('span', { className: 'editor-source-name', id: 'src-name' }, 'No source'));
        tb.appendChild(el('span', { className: 'editor-source-meta', id: 'src-meta' }, '--'));
        tb.appendChild(el('button', {
            className: 'btn btn-sm', id: 'btn-save-as',
            onClick: () => this._showSaveDialog(),
        }, 'Save As...'));
        tb.appendChild(el('button', {
            className: 'btn btn-sm', id: 'btn-download',
            onClick: () => this._download(),
        }, 'Download'));
        editPane.appendChild(tb);

        const wfArea = el('div', { className: 'waveform-area', id: 'wf-area' });
        const canvas = el('canvas', { className: 'waveform-canvas', id: 'wf-canvas' });
        const overlay = el('div', { className: 'waveform-overlay' },
            el('div', { className: 'waveform-selection', id: 'wf-sel', style: { display: 'none' } }),
            el('div', { className: 'waveform-playhead', id: 'wf-ph', style: { display: 'none' } }),
        );
        const empty = el('div', { className: 'waveform-empty', id: 'wf-empty' },
            'Pick a track from the library to start editing');
        const loading = el('div', { className: 'waveform-loading', id: 'wf-loading',
            style: { display: 'none' } }, 'Loading...');
        wfArea.appendChild(canvas);
        wfArea.appendChild(overlay);
        wfArea.appendChild(empty);
        wfArea.appendChild(loading);
        editPane.appendChild(wfArea);

        // Transport
        const transport = el('div', { className: 'transport' });
        transport.appendChild(el('button', { className: 'transport-btn primary', id: 'btn-play',
            onClick: () => this._togglePlay() }, '▶'));
        transport.appendChild(el('button', { className: 'transport-btn', id: 'btn-stop',
            onClick: () => this._stop() }, '■'));
        transport.appendChild(el('span', { className: 'transport-time', id: 'time' },
            '00:00.00 / 00:00.00'));
        transport.appendChild(el('span', { className: 'transport-selection', id: 'sel-time' }));
        const rangeRow = el('div', { className: 'range-actions' });
        for (const [key, def] of Object.entries(RANGE_OPS)) {
            rangeRow.appendChild(el('button', {
                className: 'btn btn-sm', id: `btn-${key}`,
                onClick: () => this._addRangeOp(key),
                disabled: 'disabled',
            }, def.label));
        }
        transport.appendChild(rangeRow);
        const zoomWrap = el('div', { className: 'transport-zoom' });
        zoomWrap.appendChild(el('span', null, 'Zoom'));
        zoomWrap.appendChild(el('input', {
            type: 'range', min: '1', max: '40', step: '0.5', value: '1', id: 'wf-zoom',
            onInput: (e) => this._setZoom(parseFloat(e.target.value)),
        }));
        zoomWrap.appendChild(el('span', { className: 'mono text-sm', id: 'zoom-label' }, '1.0x'));
        transport.appendChild(zoomWrap);
        editPane.appendChild(transport);
        shell.appendChild(editPane);

        // -- Right: Effects rack --
        const fxPane = el('div', { className: 'editor-pane', id: 'fx-pane' });
        fxPane.appendChild(el('div', { className: 'editor-pane-header' },
            el('span', null, 'Effects'),
            el('span', { className: 'spacer' }),
            el('button', { className: 'btn btn-sm',
                onClick: () => this._clearChain() }, 'Clear'),
        ));
        const fxBody = el('div', { className: 'editor-pane-body' });
        fxBody.appendChild(el('button', {
            className: 'fx-add', id: 'fx-add',
            onClick: (e) => this._showAddMenu(e.currentTarget),
        }, '+ Add Effect'));
        fxBody.appendChild(el('div', { id: 'fx-list' }));
        fxPane.appendChild(fxBody);
        const fxFoot = el('div', { className: 'editor-pane-footer' });
        const fmtSel = el('select', { id: 'render-format' });
        for (const f of ['wav', 'flac', 'ogg', 'mp3']) {
            fmtSel.appendChild(el('option', { value: f }, f.toUpperCase()));
        }
        fxFoot.appendChild(fmtSel);
        fxFoot.appendChild(el('button', {
            className: 'btn btn-primary', id: 'btn-render', style: { flex: '1' },
            onClick: () => this._render(),
        }, 'Render'));
        fxPane.appendChild(fxFoot);
        shell.appendChild(fxPane);

        container.appendChild(shell);

        // Hook canvas events
        this._hookCanvas();

        // Hide stray menus when clicking elsewhere. Per-item handlers already
        // call preventDefault on contextmenu — no document-level handler needed.
        document.addEventListener('click', (e) => this._maybeCloseMenus(e), true);
        window.addEventListener('resize', () => this._resizeCanvas());
    },

    // ----------------------------------------------------------------------
    // Library
    // ----------------------------------------------------------------------
    async _refreshLibrary() {
        for (const jobId of this._expandedJobs) await this._fetchEdits(jobId);
        // Trigger initial fetch — App polls regularly
        await App.pollJobs();
    },

    _renderLibrary() {
        const list = document.getElementById('lib-list');
        if (!list) return;
        list.innerHTML = '';

        const jobs = App.state.jobs || [];
        const search = this._libSearch;
        const filtered = jobs.filter(j => {
            if (!search) return true;
            const hay = `${j.text_preview || ''} ${j.model || ''} ${j.job_id || ''}`.toLowerCase();
            return hay.includes(search);
        });

        if (filtered.length === 0) {
            list.appendChild(el('div', { className: 'lib-empty' },
                jobs.length === 0 ? 'No jobs yet — generate something on the Testing tab.'
                                   : 'No matches.'));
            return;
        }

        for (const job of filtered) {
            list.appendChild(this._renderLibJob(job));
        }
    },

    _renderLibJob(job) {
        const jobId = job.job_id;
        const expanded = this._expandedJobs.has(jobId);
        const wrap = el('div', { className: `lib-job ${expanded ? 'expanded' : ''}` });

        const title = job.text_preview
            ? job.text_preview.replace(/\s+/g, ' ').slice(0, 60)
            : `${job.model || '?'} · ${jobId.slice(0, 8)}`;

        const header = el('div', {
            className: 'lib-job-header',
            onClick: (e) => {
                if (e.target.closest('.lib-item-icon-btn')) return;
                this._toggleExpand(jobId);
            },
            onContextMenu: (e) => {
                e.preventDefault();
                this._openJobCtxMenu(e, job);
            },
        },
            el('span', { className: 'lib-job-caret' }, '▶'),
            el('span', { className: 'lib-job-title', title }, title),
            el('span', { className: `lib-job-status badge ${this._statusClass(job.status)}` },
                job.status || '?'),
        );
        wrap.appendChild(header);

        const children = el('div', { className: 'lib-job-children' });
        if (expanded) {
            // Final file (if any)
            if (job.final_file) {
                const src = {
                    kind: 'final', job_id: jobId, name: job.final_file,
                    label: `${this._jobLabel(job)} · final`,
                };
                children.appendChild(this._renderLibItem(src, 'FINAL', job.final_file));
            }

            // Chunks (only show completed ones — pending chunks have no audio yet)
            const completedChunks = job.chunks_completed || 0;
            for (let i = 0; i < completedChunks; i++) {
                const src = {
                    kind: 'chunk', job_id: jobId, index: i,
                    label: `${this._jobLabel(job)} · chunk ${i}`,
                };
                children.appendChild(this._renderLibItem(src, `CHK ${i}`,
                    `chunk_${String(i).padStart(3, '0')}.wav`));
            }
            const pending = (job.total_chunks || 0) - completedChunks;
            if (pending > 0) {
                children.appendChild(el('div', {
                    className: 'lib-empty',
                    style: { padding: '4px 8px 4px 22px', textAlign: 'left' },
                }, `${pending} chunk${pending === 1 ? '' : 's'} not yet generated`));
            }

            // Edits (lazy fetch, then re-render this job)
            const edits = this._editsByJob.get(jobId);
            if (edits === undefined) {
                this._fetchEdits(jobId);  // async, will re-render when ready
                children.appendChild(el('div', { className: 'lib-empty', style: { padding: '8px' } },
                    'Loading edits...'));
            } else if (edits.length > 0) {
                for (const ed of edits) {
                    const src = {
                        kind: 'edit', job_id: jobId, name: ed.name,
                        label: `${this._jobLabel(job)} · ${ed.name}`,
                    };
                    children.appendChild(this._renderLibItem(src, 'EDIT', ed.name));
                }
            }
        }
        wrap.appendChild(children);

        return wrap;
    },

    _jobLabel(job) {
        return job.text_preview
            ? job.text_preview.slice(0, 24).trim()
            : (job.model || job.job_id?.slice(0, 8) || '?');
    },

    _renderLibItem(src, kindLabel, filename) {
        const isActive = this._sameSource(this._activeSource, src);

        const item = el('div', {
            className: 'lib-item' + (isActive ? ' active' : ''),
            onClick: () => this._loadSource(src),
            onContextMenu: (e) => {
                e.preventDefault();
                this._openItemCtxMenu(e, src);
            },
        });
        item.appendChild(el('span', { className: 'lib-item-kind' }, kindLabel));
        item.appendChild(el('span', { className: 'lib-item-name', title: filename }, filename));
        const actions = el('div', { className: 'lib-item-actions' });
        if (src.kind === 'edit') {
            actions.appendChild(el('button', {
                className: 'lib-item-icon-btn',
                title: 'Delete edit',
                onClick: (e) => { e.stopPropagation(); this._deleteEdit(src.job_id, src.name); },
            }, '✕'));
        }
        item.appendChild(actions);
        return item;
    },

    _statusClass(s) {
        const map = {
            completed: 'badge-green', running: 'badge-orange',
            partial: 'badge-orange', failed: 'badge-red',
        };
        return map[s] || 'badge-gray';
    },

    _toggleExpand(jobId) {
        if (this._expandedJobs.has(jobId)) this._expandedJobs.delete(jobId);
        else this._expandedJobs.add(jobId);
        this._renderLibrary();
    },

    async _fetchEdits(jobId) {
        try {
            const data = await App.api('GET', `/api/audio/edits/${jobId}`);
            this._editsByJob.set(jobId, data.edits || []);
            this._renderLibrary();
        } catch {
            this._editsByJob.set(jobId, []);
            this._renderLibrary();
        }
    },

    // ----------------------------------------------------------------------
    // Context menus
    // ----------------------------------------------------------------------
    _openJobCtxMenu(e, job) {
        const items = [];
        const jobId = job.job_id;
        if (job.status === 'failed' || job.status === 'partial' ||
            (job.status === 'running' && (job.chunks_completed || 0) < (job.total_chunks || 0))) {
            items.push({ label: 'Recover',
                onClick: () => this._recoverJob(jobId) });
        }
        if (job.status === 'running') {
            items.push({ label: 'Cancel',
                onClick: () => this._cancelJob(job) });
        }
        items.push({ sep: true });
        items.push({ label: 'Delete job', danger: true,
            onClick: () => this._deleteJob(jobId) });
        this._showCtxMenu(e.clientX, e.clientY, items);
    },

    _openItemCtxMenu(e, src) {
        const items = [];
        items.push({ label: 'Open / Load',
            onClick: () => this._loadSource(src) });
        items.push({ label: 'Download',
            onClick: () => this._downloadSource(src) });
        if (src.kind === 'edit') {
            items.push({ sep: true });
            items.push({ label: 'Delete edit', danger: true,
                onClick: () => this._deleteEdit(src.job_id, src.name) });
        }
        this._showCtxMenu(e.clientX, e.clientY, items);
    },

    _showCtxMenu(x, y, items) {
        this._closeCtxMenu();
        const menu = el('div', { className: 'ctx-menu' });
        for (const it of items) {
            if (it.sep) {
                menu.appendChild(el('div', { className: 'ctx-menu-sep' }));
            } else {
                const cls = 'ctx-menu-item' + (it.danger ? ' danger' : '');
                menu.appendChild(el('div', { className: cls,
                    onClick: () => { this._closeCtxMenu(); it.onClick(); } },
                    it.label));
            }
        }
        document.body.appendChild(menu);
        // Position with viewport bounds in mind
        const w = menu.offsetWidth, h = menu.offsetHeight;
        const px = Math.min(x, window.innerWidth - w - 8);
        const py = Math.min(y, window.innerHeight - h - 8);
        menu.style.left = `${px}px`;
        menu.style.top = `${py}px`;
        this._ctxMenu = menu;
    },

    _closeCtxMenu() {
        if (this._ctxMenu) {
            this._ctxMenu.remove();
            this._ctxMenu = null;
        }
    },

    _maybeCloseMenus(e) {
        if (this._ctxMenu && !this._ctxMenu.contains(e.target)) this._closeCtxMenu();
        if (this._addMenu && !this._addMenu.contains(e.target) &&
            !e.target.closest('#fx-add')) {
            this._addMenu.remove();
            this._addMenu = null;
        }
    },

    // ----------------------------------------------------------------------
    // Library actions: delete / recover / cancel
    // ----------------------------------------------------------------------
    async _deleteJob(jobId) {
        if (!confirm(`Delete job ${jobId.slice(0, 8)}? This removes all audio files.`)) return;
        try {
            const result = await App.api('POST', '/api/jobs/delete', { job_ids: [jobId] });
            if (!result.results?.[jobId]) throw new Error('Server could not delete this job; it may still be active.');
            this._editsByJob.delete(jobId);
            this._expandedJobs.delete(jobId);
            if (this._activeSource && this._activeSource.job_id === jobId) {
                this._clearActiveSource();
            }
            App.toast('Job deleted', 'success');
            await App.pollJobs();
        } catch (e) {
            App.toast(`Delete failed: ${e.message}`, 'error');
        }
    },

    async _recoverJob(jobId) {
        try {
            await App.api('POST', `/api/jobs/${jobId}/recover`);
            App.toast('Recovery started', 'success');
            await App.pollJobs();
        } catch (e) {
            App.toast(`Recovery failed: ${e.message}`, 'error');
        }
    },

    async _cancelJob(job) {
        if (!confirm(`Cancel job ${(job.job_id || '').slice(0, 8)}?`)) return;
        try {
            await App.api('POST', `/api/tts/${job.model}/cancel`, { job_id: job.job_id });
            App.toast('Cancellation requested', 'success');
            await App.pollJobs();
        } catch (e) {
            App.toast(`Cancel failed: ${e.message}`, 'error');
        }
    },

    async _deleteEdit(jobId, editName) {
        if (!confirm(`Delete edit "${editName}"?`)) return;
        try {
            await App.api('DELETE', `/api/audio/edits/${jobId}/${encodeURIComponent(editName)}`);
            const edits = this._editsByJob.get(jobId) || [];
            this._editsByJob.set(jobId, edits.filter(e => e.name !== editName));
            if (this._activeSource && this._activeSource.kind === 'edit'
                && this._activeSource.job_id === jobId
                && this._activeSource.name === editName) {
                this._clearActiveSource();
            }
            App.toast('Edit deleted', 'success');
            this._renderLibrary();
        } catch (e) {
            App.toast(`Delete failed: ${e.message}`, 'error');
        }
    },

    // ----------------------------------------------------------------------
    // Source loading + waveform
    // ----------------------------------------------------------------------
    async _loadSource(src) {
        // Don't re-load the same source — wastes a network round-trip.
        if (this._activeSource && this._sameSource(this._activeSource, src)) return;

        // Chain refers to the prior source's timeline; switching invalidates it.
        // Confirm with the user if they have unsaved work.
        if (this._chain.length > 0) {
            const ok = confirm(
                `You have ${this._chain.length} unsaved effect${this._chain.length === 1 ? '' : 's'} ` +
                `on the current source. Render or clear them before switching, or click OK to discard.`);
            if (!ok) return;
        }
        this._chain = [];
        this._selection = null;
        this._zoom = 1;
        this._viewOffset = 0;
        this._playheadSec = 0;
        this._activeSource = src;
        this._stop();
        this._audio = null;
        this._peaks = null;
        if (this._audioBlobUrl) URL.revokeObjectURL(this._audioBlobUrl);
        this._audioBlobUrl = null;
        const generation = this._sourceGeneration = (this._sourceGeneration || 0) + 1;

        document.getElementById('src-name').textContent = src.label;
        document.getElementById('src-meta').textContent = '...';
        document.getElementById('wf-empty').style.display = 'none';
        document.getElementById('wf-loading').style.display = 'block';

        this._renderChain();
        this._renderLibrary();
        this._updateRangeButtons();

        try {
            const buckets = this._canvasWidth();
            const data = await App.api('POST', '/api/audio/peaks',
                { source: this._sourceForApi(src), buckets });
            // Verify the source is still active (fast switching)
            if (this._activeSource !== src) return;
            this._peaks = data;
            this._peaksSig = JSON.stringify(this._sourceForApi(src));
            this._setupAudio();
            this._resizeCanvas();
            this._redraw();
        } catch (e) {
            if (this._activeSource !== src) return;
            App.toast(`Load failed: ${e.message}`, 'error');
            this._activeSource = null;
            this._peaks = null;
        } finally {
            if (generation !== this._sourceGeneration) return;
            const ld = document.getElementById('wf-loading');
            if (ld) ld.style.display = 'none';
            const empty = document.getElementById('wf-empty');
            if (empty) empty.style.display = this._peaks ? 'none' : 'flex';
        }
    },

    _clearActiveSource() {
        this._sourceGeneration = (this._sourceGeneration || 0) + 1;
        this._stop();
        this._audio = null;
        this._activeSource = null;
        this._peaks = null;
        this._chain = [];
        this._selection = null;
        document.getElementById('src-name').textContent = 'No source';
        document.getElementById('src-meta').textContent = '--';
        document.getElementById('wf-empty').style.display = 'flex';
        this._renderChain();
        this._redraw();
    },

    _sourceForApi(src) {
        const out = { kind: src.kind, job_id: src.job_id };
        if (src.index !== undefined) out.index = src.index;
        if (src.name) out.name = src.name;
        return out;
    },

    _sameSource(a, b) {
        if (!a || !b) return false;
        return a.kind === b.kind
            && a.job_id === b.job_id
            && a.index === b.index
            && a.name === b.name;
    },

    // Returns the bare API path (no ?token=). Callers fetch it with
    // App.fetchBlobUrl() so the token travels in the X-TTS-API-Token header
    // rather than the URL (which would leak into history/access logs).
    _audioUrlForSource(src) {
        if (!src) return null;
        if (src.kind === 'final') {
            return `/api/jobs/${src.job_id}/output`;
        }
        if (src.kind === 'chunk') {
            return `/api/jobs/${src.job_id}/chunks/${src.index}/audio`;
        }
        if (src.kind === 'edit') {
            return `/api/audio/edits/${src.job_id}/${encodeURIComponent(src.name)}`;
        }
        return null;
    },

    _setupAudio() {
        // Tear down old audio without setting src='' — that would fire a
        // spurious error event. Pausing + dropping our reference is enough
        // for GC to release the underlying resources.
        if (this._audio) {
            try { this._audio.pause(); } catch {}
            this._audio = null;
        }
        // Release the previous source's blob URL (created below). We mint a new
        // one per source rather than leaking the prior one for the session.
        if (this._audioBlobUrl) {
            try { URL.revokeObjectURL(this._audioBlobUrl); } catch {}
            this._audioBlobUrl = null;
        }
        const path = this._audioUrlForSource(this._activeSource);
        if (!path) return;
        const ownerSource = this._activeSource;  // captured for stale-event filtering
        const audio = new Audio();
        audio.preload = 'metadata';
        audio.addEventListener('ended', () => {
            if (this._audio !== audio) return;
            this._playing = false;
            this._updatePlayButton();
            this._stopRaf();
            this._renderTime();
        });
        audio.addEventListener('loadedmetadata', () => {
            if (this._audio !== audio) return;
            this._renderTime();
        });
        audio.addEventListener('error', () => {
            if (this._audio !== audio) return;            // stale element
            if (this._activeSource !== ownerSource) return; // user switched
            App.toast('Audio failed to load', 'error');
        });
        this._audio = audio;
        // Fetch the bytes with the X-TTS-API-Token header (not a ?token= URL)
        // and play a blob: URL, so the token never lands in the <audio> src.
        App.fetchBlobUrl(path).then((blobUrl) => {
            if (this._audio !== audio || this._activeSource !== ownerSource) {
                // User switched sources while we were fetching — discard.
                try { URL.revokeObjectURL(blobUrl); } catch {}
                return;
            }
            this._audioBlobUrl = blobUrl;
            audio.src = blobUrl;
        }).catch(() => {
            if (this._audio !== audio || this._activeSource !== ownerSource) return;
            App.toast('Audio failed to load', 'error');
        });
    },

    // ----------------------------------------------------------------------
    // Canvas wiring
    // ----------------------------------------------------------------------
    _hookCanvas() {
        const area = document.getElementById('wf-area');
        const canvas = document.getElementById('wf-canvas');
        let down = false, downX = 0, downTime = 0, dragged = false;

        const xToTime = (x) => {
            if (!this._peaks) return 0;
            const w = canvas.clientWidth;
            const span = this._peaks.duration_sec / this._zoom;
            return Math.max(0, Math.min(this._peaks.duration_sec,
                this._viewOffset + (x / w) * span));
        };

        // Bind window-level move/up only for the duration of a drag, and remove
        // them on mouseup. This avoids a permanent window listener that fires on
        // every pointer move for the app's lifetime, and prevents stale
        // listeners from double-firing if the shell is ever rebuilt.
        const onMove = (e) => {
            if (!down) return;
            const r = canvas.getBoundingClientRect();
            const x = e.clientX - r.left;
            if (Math.abs(x - downX) > 3) dragged = true;
            const t = xToTime(x);
            if (this._selection) {
                this._selection.start = Math.min(downTime, t);
                this._selection.end = Math.max(downTime, t);
                this._renderSelection();
                this._renderTime();
            }
        };
        const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            if (!down) return;
            down = false;
            if (!dragged) {
                // Treat as a seek click
                this._selection = null;
                this._renderSelection();
                this._seek(downTime);
            } else {
                this._renderSelection();
            }
            this._updateRangeButtons();
            this._renderTime();
        };

        canvas.addEventListener('mousedown', (e) => {
            if (!this._peaks) return;
            const r = canvas.getBoundingClientRect();
            down = true; downX = e.clientX - r.left;
            downTime = xToTime(downX);
            dragged = false;
            // Provisional selection
            this._selection = { start: downTime, end: downTime };
            this._renderSelection();
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onUp);
        });

        // Wheel: zoom around cursor
        area.addEventListener('wheel', (e) => {
            if (!this._peaks) return;
            e.preventDefault();
            const r = canvas.getBoundingClientRect();
            const x = e.clientX - r.left;
            const tBefore = xToTime(x);
            const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
            const newZoom = Math.max(1, Math.min(40, this._zoom * factor));
            this._zoom = newZoom;
            const span = this._peaks.duration_sec / this._zoom;
            // Re-anchor view so that tBefore stays under cursor
            this._viewOffset = Math.max(0, Math.min(this._peaks.duration_sec - span,
                tBefore - (x / canvas.clientWidth) * span));
            const zs = document.getElementById('wf-zoom');
            if (zs) zs.value = String(this._zoom.toFixed(1));
            const zl = document.getElementById('zoom-label');
            if (zl) zl.textContent = `${this._zoom.toFixed(1)}x`;
            this._redraw();
        }, { passive: false });
    },

    _canvasWidth() {
        const c = document.getElementById('wf-canvas');
        return Math.max(256, Math.floor(c?.clientWidth || 1024));
    },

    _resizeCanvas() {
        const canvas = document.getElementById('wf-canvas');
        if (!canvas) return;
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        // Skip when canvas is hidden (parent display:none) — clientWidth is 0
        // and resizing the backing buffer to 2px would lose detail when the
        // tab becomes visible. onTabActivated re-runs this once we're shown.
        if (w < 4 || h < 4) return;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
        this._redraw();
    },

    _redraw() {
        this._renderWaveform();
        this._renderSelection();
        this._renderPlayhead();
        this._renderTime();
    },

    _renderWaveform() {
        const canvas = document.getElementById('wf-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const w = canvas.width, h = canvas.height;
        ctx.clearRect(0, 0, w, h);

        const peaks = this._peaks;
        if (!peaks || !peaks.peaks?.length) return;

        const total = peaks.peaks.length;
        const totalDur = peaks.duration_sec || 1;
        const span = totalDur / this._zoom;
        const startSec = this._viewOffset;
        const endSec = startSec + span;
        const startIdx = Math.max(0, Math.floor(startSec / totalDur * total));
        const endIdx = Math.min(total, Math.ceil(endSec / totalDur * total));

        const visible = peaks.peaks.slice(startIdx, endIdx);
        const rms = peaks.rms ? peaks.rms.slice(startIdx, endIdx) : null;
        const mid = h / 2;

        // RMS body (darker)
        if (rms) {
            ctx.fillStyle = 'rgba(88, 166, 255, 0.18)';
            ctx.beginPath();
            ctx.moveTo(0, mid);
            for (let i = 0; i < visible.length; i++) {
                const x = (i / visible.length) * w;
                const r = rms[i];
                ctx.lineTo(x, mid - r * mid * 0.95);
            }
            for (let i = visible.length - 1; i >= 0; i--) {
                const x = (i / visible.length) * w;
                const r = rms[i];
                ctx.lineTo(x, mid + r * mid * 0.95);
            }
            ctx.closePath();
            ctx.fill();
        }

        // Peak envelope (brighter outline)
        ctx.strokeStyle = 'rgba(88, 166, 255, 0.85)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let i = 0; i < visible.length; i++) {
            const x = Math.floor((i / visible.length) * w) + 0.5;
            const [mn, mx] = visible[i];
            ctx.moveTo(x, mid - mx * mid * 0.95);
            ctx.lineTo(x, mid - mn * mid * 0.95);
        }
        ctx.stroke();

        // Center line
        ctx.strokeStyle = 'rgba(110, 118, 129, 0.4)';
        ctx.beginPath();
        ctx.moveTo(0, mid);
        ctx.lineTo(w, mid);
        ctx.stroke();
    },

    _renderSelection() {
        const sel = document.getElementById('wf-sel');
        if (!sel) return;
        if (!this._selection || !this._peaks) {
            sel.style.display = 'none';
            return;
        }
        const totalDur = this._peaks.duration_sec || 1;
        const span = totalDur / this._zoom;
        const w = document.getElementById('wf-canvas').clientWidth;
        const x1 = ((this._selection.start - this._viewOffset) / span) * w;
        const x2 = ((this._selection.end - this._viewOffset) / span) * w;
        const left = Math.max(0, Math.min(w, x1));
        const right = Math.max(0, Math.min(w, x2));
        if (right <= left || right <= 0 || left >= w) {
            sel.style.display = 'none';
            return;
        }
        sel.style.display = 'block';
        sel.style.left = `${left}px`;
        sel.style.width = `${right - left}px`;
    },

    _renderPlayhead() {
        const ph = document.getElementById('wf-ph');
        if (!ph) return;
        if (!this._peaks) {
            ph.style.display = 'none';
            return;
        }
        const totalDur = this._peaks.duration_sec || 1;
        const span = totalDur / this._zoom;
        const w = document.getElementById('wf-canvas').clientWidth;
        const x = ((this._playheadSec - this._viewOffset) / span) * w;
        if (x < 0 || x > w) {
            ph.style.display = 'none';
            return;
        }
        ph.style.display = 'block';
        ph.style.left = `${x}px`;
    },

    _renderTime() {
        const t = document.getElementById('time');
        if (!t) return;
        const dur = this._peaks?.duration_sec || 0;
        const pos = this._playheadSec || 0;
        t.textContent = `${this._fmtTime(pos)} / ${this._fmtTime(dur)}`;

        const sel = document.getElementById('sel-time');
        if (sel) {
            if (this._selection && this._peaks) {
                const len = this._selection.end - this._selection.start;
                if (len > 0.01) {
                    sel.textContent = `Sel ${this._fmtTime(this._selection.start)}–${this._fmtTime(this._selection.end)} (${len.toFixed(2)}s)`;
                } else {
                    sel.textContent = '';
                }
            } else {
                sel.textContent = '';
            }
        }

        const meta = document.getElementById('src-meta');
        if (meta && this._peaks) {
            meta.textContent = `${this._fmtTime(dur)} · ${this._peaks.sample_rate} Hz`;
        }
    },

    _fmtTime(s) {
        if (!isFinite(s) || s < 0) s = 0;
        const m = Math.floor(s / 60);
        const sec = s - m * 60;
        return `${String(m).padStart(2, '0')}:${sec.toFixed(2).padStart(5, '0')}`;
    },

    _setZoom(z) {
        const old = this._zoom;
        this._zoom = Math.max(1, z);
        if (this._peaks) {
            const span = this._peaks.duration_sec / this._zoom;
            // Center on playhead if visible, otherwise on view center
            const center = this._playheadSec >= this._viewOffset && this._playheadSec <= this._viewOffset + this._peaks.duration_sec / old
                ? this._playheadSec
                : this._viewOffset + (this._peaks.duration_sec / old) / 2;
            this._viewOffset = Math.max(0, Math.min(this._peaks.duration_sec - span, center - span / 2));
        }
        const zl = document.getElementById('zoom-label');
        if (zl) zl.textContent = `${this._zoom.toFixed(1)}x`;
        this._redraw();
    },

    // ----------------------------------------------------------------------
    // Transport
    // ----------------------------------------------------------------------
    _togglePlay() {
        if (!this._audio) return;
        if (this._playing) this._pause();
        else this._play();
    },

    _play() {
        if (!this._audio) return;
        // Stop App-level audio (testing tab) so they don't double-play
        try { App.stopAudio(); } catch {}
        const p = this._audio.play();
        this._playing = true;
        this._updatePlayButton();
        this._startRaf();
        if (p && typeof p.catch === 'function') {
            p.catch((err) => {
                console.warn('Audio play failed', err);
                this._playing = false;
                this._updatePlayButton();
                this._stopRaf();
            });
        }
    },

    _pause() {
        if (!this._audio) return;
        this._audio.pause();
        this._playing = false;
        this._updatePlayButton();
        this._stopRaf();
    },

    _stop() {
        if (this._audio) {
            this._audio.pause();
            try { this._audio.currentTime = 0; } catch {}
        }
        this._playing = false;
        this._playheadSec = 0;
        this._updatePlayButton();
        this._stopRaf();
        this._renderPlayhead();
        this._renderTime();
    },

    _seek(timeSec) {
        if (!this._audio || !this._peaks) return;
        const t = Math.max(0, Math.min(this._peaks.duration_sec || 0, timeSec));
        try { this._audio.currentTime = t; } catch {}
        this._playheadSec = t;
        this._renderPlayhead();
        this._renderTime();
    },

    _updatePlayButton() {
        const btn = document.getElementById('btn-play');
        if (btn) btn.textContent = this._playing ? '⏸' : '▶';
    },

    _startRaf() {
        this._stopRaf();
        const tick = () => {
            if (!this._playing) return;
            if (this._audio) {
                this._playheadSec = this._audio.currentTime;
                // Auto-scroll if playhead reaches edge
                if (this._peaks) {
                    const span = this._peaks.duration_sec / this._zoom;
                    if (this._playheadSec > this._viewOffset + span * 0.95) {
                        this._viewOffset = Math.max(0, Math.min(this._peaks.duration_sec - span,
                            this._playheadSec - span * 0.5));
                        this._redraw();
                    } else if (this._playheadSec < this._viewOffset && this._zoom > 1.001) {
                        this._viewOffset = Math.max(0, this._playheadSec);
                        this._redraw();
                    }
                }
                this._renderPlayhead();
                this._renderTime();
            }
            this._wfRafId = requestAnimationFrame(tick);
        };
        this._wfRafId = requestAnimationFrame(tick);
    },

    _stopRaf() {
        if (this._wfRafId) {
            cancelAnimationFrame(this._wfRafId);
            this._wfRafId = null;
        }
    },

    _updateRangeButtons() {
        const has = !!(this._selection && this._selection.end - this._selection.start > 0.01);
        for (const k of Object.keys(RANGE_OPS)) {
            const btn = document.getElementById(`btn-${k}`);
            if (btn) btn.disabled = !has;
        }
    },

    _addRangeOp(opName) {
        if (!this._selection) return;
        const def = RANGE_OPS[opName];
        if (!def) return;
        // Range ops use absolute sample times from the CURRENT waveform, so they
        // must run before any time-warping effect (tempo / pitch / cut-from-earlier).
        // Insert after the existing range ops so insertion order is preserved
        // and full-track effects always come after.
        const op = {
            id: this._chainSeq++,
            type: opName,
            params: { start_sec: this._selection.start, end_sec: this._selection.end },
            enabled: true,
            isRange: true,
            label: `${def.label} · ${this._selection.start.toFixed(2)}s–${this._selection.end.toFixed(2)}s`,
        };
        let insertAt = 0;
        while (insertAt < this._chain.length && this._chain[insertAt].isRange) insertAt++;
        this._chain.splice(insertAt, 0, op);
        this._renderChain();
    },

    // ----------------------------------------------------------------------
    // Effect chain UI
    // ----------------------------------------------------------------------
    _renderChain() {
        const list = document.getElementById('fx-list');
        if (!list) return;
        list.innerHTML = '';
        if (this._chain.length === 0) {
            list.appendChild(el('div', { className: 'fx-empty' },
                'No effects yet. Click "+ Add Effect" or use the toolbar buttons after selecting a region.'));
            return;
        }
        for (let i = 0; i < this._chain.length; i++) {
            list.appendChild(this._renderFxCard(this._chain[i], i));
        }
    },

    _renderFxCard(item, idx) {
        const def = item.isRange ? null : FX_DEFS[item.type];
        // The card itself is only a drop target; the grip is the drag handle.
        // This way the parameter sliders remain interactive and don't initiate drag.
        const card = el('div', {
            className: `fx-card ${item.enabled ? '' : 'disabled'}`,
            onDragOver: (e) => e.preventDefault(),
            onDrop: (e) => {
                e.preventDefault();
                const draggedId = parseInt(e.dataTransfer.getData('text/plain'), 10);
                if (!Number.isNaN(draggedId)) this._reorderChain(draggedId, item.id);
            },
        });

        const header = el('div', { className: 'fx-card-header' });
        const grip = el('span', {
            className: 'fx-card-grip',
            title: 'Drag to reorder',
            draggable: 'true',
            onDragStart: (e) => {
                e.dataTransfer.setData('text/plain', String(item.id));
                e.dataTransfer.effectAllowed = 'move';
                card.classList.add('dragging');
            },
            onDragEnd: () => card.classList.remove('dragging'),
        }, '☰');
        header.appendChild(grip);
        header.appendChild(el('span', { className: 'fx-card-title' },
            item.isRange ? item.label : def.label));
        if (!item.isRange) {
            header.appendChild(el('span', {
                className: `fx-card-toggle ${item.enabled ? 'on' : ''}`,
                title: item.enabled ? 'Enabled' : 'Disabled',
                onClick: () => {
                    item.enabled = !item.enabled;
                    this._renderChain();
                },
            }, item.enabled ? 'on' : 'off'));
        }
        header.appendChild(el('button', {
            className: 'fx-card-remove',
            title: 'Remove',
            onClick: () => this._removeFx(item.id),
        }, '✕'));
        card.appendChild(header);

        if (!item.isRange && def) {
            const body = el('div', { className: 'fx-card-body' });
            for (const [pname, pdef] of Object.entries(def.params)) {
                body.appendChild(this._renderFxParam(item, pname, pdef));
            }
            card.appendChild(body);
        }
        return card;
    },

    _renderFxParam(item, pname, pdef) {
        const wrap = el('div', { className: 'fx-param' });
        const valSpan = el('span', { className: 'val' }, this._fmtVal(item.params[pname], pdef));
        const lbl = el('label', null,
            el('span', null, pdef.label),
            valSpan,
        );
        wrap.appendChild(lbl);
        const slider = el('input', {
            type: 'range',
            min: String(pdef.min),
            max: String(pdef.max),
            step: String(pdef.step),
            value: String(item.params[pname]),
            onInput: (e) => {
                const v = parseFloat(e.target.value);
                item.params[pname] = v;
                valSpan.textContent = this._fmtVal(v, pdef);
            },
        });
        wrap.appendChild(slider);
        return wrap;
    },

    _fmtVal(v, pdef) {
        if (v === undefined || v === null || Number.isNaN(v)) return '-';
        const decimals = pdef.step >= 1 ? 0 : pdef.step >= 0.1 ? 1 : 2;
        const formatted = Number(v).toFixed(decimals);
        return pdef.unit ? `${formatted} ${pdef.unit}` : formatted;
    },

    _removeFx(id) {
        this._chain = this._chain.filter(c => c.id !== id);
        this._renderChain();
    },

    _reorderChain(srcId, targetId) {
        if (srcId === targetId) return;
        const srcIdx = this._chain.findIndex(c => c.id === srcId);
        const tgtIdx = this._chain.findIndex(c => c.id === targetId);
        if (srcIdx < 0 || tgtIdx < 0) return;
        const [moved] = this._chain.splice(srcIdx, 1);
        this._chain.splice(tgtIdx, 0, moved);
        this._renderChain();
    },

    _clearChain() {
        if (this._chain.length === 0) return;
        if (!confirm('Clear all effects?')) return;
        this._chain = [];
        this._renderChain();
    },

    // ----------------------------------------------------------------------
    // Add Effect menu
    // ----------------------------------------------------------------------
    _showAddMenu(anchor) {
        if (this._addMenu) {
            this._addMenu.remove();
            this._addMenu = null;
            return;
        }
        const menu = el('div', { className: 'fx-add-menu' });

        // Group by category
        const groups = {};
        for (const [type, def] of Object.entries(FX_DEFS)) {
            (groups[def.category] = groups[def.category] || []).push({ type, def });
        }
        const cats = ['Volume', 'Time / Pitch', 'EQ', 'Dynamics', 'Space', 'Restoration', 'Mastering'];
        for (const cat of cats) {
            if (!groups[cat]) continue;
            menu.appendChild(el('div', { className: 'fx-add-menu-section' }, cat));
            for (const { type, def } of groups[cat]) {
                menu.appendChild(el('div', {
                    className: 'fx-add-menu-item',
                    onClick: () => {
                        this._addFx(type);
                        if (this._addMenu) { this._addMenu.remove(); this._addMenu = null; }
                    },
                }, def.label));
            }
        }
        document.body.appendChild(menu);
        const rect = anchor.getBoundingClientRect();
        const w = menu.offsetWidth;
        const x = Math.min(rect.left, window.innerWidth - w - 8);
        const y = rect.bottom + 4;
        menu.style.left = `${x}px`;
        menu.style.top = `${y}px`;
        this._addMenu = menu;
    },

    _addFx(type) {
        const def = FX_DEFS[type];
        if (!def) return;
        const params = {};
        for (const [pname, pdef] of Object.entries(def.params)) {
            params[pname] = pdef.default;
        }
        this._chain.push({
            id: this._chainSeq++,
            type, params,
            enabled: true,
            isRange: false,
        });
        this._renderChain();
    },

    // ----------------------------------------------------------------------
    // Render / Save As / Download
    // ----------------------------------------------------------------------
    _gatherEdits() {
        const edits = [];
        const transforms = [];
        const mapTime = t => transforms.reduce((value, fn) => fn(value), t);
        for (const item of this._chain) {
            if (!item.enabled) continue;
            const params = { ...item.params };
            if (item.isRange) {
                params.start_sec = mapTime(params.start_sec);
                params.end_sec = mapTime(params.end_sec);
            }
            edits.push({ type: item.type, params });
            const start = params.start_sec, end = params.end_sec;
            if (item.type === 'cut') transforms.push(t => t <= start ? t : t >= end ? t - (end - start) : start);
            if (item.type === 'trim') transforms.push(t => Math.max(0, Math.min(end - start, t - start)));
            if (item.type === 'tempo') transforms.push(t => t / params.factor);
            if (item.type === 'pad') transforms.push(t => t + (params.front_sec || 0));
        }
        return edits;
    },

    async _render(name) {
        if (!this._activeSource) {
            App.toast('Pick a source first', 'error');
            return;
        }
        if (this._renderInProgress) return;
        const fmt = document.getElementById('render-format')?.value || 'wav';
        const edits = this._gatherEdits();
        const chainSnapshot = JSON.stringify(this._chain);

        const btn = document.getElementById('btn-render');
        const sasBtn = document.getElementById('btn-save-as');
        if (btn) btn.disabled = true;
        if (sasBtn) sasBtn.disabled = true;
        this._renderInProgress = true;
        App.toast('Rendering...', 'info');

        // Snapshot the source being rendered. The library stays clickable during
        // the awaits below, so the user may navigate to a different source; if
        // they do, skip the post-render auto-load and respect their selection.
        const srcAtRenderStart = this._activeSource;

        try {
            const body = {
                source: this._sourceForApi(this._activeSource),
                edits,
                output_format: fmt,
            };
            if (name) body.output_name = name;
            const result = await App.api('POST', '/api/audio/render', body);
            App.toast(`Saved ${result.edit_name}`, 'success');
            // The chain was just baked into the new file. Only clear it and
            // auto-load the result if the user hasn't switched sources mid-render
            // — otherwise we'd clobber their deliberate navigation.
            if (this._activeSource === srcAtRenderStart && JSON.stringify(this._chain) === chainSnapshot) {
                await this._fetchEdits(result.job_id);
                if (this._activeSource !== srcAtRenderStart || JSON.stringify(this._chain) !== chainSnapshot) return;
                this._chain = [];
                const newSrc = {
                    kind: 'edit',
                    job_id: result.job_id,
                    name: result.edit_name,
                    label: `${this._jobLabelById(result.job_id)} · ${result.edit_name}`,
                };
                this._expandedJobs.add(result.job_id);
                await this._loadSource(newSrc);
            } else {
                // User moved on; still fetch the new edit so it appears in the
                // library (_fetchEdits re-renders it), but leave their current
                // source loaded.
                this._expandedJobs.add(result.job_id);
                await this._fetchEdits(result.job_id);
            }
        } catch (e) {
            App.toast(`Render failed: ${e.message}`, 'error');
        } finally {
            if (btn) btn.disabled = false;
            if (sasBtn) sasBtn.disabled = false;
            this._renderInProgress = false;
        }
    },

    _jobLabelById(jobId) {
        const job = (App.state.jobs || []).find(j => j.job_id === jobId);
        return job ? this._jobLabel(job) : jobId.slice(0, 8);
    },

    _showSaveDialog() {
        if (!this._activeSource) {
            App.toast('Pick a source first', 'error');
            return;
        }
        if (this._saveDialog) return;
        const fmt = document.getElementById('render-format')?.value || 'wav';

        const back = el('div', { className: 'modal-back',
            onClick: (e) => { if (e.target === back) this._closeSaveDialog(); } });
        const card = el('div', { className: 'modal-card' });
        card.appendChild(el('div', { className: 'modal-title' }, 'Save edit as...'));
        const row = el('div', { className: 'modal-row' });
        row.appendChild(el('label', null, 'Name'));
        const input = el('input', {
            type: 'text',
            id: 'save-name-input',
            placeholder: 'my_edit',
            value: this._defaultSaveName(),
            onKeyDown: (e) => {
                if (e.key === 'Enter') { this._submitSaveDialog(); }
                if (e.key === 'Escape') { this._closeSaveDialog(); }
            },
        });
        row.appendChild(input);
        card.appendChild(row);
        const fmtRow = el('div', { className: 'modal-row' });
        fmtRow.appendChild(el('label', null, 'Format'));
        const fmtSel = el('select', { id: 'save-format' });
        for (const f of ['wav', 'flac', 'ogg', 'mp3']) {
            const o = el('option', { value: f }, f.toUpperCase());
            if (f === fmt) o.selected = 'selected';
            fmtSel.appendChild(o);
        }
        fmtRow.appendChild(fmtSel);
        card.appendChild(fmtRow);
        const actions = el('div', { className: 'modal-actions' });
        actions.appendChild(el('button', { className: 'btn',
            onClick: () => this._closeSaveDialog() }, 'Cancel'));
        actions.appendChild(el('button', { className: 'btn btn-primary',
            onClick: () => this._submitSaveDialog() }, 'Save'));
        card.appendChild(actions);
        back.appendChild(card);
        document.body.appendChild(back);
        this._saveDialog = back;
        setTimeout(() => input.focus(), 50);
    },

    _closeSaveDialog() {
        if (this._saveDialog) {
            this._saveDialog.remove();
            this._saveDialog = null;
        }
    },

    _defaultSaveName() {
        const src = this._activeSource;
        if (!src) return 'edit';
        let base = (src.name || src.label || 'edit')
            .replace(/\.[a-z0-9]+$/i, '')
            .replace(/_v\d{2,3}$/, '')
            .slice(0, 40);
        return base + '_edit';
    },

    async _submitSaveDialog() {
        const raw = document.getElementById('save-name-input')?.value.trim();
        const fmt = document.getElementById('save-format')?.value || 'wav';
        if (!raw) { App.toast('Enter a name', 'error'); return; }
        // Strip a trailing audio extension so we don't end up with foo.wav.wav
        const name = raw.replace(/\.(wav|mp3|flac|ogg|m4a)$/i, '');
        if (!name) { App.toast('Enter a name', 'error'); return; }
        // Apply chosen format to top selector so render uses it
        const top = document.getElementById('render-format');
        if (top) top.value = fmt;
        this._closeSaveDialog();
        await this._render(name);
    },

    _filenameForSource(src) {
        if (!src) return 'audio.wav';
        if (src.name) return src.name;
        if (src.kind === 'chunk' && Number.isInteger(src.index)) {
            return `chunk_${String(src.index).padStart(3, '0')}.wav`;
        }
        return 'audio.wav';
    },

    _download() {
        if (!this._activeSource) return;
        this._downloadSource(this._activeSource);
    },

    async _downloadSource(src) {
        const path = this._audioUrlForSource(src);
        if (!path) return;
        // Fetch with the X-TTS-API-Token header and download a blob: URL so the
        // token is never placed in the <a href> (history/access logs).
        try {
            const url = await App.fetchBlobUrl(path);
            const a = document.createElement('a');
            a.href = url;
            a.download = this._filenameForSource(src);
            a.click();
            // Revoke after the click has had a chance to start the download.
            setTimeout(() => { try { URL.revokeObjectURL(url); } catch {} }, 10000);
        } catch (e) {
            App.toast(`Download failed: ${e.message}`, 'error');
        }
    },
};
