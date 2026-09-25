const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(file, name, extras = {}) {
    const elements = new Map();
    const sandbox = {
        console, setTimeout, clearTimeout, setInterval, clearInterval,
        URL: { revokeObjectURL() {} },
        document: {
            addEventListener() {},
            getElementById(id) { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
        },
        window: {},
        ...extras,
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(__dirname, 'static', file), 'utf8') + `\nglobalThis.subject = ${name};`, sandbox);
    return { subject: sandbox.subject, sandbox, elements };
}

test('range selections remain tied to the original waveform after cuts and trims', () => {
    const { subject } = load('tab-jobs.js', 'TabJobs');
    subject._chain = [
        { type: 'cut', enabled: true, isRange: true, params: { start_sec: 1, end_sec: 2 } },
        { type: 'cut', enabled: true, isRange: true, params: { start_sec: 4, end_sec: 5 } },
        { type: 'trim', enabled: true, isRange: true, params: { start_sec: 3, end_sec: 8 } },
    ];
    const edits = subject._gatherEdits();
    assert.equal(edits[1].params.start_sec, 3);
    assert.equal(edits[1].params.end_sec, 4);
    assert.equal(edits[2].params.start_sec, 2);
    assert.equal(edits[2].params.end_sec, 6);
});

test('replaying history does not revoke its blob and pauses the Editor', () => {
    let revocations = 0, pauses = 0;
    const { subject } = load('app.js', 'App', {
        URL: { revokeObjectURL() { revocations++; } },
        TabJobs: { _pause() { pauses++; } },
        Audio: class { play() { return Promise.resolve(); } pause() {} },
    });
    subject.playAudio('blob:history');
    subject.playAudio('blob:history');
    assert.equal(revocations, 0);
    assert.equal(pauses, 2);
});

test('job polling fetches every page and prevents overlapping requests', async () => {
    const { subject } = load('app.js', 'App');
    let requests = 0;
    subject.api = async () => {
        requests++;
        await Promise.resolve();
        return { jobs: [{ job_id: String(requests) }], total: 2 };
    };
    await Promise.all([subject.pollJobs(), subject.pollJobs()]);
    assert.equal(requests, 2);
    assert.equal(subject.state.jobs.length, 2);
});

test('render completion preserves edits made while waiting', async () => {
    let finish;
    const response = new Promise(resolve => { finish = resolve; });
    const { subject } = load('tab-jobs.js', 'TabJobs', {
        App: { api: () => response, toast() {} },
    });
    subject._activeSource = { kind: 'final', job_id: 'job' };
    subject._chain = [{ type: 'gain', enabled: true, params: { db: 1 } }];
    subject._fetchEdits = async () => {};
    subject._loadSource = async () => { throw new Error('Must retain original source'); };
    const render = subject._render();
    subject._chain.push({ type: 'gain', enabled: true, params: { db: 2 } });
    finish({ job_id: 'job', edit_name: 'result.wav' });
    await render;
    assert.equal(subject._chain.length, 2);
});

test('navigation during edit refresh is not overwritten by render completion', async () => {
    let finishRefresh;
    const refreshed = new Promise(resolve => { finishRefresh = resolve; });
    const { subject } = load('tab-jobs.js', 'TabJobs', {
        App: { api: async () => ({ job_id: 'job', edit_name: 'result.wav' }), toast() {} },
    });
    subject._activeSource = { kind: 'final', job_id: 'job' };
    subject._chain = [];
    subject._fetchEdits = () => refreshed;
    subject._loadSource = async () => { throw new Error('Must retain new selection'); };
    const rendering = subject._render();
    await Promise.resolve();
    const next = { kind: 'final', job_id: 'next' };
    subject._activeSource = next;
    finishRefresh();
    await rendering;
    assert.equal(subject._activeSource, next);
});

test('earlier voice fetch cannot override a later playback click', async () => {
    const pending = [], played = [], revoked = [];
    const { subject } = load('tab-voices.js', 'TabVoices', {
        URL: { revokeObjectURL: value => revoked.push(value) },
        App: { fetchBlobUrl: () => new Promise(resolve => pending.push(resolve)), playAudio: value => played.push(value), toast() {} },
    });
    const first = subject._play('first.wav');
    const second = subject._play('second.wav');
    pending[1]('blob:second');
    await second;
    pending[0]('blob:first');
    await first;
    assert.deepEqual(played, ['blob:second']);
    assert.deepEqual(revoked, ['blob:first']);
});

function testingTab(model, refAudio) {
    const requests = [], toasts = [], downloaded = [];
    const state = {
        workers: [{ worker_id: 'selected-worker', model, device: 'cpu' }],
        models: { [model]: { ref_audio: refAudio } },
        defaults: { [model]: {} }, fields: {}, profiles: {},
    };
    const loaded = load('tab-testing.js', 'TabTesting', {
        App: {
            state, authHeaders: extra => ({ 'X-TTS-API-Token': 'test-token', ...extra }),
            toast: (...args) => toasts.push(args),
            fetchBlobUrl: async url => { downloaded.push(url); return 'blob:large-output'; },
        },
        fetch: async (url, options) => {
            requests.push({ url, ...options });
            return { ok: true, json: async () => ({ status: 'completed', format: 'wav', output_url: '/api/jobs/test/output' }) };
        },
    });
    loaded.elements.set('test-worker', { value: 'selected-worker' });
    loaded.elements.set('test-text', { value: 'Hello there' });
    loaded.subject._renderHistory = () => {};
    return { ...loaded, requests, toasts, downloaded };
}

test('Testing honors the selected worker and downloads large output without a hidden reference', async () => {
    const { subject, elements, requests, downloaded } = testingTab('kokoro', false);
    elements.set('test-ref-audio', { files: [{ name: 'hidden.wav' }] });
    elements.set('test-ref-voice', { value: 'hidden.wav' });
    await subject.generate();
    assert.equal(requests[0].url, '/api/tts/kokoro');
    const body = JSON.parse(requests[0].body);
    assert.equal(body.worker_id, 'selected-worker');
    assert.equal(body.device, 'cpu');
    assert.equal(body.voice, undefined);
    assert.deepEqual(downloaded, ['/api/jobs/test/output']);
    assert.equal(subject._history[0].audio_url, 'blob:large-output');
});

test('Testing submits ordered VibeVoice saved references', async () => {
    const { subject, elements, requests } = testingTab('vibevoice', true);
    elements.set('test-ref-voice', { value: 'host.wav' });
    elements.set('test-speaker-2', { value: 'guest.wav' });
    await subject.generate();
    assert.deepEqual(JSON.parse(requests[0].body).reference_audios, ['host.wav', 'guest.wav']);
});

test('Testing rejects missing F5 reference before sending a request', async () => {
    const { subject, requests, toasts } = testingTab('f5', true);
    await subject.generate();
    assert.equal(requests.length, 0);
    assert.match(toasts[0][0], /reference voice/);
});

test('Testing initializes with saved voices and toggles extra speakers on model changes', async () => {
    const { subject, elements, sandbox } = testingTab('vibevoice', true);
    sandbox.window.addEventListener = () => {};
    sandbox.el = () => ({});
    subject._buildUI = () => {};
    subject._buildParams = () => {};
    subject._buildProfileGrid = () => {};
    subject._buildStyleTags = () => {};
    subject._loadSavedVoices = async () => { subject._hasSavedVoices = true; };
    sandbox.App.api = async () => ({ voices: [] });
    for (const id of ['ref-row', 'ref-text-group', 'vibe-speakers', 'style-row']) {
        elements.set(id, { style: { display: 'none' } });
    }
    elements.set('test-voice', { value: '', options: [], appendChild() {} });
    subject.init();
    await Promise.resolve();
    assert.equal(elements.get('ref-row').style.display, '');
    await subject._onWorkerChange();
    assert.equal(elements.get('vibe-speakers').style.display, '');
    sandbox.App.state.workers[0].model = 'kokoro';
    sandbox.App.state.models.kokoro = { ref_audio: false };
    await subject._onWorkerChange();
    assert.equal(elements.get('vibe-speakers').style.display, 'none');
    assert.equal(elements.get('ref-row').style.display, 'none');
});
