"""High-level Flow operations built on the tab and the protocol."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import protocol as p
from .browser import BrowserError, FlowBrowser
from .config import Settings
from .jobs import Budget

log = logging.getLogger("google_flow_mcp.client")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DATA_URI = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.*)$", re.I | re.S)
MAX_UPLOAD_BYTES = 12_000_000


def sniff_image_mime(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


async def _reject_private_host(url: str) -> None:
    """Keep image URLs from reaching cluster-internal services (SSRF)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {host}: {exc}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"refusing to fetch {host}: it resolves to a private address")


def is_media_id(value: str) -> bool:
    return bool(_UUID.match(value.strip()))


@dataclass(slots=True)
class VideoResult:
    media_id: str
    video_url: Optional[str]
    thumbnail_url: Optional[str]
    operation_id: str
    project_id: str
    model_key: str
    estimated_credits: int
    note: Optional[str] = None


class FlowClient:
    def __init__(self, settings: Settings, browser: FlowBrowser, budget: Budget):
        self.settings = settings
        self.browser = browser
        self.budget = budget
        self._project_id: Optional[str] = settings.project_id
        self._state_file = settings.output_dir / "state.json"
        self.poll_delay_s = 6.0
        if not self._project_id:
            try:
                self._project_id = json.loads(self._state_file.read_text()).get("project_id")
            except Exception:  # noqa: BLE001
                pass

    # ── projects ────────────────────────────────────────────────────────
    @property
    def default_project(self) -> Optional[str]:
        return self._project_id

    def use_project(self, project_id: str) -> str:
        if not is_media_id(project_id):
            raise ValueError("project_id must be the UUID from a flow.google.com/project/<id> URL")
        self._project_id = project_id
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({"project_id": project_id}))
        except Exception:  # noqa: BLE001
            pass
        return project_id

    async def create_project(self, title: str = "google-flow-mcp") -> dict:
        payload = await self.browser.rpc_payload(p.RPC_CREATE_PROJECT, p.create_project_request(title))
        pid, name = p.read_created_project(payload)
        return {"project_id": pid, "title": name or title,
                "url": p.PROJECT_URL.format(pid=pid)}

    async def project(self, explicit: Optional[str] = None) -> str:
        """Explicit id, then the default, then the tab's current project, then a new one."""
        if explicit:
            if not is_media_id(explicit):
                raise ValueError("project_id must be a UUID")
            return explicit
        if self._project_id:
            return self._project_id
        state = await self.browser.status()
        if state.project:
            return self.use_project(state.project)
        created = await self.create_project("google-flow-mcp")
        return self.use_project(created["project_id"])

    # ── inputs ──────────────────────────────────────────────────────────
    async def _read_source(self, source: str) -> tuple[bytes, str, str]:
        """Bytes, mime and a file name from an https URL, a data URI, base64 or a path."""
        src = source.strip()
        m = _DATA_URI.match(src)
        if m:
            data = base64.b64decode(m.group(2))
            name = "upload"
        elif src.startswith(("http://", "https://")):
            import httpx

            await _reject_private_host(src)
            async with httpx.AsyncClient(follow_redirects=False, timeout=60) as c:
                r = await c.get(src)
                r.raise_for_status()
                data = r.content
            name = Path(src.split("?", 1)[0]).name or "upload"
        elif len(src) > 256 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", src[:2048]):
            data = base64.b64decode(src)
            name = "upload"
        else:
            path = Path(src).expanduser().resolve()
            allowed = [self.settings.output_dir.resolve()] + [
                Path(d).resolve() for d in self.settings.input_dirs]
            if not any(path == d or d in path.parents for d in allowed):
                raise ValueError(
                    "file paths are only read from FLOW_OUTPUT_DIR or FLOW_INPUT_DIRS on the server; "
                    "pass an https URL, data URI or base64 instead")
            if not path.is_file():
                raise ValueError(
                    f"image source {src[:80]!r} is not an https URL, data URI, base64 string, "
                    "Flow media id, or a file that exists on the server")
            data = path.read_bytes()
            name = path.name
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"image is {len(data)} bytes; the limit is {MAX_UPLOAD_BYTES}")
        mime = sniff_image_mime(data)
        if not mime:
            raise ValueError("the source is not a JPEG, PNG, WebP or GIF image")
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}[mime]
        if not name.lower().endswith(ext):
            name = Path(name).stem + ext
        return data, mime, name

    async def upload_image(self, source: str, project_id: Optional[str] = None) -> dict:
        pid = await self.project(project_id)
        data, mime, name = await self._read_source(source)
        payload = await self.browser.rpc_payload(
            p.RPC_UPLOAD_IMAGE, p.upload_request(base64.b64encode(data).decode(), pid, mime, name),
            project_id=pid, captcha_action=p.CAPTCHA_IMAGE, retries=2)
        return {"media_id": p.read_uploaded_media_id(payload), "project_id": pid,
                "mime_type": mime, "bytes": len(data)}

    async def resolve_images(self, items: Optional[list[str]], project_id: str) -> list[str]:
        """Media ids pass through; anything else is uploaded first."""
        out: list[str] = []
        for item in items or []:
            if not item:
                continue
            if is_media_id(item):
                out.append(item.strip())
            else:
                out.append((await self.upload_image(item, project_id))["media_id"])
        return out

    # ── images ──────────────────────────────────────────────────────────
    async def generate_images(self, prompt: str, *, project_id: Optional[str] = None,
                              prompts: Optional[list[str]] = None, count: int = 1,
                              aspect: str = "1:1", model: Optional[str] = None,
                              seed: Optional[int] = None, seeds: Optional[list[int]] = None,
                              references: Optional[list[str]] = None,
                              edit_image: Optional[str] = None) -> dict:
        pid = await self.project(project_id)
        refs = await self.resolve_images(references, pid)
        base = (await self.resolve_images([edit_image], pid))[0] if edit_image else None
        wire_model = p.resolve_image_model(model or self.settings.default_image_model)
        freq = p.image_request(prompt, pid, count=count, aspect=aspect, model=wire_model,
                               seed=seed, seeds=seeds, prompts=prompts,
                               reference_media_ids=refs, base_media_id=base)
        expected = len(prompts) if prompts else count
        payload = await self.browser.rpc_payload(
            p.RPC_GEN_IMAGE, freq, project_id=pid, captcha_action=p.CAPTCHA_IMAGE, retries=2)
        images = p.read_images(payload)
        result = {
            "project_id": pid, "model": wire_model, "aspect": aspect, "requested": expected,
            "images": [{"media_id": i.media_id, "url": i.url} for i in images],
        }
        if len(images) < expected:
            result["warning"] = (
                f"Flow returned {len(images)} of {expected} images. A missing image usually "
                "means the prompt was filtered; rephrase it and retry.")
        return result

    async def upscale_image(self, media_id: str, resolution: str = "2K") -> bytes:
        payload = await self.browser.rpc_payload(
            p.RPC_UPSCALE_IMAGE, p.image_upscale_request(media_id, resolution),
            project_id=self._project_id, captcha_action=p.CAPTCHA_IMAGE, retries=1)
        return base64.b64decode(p.read_upscaled_image(payload))

    # ── media ───────────────────────────────────────────────────────────
    async def media_urls(self, media_id: str) -> p.MediaUrls:
        payload = await self.browser.rpc_payload(p.RPC_MEDIA, p.media_request(media_id))
        return p.read_media_urls(payload, media_id)

    async def list_media(self, project_id: Optional[str] = None, limit: int = 30) -> dict:
        pid = await self.project(project_id)
        text = await self.browser.rpc(p.RPC_PROJECT_MEDIA, p.project_media_request(pid),
                                      max_chars=8_000_000)
        try:
            items = p.read_project_media(p.first_payload(text, p.RPC_PROJECT_MEDIA), limit=limit)
        except (p.ProtocolError, ValueError):
            items = []
        return {"project_id": pid, "count": len(items), "media": items,
                "url": p.PROJECT_URL.format(pid=pid)}

    # ── video ───────────────────────────────────────────────────────────
    def video_spec(self, prompt: str, project_id: str, **kw: Any) -> p.VideoSpec:
        return p.video_request(prompt, project_id, **kw)

    async def submit_video(self, spec: p.VideoSpec, project_id: str) -> p.Operation:
        payload = await self.browser.rpc_payload(
            spec.rpcid, spec.envelope, project_id=project_id,
            captcha_action=p.CAPTCHA_VIDEO, retries=0)
        op = p.read_video_submit(payload)
        op.project_id = op.project_id or project_id
        return op

    async def wait_video(self, op: p.Operation, project_id: str, *, timeout_s: int,
                         progress: Callable[[str], None] = lambda _m: None) -> p.MediaUrls:
        """Poll until the clip has a playable URL.

        The operation poll can lag behind reality, so the project listing is the
        authority; it is large, so the tab only hands back a window around the
        operation id, and it is consulted only every third round or when the
        poll says something happened.
        """
        deadline = time.monotonic() + timeout_s
        media_id = op.media_id
        complaint = None
        rounds = 0
        delay = self.poll_delay_s
        while time.monotonic() < deadline:
            rounds += 1
            if not media_id:
                look = rounds % 3 == 0
                try:
                    polled = p.read_operation(await self.browser.rpc_payload(
                        p.RPC_OPERATION, p.operation_request(op.operation_id)))
                    complaint = polled.error or complaint
                    look = look or polled.done or bool(polled.error)
                    progress(f"poll {rounds}: status={polled.status or 'pending'}")
                except (BrowserError, p.ProtocolError) as exc:
                    log.debug("operation poll failed: %s", exc)
                    look = True
                if look:
                    window = await self.browser.rpc(
                        p.RPC_PROJECT_MEDIA, p.project_media_request(project_id),
                        match=op.operation_id)
                    media_id = p.find_media_id_in_text(window, op.operation_id)
            if media_id:
                urls = await self.media_urls(media_id)
                if urls.video:
                    return urls
                progress(f"poll {rounds}: media {media_id[:8]} rendering")
            await asyncio.sleep(delay)
            delay = min(delay * 1.25, max(self.poll_delay_s, 20.0))
        raise TimeoutError(
            f"video not ready after {timeout_s}s (operation {op.operation_id}"
            + (f", Flow said: {complaint}" if complaint else "")
            + "). It may still finish: check flow_list_media later.")

    async def generate_video(self, spec: p.VideoSpec, project_id: str, *,
                             progress: Callable[[str], None] = lambda _m: None) -> VideoResult:
        what = f"video {spec.model_key}"
        self.budget.reserve(spec.estimated_credits, what)
        try:
            op = await self.submit_video(spec, project_id)
        except Exception:
            self.budget.release(spec.estimated_credits)
            raise
        self.budget.commit(spec.estimated_credits, what, op.operation_id)
        progress(f"submitted operation {op.operation_id}")
        urls = await self.wait_video(op, project_id, timeout_s=self.settings.job_timeout_s,
                                     progress=progress)
        return VideoResult(urls.media_id, urls.video, urls.image, op.operation_id, project_id,
                           spec.model_key, spec.estimated_credits)
