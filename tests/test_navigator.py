"""
test_navigator.py
=================
Integration test for PageNavigator + NetworkInterceptor.
Navigates to a Threads post and triggers the scroll loop to capture
lazy-loaded GraphQL comment data.

Usage:
    python tests/test_navigator.py <post_url>
    python tests/test_navigator.py <post_url> --proxy direct
    python tests/test_navigator.py <post_url> --max-scrolls 20
    python tests/test_navigator.py <post_url> --headless

Example:
    python tests/test_navigator.py https://www.threads.net/@zuck/post/ABC123
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add src/ to the path so we can import the scraper package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper.browser_manager import BrowserManager
from scraper.network_interceptor import NetworkInterceptor
from scraper.page_navigator import PageNavigator


async def main(
    post_url: str,
    proxy: str | None,
    headless: bool,
    max_scrolls: int,
) -> None:
    config_dir = Path(__file__).resolve().parents[1] / "config"

    print("=" * 60)
    print("  PageNavigator Integration Test")
    print("=" * 60)
    print(f"  Target URL  : {post_url}")
    print(f"  Proxy       : {proxy or 'auto (env/config)'}")
    print(f"  Headless    : {headless}")
    print(f"  Max scrolls : {max_scrolls}")
    print("=" * 60)

    # --- Phase 1: Stealth browser launch ---
    mgr = BrowserManager(
        config_dir,
        profile_name="test_profile",
        proxy=proxy,
        headless=headless,
    )
    page = await mgr.launch()
    print("\n[Phase 1] Browser launched [OK]")

    # --- Phase 2: Attach interceptor ---
    interceptor = NetworkInterceptor()
    interceptor.attach(mgr.context)
    print(f"[Phase 2] Interceptor attached | watching {len(interceptor.list_fingerprints())} patterns [OK]")

    # --- Phase 3: Navigate + Scroll ---
    navigator = PageNavigator(page, interceptor=interceptor)

    print(f"\n[Phase 3] Navigating to post and starting scroll loop...")
    result = await navigator.navigate_and_scroll(
        post_url,
        max_scrolls=max_scrolls,
        stale_limit=5,
    )

    if result is None:
        print("  Navigation FAILED — could not load the page.")
    else:
        print(f"\n  Scroll Result:")
        print(f"    Scrolls performed  : {result.scrolls_performed}")
        print(f"    Duration           : {result.duration_s}s")
        print(f"    Pixels scrolled    : {result.pixels_scrolled}px")
        print(f"    Final page height  : {result.final_scroll_height}px")
        print(f"    Stale rounds       : {result.stale_rounds}")
        print(f"    Stop reason        : {result.stop_reason}")
        print(f"    GraphQL captures   : {result.captures_during_scroll}")

    # --- Summary: interceptor stats ---
    print("\n" + "-" * 60)
    print("  Network Interceptor Stats")
    print("-" * 60)
    stats = interceptor.stats
    print(f"    Total responses seen   : {stats['total_responses_seen']}")
    print(f"    Skipped (resource type): {stats['skipped_resource_type']}")
    print(f"    Skipped (URL pattern)  : {stats['skipped_url_pattern']}")
    print(f"    Skipped (not GraphQL)  : {stats['skipped_not_graphql']}")
    print(f"    Skipped (not JSON)     : {stats['skipped_not_json']}")
    print(f"    Skipped (no match)     : {stats['skipped_no_fingerprint']}")
    print(f"    CAPTURED               : {stats['captured']}")
    print(f"    Errors                 : {stats['errors']}")

    captures = await interceptor.get_captures()
    if captures:
        print(f"\n  Captured {len(captures)} GraphQL responses:")
        for i, cap in enumerate(captures[:10], 1):
            body_size = len(str(cap.body))
            print(
                f"    {i}. [{cap.category.value}] {cap.matched_pattern} "
                f"(HTTP {cap.status}, ~{body_size} chars)"
            )
        if len(captures) > 10:
            print(f"    ... and {len(captures) - 10} more")

    print("\n" + "=" * 60)
    print("  Test complete. Browser will stay open for inspection.")
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
    parser = argparse.ArgumentParser(
        description="PageNavigator + NetworkInterceptor integration test"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://www.threads.net",
        help="Target Threads post URL (default: homepage)",
    )
    parser.add_argument("--proxy", default=None, help="Proxy URL or 'direct'")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument(
        "--max-scrolls", type=int, default=30, help="Max scroll ticks"
    )
    args = parser.parse_args()

    asyncio.run(main(
        post_url=args.url,
        proxy=args.proxy,
        headless=args.headless,
        max_scrolls=args.max_scrolls,
    ))
