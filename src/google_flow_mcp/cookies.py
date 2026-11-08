"""Load Google session cookies from the formats people actually export.

Supported inputs (auto-detected):

* Playwright ``storage_state`` JSON (``{"cookies": [...], "origins": [...]}``),
  which is what ``google-flow-mcp login`` writes.
* A JSON array of cookies, as exported by Cookie-Editor / EditThisCookie.
* Netscape ``cookies.txt``.
* Base64 of any of the above (handy for an env var that dislikes newlines).

Only cookies for Google domains are kept, so a full-browser export does not
leak unrelated sessions into the container.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any, Optional

GOOGLE_DOMAIN_SUFFIXES = (
    "google.com", "flow.google.com", "labs.google", "flow-content.google",
    "googleapis.com", "googleusercontent.com", "youtube.com", "gstatic.com",
)

#: Without at least one of these the session cannot be signed in.
REQUIRED_ANY = ("__Secure-1PSID", "__Secure-3PSID", "SID")

_SAMESITE = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None",
             "unspecified": "Lax"}


class CookieError(ValueError):
    pass


def _is_google(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return any(d == s or d.endswith("." + s) for s in GOOGLE_DOMAIN_SUFFIXES)


def _normalise(raw: dict) -> Optional[dict]:
    name = raw.get("name")
    value = raw.get("value")
    domain = raw.get("domain") or raw.get("host")
    if not name or value is None or not domain or not _is_google(str(domain)):
        return None
    cookie: dict[str, Any] = {
        "name": str(name),
        "value": str(value),
        "domain": str(domain),
        "path": str(raw.get("path") or "/"),
        "secure": bool(raw.get("secure", True)),
        "httpOnly": bool(raw.get("httpOnly", raw.get("httponly", False))),
    }
    expires = raw.get("expires") or raw.get("expirationDate")
    if isinstance(expires, (int, float)) and expires > 0:
        cookie["expires"] = float(expires)
    same_site = raw.get("sameSite")
    if isinstance(same_site, str) and same_site.lower() in _SAMESITE:
        cookie["sameSite"] = _SAMESITE[same_site.lower()]
    if cookie.get("sameSite") == "None":
        cookie["secure"] = True
    if cookie["name"].startswith("__Host-"):
        cookie["path"] = "/"
        cookie["domain"] = cookie["domain"].lstrip(".")
    return cookie


def _parse_netscape(text: str) -> list[dict]:
    cookies = []
    for line in text.splitlines():
        http_only = False
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
            http_only = True
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        cookies.append({
            "domain": domain, "path": path, "secure": secure.upper() == "TRUE",
            "expires": float(expires) if expires.strip().isdigit() else None,
            "name": name, "value": value.rstrip("\r\n"), "httpOnly": http_only,
        })
    return cookies


def _maybe_b64(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(("{", "[", "#")) or "\t" in stripped:
        return text
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return text
    return decoded


def parse_cookies(text: str) -> list[dict]:
    """Parse any supported format into Playwright cookie dicts."""
    text = _maybe_b64(text)
    stripped = text.strip()
    raw: list[dict]
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CookieError(f"cookie JSON is not valid: {exc}") from None
        if isinstance(data, dict):
            data = data.get("cookies", [])
        if not isinstance(data, list):
            raise CookieError("cookie JSON must be a list or a storage_state object")
        raw = [c for c in data if isinstance(c, dict)]
    else:
        raw = _parse_netscape(text)
    cookies = [c for c in (_normalise(r) for r in raw) if c]
    if not cookies:
        raise CookieError("no Google cookies found in the provided export")
    names = {c["name"] for c in cookies}
    if not any(n in names for n in REQUIRED_ANY):
        raise CookieError(
            "the export has Google cookies but no session cookie "
            f"({', '.join(REQUIRED_ANY)}); export while signed in to flow.google.com")
    return cookies


def load_cookies(path: Optional[str], inline: Optional[str]) -> Optional[list[dict]]:
    """Cookies from a file path or an inline value; ``None`` when neither is set."""
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise CookieError(f"FLOW_COOKIES_FILE points to a missing file: {p}")
        return parse_cookies(p.read_text(encoding="utf-8"))
    if inline:
        return parse_cookies(inline)
    return None


def summarise(cookies: list[dict]) -> dict:
    """What a status tool may show: names and expiry, never values."""
    import time

    now = time.time()
    expiries = [c["expires"] for c in cookies if c.get("name") in REQUIRED_ANY and c.get("expires")]
    soonest = min(expiries) if expiries else None
    return {
        "count": len(cookies),
        "domains": sorted({c["domain"].lstrip(".") for c in cookies})[:12],
        "names": sorted({c["name"] for c in cookies})[:60],
        "session_cookie_expires_in_days": round((soonest - now) / 86400, 1) if soonest else None,
    }
