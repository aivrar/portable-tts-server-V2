/* ==========================================================================
   Tab: Setup — Model installation and management
   ========================================================================== */

const TabSetup = {
    _tokenSaved: false,
    _tokenMasked: null,

    async init() {
        await this._checkToken();
        this.render();
    },

    async _checkToken() {
        try {
            const data = await App.api('GET', '/api/setup/hf-token');
            this._tokenSaved = data.saved;
            this._tokenMasked = data.masked;
        } catch {}
    },

    render() {
        const container = document.getElementById('tab-setup');
        const models = App.state.models;
        const statuses = App.state.setupStatus;
        const active = App.state.setupActiveInstalls || {};

        // Change detection — skip unnecessary DOM rebuilds that destroy user input
        const sig = JSON.stringify({ models, modelsDir: App.state.modelsDir, statuses, active, ts: this._tokenSaved, tm: this._tokenMasked });
        if (sig === this._lastSig) return;
        this._lastSig = sig;

        // Save HF token input value before clearing DOM
        const prevTokenVal = document.getElementById('hf-token-input')?.value || '';

        container.innerHTML = '';

        // HuggingFace token panel
        const tokenStatus = this._tokenSaved
            ? el('span', { className: 'badge badge-green' }, `Token: ${this._tokenMasked}`)
            : el('span', { className: 'badge badge-gray' }, 'No token set');
        const tokenPanel = el('div', { className: 'panel', style: { marginBottom: '12px' } },
            el('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' } },
                el('span', { className: 'text-sm', style: { fontWeight: 'bold' } }, 'HuggingFace Token'),
                tokenStatus,
                el('input', { type: 'password', id: 'hf-token-input',
                    placeholder: 'hf_...',
                    style: { width: '280px', fontSize: '13px', padding: '4px 8px',
                        background: 'var(--bg-tertiary)', color: 'var(--text-primary)',
                        border: '1px solid var(--border)', borderRadius: '4px' } }),
                el('button', { className: 'btn btn-sm btn-primary',
                    onClick: () => this._saveToken() }, 'Save Token'),
                el('span', { className: 'text-muted text-sm', title:
                    'Create a token at huggingface.co/settings/tokens.\n' +
                    'For fine-grained tokens: enable "Read access to contents of all public gated repos you can access" under Permissions.\n' +
                    'Classic Read tokens work out of the box.\n' +
                    'Also accept the model license on its HuggingFace page before downloading.' },
                    'Required for gated models (hover for help)'),
            ),
        );
        container.appendChild(tokenPanel);

        // Toolbar
        const toolbar = el('div', { className: 'toolbar' },
            el('button', { className: 'btn btn-primary', onClick: () => this.installAll() }, 'Install All'),
            el('button', { className: 'btn', onClick: () => App.loadSetupStatus() }, 'Refresh Status'),
            el('div', { className: 'toolbar-spacer' }),
            el('span', { className: 'text-muted' },
                `Models: ${Object.keys(models).length} | Dir: ${App.state.modelsDir || '...'}`
            ),
        );
        container.appendChild(toolbar);

        if (active.all || (active.models || []).length) {
            const activeModels = (active.models || []).join(', ') || 'preparing next model';
            container.appendChild(el('div', {
                className: 'panel',
                style: { marginBottom: '12px', borderColor: 'var(--accent)' },
            },
                el('strong', null, active.all ? 'Install-all is running' : 'Install running'),
                el('span', { className: 'text-muted', style: { marginLeft: '8px' } },
                    `Current: ${activeModels}`),
            ));
        }

        // Card grid
        const grid = el('div', { className: 'card-grid' });

        for (const [id, info] of Object.entries(models)) {
            const st = statuses[id] || {};
            const status = st.status || 'not_installed';

            const card = el('div', { className: 'card' },
                el('div', { className: 'card-header' },
                    el('h3', null, info.display || id),
                    statusBadge(status),
                ),
                el('p', null, info.desc || ''),
                el('div', { className: 'text-muted mb-12', style: { fontSize: '11px' } },
                    info.weights_size ? `Size: ${info.weights_size}` : 'Size: auto-download',
                    info.weights_repo ? ` | ${info.weights_repo}` : '',
                ),
                el('div', { className: 'card-actions' },
                    el('button', {
                        className: `btn btn-sm ${status === 'ready' || status === 'installing' ? '' : 'btn-primary'}`,
                        onClick: (e) => this.installModel(id, e.currentTarget),
                        ...(status === 'ready' || status === 'installing' ? { disabled: 'disabled' } : {}),
                    }, status === 'ready' ? 'Installed'
                        : status === 'installing' ? 'Installing...'
                        : status === 'packages_only' ? (st.weights_on_demand ? 'Packages Only (weights on first use)' : 'Install Weights')
                        : 'Install'),
                    el('button', {
                        className: 'btn btn-danger btn-sm',
                        onClick: () => this.removeModel(id),
                        ...(status === 'installing' ? { disabled: 'disabled' } : {}),
                    }, 'Remove'),
                ),
            );

            grid.appendChild(card);
        }

        container.appendChild(grid);

        // Restore HF token input value across renders
        if (prevTokenVal) {
            const tokenInput = document.getElementById('hf-token-input');
            if (tokenInput) tokenInput.value = prevTokenVal;
        }
    },

    async installModel(modelId, btn) {
        // Disable the clicked button immediately to prevent double-clicks
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Installing...';
        }
        App.toast(`Installing ${modelId} — check Log tab for progress`, 'info');
        // Switch to log tab so user can see progress
        document.querySelector('.tab-btn[data-tab="log"]')?.click();
        try {
            const result = await App.api('POST', `/api/setup/install/${modelId}`);
            if (result.status === 'busy') throw new Error(result.message || 'Another installation is active');
            if (result.status === 'already_installing') {
                App.toast(`${modelId} is already being installed`, 'info');
            } else {
                App.toast(`${modelId} install started — watch Log tab`, 'success');
            }
        } catch (e) {
            App.toast(`Error: ${e.message}`, 'error');
            // Re-enable button on failure so user can retry
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Install';
            }
        }
    },

    async installAll() {
        if (!confirm('Install all models? This may take a long time.')) return;
        App.toast('Installing all models sequentially — check Log tab', 'info');
        document.querySelector('.tab-btn[data-tab="log"]')?.click();
        try {
            const result = await App.api('POST', '/api/setup/install/all');
            if (result.status === 'busy') throw new Error(result.message || 'Another installation is active');
            if (result.status === 'already_installing') {
                App.toast(`Already installing: ${result.models?.join(', ')}`, 'info');
            } else {
                App.toast('All-model install started — watch Log tab', 'success');
            }
        } catch (e) {
            App.toast(`Error: ${e.message}`, 'error');
        }
    },

    async _saveToken() {
        const input = document.getElementById('hf-token-input');
        const token = input?.value?.trim() || '';
        const saveBtn = input?.closest('div')?.querySelector('.btn-primary');
        if (saveBtn) saveBtn.disabled = true;
        try {
            const result = await App.api('POST', '/api/setup/hf-token', { token });
            if (result.status === 'saved') {
                this._tokenSaved = true;
                this._tokenMasked = result.masked;
                App.toast('HuggingFace token saved', 'success');
            } else {
                this._tokenSaved = false;
                this._tokenMasked = null;
                App.toast('Token removed', 'info');
            }
            input.value = '';
            this.render();
        } catch (e) {
            App.toast(`Failed: ${e.message}`, 'error');
        } finally {
            if (saveBtn) saveBtn.disabled = false;
        }
    },

    async removeModel(modelId) {
        if (!confirm(`Remove ${modelId}? This deletes weights and override packages.`)) return;
        try {
            const result = await App.api('DELETE', `/api/setup/${modelId}`);
            App.toast(`${modelId} removed (${result.removed.length} items)`, 'success');
        } catch (e) {
            App.toast(`Error: ${e.message}`, 'error');
        }
        await App.loadSetupStatus();
    },
};
