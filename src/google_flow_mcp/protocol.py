"""Google Flow wire protocol (flow.google.com, batchexecute).

Since September 2026 Flow lives at ``flow.google.com``. Every call is a POST to
one ``batchexecute`` endpoint, signed by the session cookies plus the page's
``at`` token (``WIZ_global_data.SNlM0e``). Every *generation* also carries a
single-use reCAPTCHA Enterprise token that can only be minted inside a real
Flow tab. This module is pure: it builds ``f.req`` envelopes and parses
replies. It never touches the network; :mod:`google_flow_mcp.browser` runs the
envelopes inside the page.

Wire format::

    f.req = [[[rpcid, "<inner payload as a JSON string>", null, "generic"]]]

and the reply is the ``)]}'`` sentinel followed by length-prefixed chunks of
``["wrb.fr", rpcid, "<payload as a JSON string>", ...]``.

The payload shapes are derived from the public, MIT-licensed work of
crisng95/flowkit and alarconcesar/flow-mcp (see ``LICENSES/``).
"""

from __future__ import annotations

import json
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# ── endpoints ────────────────────────────────────────────────────────────────
FLOW_HOME = "https://flow.google.com/"
PROJECT_URL = "https://flow.google.com/project/{pid}"
BATCH_PATH = "/_/AiSandboxAngularFrontend/data/batchexecute"
MEDIA_HOST = "flow-content.google"

# ── RPC ids ──────────────────────────────────────────────────────────────────
RPC_GEN_IMAGE = "ogiZ0b"
RPC_GEN_VIDEO_FRAME = "eb1hJf"        # image (first frame) -> video
RPC_GEN_VIDEO_TEXT = "YhhmEf"         # text -> video
RPC_GEN_VIDEO_FIRST_LAST = "nprQif"   # first + last frame -> video
RPC_GEN_VIDEO_REFERENCES = "MZZa6b"   # reference images ("ingredients") -> video
RPC_OPERATION = "jwpduf"
RPC_CREATE_PROJECT = "jHPbke"
RPC_PROJECT_MEDIA = "Zzl0ze"
RPC_MEDIA = "as29s"
RPC_UPLOAD_IMAGE = "maseQ"
RPC_UPSCALE_IMAGE = "SPrCad"

#: RPCs that spend credits. The budget gate keys off this set.
BILLED_RPCS = frozenset({
    RPC_GEN_VIDEO_FRAME, RPC_GEN_VIDEO_TEXT, RPC_GEN_VIDEO_FIRST_LAST, RPC_GEN_VIDEO_REFERENCES,
})

CAPTCHA_IMAGE = "IMAGE_GENERATION"
CAPTCHA_VIDEO = "VIDEO_GENERATION"

#: The envelope travels with this marker; the page swaps in a fresh token
#: moments before the request leaves, so the token never sits in Python.
CAPTCHA_SLOT = "__CAPTCHA__"

SURFACE_ID = 22
FULL_FRAME_CROP = [None, None, 1, 1]
REF_TYPE_IMAGE = 1
BASE_TYPE_IMAGE = 2
STATUS_DONE = "CAE"
OUTCOME_COMPLAINT = 4

# ── image models ─────────────────────────────────────────────────────────────
IMAGE_MODEL_DEFAULT = "GEM_PIX_2"
IMAGE_MODELS = {
    "GEM_PIX_2", "NARWHAL", "HARBOR_SEAL",
    "BELUGA", "BELUGA_THINKING_LOW", "BELUGA_THINKING_MED", "BELUGA_THINKING_HIGH",
}
IMAGE_MODEL_ALIASES = {
    "NANO_BANANA_PRO": "GEM_PIX_2",
    "NANO_BANANA_2_1": "BELUGA",
    "NANO_BANANA_2": "BELUGA",
    "NANO_BANANA_2_LITE": "HARBOR_SEAL",
    "NANO_BANANA_LITE": "HARBOR_SEAL",
    "NANO-PRO": "GEM_PIX_2",
    "NANO2": "BELUGA",
}
_WIRE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,95}$")

# Image aspect ratios (measured from the JPEG header of real generations).
IMAGE_ASPECTS = {"1:1": 1, "9:16": 2, "16:9": 3, "3:4": 4, "4:3": 5}
IMAGE_UPSCALE_RESOLUTIONS = {"2K": 1, "4K": 2}

# Video aspect uses a DIFFERENT encoding: 1 is portrait here.
VIDEO_ASPECTS = {"9:16": 1, "16:9": 2}

# ── video model catalogue ────────────────────────────────────────────────────
VIDEO_MODES = ("text", "frame", "first_last", "references")
OMNI_DURATIONS = (4, 6, 8, 10)
VIDEO_RESOLUTIONS = ("360p", "720p")
#: Veo keys verified on the batchexecute path (image -> video only).
VEO_FRAME_MODELS = {
    "veo_lite": "veo_3_1_i2v_lite",
    "veo_lite_low_priority": "veo_3_1_i2v_lite_low_priority",
    "veo_fast": "veo_3_1_i2v_s_fast_ultra",
}

#: Approximate credit cost per clip, used by the budget gate when the caller
#: does not override it. Flow shows the exact quote in its UI; these values come
#: from community measurements (Pro plan, Oct-Nov 2026) and are deliberately
#: rounded up so the gate errs on the safe side.
DEFAULT_VIDEO_COST = {
    "omni": {4: 15, 6: 20, 8: 25, 10: 30},
    "veo_lite": 10,
    "veo_lite_low_priority": 10,
    "veo_fast": 20,
}


class ProtocolError(RuntimeError):
    """A reply did not have the shape we expect."""


class RpcError(ProtocolError):
    def __init__(self, rpcid: str, detail: Any):
        super().__init__(f"{rpcid} failed: {detail!r}")
        self.rpcid = rpcid
        self.detail = detail


@dataclass(slots=True)
class RpcResult:
    rpcid: str
    data: Any = None
    error: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class GeneratedImage:
    media_id: str
    url: str


@dataclass(slots=True)
class Operation:
    operation_id: str
    project_id: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None
    media_id: Optional[str] = None

    @property
    def done(self) -> bool:
        return self.status == STATUS_DONE


@dataclass(slots=True)
class MediaUrls:
    media_id: str
    video: Optional[str] = None
    image: Optional[str] = None


@dataclass(slots=True)
class VideoSpec:
    """A resolved video request: which RPC, which model key, what it costs."""

    rpcid: str
    model_key: str
    family: str
    estimated_credits: int
    envelope: str = field(repr=False, default="")


# ══════════════════════════════════════════════════════════════════════════
#  envelope codec
# ══════════════════════════════════════════════════════════════════════════

def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def build_envelope(rpcid: str, inner: Any) -> str:
    return _dumps([[[rpcid, _dumps(inner), None, "generic"]]])


def parse_envelope(text: str) -> list[RpcResult]:
    """Strip the ``)]}'`` sentinel and decode every ``wrb.fr`` chunk.

    Length prefixes count characters but drift by a byte or two on escaped
    payloads, so the decoder scans for JSON arrays instead of trusting them.
    """
    if not text:
        return []
    body = text.split("\n", 1)[1] if text.startswith(")]}'") else text
    decoder = json.JSONDecoder()
    results: list[RpcResult] = []
    index = 0
    while index < len(body):
        start = body.find("[", index)
        if start == -1:
            break
        try:
            chunk, consumed = decoder.raw_decode(body[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = start + consumed
        for entry in chunk if isinstance(chunk, list) else []:
            if not isinstance(entry, list) or not entry or entry[0] != "wrb.fr":
                continue
            rpcid = entry[1] if len(entry) > 1 else "?"
            payload = entry[2] if len(entry) > 2 else None
            if payload is None:
                results.append(RpcResult(rpcid, None, entry[5] if len(entry) > 5 else True))
                continue
            try:
                data = json.loads(payload) if isinstance(payload, str) else payload
            except json.JSONDecodeError:
                data = payload
            results.append(RpcResult(rpcid, data))
    return results


def first_payload(text: str, rpcid: str) -> Any:
    results = parse_envelope(text)
    for result in results:
        if result.rpcid != rpcid:
            continue
        if not result.ok:
            raise RpcError(rpcid, result.error)
        return result.data
    raise ProtocolError(f"no {rpcid} envelope in reply ({len(results)} other envelopes)")


def apply_captcha(freq: str, token: Optional[str]) -> str:
    return freq.replace(CAPTCHA_SLOT, token or "") if CAPTCHA_SLOT in freq else freq


def needs_captcha(freq: str) -> bool:
    return CAPTCHA_SLOT in freq


# ══════════════════════════════════════════════════════════════════════════
#  resolvers
# ══════════════════════════════════════════════════════════════════════════

def resolve_image_model(key: Optional[str]) -> str:
    if key is None or not str(key).strip():
        return IMAGE_MODEL_DEFAULT
    token = str(key).strip().upper().replace(" ", "_")
    if token in IMAGE_MODEL_ALIASES:
        return IMAGE_MODEL_ALIASES[token]
    if token in IMAGE_MODELS or _WIRE_ID_RE.match(token):
        # Unknown-but-well-formed ids pass through so a newly exposed model
        # works before this server ships a release.
        return token
    raise ValueError(f"unknown image model {key!r}")


def resolve_image_aspect(aspect: Any) -> int:
    key = str(aspect).strip()
    if key in IMAGE_ASPECTS:
        return IMAGE_ASPECTS[key]
    raise ValueError(f"image aspect must be one of {sorted(IMAGE_ASPECTS)}")


def resolve_video_aspect(aspect: Any) -> int:
    key = str(aspect).strip()
    if key in VIDEO_ASPECTS:
        return VIDEO_ASPECTS[key]
    raise ValueError(f"video aspect must be one of {sorted(VIDEO_ASPECTS)}")


# ══════════════════════════════════════════════════════════════════════════
#  request builders
# ══════════════════════════════════════════════════════════════════════════

def client_uuid() -> str:
    """The web UI sends request ids upper-case; match it."""
    return str(uuid.uuid4()).upper()


def _context(project_id: Optional[str], asset_id: Optional[str] = None) -> list:
    return [None, SURFACE_ID, None, None, asset_id, project_id, None, None, None, None,
            [CAPTCHA_SLOT, 1]]


def _image_input(media_id: str, input_type: int) -> list:
    return [media_id, None, None, None, input_type]


def image_request(
    prompt: str,
    project_id: str,
    *,
    count: int = 1,
    aspect: str = "1:1",
    model: Optional[str] = None,
    seed: Optional[int] = None,
    seeds: Optional[list[int]] = None,
    prompts: Optional[list[str]] = None,
    reference_media_ids: Optional[list[str]] = None,
    base_media_id: Optional[str] = None,
) -> str:
    """Text/image -> image. One request item per variant (there is no count
    field). ``reference_media_ids`` condition the result (character
    consistency); ``base_media_id`` is the image being edited."""
    if prompts:
        count = len(prompts)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 4:
        raise ValueError("image count must be 1-4")
    ratio = resolve_image_aspect(aspect)
    wire_model = resolve_image_model(model)
    base_seed = seed if seed is not None else random.randint(1, 10**9)
    ctx = _context(project_id, base_media_id)
    items = []
    for i in range(count):
        text = prompts[i] if prompts else prompt
        item_seed = seeds[i] if seeds and i < len(seeds) else base_seed + i * 9973
        inputs: list = []
        if base_media_id:
            inputs.append(_image_input(base_media_id, BASE_TYPE_IMAGE))
        inputs.extend(_image_input(m, REF_TYPE_IMAGE)
                      for m in (reference_media_ids or []) if m != base_media_id)
        items.append([None, None, inputs or None, item_seed, ratio, wire_model, None,
                      ctx, [[[text]]], None, None, None, None, client_uuid()])
    return build_envelope(RPC_GEN_IMAGE, [None, items, 1, ctx, [client_uuid()]])


def image_upscale_request(media_id: str, resolution: str = "2K") -> str:
    key = str(resolution).strip().upper()
    if key not in IMAGE_UPSCALE_RESOLUTIONS:
        raise ValueError("image upscale resolution must be 2K or 4K")
    return build_envelope(RPC_UPSCALE_IMAGE, [media_id, IMAGE_UPSCALE_RESOLUTIONS[key], _context(None)])


def upload_request(image_b64: str, project_id: str, mime_type: str = "image/jpeg",
                   file_name: str = "upload.jpg") -> str:
    """Bytes ride inside the RPC as plain base64; the call carries a captcha."""
    return build_envelope(RPC_UPLOAD_IMAGE, [
        _context(project_id), image_b64, mime_type, 1, None, None, None, None,
        file_name, None, client_uuid(), client_uuid(),
    ])


def create_project_request(title: str) -> str:
    clean = " ".join(str(title or "google-flow-mcp").split())[:160] or "google-flow-mcp"
    return build_envelope(RPC_CREATE_PROJECT, ["projects/*", [None, [clean]], [None, SURFACE_ID]])


def operation_request(operation_id: str) -> str:
    return build_envelope(RPC_OPERATION, [None, None, [[operation_id]]])


def project_media_request(project_id: str) -> str:
    return build_envelope(RPC_PROJECT_MEDIA, [f"projects/{project_id}", None, None, None, [1]])


def media_request(media_id: str) -> str:
    return build_envelope(RPC_MEDIA, [media_id])


def _prompt_block(prompt: str) -> list:
    return [None, None, [[[prompt]]]]


def _frame(media_id: str, crop: Optional[list] = None) -> list:
    return [None, media_id, None, None, None, FULL_FRAME_CROP if crop is None else crop]


def _ids() -> list:
    return [None, None, None, None, client_uuid(), client_uuid()]


def _video_envelope(rpcid: str, request: list, project_id: str) -> str:
    return build_envelope(rpcid, [[request], _context(project_id), [client_uuid(), 2]])


def video_request(
    prompt: str,
    project_id: str,
    *,
    mode: str = "text",
    family: str = "omni",
    aspect: str = "16:9",
    duration_s: int = 8,
    resolution: str = "720p",
    start_media_id: Optional[str] = None,
    end_media_id: Optional[str] = None,
    reference_media_ids: Optional[list[str]] = None,
    veo_model: str = "veo_lite",
) -> VideoSpec:
    """Resolve and build every supported video request.

    ===========  ======  ===========================================
    mode         family  notes
    ===========  ======  ===========================================
    text         omni    ``abra_t2v_<N>s``
    frame        omni    ``abra_i2v_<N>s`` (start frame required)
    frame        veo     ``veo_3_1_i2v_*`` (8 s, 720p)
    first_last   omni    ``omni_flash_i2v_<N>s_first_last``
    references   omni    ``abra_r2v_<N>s`` (1+ reference images)
    ===========  ======  ===========================================
    """
    if mode not in VIDEO_MODES:
        raise ValueError(f"mode must be one of {VIDEO_MODES}")
    family = (family or "omni").lower()
    if family not in ("omni", "veo"):
        raise ValueError("family must be 'omni' or 'veo'")
    ratio = resolve_video_aspect(aspect)
    res = str(resolution).strip().lower()
    if res not in VIDEO_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {VIDEO_RESOLUTIONS}")
    low = res == "360p"
    suffix = "_360p" if low else ""

    if family == "veo":
        if mode != "frame":
            raise ValueError(
                "Veo on the new Flow API is only verified for mode='frame' (image -> video). "
                "Use family='omni' for text, first_last and references modes.")
        if not start_media_id:
            raise ValueError("mode='frame' needs start_media_id")
        if veo_model not in VEO_FRAME_MODELS:
            raise ValueError(f"veo_model must be one of {sorted(VEO_FRAME_MODELS)}")
        key = VEO_FRAME_MODELS[veo_model]
        request = [_prompt_block(prompt), key, ratio, None, _frame(start_media_id), _ids()]
        return VideoSpec(RPC_GEN_VIDEO_FRAME, key, "veo", int(DEFAULT_VIDEO_COST[veo_model]),
                         _video_envelope(RPC_GEN_VIDEO_FRAME, request, project_id))

    if duration_s not in OMNI_DURATIONS:
        raise ValueError(f"Omni duration must be one of {OMNI_DURATIONS} seconds")
    cost = int(DEFAULT_VIDEO_COST["omni"][duration_s])

    if mode == "text":
        key = f"abra_t2v_{duration_s}s{suffix}"
        request = [_prompt_block(prompt), key, ratio, None, _ids()]
        if low:
            request.extend([None, None, [4]])
        return VideoSpec(RPC_GEN_VIDEO_TEXT, key, "omni", cost,
                         _video_envelope(RPC_GEN_VIDEO_TEXT, request, project_id))

    if mode == "frame":
        if not start_media_id:
            raise ValueError("mode='frame' needs start_media_id")
        key = f"abra_i2v_{duration_s}s{suffix}"
        request = [_prompt_block(prompt), key, ratio, None, _frame(start_media_id), _ids()]
        if low:
            request.extend([None, None, None, [4]])
        return VideoSpec(RPC_GEN_VIDEO_FRAME, key, "omni", cost,
                         _video_envelope(RPC_GEN_VIDEO_FRAME, request, project_id))

    if mode == "first_last":
        if not (start_media_id and end_media_id):
            raise ValueError("mode='first_last' needs start_media_id and end_media_id")
        key = f"omni_flash_i2v_{duration_s}s_first_last{suffix}"
        request = [_prompt_block(prompt), key, ratio, None, _frame(start_media_id),
                   _frame(end_media_id), _ids()]
        return VideoSpec(RPC_GEN_VIDEO_FIRST_LAST, key, "omni", cost,
                         _video_envelope(RPC_GEN_VIDEO_FIRST_LAST, request, project_id))

    refs = [str(m) for m in (reference_media_ids or []) if str(m)]
    if not refs:
        raise ValueError("mode='references' needs at least one reference_media_id")
    key = f"abra_r2v_{duration_s}s{suffix}"
    request = [_prompt_block(prompt), [[None, m] for m in refs], key, ratio, None, _ids()]
    if low:
        request.extend([None, None, None, None, None, [4]])
    return VideoSpec(RPC_GEN_VIDEO_REFERENCES, key, "omni", cost,
                     _video_envelope(RPC_GEN_VIDEO_REFERENCES, request, project_id))


# ══════════════════════════════════════════════════════════════════════════
#  reply readers
# ══════════════════════════════════════════════════════════════════════════

def walk_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from walk_strings(item)


def walk_lists(node: Any) -> Iterator[list]:
    if isinstance(node, list):
        yield node
        for item in node:
            yield from walk_lists(item)


def read_images(payload: Any) -> list[GeneratedImage]:
    """Signed CDN URLs come back inline; the media id is read off the URL path
    so a reordered reply can never pair an id with the wrong picture."""
    images: list[GeneratedImage] = []
    seen: set[str] = set()
    for text in walk_strings(payload):
        marker = MEDIA_HOST + "/image/"
        if marker not in text:
            continue
        media_id = text.split("/image/", 1)[1].split("?", 1)[0]
        if media_id and media_id not in seen:
            seen.add(media_id)
            images.append(GeneratedImage(media_id, text))
    return images


def read_uploaded_media_id(payload: Any) -> str:
    record = payload[0] if isinstance(payload, list) and payload else None
    media_id = record[0] if isinstance(record, list) and record else None
    if not isinstance(media_id, str) or not media_id:
        raise ProtocolError("upload reply carried no media id")
    return media_id


def read_upscaled_image(payload: Any) -> str:
    encoded = payload[1] if isinstance(payload, list) and len(payload) > 1 else None
    if not isinstance(encoded, str) or len(encoded) < 100:
        raise ProtocolError("upscale reply carried no encoded image")
    return encoded


def read_created_project(payload: Any) -> tuple[str, Optional[str]]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], str):
        raise ProtocolError("create-project reply carried no project id")
    title = None
    if len(payload) > 1 and isinstance(payload[1], list) and payload[1]:
        title = payload[1][0] if isinstance(payload[1][0], str) else None
    return payload[0], title


def read_operation_error(record: list) -> Optional[str]:
    """A complaint like "Media not found." is diagnostic only: jobs that carry
    it have been measured to still finish."""
    detail = record[5] if len(record) > 5 else None
    if not isinstance(detail, list) or len(detail) <= 8:
        return None
    block = detail[8]
    if not isinstance(block, list) or not block or block[0] != OUTCOME_COMPLAINT:
        return None
    for text in walk_strings(block):
        return text
    return "operation reported a problem without a message"


def read_operation(payload: Any) -> Operation:
    """``[null, 50, [[opId, projectId, sceneId, status, ...]]]``."""
    records = payload[2] if isinstance(payload, list) and len(payload) > 2 else None
    record = records[0] if isinstance(records, list) and records else None
    if not isinstance(record, list) or not record or not isinstance(record[0], str):
        raise ProtocolError("operation reply carried no record")
    return Operation(
        operation_id=record[0],
        project_id=record[1] if len(record) > 1 and isinstance(record[1], str) else None,
        status=record[3] if len(record) > 3 and isinstance(record[3], str) else None,
        error=read_operation_error(record),
    )


def read_video_submit(payload: Any) -> Operation:
    """Video submits answer in one of two shapes.

    * ``YhhmEf`` (text -> video): ``[.., .., .., [[mediaId, projectId, workflowId, status]]]``
    * the frame/reference RPCs: the operation shape of :func:`read_operation`.
    """
    records = payload[3] if isinstance(payload, list) and len(payload) > 3 else None
    record = records[0] if isinstance(records, list) and records else None
    if isinstance(record, list) and record and isinstance(record[0], str):
        workflow = record[2] if len(record) > 2 and isinstance(record[2], str) else record[0]
        return Operation(
            operation_id=workflow,
            project_id=record[1] if len(record) > 1 and isinstance(record[1], str) else None,
            status=record[3] if len(record) > 3 and isinstance(record[3], str) else None,
            media_id=record[0],
        )
    return read_operation(payload)


_UUID_PAT = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
#: A listing entry's media slot: ``<isVideo>,null,"<mediaId>"`` (escaped or not).
_MEDIA_SLOT = re.compile(r'(?:null|true|false),\s*null,\s*\\?"(' + _UUID_PAT + r')\\?"')
#: ``"<mediaId>","<opId>"``: the operation id sits right after the media id.
_MEDIA_BEFORE_OP = re.compile(r'\\?"(' + _UUID_PAT + r')\\?",\s*\\?"$')


def find_media_id_in_text(text: str, operation_id: str) -> Optional[str]:
    """Find an operation's media id in a raw project listing (or a window of it).

    Entries look like
    ``[assetId, folderId, null, [title, [sec, nanos], isVideo, null, mediaId, opId, done], projectId]``,
    so the id we hold is either the entry's asset id (media slot follows it) or
    its operation id (media id immediately precedes it).
    """
    lower = text.lower()
    op = operation_id.lower()
    start = lower.find(op)
    while start != -1:
        before = _MEDIA_BEFORE_OP.search(text, max(0, start - 60), start)
        if before:
            return before.group(1)
        match = _MEDIA_SLOT.search(text, start, min(len(text), start + 800))
        if match and match.group(1).lower() != op:
            return match.group(1)
        start = lower.find(op, start + len(op))
    return None


def read_media_urls(payload: Any, media_id: str) -> MediaUrls:
    video = image = None
    for text in walk_strings(payload):
        if not text.startswith("https://"):
            continue
        if MEDIA_HOST + "/video/" in text and video is None:
            video = text
        elif MEDIA_HOST + "/image/" in text and image is None:
            image = text
    return MediaUrls(media_id, video, image)


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def read_project_media(payload: Any, limit: int = 50) -> list[dict]:
    """Best-effort listing reader: every entry that carries a media id."""
    items: list[dict] = []
    seen: set[str] = set()
    for node in walk_lists(payload):
        if len(node) < 4 or not isinstance(node[3], list):
            continue
        detail = node[3]
        if len(detail) <= 4 or not isinstance(detail[4], str) or not _UUID_RE.match(detail[4]):
            continue
        media_id = detail[4]
        if media_id in seen:
            continue
        seen.add(media_id)
        created = None
        if len(detail) > 1 and isinstance(detail[1], list) and detail[1] and isinstance(detail[1][0], (int, float)):
            created = int(detail[1][0])
        items.append({
            "media_id": media_id,
            "title": detail[0] if isinstance(detail[0], str) else None,
            "kind": "video" if len(detail) > 2 and detail[2] else "image",
            "created_unix": created,
            "operation_id": detail[5] if len(detail) > 5 and isinstance(detail[5], str) else None,
        })
        if len(items) >= limit:
            break
    items.sort(key=lambda x: x.get("created_unix") or 0, reverse=True)
    return items
