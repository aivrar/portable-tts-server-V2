# GUI overview

The desktop UI is a WebView2 window loading the gateway's static files (`server/static/index.html` plus `app.js`, `tab-setup.js`, `tab-server.js`, `tab-voices.js`, `tab-testing.js`, `tab-jobs.js`, `tab-log.js`, `style.css`). The native window title is **Portable TTS Server V2**, with the TTS icon. Initial content size is 1440×900; Windows DPI scaling affects its display size.

You do not log in. For loopback clients, the HTML is served with a persisted API token injected as `window.__TTS_API_TOKEN__`. Every `fetch` to `/api/*` sends header `X-TTS-API-Token`. The live log stream is the one exception: `EventSource` cannot set headers, so it uses `?token=` on `/api/logs/stream` only.

## Chrome

Left to right in the header:

1. **TTS Server** heading.
2. **Gateway badge** (`#gateway-badge`):
   - `Connecting...` (gray) on first paint.
   - `Connected (N workers)` (green) when `GET /api/workers` succeeds.
   - `Disconnected` (red) when polling fails (backoff 3s → 6s → 12s → 24s).
   - `Reconnecting...` (orange) if the SSE log stream errors while the badge was green.
3. **Nav tabs**: Setup, Server, Voices, Testing, Editor, Log.
4. **Shutdown** (red, small): "Stop all workers, unload models, and close the app".

Only one tab's section is `.active` at a time. Switching to **Editor** (internal id `jobs`) calls `TabJobs.onTabActivated()` on the next animation frame so the waveform canvas can measure a non-zero size.

## The six tabs and Shutdown

| Tab label | `data-tab` | Section id | Script | Job of the tab |
| --- | --- | --- | --- | --- |
| **Setup** | `setup` | `tab-setup` | `tab-setup.js` | Hugging Face token, install/remove engines |
| **Server** | `server` | `tab-server` | `tab-server.js` | Gateway status, spawn/kill workers |
| **Voices** | `voices` | `tab-voices` | `tab-voices.js` | Reference audio library |
| **Testing** | `testing` | `tab-testing` | `tab-testing.js` | Speak text with a loaded worker |
| **Editor** | `jobs` | `tab-jobs` | `tab-jobs.js` | Waveform editor over job audio |
| **Log** | `log` | `tab-log` | `tab-log.js` | Live filtered log |
| **Shutdown** | (button, not a tab) | `#shutdown-btn` | `app.js` `initShutdownButton` | Unload everything and close |

The Editor tab is labeled **Editor** in the nav but the DOM id remains `jobs` because the library is the job list. In this manual, "Editor tab" means that button.

## How the GUI talks to the backend

On load, `App` fetches `/api/config` (models from `MODEL_SETUP`, defaults, fields, param slider config, param overrides, tooltips, audio profiles, override map, ports, paths), `/api/setup/status`, `/api/devices`, `/api/workers`, and `/api/jobs`. It then starts:

- Worker poll every **3 seconds** (`POLL_INTERVAL`).
- Job poll (Editor library).
- SSE `GET /api/logs/stream`.

Toasts appear in `#toast-container`. Click a toast to dismiss it. Errors stay 12 seconds; info/success stay 4 seconds.

`App.fetchBlobUrl` is used for Play buttons so the token stays in the header rather than in an `<audio src>` URL (which would leak into history).

## Typical click path

Follow the [illustrated quickstart](quickstart.md) for a complete example with screenshots. The manual images show the running app's web interface in Chromium; the Windows title bar is outside the capture.

1. **Setup** — save Hugging Face token if needed; **Install** Kokoro or Edge (Edge has no weights).
2. **Log** — watch the installer (Setup jumps you here automatically).
3. **Server** — pick Model, Device, Precision **Auto**, click **Spawn Worker**. Wait until Status is ready.
4. **Voices** — optional: upload a reference WAV for cloning engines.
5. **Testing** — Worker dropdown lists ready/busy workers; pick Voice; type Text; **Generate**; Play from Response History.
6. **Editor** — expand the job, click FINAL or a chunk, add effects, **Render**.
7. **Shutdown** when finished so VRAM is released and the registry file is removed.

## Status badges you will see

Install (Setup cards): `not_installed`, `partial`, `packages_only`, `ready`, `installing`.

Workers (Server table): `ready`, `busy`, `loading`, and others; **Kill All** unloads every process.

Jobs (Editor library): `completed` (green), `running` / `partial` (orange), `failed` (red).

Gateway panel: `ready` vs `dead` depending on `App.state.connected`.

## What the GUI does not expose

Some API/CLI surfaces have no dedicated button: `dryrun`, `batch`, `deliver_to`, project ZIP, maintenance `gc-jobs` / `clear-cache`, OpenAPI dump, `peers`. Use `tts.cmd` or HTTP for those. The GUI is the interactive subset: install, workers, voices, generate, edit, logs, shutdown.

## Related pages

- [Setup tab](gui-setup.md)
- [Server tab](gui-server.md)
- [Voices tab](gui-voices.md)
- [Testing tab](gui-testing.md)
- [Editor tab](gui-editor.md)
- [Log tab and Shutdown](gui-log-and-shutdown.md)
