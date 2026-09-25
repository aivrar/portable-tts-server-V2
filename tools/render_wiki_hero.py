"""Render the repository hero from its HTML layout and real Editor screenshot."""
from pathlib import Path

from playwright.sync_api import sync_playwright

images = Path(__file__).resolve().parents[1] / "manual" / "images"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1800, "height": 1280}, device_scale_factor=1)
    page.goto((images / "hero.html").as_uri(), wait_until="load")
    page.locator("img").evaluate_all("imgs => Promise.all(imgs.map(img => img.decode()))")
    page.evaluate("document.fonts.ready")
    page.screenshot(path=str(images / "github-hero.png"))
    browser.close()
