"""
test_browser.py
===============
Quick smoke test for BrowserManager stealth launch.
Navigates to a fingerprint detection site to verify anti-detection patches.

Usage:
    python tests/test_browser.py
    python tests/test_browser.py --proxy http://127.0.0.1:10808
    python tests/test_browser.py --headless
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add src/ to the path so we can import the scraper package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper.browser_manager import BrowserManager


async def main(proxy: str | None, headless: bool) -> None:
    config_dir = Path(__file__).resolve().parents[1] / "config"

    print("=" * 60)
    print("  BrowserManager Stealth Smoke Test")
    print("=" * 60)
    print(f"  Profile dir : config/browser_profiles/test_profile")
    print(f"  Proxy       : {proxy or 'none'}")
    print(f"  Headless    : {headless}")
    print("=" * 60)

    mgr = BrowserManager(
        config_dir,
        profile_name="test_profile",
        proxy=proxy,
        headless=headless,
    )

    page = await mgr.launch()

    # Test 1: Check navigator.webdriver is hidden
    print("\n[Test 1] navigator.webdriver ...")
    webdriver_val = await page.evaluate("() => navigator.webdriver")
    status = "PASS" if webdriver_val is None or webdriver_val is False else "FAIL"
    print(f"  Result: {webdriver_val}  [{status}]")

    # Test 2: Check chrome runtime object exists
    print("\n[Test 2] window.chrome.runtime ...")
    chrome_exists = await page.evaluate("() => !!window.chrome && !!window.chrome.runtime")
    status = "PASS" if chrome_exists else "FAIL"
    print(f"  Result: {chrome_exists}  [{status}]")

    # Test 3: Check navigator.plugins length
    print("\n[Test 3] navigator.plugins.length ...")
    plugins_len = await page.evaluate("() => navigator.plugins.length")
    status = "PASS" if plugins_len >= 3 else "FAIL"
    print(f"  Result: {plugins_len}  [{status}]")

    # Test 4: Check navigator.languages
    print("\n[Test 4] navigator.languages ...")
    languages = await page.evaluate("() => navigator.languages")
    status = "PASS" if languages and len(languages) > 0 else "FAIL"
    print(f"  Result: {languages}  [{status}]")

    # Test 5: Navigate to a real page
    print("\n[Test 5] Navigating to https://www.threads.net ...")
    try:
        resp = await page.goto(
            "https://www.threads.net",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        status_code = resp.status if resp else "no response"
        status = "PASS" if resp and resp.status == 200 else "WARN"
        print(f"  HTTP Status: {status_code}  [{status}]")
        print(f"  Page title : {await page.title()}")
    except Exception as e:
        print(f"  Navigation error: {e}  [FAIL]")

    print("\n" + "=" * 60)
    print("  Smoke test complete. Browser will stay open for inspection.")
    print("  Press Ctrl+C to close.")
    print("=" * 60)

    # Keep browser open so user can inspect
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await mgr.close()
        print("Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BrowserManager smoke test")
    parser.add_argument("--proxy", default=None, help="Proxy URL")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    args = parser.parse_args()

    asyncio.run(main(proxy=args.proxy, headless=args.headless))
