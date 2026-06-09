"""
outreach_manager.py
===================
Builds, manages, and dispatches a personalised DM outreach queue
targeting KOL accounts identified by MonetizationScorer.

Architecture: semi-automated with a mandatory human-approval gate
-----------------------------------------------------------------
Fully silent automated DM sending violates Meta's Terms of Service
and risks permanent account suspension. This module is deliberately
designed as a QUEUE + REVIEW system:

    score_many()           MonetizationScorer
         ↓
    build_queue()          OutreachManager   ← generates personalised drafts
         ↓
    review_queue()         Human reviews & edits each draft in terminal
         ↓
    approve / skip / edit  Human decision per account
         ↓
    export_approved()      Writes approved drafts to outreach_queue.json
         ↓
    [optional] send via    Playwright-based dm_sender (separate script)
               Threads web UI, or manual copy-paste

This keeps you in control, protects your account, and lets you
customise messages before anything goes out.

Public API
----------
  om = OutreachManager(config_path=Path("config/outreach.yaml"))

  queue   = om.build_queue(scored_results, profiles)  → List[OutreachDraft]
  queue   = om.load_queue(path)                        → List[OutreachDraft]
  om.review_queue(queue)                               → interactive CLI review
  om.export_queue(queue, path)                         → saves all drafts
  om.export_approved(queue, path)                      → saves approved only
  summary = om.queue_summary(queue)                    → Dict stats

OutreachDraft shape
-------------------
  username          str    target Threads username
  kol_tier          str    A-tier | B-tier | C-tier
  monetization_score float
  percentile_rank   float
  follower_count    int
  account_type      str
  top_cta_keywords  List[str]
  message           str    personalised draft message
  template_used     str    which template generated this message
  status            str    pending | approved | skipped | edited
  edited_message    str    human-edited version (if status=edited)
  notes             str    reviewer notes
  created_at        str    ISO timestamp
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Default message templates
# Each template is keyed by a name and contains:
#   trigger  : which account_type or kol_tier to prefer this template for
#   body     : message body with {variable} placeholders
#
# Available placeholders:
#   {username}        — target's Threads handle (no @)
#   {first_name}      — first word of username, title-cased
#   {kol_tier}        — A-tier / B-tier / C-tier
#   {top_cta}         — first CTA keyword found in their posts (or "your content")
#   {follower_count}  — formatted follower count e.g. "42.3K"
#   {hook}            — customisable pitch hook from outreach.yaml
# ---------------------------------------------------------------------------

_DEFAULT_TEMPLATES: Dict[str, Dict[str, str]] = {

    "creator_collab": {
        "trigger":  "creator",
        "body": (
            "Hey {first_name} 👋 Love what you're building on Threads — "
            "the way you use {top_cta} really shows you understand your audience.\n\n"
            "I work with creators like you to turn that engagement into a scalable "
            "lead-gen system. Would love to share how we've helped similar accounts "
            "grow their pipeline without extra posting.\n\n"
            "Open to a quick chat? {hook}"
        ),
    },

    "brand_partnership": {
        "trigger":  "brand",
        "body": (
            "Hi {first_name} — noticed your brand has strong traction on Threads "
            "({follower_count} followers and real engagement in your comments).\n\n"
            "We help brands like yours convert that audience into qualified leads "
            "more efficiently. {hook}\n\n"
            "Worth a 15-min call to explore if it's a fit?"
        ),
    },

    "general_kol": {
        "trigger":  "personal",
        "body": (
            "Hey {first_name}! Your Threads content clearly resonates — "
            "the comment engagement on your posts is impressive.\n\n"
            "I help creators and thought leaders monetise their audience more "
            "effectively. {hook}\n\n"
            "Would you be open to hearing more?"
        ),
    },

    "a_tier_priority": {
        "trigger":  "A-tier",
        "body": (
            "Hi {first_name} — your account stood out as one of the top-performing "
            "profiles we track on Threads. The combination of {follower_count} "
            "followers and active comment engagement puts you in a rare category.\n\n"
            "We're selective about who we work with, and you're exactly the type "
            "of creator our lead-gen platform is built for. {hook}\n\n"
            "I'd love to set up a call this week if you're open to it."
        ),
    },
}

# Default hook line — overridable per campaign in outreach.yaml
_DEFAULT_HOOK = "No commitment needed — just a conversation."

# Status values
_STATUSES = ("pending", "approved", "skipped", "edited")


# ---------------------------------------------------------------------------
# OutreachManager
# ---------------------------------------------------------------------------

class OutreachManager:
    """
    Builds and manages a personalised KOL outreach queue.

    Parameters
    ----------
    config_path : optional Path to outreach.yaml
                  Overrides templates, hook line, min_tier filter,
                  and daily send limit.

    outreach.yaml format:
      hook: "Book a free 20-min strategy call at cal.com/yourlink"
      min_tier: "B-tier"          # only queue A and B tier accounts
      max_queue_size: 50          # hard cap on queue length
      templates:                  # optional custom templates (same shape)
        my_template:
          trigger: creator
          body: "Hey {first_name} ..."
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._hook      = _DEFAULT_HOOK
        self._min_tier  = "B-tier"
        self._max_queue = 50
        self._templates = dict(_DEFAULT_TEMPLATES)

        if config_path and Path(config_path).exists():
            self._load_config(Path(config_path))

        logger.info(
            f"OutreachManager ready | min_tier={self._min_tier} "
            f"max_queue={self._max_queue} templates={list(self._templates)}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_queue(
        self,
        scored_results: List[Dict[str, Any]],
        profiles: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Build a personalised outreach queue from scorer output.

        Parameters
        ----------
        scored_results : output of MonetizationScorer.score_many()
                         Each dict must contain: username, kol_tier,
                         monetization_score, percentile_rank,
                         follower_count, account_type, top_cta_keywords
        profiles       : dict mapping username → ProfileData dict
                         (from ProfileScraper.fetch_many())
                         Used for bio context in message personalisation.

        Returns a list of OutreachDraft dicts, capped at max_queue_size,
        sorted by monetization_score descending (hottest leads first).
        Only accounts at or above min_tier are included.
        """
        _tier_rank = {"A-tier": 0, "B-tier": 1, "C-tier": 2, "filtered": 3}
        min_rank   = _tier_rank.get(self._min_tier, 1)

        eligible = [
            r for r in scored_results
            if _tier_rank.get(r.get("kol_tier", "filtered"), 3) <= min_rank
        ]
        eligible.sort(key=lambda r: r.get("monetization_score", 0), reverse=True)
        eligible = eligible[: self._max_queue]

        queue: List[Dict[str, Any]] = []
        for result in eligible:
            username = result.get("username", "")
            profile  = profiles.get(username, {})
            draft    = self._build_draft(result, profile)
            queue.append(draft)
            logger.debug(
                f"[outreach] Drafted message for @{username} "
                f"(tier={result.get('kol_tier')} score={result.get('monetization_score')})"
            )

        logger.info(
            f"[outreach] Queue built: {len(queue)} drafts "
            f"(from {len(scored_results)} scored accounts, "
            f"min_tier={self._min_tier})"
        )
        return queue

    def review_queue(self, queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Interactive CLI review — shows each draft and lets the reviewer
        approve, skip, or edit before anything is exported or sent.

        Controls:
          a / enter → approve as-is
          s         → skip (exclude from approved export)
          e         → open editor to type a custom message
          q         → quit review early (remaining stay 'pending')

        Returns the same queue list with updated status fields.
        """
        pending = [d for d in queue if d["status"] == "pending"]
        if not pending:
            print("[outreach] No pending drafts to review.")
            return queue

        print(f"\n{'='*60}")
        print(f"  OUTREACH QUEUE REVIEW — {len(pending)} drafts")
        print(f"  Commands: [a]pprove  [s]kip  [e]dit  [q]uit")
        print(f"{'='*60}\n")

        for i, draft in enumerate(pending, 1):
            print(f"─── Draft {i}/{len(pending)} ──────────────────────────────")
            print(f"  Target     : @{draft['username']}")
            print(f"  Tier       : {draft['kol_tier']}  "
                  f"(score={draft['monetization_score']:.3f}, "
                  f"pct={draft['percentile_rank']:.0f}th)")
            print(f"  Followers  : {self._fmt_count(draft['follower_count'])}")
            print(f"  Type       : {draft['account_type']}")
            print(f"  CTA hits   : {', '.join(draft['top_cta_keywords']) or '—'}")
            print(f"\n  Message draft:")
            print(textwrap.indent(draft["message"], "    "))
            print()

            while True:
                try:
                    choice = input("  → [a]pprove / [s]kip / [e]dit / [q]uit: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "q"

                if choice in ("a", ""):
                    draft["status"] = "approved"
                    print("  ✓ Approved\n")
                    break
                elif choice == "s":
                    draft["status"] = "skipped"
                    print("  – Skipped\n")
                    break
                elif choice == "e":
                    print("  Enter your custom message (type END on a new line to finish):")
                    lines = []
                    while True:
                        try:
                            line = input()
                        except (EOFError, KeyboardInterrupt):
                            break
                        if line.strip().upper() == "END":
                            break
                        lines.append(line)
                    if lines:
                        draft["edited_message"] = "\n".join(lines)
                        draft["status"] = "edited"
                        print("  ✓ Saved edited message\n")
                    else:
                        print("  (No input — keeping original draft)\n")
                    break
                elif choice == "q":
                    print(f"\n  Review paused. {len(pending)-i} drafts remain pending.\n")
                    return queue
                else:
                    print("  Unrecognised input. Use a / s / e / q.")

        approved = sum(1 for d in queue if d["status"] == "approved")
        edited   = sum(1 for d in queue if d["status"] == "edited")
        skipped  = sum(1 for d in queue if d["status"] == "skipped")
        print(f"{'='*60}")
        print(f"  Review complete: {approved} approved, {edited} edited, {skipped} skipped")
        print(f"{'='*60}\n")
        return queue

    def export_queue(
        self, queue: List[Dict[str, Any]], path: Path
    ) -> Path:
        """Save the full queue (all statuses) to JSON."""
        return self._write_json(queue, path)

    def export_approved(
        self, queue: List[Dict[str, Any]], path: Path
    ) -> Path:
        """
        Save only approved and edited drafts to JSON.
        For edited drafts, the 'edited_message' field is used as the
        final message so the downstream dm_sender always reads one field.
        """
        approved = []
        for d in queue:
            if d["status"] in ("approved", "edited"):
                out = dict(d)
                if d["status"] == "edited" and d.get("edited_message"):
                    out["final_message"] = d["edited_message"]
                else:
                    out["final_message"] = d["message"]
                approved.append(out)

        logger.info(f"[outreach] {len(approved)} approved drafts → {path}")
        return self._write_json(approved, path)

    def load_queue(self, path: Path) -> List[Dict[str, Any]]:
        """Load a previously saved queue from JSON."""
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            queue = json.load(f)
        logger.info(f"[outreach] Loaded {len(queue)} drafts from {path}")
        return queue

    def queue_summary(self, queue: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Return a summary stats dict for the queue."""
        return {
            "total":    len(queue),
            "pending":  sum(1 for d in queue if d["status"] == "pending"),
            "approved": sum(1 for d in queue if d["status"] == "approved"),
            "edited":   sum(1 for d in queue if d["status"] == "edited"),
            "skipped":  sum(1 for d in queue if d["status"] == "skipped"),
            "by_tier": {
                "A-tier": sum(1 for d in queue if d["kol_tier"] == "A-tier"),
                "B-tier": sum(1 for d in queue if d["kol_tier"] == "B-tier"),
                "C-tier": sum(1 for d in queue if d["kol_tier"] == "C-tier"),
            },
        }

    # ------------------------------------------------------------------
    # Draft builder
    # ------------------------------------------------------------------

    def _build_draft(
        self,
        result: Dict[str, Any],
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build a single OutreachDraft dict for one scored account."""
        username     = result.get("username", "")
        account_type = result.get("account_type", "personal")
        kol_tier     = result.get("kol_tier", "C-tier")
        top_cta_kws  = result.get("top_cta_keywords") or []

        template_name, template_body = self._pick_template(account_type, kol_tier)
        message = self._render(
            template_body,
            username    = username,
            kol_tier    = kol_tier,
            account_type= account_type,
            top_cta_kws = top_cta_kws,
            follower_count = result.get("follower_count", 0),
        )

        return {
            "username":          username,
            "kol_tier":          kol_tier,
            "monetization_score": result.get("monetization_score", 0.0),
            "percentile_rank":   result.get("percentile_rank", 0.0),
            "follower_count":    result.get("follower_count", 0),
            "account_type":      account_type,
            "top_cta_keywords":  top_cta_kws,
            "message":           message,
            "template_used":     template_name,
            "status":            "pending",
            "edited_message":    "",
            "notes":             "",
            "created_at":        datetime.now(timezone.utc).isoformat(),
        }

    def _pick_template(
        self, account_type: str, kol_tier: str
    ) -> Tuple[str, str]:
        """
        Select the best template for this account.

        Priority:
          1. A-tier gets the priority template regardless of account_type
          2. account_type-specific template if one exists
          3. general_kol fallback
        """
        if kol_tier == "A-tier" and "a_tier_priority" in self._templates:
            t = self._templates["a_tier_priority"]
            return "a_tier_priority", t["body"]

        for name, t in self._templates.items():
            if t.get("trigger") == account_type:
                return name, t["body"]

        fallback = self._templates.get("general_kol", {})
        return "general_kol", fallback.get("body", "Hi {first_name}, I'd love to connect.")

    def _render(
        self,
        template: str,
        username: str,
        kol_tier: str,
        account_type: str,
        top_cta_kws: List[str],
        follower_count: int,
    ) -> str:
        """Render a template string with personalised variable values."""
        first_name = re.sub(r"[^a-zA-Z]", " ", username).split()[0].capitalize() \
                     if username else "there"
        top_cta    = top_cta_kws[0] if top_cta_kws else "your content"

        values = {
            "username":       username,
            "first_name":     first_name,
            "kol_tier":       kol_tier,
            "top_cta":        top_cta,
            "follower_count": self._fmt_count(follower_count),
            "hook":           self._hook,
        }
        try:
            return template.format(**values)
        except KeyError as e:
            logger.warning(f"[outreach] Template placeholder {e} not found — leaving raw")
            return template

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_count(n: int) -> str:
        """Format a follower count as a human-readable string."""
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    @staticmethod
    def _write_json(data: Any, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def _load_config(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if "hook" in cfg:
                self._hook = str(cfg["hook"])
            if "min_tier" in cfg:
                self._min_tier = str(cfg["min_tier"])
            if "max_queue_size" in cfg:
                self._max_queue = int(cfg["max_queue_size"])
            if "templates" in cfg:
                for name, tmpl in cfg["templates"].items():
                    if "body" in tmpl:
                        self._templates[name] = tmpl
            logger.info(f"[outreach] Loaded config from {path}")
        except Exception as e:
            logger.warning(f"[outreach] Could not load config {path}: {e}. Using defaults.")
