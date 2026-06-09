"""
comment_scraper.py
==================
Fetches and enriches comment (reply) data for Threads posts.

Responsibilities
----------------
- Delegates all HTTP transport to ThreadsScraper.fetch_post_replies()
- Enriches raw reply dicts with text-level signals:
    · sentiment_label     — positive / neutral / negative (keyword heuristic)
    · contains_question   — bool, commenter is asking something
    · contains_cta_echo   — bool, commenter echoes a CTA ("where's the link?")
    · word_count          — int
- Aggregates per-post comment stats into a CommentSummary dict:
    · total_comments      — int
    · avg_likes           — float
    · sentiment_breakdown — {positive, neutral, negative} counts
    · question_count      — int, how many commenters asked questions
    · cta_echo_count      — int, signals audience acted on a CTA
    · top_comments        — List[EnrichedComment], top N by like_count
- Supports batch fetching across multiple posts with rate-limit delays
- Offline mode returns realistic synthetic comments so pipeline never breaks

Public API (consumed by main.py orchestrator)
---------------------------------------------
  cs = CommentScraper(scraper=threads_scraper_instance)

  # Enrich a single post's comments
  summary = cs.fetch_for_post(post_id, post_text)   → CommentSummary dict

  # Enrich all posts for an account in one call
  enriched = cs.fetch_for_posts(posts, delay=1.5)   → List[post dict + "comment_summary"]

  cs.export_json(enriched, path)
  cs.export_csv(enriched, path)                      → flat CSV, one row per post

CommentSummary shape
--------------------
  post_id              str
  total_comments       int
  avg_likes            float
  sentiment_breakdown  Dict[str, int]   {positive, neutral, negative}
  question_count       int
  cta_echo_count       int
  top_comments         List[EnrichedComment dict]
  fetch_status         str    ok | empty | error

EnrichedComment shape
---------------------
  id           str
  username     str
  text         str
  like_count   int
  created_at   str
  word_count   int
  sentiment_label    str   positive | neutral | negative
  contains_question  bool
  contains_cta_echo  bool
"""

from __future__ import annotations

import csv
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Sentiment keyword lists (lightweight heuristic — no ML dependency)
# ---------------------------------------------------------------------------

_POSITIVE_PATTERNS: List[str] = [
    r"\blove\b", r"\bgreat\b", r"\bamazing\b", r"\bawesome\b",
    r"\bthank(s|you)\b", r"\bperfect\b", r"\bexcellent\b",
    r"\bincredible\b", r"\bfire\b", r"\bgoat\b", r"\blegit\b",
    r"\bhelpful\b", r"\binspir", r"\bwow\b", r"\byes+\b",
    r"\bgood\b", r"\bnice\b", r"\bbeautiful\b", r"\bsick\b",
    r"❤", r"🔥", r"💯", r"🙌", r"👏", r"😍",
]

_NEGATIVE_PATTERNS: List[str] = [
    r"\bbad\b", r"\bterrible\b", r"\bawful\b", r"\bwrong\b",
    r"\bscam\b", r"\bfake\b", r"\bdisappoint", r"\bwaste\b",
    r"\boverpriced\b", r"\bstop\b", r"\bunfollow\b", r"\bblock\b",
    r"\bannoy", r"\bcringe\b", r"\bno+\b", r"\bnot\s+good\b",
    r"😡", r"🤮", r"👎", r"😒",
]

# Phrases that indicate the commenter is echoing / responding to a CTA
_CTA_ECHO_PATTERNS: List[str] = [
    r"where.?s\s+the\s+link",
    r"what.?s\s+the\s+link",
    r"link\s+(please|pls|\?)",
    r"how\s+(do\s+i|can\s+i)\s+(get|buy|order|sign)",
    r"dm.?d\s+you",
    r"just\s+dm.?d",
    r"sent\s+a\s+dm",
    r"entering\s+(this\s+)?giveaway",
    r"i\s+want\s+(this|one|in)",
    r"how\s+much",
    r"where\s+to\s+buy",
    r"is\s+this\s+available",
    r"code\s+(still\s+)?work",
    r"use\s+code",
]

_MAX_TOP_COMMENTS = 5   # top comments to preserve per post


# ---------------------------------------------------------------------------
# CommentScraper
# ---------------------------------------------------------------------------

class CommentScraper:
    """
    Fetches and enriches Threads post comments.

    Parameters
    ----------
    scraper : ThreadsScraper instance
        Provides fetch_post_replies(post_id) — all HTTP lives there.
    delay   : float
        Seconds between consecutive post comment fetches in fetch_for_posts().
    max_comments : int
        Max replies to fetch and analyse per post. Default 50.
    """

    def __init__(
        self,
        scraper: Any,
        delay: float = 1.5,
        max_comments: int = 50,
    ) -> None:
        self._scraper    = scraper
        self._delay      = delay
        self._max        = max_comments
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_for_post(
        self,
        post_id: str,
        post_text: str = "",
    ) -> Dict[str, Any]:
        """
        Fetch and enrich comments for a single post_id.

        Parameters
        ----------
        post_id   : Threads post ID string
        post_text : original post text — used to detect CTA echo in replies

        Returns a CommentSummary dict.
        """
        if post_id in self._cache:
            logger.debug(f"[comments] Cache hit for post {post_id}")
            return self._cache[post_id]

        logger.info(f"[comments] Fetching replies for post {post_id}")
        try:
            raw_replies = self._scraper.fetch_post_replies(post_id)
            raw_replies = raw_replies[: self._max]

            if not raw_replies:
                result = self._empty_summary(post_id, status="empty")
            else:
                enriched = [self._enrich_comment(r) for r in raw_replies]
                enriched = [e for e in enriched if e]
                result   = self._aggregate(post_id, enriched)
                result["fetch_status"] = "ok"

        except Exception as e:
            logger.exception(f"[comments] Failed for post {post_id}: {e}")
            result = self._empty_summary(post_id, status="error")

        self._cache[post_id] = result
        return result

    def fetch_for_posts(
        self,
        posts: List[Dict[str, Any]],
        delay: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch comments for every post in the list, attaching a
        'comment_summary' key to each post dict.

        Parameters
        ----------
        posts : list of parsed post dicts from ThreadsParser
                (must contain 'id' and 'text' keys)
        delay : seconds between fetches (defaults to self._delay)

        Returns the same list with each dict extended by:
            post["comment_summary"] = CommentSummary dict
        """
        pause  = delay if delay is not None else self._delay
        result = []

        for i, post in enumerate(posts):
            pid  = str(post.get("id") or "")
            text = post.get("text") or ""

            if not pid:
                logger.warning("[comments] Post missing 'id', skipping")
                enriched_post = dict(post)
                enriched_post["comment_summary"] = self._empty_summary("", status="no_id")
                result.append(enriched_post)
                continue

            summary = self.fetch_for_post(pid, text)
            enriched_post = dict(post)
            enriched_post["comment_summary"] = summary
            result.append(enriched_post)

            if i < len(posts) - 1:
                time.sleep(pause)

        ok    = sum(1 for p in result if p["comment_summary"]["fetch_status"] == "ok")
        empty = sum(1 for p in result if p["comment_summary"]["fetch_status"] == "empty")
        err   = sum(1 for p in result if p["comment_summary"]["fetch_status"] == "error")
        logger.info(
            f"[comments] fetch_for_posts done | "
            f"posts={len(result)} ok={ok} empty={empty} error={err}"
        )
        return result

    def export_json(
        self, enriched_posts: List[Dict[str, Any]], path: Path
    ) -> Path:
        """Write enriched post list (with comment_summary) to JSON."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(enriched_posts, f, ensure_ascii=False, indent=2)
        logger.info(f"[comments] Exported {len(enriched_posts)} posts → {path}")
        return path

    def export_csv(
        self, enriched_posts: List[Dict[str, Any]], path: Path
    ) -> Path:
        """
        Write a flat CSV — one row per post, comment_summary fields
        are flattened into columns.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "id", "username", "text", "like_count", "reply_count",
            "repost_count", "created_at", "url",
            "cs_total_comments", "cs_avg_likes",
            "cs_positive", "cs_neutral", "cs_negative",
            "cs_question_count", "cs_cta_echo_count",
            "cs_fetch_status",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for post in enriched_posts:
                cs = post.get("comment_summary") or {}
                sb = cs.get("sentiment_breakdown") or {}
                row = {
                    "id":               post.get("id", ""),
                    "username":         post.get("username", ""),
                    "text":             post.get("text", ""),
                    "like_count":       post.get("like_count", 0),
                    "reply_count":      post.get("reply_count", 0),
                    "repost_count":     post.get("repost_count", 0),
                    "created_at":       post.get("created_at", ""),
                    "url":              post.get("url", ""),
                    "cs_total_comments":  cs.get("total_comments", 0),
                    "cs_avg_likes":       round(cs.get("avg_likes", 0.0), 2),
                    "cs_positive":        sb.get("positive", 0),
                    "cs_neutral":         sb.get("neutral", 0),
                    "cs_negative":        sb.get("negative", 0),
                    "cs_question_count":  cs.get("question_count", 0),
                    "cs_cta_echo_count":  cs.get("cta_echo_count", 0),
                    "cs_fetch_status":    cs.get("fetch_status", ""),
                }
                writer.writerow(row)
        logger.info(f"[comments] Exported CSV → {path}")
        return path

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------
    # Comment enrichment
    # ------------------------------------------------------------------

    def _enrich_comment(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Enrich a single raw reply dict with text-level signals."""
        try:
            text = (raw.get("text") or "").strip()
            return {
                "id":               str(raw.get("id") or ""),
                "username":         raw.get("username", ""),
                "text":             text,
                "like_count":       int(raw.get("like_count") or 0),
                "created_at":       raw.get("created_at", ""),
                "word_count":       len(text.split()) if text else 0,
                "sentiment_label":  self._sentiment(text),
                "contains_question": self._is_question(text),
                "contains_cta_echo": self._is_cta_echo(text),
            }
        except Exception as e:
            logger.debug(f"[comments] _enrich_comment failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate(
        self, post_id: str, comments: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Aggregate a list of enriched comments into a CommentSummary."""
        n = len(comments)
        avg_likes = sum(c["like_count"] for c in comments) / n if n else 0.0

        sentiment_breakdown = {
            "positive": sum(1 for c in comments if c["sentiment_label"] == "positive"),
            "neutral":  sum(1 for c in comments if c["sentiment_label"] == "neutral"),
            "negative": sum(1 for c in comments if c["sentiment_label"] == "negative"),
        }

        top = sorted(comments, key=lambda c: c["like_count"], reverse=True)
        top_comments = top[:_MAX_TOP_COMMENTS]

        return {
            "post_id":              post_id,
            "total_comments":       n,
            "avg_likes":            round(avg_likes, 2),
            "sentiment_breakdown":  sentiment_breakdown,
            "question_count":       sum(1 for c in comments if c["contains_question"]),
            "cta_echo_count":       sum(1 for c in comments if c["contains_cta_echo"]),
            "top_comments":         top_comments,
            "fetch_status":         "ok",
        }

    # ------------------------------------------------------------------
    # Text signal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sentiment(text: str) -> str:
        """
        Classify a comment as positive / negative / neutral using
        keyword pattern matching. Returns the label with more hits;
        defaults to neutral on a tie or no match.
        """
        low = text.lower()
        pos = sum(1 for p in _POSITIVE_PATTERNS if re.search(p, low))
        neg = sum(1 for p in _NEGATIVE_PATTERNS if re.search(p, low))
        if pos > neg:
            return "positive"
        if neg > pos:
            return "negative"
        return "neutral"

    @staticmethod
    def _is_question(text: str) -> bool:
        """Return True if the comment contains a question."""
        return "?" in text or bool(re.search(
            r"\b(how|what|where|when|why|who|which|can\s+i|is\s+this|does\s+this)\b",
            text.lower()
        ))

    @staticmethod
    def _is_cta_echo(text: str) -> bool:
        """Return True if the comment echoes or responds to a CTA."""
        low = text.lower()
        return any(re.search(p, low) for p in _CTA_ECHO_PATTERNS)

    # ------------------------------------------------------------------
    # Empty / error summary
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_summary(post_id: str, status: str = "empty") -> Dict[str, Any]:
        return {
            "post_id":             post_id,
            "total_comments":      0,
            "avg_likes":           0.0,
            "sentiment_breakdown": {"positive": 0, "neutral": 0, "negative": 0},
            "question_count":      0,
            "cta_echo_count":      0,
            "top_comments":        [],
            "fetch_status":        status,
        }
