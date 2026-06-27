"""
llm_scorer.py
=============
LLM-powered deep content analysis for KOL accounts.

Works ALONGSIDE the existing MonetizationScorer (heuristic math).
- MonetizationScorer handles numeric signals (follower ratios, engagement rates)
- LLMScorer handles SEMANTIC signals that only an AI can detect

Public API
----------
  scorer = LLMScorer(llm_client, niche="fortune_telling")

  analysis = scorer.score_content(profile, posts)
  → dict with niche_relevance, content_quality, monetization_intent, etc.

  accounts = scorer.score_many(account_list)
  → enriches each account with account["llm_analysis"]

  message = scorer.generate_outreach(profile, posts, tier, score_result)
  → personalized DM string
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .utils.logger import get_logger

logger = get_logger(__name__)

# Default analysis result when LLM call fails or returns unparseable output
_DEFAULT_ANALYSIS: Dict[str, Any] = {
    "niche_relevance": 50,
    "content_quality": 50,
    "monetization_intent": 50,
    "audience_engagement_style": "medium",
    "recommended_approach": "General outreach",
    "summary": "Analysis unavailable — using default scores.",
}


def _strip_html(text: str) -> str:
    """Remove HTML tags from text to save LLM tokens."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _truncate(text: str, max_chars: int = 500) -> str:
    """Truncate text to max_chars, appending ellipsis if cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Attempt to extract a JSON object from LLM response text.
    Tries multiple strategies: fenced code block, raw braces, direct parse.
    """
    # Strategy 1: Find JSON inside ```json ... ``` fenced block
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 2: Find JSON inside ``` ... ``` (no language tag)
    match = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 3: Find the outermost { ... } in the response
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 4: Try parsing the entire response as JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    return None


class LLMScorer:
    """
    Uses an LLM to perform deep semantic analysis on KOL accounts.
    Provides content scoring and personalized outreach message generation.
    """

    def __init__(self, llm_client, niche: str = "fortune_telling") -> None:
        self._llm = llm_client
        self._niche = niche
        logger.info(
            f"LLMScorer ready | backend={llm_client.backend} niche='{niche}'"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_content(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Send profile bio + recent posts to the LLM for deep analysis.

        Returns a dict with:
            niche_relevance         (0-100)
            content_quality         (0-100)
            monetization_intent     (0-100)
            audience_engagement_style  ("high"/"medium"/"low")
            recommended_approach    (short sentence)
            summary                 (2-3 sentence profile summary)
        """
        username = profile.get("username", "unknown")

        # Build the content payload for the LLM
        bio = _strip_html(profile.get("bio", "") or "")
        full_name = profile.get("full_name", "")
        followers = profile.get("follower_count", 0)

        # Take up to 5 most recent posts, strip HTML, truncate
        post_texts = []
        for p in posts[:5]:
            raw = p.get("text", "") or ""
            clean = _truncate(_strip_html(raw), 500)
            if clean:
                post_texts.append(clean)

        if not post_texts and not bio:
            logger.debug(f"[llm_scorer] @{username}: no content to analyze")
            return dict(_DEFAULT_ANALYSIS)

        # Compose the user prompt
        posts_block = "\n---\n".join(
            f"Post {i+1}: {t}" for i, t in enumerate(post_texts)
        )

        user_prompt = (
            f"Analyze this {self._niche} niche account:\n\n"
            f"Username: @{username}\n"
            f"Full Name: {full_name}\n"
            f"Bio: {bio}\n"
            f"Followers: {followers}\n\n"
            f"Recent Posts:\n{posts_block}\n\n"
            "Respond with ONLY a JSON object (no extra text) using this exact schema:\n"
            "{\n"
            '  "niche_relevance": <0-100 how relevant to the niche>,\n'
            '  "content_quality": <0-100 quality of writing and engagement>,\n'
            '  "monetization_intent": <0-100 how likely they are selling/promoting>,\n'
            '  "audience_engagement_style": "<high/medium/low>",\n'
            '  "recommended_approach": "<short sentence on best outreach angle>",\n'
            '  "summary": "<2-3 sentence profile summary>"\n'
            "}"
        )

        system_prompt = (
            f"You are an expert KOL analyst specializing in the {self._niche} niche. "
            "Your job is to evaluate social media accounts for their commercial potential "
            "and niche relevance. Be precise and objective. "
            "Always respond with valid JSON only, no markdown, no explanation."
        )

        try:
            response = self._llm.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            if not response:
                logger.warning(f"[llm_scorer] @{username}: empty LLM response")
                return dict(_DEFAULT_ANALYSIS)

            parsed = _extract_json(response)
            if parsed is None:
                logger.warning(
                    f"[llm_scorer] @{username}: failed to parse LLM JSON response"
                )
                return dict(_DEFAULT_ANALYSIS)

            # Validate and fill missing fields with defaults
            result = dict(_DEFAULT_ANALYSIS)
            result.update(parsed)

            # Clamp numeric scores to 0-100
            for key in ("niche_relevance", "content_quality", "monetization_intent"):
                try:
                    result[key] = max(0, min(100, int(result[key])))
                except (ValueError, TypeError):
                    result[key] = 50

            logger.debug(
                f"[llm_scorer] @{username}: relevance={result['niche_relevance']} "
                f"quality={result['content_quality']} "
                f"monetization={result['monetization_intent']}"
            )
            return result

        except Exception as e:
            logger.error(f"[llm_scorer] @{username}: score_content failed: {e}")
            return dict(_DEFAULT_ANALYSIS)

    def score_many(
        self, accounts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Score all accounts using the LLM.
        Attaches result as account["llm_analysis"] for each account.
        Logs progress every 10 accounts.
        """
        total = len(accounts)
        logger.info(f"[llm_scorer] Starting LLM analysis for {total} accounts")

        for i, account in enumerate(accounts):
            profile = account.get("profile", {})
            posts = account.get("posts", [])
            username = profile.get("username", "unknown")

            analysis = self.score_content(profile, posts)
            account["llm_analysis"] = analysis

            # Log progress every 10 accounts
            if (i + 1) % 10 == 0 or (i + 1) == total:
                logger.info(
                    f"[llm_scorer] Progress: {i + 1}/{total} accounts analyzed"
                )

        logger.info(f"[llm_scorer] LLM analysis complete for {total} accounts")
        return accounts

    def generate_outreach(
        self,
        profile: Dict[str, Any],
        posts: List[Dict[str, Any]],
        tier: str,
        score_result: Dict[str, Any],
    ) -> str:
        """
        Generate a personalized outreach DM message for a KOL.

        The message will:
        - Reference something specific from their recent posts
        - Feel like a genuine fan/peer reaching out
        - Include a soft CTA
        """
        username = profile.get("username", "unknown")
        full_name = profile.get("full_name", username)
        bio = _strip_html(profile.get("bio", "") or "")

        # Get 2-3 recent post snippets for context
        post_snippets = []
        for p in posts[:3]:
            raw = p.get("text", "") or ""
            clean = _truncate(_strip_html(raw), 200)
            if clean:
                post_snippets.append(clean)

        posts_context = "\n".join(
            f"- {t}" for t in post_snippets
        ) if post_snippets else "No recent posts available."

        # Get LLM analysis summary if available
        summary = score_result.get("summary", "")
        approach = score_result.get("recommended_approach", "")

        user_prompt = (
            f"Write a short, personalized DM to this KOL:\n\n"
            f"Username: @{username}\n"
            f"Name: {full_name}\n"
            f"Bio: {bio}\n"
            f"Tier: {tier}\n"
            f"Profile Summary: {summary}\n"
            f"Recommended Approach: {approach}\n\n"
            f"Their recent posts:\n{posts_context}\n\n"
            "Requirements:\n"
            "- Keep it under 150 words\n"
            "- Reference something specific from their recent content\n"
            "- Sound like a genuine fan or peer, NOT a salesperson\n"
            "- Include one soft CTA (e.g., 'would love to chat', 'open to connecting?')\n"
            "- Do NOT use hashtags or emojis excessively\n"
            "- Write in a warm, conversational tone\n\n"
            "Return ONLY the message text, no quotes, no labels."
        )

        system_prompt = (
            "You are a professional community manager who writes authentic, "
            "personalized outreach messages. Your messages feel genuine and "
            "never come across as spam or automated. You specialize in "
            f"the {self._niche} community."
        )

        try:
            response = self._llm.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            if not response:
                logger.warning(
                    f"[llm_scorer] @{username}: empty outreach response, using fallback"
                )
                return self._fallback_message(username, full_name)

            # Clean up the response (remove potential quotes or labels)
            message = response.strip().strip('"').strip("'")
            logger.debug(
                f"[llm_scorer] @{username}: generated outreach message "
                f"({len(message)} chars)"
            )
            return message

        except Exception as e:
            logger.error(
                f"[llm_scorer] @{username}: outreach generation failed: {e}"
            )
            return self._fallback_message(username, full_name)

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_message(username: str, full_name: str) -> str:
        """Simple fallback message when LLM is unavailable."""
        name = full_name if full_name and full_name != username else username
        return (
            f"Hey {name}! I've been following your content and really "
            f"love what you're sharing. Would love to connect and chat "
            f"sometime if you're open to it!"
        )
