"""
storage_handover.py
===================
Phase 5: Storage Alignment & Resource Handover module.

Responsibilities
----------------
1. Structured Alignment ("结构化对齐"):
   - Packages extracted raw comment data into standardized, normalized dictionaries:
     ``post_id``, ``comment_id``, ``username``, ``comment_text``, ``like_count``, ``reply_count``, ``timestamp``.
   - Packages post audit summaries into structured records ready for SQLite database insertion or downstream pipeline handover:
     ``post_id``, ``post_url``, ``fetch_status``, ``total_comments_captured``, ``comments``, ``audit_timestamp``, ``interceptor_stats``, ``error_message``.

2. Resource Cleanup & Graceful Degradation ("资源回收与优雅降级"):
   - Resource Recycling: Safely closes individual page tabs (Page) after each post session and flushes ``NetworkInterceptor`` buffer to prevent memory leaks across batch scraping runs.
   - Fault Tolerance / Exception Safety:
     Catches network timeouts, HTTP 403/429/rate-limit blocks, Playwright errors, and generic exceptions.
     If no GraphQL packets are captured or an error occurs, marks the post as ``"no_comments_captured"``, ``"timeout"``, ``"blocked"``, or ``"error"``,
     and gracefully completes without crashing the orchestrator process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

from .browser_manager import BrowserManager
from .network_interceptor import NetworkInterceptor
from .page_navigator import PageNavigator, ScrollResult
from .utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Structured Comment Entity
# ---------------------------------------------------------------------------

@dataclass
class StructuredComment:
    """Structured comment entity aligned for SQLite database & pipeline handover."""
    post_id: str
    comment_id: str
    username: str
    comment_text: str
    like_count: int
    reply_count: int
    timestamp: str  # ISO8601 UTC timestamp string

    def to_dict(self) -> Dict[str, Any]:
        """Return standardized dict representation."""
        return {
            "post_id": self.post_id,
            "comment_id": self.comment_id,
            "username": self.username,
            "comment_text": self.comment_text,
            "like_count": self.like_count,
            "reply_count": self.reply_count,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Structured Post Audit Record
# ---------------------------------------------------------------------------

@dataclass
class StructuredPostAuditRecord:
    """Structured post audit summary aligned for storage and handover."""
    post_id: str
    post_url: str
    fetch_status: str  # "ok" | "no_comments_captured" | "blocked" | "timeout" | "error"
    total_comments_captured: int
    comments: List[Dict[str, Any]] = field(default_factory=list)
    audit_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    interceptor_stats: Dict[str, int] = field(default_factory=dict)
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return JSON-serializable structured dictionary."""
        return {
            "post_id": self.post_id,
            "post_url": self.post_url,
            "fetch_status": self.fetch_status,
            "total_comments_captured": self.total_comments_captured,
            "comments": self.comments,
            "audit_timestamp": self.audit_timestamp,
            "interceptor_stats": self.interceptor_stats,
            "error_message": self.error_message,
        }

    def to_sqlite_rows(self) -> List[Dict[str, Any]]:
        """Return list of comment rows ready for SQLite table insertion."""
        rows = []
        for c in self.comments:
            rows.append({
                "post_id": self.post_id,
                "comment_id": c.get("comment_id") or c.get("id"),
                "username": c.get("username", ""),
                "comment_text": c.get("comment_text") or c.get("text", ""),
                "like_count": c.get("like_count", 0),
                "reply_count": c.get("reply_count", 0),
                "timestamp": c.get("timestamp") or c.get("created_at", ""),
            })
        return rows


# ---------------------------------------------------------------------------
# Post Audit Session & Lifecycle Manager
# ---------------------------------------------------------------------------

class PostAuditSession:
    """
    Manages the lifecycle of auditing a Threads post (Phases 1 - 5).

    Features:
      - Opens a dedicated Page tab per post and guarantees tab cleanup.
      - Intercepts & extracts GraphQL comments into structured dictionaries.
      - Clears interceptor buffers on session completion.
      - Handles timeouts, HTTP blocks, and missing packets gracefully.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        interceptor: NetworkInterceptor,
    ) -> None:
        """
        Args:
            browser_manager: Initialized BrowserManager instance.
            interceptor:     Attached NetworkInterceptor instance.
        """
        self._browser_mgr = browser_manager
        self._interceptor = interceptor

    async def audit_post(
        self,
        post_url: str,
        *,
        post_id: Optional[str] = None,
        max_scrolls: int = 80,
        stale_limit: int = 10,
        target_comments: int = 50,
        timeout_ms: int = 30000,
    ) -> StructuredPostAuditRecord:
        """
        Audit a Threads post URL with full lifecycle management & fault tolerance.

        Guarantees:
          1. Page tab is always closed in finally block.
          2. Interceptor buffer is flushed in finally block.
          3. Returns StructuredPostAuditRecord without crashing on errors or 0 captures.
        """
        resolved_post_id = post_id or self._extract_post_id_from_url(post_url)

        context: Optional[BrowserContext] = self._browser_mgr.context
        if not context:
            logger.error("BrowserManager context is not initialized!")
            return StructuredPostAuditRecord(
                post_id=resolved_post_id,
                post_url=post_url,
                fetch_status="error",
                total_comments_captured=0,
                comments=[],
                error_message="Browser context not initialized",
                interceptor_stats=self._interceptor.stats,
            )

        page: Optional[Page] = None
        fetch_status = "error"
        extracted_comments: List[Dict[str, Any]] = []
        error_msg: Optional[str] = None

        try:
            # 1. Open dedicated page tab for this post
            logger.info(f"Phase 5: Opening page tab for post audit | url={post_url}")
            page = await context.new_page()

            # Ensure interceptor is attached to context
            self._interceptor.attach(context)

            # 2. Instantiate navigator and execute navigate + scroll loop
            navigator = PageNavigator(page, interceptor=self._interceptor)
            scroll_result: Optional[ScrollResult] = await navigator.navigate_and_scroll(
                post_url,
                timeout_ms=timeout_ms,
                max_scrolls=max_scrolls,
                stale_limit=stale_limit,
                target_comments=target_comments,
            )

            if scroll_result is None:
                fetch_status = "no_comments_captured"
                error_msg = "Navigation returned no response or page failed to load"
                logger.warning(f"Post audit completed with status: {fetch_status}")
            else:
                raw_comments = scroll_result.extracted_comments
                if raw_comments:
                    fetch_status = "ok"
                    for item in raw_comments:
                        sc = StructuredComment(
                            post_id=resolved_post_id,
                            comment_id=str(item.get("id", "")),
                            username=item.get("username", ""),
                            comment_text=item.get("text", ""),
                            like_count=int(item.get("like_count", 0)),
                            reply_count=int(item.get("reply_count", 0)),
                            timestamp=item.get("created_at", ""),
                        )
                        extracted_comments.append(sc.to_dict())
                else:
                    fetch_status = "no_comments_captured"
                    logger.info(
                        f"Post audit finished with 0 captured comments. "
                        f"Reason: {scroll_result.stop_reason}"
                    )

        except (PlaywrightTimeoutError, asyncio.TimeoutError) as e:
            fetch_status = "timeout"
            error_msg = f"Network or navigation timeout: {e}"
            logger.warning(f"Graceful degradation | post_id={resolved_post_id} status={fetch_status}")

        except PlaywrightError as e:
            err_str = str(e).lower()
            if "blocked" in err_str or "403" in err_str or "429" in err_str or "access denied" in err_str:
                fetch_status = "blocked"
                error_msg = f"Account or IP restriction detected: {e}"
            else:
                fetch_status = "error"
                error_msg = f"Playwright execution error: {e}"
            logger.warning(f"Graceful degradation | post_id={resolved_post_id} status={fetch_status}")

        except Exception as e:
            fetch_status = "error"
            error_msg = f"Unhandled error during post audit: {e}"
            logger.error(f"Graceful degradation | post_id={resolved_post_id} error={e}")

        finally:
            # ── Resource Cleanup & Recycling ──
            if page:
                try:
                    await page.close()
                    logger.debug(f"Closed page tab for post {resolved_post_id}")
                except Exception as e:
                    logger.warning(f"Failed to close page tab cleanly: {e}")

            # Flush interceptor buffer for next session
            try:
                await self._interceptor.flush()
                logger.debug("Flushed NetworkInterceptor buffer")
            except Exception as e:
                logger.warning(f"Failed to flush interceptor: {e}")

        # Construct and return structured record (never crashes main process)
        record = StructuredPostAuditRecord(
            post_id=resolved_post_id,
            post_url=post_url,
            fetch_status=fetch_status,
            total_comments_captured=len(extracted_comments),
            comments=extracted_comments,
            interceptor_stats=self._interceptor.stats,
            error_message=error_msg,
        )

        logger.info(
            f"Handover record ready | post_id={resolved_post_id} "
            f"status={fetch_status} comments={len(extracted_comments)}"
        )
        return record

    @staticmethod
    def _extract_post_id_from_url(url: str) -> str:
        """Helper to derive a post ID/code from a Threads URL."""
        parts = [p for p in url.split("/") if p]
        if "post" in parts:
            idx = parts.index("post")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        return parts[-1] if parts else "unknown_post"
