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
import random
from pathlib import Path
from typing import Any, Dict, Optional

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
        proxy: Optional[str] = None,
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

        # Configure proxy if provided
        if self._proxy:
            ctx_options["proxy"] = self._build_proxy_config(self._proxy)
            logger.info(f"Browser proxy configured: {self._proxy}")

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

        # Set extra HTTP headers for realism
        await self._page.set_extra_http_headers({
            "Accept-Language": f"{self._locale},en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Sec-Ch-Ua": (
                '"Chromium";v="148", "Google Chrome";v="148", '
                '"Not/A)Brand";v="99"'
            ),
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
        })

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
                cookie: Dict[str, Any] = {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ".threads.net"),
                    "path": c.get("path", "/"),
                }
                # Handle sameSite (Playwright requires specific casing)
                same_site = c.get("sameSite") or "Lax"
                if isinstance(same_site, str) and same_site.lower() in ("strict", "lax", "none"):
                    cookie["sameSite"] = same_site.capitalize()
                    if cookie["sameSite"] == "None":
                        cookie["sameSite"] = "None"
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
