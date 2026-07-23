"""
run_real_test.py
================
End-to-End Real Test for Phase 1 - 4 Pipeline Verification:
  Phase 1: Stealth Persistent Browser Launch
  Phase 2: Network Interceptor & GraphQL Fingerprint Whitelist
  Phase 3: Page Navigation & Human-Like Scroll Loop
  Phase 4: In-Memory JSON Comment Extraction & Circuit Breaker Flow Control

Usage:
    python tests/run_real_test.py
    python tests/run_real_test.py <url>
    python tests/run_real_test.py <url> --proxy direct
    python tests/run_real_test.py <url> --max-scrolls 20 --target-comments 50
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add src/ to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scraper.browser_manager import BrowserManager
from scraper.network_interceptor import NetworkInterceptor
from scraper.page_navigator import PageNavigator


async def run_end_to_end_test(
    url: str,
    proxy: str | None,
    headless: bool,
    max_scrolls: int,
    target_comments: int,
    stale_limit: int,
) -> None:
    config_dir = Path(__file__).resolve().parents[1] / "config"

    print("=" * 70)
    print("  LAYER0-KOL: END-TO-END PIPELINE REAL TEST (PHASES 1-4)")
    print("=" * 70)
    print(f"  Target URL       : {url}")
    print(f"  Proxy Mode       : {proxy or 'auto'}")
    print(f"  Headless Mode    : {headless}")
    print(f"  Max Scrolls      : {max_scrolls}")
    print(f"  Target Threshold : {target_comments} comments")
    print(f"  Stale Limit      : {stale_limit} consecutive scrolls")
    print("=" * 70)

    # ── Phase 1: Stealth Browser Launch ──
    print("\n[Phase 1: Initialization] Launching persistent stealth browser...")
    mgr = BrowserManager(
        config_dir,
        profile_name="real_test_profile",
        proxy=proxy,
        headless=headless,
    )
    page = await mgr.launch()
    print("  -> Phase 1 Complete: Stealth Chromium launched [OK]")

    # ── Phase 2: Routing & Interception ──
    print("\n[Phase 2: Routing & Interception] Attaching NetworkInterceptor...")
    interceptor = NetworkInterceptor()
    interceptor.attach(mgr.context)
    print(f"  -> Phase 2 Complete: Interceptor active, watching {len(interceptor.list_fingerprints())} GraphQL fingerprints [OK]")

    # ── Phase 3 & 4: Navigation, Scroll Loop, JSON Extraction & Circuit Breaker ──
    print(f"\n[Phase 3 & 4: Navigation & Flow Control] Navigating to {url}...")
    navigator = PageNavigator(page, interceptor=interceptor)

    result = await navigator.navigate_and_scroll(
        url,
        max_scrolls=max_scrolls,
        stale_limit=stale_limit,
        target_comments=target_comments,
    )

    print("\n" + "=" * 70)
    print("  PIPELINE EXECUTION SUMMARY REPORT")
    print("=" * 70)

    if result is None:
        print("  ❌ NAVIGATION FAILED: Could not load target URL.")
    else:
        print(f"  Scroll Ticks Performed   : {result.scrolls_performed} / {max_scrolls}")
        print(f"  Duration                 : {result.duration_s}s")
        print(f"  Total Pixels Scrolled    : {result.pixels_scrolled}px")
        print(f"  Final Document Height    : {result.final_scroll_height}px")
        print(f"  Stale Rounds             : {result.stale_rounds} / {stale_limit}")
        print(f"  GraphQL Responses Caught : {result.captures_during_scroll}")
        print(f"  Extracted Unique Comments: {result.extracted_comments_count}")
        print(f"  Circuit Breaker Stop     : {result.stop_reason}")

        if result.extracted_comments:
            print("\n  Sample Extracted Comments (JSON path without CSS classes):")
            for i, comment in enumerate(result.extracted_comments[:5], 1):
                user = comment.get("username") or "anonymous"
                text = comment.get("text", "").replace("\n", " ")[:60]
                likes = comment.get("like_count", 0)
                print(f"    {i}. [{user}] ({likes} likes): {text}")

    # ── Interceptor Telemetry Stats ──
    print("\n" + "-" * 70)
    print("  Network Interceptor Filtering Telemetry")
    print("-" * 70)
    stats = interceptor.stats
    print(f"  Total Responses Seen    : {stats['total_responses_seen']}")
    print(f"  Skipped (Resource Type) : {stats['skipped_resource_type']} (images, css, fonts ignored)")
    print(f"  Skipped (URL Pattern)   : {stats['skipped_url_pattern']} (ads, tracking pixels ignored)")
    print(f"  Skipped (Not GraphQL)   : {stats['skipped_not_graphql']}")
    print(f"  Skipped (Not JSON)      : {stats['skipped_not_json']}")
    print(f"  Skipped (No Fingerprint): {stats['skipped_no_fingerprint']}")
    print(f"  CAPTURED TARGET PACKETS : {stats['captured']}")

    print("\n" + "=" * 70)
    print("  REAL TEST COMPLETE [OK]")
    print("=" * 70)

    await mgr.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="End-to-end real test for Phases 1-4 pipeline")
    parser.add_argument("url", nargs="?", default="https://www.threads.net", help="Target URL")
    parser.add_argument("--proxy", default="direct", help="Proxy URL or 'direct'")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument("--max-scrolls", type=int, default=15, help="Max scroll ticks")
    parser.add_argument("--target-comments", type=int, default=50, help="Comment threshold for circuit breaker")
    parser.add_argument("--stale-limit", type=int, default=10, help="Stale scrolls limit for circuit breaker")
    args = parser.parse_args()

    asyncio.run(run_end_to_end_test(
        url=args.url,
        proxy=args.proxy,
        headless=args.headless,
        max_scrolls=args.max_scrolls,
        target_comments=args.target_comments,
        stale_limit=args.stale_limit,
    ))
