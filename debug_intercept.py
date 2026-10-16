"""
Debug script: loads cookies, opens newspapers.com/clippings in a VISIBLE
browser, logs every JSON network response, and saves the page HTML.
Run: .venv/Scripts/python debug_intercept.py
"""
import json
import time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from scraper.auth import cookies_as_playwright_list, USER_AGENT
from playwright.sync_api import sync_playwright

COOKIES_FILE = "cookies.txt"
OUTPUT_FILE = "debug_responses.json"

captured = []

def handle_response(response):
    url = response.url
    ct = response.headers.get("content-type", "")
    status = response.status
    if "json" in ct:
        try:
            body = response.json()
            captured.append({"url": url, "status": status, "body": body})
            print(f"  JSON {status}: {url}")
        except Exception:
            pass
    elif status >= 400:
        print(f"  ERROR {status}: {url}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)  # visible window
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )
    pw_cookies = cookies_as_playwright_list(COOKIES_FILE)
    print(f"Loaded {len(pw_cookies)} cookies")
    context.add_cookies(pw_cookies)

    page = context.new_page()
    page.on("response", handle_response)

    print("Navigating to clippings page...")
    page.goto("https://www.newspapers.com/clippings/", wait_until="networkidle", timeout=60_000)

    print(f"\nPage title: {page.title()}")
    print(f"Final URL:  {page.url}")

    # Save HTML for inspection
    Path("debug_page.html").write_text(page.content(), encoding="utf-8")
    print("Page HTML saved to debug_page.html")

    # Scroll to trigger any lazy-loaded content
    print("Scrolling to trigger lazy loads...")
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 800)")
        page.wait_for_timeout(1500)

    print(f"\nCaptured {len(captured)} JSON responses total.")
    for c in captured:
        print(f"  [{c['status']}] {c['url']}")

    # Save all captured responses
    Path(OUTPUT_FILE).write_text(json.dumps(captured, indent=2, default=str), encoding="utf-8")
    print(f"\nAll JSON responses saved to {OUTPUT_FILE}")

    print("\nBrowser staying open for 10s so you can inspect it...")
    time.sleep(10)
    browser.close()
