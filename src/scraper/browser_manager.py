"""
browser_manager.py
==================
Manages a stealth Playwright browser instance with persistent login context,
proxy routing, and anti-fingerprinting measures.

This module handles the "Initialization" phase of the pipeline:
  1. Launch a Chromium instance with a persistent user-data directory
     (preserves cookies / localStorage / login sessions across runs).
  2. Route all traffic through a configurable proxy server.
  3. Apply stealth patches to defeat WebDriver detection (navigator.webdriver,
     Chrome DevTools Protocol leaks, permission anomalies, etc.).

Public API
----------
  mgr = BrowserManager(config_dir, proxy="http://127.0.0.1:10808")
  page = await mgr.launch()        # returns a ready-to-use Page
  await mgr.close()                # graceful teardown

  # or use as async context manager:
  async with BrowserManager(...) as page:
      await page.goto("https://www.threads.net")
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

import yaml
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Stealth injection scripts
# ---------------------------------------------------------------------------
# These JS snippets run in every new page/frame BEFORE the site's own code.
# They patch the most common WebDriver detection vectors used by Meta / Threads.

_STEALTH_SCRIPTS: list[str] = [
    # 1. Hide navigator.webdriver flag
    """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
    });
    """,

    # 2. Fake Chrome runtime object (missing in headless / automation)
    """
    window.chrome = {
        runtime: {
            onConnect: { addListener: function() {} },
            onMessage: { addListener: function() {} },
        },
        loadTimes: function() { return {}; },
        csi: function() { return {}; },
    };
    """,

    # 3. Fake Notification permission (headless returns 'denied' by default)
    """
    const originalQuery = window.Notification
        ? window.Notification.permission
        : 'default';
    if (window.Notification) {
        Object.defineProperty(Notification, 'permission', {
            get: () => originalQuery === 'denied' ? 'default' : originalQuery,
        });
    }
    """,

    # 4. Fix navigator.plugins length (headless has 0, real Chrome has ≥3)
    """
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ];
            plugins.length = 3;
            return plugins;
        },
    });
    """,

    # 5. Fix navigator.languages (headless sometimes returns empty array)
    """
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en', 'zh-CN', 'zh'],
    });
    """,

    # 6. Spoof WebGL vendor & renderer (avoid "SwiftShader" headless fingerprint)
    """
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (NVIDIA)';
        if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650, OpenGL 4.5)';
        return getParameter.call(this, param);
    };
    """,

    # 7. Hide CDP Runtime.enable detection
    """
    const origGet = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden');
    // Prevent detection of devtools protocol attachment
    if (window.cdc_adoQpoasnfa76pfcZLmcfl_Array ||
        window.cdc_adoQpoasnfa76pfcZLmcfl_Promise ||
        window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol) {
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
    }
    """,
]

# ---------------------------------------------------------------------------
# Realistic browser launch arguments
# ---------------------------------------------------------------------------

_CHROMIUM_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",   # Core: removes automation flag
    "--disable-infobars",                              # Hide "Chrome is being controlled"
    "--disable-dev-shm-usage",                         # Avoid /dev/shm issues on Linux
    "--disable-background-timer-throttling",            # Keep timers accurate
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--disable-extensions",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-service-autorun",
    "--password-store=basic",
]

# Default viewport dimensions — randomized slightly per session
_DEFAULT_VIEWPORT = {"width": 1440, "height": 900}

# Realistic desktop User-Agent string
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


class BrowserManager:
    """
    Launches and manages a stealth Playwright Chromium browser.

    Features:
      - Persistent context (preserves login state across runs)
      - Proxy routing (supports HTTP/SOCKS5)
      - Anti-fingerprinting stealth patches
      - Human-like viewport randomization
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        profile_name: str = "default",
        proxy: Optional[Union[str, Callable[[], Optional[str]]]] = None,
        headless: bool = False,
        user_agent: Optional[str] = None,
        viewport: Optional[Dict[str, int]] = None,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
    ) -> None:
        """
        Args:
            config_dir:   Path to the project config directory.
            profile_name: Name of the browser profile subdirectory.
                          Each alt account should use a different name
                          (e.g. "acct_01", "acct_02") for full isolation.
            proxy:        Proxy URL, e.g. "http://127.0.0.1:10808"
                          or "socks5://user:pass@host:port".
            headless:     Run browser in headless mode (not recommended for
                          stealth — use headed mode to avoid detection).
            user_agent:   Override the default User-Agent string.
            viewport:     Override default viewport {width, height}.
            locale:       Browser locale for Accept-Language and JS APIs.
            timezone_id:  IANA timezone for Date/Intl JS APIs.
        """
        self._config_dir = Path(config_dir)
        self._profile_dir = self._config_dir / "browser_profiles" / profile_name
        self._profile_dir.mkdir(parents=True, exist_ok=True)

        self._proxy = proxy
        self._headless = headless
        self._locale = locale
        self._timezone_id = timezone_id

        # Add slight randomization to viewport to defeat canvas fingerprinting
        base_vp = viewport or dict(_DEFAULT_VIEWPORT)
        self._viewport = {
            "width": base_vp["width"] + random.randint(-10, 10),
            "height": base_vp["height"] + random.randint(-5, 5),
        }

        self._user_agent = user_agent or _DEFAULT_USER_AGENT

        # Internal state
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        logger.info(
            f"BrowserManager init | profile={profile_name} "
            f"proxy={'set' if proxy else 'none'} headless={headless}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def launch(self) -> Page:
        """
        Start Playwright, open a persistent browser context, apply stealth
        patches, and return the main Page object.

        The persistent context stores all cookies, localStorage, and session
        data under config/browser_profiles/<profile_name>/. On the next run,
        Threads will see the same logged-in session without re-authentication.
        """
        self._playwright = await async_playwright().start()

        # Build context options
        ctx_options: Dict[str, Any] = {
            "user_data_dir": str(self._profile_dir),
            "headless": self._headless,
            "args": _CHROMIUM_ARGS,
            "ignore_default_args": ["--enable-automation"],
            "viewport": self._viewport,
            "user_agent": self._user_agent,
            "locale": self._locale,
            "timezone_id": self._timezone_id,
            "color_scheme": "light",
            "permissions": ["geolocation"],
            "bypass_csp": True,                # Allow our stealth scripts to run
            "java_script_enabled": True,
        }

        # Configure proxy dynamically if available
        resolved_proxy = self._resolve_proxy()
        if resolved_proxy:
            ctx_options["proxy"] = self._build_proxy_config(resolved_proxy)
            logger.info(f"Browser proxy configured: {resolved_proxy}")

        # Load existing cookies from config if available
        cookies_path = self._config_dir / "cookie.json"
        if cookies_path.exists():
            logger.info(f"Found cookie.json, will inject after launch")

        # Launch persistent Chromium context
        logger.info(
            f"Launching Chromium | profile_dir={self._profile_dir} "
            f"viewport={self._viewport['width']}x{self._viewport['height']}"
        )
        self._context = await self._playwright.chromium.launch_persistent_context(
            **ctx_options
        )

        # Apply stealth scripts to every page/frame
        for script in _STEALTH_SCRIPTS:
            await self._context.add_init_script(script)
        logger.info(f"Applied {len(_STEALTH_SCRIPTS)} stealth patches")

        # Inject cookies from cookie.json if available
        if cookies_path.exists():
            await self._inject_cookies(cookies_path)

        # Grab the first (default) page or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        logger.info("Browser launched and stealth-configured successfully")
        return self._page

    async def close(self) -> None:
        """Gracefully shut down the browser and Playwright."""
        if self._context:
            # Save current cookies back to cookie.json for persistence
            try:
                await self._export_cookies()
            except Exception as e:
                logger.warning(f"Failed to export cookies on close: {e}")

            await self._context.close()
            self._context = None
            logger.info("Browser context closed")

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
            logger.info("Playwright stopped")

    @property
    def context(self) -> Optional[BrowserContext]:
        """Access the underlying BrowserContext for advanced operations."""
        return self._context

    @property
    def page(self) -> Optional[Page]:
        """Access the current active Page."""
        return self._page

    # ------------------------------------------------------------------
    # Async context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Page:
        return await self.launch()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_proxy(self) -> Optional[str]:
        """
        Dynamically resolve the proxy URL at launch time.
        Priority:
          1. Explicitly passed proxy (if callable, invoke it; if string, use it unless 'auto').
          2. Environment variables: THREADS_PROXY, HTTPS_PROXY, HTTP_PROXY, ALL_PROXY.
          3. config/settings.yaml: 'proxy', 'default_proxy', or rotating from proxies.json when 'use_proxies' is true.
        """
        if callable(self._proxy):
            try:
                res = self._proxy()
                if res:
                    return str(res)
            except Exception as e:
                logger.warning(f"Callable proxy getter failed: {e}")

        # "direct" or "none" explicitly disables proxy (skips env var / config fallback)
        if isinstance(self._proxy, str) and self._proxy.strip().lower() in ("direct", "none", "off"):
            logger.info("Proxy explicitly disabled (direct mode)")
            return None

        if isinstance(self._proxy, str) and self._proxy.strip() and self._proxy.lower() != "auto":
            return self._proxy.strip()

        # Check environment variables
        for env_key in ("THREADS_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
            val = os.environ.get(env_key)
            if val and val.strip():
                logger.info(f"Resolved proxy from env var {env_key}")
                return val.strip()

        # Check settings.yaml
        settings_path = self._config_dir / "settings.yaml"
        if settings_path.exists():
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = yaml.safe_load(f) or {}

                # Direct proxy setting in settings.yaml
                for key in ("proxy", "default_proxy", "threads_proxy"):
                    val = settings.get(key)
                    if isinstance(val, str) and val.strip():
                        logger.info(f"Resolved proxy from settings.yaml ({key})")
                        return val.strip()

                # If use_proxies is true, try to load from data/raw/proxies.json
                if settings.get("use_proxies", False):
                    proxies_path = self._config_dir.parent / "data" / "raw" / "proxies.json"
                    if proxies_path.exists():
                        with open(proxies_path, "r", encoding="utf-8") as pf:
                            pdata = json.load(pf)
                        if isinstance(pdata, list) and pdata:
                            valid_p = [p for p in pdata if isinstance(p, dict)]
                            if valid_p:
                                chosen = random.choice(valid_p)
                                proxy_val = chosen.get("https") or chosen.get("http") or chosen.get("server") or next(iter(chosen.values()), None)
                                if proxy_val and isinstance(proxy_val, str):
                                    logger.info("Resolved proxy from data/raw/proxies.json pool")
                                    return proxy_val.strip()
            except Exception as e:
                logger.warning(f"Failed to resolve proxy from config files: {e}")

        return None

    @staticmethod
    def _build_proxy_config(proxy_url: str) -> Dict[str, str]:
        """
        Convert a proxy URL string into the dict format Playwright expects.
        Supports: http://, https://, socks5://
        Supports auth: http://user:pass@host:port
        """
        config: Dict[str, str] = {"server": proxy_url}

        # Extract username:password if present
        if "@" in proxy_url:
            # e.g. "http://user:pass@host:port"
            proto_rest = proxy_url.split("://", 1)
            if len(proto_rest) == 2:
                auth_host = proto_rest[1]
                if "@" in auth_host:
                    auth, host = auth_host.rsplit("@", 1)
                    if ":" in auth:
                        username, password = auth.split(":", 1)
                        config["server"] = f"{proto_rest[0]}://{host}"
                        config["username"] = username
                        config["password"] = password

        return config

    async def _inject_cookies(self, cookies_path: Path) -> None:
        """
        Read cookies from cookie.json and inject them into the browser context.
        Supports both Playwright cookie format and Netscape/browser-export format.
        """
        try:
            with open(cookies_path, "r", encoding="utf-8") as f:
                raw_cookies = json.load(f)

            if not isinstance(raw_cookies, list):
                logger.warning("cookie.json is not a list, skipping injection")
                return

            # Normalize cookies to Playwright format
            pw_cookies = []
            for c in raw_cookies:
                # Auto-correct common domain mismatch (.threads.com → .threads.net)
                domain = c.get("domain", ".threads.net")
                if isinstance(domain, str):
                    domain = domain.replace(".threads.com", ".threads.net")
                    if domain == "threads.com":
                        domain = ".threads.net"

                cookie: Dict[str, Any] = {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": domain,
                    "path": c.get("path", "/"),
                }
                # Handle sameSite (Playwright requires specific casing)
                # Also normalize browser-export format "no_restriction" → "None"
                same_site = c.get("sameSite") or "Lax"
                if isinstance(same_site, str):
                    normalized = same_site.lower().strip()
                    if normalized in ("no_restriction", "none"):
                        cookie["sameSite"] = "None"
                    elif normalized in ("strict", "lax"):
                        cookie["sameSite"] = normalized.capitalize()
                    else:
                        cookie["sameSite"] = "Lax"
                else:
                    cookie["sameSite"] = "Lax"

                # Handle secure flag
                cookie["secure"] = c.get("secure", False)

                # Handle httpOnly flag
                cookie["httpOnly"] = c.get("httpOnly", False)

                # Handle expiry (Playwright uses 'expires' as unix timestamp)
                if "expirationDate" in c:
                    cookie["expires"] = c["expirationDate"]
                elif "expires" in c:
                    cookie["expires"] = c["expires"]

                if cookie["name"] and cookie["value"]:
                    pw_cookies.append(cookie)

            if pw_cookies:
                await self._context.add_cookies(pw_cookies)
                logger.info(
                    f"Injected {len(pw_cookies)} cookies from {cookies_path.name}"
                )
            else:
                logger.warning("No valid cookies found in cookie.json")

        except Exception as e:
            logger.error(f"Failed to inject cookies: {e}")

    async def _export_cookies(self) -> None:
        """Save current browser cookies back to cookie.json for next session."""
        if not self._context:
            return

        cookies = await self._context.cookies()
        if not cookies:
            return

        out_path = self._config_dir / "cookie.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)

        logger.info(f"Exported {len(cookies)} cookies to {out_path.name}")
"""
browser_manager.py — Module Summary
=====================================
This module provides a stealth-configured Playwright browser that:
  - Uses persistent context to maintain login across sessions
  - Routes traffic through a configurable proxy
  - Applies 7 anti-detection stealth patches
  - Randomizes viewport to defeat canvas fingerprinting
  - Auto-injects and exports cookies from config/cookie.json
"""
