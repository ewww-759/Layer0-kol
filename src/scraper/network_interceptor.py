"""
network_interceptor.py
======================
Passive network traffic interceptor for Playwright browser contexts.

Registers an async ``on("response")`` listener on the BrowserContext that
silently captures specific GraphQL API responses in the background while
the user (or automation) browses Threads normally.

Design Philosophy
-----------------
This is a *passive sniffer*, not an active scraper. It sits behind the
browser like a one-way mirror:
  - It does NOT block, modify, or delay any request/response.
  - It only *reads* responses that match a strict whitelist of GraphQL
    operation fingerprints.
  - All non-matching traffic (images, CSS, JS bundles, ads, tracking
    pixels, fonts, videos) is silently ignored at zero cost.

Whitelist Strategy
------------------
A response is captured ONLY when ALL of these conditions are true:
  1. The URL contains ``graphql/query`` (Threads' GraphQL gateway).
  2. The response Content-Type is JSON (``application/json``).
  3. The originating request's POST body contains at least one of the
     registered operation fingerprints (e.g. ``BarcelonaThreadCommentsQuery``,
     ``BarcelonaReplyControlQuery``, ``BarcelonaProfileThreadsTabQuery``).

Public API
----------
  interceptor = NetworkInterceptor()

  # Register custom fingerprints if needed
  interceptor.add_fingerprint("MyCustomQuery")

  # Attach to browser context
  interceptor.attach(browser_context)

  # ... user browses / automation runs ...

  # Retrieve captured data
  captures = interceptor.get_captures()              # all
  captures = interceptor.get_captures("threads")     # by category
  interceptor.flush()                                # clear buffer
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from playwright.async_api import BrowserContext, Response

from .utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Resource types to skip (never even inspect the body)
# ---------------------------------------------------------------------------

_SKIP_RESOURCE_TYPES: Set[str] = {
    "image",
    "media",
    "font",
    "stylesheet",
    "manifest",
    "websocket",
    "texttrack",
    "eventsource",
    "other",
}

# URL substrings that indicate static/ad/tracking resources — fast pre-filter
_SKIP_URL_PATTERNS: list[str] = [
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".css", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".webm", ".m3u8",
    "/logging_client_events",
    "/adspixels/",
    "/tr/",                     # Facebook tracking pixel
    "analytics",
    "doubleclick.net",
    "googlesyndication",
    "facebook.com/ajax/bz",     # Meta internal beacon
]


# ---------------------------------------------------------------------------
# GraphQL operation fingerprints — the "whitelist"
# ---------------------------------------------------------------------------

class CaptureCategory(str, Enum):
    """Categories for organizing captured responses."""
    THREADS = "threads"          # User's thread/post feed
    COMMENTS = "comments"        # Thread comments / replies
    PROFILE = "profile"          # Profile information
    SEARCH = "search"            # Search results
    FOLLOWERS = "followers"      # Follower/following lists
    FEED = "feed"                # Home feed / recommended
    OTHER = "other"              # Matched fingerprint but uncategorized


# Each fingerprint maps to:
#   - The string to search for in the POST body / URL params
#   - The category it belongs to
@dataclass
class OperationFingerprint:
    """A GraphQL operation signature to watch for."""
    pattern: str                             # Substring to match in POST body
    category: CaptureCategory                # Logical grouping
    description: str = ""                    # Human-readable note


# Default fingerprint registry — covers known Threads GraphQL operations
_DEFAULT_FINGERPRINTS: list[OperationFingerprint] = [
    # --- Thread / Post content ---
    OperationFingerprint(
        "BarcelonaProfileThreadsTabQuery",
        CaptureCategory.THREADS,
        "User profile thread feed (paginated)",
    ),
    OperationFingerprint(
        "BarcelonaPostPageQuery",
        CaptureCategory.THREADS,
        "Single post detail view",
    ),
    OperationFingerprint(
        "BarcelonaFeedQuery",
        CaptureCategory.FEED,
        "Home feed / For You feed",
    ),

    # --- Comments / Replies ---
    OperationFingerprint(
        "BarcelonaThreadCommentsQuery",
        CaptureCategory.COMMENTS,
        "Top-level comments on a thread",
    ),
    OperationFingerprint(
        "BarcelonaReplyControlQuery",
        CaptureCategory.COMMENTS,
        "Reply permission and control metadata",
    ),
    OperationFingerprint(
        "BarcelonaReplyListQuery",
        CaptureCategory.COMMENTS,
        "Nested reply list under a comment",
    ),
    OperationFingerprint(
        "ThreadComments",
        CaptureCategory.COMMENTS,
        "Legacy thread comments query name",
    ),
    OperationFingerprint(
        "ReplyList",
        CaptureCategory.COMMENTS,
        "Legacy reply list query name",
    ),

    # --- Profile ---
    OperationFingerprint(
        "BarcelonaProfilePageQuery",
        CaptureCategory.PROFILE,
        "Full profile page data",
    ),
    OperationFingerprint(
        "BarcelonaProfileRepliesTabQuery",
        CaptureCategory.PROFILE,
        "Profile replies tab",
    ),

    # --- Search ---
    OperationFingerprint(
        "BarcelonaSearchResultsQuery",
        CaptureCategory.SEARCH,
        "Search results page",
    ),
    OperationFingerprint(
        "BarcelonaSearchAutocompleteQuery",
        CaptureCategory.SEARCH,
        "Search autocomplete suggestions",
    ),

    # --- Followers ---
    OperationFingerprint(
        "BarcelonaFollowersQuery",
        CaptureCategory.FOLLOWERS,
        "Follower list",
    ),
    OperationFingerprint(
        "BarcelonaFollowingQuery",
        CaptureCategory.FOLLOWERS,
        "Following list",
    ),
]


# ---------------------------------------------------------------------------
# Captured response data container
# ---------------------------------------------------------------------------

@dataclass
class CapturedResponse:
    """A single intercepted GraphQL response with metadata."""
    timestamp: float                         # Unix timestamp of capture
    url: str                                 # Full request URL
    category: CaptureCategory                # Which category matched
    matched_pattern: str                     # Which fingerprint triggered
    status: int                              # HTTP status code
    body: Dict[str, Any]                     # Parsed JSON response body
    request_post_data: Optional[str] = None  # Original POST body (if available)
    headers: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# NetworkInterceptor
# ---------------------------------------------------------------------------

class NetworkInterceptor:
    """
    Passive GraphQL response interceptor for Playwright BrowserContext.

    Attaches an ``on("response")`` listener that filters all browser traffic
    and only captures responses matching registered GraphQL fingerprints.
    All static resources (images, CSS, fonts, ads) are ignored.

    Thread-safe: uses asyncio.Lock for the capture buffer.
    """

    def __init__(
        self,
        *,
        max_buffer_size: int = 5000,
        on_capture: Optional[Callable[[CapturedResponse], None]] = None,
    ) -> None:
        """
        Args:
            max_buffer_size: Maximum number of responses to buffer before
                             the oldest are evicted (ring buffer behavior).
            on_capture:      Optional callback invoked immediately when a
                             response is captured. Useful for streaming to
                             a database or external consumer in real time.
        """
        self._fingerprints: list[OperationFingerprint] = list(_DEFAULT_FINGERPRINTS)
        self._captures: list[CapturedResponse] = []
        self._max_buffer = max_buffer_size
        self._on_capture = on_capture
        self._lock = asyncio.Lock()
        self._attached = False

        # Stats
        self._stats = {
            "total_responses_seen": 0,
            "skipped_resource_type": 0,
            "skipped_url_pattern": 0,
            "skipped_not_graphql": 0,
            "skipped_not_json": 0,
            "skipped_no_fingerprint": 0,
            "captured": 0,
            "errors": 0,
        }

        logger.info(
            f"NetworkInterceptor init | "
            f"fingerprints={len(self._fingerprints)} "
            f"buffer_size={max_buffer_size}"
        )

    # ------------------------------------------------------------------
    # Public API — Fingerprint management
    # ------------------------------------------------------------------

    def add_fingerprint(
        self,
        pattern: str,
        category: CaptureCategory = CaptureCategory.OTHER,
        description: str = "",
    ) -> None:
        """Register a new GraphQL operation fingerprint to watch for."""
        fp = OperationFingerprint(pattern, category, description)
        self._fingerprints.append(fp)
        logger.info(f"Added fingerprint: {pattern} [{category.value}]")

    def remove_fingerprint(self, pattern: str) -> bool:
        """Remove a fingerprint by pattern string. Returns True if found."""
        before = len(self._fingerprints)
        self._fingerprints = [
            fp for fp in self._fingerprints if fp.pattern != pattern
        ]
        removed = len(self._fingerprints) < before
        if removed:
            logger.info(f"Removed fingerprint: {pattern}")
        return removed

    def list_fingerprints(self) -> list[OperationFingerprint]:
        """Return a copy of the current fingerprint registry."""
        return list(self._fingerprints)

    # ------------------------------------------------------------------
    # Public API — Attach / Detach
    # ------------------------------------------------------------------

    def attach(self, context: BrowserContext) -> None:
        """
        Register the response interceptor on the given BrowserContext.
        Must be called AFTER the context is created and BEFORE navigation.
        """
        if self._attached:
            logger.warning("NetworkInterceptor already attached, skipping")
            return

        context.on("response", self._on_response)
        self._attached = True
        logger.info(
            f"NetworkInterceptor attached | "
            f"watching {len(self._fingerprints)} GraphQL patterns"
        )

    # ------------------------------------------------------------------
    # Public API — Retrieve captured data
    # ------------------------------------------------------------------

    async def get_captures(
        self,
        category: Optional[str] = None,
    ) -> list[CapturedResponse]:
        """
        Return captured responses, optionally filtered by category name.

        Args:
            category: Filter by category string (e.g. "threads", "comments").
                      Pass None to return all captures.
        """
        async with self._lock:
            if category is None:
                return list(self._captures)
            return [
                c for c in self._captures
                if c.category.value == category
            ]

    def get_captures_sync(
        self,
        category: Optional[str] = None,
    ) -> list[CapturedResponse]:
        """Synchronous non-async snapshot of current captures."""
        if category is None:
            return list(self._captures)
        return [
            c for c in self._captures
            if c.category.value == category
        ]

    async def get_captures_as_dicts(
        self,
        category: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
        """Return captures as plain dicts (JSON-serializable)."""
        captures = await self.get_captures(category)
        results = []
        for c in captures:
            results.append({
                "timestamp": c.timestamp,
                "url": c.url,
                "category": c.category.value,
                "matched_pattern": c.matched_pattern,
                "status": c.status,
                "body": c.body,
                "request_post_data": c.request_post_data,
            })
        return results

    async def flush(self) -> list[CapturedResponse]:
        """Return all captures and clear the buffer."""
        async with self._lock:
            flushed = list(self._captures)
            self._captures.clear()
            logger.info(f"Flushed {len(flushed)} captured responses")
            return flushed

    @property
    def capture_count(self) -> int:
        """Number of responses currently in the buffer."""
        return len(self._captures)

    @property
    def stats(self) -> Dict[str, int]:
        """Return a copy of the filtering statistics."""
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Internal — the core response handler
    # ------------------------------------------------------------------

    async def _on_response(self, response: Response) -> None:
        """
        Async callback invoked for EVERY response in the browser context.
        Applies a multi-stage filter pipeline to minimize overhead:

          Stage 1: Skip by resource type (image/css/font/media)
          Stage 2: Skip by URL pattern (static files, tracking pixels)
          Stage 3: Require "graphql/query" in URL
          Stage 4: Require JSON Content-Type
          Stage 5: Match POST body against fingerprint registry
        """
        self._stats["total_responses_seen"] += 1

        try:
            request = response.request

            # ── Stage 1: Skip unwanted resource types ──
            resource_type = request.resource_type
            if resource_type in _SKIP_RESOURCE_TYPES:
                self._stats["skipped_resource_type"] += 1
                return

            # ── Stage 2: Skip static/ad URLs via substring match ──
            url = response.url.lower()
            if any(pat in url for pat in _SKIP_URL_PATTERNS):
                self._stats["skipped_url_pattern"] += 1
                return

            # ── Stage 3: Must be a GraphQL query endpoint ──
            if "graphql/query" not in url and "graphql" not in url:
                self._stats["skipped_not_graphql"] += 1
                return

            # ── Stage 4: Must return JSON ──
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type and "text/json" not in content_type:
                self._stats["skipped_not_json"] += 1
                return

            # ── Stage 5: Match fingerprints in POST body or URL params ──
            post_data = request.post_data or ""
            search_blob = post_data + url  # Search both POST body and URL

            matched_fp: Optional[OperationFingerprint] = None
            for fp in self._fingerprints:
                if fp.pattern in search_blob:
                    matched_fp = fp
                    break

            if matched_fp is None:
                self._stats["skipped_no_fingerprint"] += 1
                return

            # ── All filters passed — capture this response ──
            try:
                body_bytes = await response.body()
                body_json = json.loads(body_bytes)
            except Exception:
                # Response body might be unavailable (e.g., navigated away)
                body_json = {"_error": "failed_to_parse_body"}

            captured = CapturedResponse(
                timestamp=time.time(),
                url=response.url,
                category=matched_fp.category,
                matched_pattern=matched_fp.pattern,
                status=response.status,
                body=body_json,
                request_post_data=post_data[:2000] if post_data else None,
                headers=dict(response.headers),
            )

            async with self._lock:
                self._captures.append(captured)
                # Evict oldest if buffer is full (ring buffer)
                if len(self._captures) > self._max_buffer:
                    evicted = len(self._captures) - self._max_buffer
                    self._captures = self._captures[evicted:]
                    logger.debug(f"Buffer full, evicted {evicted} oldest captures")

            self._stats["captured"] += 1

            logger.info(
                f"CAPTURED [{matched_fp.category.value}] "
                f"{matched_fp.pattern} | "
                f"status={response.status} "
                f"size={len(body_bytes) if body_bytes else 0}B"
            )

            # Invoke real-time callback if registered
            if self._on_capture:
                try:
                    self._on_capture(captured)
                except Exception as e:
                    logger.warning(f"on_capture callback error: {e}")

        except Exception as e:
            self._stats["errors"] += 1
            # Silently swallow — never crash the browser over an interceptor bug
            logger.debug(f"Interceptor error (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Debug / Introspection
    # ------------------------------------------------------------------

    def print_stats(self) -> None:
        """Log current filtering statistics."""
        logger.info("NetworkInterceptor stats:")
        for key, val in self._stats.items():
            logger.info(f"  {key}: {val}")
