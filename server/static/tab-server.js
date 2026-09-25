/* ==========================================================================
   Tab: Server — Gateway and worker management
   ========================================================================== */

const TabServer = {
    _initialized: false,

    init() {
        this._buildUI();
        this._initialized = true;
        this.render();
    },

    _buildUI() {
        const container = document.getElementById('tab-server');
        container.innerHTML = '';

        // Gateway panel (updated by render)
        container.appendChild(el('div', { id: 'srv-gateway', className: 'panel' }));

        // Spawn panel (built once, never rebuilt)
        const spawnPanel = el('div', { className: 'panel' },
            el('span', { className: 'panel-title mb-12' }, 'Spawn Worker'),
            el('div', { className: 'form-row', style: { marginTop: '12px' } },
                el('div', { className: 'form-group' },
                    el('label', null, 'Model'),
                    this._modelSelect(),
                ),
                el('div', { className: 'form-group' },
                    el('label', null, 'Device'),
                    this._deviceSelect(),
                ),
                el('div', { className: 'form-group' },
                    el('label', null, 'Precision'),
                    el('select', { id: 'spawn-precision' },
                        el('option', { value: '' }, 'Auto'),
                        el('option', { value: 'fp16' }, 'FP16 (half)'),
                        el('option', { value: 'bf16' }, 'BF16'),
                        el('option', { value: 'fp32' }, 'FP32 (full)'),
                    ),
                ),
                el('button', {
                    className: 'btn btn-primary',
                    id: 'spawn-btn',
                    onClick: () => this.spawnWorker(),
                }, 'Spawn Worker'),
            ),
        );
        container.appendChild(spawnPanel);

        // Workers table (updated by render)
        container.appendChild(el('div', { id: 'srv-workers', className: 'panel' }));
    },

    render() {
        if (!this._initialized) return;

        const workers = App.state.workers;

        // Change detection — skip unnecessary DOM rebuilds
        const sig = JSON.stringify(workers) + '|' + App.state.connected;
        if (sig === this._lastSig) return;
        this._lastSig = sig;

        // Update gateway panel
        const gw = document.getElementById('srv-gateway');
        if (gw) {
            const gatewayPort = App.state.gatewayPort || window.location.port || '?';
            const bridgePort = App.state.bridgePort || window.location.port || null;
            const portLabel = bridgePort && String(bridgePort) !== String(gatewayPort)
                ? `Gateway ${gatewayPort} | Bridge ${bridgePort}`
                : `Gateway ${gatewayPort}`;
            gw.innerHTML = '';
            gw.appendChild(el('div', { className: 'panel-header' },
                el('span', { className: 'panel-title' }, 'Gateway'),
                App.state.connected ? statusBadge('ready') : statusBadge('dead'),
            ));
            gw.appendChild(el('div', { className: 'text-muted text-sm' },
                `${portLabel} | Workers: ${workers.length} | `,
                `Models loaded: ${[...new Set(workers.filter(w => w.status === 'ready').map(w => w.model))].join(', ') || 'none'}`,
            ));
        }

        // Update workers table only
        const wp = document.getElementById('srv-workers');
        if (!wp) return;
        wp.innerHTML = '';

        wp.appendChild(el('div', { className: 'panel-header' },
            el('span', { className: 'panel-title' }, `Workers (${workers.length})`),
            el('button', { className: 'btn btn-danger btn-sm',
                onClick: () => this.killAll() }, 'Kill All'),
        ));

        if (workers.length === 0) {
            wp.appendChild(el('div', { className: 'text-muted text-sm' },
                'No workers running. Spawn one above.'));
        } else {
            const wrap = el('div', { className: 'table-wrap' });
            const table = el('table');
            table.innerHTML = `<thead><tr>
                <th>ID</th><th>Model</th><th>Port</th><th>Device</th><th>Status</th><th>VRAM</th><th>Actions</th>
            </tr></thead>`;
            const tbody = el('tbody');

            for (const w of workers) {
                const vram = w.vram_total_mb
                    ? `${w.vram_used_mb || 0}/${w.vram_total_mb}MB`
                    : '-';
                // Resolve GPU name from device list for clarity
                const devInfo = App.state.devices.find(d => d.id === w.device);
                const devLabel = devInfo?.name ? `${w.device} (${devInfo.name})` : (w.device || '');
                const row = el('tr', null,
                    el('td', { className: 'mono text-sm' }, w.worker_id || ''),
                    el('td', null, w.model || ''),
                    el('td', { className: 'mono' }, String(w.port || '')),
                    el('td', { className: 'mono text-sm' }, devLabel),
                    el('td', null, statusBadge(w.status || 'unknown')),
                    el('td', { className: 'mono text-sm' }, vram),
                    el('td', null,
                        el('button', { className: 'btn btn-danger btn-sm',
                            onClick: () => this.killWorker(w.worker_id) }, 'Kill'),
                    ),
                );
                tbody.appendChild(row);
            }
            table.appendChild(tbody);
            wrap.appendChild(table);
            wp.appendChild(wrap);
        }
    },

    _modelSelect() {
        const sel = el('select', { id: 'spawn-model' });
        for (const id of Object.keys(App.state.models)) {
            if (id === 'whisper') continue;
            const info = App.state.models[id];
            sel.appendChild(el('option', { value: id }, info.display || id));
        }
        return sel;
    },

    _deviceSelect() {
        const sel = el('select', { id: 'spawn-device' });
        const devices = App.state.devices;
        if (devices.length === 0) {
            sel.appendChild(el('option', { value: 'cuda:0' }, 'cuda:0'));
            sel.appendChild(el('option', { value: 'cpu' }, 'CPU'));
        } else {
            for (const d of devices) {
                const label = d.name && d.id !== 'cpu'
                    ? `${d.name} — ${d.vram_total_mb || '?'} MB (${d.id})`
                    : d.name || d.id;
                sel.appendChild(el('option', { value: d.id }, label));
            }
        }
        return sel;
    },

    async spawnWorker() {
        const btn = document.getElementById('spawn-btn');
        const model = document.getElementById('spawn-model')?.value;
        const device = document.getElementById('spawn-device')?.value;
        const precision = document.getElementById('spawn-precision')?.value || null;
        if (!model) return;

        if (btn) btn.disabled = true;
        const precLabel = precision ? ` (${precision})` : '';
        App.toast(`Spawning ${model} on ${device}${precLabel}...`, 'info');
        try {
            await App.api('POST', '/api/workers/spawn', { model, device, precision });
            App.toast(`${model} worker spawned!`, 'success');
            await App.poll();
        } catch (e) {
            App.toast(`Spawn failed: ${e.message}`, 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
    },

    async killWorker(workerId) {
        try {
            await App.api('DELETE', `/api/workers/${workerId}`);
            App.toast('Worker killed', 'success');
            await App.poll();
        } catch (e) {
            App.toast(`Kill failed: ${e.message}`, 'error');
        }
    },

    async killAll() {
        if (!confirm('Kill all workers? This unloads all models from GPU.')) return;
        const workers = [...App.state.workers];
        const results = await Promise.allSettled(
            workers.map(w => App.api('DELETE', `/api/workers/${w.worker_id}`))
        );
        const failed = results.filter(r => r.status === 'rejected').length;
        if (failed) {
            App.toast(`Killed ${results.length - failed}/${results.length} workers (${failed} failed)`, 'error');
        } else {
            App.toast('All workers killed', 'success');
        }
        await App.poll();
    },
};
