"""
monetization_scorer.py
=======================
Scores and filters Threads accounts by their potential as monetizable
lead-generation targets — i.e. KOLs who are likely to become your customers.

Mental model
------------
This module is a FILTER + RANKER. It takes a list of scraped accounts and
answers two questions:
  1. Does this account have genuine audience influence worth targeting?
  2. Does this account show behaviours that signal they are open to,
     or already engaged in, monetization — making them receptive to
     a pitch about YOUR lead-gen / marketing product?

A high score = strong KOL signal + strong monetization openness.
The module does NOT send outreach. It only produces a ranked shortlist.

Inputs (from upstream modules)
------------------------------
  profile   : Dict — output of ProfileScraper.fetch()
  posts     : List[Dict] — output of ThreadsParser.parse_item() for each post
              Fields used: text, like_count, reply_count, repost_count

Scoring model (weighted composite, 0.0 – 1.0)
----------------------------------------------
  Component                    Weight   Source
  ─────────────────────────────────────────────────────────────────────
  A. Audience reach score       0.25    profile.follower_tier + ff_ratio
  B. Engagement rate            0.25    (likes+replies+reposts)/followers/posts
  C. CTA density                0.20    CTA keyword hits across post texts
  D. Comment–follower ratio     0.15    avg reply_count / follower_count
  E. Monetization intent        0.10    bio signals (link cue, contact cue,
                                        external URL, account_type)
  F. Credibility bonus          0.05    is_verified + follower_tier modifier
  ─────────────────────────────────────────────────────────────────────
  Total                         1.00

KOL tier classification (percentile-based, computed across the batch)
-----------------------------------------------------------------------
  Top 15%  (>= 85th pct)  →  "A-tier"    top priority outreach targets
  Top 40%  (>= 60th pct)  →  "B-tier"    strong leads, worth outreach
  Top 75%  (>= 25th pct)  →  "C-tier"    emerging, nurture or monitor
  Bottom 25% + pre-filtered → "filtered"  excluded from shortlist

Public API
----------
  scorer = MonetizationScorer()                     # default weights
  scorer = MonetizationScorer(config_path=Path("config/scorer.yaml"))

  result  = scorer.score(profile, posts)            → ScoreResult dict
  results = scorer.score_many(account_list)         → List[ScoreResult dict]
  leads   = scorer.filter(results, min_tier="B-tier") → filtered + sorted list
  scorer.export_json(leads, path)
  scorer.export_csv(leads, path)

ScoreResult shape
-----------------
  username               str
  monetization_score     float   0.0 – 1.0  (2 d.p.)
  kol_tier               str     A-tier | B-tier | C-tier | filtered
  # --- component breakdown ---
  score_audience         float
  score_engagement       float
  score_cta_density      float
  score_comment_follower float
  score_monetization_intent float
  score_credibility      float
  # --- key evidence (human-readable) ---
  top_cta_keywords       List[str]   top CTA terms found in posts
  engagement_rate        float       raw ER before normalization
  avg_reply_count        float
  follower_tier          str
  follower_count         int
  bio_has_link_cue       bool
  bio_has_contact_cue    bool
  has_external_url       bool
  is_verified            bool
  account_type           str
  total_posts_analysed   int
  # --- meta ---
  score_version          str     version tag for reproducibility
"""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Version tag — bump when scoring logic changes so outputs are traceable
# ---------------------------------------------------------------------------
_SCORE_VERSION = "v1.1"

# ---------------------------------------------------------------------------
# Default component weights  (must sum to 1.0)
# ---------------------------------------------------------------------------
# v1.1 change: comment_follower raised 0.15 → 0.25 (community engagement is
# the strongest signal that an audience is active and persuadable).
# Reductions to keep sum = 1.0:
#   audience:            0.25 → 0.20
#   engagement:          0.25 → 0.20
#   cta_density:         0.20 → 0.18
#   monetization_intent: 0.10 → 0.12  (slight raise — intent matters more than reach)
#   credibility:         0.05 → 0.05  (unchanged — small amplifier, not a driver)
# ---------------------------------------------------------------------------
_DEFAULT_WEIGHTS: Dict[str, float] = {
    "audience":             0.20,
    "engagement":           0.20,
    "cta_density":          0.18,
    "comment_follower":     0.25,
    "monetization_intent":  0.12,
    "credibility":          0.05,
}

# ---------------------------------------------------------------------------
# Follower-tier audience reach scores
# These map profile_scraper's tier labels → raw reach score (0–1)
# Reflects that mid-tier KOLs often have BETTER engagement than mega-tier
# ---------------------------------------------------------------------------
_TIER_REACH_SCORE: Dict[str, float] = {
    "mega":  0.80,   # 1M+  — reach is huge but ER typically low
    "macro": 0.90,   # 100K – 999K — sweet spot for advertisers
    "mid":   1.00,   # 10K – 99K — highest typical ER, best KOL targets
    "micro": 0.70,   # 1K – 9K — niche but genuine
    "nano":  0.30,   # <1K — too small for most campaigns
}

# ---------------------------------------------------------------------------
# CTA keyword list — signals the account is already monetizing content
# These are detected in POST TEXT (not bio, which is handled by profile_scraper)
# ---------------------------------------------------------------------------
_CTA_KEYWORDS: List[Tuple[str, int]] = [
    # (pattern, weight)  weight 2 = strong monetization signal, 1 = moderate
    (r"link\s+in\s+bio",          2),
    (r"swipe\s+up",               2),
    (r"dm\s+(me|us|for)",         2),
    (r"shop\s+now",               2),
    (r"limited\s+(time|offer)",   2),
    (r"use\s+code\s+\w+",         2),   # discount codes
    (r"promo\s+code",             2),
    (r"click\s+(the\s+)?link",    2),
    (r"sign\s+up",                1),
    (r"free\s+\w+",               1),
    (r"discount",                 1),
    (r"collab",                   1),
    (r"sponsored",                1),
    (r"ad\b",                     1),
    (r"#ad\b",                    1),
    (r"partner(ed)?\s+with",      1),
    (r"comment\s+below",          1),
    (r"drop\s+(a\s+)?comment",    1),
    (r"save\s+this",              1),
    (r"share\s+this",             1),
    (r"tag\s+(a\s+)?\w+",         1),
    (r"giveaway",                 2),
    (r"enter\s+to\s+win",         2),
    (r"book\s+(a\s+)?call",       2),
    (r"check\s+out",              1),
    (r"available\s+now",          1),
    (r"order\s+now",              2),
]

# Tier labels in descending priority order for filter()
_TIER_ORDER = ["A-tier", "B-tier", "C-tier", "filtered"]

# Percentile cutoffs for tier assignment (applied across the scored batch).
# Accounts pre-filtered to score=0.0 are excluded from the percentile pool
# and always land in "filtered" regardless of batch composition.
#
#   Top 15%   → A-tier   (hottest outreach targets)
#   Next 25%  → B-tier   (strong leads, top 40% overall)
#   Next 35%  → C-tier   (emerging, top 75% overall)
#   Bottom 25% → filtered
#
# Overridable in scorer.yaml:
#   percentile_tiers:
#     A-tier: 85
#     B-tier: 60
#     C-tier: 25
_DEFAULT_PERCENTILE_CUTOFFS: Dict[str, float] = {
    "A-tier": 85.0,   # score >= 85th percentile of batch
    "B-tier": 60.0,   # score >= 60th percentile
    "C-tier": 25.0,   # score >= 25th percentile
    # below 25th percentile → "filtered"
}


# ---------------------------------------------------------------------------
# MonetizationScorer
# ---------------------------------------------------------------------------

class MonetizationScorer:
    """
    Scores and filters Threads accounts by monetization potential.

    Parameters
    ----------
    config_path : optional Path to a YAML file that overrides default weights
                  and CTA keywords. If None, defaults are used.

    YAML override format (config/scorer.yaml):
      weights:
        audience: 0.20
        engagement: 0.20
        cta_density: 0.18
        comment_follower: 0.25
        monetization_intent: 0.12
        credibility: 0.05
      percentile_tiers:
        A-tier: 85
        B-tier: 60
        C-tier: 25
      min_follower_count: 500
      min_posts_required: 3
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._weights = dict(_DEFAULT_WEIGHTS)
        self._min_follower_count: int = 500
        self._min_posts_required: int = 3
        self._percentile_cutoffs: Dict[str, float] = dict(_DEFAULT_PERCENTILE_CUTOFFS)

        if config_path and Path(config_path).exists():
            self._load_config(Path(config_path))

        self._validate_weights()
        logger.info(
            f"MonetizationScorer ready | version={_SCORE_VERSION} "
            f"weights={self._weights}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Score a single account.

        Parameters
        ----------
        profile : ProfileData dict from ProfileScraper.fetch()
        posts   : list of parsed post dicts from ThreadsParser.parse_item()

        Returns a ScoreResult dict.
        """
        username = profile.get("username", "unknown")

        # --- guard: too few posts or too few followers to be meaningful ---
        follower_count = int(profile.get("follower_count") or 0)
        if follower_count < self._min_follower_count:
            logger.debug(
                f"@{username}: follower_count={follower_count} "
                f"< min={self._min_follower_count} → auto-filtered"
            )
            return self._zero_result(profile, posts, reason="below_follower_floor")

        if len(posts) < self._min_posts_required:
            logger.debug(
                f"@{username}: only {len(posts)} posts "
                f"< min={self._min_posts_required} → auto-filtered"
            )
            return self._zero_result(profile, posts, reason="insufficient_posts")

        # --- compute each component ---
        s_audience,    audience_meta    = self._score_audience(profile)
        s_engagement,  engagement_meta  = self._score_engagement(profile, posts)
        s_cta,         cta_meta         = self._score_cta_density(posts)
        s_cf,          cf_meta          = self._score_comment_follower(profile, posts)
        s_intent,      intent_meta      = self._score_monetization_intent(profile)
        s_cred,        cred_meta        = self._score_credibility(profile)

        # --- weighted composite ---
        w = self._weights
        raw_score = (
            s_audience   * w["audience"]            +
            s_engagement * w["engagement"]          +
            s_cta        * w["cta_density"]         +
            s_cf         * w["comment_follower"]    +
            s_intent     * w["monetization_intent"] +
            s_cred       * w["credibility"]
        )
        final_score = round(min(max(raw_score, 0.0), 1.0), 4)
        tier = "pending"   # assigned by _assign_percentile_tiers in score_many()

        logger.info(
            f"@{username} | score={final_score:.4f} tier={tier} | "
            f"audience={s_audience:.3f} engagement={s_engagement:.3f} "
            f"cta={s_cta:.3f} cf={s_cf:.3f} intent={s_intent:.3f} "
            f"cred={s_cred:.3f}"
        )

        return {
            # --- identity ---
            "username":                 username,
            "monetization_score":       final_score,
            "kol_tier":                 tier,

            # --- component scores ---
            "score_audience":           round(s_audience, 4),
            "score_engagement":         round(s_engagement, 4),
            "score_cta_density":        round(s_cta, 4),
            "score_comment_follower":   round(s_cf, 4),
            "score_monetization_intent": round(s_intent, 4),
            "score_credibility":        round(s_cred, 4),

            # --- key evidence (human-readable for outreach context) ---
            "top_cta_keywords":         cta_meta["top_keywords"],
            "engagement_rate":          round(engagement_meta["raw_er"], 6),
            "avg_reply_count":          round(cf_meta["avg_replies"], 2),
            "follower_tier":            audience_meta["tier"],
            "follower_count":           follower_count,
            "bio_has_link_cue":         profile.get("bio_has_link_cue", False),
            "bio_has_contact_cue":      profile.get("bio_has_contact_cue", False),
            "has_external_url":         profile.get("has_external_url", False),
            "is_verified":              profile.get("is_verified", False),
            "account_type":             profile.get("account_type", "personal"),
            "total_posts_analysed":     len(posts),

            # --- meta ---
            "score_version":            _SCORE_VERSION,
        }

    def score_many(
        self,
        account_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Score a list of accounts, then assign tiers by percentile rank
        across the batch.

        Parameters
        ----------
        account_list : list of dicts, each with keys:
            "profile" → ProfileData dict
            "posts"   → List of parsed post dicts

        Returns a list of ScoreResult dicts, sorted by monetization_score
        descending (highest-potential KOLs first).

        Tier assignment
        ---------------
        After all raw scores are computed, accounts that passed floor
        filters (score > 0) form the percentile pool. Tiers are assigned
        by where each account sits in that distribution:

            A-tier   : >= 85th percentile  (top 15%)
            B-tier   : >= 60th percentile  (top 40%)
            C-tier   : >= 25th percentile  (top 75%)
            filtered : <  25th percentile, or pre-filtered (score = 0)

        Tiers are always relative to YOUR collected batch, not fixed
        absolute thresholds.
        """
        # step 1 — compute raw scores
        results = []
        for account in account_list:
            profile = account.get("profile", {})
            posts   = account.get("posts", [])
            result  = self.score(profile, posts)
            results.append(result)

        # step 2 — assign percentile-based tiers across the batch
        results = self._assign_percentile_tiers(results)

        # step 3 — sort by score descending
        results.sort(key=lambda r: r["monetization_score"], reverse=True)

        logger.info(
            f"score_many: {len(results)} accounts scored | "
            f"A={sum(1 for r in results if r['kol_tier'] == 'A-tier')} "
            f"B={sum(1 for r in results if r['kol_tier'] == 'B-tier')} "
            f"C={sum(1 for r in results if r['kol_tier'] == 'C-tier')} "
            f"filtered={sum(1 for r in results if r['kol_tier'] == 'filtered')}"
        )
        return results

    def filter(
        self,
        results: List[Dict[str, Any]],
        min_tier: str = "B-tier",
    ) -> List[Dict[str, Any]]:
        """
        Filter a scored list to only accounts at or above min_tier.

        Parameters
        ----------
        results  : output of score_many()
        min_tier : "A-tier" | "B-tier" | "C-tier"
                   Accounts below this tier (including "filtered") are excluded.

        Returns filtered list, still sorted by monetization_score descending.
        """
        if min_tier not in _TIER_ORDER:
            raise ValueError(
                f"min_tier must be one of {_TIER_ORDER[:-1]}, got '{min_tier}'"
            )
        cutoff_idx = _TIER_ORDER.index(min_tier)
        accepted_tiers = set(_TIER_ORDER[:cutoff_idx + 1])

        shortlist = [r for r in results if r["kol_tier"] in accepted_tiers]
        logger.info(
            f"filter(min_tier={min_tier}): "
            f"{len(shortlist)}/{len(results)} accounts passed"
        )
        return shortlist

    def export_json(
        self, results: List[Dict[str, Any]], path: Path
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info(f"[scorer] Exported {len(results)} results → {path}")
        return path

    def export_csv(
        self, results: List[Dict[str, Any]], path: Path
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not results:
            logger.warning("[scorer] export_csv called with empty list")
            return path

        fieldnames = [k for k in results[0].keys()]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in results:
                flat = dict(row)
                # Flatten list fields for CSV
                flat["top_cta_keywords"] = " | ".join(row.get("top_cta_keywords") or [])
                writer.writerow(flat)
        logger.info(f"[scorer] Exported {len(results)} results → {path}")
        return path

    # ------------------------------------------------------------------
    # Component A — Audience reach score
    # ------------------------------------------------------------------

    def _score_audience(
        self, profile: Dict[str, Any]
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Combines follower_tier reach score with follower_following_ratio.

        Rationale:
          - Tier score reflects that mid-tier (10K-99K) KOLs typically have
            the best engagement-per-follower for marketers.
          - FF ratio bonus rewards accounts with organic audience pull
            (many followers, few following back).
        """
        tier = profile.get("follower_tier", "nano")
        tier_score = _TIER_REACH_SCORE.get(tier, 0.3)

        ff_ratio = float(profile.get("follower_following_ratio") or 0.0)
        # Normalise: ratio of 10+ is excellent, cap bonus at 1.0
        ff_bonus = min(ff_ratio / 10.0, 1.0) * 0.2   # up to +0.2

        score = min(tier_score * 0.8 + ff_bonus, 1.0)
        return score, {"tier": tier, "tier_score": tier_score, "ff_bonus": ff_bonus}

    # ------------------------------------------------------------------
    # Component B — Engagement rate
    # ------------------------------------------------------------------

    def _score_engagement(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Standard engagement rate = (likes + replies + reposts) / followers / posts.

        Industry ER benchmarks for Threads/Instagram-style platforms:
          > 0.06 (6%)  — exceptional
          > 0.03 (3%)  — good
          > 0.01 (1%)  — average
          < 0.01       — below average

        We use a logarithmic curve so that the score is not brutally
        penalised for large accounts (which naturally have lower raw ER).
        """
        follower_count = max(int(profile.get("follower_count") or 1), 1)
        n = len(posts)
        if n == 0:
            return 0.0, {"raw_er": 0.0}

        total_interactions = sum(
            int(p.get("like_count") or 0)
            + int(p.get("reply_count") or 0)
            + int(p.get("repost_count") or 0)
            for p in posts
        )
        raw_er = total_interactions / follower_count / n

        # Log-scale normalisation: ER of 6% → score ~1.0
        # score = log(1 + raw_er / 0.001) / log(1 + 60)
        # This gives a smooth curve: 1% → 0.54, 3% → 0.75, 6% → 0.87, 10% → 0.95
        score = math.log1p(raw_er / 0.001) / math.log1p(60)
        score = min(score, 1.0)

        return score, {"raw_er": raw_er}

    # ------------------------------------------------------------------
    # Component C — CTA density in post text
    # ------------------------------------------------------------------

    def _score_cta_density(
        self, posts: List[Dict[str, Any]]
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Measures how frequently this account uses CTA language in posts.

        Method:
          - For each post, scan text against _CTA_KEYWORDS (weighted).
          - Compute weighted_hits / total_posts, normalised to 0–1.
          - Also track which keywords appear most (for outreach evidence).

        Scoring:
          - A post with 2+ strong CTAs scores 1.0 for that post.
          - Density is the fraction of posts with meaningful CTA presence.
          - We cap at 1.0: an account doesn't score higher for spamming CTAs.
        """
        if not posts:
            return 0.0, {"top_keywords": []}

        keyword_hit_counts: Dict[str, int] = {}
        weighted_post_scores: List[float] = []

        for post in posts:
            text = (post.get("text") or "").lower()
            post_weight = 0.0
            for pattern, weight in _CTA_KEYWORDS:
                if re.search(pattern, text):
                    post_weight += weight
                    # Track readable keyword for evidence output
                    readable = pattern.replace(r"\s+", " ").replace(r"\b", "").replace("\\", "")
                    keyword_hit_counts[readable] = keyword_hit_counts.get(readable, 0) + 1

            # Normalise per-post score: 2 strong keywords (weight 4) → 1.0
            weighted_post_scores.append(min(post_weight / 4.0, 1.0))

        density = sum(weighted_post_scores) / len(weighted_post_scores)

        # Top 5 CTA keywords by frequency
        top_keywords = sorted(
            keyword_hit_counts, key=keyword_hit_counts.get, reverse=True
        )[:5]

        return min(density, 1.0), {"top_keywords": top_keywords}

    # ------------------------------------------------------------------
    # Component D — Comment–follower ratio
    # ------------------------------------------------------------------

    def _score_comment_follower(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Average reply_count / follower_count across all posts.

        This is the most direct signal of genuine community engagement.
        A KOL whose followers actually reply (not just scroll past) is
        far more monetizable — their audience is active and persuadable.

        Benchmarks:
          > 0.005 (0.5%)  — exceptional community engagement
          > 0.002 (0.2%)  — good
          > 0.0005        — average
          < 0.0005        — passive audience

        Uses sigmoid-like normalisation to score generously in the 0–0.5%
        range (where most real accounts live) while still rewarding outliers.
        """
        follower_count = max(int(profile.get("follower_count") or 1), 1)
        if not posts:
            return 0.0, {"avg_replies": 0.0, "cf_ratio": 0.0}

        avg_replies = sum(int(p.get("reply_count") or 0) for p in posts) / len(posts)
        cf_ratio = avg_replies / follower_count

        # Normalise: cf_ratio of 0.005 (0.5%) → score ~1.0
        # score = cf_ratio / (cf_ratio + 0.001)   [a smooth sigmoid-like curve]
        score = cf_ratio / (cf_ratio + 0.001) if cf_ratio > 0 else 0.0
        score = min(score, 1.0)

        return score, {"avg_replies": avg_replies, "cf_ratio": cf_ratio}

    # ------------------------------------------------------------------
    # Component E — Monetization intent (profile-level signals)
    # ------------------------------------------------------------------

    def _score_monetization_intent(
        self, profile: Dict[str, Any]
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Binary and heuristic signals from the profile that indicate
        the account is ALREADY monetizing or actively seeking to.

        These are the strongest signals that this KOL is a RECEPTIVE CUSTOMER:
          · external_url       — they have something to sell / promote
          · bio_has_link_cue   — they direct followers to buy / visit
          · bio_has_contact_cue— they are open to brand deals / collabs
          · account_type=brand — already running a commercial account
          · account_type=creator — likely seeking monetization partnerships
        """
        score = 0.0

        has_url     = bool(profile.get("has_external_url"))
        link_cue    = bool(profile.get("bio_has_link_cue"))
        contact_cue = bool(profile.get("bio_has_contact_cue"))
        acc_type    = profile.get("account_type", "personal")

        # Additive scoring — each signal independently contributes
        if has_url:
            score += 0.35   # strongest signal: they have something to promote
        if contact_cue:
            score += 0.30   # open to collabs/partnerships — directly receptive
        if link_cue:
            score += 0.20   # actively directing followers to convert
        if acc_type == "creator":
            score += 0.10   # creators are the core KOL target segment
        elif acc_type == "brand":
            score += 0.05   # brands may be competitors rather than customers

        return min(score, 1.0), {}

    # ------------------------------------------------------------------
    # Component F — Credibility bonus
    # ------------------------------------------------------------------

    def _score_credibility(
        self, profile: Dict[str, Any]
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Small additive bonus for signals that increase trust / legitimacy.
        Keeps weight low (0.05) — credibility amplifies other signals,
        it doesn't compensate for weak engagement or zero CTAs.
        """
        score = 0.0

        if profile.get("is_verified"):
            score += 0.60   # verified = platform-confirmed public figure

        tier = profile.get("follower_tier", "nano")
        if tier in ("macro", "mega"):
            score += 0.40   # large established following adds credibility
        elif tier == "mid":
            score += 0.20

        return min(score, 1.0), {}

    # ------------------------------------------------------------------
    # Tier classifier (percentile-based)
    # ------------------------------------------------------------------

    def _assign_percentile_tiers(
        self, results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Assign kol_tier to every result based on percentile rank within
        the batch of accounts that passed floor filters (score > 0).

        Accounts with score = 0 are always "filtered" and excluded from
        the pool so they don't drag down the distribution.

        Also stamps each result with:
            percentile_rank  float  0–100, position in the scored pool
        """
        cutoffs  = self._percentile_cutoffs
        pool     = [r for r in results if r["monetization_score"] > 0.0]
        excluded = [r for r in results if r["monetization_score"] == 0.0]

        if not pool:
            return results

        scores = sorted(r["monetization_score"] for r in pool)
        n = len(scores)

        def pct_value(p: float) -> float:
            """Score at percentile p via linear interpolation."""
            rank = (p / 100.0) * (n - 1)
            lo   = int(rank)
            hi   = min(lo + 1, n - 1)
            return scores[lo] + (rank - lo) * (scores[hi] - scores[lo])

        threshold_a = pct_value(cutoffs["A-tier"])
        threshold_b = pct_value(cutoffs["B-tier"])
        threshold_c = pct_value(cutoffs["C-tier"])

        logger.debug(
            f"[scorer] Percentile thresholds | pool={n} | "
            f"A(p{cutoffs['A-tier']})={threshold_a:.4f} "
            f"B(p{cutoffs['B-tier']})={threshold_b:.4f} "
            f"C(p{cutoffs['C-tier']})={threshold_c:.4f}"
        )

        for r in pool:
            s = r["monetization_score"]
            if   s >= threshold_a: r["kol_tier"] = "A-tier"
            elif s >= threshold_b: r["kol_tier"] = "B-tier"
            elif s >= threshold_c: r["kol_tier"] = "C-tier"
            else:                  r["kol_tier"] = "filtered"
            # percentile rank: fraction of pool at or below this score
            below = sum(1 for sc in scores if sc <= s)
            r["percentile_rank"] = round((below / n) * 100, 1)

        for r in excluded:
            r["percentile_rank"] = 0.0

        return pool + excluded

    # ------------------------------------------------------------------
    # Config loader
    # ------------------------------------------------------------------

    def _load_config(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if "weights" in cfg:
                self._weights.update(cfg["weights"])
            if "percentile_tiers" in cfg:
                self._percentile_cutoffs.update(cfg["percentile_tiers"])
            if "min_follower_count" in cfg:
                self._min_follower_count = int(cfg["min_follower_count"])
            if "min_posts_required" in cfg:
                self._min_posts_required = int(cfg["min_posts_required"])
            logger.info(f"[scorer] Loaded config from {path}")
        except Exception as e:
            logger.warning(f"[scorer] Could not load config {path}: {e}. Using defaults.")

    def _validate_weights(self) -> None:
        total = sum(self._weights.values())
        if not (0.999 < total < 1.001):
            logger.warning(
                f"[scorer] Weights sum to {total:.4f}, not 1.0. "
                "Scores will be proportionally off. Check your scorer.yaml."
            )

    # ------------------------------------------------------------------
    # Zero result (for filtered-before-scoring accounts)
    # ------------------------------------------------------------------

    @staticmethod
    def _zero_result(
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
        reason: str = "",
    ) -> Dict[str, Any]:
        return {
            "username":                     profile.get("username", "unknown"),
            "monetization_score":           0.0,
            "kol_tier":                     "filtered",
            "score_audience":               0.0,
            "score_engagement":             0.0,
            "score_cta_density":            0.0,
            "score_comment_follower":       0.0,
            "score_monetization_intent":    0.0,
            "score_credibility":            0.0,
            "top_cta_keywords":             [],
            "engagement_rate":              0.0,
            "avg_reply_count":              0.0,
            "follower_tier":                profile.get("follower_tier", "nano"),
            "follower_count":               int(profile.get("follower_count") or 0),
            "bio_has_link_cue":             profile.get("bio_has_link_cue", False),
            "bio_has_contact_cue":          profile.get("bio_has_contact_cue", False),
            "has_external_url":             profile.get("has_external_url", False),
            "is_verified":                  profile.get("is_verified", False),
            "account_type":                 profile.get("account_type", "personal"),
            "total_posts_analysed":         len(posts),
            "score_version":                _SCORE_VERSION,
            "filter_reason":                reason,
            "percentile_rank":              0.0,
        }
