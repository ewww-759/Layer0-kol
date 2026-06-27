"""
llm_client.py
=============
Unified LLM client that supports switching between cloud APIs and
local Ollama models.

This is a drop-in module that any layer in the pipeline can import:

    from .llm_client import LLMClient

    llm = LLMClient(config_path=Path("config/llm_config.yaml"))
    answer = llm.generate("Summarize this bio", system_prompt="You are a classifier.")

Supported backends
------------------
  openai  — OpenAI API (GPT-4o-mini, GPT-4o, etc.) via the ``openai`` package
  gemini  — Google Gemini API via the ``google-genai`` package
  ollama  — Local Ollama instance via HTTP POST

Configuration
-------------
All settings are read from ``config/llm_config.yaml``.
API keys can be specified in YAML or via environment variables
(OPENAI_API_KEY, GEMINI_API_KEY).  Env vars take effect only when
the YAML value is empty.

Error handling
--------------
If a backend call fails, the error is logged and an empty string is
returned — the pipeline is never crashed by an LLM failure.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import yaml

from .utils.logger import get_logger

logger = get_logger(__name__)

# Supported backend identifiers
_VALID_BACKENDS = {"openai", "gemini", "ollama"}


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class LLMClient:
    """
    Unified LLM client with pluggable backends.

    Parameters
    ----------
    config_path : Path
        Path to ``llm_config.yaml``.  Relative paths are resolved from
        the current working directory.

    Public API
    ----------
    generate(prompt, system_prompt="") -> str
        Send a prompt and return the model's text response.

    is_available() -> bool
        Check whether the configured backend is reachable.

    backend : str  (property)
        Name of the active backend ("openai" / "gemini" / "ollama").
    """

    # ------------------------------------------------------------------ init
    def __init__(self, config_path: Optional[Path] = None) -> None:
        if config_path is None:
            config_path = Path("config/llm_config.yaml")
        self._config_path = Path(config_path)
        self._raw_config: Dict[str, Any] = {}
        self._backend_name: str = ""

        self._load_config()
        logger.info(
            "LLMClient initialised  backend=%s  model=%s",
            self._backend_name,
            self._model,
        )

    # ------------------------------------------------------------- properties
    @property
    def backend(self) -> str:
        """Return the name of the currently active backend."""
        return self._backend_name

    # ----------------------------------------------------------- config load
    def _load_config(self) -> None:
        """Parse YAML config and resolve backend-specific settings."""
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                self._raw_config = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            logger.error("Config file not found: %s", self._config_path)
            raise
        except yaml.YAMLError as exc:
            logger.error("Failed to parse YAML config: %s", exc)
            raise

        # -- backend selection
        self._backend_name = str(self._raw_config.get("backend", "gemini")).lower()
        if self._backend_name not in _VALID_BACKENDS:
            logger.warning(
                "Unknown backend '%s', falling back to 'gemini'",
                self._backend_name,
            )
            self._backend_name = "gemini"

        # -- per-backend settings
        section: Dict[str, Any] = self._raw_config.get(self._backend_name, {})
        self._model: str = section.get("model", "")
        self._temperature: float = float(section.get("temperature", 0.3))
        self._max_tokens: int = int(section.get("max_tokens", 1024))

        # -- API key resolution (YAML first, then env var fallback)
        if self._backend_name == "openai":
            yaml_key = section.get("api_key", "")
            self._api_key = yaml_key or os.getenv("OPENAI_API_KEY", "")
        elif self._backend_name == "gemini":
            yaml_key = section.get("api_key", "")
            self._api_key = yaml_key or os.getenv("GEMINI_API_KEY", "")
        else:
            # Ollama does not require an API key
            self._api_key = ""

        # -- Ollama base URL
        if self._backend_name == "ollama":
            self._base_url: str = section.get(
                "base_url", "http://localhost:11434"
            )

    # ---------------------------------------------------------------- generate
    def generate(self, prompt: str, system_prompt: str = "") -> str:
        """
        Send *prompt* to the configured backend and return the text response.

        Parameters
        ----------
        prompt : str
            The user / main prompt.
        system_prompt : str, optional
            An optional system-level instruction prepended to the request.

        Returns
        -------
        str
            The model's text output, or ``""`` on any failure.
        """
        if self._backend_name == "openai":
            return self._generate_openai(prompt, system_prompt)
        elif self._backend_name == "gemini":
            return self._generate_gemini(prompt, system_prompt)
        elif self._backend_name == "ollama":
            return self._generate_ollama(prompt, system_prompt)
        else:
            logger.error("No generate handler for backend '%s'", self._backend_name)
            return ""

    # -------------------------------------------------------------- openai
    def _generate_openai(self, prompt: str, system_prompt: str) -> str:
        """Call OpenAI chat completions endpoint."""
        try:
            import openai  # lazy import to avoid hard dependency

            client = openai.OpenAI(api_key=self._api_key)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            response = client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            text = response.choices[0].message.content or ""
            return text.strip()

        except ImportError:
            logger.error(
                "openai package is not installed. "
                "Run: pip install openai"
            )
            return ""
        except Exception as exc:
            logger.error("OpenAI generate failed: %s", exc)
            return ""

    # -------------------------------------------------------------- gemini
    def _generate_gemini(self, prompt: str, system_prompt: str) -> str:
        """Call Google Gemini via the google-genai SDK."""
        try:
            from google import genai  # lazy import

            client = genai.Client(api_key=self._api_key)

            # Build generation config
            config_kwargs: Dict[str, Any] = {
                "temperature": self._temperature,
                "max_output_tokens": self._max_tokens,
            }
            if system_prompt:
                config_kwargs["system_instruction"] = system_prompt

            gen_config = genai.types.GenerateContentConfig(**config_kwargs)

            response = client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=gen_config,
            )
            text = response.text or ""
            return text.strip()

        except ImportError:
            logger.error(
                "google-genai package is not installed. "
                "Run: pip install google-genai"
            )
            return ""
        except Exception as exc:
            logger.error("Gemini generate failed: %s", exc)
            return ""

    # -------------------------------------------------------------- ollama
    def _generate_ollama(self, prompt: str, system_prompt: str) -> str:
        """Call a local Ollama instance via its REST API (/api/chat)."""
        url = f"{self._base_url}/api/chat"
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_tokens,
            },
        }

        try:
            resp = requests.post(
                url, 
                json=payload, 
                timeout=120,
                proxies={"http": None, "https": None}
            )
            if resp.status_code != 200:
                err_msg = resp.text
                try:
                    err_msg = resp.json().get("error", resp.text)
                except Exception:
                    pass
                logger.error(f"Ollama error (HTTP {resp.status_code}): {err_msg}")
                return ""
            data = resp.json()
            message = data.get("message", {})
            text = message.get("content", "")
            if not text.strip():
                logger.warning(f"[llm_client] Ollama returned empty content. Raw response: {data}")
            return text.strip()

        except requests.ConnectionError:
            logger.error(
                "Cannot connect to Ollama at %s — is the server running?",
                self._base_url,
            )
            return ""
        except Exception as exc:
            logger.error("Ollama generate failed: %s", exc)
            return ""

    # --------------------------------------------------------- is_available
    def is_available(self) -> bool:
        """
        Check whether the configured backend is reachable.

        Returns
        -------
        bool
            ``True`` if a lightweight connectivity check succeeds.
        """
        try:
            if self._backend_name == "openai":
                return self._check_openai()
            elif self._backend_name == "gemini":
                return self._check_gemini()
            elif self._backend_name == "ollama":
                return self._check_ollama()
            else:
                return False
        except Exception as exc:
            logger.debug("is_available check failed: %s", exc)
            return False

    def _check_openai(self) -> bool:
        """Verify OpenAI API key is present and the package is importable."""
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return bool(self._api_key)

    def _check_gemini(self) -> bool:
        """Verify Gemini API key is present and the package is importable."""
        try:
            from google import genai  # noqa: F401
        except ImportError:
            return False
        return bool(self._api_key)

    def _check_ollama(self) -> bool:
        """Ping the Ollama server's root endpoint."""
        try:
            resp = requests.get(self._base_url, timeout=5)
            return resp.status_code == 200
        except requests.ConnectionError:
            return False

    # ------------------------------------------------------------- repr
    def __repr__(self) -> str:
        return (
            f"LLMClient(backend={self._backend_name!r}, "
            f"model={self._model!r})"
        )
