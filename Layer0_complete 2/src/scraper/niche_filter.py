"""
niche_filter.py
===============
Confirms that scraped accounts genuinely belong to the target niche
before they reach MonetizationScorer.

Sits between the scraper layer and the scoring layer:

    SeedDiscoverer  →  ThreadsScraper  →  NicheFilter  →  MonetizationScorer

Without this filter, keyword search results include off-topic accounts
that happen to mention a niche word once. This module enforces a
minimum evidence threshold before an account enters the scoring pipeline.

Filtering strategy (two-signal AND gate)
-----------------------------------------
An account passes if BOTH of these are true:

  1. Bio signal   : bio text contains >= min_bio_keyword_hits
                    words from the niche bio_keywords list

  2. Post signal  : across all scraped posts, the total count of
                    niche post_keyword matches >= min_post_keyword_hits

Either threshold can be set to 0 in niche_config.yaml to disable
that signal (e.g. min_bio_keyword_hits: 0 to filter on posts only).

Public API
----------
  nf = NicheFilter(config_path=Path("config/niche_config.yaml"))

  passes, evidence = nf.passes(profile, posts)
  → passes  : bool
  → evidence: {bio_hits, post_hits, matched_bio_kws, matched_post_kws}

  qualified = nf.filter_many(account_list)
  → returns only accounts that pass, with evidence attached

  nf.explain(profile, posts)
  → prints a human-readable pass/fail breakdown to stdout (debug aid)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from .utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults — used when no niche_config.yaml is provided
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG: Dict[str, Any] = {
    "niche":                   "general",
    "bio_keywords":            [],
    "post_keywords":           [],
    "min_bio_keyword_hits":    1,
    "min_post_keyword_hits":   2,
    "case_sensitive":          False,
}


# ---------------------------------------------------------------------------
# NicheFilter
# ---------------------------------------------------------------------------

class NicheFilter:
    """
    Filters scraped accounts to those that genuinely match a target niche.

    Parameters
    ----------
    config_path : Path to niche_config.yaml
                  Must contain bio_keywords and/or post_keywords.
                  All threshold keys are optional with sensible defaults.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._cfg = dict(_DEFAULT_CONFIG)

        if config_path and Path(config_path).exists():
            self._load_config(Path(config_path))

        self._niche          = self._cfg.get("niche", "general")
        self._bio_kws        = [k.lower() for k in self._cfg.get("bio_keywords", [])]
        self._post_kws       = [k.lower() for k in self._cfg.get("post_keywords", [])]
        self._min_bio_hits   = int(self._cfg.get("min_bio_keyword_hits", 1))
        self._min_post_hits  = int(self._cfg.get("min_post_keyword_hits", 2))
        self._case_sensitive = bool(self._cfg.get("case_sensitive", False))

        logger.info(
            f"NicheFilter ready | niche='{self._niche}' "
            f"bio_kws={len(self._bio_kws)} "
            f"post_kws={len(self._post_kws)} "
            f"min_bio={self._min_bio_hits} "
            f"min_post={self._min_post_hits}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def passes(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Check whether an account passes the niche filter.

        Parameters
        ----------
        profile : ProfileData dict from ProfileScraper.fetch()
        posts   : list of parsed post dicts from ThreadsParser

        Returns
        -------
        (passes, evidence) where evidence contains:
            bio_hits         int   — number of bio keyword matches
            post_hits        int   — total post keyword matches across all posts
            matched_bio_kws  list  — which bio keywords were found
            matched_post_kws list  — which post keywords were found
            bio_passes       bool
            post_passes      bool
        """
        bio_hits,  matched_bio  = self._check_bio(profile)
        post_hits, matched_post = self._check_posts(posts)

        bio_passes  = bio_hits  >= self._min_bio_hits  if self._min_bio_hits  > 0 else True
        post_passes = post_hits >= self._min_post_hits if self._min_post_hits > 0 else True

        passes = bio_passes and post_passes

        evidence = {
            "bio_hits":         bio_hits,
            "post_hits":        post_hits,
            "matched_bio_kws":  matched_bio,
            "matched_post_kws": matched_post,
            "bio_passes":       bio_passes,
            "post_passes":      post_passes,
        }
        return passes, evidence

    def filter_many(
        self,
        account_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Filter a list of account dicts to only niche-matching accounts.

        Parameters
        ----------
        account_list : list of dicts each with keys:
            "profile" → ProfileData dict
            "posts"   → List of parsed post dicts

        Returns the filtered list with each dict extended by:
            account["niche_evidence"] = evidence dict from passes()

        Logs a summary of how many accounts passed/failed.
        """
        passed  = []
        n_total = len(account_list)

        for account in account_list:
            profile  = account.get("profile", {})
            posts    = account.get("posts", [])
            username = profile.get("username", "unknown")

            ok, evidence = self.passes(profile, posts)
            account["niche_evidence"] = evidence

            if ok:
                passed.append(account)
                logger.debug(
                    f"[niche] ✓ @{username} passed | "
                    f"bio_hits={evidence['bio_hits']} "
                    f"post_hits={evidence['post_hits']} "
                    f"matched_bio={evidence['matched_bio_kws']}"
                )
            else:
                logger.debug(
                    f"[niche] ✗ @{username} filtered | "
                    f"bio_hits={evidence['bio_hits']}/{self._min_bio_hits} "
                    f"post_hits={evidence['post_hits']}/{self._min_post_hits}"
                )

        n_pass = len(passed)
        n_fail = n_total - n_pass
        logger.info(
            f"[niche] filter_many: {n_pass}/{n_total} passed "
            f"({n_fail} filtered as off-niche) | niche='{self._niche}'"
        )
        return passed

    def explain(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
    ) -> None:
        """
        Print a human-readable pass/fail breakdown to stdout.
        Useful during development to understand why an account passes or fails.
        """
        username        = profile.get("username", "unknown")
        passes, evidence = self.passes(profile, posts)

        print(f"\n{'='*55}")
        print(f"  Niche filter: @{username} — niche='{self._niche}'")
        print(f"{'='*55}")
        print(f"  Overall: {'✓ PASS' if passes else '✗ FAIL'}")
        print()
        print(f"  Bio check   : {evidence['bio_hits']} hits "
              f"(need {self._min_bio_hits}) "
              f"→ {'✓' if evidence['bio_passes'] else '✗'}")
        if evidence["matched_bio_kws"]:
            print(f"    matched: {evidence['matched_bio_kws']}")
        print()
        print(f"  Post check  : {evidence['post_hits']} hits "
              f"(need {self._min_post_hits}) "
              f"→ {'✓' if evidence['post_passes'] else '✗'}")
        if evidence["matched_post_kws"]:
            print(f"    matched: {evidence['matched_post_kws']}")
        print(f"{'='*55}\n")

    # ------------------------------------------------------------------
    # Signal checkers
    # ------------------------------------------------------------------

    def _check_bio(
        self, profile: Dict[str, Any]
    ) -> Tuple[int, List[str]]:
        """
        Count how many niche bio_keywords appear in the profile bio.
        Also checks full_name and username for extra signal.

        Returns (hit_count, matched_keywords).
        """
        if not self._bio_kws:
            return 0, []

        # Combine bio + full_name + username for matching surface
        bio      = (profile.get("bio") or "").strip()
        fullname = (profile.get("full_name") or "").strip()
        username = (profile.get("username") or "").strip()
        text     = f"{bio} {fullname} {username}"

        return self._count_keyword_hits(text, self._bio_kws)

    def _check_posts(
        self, posts: List[Dict[str, Any]]
    ) -> Tuple[int, List[str]]:
        """
        Count total niche post_keyword hits across all post texts.
        Each unique keyword is counted once per post to avoid one
        repetitive post dominating the score.

        Returns (total_hit_count, unique_matched_keywords).
        """
        if not self._post_kws or not posts:
            return 0, []

        total_hits     = 0
        all_matched: Set[str] = set()

        for post in posts:
            text = (post.get("text") or "").strip()
            hits, matched = self._count_keyword_hits(text, self._post_kws)
            total_hits += hits
            all_matched.update(matched)

        return total_hits, sorted(all_matched)

    def _count_keyword_hits(
        self, text: str, keywords: List[str]
    ) -> Tuple[int, List[str]]:
        """
        Count how many keywords from the list appear in text.
        Uses whole-word matching to avoid partial hits
        (e.g. "astro" matching "astronaut").
        Multi-word keywords (e.g. "yi ching") use substring match.

        Returns (hit_count, list_of_matched_keywords).
        """
        if not text:
            return 0, []

        search_text = text if self._case_sensitive else text.lower()
        hits        = 0
        matched     = []

        for kw in keywords:
            kw_search = kw if self._case_sensitive else kw.lower()

            if " " in kw_search:
                # Multi-word keyword: substring match
                if kw_search in search_text:
                    hits += 1
                    matched.append(kw)
            else:
                # Single-word keyword: whole-word boundary match
                pattern = rf"\b{re.escape(kw_search)}\b"
                if re.search(pattern, search_text):
                    hits += 1
                    matched.append(kw)

        return hits, matched

    # ------------------------------------------------------------------
    # Config loader
    # ------------------------------------------------------------------

    def _load_config(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            self._cfg.update(cfg)
            logger.info(f"[niche] Loaded config from {path}")
        except Exception as e:
            logger.warning(
                f"[niche] Could not load config {path}: {e}. Using defaults."
            )
