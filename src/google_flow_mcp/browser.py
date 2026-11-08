"""The one real Chrome tab every Flow call runs through.

Flow's generation endpoints only accept a reCAPTCHA Enterprise token minted by
the page itself, and Google's anti-bot stack rejects Chrome's ``--headless``
mode. So the server keeps one headed Chrome (under Xvfb inside the container),
signed in with the user's exported cookies, and executes every RPC inside that
tab with ``page.evaluate``. Calls are serialised through one lock: a single tab
is the honest concurrency limit, and pacing keeps the account unremarkable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import protocol as p
from .config import Settings
from .cookies import load_cookies, summarise

log = logging.getLogger("google_flow_mcp.browser")


class BrowserError(RuntimeError):
    """The Flow tab could not do what was asked."""


class NotSignedInError(BrowserError):
    """Flow sent us to the Google sign-in page: the cookies are missing or stale."""


class FlowLandingError(NotSignedInError):
    """Flow parked the tab on its public ``/about`` page instead of opening the app."""


#: Answers with the accounts the cookie jar is signed in to, without any UI.
_LIST_ACCOUNTS_URL = ("https://accounts.google.com/ListAccounts"
                      "?gpsia=1&source=ChromiumBrowser&json=standard")

_ABOUT_HELP = (
    "Flow sent the browser to its public page (flow.google.com/about) instead of opening the "
    "app. The Google cookies were loaded, but Flow did not accept this session. Known causes: "
    "Google wants this account to 'Confirm it's you' again, the exported session was signed "
    "out or rotated on the computer it came from, or Google does not trust the session from "
    "this server's IP address. Run flow_status for a diagnosis.")


_LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=Translate,MediaRouter",
    "--window-size=1366,900",
]

_STEALTH_JS = """
Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true });
"""

_STATE_JS = """
() => {
    const w = globalThis.WIZ_global_data || {};
    const m = location.pathname.match(/\\/project\\/([0-9a-fA-F-]{36})/);
    let email = null;
    for (const k of ['oPEP7c', 'qDCSke']) {
        if (typeof w[k] === 'string' && w[k].includes('@')) { email = w[k]; break; }
    }
    return {
        url: location.href,
        host: location.host,
        at: !!w.SNlM0e,
        grecaptcha: !!(window.grecaptcha && window.grecaptcha.enterprise
                       && typeof window.grecaptcha.enterprise.execute === 'function'),
        project: m ? m[1] : null,
        email,
        lang: document.documentElement.lang || null,
    };
}
"""

#: Mint (when needed) and send in one evaluate, so the single-use token is
#: spent within milliseconds of being minted.
#:
#: The current Flow bundle wraps ``grecaptcha.enterprise.execute`` with an
#: ``Object.assign`` that forces ``action: "extension_hijack_detected"``; a token
#: minted through the public API comes out poisoned. The assign is neutralised
#: only for the duration of the call (technique from alarconcesar/flow-mcp).
_RPC_JS = """
async ([rpcid, freq, captchaAction, slot, match, maxChars]) => {
    const wiz = globalThis.WIZ_global_data || {};
    const at = wiz.SNlM0e;
    if (!at) return { error: 'NO_AT_TOKEN' };
    if (captchaAction) {
        const g = window.grecaptcha && window.grecaptcha.enterprise;
        if (!g || typeof g.execute !== 'function') return { error: 'NO_GRECAPTCHA' };
        let siteKey = null;
        for (const s of document.querySelectorAll('script[src*="recaptcha/enterprise.js"]')) {
            const m = (s.getAttribute('src') || '').match(/[?&]render=([^&]+)/);
            if (m) { siteKey = m[1]; break; }
        }
        if (!siteKey) siteKey = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV';
        const realAssign = Object.assign;
        Object.assign = function (target, ...sources) {
            const out = realAssign(target, ...sources);
            try {
                if (out && typeof out === 'object' && out.action === 'extension_hijack_detected') {
                    out.action = captchaAction;
                }
            } catch (e) {}
            return out;
        };
        let token;
        try {
            token = await new Promise((resolve, reject) => {
                const run = () => g.execute(siteKey, { action: captchaAction }).then(resolve, reject);
                if (typeof g.ready === 'function') g.ready(run); else run();
            });
        } catch (e) {
            return { error: 'CAPTCHA_FAILED: ' + (e && e.message ? e.message : e) };
        } finally {
            Object.assign = realAssign;
        }
        if (!token) return { error: 'CAPTCHA_EMPTY' };
        freq = freq.split(slot).join(token);
    }
    const hl = (document.documentElement.lang || navigator.language || 'en').split('-')[0];
    const url = '/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=' + encodeURIComponent(rpcid)
        + '&source-path=' + encodeURIComponent(location.pathname || '/')
        + '&bl=' + encodeURIComponent(wiz.cfb2h || '')
        + '&f.sid=' + encodeURIComponent(wiz.FdrFJe || '')
        + '&hl=' + encodeURIComponent(hl)
        + '&_reqid=' + (Math.floor(Math.random() * 900000) + 100000) + '&rt=c';
    let resp, text;
    try {
        resp = await fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers: { 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8', 'x-same-domain': '1' },
            body: new URLSearchParams({ 'f.req': freq, at }),
        });
        text = await resp.text();
    } catch (e) {
        return { error: 'FETCH_FAILED: ' + (e && e.message ? e.message : e) };
    }
    if (match) {
        const i = text.toLowerCase().indexOf(match.toLowerCase());
        return { status: resp.status, found: i >= 0,
                 text: i >= 0 ? text.slice(Math.max(0, i - 400), i + 1200) : '' };
    }
    return { status: resp.status, length: text.length,
             text: text.length > maxChars ? text.slice(0, maxChars) : text };
}
"""

_FETCH_BYTES_JS = """
async ([url, maxBytes]) => {
    try {
        const r = await fetch(url, { credentials: 'include' });
        if (!r.ok) return { error: 'HTTP ' + r.status };
        const buf = await r.arrayBuffer();
        if (buf.byteLength > maxBytes) return { error: 'TOO_LARGE', size: buf.byteLength };
        let bin = '';
        const bytes = new Uint8Array(buf);
        for (let i = 0; i < bytes.length; i += 0x8000) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }
        return { b64: btoa(bin), type: r.headers.get('content-type'), size: buf.byteLength };
    } catch (e) {
        return { error: 'FETCH_FAILED: ' + (e && e.message ? e.message : e) };
    }
}
"""


@dataclass(slots=True)
class PageState:
    url: str
    signed_in: bool
    at: bool
    grecaptcha: bool
    project: Optional[str]
    email: Optional[str]
    landing: Optional[str] = None


def landing_kind(url: str) -> Optional[str]:
    """``"signin"`` for Google's auth pages, ``"public"`` for Flow's /about page."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if host == "accounts.google.com" or path.endswith("/signin"):
        return "signin"
    if host == "flow.google.com" and path == "/about":
        return "public"
    return None


def find_chrome(explicit: Optional[str]) -> Optional[str]:
    """Real Google Chrome first: Flow's reCAPTCHA scores Chromium builds worse."""
    candidates = [explicit] if explicit else []
    candidates += [
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("google-chrome") or shutil.which("chrome")


class FlowBrowser:
    """Owns Playwright, the Chrome context and the Flow tab."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._last_billed_submit = 0.0
        self._last_used = time.monotonic()
        self.cookie_summary: Optional[dict] = None
        self.started_at: Optional[float] = None
        self.chrome_executable: Optional[str] = None

    # ── lifecycle ───────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def start(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            await self._close_quietly()
            from playwright.async_api import async_playwright

            s = self.settings
            cookies = load_cookies(s.cookies_file, s.cookies)
            if not cookies and not s.profile_dir:
                raise NotSignedInError(
                    "No Google session configured. Set FLOW_COOKIES_FILE (or FLOW_COOKIES) to a "
                    "cookie export of a browser signed in to flow.google.com, or FLOW_PROFILE_DIR "
                    "to a signed-in Chrome profile. `google-flow-mcp login` creates the export.")
            self.cookie_summary = summarise(cookies) if cookies else None

            self._pw = await async_playwright().start()
            exe = find_chrome(s.chrome_path)
            self.chrome_executable = exe or "playwright-chromium"
            launch: dict[str, Any] = {
                "headless": s.headless,
                # Kubernetes pods (Obot: non-root, all capabilities dropped,
                # RuntimeDefault seccomp) cannot create Chrome's user-namespace
                # sandbox, so Linux disables it unless explicitly asked not to.
                "args": list(_LAUNCH_ARGS) + (
                    ["--no-sandbox"] if os.name == "posix" and os.uname().sysname == "Linux"
                    and os.environ.get("FLOW_CHROME_SANDBOX") != "1" else []),
                "ignore_default_args": ["--enable-automation"],
            }
            if exe:
                launch["executable_path"] = exe
            if s.proxy:
                launch["proxy"] = {"server": s.proxy}
            ctx_opts: dict[str, Any] = {
                "locale": s.locale,
                "viewport": {"width": 1366, "height": 800},
                "accept_downloads": True,
            }
            if s.profile_dir:
                os.makedirs(s.profile_dir, exist_ok=True)
                self._context = await self._pw.chromium.launch_persistent_context(
                    s.profile_dir, **launch, **ctx_opts)
            else:
                self._browser = await self._pw.chromium.launch(**launch)
                self._context = await self._browser.new_context(**ctx_opts)
            await self._context.add_init_script(_STEALTH_JS)
            if cookies:
                await self._context.add_cookies(cookies)
            self._context.set_default_timeout(s.navigation_timeout_ms)
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            self.started_at = time.time()
            log.info("browser started (%s, headless=%s)", self.chrome_executable, s.headless)

    async def _close_quietly(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = self._page = None

    async def close(self) -> None:
        async with self._start_lock:
            await self._close_quietly()

    async def restart(self) -> None:
        await self.close()
        await self.start()

    # ── page state ──────────────────────────────────────────────────────
    async def _state(self) -> PageState:
        page = self._page
        try:
            raw = await page.evaluate(_STATE_JS)
        except Exception:  # navigating
            raw = {"url": page.url, "at": False, "grecaptcha": False, "project": None, "email": None,
                   "host": ""}
        url = raw.get("url") or page.url or ""
        landing = landing_kind(url)
        return PageState(url, landing is None, bool(raw.get("at")), bool(raw.get("grecaptcha")),
                         raw.get("project"), raw.get("email"), landing)

    async def _goto(self, url: str) -> None:
        try:
            await self._page.goto(url, wait_until="domcontentloaded",
                                  timeout=self.settings.navigation_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - the page is often usable anyway
            log.warning("navigation to %s: %s", url, exc)

    async def _wait_ready(self, need_captcha: bool, timeout_s: float = 60,
                          settle: bool = False) -> PageState:
        deadline = time.monotonic() + timeout_s
        state = await self._state()
        while time.monotonic() < deadline:
            state = await self._state()
            if state.landing == "public":
                raise FlowLandingError(_ABOUT_HELP)
            if not state.signed_in:
                raise NotSignedInError(
                    "Flow redirected to Google sign-in: the session cookies are missing or "
                    "expired. Export fresh cookies and update FLOW_COOKIES (or FLOW_COOKIES_FILE).")
            if state.at and (state.grecaptcha or not need_captcha):
                if settle:
                    # Flow decides client-side, a few hundred ms after load, whether to
                    # hop to /about; a page that looked ready may still leave.
                    settle = False
                    await asyncio.sleep(1.5)
                    continue
                return state
            if state.url.startswith("chrome-error://"):
                raise BrowserError(
                    "Chrome could not reach flow.google.com. Check the network/egress policy: the "
                    "pod needs flow.google.com, *.google.com, *.gstatic.com, *.googleapis.com, "
                    "flow-content.google and www.recaptcha.net.")
            await asyncio.sleep(1.0)
        raise BrowserError(
            f"Flow page never became ready (at={state.at}, recaptcha={state.grecaptcha}, url={state.url})")

    async def ensure(self, project_id: Optional[str] = None, need_captcha: bool = False,
                     timeout_s: float = 60) -> PageState:
        """A ready Flow tab: on the project page when a project is given (the
        only view that loads reCAPTCHA and the right ``source-path``)."""
        await self.start()
        state = await self._state()
        navigated = False
        if project_id:
            if state.project != project_id or not state.at or (need_captcha and not state.grecaptcha):
                await self._goto(p.PROJECT_URL.format(pid=project_id))
                navigated = True
        elif "flow.google.com" not in state.url or not state.at or state.landing:
            await self._goto(p.FLOW_HOME)
            navigated = True
        return await self._wait_ready(need_captcha=need_captcha, timeout_s=timeout_s,
                                      settle=navigated)

    async def status(self, timeout_s: float = 25) -> PageState:
        async with self._lock:
            return await self.ensure(timeout_s=timeout_s)

    async def google_accounts(self) -> Optional[list[str]]:
        """Accounts Google itself considers signed in for this cookie jar (no UI).

        ``[]`` means the cookies no longer sign in to Google at all; ``None``
        means the check could not run."""
        await self.start()
        try:
            resp = await self._context.request.get(_LIST_ACCOUNTS_URL, timeout=15_000)
            text = await resp.text()
        except Exception as exc:  # noqa: BLE001
            log.warning("ListAccounts failed: %s", exc)
            return None
        if resp.status != 200:
            return None
        return sorted(set(re.findall(r'"([^"@\s]+@[^"\s]+\.[^"\s]+)"', text)))

    async def diagnose_landing(self) -> dict[str, Any]:
        """Why Flow shows /about: ask Google who is signed in, then press Flow's own
        main button once and report where it leads (nothing is created or spent)."""
        async with self._lock:
            out: dict[str, Any] = {"google_accounts": await self.google_accounts()}
            page = self._page
            try:
                await self._goto(p.FLOW_HOME)
                await asyncio.sleep(2.5)
                if landing_kind(page.url) != "public":
                    out["landing_after_reload"] = page.url
                    return out
                button = page.get_by_role("button", name=re.compile("Create with", re.I))
                if not await button.count():
                    button = page.get_by_role("link", name=re.compile("Create with", re.I))
                await button.first.click(timeout=8_000)
                for _ in range(16):
                    await asyncio.sleep(0.5)
                    if landing_kind(page.url) != "public":
                        break
                await asyncio.sleep(1.5)
                out["button_leads_to"] = page.url
                try:
                    body = (await page.inner_text("body", timeout=3_000))[:600]
                except Exception:  # noqa: BLE001
                    body = ""
                out["page_text"] = " ".join(body.split())[:300]
            except Exception as exc:  # noqa: BLE001
                out["click_error"] = str(exc)[:200]
            return out

    # ── RPC ─────────────────────────────────────────────────────────────
    async def rpc(self, rpcid: str, freq: str, *, project_id: Optional[str] = None,
                  captcha_action: Optional[str] = None, match: Optional[str] = None,
                  max_chars: int = 6_000_000, retries: int = 1, timeout_s: float = 150) -> str:
        """Run one batchexecute call in the tab and return the raw reply text."""
        need_captcha = p.needs_captcha(freq)
        if need_captcha and not captcha_action:
            captcha_action = p.CAPTCHA_IMAGE
        if not need_captcha:
            captcha_action = None
        async with self._lock:
            self._last_used = time.monotonic()
            for attempt in range(retries + 1):
                await self.ensure(project_id, need_captcha=bool(captcha_action))
                if rpcid in p.BILLED_RPCS:
                    gap = self.settings.min_submit_interval_s - (time.monotonic() - self._last_billed_submit)
                    if gap > 0:
                        await asyncio.sleep(gap)
                try:
                    out = await asyncio.wait_for(self._page.evaluate(
                        _RPC_JS, [rpcid, freq, captcha_action, p.CAPTCHA_SLOT, match, max_chars]),
                        timeout=timeout_s)
                except asyncio.TimeoutError:
                    # A stuck tab would block every later call; start clean next time.
                    await self._goto(p.FLOW_HOME)
                    raise BrowserError(f"{rpcid}: no reply within {timeout_s:.0f}s") from None
                if rpcid in p.BILLED_RPCS:
                    self._last_billed_submit = time.monotonic()
                if not isinstance(out, dict):
                    raise BrowserError(f"unexpected page result for {rpcid}: {out!r}")
                err = out.get("error")
                if err in ("NO_AT_TOKEN", "NO_GRECAPTCHA") and attempt < retries:
                    await self._goto(p.PROJECT_URL.format(pid=project_id) if project_id else p.FLOW_HOME)
                    continue
                if err:
                    raise BrowserError(f"{rpcid}: {err}")
                status = int(out.get("status") or 0)
                text = out.get("text") or ""
                if status == 200:
                    return text
                if status in (401, 403) and "UNUSUAL_ACTIVITY" not in text and attempt < retries:
                    await self._goto(p.FLOW_HOME)
                    continue
                hint = ""
                if "UNUSUAL_ACTIVITY" in text:
                    hint = (" (Flow flagged the request as unusual activity: the reCAPTCHA score was "
                            "too low. Slow down, keep the browser headed, and retry later.)")
                raise BrowserError(f"{rpcid} returned HTTP {status}{hint}: {text[:300]}")
        raise BrowserError(f"{rpcid}: retries exhausted")

    async def rpc_payload(self, rpcid: str, freq: str, **kwargs: Any) -> Any:
        return p.first_payload(await self.rpc(rpcid, freq, **kwargs), rpcid)

    # ── media ───────────────────────────────────────────────────────────
    async def fetch_bytes(self, url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
        """Fetch a signed media URL. Plain HTTP first; through the tab (with the
        session cookies) when the CDN insists."""
        import base64

        import httpx

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code == 200:
                        chunks, size = [], 0
                        async for chunk in r.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise BrowserError(f"media is larger than {max_bytes} bytes")
                            chunks.append(chunk)
                        return b"".join(chunks), r.headers.get("content-type")
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("direct media fetch failed, trying the tab: %s", exc)
        async with self._lock:
            await self.ensure()
            out = await asyncio.wait_for(self._page.evaluate(_FETCH_BYTES_JS, [url, max_bytes]), 180)
        if not isinstance(out, dict) or out.get("error"):
            raise BrowserError(f"media download failed: {out.get('error') if isinstance(out, dict) else out}")
        return base64.b64decode(out["b64"]), out.get("type")

    async def screenshot(self) -> bytes:
        async with self._lock:
            await self.start()
            return await self._page.screenshot(type="png")

    async def export_storage_state(self) -> dict:
        """Current cookies (rotated by Google while the tab lives)."""
        await self.start()
        return await self._context.storage_state()
