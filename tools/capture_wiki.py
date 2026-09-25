"""Capture the running app using a real, public demo narration.

Requires Python Playwright and a ready Kokoro worker. Creates one demo job,
one named edit and an uploaded demo voice. It never installs/kills workers.
See manual/images/README.md for preparation and capture details.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEMO = (
    "Studio demo. Every voice begins with an idea. Turn a short script into clear, "
    "natural narration, then shape the sound in the editor. A gentle pause gives "
    "each sentence room to breathe. Listen to the take, refine the details, and "
    "save a finished version when it sounds right."
)


def capture(url: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / "output" / "wiki-capture"
    scratch.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 950}, device_scale_factor=2,
            locale="en-US", color_scheme="dark", reduced_motion="reduce",
        )
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("App.tabsReady && document.querySelector('#tab-setup .card')")
        worker = page.evaluate("App.state.workers.find(w => w.model === 'kokoro' && w.status === 'ready')")
        if not worker:
            raise RuntimeError("Install Kokoro and spawn a ready worker in the app first.")

        def tab(name: str) -> None:
            page.locator(f'.tab-btn[data-tab="{name}"]').click()

        def settle() -> None:
            page.wait_for_function("document.querySelectorAll('.toast').length === 0", timeout=15000)
            page.mouse.move(1438, 2)
            page.evaluate("document.activeElement?.blur()")
            page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")

        def shot(name: str, height: int | None = None, **kwargs) -> None:
            settle()
            if height:
                kwargs["clip"] = {"x": 0, "y": 0, "width": 1440, "height": height}
            page.screenshot(path=str(output / f"{name}.png"), animations="disabled", **kwargs)
            print(f"Captured {name}", flush=True)

        # Real installation state, with all saved-token fragments concealed.
        page.set_viewport_size({"width": 1440, "height": 1250})
        tab("setup")
        cards = page.locator("#tab-setup .card")
        bottom = cards.last.bounding_box()
        shot("setup", min(1250, int(bottom["y"] + bottom["height"] + 16)),
             mask=[page.locator("#tab-setup > .panel").first.locator(".badge"), page.locator("#hf-token-input")],
             mask_color="#30363d")

        page.set_viewport_size({"width": 1440, "height": 950})
        tab("server")
        page.locator("#spawn-model").select_option("kokoro")
        page.locator("#spawn-device").select_option(worker["device"])
        panel = page.locator("#srv-workers").bounding_box()
        shot("server", int(panel["y"] + panel["height"] + 16))

        # Clear only the displayed log before generating public demo text.
        tab("log")
        page.locator("#tab-log").get_by_role("button", name="Clear", exact=True).click()
        tab("testing")
        page.locator("#test-worker").select_option(worker["worker_id"])
        page.locator('#test-voice option[value="af_heart"]').wait_for(state="attached")
        page.locator("#test-voice").select_option("af_heart")
        page.locator("#test-text").fill(DEMO)
        page.locator("#test-generate").click()
        page.wait_for_function("TabTesting._history.length > 0", timeout=300000)
        result = page.evaluate("({status: TabTesting._history[0].status, error: TabTesting._history[0].error})")
        if result["status"] != "completed":
            raise RuntimeError(f"Demo generation failed: {result.get('error')}")
        shot("testing")
        with page.expect_download() as download:
            page.locator("#tab-testing").get_by_role("button", name="Save", exact=True).click()
        voice = scratch / "00_wiki_demo.wav"
        download.value.save_as(str(voice))

        tab("log")
        shot("log", 620)

        # Upload through the same control as a user; crop before unrelated rows.
        tab("voices")
        page.locator("#voice-upload").set_input_files(str(voice))
        page.locator("#tab-voices").get_by_role("button", name="Upload", exact=True).click()
        demo_row = page.locator("#voice-table-wrap tbody tr").filter(has_text="00_wiki_demo")
        demo_row.wait_for()
        first = page.locator("#voice-table-wrap tbody tr").first
        if "00_wiki_demo" not in first.inner_text():
            raise RuntimeError("Demo voice must sort first for a private-data-free crop.")
        row_box = demo_row.bounding_box()
        shot("voices", int(row_box["y"] + row_box["height"]))

        # Use the visible library search; never replace job data with mock rows.
        page.set_viewport_size({"width": 1440, "height": 820})
        tab("jobs")
        page.locator("#lib-search").fill("Studio demo")
        job = page.locator(".lib-job").first
        job.locator(".lib-job-header").click()
        job.locator(".lib-item").filter(has_text="FINAL").first.click()
        page.wait_for_function("TabJobs._peaks && !TabJobs._loading")
        for effect in ["High-pass", "Compressor", "LUFS Normalize"]:
            page.locator("#fx-add").click()
            page.locator(".fx-add-menu-item").get_by_text(effect, exact=True).click()
        canvas = page.locator("#wf-canvas").bounding_box()
        y = canvas["y"] + canvas["height"] * .5
        page.mouse.move(canvas["x"] + canvas["width"] * .12, y)
        page.mouse.down()
        page.mouse.move(canvas["x"] + canvas["width"] * .34, y, steps=12)
        page.mouse.up()
        shot("editor")

        page.locator("#btn-save-as").click()
        page.locator("#save-name-input").fill("studio_demo_master")
        shot("editor-save")
        page.locator(".modal-card").get_by_role("button", name="Save", exact=True).click()
        page.wait_for_function("!TabJobs._renderInProgress && TabJobs._activeSource?.kind === 'edit'", timeout=120000)
        page.wait_for_function("TabJobs._peaks && !TabJobs._loading")
        shot("editor-rendered")

        if errors:
            raise RuntimeError("Browser errors: " + repr(errors))
        (scratch / "capture-result.json").write_text(json.dumps({
            "model": "kokoro", "voice": "af_heart", "device": worker["device"],
            "text": DEMO, "browser_errors": errors,
            "editor_source": page.evaluate("TabJobs._activeSource"),
        }, indent=2), encoding="utf-8")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:9300")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "manual" / "images")
    args = parser.parse_args()
    capture(args.url, args.output_dir)
