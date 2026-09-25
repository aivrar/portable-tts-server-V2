"""Render the source manual into a cloned GitHub wiki, without committing or pushing.

Usage: python tools/sync_wiki.py --output output/github-wiki
See release/WIKI.md for the publishing workflow.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/aivrar/portable-tts-server-V2"
WIKI = REPO + "/wiki"
RAW = "https://raw.githubusercontent.com/wiki/aivrar/portable-tts-server-V2"
PAGES = {
    "manual/README.md": "Manual-Index",
    "manual/quickstart.md": "Quickstart",
    "manual/identity-and-requirements.md": "Requirements",
    "manual/start-stop-and-portability.md": "Start-Stop-and-Portability",
    "manual/storage-layout-and-ports.md": "Storage-and-Ports",
    "manual/gui-overview.md": "Desktop-Overview",
    "manual/gui-setup.md": "Setup",
    "manual/gui-server.md": "Server",
    "manual/gui-voices.md": "Voices",
    "manual/gui-testing.md": "Testing-and-Generation",
    "manual/gui-editor.md": "Editor",
    "manual/gui-log-and-shutdown.md": "Log-and-Shutdown",
    "manual/windows-cli.md": "Windows-CLI",
    "manual/http-api.md": "HTTP-API",
    "manual/engines-overview.md": "Engines",
    "manual/engines-catalog.md": "Engine-Catalog",
    "manual/jobs-and-projects.md": "Jobs-and-Projects",
    "manual/srt-and-whisper.md": "Subtitles-and-Whisper",
    "manual/audio-editor-usage.md": "Audio-Editing",
    "manual/diagnostics-and-maintenance.md": "Diagnostics-and-Maintenance",
    "manual/troubleshooting.md": "Troubleshooting",
    "manual/a-to-z.md": "A-to-Z",
    "PORTABILITY.md": "Portable-Storage-and-Transfer",
    "release/BUILD.md": "Building-a-Release",
    "manual/images/README.md": "Screenshot-Notes",
}


def link(label, page):
    return f"[{label}]({WIKI}/{page})"


def rewrite_links(text, source):
    def replace(match):
        target = match[2]
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or target.startswith("#"):
            return match[0]
        path = (source.parent / parsed.path).resolve()
        if not path.is_relative_to(ROOT) or not path.exists():
            raise ValueError(f"Missing or escaping link in {source.name}: {target}")
        relative = path.relative_to(ROOT).as_posix()
        if relative in PAGES:
            url = WIKI + "/" + PAGES[relative]
        elif path.parent == ROOT / "manual/images" and path.suffix == ".png":
            url = RAW + "/images/" + quote(path.name)
        else:
            url = REPO + ("/tree/main/" if path.is_dir() else "/blob/main/") + quote(relative)
        if parsed.query:
            url += "?" + parsed.query
        if parsed.fragment:
            url += "#" + parsed.fragment
        return match[1] + url + match[3]

    # Preserve command examples and sample Markdown inside fenced code blocks.
    sections = re.split(r"(```.*?```|~~~.*?~~~)", text, flags=re.DOTALL)
    for index in range(0, len(sections), 2):
        sections[index] = re.sub(r"(!?\[[^\]\n]*\]\()([^\s)]+)(\))", replace, sections[index])
    return "".join(sections)


def home():
    return f"""# Portable TTS Server V2

**A portable speech studio for Windows, powered by its own WSL2 Linux distro.** Generate speech, manage engines and voices, create subtitles, and edit audio in a desktop window. A Windows CLI and authenticated HTTP API are included.

**[Download the portable release]({REPO}/releases/latest)** · **{link('Follow the illustrated quickstart', 'Quickstart')}** · **{link('Browse the complete manual', 'Manual-Index')}**

![Portable TTS Server V2 with a real Kokoro narration in the Editor]({RAW}/images/github-hero.png)

## Get running

1. Use Windows 10/11 x64 with **WSL2** enabled. Python, .NET, WebView2, Linux dependencies, and Kokoro are bundled. A GPU is optional; GPU use needs the Windows NVIDIA driver.
2. Download both extraction helpers from the [release page]({REPO}/releases/latest), put them in one folder, and run `Extract-Portable-TTS.cmd -Download` from a terminal. It downloads all four parts, verifies their checksums, and extracts the app. You can also download the parts and manifest yourself and double-click the helper.
3. Choose a writable local drive. Allow at least **30 GiB free**, plus space for retained download parts and archives. GitHub's automatic **Source code** ZIP contains source only.
4. Double-click **Start-TTSServer.cmd** in the extracted folder. First launch imports the bundled Linux image.
5. In **Server**, select **Kokoro 82M**, choose CPU or a GPU, and spawn a worker. In **Testing**, choose a built-in voice, enter text, and generate.

**Kokoro works offline.** Other engines install into this portable copy when selected and need internet for their downloads. Gated models require your Hugging Face access. Edge uses an online service; optional Whisper transcription weights download separately.

Follow the {link('illustrated first narration', 'Quickstart')} for screenshots of generating speech, loading the waveform, applying effects, and saving an edit.

## Find the instructions you need

| Task | Pages |
| --- | --- |
| Install and start | {link('Requirements', 'Requirements')} · {link('Start and stop', 'Start-Stop-and-Portability')} · {link('Storage and ports', 'Storage-and-Ports')} |
| Use the desktop | {link('Overview', 'Desktop-Overview')} · {link('Setup', 'Setup')} · {link('Server', 'Server')} · {link('Voices', 'Voices')} · {link('Testing', 'Testing-and-Generation')} · {link('Editor', 'Editor')} · {link('Log and Shutdown', 'Log-and-Shutdown')} |
| Choose an engine | {link('Engine workflow', 'Engines')} · {link('All 20 engines', 'Engine-Catalog')} |
| Work with audio | {link('Jobs and projects', 'Jobs-and-Projects')} · {link('Audio editing', 'Audio-Editing')} · {link('Subtitles and Whisper', 'Subtitles-and-Whisper')} |
| Automate | {link('Windows CLI', 'Windows-CLI')} · {link('HTTP API', 'HTTP-API')} |
| Maintain or troubleshoot | {link('Diagnostics', 'Diagnostics-and-Maintenance')} · {link('Troubleshooting', 'Troubleshooting')} · {link('A to Z', 'A-to-Z')} |

## Move your installation

Run **Stop-TTSServer.cmd** first. When it confirms the disk is released, copy the **entire app folder**, including `wsl/ext4.vhdx`, voices, projects, and settings.

If WSL keeps the disk locked, use the transfer helper from the app folder:

```powershell
.\\Copy-TTSServer.ps1 -Destination 'D:\\Portable-TTS-Server-V2'
```

This preserves the original and transfers its Linux state without stopping unrelated WSL apps. See {link('portable storage and transfer', 'Portable-Storage-and-Transfer')} for space, startup, and snapshot cleanup instructions.

## About this manual

The eight app screenshots and repository hero show a real Kokoro workflow. GPU names and timings are examples from the capture session. See {link('screenshot notes', 'Screenshot-Notes')} for captions and capture details.

Maintainers can read {link('building a release', 'Building-a-Release')} and the [source repository]({REPO}). The source manual also ships with the portable app for local reading.
"""


def sidebar():
    groups = {
        "Start here": [("Home", "Home"), ("Quickstart", "Quickstart"), ("Requirements", "Requirements"),
                       ("Start, stop and portability", "Start-Stop-and-Portability"), ("Complete manual", "Manual-Index")],
        "Desktop tabs": [("Overview", "Desktop-Overview"), ("Setup", "Setup"), ("Server", "Server"),
                         ("Voices", "Voices"), ("Testing and generation", "Testing-and-Generation"),
                         ("Editor", "Editor"), ("Log and Shutdown", "Log-and-Shutdown")],
        "Engines and audio": [("Engine workflow", "Engines"), ("Engine catalog", "Engine-Catalog"),
                              ("Jobs and projects", "Jobs-and-Projects"), ("Audio editing", "Audio-Editing"),
                              ("Subtitles and Whisper", "Subtitles-and-Whisper")],
        "CLI and API": [("Windows CLI", "Windows-CLI"), ("HTTP API", "HTTP-API")],
        "Maintenance": [("Storage and ports", "Storage-and-Ports"), ("Move or copy the app", "Portable-Storage-and-Transfer"),
                        ("Diagnostics", "Diagnostics-and-Maintenance"), ("Troubleshooting", "Troubleshooting"), ("A to Z", "A-to-Z")],
        "Reference": [("Building a release", "Building-a-Release"), ("Screenshot notes", "Screenshot-Notes")],
    }
    return "\n\n".join("**" + heading + "**\n\n" + "\n".join("- " + link(label, page) for label, page in pages)
                        for heading, pages in groups.items()) + f"\n\n[Download release]({REPO}/releases/latest)\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    remote = subprocess.check_output(["git", "-C", str(destination), "remote", "get-url", "origin"], text=True).strip()
    if remote != REPO + ".wiki.git":
        raise SystemExit("Output must be a clone of this project's GitHub wiki")
    actual_manual = {p.relative_to(ROOT).as_posix() for p in (ROOT / "manual").glob("*.md")}
    if actual_manual != {p for p in PAGES if p.startswith("manual/") and p.count("/") == 1}:
        raise SystemExit("Update the wiki page map to cover every manual page")
    navigation = link("Home", "Home") + " · " + link("Manual index", "Manual-Index") + "\n\n"
    for relative, page in PAGES.items():
        source = ROOT / relative
        text = rewrite_links(source.read_text(encoding="utf-8"), source)
        (destination / (page + ".md")).write_text(navigation + text.rstrip() + "\n", encoding="utf-8")
    (destination / "Home.md").write_text(home(), encoding="utf-8")
    (destination / "_Sidebar.md").write_text(sidebar(), encoding="utf-8")
    (destination / "_Footer.md").write_text(
        f"{link('Wiki home', 'Home')} · {link('Manual index', 'Manual-Index')} · "
        f"[Download release]({REPO}/releases/latest) · [Source]({REPO})\n\n"
        "Portable TTS Server V2 · Windows + WSL2 · Offline-ready Kokoro\n", encoding="utf-8")
    images = destination / "images"
    images.mkdir(exist_ok=True)
    for source in (ROOT / "manual/images").glob("*.png"):
        shutil.copy2(source, images / source.name)
    (destination / ".gitattributes").write_text("*.md text eol=lf\n*.png binary\n", encoding="utf-8")
    print(f"Rendered {len(PAGES) + 1} wiki pages, sidebar, footer, and {len(list(images.glob('*.png')))} images into {destination}")
    print("Review the wiki diff, commit, and push its default branch when ready.")


if __name__ == "__main__":
    main()
