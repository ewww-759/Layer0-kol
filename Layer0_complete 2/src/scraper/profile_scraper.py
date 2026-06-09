"""
profile_scraper.py
==================
Fetches and enriches Threads account profile data for KOL identification.

Responsibilities
----------------
- Delegates all HTTP/GraphQL transport to ThreadsScraper (no duplicate sessions)
- Fetches follower count, following count, bio, verification, external URL
- Enriches raw profile data with derived signals useful for KOL scoring:
    · follower_tier          — nano / micro / mid / macro / mega
    · follower_following_ratio
    · bio_word_count
    · bio_has_link_cue       — detects "link in bio" style phrases
    · bio_has_contact_cue    — detects "DM / email / contact" phrases
    · bio_keywords           — extracted notable words from bio
    · account_type           — personal / creator / brand (heuristic)
    · has_external_url       — bool convenience flag
- Supports batch fetching of multiple usernames with rate-limit delays
- Offline mode returns rich synthetic profiles so downstream pipeline never breaks
- Caches results in-memory within a session to avoid redundant fetches

Public API (consumed by main.py orchestrator and monetization_scorer.py)
-------------------------------------------------------------------------
  profiler = ProfileScraper(scraper=threads_scraper_instance)

  profile  = profiler.fetch(username)                  → ProfileData dict
  profiles = profiler.fetch_many(usernames, delay=2.0) → List[ProfileData dict]
  profiler.export_json(profiles, path)                 → writes JSON file
  profiler.export_csv(profiles, path)                  → writes CSV file

ProfileData shape
-----------------
  # --- identity ---
  user_id              str
  username             str
  full_name            str
  is_verified          bool

  # --- bio ---
  bio                  str      raw biography text
  bio_word_count       int
  bio_has_link_cue     bool     e.g. "link in bio", "check bio"
  bio_has_contact_cue  bool     e.g. "DM me", "email:", "contact:"
  bio_keywords         List[str]

  # --- link ---
  external_url         str      raw URL from profile (may be empty)
  has_external_url     bool

  # --- audience ---
  follower_count       int
  following_count      int
  follower_tier        str      nano | micro | mid | macro | mega
  follower_following_ratio  float

  # --- heuristic classification ---
  account_type         str      personal | creator | brand

  # --- meta ---
  profile_pic_url      str
  fetch_status         str      ok | empty | error
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Bio signal patterns
# ---------------------------------------------------------------------------

# Phrases that indicate "link in bio" style monetization CTAs in the bio itself
_LINK_CUE_PATTERNS: List[str] = [
    r"link\s+in\s+bio",
    r"check\s+(my\s+)?bio",
    r"bio\s+link",
    r"linktree",
    r"linktr\.ee",
    r"tap\s+(the\s+)?link",
    r"click\s+(the\s+)?link",
    r"see\s+link",
    r"visit\s+link",
]

# Phrases that indicate direct contact / outreach willingness
_CONTACT_CUE_PATTERNS: List[str] = [
    r"\bdm\s+(me|for|us)\b",
    r"\bdm[s]?\s+open\b",
    r"\bemail\s*[:\-]",
    r"\bcontact\s*[:\-]",
    r"\bcollabs?\b",
    r"\bpartnerships?\b",
    r"\bbusiness\s+inquir",
    r"\bbook\s+(me|a\s+call)\b",
    r"\bhire\s+me\b",
    r"\bwork\s+with\s+(me|us)\b",
]

# Words that strongly suggest a creator / brand account
_CREATOR_SIGNALS: List[str] = [
    "creator", "influencer", "content", "coach", "consultant",
    "author", "speaker", "founder", "ceo", "entrepreneur",
    "photographer", "filmmaker", "artist", "designer", "writer",
    "podcast", "youtube", "newsletter", "course", "community",
]

_BRAND_SIGNALS: List[str] = [
    "official", "store", "shop", "brand", "company", "agency",
    "inc", "llc", "ltd", "co.", "corp", "media", "group", "hq",
]

# Follower tier thresholds (industry-standard definitions)
_TIER_THRESHOLDS = [
    ("mega",  1_000_000),
    ("macro",   100_000),
    ("mid",      10_000),
    ("micro",     1_000),
    ("nano",          0),   # catch-all
]


# ---------------------------------------------------------------------------
# ProfileScraper
# ---------------------------------------------------------------------------

class ProfileScraper:
    """
    Fetches and enriches Threads account profile data.

    Parameters
    ----------
    scraper : ThreadsScraper instance
        Provides fetch_user_profile() — all HTTP/session/proxy logic lives there.
        ProfileScraper never opens its own HTTP session.
    delay   : float
        Seconds to wait between consecutive profile fetches in fetch_many().
        Overridable per-call. Default 2.0s is conservative but safe.
    """

    def __init__(self, scraper: Any, delay: float = 2.0) -> None:
        self._scraper = scraper
        self._default_delay = delay
        # in-session cache: username → ProfileData dict
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, username: str) -> Dict[str, Any]:
        """
        Fetch and enrich the profile for a single username.

        Returns a ProfileData dict. On any error, returns an empty profile
        with fetch_status="error" so the pipeline never crashes.
        """
        username = username.lstrip("@").strip().lower()

        if username in self._cache:
            logger.debug(f"[profile] Cache hit for @{username}")
            return self._cache[username]

        logger.info(f"[profile] Fetching @{username}")
        try:
            raw = self._scraper.fetch_user_profile(username)
            if not raw or not raw.get("username"):
                logger.warning(f"[profile] Empty response for @{username}")
                result = self._empty_profile(username, status="empty")
            else:
                result = self._enrich(raw)
                result["fetch_status"] = "ok"
        except Exception as e:
            logger.exception(f"[profile] Failed to fetch @{username}: {e}")
            result = self._empty_profile(username, status="error")

        self._cache[username] = result
        return result

    def fetch_many(
        self,
        usernames: List[str],
        delay: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch profiles for a list of usernames with polite delays between calls.

        Parameters
        ----------
        usernames : list of Threads usernames (with or without @)
        delay     : seconds between fetches (defaults to self._default_delay)

        Returns a list of ProfileData dicts in the same order as `usernames`.
        """
        pause = delay if delay is not None else self._default_delay
        results: List[Dict[str, Any]] = []

        for i, username in enumerate(usernames):
            profile = self.fetch(username)
            results.append(profile)
            # Skip delay after the last username
            if i < len(usernames) - 1:
                logger.debug(f"[profile] Waiting {pause}s before next fetch...")
                time.sleep(pause)

        logger.info(
            f"[profile] Fetched {len(results)} profiles | "
            f"ok={sum(1 for r in results if r['fetch_status'] == 'ok')} "
            f"empty={sum(1 for r in results if r['fetch_status'] == 'empty')} "
            f"error={sum(1 for r in results if r['fetch_status'] == 'error')}"
        )
        return results

    def export_json(
        self, profiles: List[Dict[str, Any]], path: Path
    ) -> Path:
        """Write a list of ProfileData dicts to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
        logger.info(f"[profile] Exported {len(profiles)} profiles → {path}")
        return path

    def export_csv(
        self, profiles: List[Dict[str, Any]], path: Path
    ) -> Path:
        """Write a list of ProfileData dicts to a CSV file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not profiles:
            logger.warning("[profile] export_csv called with empty list — writing header only")

        fieldnames = list(_EMPTY_PROFILE_TEMPLATE.keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for p in profiles:
                # bio_keywords is a list — flatten to pipe-separated string for CSV
                row = dict(p)
                row["bio_keywords"] = " | ".join(p.get("bio_keywords") or [])
                writer.writerow(row)
        logger.info(f"[profile] Exported {len(profiles)} profiles → {path}")
        return path

    def clear_cache(self) -> None:
        """Flush the in-memory cache (useful in long-running batch jobs)."""
        self._cache.clear()
        logger.debug("[profile] Cache cleared")

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------

    def _enrich(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Take a raw profile dict from ThreadsScraper._normalize_profile()
        and add all derived KOL-scoring signals.
        """
        bio: str = (raw.get("bio") or "").strip()
        follower_count: int = int(raw.get("follower_count") or 0)
        following_count: int = int(raw.get("following_count") or 0)
        external_url: str = raw.get("external_url") or ""

        return {
            # --- identity ---
            "user_id":              raw.get("user_id", ""),
            "username":             raw.get("username", ""),
            "full_name":            raw.get("full_name", ""),
            "is_verified":          bool(raw.get("is_verified", False)),

            # --- bio ---
            "bio":                  bio,
            "bio_word_count":       self._word_count(bio),
            "bio_has_link_cue":     self._match_any(bio, _LINK_CUE_PATTERNS),
            "bio_has_contact_cue":  self._match_any(bio, _CONTACT_CUE_PATTERNS),
            "bio_keywords":         self._extract_bio_keywords(bio),

            # --- link ---
            "external_url":         external_url,
            "has_external_url":     bool(external_url),

            # --- audience ---
            "follower_count":           follower_count,
            "following_count":          following_count,
            "follower_tier":            self._follower_tier(follower_count),
            "follower_following_ratio": self._ff_ratio(follower_count, following_count),

            # --- heuristic classification ---
            "account_type":         self._classify_account(
                                        raw.get("full_name", ""),
                                        bio,
                                        raw.get("is_verified", False),
                                    ),

            # --- meta ---
            "profile_pic_url":      raw.get("profile_pic_url", ""),
            "fetch_status":         "ok",
        }

    # ------------------------------------------------------------------
    # Bio signal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _word_count(text: str) -> int:
        """Count whitespace-delimited words in a string."""
        return len(text.split()) if text else 0

    @staticmethod
    def _match_any(text: str, patterns: List[str]) -> bool:
        """Return True if any regex pattern matches the text (case-insensitive)."""
        lowered = text.lower()
        return any(re.search(p, lowered) for p in patterns)

    @staticmethod
    def _extract_bio_keywords(bio: str) -> List[str]:
        """
        Extract notable single keywords from the bio.

        Strategy:
          1. Tokenise to lowercase words (strip punctuation).
          2. Remove very short tokens (≤2 chars) and common stop-words.
          3. Return words that appear in the creator/brand signal lists
             plus any hashtags present.
        """
        if not bio:
            return []

        # Pull hashtags first (preserve # prefix)
        hashtags = re.findall(r"#\w+", bio)

        # Tokenise remainder
        tokens = re.sub(r"[^\w\s]", " ", bio.lower()).split()

        _STOPWORDS = {
            "a", "an", "the", "and", "or", "but", "in", "on", "at",
            "to", "for", "of", "with", "is", "am", "are", "i", "my",
            "me", "we", "our", "you", "your", "it", "its", "this",
            "that", "be", "by", "from", "as", "was", "im",
        }

        all_signals = set(_CREATOR_SIGNALS + _BRAND_SIGNALS)
        keywords = [
            t for t in tokens
            if len(t) > 2
            and t not in _STOPWORDS
            and t in all_signals
        ]

        # Deduplicate while preserving order
        seen: set = set()
        unique_keywords: List[str] = []
        for kw in keywords + hashtags:
            if kw not in seen:
                seen.add(kw)
                unique_keywords.append(kw)

        return unique_keywords

    # ------------------------------------------------------------------
    # Audience helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _follower_tier(follower_count: int) -> str:
        """
        Map a raw follower count to an industry-standard tier label.

        Thresholds (standard influencer marketing definitions):
          mega  : 1M+
          macro : 100K – 999K
          mid   : 10K – 99K
          micro : 1K – 9K
          nano  : < 1K
        """
        for tier, threshold in _TIER_THRESHOLDS:
            if follower_count >= threshold:
                return tier
        return "nano"

    @staticmethod
    def _ff_ratio(followers: int, following: int) -> float:
        """
        Follower-to-following ratio.
        A high ratio (>> 1.0) indicates genuine audience pull.
        Returns 0.0 when following is 0 to avoid division error.
        Capped at 9999.0 to prevent absurd values for accounts following nobody.
        """
        if following == 0:
            return 9999.0 if followers > 0 else 0.0
        ratio = round(followers / following, 4)
        return min(ratio, 9999.0)

    # ------------------------------------------------------------------
    # Account-type classifier
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_account(
        full_name: str,
        bio: str,
        is_verified: bool,
    ) -> str:
        """
        Heuristic classification of account type.

        Logic (in priority order):
          1. brand   — bio or full_name contains brand signals
          2. creator — bio or full_name contains creator signals,
                       OR account is verified (verified = typically public figure / creator)
          3. personal — default
        """
        combined = (full_name + " " + bio).lower()

        if any(sig in combined for sig in _BRAND_SIGNALS):
            return "brand"
        if is_verified or any(sig in combined for sig in _CREATOR_SIGNALS):
            return "creator"
        return "personal"

    # ------------------------------------------------------------------
    # Empty / error profile
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_profile(username: str, status: str = "empty") -> Dict[str, Any]:
        """Return a zero-value ProfileData dict with a given fetch_status."""
        result = dict(_EMPTY_PROFILE_TEMPLATE)
        result["username"] = username
        result["fetch_status"] = status
        return result


# ---------------------------------------------------------------------------
# Empty profile template (single source of truth for field list)
# Used by: _empty_profile(), export_csv() fieldnames
# ---------------------------------------------------------------------------

_EMPTY_PROFILE_TEMPLATE: Dict[str, Any] = {
    # identity
    "user_id":                  "",
    "username":                 "",
    "full_name":                "",
    "is_verified":              False,
    # bio
    "bio":                      "",
    "bio_word_count":           0,
    "bio_has_link_cue":         False,
    "bio_has_contact_cue":      False,
    "bio_keywords":             [],
    # link
    "external_url":             "",
    "has_external_url":         False,
    # audience
    "follower_count":           0,
    "following_count":          0,
    "follower_tier":            "nano",
    "follower_following_ratio": 0.0,
    # classification
    "account_type":             "personal",
    # meta
    "profile_pic_url":          "",
    "fetch_status":             "empty",
}
