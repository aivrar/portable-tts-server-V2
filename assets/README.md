# TTS app artwork

`tts-icon.png` is the generated master. `tts.ico` contains 16, 20, 24, 32,
48, 64, 128 and 256 pixel images for Windows. The app header uses the 64 pixel
PNG under `server/static/`; the browser favicon and native launcher use the ICO.

Created with the built-in image-generation tool on 2026-09-25. Prompt:

> Create a premium square Windows desktop icon for a text-to-speech app. The
> only text is the exact large, bold uppercase letters TTS. Integrate an audio
> waveform above the letters and a subtle speech-bubble tile silhouette.
> Front-facing composition, crisp edges, readable at small sizes. Deep
> charcoal/navy tile, teal and electric-blue accents, restrained depth,
> rounded corners, transparent exterior. No extra words, watermark, mockup,
> perspective tilt or alternative icons.

Rebuild the format exports from the master with
`powershell -NoProfile -File tools/Convert-TTSIcon.ps1`.
