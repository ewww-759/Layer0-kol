"""
seed_discoverer.py — v3
=======================
Key fix: search results already contain user_id and full profile data.
We now extract and cache user_id + profile at search time, completely
bypassing the broken /api/v1/users/by/username/ endpoint.
"""

from __future__ import annotations

import json
import re
import time
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

from .utils.logger import get_logger

logger = get_logger(__name__)

_SEARCH_URL = "https://www.threads.net/search"
_API_SEARCH = "https://www.threads.net/api/v1/users/search/"
_DELAY_MIN  = 1.5
_DELAY_MAX  = 3.0

_DEFAULT_CONFIG: Dict[str, Any] = {
    "search_keywords":           [],
    "search_concurrent_workers": 3,
    "search_limit_per_keyword":  50,
    "min_keyword_hits":          1,
}


class SeedDiscoverer:
    def __init__(self, scraper: Any, config_path: Optional[Path] = None) -> None:
        self._scraper   = scraper
        self._cfg       = dict(_DEFAULT_CONFIG)

        if config_path and Path(config_path).exists():
            self._load_config(Path(config_path))

        self._max_workers = int(self._cfg.get("search_concurrent_workers", 1))
        # Force single-threaded execution to prevent instant 429s or bans when using a Cookie
        self._max_workers = 1

        self._limit       = int(self._cfg.get("search_limit_per_keyword", 50))
        self._min_hits    = int(self._cfg.get("min_keyword_hits", 1))

        # Cache: username → full user dict including user_id
        # This is the key fix — populated at search time
        self.user_cache: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"SeedDiscoverer ready | "
            f"keywords={self._cfg.get('search_keywords', [])} "
            f"workers={self._max_workers} limit={self._limit}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, keywords: Optional[List[str]] = None) -> List[str]:
        kws = keywords or self._cfg.get("search_keywords", [])
        if not kws:
            logger.error("[seed] No keywords provided.")
            return []

        if self._scraper.use_offline:
            return self._synthetic_seeds(kws)

        logger.info(f"[seed] Searching {len(kws)} keywords with {self._max_workers} workers")
        hit_counter: Counter  = Counter()
        keyword_results: Dict = {}

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(self._search_keyword, kw): kw for kw in kws}
            for future in as_completed(futures):
                kw = futures[future]
                try:
                    users = future.result()
                except Exception as e:
                    logger.error(f"[seed] '{kw}' failed: {e}")
                    users = []

                usernames = []
                for u in users:
                    uname = u.get("username", "").lower().strip()
                    if uname:
                        usernames.append(uname)
                        hit_counter[uname] += 1
                        # Cache full user data including user_id
                        if uname not in self.user_cache:
                            self.user_cache[uname] = u

                keyword_results[kw] = usernames
                logger.info(f"[seed] '{kw}' → {len(usernames)} accounts")

        qualified = {u for u, c in hit_counter.items() if c >= self._min_hits}
        ranked    = sorted(qualified, key=lambda u: hit_counter[u], reverse=True)

        # Log how many have user_id cached
        with_uid = sum(1 for u in ranked if self.user_cache.get(u, {}).get("pk"))
        logger.info(
            f"[seed] Done | unique={len(hit_counter)} "
            f"qualified={len(ranked)} with_user_id={with_uid}"
        )
        return ranked

    def get_cached_user_id(self, username: str) -> Optional[str]:
        """Return cached user_id for a username found during search."""
        user = self.user_cache.get(username.lower(), {})
        uid  = user.get("pk") or user.get("pk_id") or user.get("id")
        return str(uid) if uid else None

    def get_cached_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """Return cached profile data for a username found during search."""
        return self.user_cache.get(username.lower())

    def export(self, usernames: List[str], path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "seed_count": len(usernames),
                "usernames":  usernames,
                "user_cache": self.user_cache,
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"[seed] Exported {len(usernames)} seeds → {path}")
        return path

    def load(self, path: Path) -> List[str]:
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            usernames        = data.get("usernames", [])
            self.user_cache  = data.get("user_cache", {})
        else:
            usernames = data
        logger.info(f"[seed] Loaded {len(usernames)} seeds from {path}")
        return usernames

    # ------------------------------------------------------------------
    # Search — returns List[Dict] with full user objects
    # ------------------------------------------------------------------

    def _search_keyword(self, keyword: str) -> List[Dict[str, Any]]:
        """Search and return full user dicts (not just usernames)."""
        users = self._search_via_api(keyword)
        if not users:
            users = self._search_via_page_extract(keyword)
        time.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
        return users

    def _search_via_api(self, keyword: str) -> List[Dict[str, Any]]:
        """
        Use /api/v1/users/search/?q={keyword}
        Returns full user objects including pk (user_id).
        """
        try:
            resp = self._scraper._session.get(
                _API_SEARCH,
                params={"q": keyword, "count": self._limit},
                headers={
                    "Accept": "*/*",
                    "X-IG-App-ID": "238260118697367",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                },
                proxies=self._scraper._get_proxies(),
                timeout=self._scraper.timeout,
            )
            if resp.status_code != 200:
                logger.debug(f"[seed] API search {resp.status_code} for '{keyword}'")
                return []
            data  = resp.json()
            users = data.get("users") or []
            logger.debug(f"[seed] API returned {len(users)} users for '{keyword}'")
            return users
        except Exception as e:
            logger.debug(f"[seed] API search failed for '{keyword}': {e}")
            return []

    def _extract_users_from_dict(self, data: Any, users: List[Dict[str, Any]], seen: Set[str]) -> None:
        """Recursively traverse dicts/lists to safely extract user info containing pk and username."""
        if isinstance(data, dict):
            if ("pk" in data or "pk_id" in data or "id" in data) and "username" in data:
                pk = data.get("pk") or data.get("pk_id") or data.get("id")
                uname = str(data.get("username", "")).lower().strip()
                if uname and uname not in seen and not uname.startswith("__"):
                    seen.add(uname)
                    users.append({"pk": str(pk), "username": uname})
            for v in data.values():
                self._extract_users_from_dict(v, users, seen)
        elif isinstance(data, list):
            for item in data:
                self._extract_users_from_dict(item, users, seen)

    def _search_via_page_extract(self, keyword: str) -> List[Dict[str, Any]]:
        """
        Scrape /search?q={keyword} and extract user data from JSON blobs.
        Parses full JSON objects from script tags to avoid regex fragility.
        """
        try:
            resp = self._scraper._session.get(
                _SEARCH_URL,
                params={"q": keyword, "serp_type": "default"},
                headers={
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                },
                proxies=self._scraper._get_proxies(),
                timeout=self._scraper.timeout,
            )
            if resp.status_code != 200:
                return []

            html  = resp.text
            users: List[Dict[str, Any]] = []
            seen:  Set[str] = set()

            # Safely extract all JSON blobs inside <script> tags
            script_blobs = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
            for blob in script_blobs:
                blob = blob.strip()
                # Meta usually wraps state in {"require":...} or similar JSON
                if blob.startswith('{') and blob.endswith('}') and '"username"' in blob:
                    try:
                        data = json.loads(blob)
                        self._extract_users_from_dict(data, users, seen)
                    except json.JSONDecodeError:
                        pass

            # Fallback: loose extraction only if JSON parsing yields nothing
            if not users:
                for m in re.finditer(r'"username"\s*:\s*"([^"]{2,30})"', html):
                    uname = m.group(1).lower().strip()
                    if uname not in seen and not uname.startswith("__"):
                        seen.add(uname)
                        users.append({"username": uname})

            return users[:self._limit]
        except Exception as e:
            logger.debug(f"[seed] Page search failed for '{keyword}': {e}")
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _synthetic_seeds(keywords: List[str]) -> List[str]:
        stem_map = {
            "tarot":          ["tarotbytara", "dailytarot", "tarotwithluna"],
            "yi ching":       ["yiching_master", "iching_wisdom"],
            "astrology":      ["astro_insights", "cosmiccharts"],
            "psychic reading":["psychic_sarah", "intuitive_guide"],
            "numerology":     ["numberspath", "sacred_numerology"],
        }
        seeds: List[str] = []
        seen:  Set[str]  = set()
        for kw in keywords:
            matches = stem_map.get(kw.lower(), [f"{re.sub(chr(32),'_',kw.lower())}_kol"])
            for s in matches:
                if s not in seen:
                    seen.add(s)
                    seeds.append(s)
        return seeds

    def _load_config(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._cfg.update(yaml.safe_load(f) or {})
            logger.info(f"[seed] Loaded config from {path}")
        except Exception as e:
            logger.warning(f"[seed] Could not load config: {e}")
