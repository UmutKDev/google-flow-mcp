"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    # transport
    transport: str = "streamable-http"
    host: str = "0.0.0.0"
    port: int = 8080
    path: str = "/mcp"
    #: Optional shared secret. When set, every HTTP request must carry
    #: ``Authorization: Bearer <token>``. Leave unset behind Obot, which already
    #: authenticates callers.
    auth_token: Optional[str] = None

    # authentication into Flow
    #: Path to an exported cookie file (Playwright storage_state, Cookie-Editor
    #: JSON, or Netscape cookies.txt). In Obot this is a ``file: true`` field.
    cookies_file: Optional[str] = None
    #: The same content inline (raw JSON / cookies.txt / base64 of either).
    cookies: Optional[str] = None
    #: Optional persistent Chrome profile (Docker volume). Skipped when unset.
    profile_dir: Optional[str] = None
    expected_account: Optional[str] = None

    # browser
    chrome_path: Optional[str] = None
    headless: bool = False
    browser_idle_timeout_s: int = 0
    navigation_timeout_ms: int = 60_000
    locale: str = "en-US"
    proxy: Optional[str] = None

    # Flow
    project_id: Optional[str] = None
    default_image_model: str = "NANO_BANANA_PRO"
    #: Seconds to wait between two billed submits (pacing keeps the account
    #: unremarkable).
    min_submit_interval_s: float = 4.0

    # spending guard
    #: Hard ceiling of credits this process may spend. 0 disables video.
    budget_credits: int = 200
    #: Billed calls must pass confirm=true when their estimate is at or above this.
    confirm_threshold: int = 1

    # jobs and media
    job_timeout_s: int = 900
    #: A tool call waits at most this long before answering with a job id.
    sync_wait_s: float = 40.0
    output_dir: Path = field(default_factory=lambda: Path("/tmp/google-flow-mcp"))
    #: Extra server directories flow_upload_image may read local files from.
    input_dirs: list[str] = field(default_factory=list)
    inline_image_max_bytes: int = 3_500_000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.transport = (_env("FLOW_MCP_TRANSPORT", s.transport) or s.transport).lower()
        s.host = _env("FLOW_MCP_HOST", s.host) or s.host
        s.port = _env_int("PORT", _env_int("FLOW_MCP_PORT", s.port))
        s.path = _env("FLOW_MCP_PATH", s.path) or s.path
        s.auth_token = _env("FLOW_MCP_AUTH_TOKEN")
        s.cookies_file = _env("FLOW_COOKIES_FILE")
        s.cookies = _env("FLOW_COOKIES")
        s.profile_dir = _env("FLOW_PROFILE_DIR")
        s.expected_account = _env("FLOW_EXPECTED_ACCOUNT")
        s.chrome_path = _env("FLOW_CHROME_PATH")
        s.headless = _env_bool("FLOW_HEADLESS", s.headless)
        s.browser_idle_timeout_s = _env_int("FLOW_BROWSER_IDLE_TIMEOUT", s.browser_idle_timeout_s)
        s.navigation_timeout_ms = _env_int("FLOW_NAVIGATION_TIMEOUT_MS", s.navigation_timeout_ms)
        s.locale = _env("FLOW_LOCALE", s.locale) or s.locale
        s.proxy = _env("FLOW_BROWSER_PROXY")
        s.project_id = _env("FLOW_PROJECT_ID")
        s.default_image_model = _env("FLOW_DEFAULT_IMAGE_MODEL", s.default_image_model) or s.default_image_model
        s.min_submit_interval_s = _env_float("FLOW_MIN_SUBMIT_INTERVAL", s.min_submit_interval_s)
        s.budget_credits = _env_int("FLOW_BUDGET_CREDITS", s.budget_credits)
        s.confirm_threshold = _env_int("FLOW_CONFIRM_THRESHOLD", s.confirm_threshold)
        s.job_timeout_s = _env_int("FLOW_JOB_TIMEOUT", s.job_timeout_s)
        s.sync_wait_s = _env_float("FLOW_SYNC_WAIT", s.sync_wait_s)
        s.output_dir = Path(_env("FLOW_OUTPUT_DIR", str(s.output_dir)) or s.output_dir)
        s.input_dirs = [d for d in (_env("FLOW_INPUT_DIRS", "") or "").split(os.pathsep) if d]
        s.inline_image_max_bytes = _env_int("FLOW_INLINE_IMAGE_MAX_BYTES", s.inline_image_max_bytes)
        s.log_level = (_env("FLOW_LOG_LEVEL", s.log_level) or s.log_level).upper()
        return s
