"""
data_processor.py
=================
Data processing & circuit breaker module for intercepted GraphQL packets.

Responsibilities
----------------
1. In-memory JSON extraction ("半路截胡"):
   - Parses intercepted raw GraphQL JSON payloads from Meta/Threads servers.
   - Navigates standard JSON paths (e.g. ``data -> containing_thread / reply_threads -> thread_items -> post -> caption / text``)
     to extract clean comment text, user info, timestamps, and engagement metrics.
   - Completely bypasses obfuscated CSS class names on the frontend.

2. Circuit Breaker / Flow Control ("熔断机制"):
   - Maintains real-time deduplicated comment counts.
   - Signals when the target comment count (e.g., 50 comments) has been reached.
   - Tracks consecutive scroll rounds without new comment packets to detect
     bottom-of-page or closed comments.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .network_interceptor import CapturedResponse, NetworkInterceptor
from .utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Extracted Comment Data Container
# ---------------------------------------------------------------------------

def _coerce_iso_datetime(value: Any) -> str:
    """Coerce timestamp (int/float/ISO string) into UTC ISO8601 string."""
    if not value:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        val_str = str(value)
        if val_str.isdigit():
            return datetime.fromtimestamp(float(val_str), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return datetime.fromisoformat(val_str.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# GraphQL JSON Comment Extractor
# ---------------------------------------------------------------------------

class GraphQLCommentExtractor:
    """
    Extracts structured comment records from Meta/Threads GraphQL JSON payloads.
    Directly navigates JSON schema paths:
      data -> containing_thread / reply_threads -> thread_items -> post -> caption / text
    """

    @classmethod
    def extract_from_json(cls, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parse a raw GraphQL response dict and return a list of normalized comment dicts.
        """
        if not isinstance(payload, dict):
            return []

        results: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        data = payload.get("data")
        if not isinstance(data, dict):
            return []

        # Path 1: data -> containing_thread -> thread_items / reply_threads
        containing_thread = data.get("containing_thread") or {}
        if isinstance(containing_thread, dict):
            cls._extract_from_thread_container(containing_thread, results, seen_ids)

        # Path 2: data -> reply_threads (top-level array in comments query)
        reply_threads = data.get("reply_threads")
        if isinstance(reply_threads, list):
            for thread in reply_threads:
                if isinstance(thread, dict):
                    cls._extract_from_thread_container(thread, results, seen_ids)

        # Path 3: data -> mediaData / thread_items / edges / nodes (generic fallback)
        edges = data.get("edges") or data.get("nodes")
        if isinstance(edges, list):
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else edge
                if isinstance(node, dict):
                    comment = cls._parse_post_dict(node)
                    if comment and comment["id"] not in seen_ids:
                        seen_ids.add(comment["id"])
                        results.append(comment)

        return results

    @classmethod
    def _extract_from_thread_container(
        cls,
        container: Dict[str, Any],
        results: List[Dict[str, Any]],
        seen_ids: Set[str],
    ) -> None:
        # Check thread_items
        items = container.get("thread_items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    post = item.get("post") or item
                    comment = cls._parse_post_dict(post)
                    if comment and comment["id"] not in seen_ids:
                        seen_ids.add(comment["id"])
                        results.append(comment)

        # Check nested reply_threads
        nested_replies = container.get("reply_threads")
        if isinstance(nested_replies, list):
            for sub_thread in nested_replies:
                if isinstance(sub_thread, dict):
                    cls._extract_from_thread_container(sub_thread, results, seen_ids)

    @classmethod
    def _parse_post_dict(cls, post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a post/comment dictionary into a standard comment record."""
        if not isinstance(post, dict):
            return None

        comment_id = post.get("id") or post.get("pk") or post.get("code")
        if not comment_id:
            return None

        # Extract text: post -> caption -> text OR post -> text
        text = ""
        caption = post.get("caption")
        if isinstance(caption, dict):
            text = caption.get("text") or ""
        elif isinstance(caption, str):
            text = caption
        elif isinstance(post.get("text"), str):
            text = post.get("text") or ""

        text = html.unescape(text).strip()
        if not text:
            return None

        # Extract user info
        user = post.get("user") or {}
        username = ""
        if isinstance(user, dict):
            username = user.get("username") or user.get("full_name") or ""

        # Extract engagement metrics
        like_count = post.get("like_count") or post.get("likes") or 0
        reply_count = post.get("text_post_app_info", {}).get("direct_reply_count") or post.get("reply_count") or 0

        # Extract timestamp
        ts = post.get("taken_at") or post.get("timestamp") or post.get("created_at")
        created_at = _coerce_iso_datetime(ts)

        return {
            "id": str(comment_id),
            "username": username,
            "text": text,
            "like_count": int(like_count or 0),
            "reply_count": int(reply_count or 0),
            "created_at": created_at,
        }


# ---------------------------------------------------------------------------
# DataProcessor & Circuit Breaker Manager
# ---------------------------------------------------------------------------

class DataProcessor:
    """
    Manages in-memory comment extraction and circuit breaking (熔断机制).

    Flow Control Rules:
      1. Threshold breach: Stop when extracted comment count >= target_threshold (e.g. 50).
      2. Stale count breach: Stop when stale_scrolls >= stale_limit (e.g. 10) without new comments.
    """

    def __init__(
        self,
        interceptor: NetworkInterceptor,
        *,
        target_comment_threshold: int = 50,
        stale_limit: int = 10,
    ) -> None:
        """
        Args:
            interceptor:               Attached NetworkInterceptor instance.
            target_comment_threshold: Maximum comments to collect before triggering circuit breaker.
            stale_limit:               Maximum consecutive scrolls without new comments.
        """
        self.interceptor = interceptor
        self.target_threshold = target_comment_threshold
        self.stale_limit = stale_limit

        self._comments_by_id: Dict[str, Dict[str, Any]] = {}
        self._last_processed_idx = 0
        self._stale_scrolls = 0

    def process_new_captures(self) -> List[Dict[str, Any]]:
        """
        Scan unprocessed captured responses from interceptor and extract comments.
        Returns newly added comment dicts.
        """
        captures = self.interceptor.get_captures_sync() if hasattr(self.interceptor, "get_captures_sync") else []
        new_comments: List[Dict[str, Any]] = []

        # Process captures since last index
        for cap in captures[self._last_processed_idx:]:
            if isinstance(cap.body, dict):
                extracted = GraphQLCommentExtractor.extract_from_json(cap.body)
                for comment in extracted:
                    cid = comment["id"]
                    if cid not in self._comments_by_id:
                        self._comments_by_id[cid] = comment
                        new_comments.append(comment)

        self._last_processed_idx = len(captures)

        if new_comments:
            self._stale_scrolls = 0
            logger.info(
                f"DataProcessor: extracted {len(new_comments)} new comments "
                f"(total unique: {len(self._comments_by_id)}/{self.target_threshold})"
            )
        return new_comments

    def record_scroll_tick(self, new_comments_found: int) -> None:
        """Record a scroll tick. Increment stale count if no new comments came in."""
        if new_comments_found <= 0:
            self._stale_scrolls += 1
            logger.debug(f"DataProcessor: stale scroll #{self._stale_scrolls}/{self.stale_limit}")
        else:
            self._stale_scrolls = 0

    def should_circuit_break(self) -> Optional[str]:
        """
        Check circuit breaker (熔断) conditions.

        Returns:
            Reason string if circuit breaker should trigger, None otherwise.
        """
        count = len(self._comments_by_id)
        if self.target_threshold > 0 and count >= self.target_threshold:
            reason = f"circuit_breaker: target threshold reached ({count}/{self.target_threshold} comments)"
            logger.info(reason)
            return reason

        if self.stale_limit > 0 and self._stale_scrolls >= self.stale_limit:
            reason = f"circuit_breaker: stale limit reached ({self._stale_scrolls}/{self.stale_limit} scrolls without new data)"
            logger.info(reason)
            return reason

        return None

    @property
    def comment_count(self) -> int:
        return len(self._comments_by_id)

    def get_all_comments(self) -> List[Dict[str, Any]]:
        return list(self._comments_by_id.values())
