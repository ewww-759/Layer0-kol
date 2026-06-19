"""
threads_scraper.py — v3
=======================
Key fix: accepts pre-resolved user_id from SeedDiscoverer cache,
completely bypassing the broken /api/v1/users/by/username/ endpoint.

The user_id is now resolved in this priority order:
  1. Passed directly via uid_cache (from SeedDiscoverer)
  2. Extracted from /search results (already done by SeedDiscoverer)
  3. GraphQL fallback (last resort)
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from curl_cffi import requests

from .utils.error_handler import retry
from .utils.logger import get_logger
from .utils.proxy_manager import ProxyManager

logger = get_logger(__name__)

_BASE_URL   = "https://www.threads.net"
_API_BASE   = "https://www.threads.net/api/v1"
_API_SEARCH = "https://www.threads.net/api/v1/users/search/"

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-Ch-Ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}

_OFFLINE_FIXTURE = "sample_threads.json"
_DELAY_MIN = 1.5
_DELAY_MAX = 3.0


class ThreadsScraper:

    def __init__(
        self,
        settings: Dict[str, Any],
        config_dir: Path,
        data_dir: Path,
    ) -> None:
        self.settings    = settings
        self.data_dir    = Path(data_dir)
        self.timeout     = int(settings.get("timeout", 30))
        self.use_offline = bool(settings.get("use_offline", False))

        # Check for cookie.json first, then fallback to .env/settings
        cookie_path = config_dir / "cookie.json"
        if cookie_path.exists():
            try:
                with open(cookie_path, "r", encoding="utf-8") as f:
                    self._cookie = f.read()
            except Exception as e:
                logger.error(f"Failed to read cookie.json: {e}")
                self._cookie = ""
        else:
            self._cookie = (
                os.environ.get("THREADS_COOKIE")
                or settings.get("cookie", "")
                or ""
            )

        proxies_path    = self.data_dir / "raw" / "proxies.json"
        self._proxy_mgr = (
            ProxyManager(proxies_path)
            if settings.get("use_proxies", False) else None
        )

        self._session    = self._build_session()
        # uid_cache can be pre-populated by SeedDiscoverer
        self._uid_cache: Dict[str, str] = {}

        logger.info(
            f"ThreadsScraper ready | offline={self.use_offline} "
            f"cookie={'set' if self._cookie else 'NOT SET'}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_user_threads(
        self, username: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        if self.use_offline:
            return self._load_offline_fixture(username, limit)

        # RSSHub doesn't require user_id, so we bypass the lookup completely
        return self._fetch_posts("", username, limit)

    def fetch_user_profile(self, username: str) -> Dict[str, Any]:
        if self.use_offline:
            return self._offline_profile(username)

        user_id = self._resolve_user_id(username)
        if not user_id:
            return self._empty_profile(username)
        return self._fetch_profile(user_id, username)

    def fetch_post_replies(self, post_id: str) -> List[Dict[str, Any]]:
        if self.use_offline:
            return []
        return self._fetch_replies(post_id)

    def seed_uid_cache(self, uid_map: Dict[str, str]) -> None:
        """
        Pre-populate user_id cache from SeedDiscoverer.
        Call this after discover() to avoid redundant user_id lookups.
        uid_map: {username → user_id}
        """
        self._uid_cache.update(uid_map)
        logger.info(f"[scraper] Pre-seeded uid_cache with {len(uid_map)} entries")

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _build_session(self) -> requests.Session:
        session = requests.Session(impersonate="chrome120")
        headers = dict(_BASE_HEADERS)
        
        cookie_str = self._cookie.strip() if self._cookie else ""
        if cookie_str:
            parsed_cookie = ""
            # Check if it's a JSON array (e.g. from EditThisCookie)
            if cookie_str.startswith("[") and cookie_str.endswith("]"):
                try:
                    cookie_list = json.loads(cookie_str)
                    pairs = [f"{c['name']}={c['value']}" for c in cookie_list if "name" in c and "value" in c]
                    parsed_cookie = "; ".join(pairs)
                except Exception as e:
                    logger.error(f"Failed to parse JSON cookie: {e}")
                    parsed_cookie = cookie_str
            # Check if it's a JSON object
            elif cookie_str.startswith("{") and cookie_str.endswith("}"):
                try:
                    cookie_dict = json.loads(cookie_str)
                    pairs = [f"{k}={v}" for k, v in cookie_dict.items()]
                    parsed_cookie = "; ".join(pairs)
                except Exception as e:
                    logger.error(f"Failed to parse JSON dict cookie: {e}")
                    parsed_cookie = cookie_str
            else:
                parsed_cookie = cookie_str
            
            headers["Cookie"] = parsed_cookie
            csrf = re.search(r"csrftoken=([^;]+)", parsed_cookie)
            if csrf:
                headers["X-CSRFToken"] = csrf.group(1)
        session.headers.update(headers)

        # Proxy — reads from settings.yaml or env
        proxy_url = (
            self.settings.get("https_proxy")
            or self.settings.get("http_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}
            logger.info(f"ThreadsScraper using proxy: {proxy_url}")

        return session

    def _get_proxies(self) -> Optional[Dict]:
        if self._proxy_mgr:
            return self._proxy_mgr.get_proxy()
        if self._session.proxies:
            return self._session.proxies
        return None

    # ------------------------------------------------------------------
    # User ID resolution
    # Priority: cache → search API → GraphQL fallback
    # ------------------------------------------------------------------

    def _resolve_user_id(self, username: str) -> Optional[str]:
        username = username.lower().strip()

        # 1. Check cache (pre-populated by SeedDiscoverer or previous lookups)
        if username in self._uid_cache:
            return self._uid_cache[username]

        # 2. Try search API — search for exact username, extract pk
        uid = self._uid_via_search(username)
        if uid:
            self._uid_cache[username] = uid
            logger.debug(f"Resolved @{username} → {uid} via search")
            return uid

        # 3. GraphQL fallback
        uid = self._uid_via_graphql(username)
        if uid:
            self._uid_cache[username] = uid
            logger.debug(f"Resolved @{username} → {uid} via GraphQL")
            return uid

        logger.warning(f"Could not resolve user_id for @{username}")
        return None

    def _uid_via_search(self, username: str) -> Optional[str]:
        """Search for the exact username and extract pk from results."""
        try:
            resp = self._session.get(
                _API_SEARCH,
                params={"q": username, "count": 5},
                headers={
                    "Accept": "*/*",
                    "X-IG-App-ID": "238260118697367",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                },
                proxies=self._get_proxies(),
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data  = resp.json()
            users = data.get("users") or []
            # Find exact username match
            for user in users:
                if user.get("username", "").lower() == username:
                    uid = user.get("pk") or user.get("pk_id") or user.get("id")
                    if uid:
                        return str(uid)
            # If no exact match, take first result
            if users:
                uid = users[0].get("pk") or users[0].get("pk_id")
                if uid:
                    return str(uid)
        except Exception as e:
            logger.debug(f"_uid_via_search failed for @{username}: {e}")
        return None

    @retry(exceptions=(Exception,), tries=2, delay=2.0, backoff=2.0)
    def _uid_via_graphql(self, username: str) -> Optional[str]:
        """Last resort: try GraphQL user lookup."""
        try:
            resp = self._session.post(
                "https://www.threads.net/api/graphql",
                data={
                    "variables": json.dumps({"username": username}),
                    "doc_id": "23996318473300828",
                },
                headers={
                    "Accept": "*/*",
                    "X-IG-App-ID": "238260118697367",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                proxies=self._get_proxies(),
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            user = (
                (data.get("data") or {}).get("xdt_user_by_username")
                or (data.get("data") or {}).get("userData", {}).get("user")
                or {}
            )
            uid = user.get("id") or user.get("pk")
            return str(uid) if uid else None
        except Exception as e:
            logger.debug(f"_uid_via_graphql failed for @{username}: {e}")
        return None

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------

    def _fetch_posts(
        self, user_id: str, username: str, limit: int
    ) -> List[Dict[str, Any]]:
        """Fetch posts using local RSSHub instance."""
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        
        # Use local RSSHub for pure content scraping (corrected 'hreads' typo to standard RSSHub route)
        url = f"http://localhost:1200/threads/{username}"
        items = []
        try:
            resp = self._session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning(f"RSSHub API {resp.status_code} for @{username}")
                return items
            
            root = ET.fromstring(resp.text)
            for item in root.findall('./channel/item'):
                title = item.find('title')
                desc = item.find('description')
                link = item.find('link')
                pubDate = item.find('pubDate')
                
                text = desc.text if desc is not None and desc.text else (title.text if title is not None else "")
                
                created_at = ""
                if pubDate is not None and pubDate.text:
                    try:
                        dt = parsedate_to_datetime(pubDate.text)
                        created_at = dt.isoformat()
                    except Exception:
                        created_at = pubDate.text

                code = ""
                if link is not None and link.text:
                    match = re.search(r'/post/([^/?]+)', link.text)
                    if match:
                        code = match.group(1)

                items.append({
                    "id": code or f"rss_{len(items)}",
                    "username": username,
                    "text": text,
                    "like_count": 0,
                    "reply_count": 0,
                    "repost_count": 0,
                    "created_at": created_at,
                    "url": link.text if link is not None else "",
                })
                if len(items) >= limit:
                    break
        except Exception as e:
            logger.error(f"RSSHub fetch failed for @{username}: {e}")
            
        logger.info(f"@{username}: fetched {len(items)} posts from RSSHub")
        return items[:limit]

    def _normalize_post(
        self, item: Dict[str, Any], username: str
    ) -> Optional[Dict[str, Any]]:
        try:
            pid     = item.get("pk") or item.get("id")
            if not pid:
                return None
            caption = item.get("caption") or {}
            text    = caption.get("text", "") if isinstance(caption, dict) else ""
            code    = item.get("code") or ""
            user_obj = item.get("user") or {}
            return {
                "id":           str(pid),
                "username":     user_obj.get("username") or username,
                "text":         text.strip(),
                "like_count":   int(item.get("like_count") or 0),
                "reply_count":  int(
                    item.get("text_post_app_info", {}).get("direct_reply_count")
                    or item.get("comment_count") or 0
                ),
                "repost_count": int(
                    item.get("text_post_app_info", {}).get("repost_count")
                    or item.get("repost_count") or 0
                ),
                "created_at":   item.get("taken_at") or "",
                "url":          f"https://www.threads.net/t/{code}" if code else "",
            }
        except Exception as e:
            logger.debug(f"_normalize_post error: {e}")
            return None

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def _fetch_profile(
        self, user_id: str, username: str
    ) -> Dict[str, Any]:
        url     = f"{_API_BASE}/users/{user_id}/info/"
        headers = {
            "Accept": "*/*",
            "X-IG-App-ID": "238260118697367",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
        }
        try:
            resp = self._session.get(
                url, headers=headers,
                proxies=self._get_proxies(), timeout=self.timeout,
            )
            if resp.status_code != 200:
                return self._empty_profile(username)
            data = resp.json()
            user = data.get("user") or {}
            return {
                "user_id":         str(user.get("pk") or user_id),
                "username":        user.get("username") or username,
                "full_name":       user.get("full_name") or "",
                "bio":             (user.get("biography") or "").strip(),
                "follower_count":  int(user.get("follower_count") or 0),
                "following_count": int(user.get("following_count") or 0),
                "is_verified":     bool(
                    user.get("is_verified") or user.get("is_blue_verified")
                ),
                "external_url":    user.get("external_url") or "",
                "profile_pic_url": user.get("profile_pic_url") or "",
            }
        except Exception as e:
            logger.error(f"Profile fetch failed uid={user_id}: {e}")
            return self._empty_profile(username)

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------

    def _fetch_replies(self, post_id: str) -> List[Dict[str, Any]]:
        url     = f"{_API_BASE}/media/{post_id}/replies/"
        headers = {
            "Accept": "*/*",
            "X-IG-App-ID": "238260118697367",
        }
        try:
            resp = self._session.get(
                url, headers=headers,
                proxies=self._get_proxies(), timeout=self.timeout,
            )
            if resp.status_code != 200:
                return []
            data    = resp.json()
            replies = []
            for item in (data.get("caption_responses") or data.get("items") or []):
                user_obj = item.get("user") or {}
                caption  = item.get("caption") or {}
                text     = caption.get("text", "") if isinstance(caption, dict) else ""
                replies.append({
                    "id":         str(item.get("pk") or ""),
                    "username":   user_obj.get("username", ""),
                    "text":       text.strip(),
                    "like_count": int(item.get("like_count") or 0),
                    "created_at": item.get("taken_at") or "",
                })
            return replies
        except Exception as e:
            logger.error(f"Replies fetch failed post={post_id}: {e}")
            return []

    # ------------------------------------------------------------------
    # Offline / helpers
    # ------------------------------------------------------------------

    def _load_offline_fixture(
        self, username: str, limit: int
    ) -> List[Dict[str, Any]]:
        fixture = self.data_dir / "raw" / _OFFLINE_FIXTURE
        if fixture.exists():
            try:
                with open(fixture, "r", encoding="utf-8") as f:
                    records = json.load(f)
                filtered = [r for r in records if r.get("username") == username]
                return (filtered or records)[:limit]
            except Exception as e:
                logger.warning(f"Could not read fixture: {e}")
        return self._synthetic_records(username, limit)

    def _offline_profile(self, username: str) -> Dict[str, Any]:
        return {
            "user_id": "000000000", "username": username,
            "full_name": f"{username} (offline)", "bio": "Offline mode.",
            "follower_count": 10000, "following_count": 500,
            "is_verified": False, "external_url": "", "profile_pic_url": "",
        }

    @staticmethod
    def _empty_profile(username: str) -> Dict[str, Any]:
        return {
            "user_id": "", "username": username, "full_name": "", "bio": "",
            "follower_count": 0, "following_count": 0,
            "is_verified": False, "external_url": "", "profile_pic_url": "",
        }

    @staticmethod
    def _synthetic_records(username: str, count: int) -> List[Dict[str, Any]]:
        texts = [
            "Daily tarot pull — link in bio 🔮",
            "DM me for a reading! Limited slots.",
            "Mercury retrograde survival guide 🌀",
            "Full moon ritual tonight ✨",
            "Use code COSMIC20 for my oracle deck",
        ]
        return [
            {
                "id":           f"syn_{username}_{i:04d}",
                "username":     username,
                "text":         texts[i % len(texts)],
                "like_count":   random.randint(50, 3000),
                "reply_count":  random.randint(5, 200),
                "repost_count": random.randint(2, 100),
                "created_at":   int(time.time()) - (i * 3600),
                "url":          f"https://www.threads.net/@{username}/post/syn_{i:04d}",
            }
            for i in range(count)
        ]

    @staticmethod
    def _polite_delay() -> None:
        time.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
