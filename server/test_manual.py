"""Coverage checks for the user-facing manual under ../manual/.

Reads the real Markdown pages (not fixtures). When MANUAL_EVIDENCE_DIR is
set, also writes a page inventory and a pass log into that directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

MANUAL_DIR = Path(__file__).resolve().parent.parent / "manual"

ENGINE_IDS = (
    "bark",
    "chatterbox",
    "dia",
    "f5",
    "fish",
    "higgs",
    "kokoro",
    "qwen",
    "vibevoice",
    "whisper",
    "xtts",
    "speecht5",
    "parler",
    "outetts",
    "vits",
    "edge",
    "voxtral",
    "voxcpm2",
    "csm",
    "orpheus",
)

GUI_TABS = ("Setup", "Server", "Voices", "Testing", "Editor", "Log", "Shutdown")

STUB_PATTERNS = (
    re.compile(r"\bTODO\b"),
    re.compile(r"\bTBD\b"),
    re.compile(r"placeholder", re.IGNORECASE),
    re.compile(r"coming soon", re.IGNORECASE),
)

MIN_PAGE_CHARS = 2000


def _page_files() -> list[Path]:
    assert MANUAL_DIR.is_dir(), f"manual directory missing: {MANUAL_DIR}"
    pages = sorted(p for p in MANUAL_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".md")
    return pages


def test_manual_local_links_resolve():
    for page in _page_files():
        text = page.read_text(encoding="utf-8")
        for destination in re.findall(r"!?\[[^]]*\]\(([^)]+)\)", text):
            target = destination.split("#", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue
            assert (page.parent / target).exists(), f"broken link in {page.name}: {destination}"


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _corpus(pages: list[Path]) -> tuple[dict[Path, str], str]:
    by_file = {p: p.read_text(encoding="utf-8") for p in pages}
    return by_file, "\n".join(by_file.values())


def _write_evidence(pages: list[Path], by_file: dict[Path, str], extra: str) -> None:
    dest = os.environ.get("MANUAL_EVIDENCE_DIR", "").strip()
    if not dest:
        return
    out_dir = Path(dest)
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory_lines = []
    for page in pages:
        text = by_file[page]
        heading = _first_heading(text)
        inventory_lines.append(f"{page.name}\t{len(text.encode('utf-8'))}\t{heading}")
    (out_dir / "manual_inventory.txt").write_text("\n".join(inventory_lines) + "\n", encoding="utf-8")
    (out_dir / "manual_pytest_pass.txt").write_text(
        "PASS\n" + extra + "\n" + "\n".join(inventory_lines) + "\n",
        encoding="utf-8",
    )


def test_manual_pages_identity_requirements_coverage_and_length():
    pages = _page_files()
    assert len(pages) > 1, f"expected multiple manual pages, found {len(pages)}"

    by_file, corpus = _corpus(pages)
    headings = [_first_heading(text) for text in by_file.values()]

    a_to_z = [
        page
        for page, text in by_file.items()
        if re.search(r"A to Z knowledge page", _first_heading(text), re.IGNORECASE)
        or re.search(r"^#\s*A to Z knowledge page\b", text, re.IGNORECASE | re.MULTILINE)
    ]
    assert a_to_z, f"missing A to Z knowledge page heading in {headings!r}"

    required_phrases = (
        "linux distro for windows users",
        "linbox-TTS_Server",
        r"E:\tts_server",
        "WSL2",
        "Start-TTSServer.cmd",
        "ext4.vhdx",
        "tts.cmd",
        "X-TTS-API-Token",
        "jobs",
        "voices",
        "SRT",
        "audio editor",
    )
    missing_phrases = [p for p in required_phrases if p not in corpus]
    assert not missing_phrases, f"manual missing required phrases: {missing_phrases}"

    missing_tabs = [tab for tab in GUI_TABS if tab not in corpus]
    assert not missing_tabs, f"manual missing GUI tab names: {missing_tabs}"

    missing_engines = []
    for engine_id in ENGINE_IDS:
        if not re.search(rf"(?i)\b{re.escape(engine_id)}\b", corpus):
            missing_engines.append(engine_id)
    assert not missing_engines, f"manual missing MODEL_SETUP engine ids: {missing_engines}"

    stub_hits = []
    short_pages = []
    for page, text in by_file.items():
        if len(text) < MIN_PAGE_CHARS:
            short_pages.append(f"{page.name}:{len(text)}")
        if not _first_heading(text):
            short_pages.append(f"{page.name}:no-heading")
        for pat in STUB_PATTERNS:
            if pat.search(text):
                stub_hits.append(f"{page.name}:{pat.pattern}")
    assert not stub_hits, f"stub language in manual pages: {stub_hits}"
    assert not short_pages, f"pages too short or heading-only: {short_pages}"

    summary = (
        f"pages={len(pages)} a_to_z={a_to_z[0].name} "
        f"engines={len(ENGINE_IDS)} tabs={len(GUI_TABS)}"
    )
    _write_evidence(pages, by_file, summary)
    print("MANUAL_INVENTORY")
    for page in pages:
        text = by_file[page]
        print(f"{page.name}\t{len(text.encode('utf-8'))}\t{_first_heading(text)}")
    print(summary)
