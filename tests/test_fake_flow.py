"""End-to-end against a fake flow.google.com served through Playwright routing.

This drives the real browser code (in-page reCAPTCHA mint with the poison
bypass, batchexecute fetch, envelope parsing, polling, media download) and the
MCP tools, without a Google account.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse

import pytest

from google_flow_mcp import protocol as p
from google_flow_mcp.config import Settings
from google_flow_mcp.server import build_server

PID = "11111111-2222-3333-4444-555555555555"
IMG = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
UPL = "aaaaaaaa-bbbb-cccc-dddd-000000000002"
VID = "aaaaaaaa-bbbb-cccc-dddd-000000000003"
OP = "bbbbbbbb-0000-0000-0000-000000000009"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

PAGE = """<!doctype html><html lang="en"><head>
<script>window.WIZ_global_data = {SNlM0e: 'AT-TOKEN', FdrFJe: 'sid', cfb2h: 'bl', oPEP7c: 'umut@example.com'};</script>
<script src="https://www.google.com/recaptcha/enterprise.js?render=SITEKEY"></script>
</head><body>fake flow</body></html>"""

# Mimics the real bundle: execute() is wrapped so that options are passed
# through Object.assign, which forces the poisoned action.
RECAPTCHA_JS = """
window.grecaptcha = { enterprise: {
  ready: (cb) => cb(),
  execute: (key, opts) => {
    const o = Object.assign({}, opts, { action: 'extension_hijack_detected' });
    return Promise.resolve('tok:' + key + ':' + o.action);
  },
}};
"""


def wrb(rpcid: str, payload) -> str:
    return ")]}'\n\n100\n" + json.dumps([["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]])


class FakeFlow:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []
        self.polls = 0

    def reply(self, rpcid: str, inner: list, token: str | None) -> str:
        cdn = f"https://{p.MEDIA_HOST}"
        if rpcid == p.RPC_CREATE_PROJECT:
            return wrb(rpcid, [PID, [inner[1][1][0]]])
        if rpcid == p.RPC_GEN_IMAGE:
            items = inner[1]
            return wrb(rpcid, [[None, [f"{cdn}/image/{IMG[:-1]}{i}?sig=x" for i in range(len(items))]]])
        if rpcid == p.RPC_UPLOAD_IMAGE:
            return wrb(rpcid, [[UPL, PID, "op", "CAE"]])
        if rpcid == p.RPC_GEN_VIDEO_TEXT:
            return wrb(rpcid, [None, None, None, [[VID, PID, "wf-1", "PENDING"]]])
        if rpcid == p.RPC_GEN_VIDEO_FRAME:
            return wrb(rpcid, [None, 50, [[OP, PID, "scene", "PENDING"]]])
        if rpcid == p.RPC_OPERATION:
            self.polls += 1
            return wrb(rpcid, [None, 50, [[OP, PID, "scene", "CAE" if self.polls >= 2 else "PENDING"]]])
        if rpcid == p.RPC_PROJECT_MEDIA:
            listing = [[["asset", "f", None, ["clip", [1, 0], True, None, VID, OP, True], PID]]]
            return wrb(rpcid, listing)
        if rpcid == p.RPC_MEDIA:
            mid = inner[0]
            return wrb(rpcid, [[f"{cdn}/video/{mid}?sig=v", f"{cdn}/image/{mid}?sig=t"]])
        if rpcid == p.RPC_UPSCALE_IMAGE:
            return wrb(rpcid, [None, base64.b64encode(PNG * 20).decode()])
        return wrb(rpcid, None)

    async def handle(self, route, request):
        url = request.url
        if "recaptcha/enterprise.js" in url:
            return await route.fulfill(status=200, content_type="text/javascript", body=RECAPTCHA_JS)
        if p.MEDIA_HOST in url:
            return await route.fulfill(status=200, content_type="image/png", body=PNG)
        if p.BATCH_PATH in url:
            form = urllib.parse.parse_qs(request.post_data or "")
            assert form["at"] == ["AT-TOKEN"]
            freq = json.loads(form["f.req"][0])
            rpcid, payload, _, _ = freq[0][0]
            inner = json.loads(payload)
            token = None
            m = re.search(r'"(tok:[^"]+)"', payload)
            if m:
                token = m.group(1)
                if token.endswith("extension_hijack_detected"):
                    return await route.fulfill(status=403, body="PUBLIC_ERROR_UNUSUAL_ACTIVITY")
            if rpcid in {p.RPC_GEN_IMAGE, p.RPC_UPLOAD_IMAGE} | p.BILLED_RPCS:
                assert token, f"{rpcid} went out without a captcha token"
                assert p.CAPTCHA_SLOT not in payload
            self.calls.append((rpcid, inner))
            return await route.fulfill(status=200, content_type="application/json", body=self.reply(rpcid, inner, token))
        return await route.fulfill(status=200, content_type="text/html", body=PAGE)


@pytest.fixture
async def server(tmp_path):
    cookies = tmp_path / "cookies.json"
    cookies.write_text(json.dumps([{"name": "__Secure-1PSID", "value": "x", "domain": ".google.com"}]))
    s = Settings()
    s.cookies_file = str(cookies)
    s.headless = True
    s.chrome_path = None
    s.output_dir = tmp_path / "out"
    s.min_submit_interval_s = 0
    s.sync_wait_s = 30
    s.budget_credits = 100
    mcp, ctx = build_server(s)
    fake = FakeFlow()
    browser = ctx["browser"]
    await browser.start()
    await browser._context.route("**/*", fake.handle)
    ctx["client"].poll_delay_s = 0.05  # fast polling for the test
    yield mcp, ctx, fake
    await browser.close()


def payload_of(result):
    blocks = result.content if hasattr(result, "content") else result
    texts = [b.text for b in blocks if getattr(b, "type", "") == "text"]
    return json.loads(texts[0]), blocks


pytestmark = pytest.mark.skipif(os.environ.get("SKIP_BROWSER_TESTS") == "1", reason="no browser")


async def test_end_to_end(server):
    mcp, ctx, fake = server

    status, _ = payload_of(await mcp.call_tool("flow_status", {}))
    assert status["signed_in"] and status["account"] == "umut@example.com"

    created, _ = payload_of(await mcp.call_tool("flow_create_project", {"title": "Test"}))
    assert created["project_id"] == PID and ctx["client"].default_project == PID

    data_uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    img, blocks = payload_of(await mcp.call_tool("flow_generate_image", {
        "prompt": "a red fox", "count": 2, "aspect": "9:16", "reference_images": [data_uri]}))
    assert len(img["images"]) == 2
    assert any(getattr(b, "type", "") == "image" for b in blocks)
    gen = [c for c in fake.calls if c[0] == p.RPC_GEN_IMAGE][0][1]
    assert gen[1][0][2] == [[UPL, None, None, None, p.REF_TYPE_IMAGE]]  # uploaded then referenced

    # Video without confirm only quotes.
    quote, _ = payload_of(await mcp.call_tool("flow_generate_video", {"prompt": "waves"}))
    assert quote["confirmed"] is False and not any(c[0] == p.RPC_GEN_VIDEO_TEXT for c in fake.calls)

    text_vid, _ = payload_of(await mcp.call_tool("flow_generate_video", {
        "prompt": "waves", "duration_s": 4, "confirm": True}))
    assert text_vid["status"] == "succeeded", text_vid
    assert text_vid["result"]["video_url"].startswith(f"https://{p.MEDIA_HOST}/video/{VID}")

    frame_vid, _ = payload_of(await mcp.call_tool("flow_generate_video", {
        "prompt": "go", "mode": "frame", "family": "veo", "start_image": IMG, "confirm": True}))
    assert frame_vid["status"] == "succeeded", frame_vid
    assert frame_vid["result"]["media_id"] == VID

    budget = ctx["budget"].snapshot()
    assert budget["spent_estimate"] == p.DEFAULT_VIDEO_COST["omni"][4] + p.DEFAULT_VIDEO_COST["veo_lite"]

    up, _ = payload_of(await mcp.call_tool("flow_upscale_image", {"media_id": IMG, "resolution": "4K"}))
    assert os.path.exists(up["saved_to"])

    media, _ = payload_of(await mcp.call_tool("flow_list_media", {}))
    assert media["media"][0]["media_id"] == VID


async def test_budget_blocks_before_any_billed_call(server):
    mcp, ctx, fake = server
    ctx["budget"].ceiling = 5
    await mcp.call_tool("flow_create_project", {})
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="remain in the budget"):
        await mcp.call_tool("flow_generate_video", {"prompt": "x", "confirm": True})
    assert not any(c[0] in p.BILLED_RPCS for c in fake.calls)
