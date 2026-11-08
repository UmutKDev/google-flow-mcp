"""Command line: ``google-flow-mcp [serve|login|check-cookies]``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import Settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _login(out: Path, profile: Path, timeout_s: int) -> int:
    """Open a real Chrome, let the user sign in, save the Google cookies."""
    from playwright.async_api import async_playwright

    from .browser import _LAUNCH_ARGS, find_chrome
    from .cookies import parse_cookies, summarise

    exe = find_chrome(None)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile), headless=False, executable_path=exe, args=_LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"], viewport=None)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://flow.google.com/")
        print("A Chrome window is open. Sign in to Google and open Flow (flow.google.com).",
              file=sys.stderr)
        deadline = time.monotonic() + timeout_s
        ready = False
        while time.monotonic() < deadline:
            try:
                ready = await page.evaluate(
                    "() => location.host === 'flow.google.com' && "
                    "!!(globalThis.WIZ_global_data || {}).SNlM0e")
            except Exception:  # noqa: BLE001 - navigating
                ready = False
            if ready:
                break
            await asyncio.sleep(2)
        if not ready:
            print("Timed out waiting for a signed-in Flow page.", file=sys.stderr)
            await ctx.close()
            return 1
        await asyncio.sleep(3)  # let Flow settle its cookies
        state = await ctx.storage_state()
        await ctx.close()
    cookies = parse_cookies(json.dumps(state))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=1))
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({"saved": str(out), **summarise(cookies)}, indent=2))
    print("\nUpload this file as the 'Google cookies' field in Obot (or set FLOW_COOKIES_FILE). "
          "Treat it like a password: it signs in as you.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="google-flow-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=["streamable-http", "stdio"])
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    login = sub.add_parser("login", help="sign in with a local Chrome and export cookies")
    login.add_argument("--out", type=Path, default=Path.home() / ".google-flow-mcp" / "cookies.json")
    login.add_argument("--profile", type=Path, default=Path.home() / ".google-flow-mcp" / "login-profile")
    login.add_argument("--timeout", type=int, default=600)

    check = sub.add_parser("check-cookies", help="validate a cookie export without starting Chrome")
    check.add_argument("path", type=Path)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    _setup_logging(settings.log_level)

    if args.cmd == "login":
        return asyncio.run(_login(args.out, args.profile, args.timeout))
    if args.cmd == "check-cookies":
        from .cookies import CookieError, parse_cookies, summarise

        try:
            print(json.dumps(summarise(parse_cookies(args.path.read_text())), indent=2))
            return 0
        except CookieError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 1

    if getattr(args, "transport", None):
        settings.transport = args.transport
    if getattr(args, "host", None):
        settings.host = args.host
    if getattr(args, "port", None):
        settings.port = args.port
    from .server import serve_http, serve_stdio

    runner = serve_stdio if settings.transport == "stdio" else serve_http
    asyncio.run(runner(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
