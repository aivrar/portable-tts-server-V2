/* ==========================================================================
   Tab: Voices — Browse, play, transcribe, and manage reference voice files
   ========================================================================== */

const TabVoices = {
    _voices: [],
    _selected: new Set(),

    init() {
        this._buildUI();
        this.refresh();
    },

    _buildUI() {
        const container = document.getElementById('tab-voices');
        container.innerHTML = '';

        // Upload row
        const uploadRow = el('div', { className: 'form-row mb-16' },
            el('div', { className: 'form-group' },
                el('label', null, 'Upload Voice'),
                el('input', { type: 'file', id: 'voice-upload', accept: 'audio/*', multiple: true }),
            ),
            el('div', { className: 'form-group', style: { justifyContent: 'flex-end' } },
                el('button', { className: 'btn btn-primary', onClick: () => this._upload() }, 'Upload'),
            ),
        );
        container.appendChild(uploadRow);

        // Toolbar
        const toolbar = el('div', { className: 'toolbar' },
            el('button', { className: 'btn btn-sm', onClick: () => this.refresh() }, 'Refresh'),
            el('button', { className: 'btn btn-sm', onClick: () => this._selectAll() }, 'Select All'),
            el('button', { className: 'btn btn-sm', onClick: () => this._deselectAll() }, 'Deselect All'),
            el('span', { className: 'toolbar-spacer' }),
            el('button', {
                className: 'btn btn-sm btn-danger',
                id: 'voice-delete-selected',
                onClick: () => this._deleteSelected(),
            }, 'Delete Selected (0)'),
        );
        container.appendChild(toolbar);

        // Voice table
        const tablePanel = el('div', { className: 'table-wrap', id: 'voice-table-wrap' });
        container.appendChild(tablePanel);
    },

    async refresh() {
        try {
            const data = await App.api('GET', '/api/voices');
            this._voices = data.voices || [];
        } catch (error) {
            App.toast('Voice refresh failed: ' + error.message, 'error');
            return;
        }
        if (typeof TabTesting !== 'undefined') TabTesting._loadSavedVoices();
        // Prune selections that no longer exist
        const filenames = new Set(this._voices.map(v => v.filename));
        for (const s of this._selected) {
            if (!filenames.has(s)) this._selected.delete(s);
        }
        this._render();
    },

    _render() {
        const wrap = document.getElementById('voice-table-wrap');
        if (!wrap) return;

        this._updateDeleteBtn();

        if (this._voices.length === 0) {
            wrap.innerHTML = '<div class="text-muted text-sm" style="padding:12px">' +
                'No voice files found. Upload .wav/.mp3/.flac/.ogg files above, ' +
                'or drop them into the <code>voices/</code> directory.</div>';
            return;
        }

        const table = el('table');
        table.innerHTML = '<thead><tr>' +
            '<th style="width:32px"></th>' +
            '<th>Name</th><th>Format</th><th>Size</th>' +
            '<th>Transcription</th><th>Actions</th>' +
            '</tr></thead>';
        const tbody = el('tbody');

        for (const v of this._voices) {
            const checked = this._selected.has(v.filename);
            const cb = el('input', {
                type: 'checkbox',
                onChange: () => this._toggleSelect(v.filename),
            });
            cb.checked = checked;

            const transCell = el('td', { className: 'text-sm', style: { maxWidth: '300px' } });
            if (v.transcription) {
                transCell.appendChild(el('span', { className: 'text-muted' }, v.transcription));
            } else {
                transCell.appendChild(el('span', { className: 'text-muted', style: { fontStyle: 'italic' } }, '--'));
            }

            // Capture the button element directly instead of round-tripping
            // through a string-built DOM id (filenames may contain whitespace/
            // punctuation that is invalid in an id token).
            const transBtn = el('button', {
                className: 'btn btn-sm',
            }, 'Transcribe');
            transBtn.addEventListener('click', () => this._transcribe(v.filename, transBtn));

            const row = el('tr', null,
                el('td', null, cb),
                el('td', null, v.name),
                el('td', { className: 'mono text-sm' }, v.format),
                el('td', { className: 'mono text-sm' }, this._fmtSize(v.size)),
                transCell,
                el('td', null,
                    el('div', { className: 'audio-controls' },
                        el('button', {
                            className: 'btn btn-sm',
                            onClick: () => this._play(v.filename),
                        }, 'Play'),
                        el('button', {
                            className: 'btn btn-sm',
                            onClick: () => App.stopAudio(),
                        }, 'Stop'),
                        transBtn,
                        el('button', {
                            className: 'btn btn-sm btn-danger',
                            onClick: () => this._delete(v.filename),
                        }, 'Delete'),
                    ),
                ),
            );
            tbody.appendChild(row);
        }

        table.appendChild(tbody);
        wrap.innerHTML = '';
        wrap.appendChild(table);
    },

    _fmtSize(bytes) {
        if (!bytes) return '?';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    },

    async _play(filename) {
        const generation = this._playGeneration = (this._playGeneration || 0) + 1;
        // Fetch the bytes with the X-TTS-API-Token header and play a blob: URL,
        // so the token never ends up in the <audio> src (history/access logs).
        try {
            const url = await App.fetchBlobUrl(`/api/voices/${encodeURIComponent(filename)}/audio`);
            if (generation !== this._playGeneration) { URL.revokeObjectURL(url); return; }
            if (this._playUrl) URL.revokeObjectURL(this._playUrl);
            this._playUrl = url;
            App.playAudio(url);
        } catch (e) {
            App.toast(`Playback failed: ${e.message}`, 'error');
        }
    },

    _toggleSelect(filename) {
        if (this._selected.has(filename)) {
            this._selected.delete(filename);
        } else {
            this._selected.add(filename);
        }
        this._updateDeleteBtn();
    },

    _selectAll() {
        for (const v of this._voices) this._selected.add(v.filename);
        this._render();
    },

    _deselectAll() {
        this._selected.clear();
        this._render();
    },

    _updateDeleteBtn() {
        const btn = document.getElementById('voice-delete-selected');
        if (btn) btn.textContent = `Delete Selected (${this._selected.size})`;
    },

    async _upload() {
        const input = document.getElementById('voice-upload');
        const files = input?.files;
        if (!files || files.length === 0) {
            App.toast('Select files to upload', 'error');
            return;
        }
        const uploadBtn = input?.closest('.form-row')?.querySelector('.btn-primary');
        if (uploadBtn) uploadBtn.disabled = true;
        let ok = 0;
        try {
            for (const file of files) {
                try {
                    const form = new FormData();
                    form.append('file', file);
                    const resp = await fetch('/api/voices/upload', {
                        method: 'POST',
                        headers: App.authHeaders(),
                        body: form,
                    });
                    if (!resp.ok) {
                        const err = await resp.text().catch(() => resp.statusText);
                        throw new Error(err);
                    }
                    ok++;
                } catch (e) {
                    App.toast(`Failed to upload ${file.name}: ${e.message}`, 'error');
                }
            }
            if (ok > 0) {
                App.toast(`Uploaded ${ok} file(s)`, 'success');
                input.value = '';
                await this.refresh();
            }
        } finally {
            if (uploadBtn) uploadBtn.disabled = false;
        }
    },

    async _delete(filename) {
        try {
            await App.api('DELETE', `/api/voices/${encodeURIComponent(filename)}`);
            App.toast(`Deleted ${filename}`, 'success');
            this._selected.delete(filename);
            await this.refresh();
        } catch (e) {
            App.toast(`Delete failed: ${e.message}`, 'error');
        }
    },

    async _deleteSelected() {
        const filenames = [...this._selected];
        if (filenames.length === 0) {
            App.toast('No voices selected', 'error');
            return;
        }
        try {
            const data = await App.api('POST', '/api/voices/delete', filenames);
            const n = data.deleted?.length || 0;
            App.toast(`Deleted ${n} voice(s)`, 'success');
            this._selected.clear();
            await this.refresh();
        } catch (e) {
            App.toast(`Bulk delete failed: ${e.message}`, 'error');
        }
    },

    async _transcribe(filename, btn) {
        if (btn) { btn.disabled = true; btn.textContent = 'Working...'; }

        try {
            const data = await App.api('POST', `/api/voices/${encodeURIComponent(filename)}/transcribe?size=base`);
            const text = data.text || '(empty)';
            App.toast(`Transcribed: ${text.slice(0, 80)}`, 'success');
            // Update the local cache and re-render to show transcription
            const v = this._voices.find(x => x.filename === filename);
            if (v) v.transcription = text;
            this._render();
        } catch (e) {
            App.toast(`Transcription failed: ${e.message}`, 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Transcribe'; }
        }
    },
};
