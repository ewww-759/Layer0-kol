"""
page_navigator.py
=================
Handles target page navigation and human-like scroll automation for
triggering lazy-loaded content (comments, replies) on Threads.

Design Philosophy
-----------------
Threads loads comments incrementally via infinite scroll. The browser must
simulate realistic human scrolling behavior to coax the server into
delivering successive pages of GraphQL data. Each scroll tick:
  1. Scrolls down 400-600px (randomized).
  2. Pauses 0.5-1.5s (randomized reading interval).
  3. Checks if new content has loaded (scroll height changed).
  4. Repeats until a configurable stop condition is met.

Stop conditions (any one triggers exit):
  - ``max_scrolls`` reached (hard cap, prevents infinite loops).
  - ``max_duration_s`` elapsed since scroll loop started.
  - Scroll height stops growing for ``stale_rounds`` consecutive ticks
    (meaning no more content to load).
  - NetworkInterceptor has captured ``target_captures`` new responses
    since the scroll began (enough data collected).

Public API
----------
  navigator = PageNavigator(page)

  # Navigate to a specific post
  await navigator.goto("https://www.threads.net/@user/post/ABC123")

  # Trigger lazy-loading via scroll loop
  result = await navigator.scroll_for_comments()

  # Or combine both in one call
  result = await navigator.navigate_and_scroll(
      "https://www.threads.net/@user/post/ABC123",
      interceptor=my_interceptor,
  )
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from playwright.async_api import Page, Response

from .data_processor import DataProcessor
from .network_interceptor import NetworkInterceptor
from .utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Scroll result container
# ---------------------------------------------------------------------------

@dataclass
class ScrollResult:
    """Summary of a completed scroll session."""
    scrolls_performed: int            # Total scroll ticks executed
    duration_s: float                 # Wall-clock time spent scrolling
    pixels_scrolled: int              # Cumulative pixels scrolled down
    final_scroll_height: int          # Page scroll height when stopped
    stale_rounds: int                 # Consecutive rounds with no new content
    stop_reason: str                  # Why the loop exited
    captures_during_scroll: int       # GraphQL responses captured during scroll
    extracted_comments_count: int = 0 # Unique comments extracted via JSON path
    extracted_comments: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Default scroll parameters
# ---------------------------------------------------------------------------

_DEFAULT_SCROLL_PX_MIN = 400
_DEFAULT_SCROLL_PX_MAX = 600
_DEFAULT_PAUSE_MIN = 0.5
_DEFAULT_PAUSE_MAX = 1.5
_DEFAULT_MAX_SCROLLS = 80
_DEFAULT_MAX_DURATION_S = 120.0
_DEFAULT_STALE_LIMIT = 10          # Stop if 10 consecutive scrolls yield no new data
_DEFAULT_TARGET_COMMENTS = 50      # Circuit breaker threshold: 50 comments
_DEFAULT_TARGET_CAPTURES = 0       # 0 = disabled (don't stop on capture count)


# ---------------------------------------------------------------------------
# PageNavigator
# ---------------------------------------------------------------------------

class PageNavigator:
    """
    Handles page navigation and human-like scrolling for Playwright Pages.

    Works with NetworkInterceptor to provide intelligent scroll termination
    based on captured GraphQL response count.
    """

    def __init__(
        self,
        page: Page,
        *,
        interceptor: Optional[NetworkInterceptor] = None,
    ) -> None:
        """
        Args:
            page:        An active Playwright Page object.
            interceptor: Optional NetworkInterceptor for capture-aware scrolling.
        """
        self._page = page
        self._interceptor = interceptor
        logger.info("PageNavigator initialized")

    # ------------------------------------------------------------------
    # Public API — Navigation
    # ------------------------------------------------------------------

    async def goto(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 30000,
        settle_ms: int = 2000,
    ) -> Optional[Response]:
        """
        Navigate to a URL and wait for the page to settle.

        Args:
            url:        Target URL (e.g. a Threads post URL).
            wait_until: Playwright wait condition ('domcontentloaded',
                        'load', 'networkidle').
            timeout_ms: Navigation timeout in milliseconds.
            settle_ms:  Extra wait after navigation for JS rendering
                        to complete (SPA hydration time).

        Returns:
            The Playwright Response object, or None if navigation failed.
        """
        logger.info(f"Navigating to: {url}")
        try:
            response = await self._page.goto(
                url,
                wait_until=wait_until,
                timeout=timeout_ms,
            )
            status = response.status if response else "no response"
            logger.info(f"Navigation complete | status={status}")

            # Allow SPA framework (React) to hydrate and render
            if settle_ms > 0:
                await asyncio.sleep(settle_ms / 1000.0)
                logger.debug(f"Settled for {settle_ms}ms after navigation")

            return response

        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Public API — Scroll loop
    # ------------------------------------------------------------------

    async def scroll_for_comments(
        self,
        *,
        scroll_px_min: int = _DEFAULT_SCROLL_PX_MIN,
        scroll_px_max: int = _DEFAULT_SCROLL_PX_MAX,
        pause_min: float = _DEFAULT_PAUSE_MIN,
        pause_max: float = _DEFAULT_PAUSE_MAX,
        max_scrolls: int = _DEFAULT_MAX_SCROLLS,
        max_duration_s: float = _DEFAULT_MAX_DURATION_S,
        stale_limit: int = _DEFAULT_STALE_LIMIT,
        target_comments: int = _DEFAULT_TARGET_COMMENTS,
        target_captures: int = _DEFAULT_TARGET_CAPTURES,
    ) -> ScrollResult:
        """
        Execute a human-like scroll loop to trigger lazy-loading of comments.

        Each tick scrolls a random distance (400-600px), pauses for a
        random interval (0.5-1.5s), then checks whether new content has
        appeared. The loop exits when any stop condition or circuit breaker is met.

        Args:
            scroll_px_min:    Minimum pixels per scroll tick.
            scroll_px_max:    Maximum pixels per scroll tick.
            pause_min:        Minimum pause between ticks (seconds).
            pause_max:        Maximum pause between ticks (seconds).
            max_scrolls:      Hard cap on total scroll ticks.
            max_duration_s:   Maximum wall-clock time for the loop.
            stale_limit:      Stop after this many consecutive ticks without new comment data (default 10).
            target_comments:  Circuit breaker threshold: stop when unique comment count reaches this number (default 50).
            target_captures:  Stop after the interceptor captures this many new responses (0 = disabled).

        Returns:
            A ScrollResult summarizing the scroll session and extracted comments.
        """
        logger.info(
            f"Starting scroll loop | "
            f"px=[{scroll_px_min}-{scroll_px_max}] "
            f"pause=[{pause_min}-{pause_max}]s "
            f"max_scrolls={max_scrolls} max_time={max_duration_s}s "
            f"stale_limit={stale_limit} target_comments={target_comments}"
        )

        processor: Optional[DataProcessor] = None
        if self._interceptor:
            processor = DataProcessor(
                self._interceptor,
                target_comment_threshold=target_comments,
                stale_limit=stale_limit,
            )

        # Snapshot interceptor capture count at start
        captures_at_start = 0
        if self._interceptor and target_captures > 0:
            captures_at_start = self._interceptor.capture_count

        start_time = time.time()
        total_px = 0
        stale_count = 0
        prev_height = await self._get_scroll_height()
        stop_reason = "max_scrolls"
        tick = 0

        for tick in range(1, max_scrolls + 1):
            # --- Check time limit ---
            elapsed = time.time() - start_time
            if elapsed >= max_duration_s:
                stop_reason = f"max_duration ({max_duration_s}s)"
                logger.info(f"Scroll stopped: time limit reached ({elapsed:.1f}s)")
                break

            # --- Randomized scroll distance ---
            scroll_px = random.randint(scroll_px_min, scroll_px_max)

            # Occasionally do a micro-scroll (human hesitation behavior)
            if random.random() < 0.08:
                scroll_px = random.randint(80, 200)

            await self._page.evaluate(f"window.scrollBy(0, {scroll_px})")
            total_px += scroll_px

            # --- Randomized pause (simulate reading) ---
            pause = random.uniform(pause_min, pause_max)

            # Occasionally do a longer pause (human distraction / reading)
            if random.random() < 0.05:
                pause += random.uniform(1.0, 3.0)

            await asyncio.sleep(pause)

            # --- Process intercepted GraphQL packets & update flow control ---
            new_comments_found = 0
            if processor:
                new_items = processor.process_new_captures()
                new_comments_found = len(new_items)
                processor.record_scroll_tick(new_comments_found)

                # Check flow control circuit breaker (50 comments threshold OR 10 stale scrolls)
                cb_reason = processor.should_circuit_break()
                if cb_reason:
                    stop_reason = cb_reason
                    logger.info(f"Scroll stopped by circuit breaker: {stop_reason}")
                    break

            # --- Check if DOM scroll height loaded ---
            current_height = await self._get_scroll_height()
            if current_height > prev_height:
                stale_count = 0
                prev_height = current_height
                logger.debug(
                    f"Scroll #{tick}: +{scroll_px}px → new height={current_height} "
                    f"(content loaded)"
                )
            else:
                stale_count += 1
                logger.debug(
                    f"Scroll #{tick}: +{scroll_px}px → height unchanged "
                    f"(stale {stale_count}/{stale_limit})"
                )

            # --- Fallback check for DOM stale limit if processor not active ---
            if not processor and stale_count >= stale_limit:
                stop_reason = f"stale_limit ({stale_limit} consecutive)"
                logger.info(f"Scroll stopped: no new content for {stale_limit} rounds")
                break

            # --- Check capture target ---
            if self._interceptor and target_captures > 0:
                new_captures = self._interceptor.capture_count - captures_at_start
                if new_captures >= target_captures:
                    stop_reason = f"target_captures ({new_captures}/{target_captures})"
                    logger.info(
                        f"Scroll stopped: captured {new_captures} responses "
                        f"(target={target_captures})"
                    )
                    break

            # --- Periodic progress log ---
            if tick % 10 == 0:
                elapsed = time.time() - start_time
                captures_so_far = 0
                extracted_so_far = processor.comment_count if processor else 0
                if self._interceptor:
                    captures_so_far = self._interceptor.capture_count - captures_at_start
                logger.info(
                    f"Scroll progress | tick={tick}/{max_scrolls} "
                    f"px={total_px} elapsed={elapsed:.1f}s "
                    f"captures={captures_so_far} extracted_comments={extracted_so_far}"
                )

        # --- Build result ---
        duration = time.time() - start_time
        final_height = await self._get_scroll_height()
        captures_total = 0
        extracted_all: List[Dict[str, Any]] = []
        if self._interceptor:
            captures_total = self._interceptor.capture_count - captures_at_start
        if processor:
            extracted_all = processor.get_all_comments()

        result = ScrollResult(
            scrolls_performed=tick,
            duration_s=round(duration, 2),
            pixels_scrolled=total_px,
            final_scroll_height=final_height,
            stale_rounds=stale_count,
            stop_reason=stop_reason,
            captures_during_scroll=captures_total,
            extracted_comments_count=len(extracted_all),
            extracted_comments=extracted_all,
        )

        logger.info(
            f"Scroll complete | scrolls={result.scrolls_performed} "
            f"duration={result.duration_s}s pixels={result.pixels_scrolled} "
            f"captures={result.captures_during_scroll} "
            f"extracted_comments={result.extracted_comments_count} "
            f"reason={result.stop_reason}"
        )

        return result

    # ------------------------------------------------------------------
    # Public API — Combined navigate + scroll
    # ------------------------------------------------------------------

    async def navigate_and_scroll(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 30000,
        settle_ms: int = 2000,
        scroll_px_min: int = _DEFAULT_SCROLL_PX_MIN,
        scroll_px_max: int = _DEFAULT_SCROLL_PX_MAX,
        pause_min: float = _DEFAULT_PAUSE_MIN,
        pause_max: float = _DEFAULT_PAUSE_MAX,
        max_scrolls: int = _DEFAULT_MAX_SCROLLS,
        max_duration_s: float = _DEFAULT_MAX_DURATION_S,
        stale_limit: int = _DEFAULT_STALE_LIMIT,
        target_comments: int = _DEFAULT_TARGET_COMMENTS,
        target_captures: int = _DEFAULT_TARGET_CAPTURES,
    ) -> Optional[ScrollResult]:
        """
        Navigate to a post URL, then trigger the scroll loop.

        Convenience method combining goto() + scroll_for_comments().

        Returns:
            ScrollResult on success, None if navigation failed.
        """
        response = await self.goto(
            url,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            settle_ms=settle_ms,
        )

        if response is None:
            logger.error("Aborting scroll: navigation failed")
            return None

        if response.status >= 400:
            logger.warning(
                f"Page returned HTTP {response.status}, "
                f"scroll may not yield results"
            )

        return await self.scroll_for_comments(
            scroll_px_min=scroll_px_min,
            scroll_px_max=scroll_px_max,
            pause_min=pause_min,
            pause_max=pause_max,
            max_scrolls=max_scrolls,
            max_duration_s=max_duration_s,
            stale_limit=stale_limit,
            target_comments=target_comments,
            target_captures=target_captures,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_scroll_height(self) -> int:
        """Get current document scroll height in pixels."""
        return await self._page.evaluate("document.documentElement.scrollHeight")
