"""MCP tool surface."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Annotated, Any, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from . import protocol as p
from .browser import BrowserError, FlowBrowser, NotSignedInError
from .client import FlowClient
from .config import Settings
from .cookies import CookieError
from .jobs import Budget, BudgetExceeded, Job, JobManager

log = logging.getLogger("google_flow_mcp")

Content = list[TextContent | ImageContent]
ImageInputs = Annotated[Optional[list[str]], Field(
    description="Images as Flow media ids, https URLs, data URIs or base64. "
                "Anything that is not a media id is uploaded first.")]
VideoMode = Literal["text", "frame", "first_last", "references"]

INSTRUCTIONS = """\
Google Flow (flow.google.com) image and video generation through the user's own Google account.

Workflow that spends the fewest credits:
1. Images are free: draft with flow_generate_image (Nano Banana), iterate on the still.
2. Keep characters consistent by passing earlier media ids as reference_images.
3. Animate the approved still with flow_generate_video(mode="frame", start_image=<media id>).
   Video costs credits: call flow_estimate_video_cost first and pass confirm=true.
4. Long jobs answer with a job_id; collect them with flow_wait.

Every media id returned here can be reused as an input to any other tool. Signed media URLs
expire after a few hours; flow_get_media returns fresh ones.
"""


def _text(obj: Any) -> TextContent:
    if not isinstance(obj, str):
        obj = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return TextContent(type="text", text=obj)


def build_server(settings: Optional[Settings] = None) -> tuple[MCPServer, dict]:
    settings = settings or Settings.from_env()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    browser = FlowBrowser(settings)
    budget = Budget(settings.budget_credits, settings.output_dir / "ledger.json")
    client = FlowClient(settings, browser, budget)
    jobs = JobManager()
    started = time.time()

    mcp = MCPServer(
        name="google-flow",
        title="Google Flow",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/UmutKDev/google-flow-mcp",
    )

    # ── helpers ─────────────────────────────────────────────────────────
    def fail(exc: Exception) -> ToolError:
        if isinstance(exc, ToolError):
            return exc
        if isinstance(exc, (NotSignedInError, CookieError)):
            return ToolError(f"Not signed in to Google Flow: {exc}")
        if isinstance(exc, BudgetExceeded):
            return ToolError(str(exc))
        if isinstance(exc, (ValueError, KeyError)):
            return ToolError(str(exc).strip("'\""))
        if isinstance(exc, (BrowserError, p.ProtocolError, TimeoutError)):
            return ToolError(f"Flow error: {exc}")
        log.exception("unexpected tool failure")
        return ToolError(f"{type(exc).__name__}: {exc}")

    async def _diagnose_session() -> dict[str, Any]:
        """Explain a refused session in plain words; spends nothing."""
        try:
            d = await browser.diagnose_landing()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}
        accounts = d.get("google_accounts")
        leads = str(d.get("button_leads_to") or "")
        if accounts == []:
            d["verdict"] = (
                "Google itself does not accept these cookies any more: the session was signed "
                "out or rotated on the computer it came from. Export a fresh session with "
                "`google-flow-mcp login` (it makes a separate session) and do not sign out of it.")
        elif "accounts.google.com" in leads and ("confirm" in leads or "challenge" in leads
                                                 or "signin" in leads):
            d["verdict"] = (
                "Google accepts the cookies but asks this account to confirm its identity before "
                "Flow opens, most likely because the session is now used from a different IP "
                "address. Open flow.google.com once on your own computer and pass the check, "
                "then export fresh cookies. If it keeps happening, run the server from your own "
                "network (local Docker/stdio, or FLOW_BROWSER_PROXY through a home connection).")
        elif accounts:
            d["verdict"] = (
                "Google accepts the cookies, but Flow keeps this session on its public page. "
                "Check that this account can open flow.google.com in a normal browser.")
        return d

    async def inline_image(url: str) -> Optional[ImageContent]:
        try:
            data, mime = await browser.fetch_bytes(url, settings.inline_image_max_bytes)
        except Exception as exc:  # noqa: BLE001
            log.info("could not inline %s: %s", url[:60], exc)
            return None
        from .client import sniff_image_mime

        mime = sniff_image_mime(data) or (mime or "image/jpeg").split(";")[0]
        return ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=mime)

    async def answer_job(job: Job, wait_s: float) -> Content:
        await jobs.wait(job.id, wait_s)
        if job.status == "running":
            return [_text({"job_id": job.id, "status": "running", "progress": job.progress,
                           "next": f"call flow_wait(job_id='{job.id}') to collect the result"})]
        return await render_job(job)

    async def render_job(job: Job) -> Content:
        out: Content = [_text(job.public())]
        if job.status == "succeeded" and isinstance(job.result, dict):
            thumb = job.result.get("thumbnail_url")
            if thumb and job.result.get("_inline_thumbnail"):
                img = await inline_image(thumb)
                if img:
                    out.append(img)
        return out

    RO = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    FREE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
    BILLED = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                             open_world_hint=True)

    # ── session ─────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO)
    async def flow_status() -> Content:
        """Check the Google session, the browser, the default project, the budget and jobs.
        Spends nothing. Run this first when something fails."""
        info: dict[str, Any] = {
            "server_version": __version__,
            "uptime_s": int(time.time() - started),
            "default_project_id": client.default_project,
            "budget": budget.snapshot(),
            "jobs_running": jobs.running,
        }
        try:
            state = await browser.status()
            info.update({
                "signed_in": state.signed_in, "ready": state.at, "account": state.email,
                "page": state.url, "browser": browser.chrome_executable,
                "cookies": browser.cookie_summary,
            })
            if settings.expected_account and state.email and \
                    state.email.lower() != settings.expected_account.lower():
                info["warning"] = f"signed in as {state.email}, expected {settings.expected_account}"
        except Exception as exc:  # noqa: BLE001
            info.update({"signed_in": False, "error": str(fail(exc)),
                         "cookies": browser.cookie_summary})
            if isinstance(exc, NotSignedInError) and browser.running:
                info["diagnosis"] = await _diagnose_session()
        return [_text(info)]

    @mcp.tool(annotations=FREE)
    async def flow_restart_browser() -> Content:
        """Restart Chrome and reload the cookies. Use after updating the cookie file or when the
        tab is stuck."""
        try:
            await browser.restart()
            state = await browser.status()
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        return [_text({"restarted": True, "signed_in": state.signed_in, "account": state.email})]

    @mcp.tool(annotations=RO)
    async def flow_screenshot() -> Content:
        """A PNG of the Flow tab, for debugging sign-in or UI problems."""
        try:
            png = await browser.screenshot()
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        return [ImageContent(type="image", data=base64.b64encode(png).decode(), mime_type="image/png")]

    @mcp.tool(annotations=RO)
    async def flow_list_models() -> Content:
        """The image and video models this server can drive, with estimated credit costs."""
        return [_text({
            "image_models": {
                "NANO_BANANA_PRO": "GEM_PIX_2 (default, best quality)",
                "NANO_BANANA_2": "BELUGA (Nano Banana 2.1)",
                "NANO_BANANA_2_LITE": "HARBOR_SEAL (fastest)",
                "BELUGA_THINKING_LOW|MED|HIGH": "Nano Banana 2.1 with reasoning",
                "note": "Images do not spend credits. Any other Flow wire id is passed through.",
            },
            "image_aspects": sorted(p.IMAGE_ASPECTS),
            "video": {
                "omni": {"modes": ["text", "frame", "first_last", "references"],
                         "durations_s": list(p.OMNI_DURATIONS), "resolutions": list(p.VIDEO_RESOLUTIONS),
                         "estimated_credits": p.DEFAULT_VIDEO_COST["omni"]},
                "veo": {"modes": ["frame"], "models": p.VEO_FRAME_MODELS,
                        "estimated_credits": {k: p.DEFAULT_VIDEO_COST[k] for k in p.VEO_FRAME_MODELS}},
                "aspects": sorted(p.VIDEO_ASPECTS),
                "note": "Costs are conservative estimates; Flow's own quote is authoritative.",
            },
        })]

    # ── projects and media ──────────────────────────────────────────────
    @mcp.tool(annotations=FREE)
    async def flow_create_project(
        title: Annotated[str, Field(description="Project title shown in Flow")] = "google-flow-mcp",
        make_default: Annotated[bool, Field(description="Use it for later calls")] = True,
    ) -> Content:
        """Create a Flow project. Every generation lives in a project."""
        try:
            created = await client.create_project(title)
            if make_default:
                client.use_project(created["project_id"])
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        return [_text(created | {"default": make_default})]

    @mcp.tool(annotations=FREE)
    async def flow_use_project(
        project_id: Annotated[str, Field(description="UUID from flow.google.com/project/<id>")],
    ) -> Content:
        """Make an existing Flow project the default for later calls."""
        try:
            pid = client.use_project(project_id.strip())
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        return [_text({"default_project_id": pid, "url": p.PROJECT_URL.format(pid=pid)})]

    @mcp.tool(annotations=RO)
    async def flow_list_media(
        project_id: Optional[str] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 30,
    ) -> Content:
        """List the images and videos in a project, newest first."""
        try:
            return [_text(await client.list_media(project_id, limit))]
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc

    @mcp.tool(annotations=RO)
    async def flow_get_media(
        media_id: str,
        include_image: Annotated[bool, Field(description="Inline the image (or video poster)")] = False,
    ) -> Content:
        """Fresh signed URLs for a media id (they expire after a few hours)."""
        try:
            urls = await client.media_urls(media_id.strip())
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        out: Content = [_text({"media_id": urls.media_id, "video_url": urls.video, "image_url": urls.image})]
        if include_image and urls.image:
            img = await inline_image(urls.image)
            if img:
                out.append(img)
        return out

    @mcp.tool(annotations=FREE)
    async def flow_upload_image(
        source: Annotated[str, Field(description="https URL, data URI, base64, or a path on the server")],
        project_id: Optional[str] = None,
    ) -> Content:
        """Upload an image into a project so it can be used as a reference, an edit base or a
        video frame. Returns its media id. Free."""
        try:
            return [_text(await client.upload_image(source, project_id))]
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc

    # ── images ──────────────────────────────────────────────────────────
    @mcp.tool(annotations=FREE)
    async def flow_generate_image(
        prompt: Annotated[str, Field(description="What to draw. Ignored when prompts is given.")] = "",
        prompts: Annotated[Optional[list[str]], Field(
            description="1-4 different prompts, one image each, in one call")] = None,
        count: Annotated[int, Field(ge=1, le=4, description="Variants of the same prompt")] = 1,
        aspect: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] = "16:9",
        model: Annotated[Optional[str], Field(
            description="NANO_BANANA_PRO (default), NANO_BANANA_2, NANO_BANANA_2_LITE or a wire id")] = None,
        reference_images: ImageInputs = None,
        edit_image: Annotated[Optional[str], Field(
            description="An image to edit (media id or source); the prompt describes the change")] = None,
        seed: Optional[int] = None,
        project_id: Optional[str] = None,
        return_images: Annotated[bool, Field(description="Inline the pictures in the reply")] = True,
    ) -> Content:
        """Generate images with Nano Banana (text to image, image to image, or edit).
        Free on Flow. Reference images keep characters, products and styles consistent."""
        if not prompt.strip() and not prompts:
            raise ToolError("give a prompt or prompts")
        try:
            result = await client.generate_images(
                prompt, project_id=project_id, prompts=prompts, count=count, aspect=aspect,
                model=model, seed=seed, references=reference_images, edit_image=edit_image)
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        out: Content = [_text(result)]
        if return_images:
            for item in result["images"]:
                img = await inline_image(item["url"])
                if img:
                    out.append(img)
        return out

    @mcp.tool(annotations=FREE)
    async def flow_upscale_image(
        media_id: str,
        resolution: Literal["2K", "4K"] = "2K",
        return_image: Annotated[bool, Field(description="Inline the result (4K can be large)")] = False,
    ) -> Content:
        """Upscale a generated image to 2K or 4K. Saves the file on the server and returns its
        path; optionally inlines it."""
        try:
            data = await client.upscale_image(media_id.strip(), resolution)
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        from .client import sniff_image_mime

        mime = sniff_image_mime(data) or "image/png"
        ext = ".png" if mime == "image/png" else ".jpg"
        path = settings.output_dir / f"{media_id.strip()}-{resolution}{ext}"
        path.write_bytes(data)
        out: Content = [_text({"media_id": media_id, "resolution": resolution, "bytes": len(data),
                               "saved_to": str(path)})]
        if return_image and len(data) <= settings.inline_image_max_bytes:
            out.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=mime))
        return out

    # ── video ───────────────────────────────────────────────────────────
    def spec_for(prompt: str, pid: str, mode: str, family: str, aspect: str, duration_s: int,
                 resolution: str, veo_model: str, start: Optional[str] = None,
                 end: Optional[str] = None, refs: Optional[list[str]] = None) -> p.VideoSpec:
        return client.video_spec(
            prompt, pid, mode=mode, family=family, aspect=aspect, duration_s=duration_s,
            resolution=resolution, veo_model=veo_model, start_media_id=start, end_media_id=end,
            reference_media_ids=refs)

    @mcp.tool(annotations=RO)
    async def flow_estimate_video_cost(
        mode: VideoMode = "text",
        family: Literal["omni", "veo"] = "omni",
        duration_s: Literal[4, 6, 8, 10] = 8,
        resolution: Literal["360p", "720p"] = "720p",
        veo_model: Literal["veo_lite", "veo_lite_low_priority", "veo_fast"] = "veo_lite",
    ) -> Content:
        """Estimate the credits a video would cost and check the remaining budget. Free."""
        dummy = "00000000-0000-0000-0000-000000000000"
        try:
            spec = spec_for("x", dummy, mode, family, "16:9", duration_s, resolution, veo_model,
                            dummy, dummy, [dummy])
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        return [_text({"model_key": spec.model_key, "estimated_credits": spec.estimated_credits,
                       "budget_remaining": budget.remaining,
                       "allowed": spec.estimated_credits <= budget.remaining})]

    @mcp.tool(annotations=BILLED)
    async def flow_generate_video(
        prompt: Annotated[str, Field(description="Motion, camera, sound and dialogue for the clip")],
        mode: Annotated[VideoMode, Field(
            description="text: prompt only | frame: animate start_image | first_last: "
                        "start_image to end_image | references: reference_images as ingredients")] = "text",
        family: Annotated[Literal["omni", "veo"], Field(
            description="omni (all modes) or veo (frame mode only)")] = "omni",
        aspect: Literal["16:9", "9:16"] = "16:9",
        duration_s: Annotated[Literal[4, 6, 8, 10], Field(description="Omni only; Veo is 8 s")] = 8,
        resolution: Literal["360p", "720p"] = "720p",
        veo_model: Literal["veo_lite", "veo_lite_low_priority", "veo_fast"] = "veo_lite",
        start_image: Annotated[Optional[str], Field(description="Media id or image source")] = None,
        end_image: Annotated[Optional[str], Field(description="Media id or image source")] = None,
        reference_images: ImageInputs = None,
        project_id: Optional[str] = None,
        confirm: Annotated[bool, Field(
            description="Must be true: video spends Flow credits")] = False,
        wait_s: Annotated[Optional[float], Field(
            ge=0, le=600, description="How long to wait before answering with a job id")] = None,
    ) -> Content:
        """Generate a video clip (Omni Flash or Veo 3.1). SPENDS CREDITS. Without confirm=true it
        only returns the estimate. Answers with the result, or a job_id to pass to flow_wait."""
        try:
            pid = await client.project(project_id)
            dummy = "00000000-0000-0000-0000-000000000000"
            preview = spec_for(prompt, pid, mode, family, aspect, duration_s, resolution, veo_model,
                               dummy if start_image else None, dummy if end_image else None,
                               [dummy] * len(reference_images or []))
            if not confirm and preview.estimated_credits >= settings.confirm_threshold:
                return [_text({
                    "confirmed": False, "model_key": preview.model_key,
                    "estimated_credits": preview.estimated_credits,
                    "budget_remaining": budget.remaining,
                    "next": "call again with confirm=true to generate",
                })]
            if preview.estimated_credits > budget.remaining:
                raise BudgetExceeded(
                    f"estimated {preview.estimated_credits} credits, {budget.remaining} remain in the budget")
            start = (await client.resolve_images([start_image], pid))[0] if start_image else None
            end = (await client.resolve_images([end_image], pid))[0] if end_image else None
            refs = await client.resolve_images(reference_images, pid) if reference_images else None
            spec = spec_for(prompt, pid, mode, family, aspect, duration_s, resolution, veo_model,
                            start, end, refs)
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc

        async def work(job: Job) -> dict:
            def progress(msg: str) -> None:
                job.progress = msg
            res = await client.generate_video(spec, pid, progress=progress)
            return {"media_id": res.media_id, "video_url": res.video_url,
                    "thumbnail_url": res.thumbnail_url, "operation_id": res.operation_id,
                    "project_id": res.project_id, "model_key": res.model_key,
                    "estimated_credits": res.estimated_credits, "_inline_thumbnail": True}

        job = jobs.start("video", f"{spec.model_key}: {prompt}", work)
        return await answer_job(job, settings.sync_wait_s if wait_s is None else wait_s)

    @mcp.tool(annotations=BILLED)
    async def flow_storyboard(
        shots: Annotated[list[str], Field(min_length=1, max_length=12,
                                          description="One prompt per shot, in order")],
        reference_images: ImageInputs = None,
        style: Annotated[str, Field(description="Appended to every shot prompt")] = "",
        aspect: Literal["16:9", "9:16"] = "16:9",
        animate: Annotated[bool, Field(description="Also animate each still (spends credits)")] = False,
        duration_s: Literal[4, 6, 8, 10] = 8,
        motion_prompts: Annotated[Optional[list[str]], Field(
            description="Per-shot motion prompts for animation; defaults to the shot prompt")] = None,
        project_id: Optional[str] = None,
        confirm: bool = False,
    ) -> Content:
        """Multi-shot pipeline: a consistent still per shot (free, sharing reference_images),
        then optionally one Omni clip per still. Always runs as a job; collect it with flow_wait."""
        try:
            pid = await client.project(project_id)
            refs = await client.resolve_images(reference_images, pid) if reference_images else []
            per_clip = int(p.DEFAULT_VIDEO_COST["omni"][duration_s])
            total = per_clip * len(shots) if animate else 0
            if animate and not confirm:
                return [_text({"confirmed": False, "shots": len(shots),
                               "estimated_credits": total, "budget_remaining": budget.remaining,
                               "next": "call again with confirm=true to run"})]
            if total > budget.remaining:
                raise BudgetExceeded(f"storyboard needs ~{total} credits, {budget.remaining} remain")
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc

        async def work(job: Job) -> dict:
            frames: list[dict] = []
            for i, shot in enumerate(shots, 1):
                job.progress = f"still {i}/{len(shots)}"
                text = f"{shot}. {style}".strip(". ") if style else shot
                r = await client.generate_images(text, project_id=pid, aspect=aspect, references=refs)
                img = r["images"][0] if r["images"] else None
                frames.append({"shot": i, "prompt": text, "media_id": img and img["media_id"],
                               "image_url": img and img["url"], "warning": r.get("warning")})
            if animate:
                for i, frame in enumerate(frames):
                    if not frame["media_id"]:
                        continue
                    job.progress = f"clip {i + 1}/{len(frames)}"
                    motion = (motion_prompts[i] if motion_prompts and i < len(motion_prompts)
                              else frame["prompt"])
                    spec = client.video_spec(motion, pid, mode="frame", family="omni", aspect=aspect,
                                             duration_s=duration_s, start_media_id=frame["media_id"])
                    try:
                        res = await client.generate_video(spec, pid)
                        frame.update({"video_media_id": res.media_id, "video_url": res.video_url})
                    except Exception as exc:  # noqa: BLE001
                        frame["video_error"] = str(exc)
            return {"project_id": pid, "shots": frames}

        job = jobs.start("storyboard", f"{len(shots)} shots", work)
        return await answer_job(job, 5)

    # ── jobs ────────────────────────────────────────────────────────────
    @mcp.tool(annotations=RO)
    async def flow_wait(
        job_id: str,
        timeout_s: Annotated[float, Field(ge=0, le=600)] = 45,
    ) -> Content:
        """Wait for a job and return its result (or its progress if still running)."""
        try:
            job = await jobs.wait(job_id.strip(), timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc
        if job.status == "running":
            return [_text({"job_id": job.id, "status": "running", "progress": job.progress,
                           "elapsed_s": round(time.time() - job.created)})]
        return await render_job(job)

    @mcp.tool(annotations=RO)
    async def flow_job_status(job_id: Optional[str] = None) -> Content:
        """One job's status, or the 20 most recent jobs when job_id is omitted. Never waits."""
        try:
            if job_id:
                return [_text(jobs.get(job_id.strip()).public())]
            return [_text({"jobs": jobs.list()})]
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc

    @mcp.tool(annotations=FREE)
    async def flow_cancel_job(job_id: str) -> Content:
        """Stop waiting for a job. Flow may still finish (and bill) a clip it already accepted."""
        try:
            return [_text(jobs.cancel(job_id.strip()).public())]
        except Exception as exc:  # noqa: BLE001
            raise fail(exc) from exc

    # ── resources ───────────────────────────────────────────────────────
    @mcp.resource("flow://guide", name="guide", mime_type="text/markdown",
                  description="How to get good, cheap results from this server")
    def guide() -> str:
        return INSTRUCTIONS

    # ── HTTP extras ─────────────────────────────────────────────────────
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "browser_running": browser.running,
                             "jobs_running": jobs.running})

    return mcp, {"settings": settings, "browser": browser, "client": client, "jobs": jobs,
                 "budget": budget}


class BearerAuth:
    """Optional shared-secret check for standalone deployments."""

    def __init__(self, app, token: str, open_paths: tuple[str, ...] = ("/healthz",)):
        self.app = app
        self.token = token
        self.open_paths = open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") not in self.open_paths:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if auth != f"Bearer {self.token}":
                from starlette.responses import JSONResponse

                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def serve_http(settings: Settings) -> None:
    import uvicorn

    mcp, ctx = build_server(settings)
    app = mcp.streamable_http_app(streamable_http_path=settings.path, host=settings.host,
                                  stateless_http=False)
    if settings.auth_token:
        app = BearerAuth(app, settings.auth_token)
    config = uvicorn.Config(app, host=settings.host, port=settings.port,
                            log_level=settings.log_level.lower(), proxy_headers=True,
                            forwarded_allow_ips="*")
    try:
        await uvicorn.Server(config).serve()
    finally:
        await ctx["browser"].close()


async def serve_stdio(settings: Settings) -> None:
    mcp, ctx = build_server(settings)
    try:
        await mcp.run_stdio_async()
    finally:
        await ctx["browser"].close()
