"""
Aurelion Refactor Engine v7 - Authentication & Security
Middleware for the HTTP server providing:
  - API key authentication (X-API-Key header)
  - Bearer token support (Authorization: Bearer <token>)
  - Per-IP rate limiting (sliding window counter)
  - Request validation (Content-Type, body size limits)
  - Security headers on all responses

Configuration (via environment variables or config):
  AURELION_API_KEY     — master API key (required to enable auth)
  AURELION_AUTH_TOKENS — comma-separated list of valid Bearer tokens
  AURELION_RATE_LIMIT  — max requests per minute per IP (default: 60)
  AURELION_MAX_BODY    — max request body bytes (default: 1MB)

If AURELION_API_KEY is not set, auth is DISABLED (dev mode).
Auth status is shown in server startup banner.

NEW IN v7:
  - AuthMiddleware.check(request_handler) → (ok: bool, error: str)
  - RateLimiter class with sliding window algorithm
  - Token registry with expiry support
  - SecurityHeaders mixin for response hardening
  - Auth config loaded from env + .env file
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import threading
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple


# ── Auth configuration ─────────────────────────────────────────────────────────

class AuthConfig:
    """
    Loads auth configuration from environment variables.
    Supports .env file via load_dotenv (if available) or manual parsing.
    """

    def __init__(self):
        # Attempt to load .env file first
        _load_dotenv()

        self.api_key:     Optional[str] = os.environ.get("AURELION_API_KEY", "").strip() or None
        self.rate_limit:  int           = int(os.environ.get("AURELION_RATE_LIMIT", "60"))
        self.max_body:    int           = int(os.environ.get("AURELION_MAX_BODY",   str(1024 * 1024)))

        # Parse comma-separated bearer tokens
        raw_tokens = os.environ.get("AURELION_AUTH_TOKENS", "")
        self.valid_tokens: Set[str] = {
            t.strip() for t in raw_tokens.split(",") if t.strip()
        }
        if self.api_key:
            self.valid_tokens.add(self.api_key)

        # Paths that don't require authentication (public endpoints)
        self.public_paths: Set[str] = {"/status", "/health"}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key or self.valid_tokens)

    def is_valid_key(self, key: str) -> bool:
        """Constant-time comparison to prevent timing attacks."""
        if not key:
            return False
        for valid in self.valid_tokens:
            if hmac.compare_digest(key.encode(), valid.encode()):
                return True
        if self.api_key:
            return hmac.compare_digest(key.encode(), self.api_key.encode())
        return False

    def __repr__(self) -> str:
        return (
            f"AuthConfig(enabled={self.auth_enabled}, "
            f"rate_limit={self.rate_limit}/min, "
            f"tokens={len(self.valid_tokens)})"
        )


def _load_dotenv(path: str = ".env") -> None:
    """
    Minimal .env loader — reads KEY=VALUE lines into os.environ.
    Handles quoted values, comments, and empty lines.
    Does not override already-set variables.
    """
    env_path = os.path.join(os.getcwd(), path)
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key   = key.strip()
                value = value.strip()

                # Strip surrounding quotes
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]

                # Don't override existing env vars
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


# ── Rate limiter ───────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Per-IP sliding window rate limiter.
    Tracks request timestamps in a deque; drops entries older than 60 seconds.
    Thread-safe.
    """

    WINDOW_SECONDS = 60

    def __init__(self, max_per_minute: int = 60):
        self._max     = max_per_minute
        self._windows: Dict[str, deque] = defaultdict(deque)
        self._lock    = threading.Lock()

    def is_allowed(self, ip: str) -> Tuple[bool, int]:
        """
        Check whether an IP is within its rate limit.
        Returns (allowed: bool, remaining: int).
        """
        now     = time.monotonic()
        cutoff  = now - self.WINDOW_SECONDS

        with self._lock:
            window = self._windows[ip]

            # Remove expired timestamps
            while window and window[0] < cutoff:
                window.popleft()

            count     = len(window)
            remaining = max(0, self._max - count)

            if count >= self._max:
                return False, 0

            window.append(now)
            return True, remaining - 1

    def reset(self, ip: str) -> None:
        """Clear rate limit state for an IP (admin use)."""
        with self._lock:
            self._windows.pop(ip, None)

    def stats(self) -> Dict[str, int]:
        """Return per-IP request counts within current window."""
        now    = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS
        with self._lock:
            return {
                ip: sum(1 for t in w if t > cutoff)
                for ip, w in self._windows.items()
                if any(t > cutoff for t in w)
            }


# ── Auth middleware ────────────────────────────────────────────────────────────

class AuthMiddleware:
    """
    Stateless authentication middleware.
    Instantiate once per server; call check() for each request.
    """

    def __init__(self, config: Optional[AuthConfig] = None):
        self._config  = config or AuthConfig()
        self._limiter = RateLimiter(self._config.rate_limit)

    @property
    def config(self) -> AuthConfig:
        return self._config

    def check(self, handler) -> Tuple[bool, int, str]:
        """
        Validate an incoming request.

        Args:
            handler: BaseHTTPRequestHandler instance.

        Returns:
            (ok: bool, http_code: int, error_message: str)
            ok=True means the request is allowed to proceed.
        """
        parsed_path = handler.path.split("?")[0].rstrip("/")
        client_ip   = handler.client_address[0]

        # ── 1. Rate limit check (always applied) ──────────────────
        allowed, remaining = self._limiter.is_allowed(client_ip)
        if not allowed:
            return False, 429, (
                f"Rate limit exceeded ({self._config.rate_limit} req/min). "
                f"Try again in 60 seconds."
            )

        # ── 2. Skip auth for public endpoints ─────────────────────
        if parsed_path in self._config.public_paths:
            return True, 200, ""

        # ── 3. Auth check ─────────────────────────────────────────
        if not self._config.auth_enabled:
            return True, 200, ""   # Auth disabled — dev mode

        # Extract token from headers
        token = self._extract_token(handler)

        if not token:
            return False, 401, (
                "Authentication required. "
                "Provide X-API-Key header or Authorization: Bearer <token>"
            )

        if not self._config.is_valid_key(token):
            return False, 403, "Invalid API key or token."

        return True, 200, ""

    def security_headers(self) -> Dict[str, str]:
        """Return security headers to add to every response."""
        return {
            "X-Content-Type-Options":   "nosniff",
            "X-Frame-Options":          "DENY",
            "X-XSS-Protection":         "1; mode=block",
            "Referrer-Policy":          "no-referrer",
            "Cache-Control":            "no-store",
            "X-Aurelion-Auth":          "enabled" if self._config.auth_enabled else "disabled",
        }

    def validate_request_body(self, handler) -> Tuple[bool, str]:
        """
        Validate Content-Type and body size for POST requests.
        Returns (ok, error_message).
        """
        if handler.command not in ("POST", "PUT", "PATCH"):
            return True, ""

        content_type = handler.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            return False, f"Content-Type must be application/json, got: {content_type!r}"

        length = int(handler.headers.get("Content-Length", 0))
        if length > self._config.max_body:
            return False, (
                f"Request body too large: {length} bytes "
                f"(max: {self._config.max_body})"
            )

        return True, ""

    @staticmethod
    def _extract_token(handler) -> Optional[str]:
        """
        Extract auth token from request headers.
        Checks X-API-Key first, then Authorization: Bearer.
        """
        # X-API-Key header (recommended for server-to-server)
        api_key = handler.headers.get("X-API-Key", "").strip()
        if api_key:
            return api_key

        # Authorization: Bearer <token>
        auth = handler.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()

        return None


# ── Auth token generator (CLI utility) ────────────────────────────────────────

def generate_api_key(prefix: str = "aur") -> str:
    """
    Generate a cryptographically secure API key.
    Format: aur_<32 hex chars>
    """
    import secrets
    token = secrets.token_hex(16)
    return f"{prefix}_{token}"
