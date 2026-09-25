/* ==========================================================================
   Tab: Testing — TTS inference, playback, and response history
   ========================================================================== */

const TabTesting = {
    _selectedModel: null,
    _voices: [],
    _history: [],  // { id, model, audio_url, duration, gen_time, sr, status, text }
    _voiceGen: 0,

    // Common speech style tags users can insert
    STYLE_TAGS: [
        { label: '— Emotions —',       value: '', disabled: true },
        { label: 'Happy / Excited',     value: '(happily) ' },
        { label: 'Sad / Somber',        value: '(sadly) ' },
        { label: 'Angry / Frustrated',  value: '(angrily) ' },
        { label: 'Surprised',           value: '(with surprise) ' },
        { label: 'Sarcastic',           value: '(sarcastically) ' },
        { label: 'Nervous / Anxious',   value: '(nervously) ' },
        { label: '— Delivery —',        value: '', disabled: true },
        { label: 'Whispering',          value: '(whispering) ' },
        { label: 'Shouting',            value: '(shouting) ' },
        { label: 'Laughing',            value: '(laughing) ' },
        { label: 'Sighing',             value: '(sighs) ' },
        { label: 'Dramatic pause',      value: '... ' },
        { label: 'Cheerful narration',  value: '(cheerfully) ' },
        { label: 'Calm / Soothing',     value: '(calmly) ' },
        { label: 'Serious / Grave',     value: '(in a serious tone) ' },
        { label: '— Pacing —',          value: '', disabled: true },
        { label: 'Speaking slowly',     value: '(speaking slowly) ' },
        { label: 'Speaking quickly',    value: '(speaking quickly) ' },
    ],

    // Engines use incompatible inline-control dialects. Prefer a compact,
    // model-specific list over a generic cue that may be spoken literally.
    MODEL_STYLE_TAGS: {
        bark: [
            { label: 'Laugh', value: '[laughs] ' },
            { label: 'Sigh', value: '[sighs] ' },
            { label: 'Gasp', value: '[gasps] ' },
            { label: 'Clear throat', value: '[clears throat] ' },
            { label: 'Music', value: '[music] ' },
        ],
        dia: [
            { label: 'Laugh', value: '(laughs) ' },
            { label: 'Sigh', value: '(sighs) ' },
            { label: 'Clear throat', value: '(clears throat) ' },
            { label: 'Cough', value: '(coughs) ' },
            { label: 'Gasp', value: '(gasps) ' },
        ],
        fish: [
            { label: 'Excited', value: '(excited) ' },
            { label: 'Sad', value: '(sad) ' },
            { label: 'Angry', value: '(angry) ' },
            { label: 'Whispering', value: '(whispering) ' },
            { label: 'Shouting', value: '(shouting) ' },
            { label: 'Laughing', value: '(laughing) ' },
            { label: 'Sighing', value: '(sighing) ' },
        ],
        voxcpm2: [
            { label: 'Warm calm woman', value: '(young woman, warm and calm) ' },
            { label: 'Deep steady man', value: '(mature man, deep and steady) ' },
            { label: 'Soft intimate', value: '(soft intimate voice, gentle) ' },
            { label: 'Bright energetic', value: '(young energetic speaker, bright and excited) ' },
        ],
        orpheus: [
            { label: 'Laugh', value: '<laugh> ' },
            { label: 'Sigh', value: '<sigh> ' },
            { label: 'Chuckle', value: '<chuckle> ' },
            { label: 'Cough', value: '<cough> ' },
            { label: 'Sniffle', value: '<sniffle> ' },
            { label: 'Groan', value: '<groan> ' },
            { label: 'Yawn', value: '<yawn> ' },
            { label: 'Gasp', value: '<gasp> ' },
        ],
    },

    // Per-model native params used if /api/config has no defaults for a model.
    // The app handles speed, chunking, and max-token controls separately.
    MODEL_PARAMS: {
        xtts:       ['temperature', 'repetition_penalty', 'top_k', 'top_p'],
        fish:       ['temperature', 'repetition_penalty', 'top_p', 'seed'],
        kokoro:     [],
        bark:       ['temperature', 'waveform_temperature'],
        chatterbox: ['temperature', 'repetition_penalty', 'exaggeration', 'cfg_weight'],
        f5:         ['nfe_step', 'cfg_scale', 'seed'],
        dia:        ['temperature', 'cfg_scale', 'top_p', 'top_k'],
        qwen:       ['temperature', 'top_p', 'top_k'],
        vibevoice:  ['cfg_scale'],
        higgs:      ['temperature', 'top_p', 'top_k'],
        speecht5:   [],
        parler:     ['temperature'],
        outetts:    ['temperature', 'repetition_penalty', 'top_p', 'top_k'],
        vits:       [],
        edge:       ['pitch', 'volume'],
    },

    // Models that cannot synthesize without a reference voice.
    VOICE_REQUIRED: new Set(['vibevoice', 'f5']),

    // Audio profile slider definitions: id -> [label, min, max, step, decimalPlaces]
    PROFILE_PARAMS: {
        inter_pause_sec:  ['Inter-chunk Pause (s)',     0,     2.0,   0.05, 2],
        front_pad_sec:    ['Front Pad (s)',             0,     1.0,   0.05, 2],
        padding_sec:      ['Start/End Pad (s)',         0,     2.0,   0.1,  1],
        trim_db:          ['Silence Threshold (dB)',   -60,   -10,    1,    0],
        min_silence_ms:   ['Min Silence (ms)',          100,   2000,  50,   0],
        front_protect_ms: ['Front Protect (ms)',        0,     500,   10,   0],
        end_protect_ms:   ['End Protect (ms)',          0,     2000,  50,   0],
        clipping:         ['Peak Limit',                0.5,   1.0,   0.01, 2],
        lufs:             ['Target LUFS',              -40,   -10,    0.5,  1],
    },

    init() {
        this._buildUI();
        // Revoke all retained history blob URLs on navigation/unload. pagehide
        // (not beforeunload) keeps bfcache compatibility. Bound once.
        if (!this._unloadBound) {
            this._unloadBound = true;
            window.addEventListener('pagehide', () => {
                for (const h of this._history) {
                    if (h.audio_url) {
                        try { URL.revokeObjectURL(h.audio_url); } catch {}
                    }
                }
            });
        }
        // Pre-load saved voices so the ref-row is usable before a worker loads
        this._loadSavedVoices().then(() => {
            if (this._hasSavedVoices && !this._selectedModel) {
                const refRow = document.getElementById('ref-row');
                if (refRow) refRow.style.display = '';
            }
        });
    },

    _buildUI() {
        const container = document.getElementById('tab-testing');
        container.innerHTML = '';

        // Worker selector row
        const workerRow = el('div', { className: 'form-row mb-16' },
            el('div', { className: 'form-group' },
                el('label', null, 'Worker'),
                el('select', { id: 'test-worker', onChange: () => this._onWorkerChange() }),
            ),
            el('div', { className: 'form-group' },
                el('label', null, 'Voice'),
                el('select', { id: 'test-voice' }),
            ),
        );
        container.appendChild(workerRow);

        // Reference audio row (for models that support voice cloning)
        const refRow = el('div', { className: 'form-row mb-16', id: 'ref-row', style: { display: 'none' } },
            el('div', { className: 'form-group', style: { flex: '1' } },
                el('label', null, 'Saved Voice'),
                el('select', { id: 'test-ref-voice', onChange: () => {
                    // Clear file picker when a saved voice is chosen
                    if (document.getElementById('test-ref-voice')?.value) {
                        const fp = document.getElementById('test-ref-audio');
                        if (fp) fp.value = '';
                    }
                } }),
            ),
            el('div', { className: 'form-group', style: { flex: '1' } },
                el('label', null, 'Or Upload'),
                el('input', { type: 'file', id: 'test-ref-audio', accept: 'audio/*', onChange: () => {
                    // Clear saved voice when a file is uploaded
                    if (document.getElementById('test-ref-audio')?.files?.length) {
                        const sv = document.getElementById('test-ref-voice');
                        if (sv) sv.value = '';
                    }
                } }),
            ),
            el('div', { className: 'form-group', id: 'ref-text-group', style: { flex: '1', display: 'none' } },
                el('label', null, 'Reference Text'),
                el('input', { type: 'text', id: 'test-ref-text', placeholder: 'Transcript of reference audio' }),
            ),
        );
        container.appendChild(refRow);
        const speakers = el('div', { id: 'vibe-speakers', className: 'form-row mb-16', style: { display: 'none' } });
        for (let i = 2; i <= 4; i++) {
            speakers.appendChild(el('div', { className: 'form-group' },
                el('label', null, 'Speaker ' + i + ' saved voice'),
                el('select', { id: 'test-speaker-' + i })));
        }
        container.appendChild(speakers);

        // Text input
        container.appendChild(
            el('div', { className: 'form-group mb-16' },
                el('label', null, 'Text'),
                el('textarea', {
                    id: 'test-text',
                    rows: '4',
                    placeholder: 'Enter text to synthesize...',
                }, 'Hello, this is a test of the text to speech system.'),
            )
        );

        // Style tag helper (shown for models that support text-based style cues)
        const styleSelect = el('select', { id: 'test-style-tag', style: { flex: '1' } });
        styleSelect.appendChild(el('option', { value: '' }, 'Insert style tag...'));
        for (const tag of this.STYLE_TAGS) {
            const opt = el('option', { value: tag.value }, tag.label);
            if (tag.disabled) opt.disabled = true;
            styleSelect.appendChild(opt);
        }
        const styleRow = el('div', {
            className: 'form-row mb-16',
            id: 'style-row',
            style: { display: 'none', alignItems: 'center', gap: '8px' },
        },
            styleSelect,
            el('button', {
                className: 'btn btn-sm',
                onClick: () => {
                    const sel = document.getElementById('test-style-tag');
                    const textarea = document.getElementById('test-text');
                    if (sel.value && textarea) {
                        if (this._selectedModel === 'voxcpm2') {
                            // VoxCPM2 voice design is a leading instruction,
                            // not an inline effect. Replace an existing design
                            // and keep the instruction at the start.
                            const rest = textarea.value.replace(
                                /^\s*\([^\r\n)]{1,500}\)\s*/, '',
                            );
                            textarea.value = sel.value + rest;
                            textarea.focus();
                            textarea.setSelectionRange(sel.value.length, sel.value.length);
                            sel.value = '';
                            return;
                        }
                        const start = textarea.selectionStart;
                        const before = textarea.value.substring(0, start);
                        const after = textarea.value.substring(start);
                        textarea.value = before + sel.value + after;
                        textarea.focus();
                        const newPos = start + sel.value.length;
                        textarea.setSelectionRange(newPos, newPos);
                        sel.value = '';
                    }
                },
            }, 'Insert'),
            el('span', { className: 'text-muted text-sm' },
                'Add emotion/style cues — model interprets these from the text'),
        );
        container.appendChild(styleRow);

        // Parameters panel
        const paramPanel = el('div', { className: 'panel mb-16' },
            el('span', { className: 'section-title' }, 'Parameters'),
            el('div', { className: 'param-grid', id: 'param-grid' }),
        );
        container.appendChild(paramPanel);

        // Post-processing panel
        const ppPanel = el('div', { className: 'panel mb-16' },
            el('span', { className: 'section-title' }, 'Post-Processing'),
            el('div', { className: 'param-grid', id: 'pp-grid' },
                this._rangeParam('speed', 'Speed', 0.5, 2.0, 0.1, 1.0),
                this._rangeParam('de_reverb', 'De-reverb', 0, 1, 0.1, 0),
                this._rangeParam('de_ess', 'De-ess', 0, 1, 0.1, 0),
                el('div', { className: 'param-group' },
                    el('label', null, 'Format'),
                    el('select', { id: 'test-format' },
                        el('option', { value: 'wav' }, 'WAV'),
                        el('option', { value: 'mp3' }, 'MP3'),
                        el('option', { value: 'ogg' }, 'OGG'),
                        el('option', { value: 'flac' }, 'FLAC'),
                    ),
                ),
                el('div', { className: 'param-group' },
                    el('label', null, 'Retries'),
                    el('input', { type: 'number', id: 'test-retries',
                        min: '0', max: '10', value: '3',
                        style: { width: '60px' } }),
                ),
                el('div', { className: 'param-group' },
                    el('div', { className: 'toggle-wrap' },
                        el('label', { className: 'toggle' },
                            el('input', { type: 'checkbox', id: 'test-skip-pp' }),
                            el('span', { className: 'toggle-slider' }),
                        ),
                        el('span', { className: 'text-sm' }, 'Skip post-processing'),
                    ),
                ),
            ),
        );
        container.appendChild(ppPanel);

        // Whisper verification panel
        const whisperPanel = el('div', { className: 'panel mb-16' },
            el('span', { className: 'section-title' }, 'Whisper Verification'),
            el('div', { className: 'form-row' },
                el('div', { className: 'param-group' },
                    el('div', { className: 'toggle-wrap' },
                        el('label', { className: 'toggle' },
                            el('input', { type: 'checkbox', id: 'test-whisper' }),
                            el('span', { className: 'toggle-slider' }),
                        ),
                        el('span', { className: 'text-sm' }, 'Enable verification'),
                    ),
                ),
                el('div', { className: 'param-group' },
                    el('label', null, 'Model'),
                    el('select', { id: 'test-whisper-model' },
                        el('option', { value: 'tiny' }, 'tiny (39M)'),
                        el('option', { value: 'base', selected: '' }, 'base (74M)'),
                        el('option', { value: 'small' }, 'small (244M)'),
                        el('option', { value: 'medium' }, 'medium (769M)'),
                        el('option', { value: 'large' }, 'large (1.5B)'),
                    ),
                ),
                this._rangeParam('tolerance', 'Tolerance %', 0, 100, 5, 80),
            ),
        );
        container.appendChild(whisperPanel);

        // Audio Profile panel (collapsible)
        const profileToggle = el('span', { className: 'section-title', style: { cursor: 'pointer' } },
            'Audio Profile \u25B6');
        const profileGrid = el('div', { className: 'param-grid', id: 'profile-grid',
            style: { display: 'none' } });
        profileToggle.addEventListener('click', () => {
            const open = profileGrid.style.display !== 'none';
            profileGrid.style.display = open ? 'none' : '';
            profileToggle.textContent = open ? 'Audio Profile \u25B6' : 'Audio Profile \u25BC';
        });
        const profilePanel = el('div', { className: 'panel mb-16' },
            profileToggle,
            el('div', { className: 'text-muted text-sm', style: { marginBottom: '8px' } },
                'Timing, spacing, silence trimming, and loudness settings per model.'),
            profileGrid,
        );
        container.appendChild(profilePanel);

        // Generate button
        container.appendChild(
            el('div', { className: 'toolbar mb-16' },
                el('button', {
                    className: 'btn btn-primary',
                    id: 'test-generate',
                    onClick: () => this.generate(),
                }, 'Generate'),
                el('span', { id: 'gen-status', className: 'text-muted' }),
            )
        );

        // Response history
        const histPanel = el('div', { className: 'panel' },
            el('div', { className: 'panel-header' },
                el('span', { className: 'panel-title' }, 'Response History'),
                el('button', { className: 'btn btn-sm', onClick: () => this.clearHistory() }, 'Clear'),
            ),
            el('div', { className: 'table-wrap', id: 'history-wrap' }),
        );
        container.appendChild(histPanel);

        this.updateWorkerList();
    },

    _decimalsForFormat(fmt, step) {
        if (fmt === 'd') return 0;
        const match = String(fmt || '').match(/\.(\d+)f/);
        if (match) return parseInt(match[1], 10);
        return step < 1 ? 1 : 0;
    },

    _rangeParam(id, label, min, max, step, value, decimals, tooltip) {
        // If decimals not explicitly provided, infer from step size
        const dp = decimals != null ? decimals : (step < 1 ? 1 : 0);
        const inputAttrs = {
            type: 'range', id: `test-${id}`,
            min: String(min), max: String(max), step: String(step), value: String(value),
        };
        if (tooltip) inputAttrs.title = tooltip;
        const input = el('input', inputAttrs);
        // Use input.value (not raw `value`) so the displayed number matches the
        // browser's step-snapped slider position — otherwise a default like 1.3
        // with step 0.5 shows "1.3" while the slider sits at 1.5.
        const valSpan = el('span', { className: 'param-value', id: `val-${id}` },
            parseFloat(input.value).toFixed(dp));
        input.addEventListener('input', () => {
            valSpan.textContent = parseFloat(input.value).toFixed(dp);
        });
        const groupAttrs = { className: 'param-group' };
        if (tooltip) groupAttrs.title = tooltip;
        return el('div', groupAttrs,
            el('label', { for: `test-${id}` }, label),
            el('div', { className: 'range-row' }, input, valSpan),
        );
    },

    _fieldParam(id, cfg) {
        const label = cfg.label || id;
        const tooltip = App.state.tooltips?.[id] || cfg.tooltip || '';
        const attrs = { id: `test-${id}` };
        if (tooltip) attrs.title = tooltip;
        let input;
        if (cfg.type === 'select') {
            input = el('select', attrs);
            for (const opt of (cfg.options || [])) {
                const value = Array.isArray(opt) ? opt[0] : opt;
                const text = Array.isArray(opt) ? opt[1] : opt;
                input.appendChild(el('option', { value }, text));
            }
            input.value = cfg.default || '';
        } else {
            input = el('input', {
                ...attrs,
                type: 'text',
                value: cfg.default || '',
                placeholder: cfg.placeholder || '',
                style: { minWidth: '260px', width: '100%' },
            });
        }
        const groupAttrs = { className: 'param-group' };
        if (tooltip) groupAttrs.title = tooltip;
        return el('div', groupAttrs,
            el('label', { for: `test-${id}` }, label),
            input,
        );
    },

    updateWorkerList() {
        const sel = document.getElementById('test-worker');
        if (!sel) return;

        // Compare on what we actually show: ready/busy workers with their status
        const ready = App.state.workers.filter(w => w.status === 'ready' || w.status === 'busy');
        const sig = ready.map(w => `${w.worker_id}:${w.status}`).join(',');
        if (sig === (this._lastWorkerSig || '')) return;
        this._lastWorkerSig = sig;

        const prev = sel.value;
        const prevModel = this._selectedModel;
        sel.innerHTML = '';
        if (ready.length === 0) {
            sel.appendChild(el('option', { value: '' }, '-- No workers loaded --'));
        }
        for (const w of ready) {
            const label = `${w.model} (${w.device}${w.status === 'busy' ? ', busy' : ''})`;
            sel.appendChild(el('option', { value: w.worker_id }, label));
        }

        // Restore previous selection, or find another worker for the same model.
        // Round-trip through sel.value (no CSS-selector escaping needed): the
        // browser only accepts the assignment if a matching <option> exists, so
        // sel.value === prev afterwards iff the option is present.
        if (prev) sel.value = prev;
        if (!prev || sel.value !== prev) {
            if (prevModel) {
                const sameModel = ready.find(w => w.model === prevModel);
                if (sameModel) sel.value = sameModel.worker_id;
            }
        }

        this._onWorkerChange();
    },

    async _onWorkerChange() {
        const sel = document.getElementById('test-worker');
        const worker = App.state.workers.find(w => w.worker_id === sel?.value);
        const model = worker?.model || null;

        // Don't rebuild if model hasn't changed — preserves slider/voice values.
        // Also don't rebuild if model went null temporarily (worker flickered).
        if (model === this._selectedModel) return;
        if (model === null && this._selectedModel) return;  // keep params during transient loss
        this._selectedModel = model;

        // Generation counter: discard stale voice-fetch responses
        const gen = ++this._voiceGen;

        // Show/hide reference audio & style tags based on model capability flags
        const modelInfo = model && App.state.models?.[model];
        const refRow = document.getElementById('ref-row');
        const refTextGroup = document.getElementById('ref-text-group');
        const speakers = document.getElementById('vibe-speakers');
        if (speakers) speakers.style.display = model === 'vibevoice' ? '' : 'none';
        if (modelInfo?.ref_audio) {
            refRow.style.display = '';
            refTextGroup.style.display = modelInfo.ref_text ? '' : 'none';
            this._loadSavedVoices();
        } else {
            refRow.style.display = 'none';
            refTextGroup.style.display = 'none';
        }

        // Show/hide style tag helper
        const styleRow = document.getElementById('style-row');
        if (styleRow) {
            styleRow.style.display = modelInfo?.style_tags ? '' : 'none';
            this._buildStyleTags(model);
        }

        // Build parameter grid and audio profile (only when model actually changes)
        this._buildParams(model);
        this._buildProfileGrid(model);

        // Fetch voices (only when model actually changes)
        const prevVoice = document.getElementById('test-voice')?.value;
        if (model) {
            try {
                const data = await App.api('GET', `/api/tts/${model}/voices`);
                if (gen !== this._voiceGen) return;  // stale response, discard
                this._voices = data.voices || [];
            } catch {
                if (gen !== this._voiceGen) return;
                this._voices = [];
            }
        } else {
            this._voices = [];
        }

        const voiceSel = document.getElementById('test-voice');
        voiceSel.innerHTML = '';
        if (this._voices.length === 0) {
            voiceSel.appendChild(el('option', { value: '' }, '-- None --'));
        }
        for (const v of this._voices) {
            const name = typeof v === 'string' ? v : v.name || v;
            voiceSel.appendChild(el('option', { value: name }, name));
        }
        // Restore previous voice selection if still available, without using a
        // CSS attribute selector (a voice filename may contain quotes/brackets
        // that make the selector invalid). Round-trip through value/index: only
        // restore if the option is actually present, otherwise leave the
        // browser's default (first option) selected.
        if (prevVoice) {
            const prevIdx = voiceSel.selectedIndex;
            voiceSel.value = prevVoice;
            if (voiceSel.value !== prevVoice) voiceSel.selectedIndex = prevIdx;
        }
    },

    async _loadSavedVoices() {
        const sel = document.getElementById('test-ref-voice');
        if (!sel) return;
        const prev = sel.value;
        try {
            const data = await App.api('GET', '/api/voices');
            const voices = data.voices || [];
            this._hasSavedVoices = voices.length > 0;
            for (let i = 2; i <= 4; i++) {
                const speaker = document.getElementById('test-speaker-' + i);
                if (!speaker) continue;
                const selected = speaker.value;
                speaker.replaceChildren(el('option', { value: '' }, '-- None --'));
                for (const v of voices) speaker.appendChild(el('option', { value: v.filename }, v.filename));
                speaker.value = selected;
            }
            sel.innerHTML = '';
            sel.appendChild(el('option', { value: '' }, '-- None (upload or manage in Voices tab) --'));
            for (const v of voices) {
                const filename = v.filename || (v.name + '.' + v.format);
                sel.appendChild(el('option', { value: filename }, v.name));
            }
            // Restore previous saved-voice selection without a CSS attribute
            // selector — `prev` is a user-controlled voice filename that may
            // contain quotes/brackets which would make the selector invalid and
            // throw, aborting this function and dropping the just-built list.
            if (prev) {
                const prevIdx = sel.selectedIndex;
                sel.value = prev;
                if (sel.value !== prev) sel.selectedIndex = prevIdx;
            }
        } catch {
            this._hasSavedVoices = false;
            sel.innerHTML = '';
            sel.appendChild(el('option', { value: '' }, '-- None --'));
        }
    },

    _buildParams(model) {
        const grid = document.getElementById('param-grid');
        grid.innerHTML = '';

        if (!model) return;

        // Prefer dynamic param list from server config; fall back to hardcoded map
        const serverDefaults = App.state.defaults?.[model];
        const params = serverDefaults
            ? Object.keys(serverDefaults)
            : (this.MODEL_PARAMS[model] || []);
        const defaults = serverDefaults || {};
        const cfgs = App.state.params;

        const overrides = App.state.paramOverrides?.[model] || {};
        for (const paramId of params) {
            const cfg = cfgs[paramId];
            if (!cfg) continue;
            let [label, min, max, step, fmt] = cfg;
            const ov = overrides[paramId];
            if (ov) [min, max, step] = ov;
            let defVal = defaults[paramId] ?? ((min + max) / 2);
            if (defVal > max) defVal = max;
            if (defVal < min) defVal = min;
            const decimals = this._decimalsForFormat(fmt, step);
            grid.appendChild(this._rangeParam(
                paramId, label, min, max, step, defVal, decimals,
                App.state.tooltips?.[paramId],
            ));
        }

        const fields = App.state.fields?.[model] || {};
        for (const [fieldId, fieldCfg] of Object.entries(fields)) {
            grid.appendChild(this._fieldParam(fieldId, fieldCfg));
        }
    },

    _buildStyleTags(model) {
        const select = document.getElementById('test-style-tag');
        if (!select) return;
        select.innerHTML = '';
        select.appendChild(el('option', { value: '' }, 'Insert style tag...'));
        const tags = this.MODEL_STYLE_TAGS[model] || this.STYLE_TAGS;
        for (const tag of tags) {
            const option = el('option', { value: tag.value }, tag.label);
            if (tag.disabled) option.disabled = true;
            select.appendChild(option);
        }
    },

    _buildProfileGrid(model) {
        const grid = document.getElementById('profile-grid');
        if (!grid) return;
        grid.innerHTML = '';

        if (!model) return;

        const profile = App.state.profiles[model];
        if (!profile) return;

        for (const [id, cfg] of Object.entries(this.PROFILE_PARAMS)) {
            const [label, min, max, step, decimals] = cfg;
            const defVal = profile[id] ?? ((min + max) / 2);
            grid.appendChild(this._rangeParam(`prof-${id}`, label, min, max, step, defVal, decimals));
        }
    },

    async generate() {
        const btn = document.getElementById('test-generate');
        const statusEl = document.getElementById('gen-status');
        const worker = App.state.workers.find(w =>
            w.worker_id === document.getElementById('test-worker')?.value);
        if (!worker) { App.toast('Select a worker first', 'error'); return; }

        const model = worker.model;
        const text = document.getElementById('test-text')?.value?.trim();
        if (!text) { App.toast('Enter text to synthesize', 'error'); return; }

        // Voice — native voice from dropdown, or saved reference voice
        const voice = document.getElementById('test-voice')?.value;
        const refVoice = App.state.models?.[model]?.ref_audio ? document.getElementById('test-ref-voice')?.value : '';
        const refFileEarly = document.getElementById('test-ref-audio')?.files?.[0];
        if (this.VOICE_REQUIRED.has(model) && !voice && !refVoice && !refFileEarly) {
            App.toast(`${model} needs a reference voice — pick one from "Saved Voice" or upload an audio file`, 'error');
            return;
        }

        btn.disabled = true;
        statusEl.innerHTML = '<span class="spinner"></span> Generating...';

        const body = { text };
        if (voice) body.voice = voice;
        if (refVoice) body.voice = refVoice;
        if (model === 'vibevoice') {
            const refs = [refVoice, ...[2, 3, 4].map(i => document.getElementById('test-speaker-' + i)?.value)];
            if (refs.slice(1).some(Boolean)) {
                if (!refs[0] || refs.some((v, i) => !v && refs.slice(i + 1).some(Boolean))) {
                    btn.disabled = false;
                    statusEl.textContent = '';
                    App.toast('Choose saved voices in speaker order, starting with Speaker 1.', 'error');
                    return;
                }
                body.reference_audios = refs.filter(Boolean);
                delete body.voice;
            }
        }

        // Parameters — prefer dynamic list from server config
        const serverDefaults = App.state.defaults?.[model];
        const params = serverDefaults
            ? Object.keys(serverDefaults)
            : (this.MODEL_PARAMS[model] || []);
        for (const p of params) {
            const input = document.getElementById(`test-${p}`);
            if (input) {
                const val = parseFloat(input.value);
                if (!isNaN(val)) body[p] = val;
            }
        }

        const fields = App.state.fields?.[model] || {};
        for (const fieldId of Object.keys(fields)) {
            const input = document.getElementById(`test-${fieldId}`);
            const val = input?.value?.trim?.() ?? input?.value;
            if (val) body[fieldId] = val;
        }

        // Post-processing (speed, de-reverb, de-ess are app-level, not model-level)
        body.speed = parseFloat(document.getElementById('test-speed')?.value || '1.0');
        body.de_reverb = parseFloat(document.getElementById('test-de_reverb')?.value || '0');
        body.de_ess = parseFloat(document.getElementById('test-de_ess')?.value || '0');
        body.output_format = document.getElementById('test-format')?.value || 'wav';
        body.skip_post_process = document.getElementById('test-skip-pp')?.checked || false;
        const retries = parseInt(document.getElementById('test-retries')?.value || '3', 10);
        if (!isNaN(retries)) body.auto_retry = retries;

        // Audio profile overrides (only send values the user actually changed)
        const profile = App.state.profiles[model] || {};
        for (const id of Object.keys(this.PROFILE_PARAMS)) {
            const input = document.getElementById(`test-prof-${id}`);
            if (input) {
                const val = parseFloat(input.value);
                if (!isNaN(val) && val !== profile[id]) {
                    body[id] = val;
                }
            }
        }

        // Whisper
        if (document.getElementById('test-whisper')?.checked) {
            body.verify_whisper = true;
            body.whisper_model = document.getElementById('test-whisper-model')?.value || 'base';
            body.tolerance = parseFloat(document.getElementById('test-tolerance')?.value || '80');
        }

        // Reference audio
        const refFile = App.state.models?.[model]?.ref_audio ? document.getElementById('test-ref-audio')?.files?.[0] : null;
        const refText = document.getElementById('test-ref-text')?.value?.trim();
        if (refText) body.reference_text = refText;

        // Specify device/worker
        body.device = worker.device;
        body.worker_id = worker.worker_id;

        const startTime = Date.now();

        try {
            let resp;
            if (refFile) {
                // Multipart upload — avoids base64 bloat. Send ALL params.
                const form = new FormData();
                form.append('reference_audio', refFile);
                for (const [k, v] of Object.entries(body)) {
                    if (v != null && v !== '') form.append(k, String(v));
                }
                resp = await fetch(`/api/tts/${model}/upload`, {
                    method: 'POST',
                    headers: App.authHeaders(),
                    body: form,
                });
            } else {
                resp = await fetch(`/api/tts/${model}`, {
                    method: 'POST',
                    headers: App.authHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify(body),
                });
            }

            const genTime = ((Date.now() - startTime) / 1000).toFixed(1);

            // Parse response — could be success JSON or error JSON
            const result = await resp.json().catch(() => null);

            if (!resp.ok || result?.error) {
                // Extract a readable error message from the response
                const reason = result?.reason || result?.error || result?.detail
                    || `HTTP ${resp.status}`;
                throw new Error(typeof reason === 'object' ? reason.message || JSON.stringify(reason) : reason);
            }

            if (!result) throw new Error('Server returned invalid response');
            const audioData = result.audio_base64 || result.audio;
            const sr = result.sample_rate || 24000;
            const duration = result.duration || result.duration_sec || 0;

            // Create blob URL for playback
            let audioUrl = null;
            if (audioData) {
                const binaryStr = atob(audioData);
                const bytes = new Uint8Array(binaryStr.length);
                for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);
                const fmt = result.format || body.output_format || 'wav';
                const mimeMap = { wav: 'audio/wav', mp3: 'audio/mpeg', ogg: 'audio/ogg', flac: 'audio/flac', m4a: 'audio/mp4' };
                const blob = new Blob([bytes], { type: mimeMap[fmt] || 'audio/wav' });
                audioUrl = URL.createObjectURL(blob);
            } else if (result.output_url) {
                audioUrl = await App.fetchBlobUrl(result.output_url);
            }

            this._history.unshift({
                id: Date.now(),
                model, text,
                audio_url: audioUrl,
                format: body.output_format || 'wav',
                duration: typeof duration === 'number' ? duration.toFixed(1) : duration,
                gen_time: genTime,
                sr, status: 'completed',
            });
            this._trimHistory();

            statusEl.textContent = `Done in ${genTime}s`;
            App.toast(`Generated ${duration?.toFixed?.(1) || '?'}s of audio`, 'success');
        } catch (e) {
            const genTime = ((Date.now() - startTime) / 1000).toFixed(1);
            this._history.unshift({
                id: Date.now(), model, text,
                audio_url: null, format: body.output_format || 'wav',
                duration: '-', gen_time: genTime,
                sr: '-', status: 'error', error: e.message,
            });
            this._trimHistory();
            statusEl.textContent = 'Failed';
            App.toast(`Generation failed: ${e.message}`, 'error');
        } finally {
            // Always re-enable the button and refresh history, even if the catch
            // handler itself throws (e.g. App.toast on a missing container) —
            // otherwise the button stays disabled and the spinner stuck.
            btn.disabled = false;
            this._renderHistory();
        }
    },

    _renderHistory() {
        const wrap = document.getElementById('history-wrap');
        if (!wrap) return;

        if (this._history.length === 0) {
            wrap.innerHTML = '<div class="text-muted text-sm" style="padding:12px">No responses yet.</div>';
            return;
        }

        const table = el('table');
        table.innerHTML = `<thead><tr>
            <th>#</th><th>Model</th><th>Text</th><th>Duration</th><th>Time</th><th>Status</th><th>Actions</th>
        </tr></thead>`;
        const tbody = el('tbody');

        this._history.forEach((h, i) => {
            // Text cell: show full text, wrap it
            const textCell = el('td', {
                className: 'text-sm',
                style: { maxWidth: '320px', whiteSpace: 'pre-wrap', wordBreak: 'break-word' },
            }, h.text || '');

            // If error, append the error reason below
            if (h.error) {
                textCell.appendChild(el('div', {
                    style: { color: 'var(--accent-red-text)', marginTop: '4px', fontSize: '11px' },
                }, `Error: ${h.error}`));
            }

            const row = el('tr', null,
                el('td', { className: 'mono text-sm' }, String(i + 1)),
                el('td', null, h.model),
                textCell,
                el('td', { className: 'mono' }, `${h.duration}s`),
                el('td', { className: 'mono' }, `${h.gen_time}s`),
                el('td', null, statusBadge(h.status)),
                el('td', null,
                    el('div', { className: 'audio-controls' },
                        h.audio_url ? el('button', {
                            className: 'btn btn-sm',
                            onClick: () => App.playAudio(h.audio_url),
                        }, 'Play') : null,
                        h.audio_url ? el('button', {
                            className: 'btn btn-sm',
                            onClick: () => App.stopAudio(),
                        }, 'Stop') : null,
                        h.audio_url ? el('button', {
                            className: 'btn btn-sm',
                            onClick: () => this._saveAudio(h),
                        }, 'Save') : null,
                    ),
                ),
            );
            tbody.appendChild(row);
        });

        table.appendChild(tbody);
        wrap.innerHTML = '';
        wrap.appendChild(table);
    },

    _saveAudio(entry) {
        if (!entry.audio_url) return;
        const a = document.createElement('a');
        a.href = entry.audio_url;
        const ext = entry.format || 'wav';
        a.download = `${entry.model}_${entry.id}.${ext}`;
        a.click();
    },

    _MAX_HISTORY: 100,

    _trimHistory() {
        while (this._history.length > this._MAX_HISTORY) {
            const old = this._history.pop();
            if (old && old.audio_url) URL.revokeObjectURL(old.audio_url);
        }
    },

    clearHistory() {
        for (const h of this._history) {
            if (h.audio_url) URL.revokeObjectURL(h.audio_url);
        }
        this._history = [];
        this._renderHistory();
    },
};
