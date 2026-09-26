# Third-party components

Portable TTS Server V2 application code is MIT licensed; see [LICENSE](LICENSE).
Bundled dependencies retain their own licences and notices.

## Distro origin

This TTS distro originated from the author's **[Portable Linux in a Box](https://github.com/aivrar/portable-linux-in-a-box)** project, the source of its portable Linux application foundation. V2 retains that project lineage while rebuilding its public runtime from a clean Ubuntu base. Portable Linux in a Box is MIT licensed.

| Component | Distribution and notices |
| --- | --- |
| Ubuntu 24.04 base and system packages | Copyright files remain under `/usr/share/doc/<package>/copyright` inside the Linux runtime. Exact installed and source package versions are listed in `runtime/licenses/linux-packages.tsv`. Corresponding source packages are available in the [Ubuntu archive](https://archive.ubuntu.com/ubuntu/pool/). |
| Linux Python and speech libraries | Original package metadata and licence files remain in the bundled virtual environment. `runtime/licenses/python-packages.json` and `python-freeze.txt` identify the actual build. |
| Kokoro 82M model | Apache 2.0, as declared by the [upstream model repository](https://huggingface.co/hexgrad/Kokoro-82M). The package includes the upstream model card and the pinned model inventory. |
| Kokoro / Misaki | Upstream notices are retained with installed packages. [Kokoro](https://github.com/hexgrad/kokoro), [Misaki](https://github.com/hexgrad/misaki). |
| spaCy English model, UniDic, Open JTalk dictionaries | Their licence and dictionary notices are retained with the installed language packages and dictionary data. |
| NVIDIA CUDA libraries supplied with PyTorch | Original NVIDIA wheel licence/EULA files remain with the packages. The GPU driver is supplied by the host computer. |
| Windows Python | The embeddable distribution's `LICENSE.txt` is included under `runtime/python`. |
| .NET | The self-contained launcher includes its runtime and the original .NET licence/third-party notices. |
| Microsoft WebView2 | Fixed Version runtime and SDK are redistributed intact under Microsoft's applicable terms. Runtime files and notices are under `runtime/webview2`; loader/managed assemblies are under `runtime/launcher`. |

The app's MIT licence does not change third-party terms. Optional engines are
downloaded only when selected and retain their own code/model licences; some
model repositories also require the user to accept access terms.

Download URLs, component versions and archive checksums used for the release are
recorded in [release/runtime-sources.json](release/runtime-sources.json).
The complete Linux image includes its package copyright files, source repository
configuration and Python distribution metadata so the notices travel with the app.
Exact Ubuntu source archives and packaging/build instructions accompany the runtime
under `runtime/sources/ubuntu`; checksums are in `runtime/licenses/ubuntu-sources.json`.
These source archives are not needed for normal speech generation.
