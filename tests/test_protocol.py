import json

import pytest

from google_flow_mcp import protocol as p

PID = "11111111-2222-3333-4444-555555555555"
MID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def inner(envelope: str):
    outer = json.loads(envelope)
    rpcid, payload, _, generic = outer[0][0]
    assert generic == "generic"
    return rpcid, json.loads(payload)


def test_envelope_roundtrip():
    env = p.build_envelope("abc123", [1, "x", None])
    assert inner(env) == ("abc123", [1, "x", None])


def test_parse_envelope_with_sentinel_and_error_slot():
    payload = json.dumps([None, "ok"])
    text = ")]}'\n\n123\n" + json.dumps([["wrb.fr", "ogiZ0b", payload, None, None, None, "generic"],
                                       ["wrb.fr", "jwpduf", None, None, None, [5], "generic"]])
    results = p.parse_envelope(text)
    assert results[0].ok and results[0].data == [None, "ok"]
    assert not results[1].ok and results[1].error == [5]
    assert p.first_payload(text, "ogiZ0b") == [None, "ok"]
    with pytest.raises(p.RpcError):
        p.first_payload(text, "jwpduf")
    with pytest.raises(p.ProtocolError):
        p.first_payload(text, "nope00")


def test_image_request_shape():
    env = p.image_request("a cat", PID, count=2, aspect="9:16", model="nano_banana_pro", seed=10,
                          reference_media_ids=[MID])
    rpcid, body = inner(env)
    assert rpcid == p.RPC_GEN_IMAGE
    items = body[1]
    assert len(items) == 2
    first = items[0]
    assert first[2] == [[MID, None, None, None, p.REF_TYPE_IMAGE]]
    assert first[3] == 10 and items[1][3] == 10 + 9973
    assert first[4] == 2  # 9:16
    assert first[5] == "GEM_PIX_2"
    assert first[7][5] == PID and first[7][10] == [p.CAPTCHA_SLOT, 1]
    assert first[8] == [[["a cat"]]]
    assert p.needs_captcha(env)


def test_image_edit_puts_base_first_and_sets_asset():
    _, body = inner(p.image_request("make it blue", PID, base_media_id=MID, reference_media_ids=[MID]))
    item = body[1][0]
    assert item[2] == [[MID, None, None, None, p.BASE_TYPE_IMAGE]]
    assert item[7][4] == MID


def test_prompts_override_count():
    _, body = inner(p.image_request("", PID, prompts=["a", "b", "c"]))
    assert [i[8][0][0][0] for i in body[1]] == ["a", "b", "c"]


@pytest.mark.parametrize("bad", [0, 5, True])
def test_image_count_bounds(bad):
    with pytest.raises(ValueError):
        p.image_request("x", PID, count=bad)


def test_model_resolution():
    assert p.resolve_image_model(None) == "GEM_PIX_2"
    assert p.resolve_image_model("nano banana 2") == "BELUGA"
    assert p.resolve_image_model("SOME_NEW_MODEL") == "SOME_NEW_MODEL"
    with pytest.raises(ValueError):
        p.resolve_image_model("bad model!")


def test_video_text_omni():
    spec = p.video_request("waves", PID, mode="text", duration_s=6, aspect="9:16")
    rpcid, body = inner(spec.envelope)
    assert rpcid == p.RPC_GEN_VIDEO_TEXT == spec.rpcid
    req = body[0][0]
    assert req[1] == "abra_t2v_6s" and req[2] == 1
    assert body[1][5] == PID and body[2][1] == 2
    assert spec.estimated_credits == p.DEFAULT_VIDEO_COST["omni"][6]


def test_video_text_360p_adds_low_res_slot():
    spec = p.video_request("waves", PID, mode="text", resolution="360p")
    req = inner(spec.envelope)[1][0][0]
    assert req[1] == "abra_t2v_8s_360p" and req[-1] == [4]


def test_video_frame_veo_and_omni():
    veo = p.video_request("go", PID, mode="frame", family="veo", start_media_id=MID)
    req = inner(veo.envelope)[1][0][0]
    assert veo.rpcid == p.RPC_GEN_VIDEO_FRAME and req[1] == "veo_3_1_i2v_lite"
    assert req[4] == [None, MID, None, None, None, p.FULL_FRAME_CROP]
    omni = p.video_request("go", PID, mode="frame", start_media_id=MID, duration_s=10)
    assert inner(omni.envelope)[1][0][0][1] == "abra_i2v_10s"


def test_video_first_last_and_references():
    fl = p.video_request("go", PID, mode="first_last", start_media_id=MID, end_media_id=PID)
    req = inner(fl.envelope)[1][0][0]
    assert fl.rpcid == p.RPC_GEN_VIDEO_FIRST_LAST and req[1] == "omni_flash_i2v_8s_first_last"
    assert req[4][1] == MID and req[5][1] == PID
    refs = p.video_request("go", PID, mode="references", reference_media_ids=[MID, PID])
    req = inner(refs.envelope)[1][0][0]
    assert refs.rpcid == p.RPC_GEN_VIDEO_REFERENCES and req[1] == [[None, MID], [None, PID]]


@pytest.mark.parametrize("kwargs", [
    {"mode": "text", "family": "veo"},
    {"mode": "frame"},
    {"mode": "first_last", "start_media_id": MID},
    {"mode": "references"},
    {"mode": "text", "duration_s": 5},
    {"mode": "text", "aspect": "1:1"},
    {"mode": "bogus"},
])
def test_video_validation(kwargs):
    with pytest.raises(ValueError):
        p.video_request("x", PID, **kwargs)


def test_readers():
    url = f"https://{p.MEDIA_HOST}/image/{MID}?sig=1"
    images = p.read_images([[None, [url, url]], "noise"])
    assert [(i.media_id, i.url) for i in images] == [(MID, url)]
    op = p.read_operation([None, 50, [["op-1", PID, "scene", "CAE"]]])
    assert op.done and op.project_id == PID
    sub = p.read_video_submit([None, None, None, [[MID, PID, "wf-1", "PENDING"]]])
    assert sub.media_id == MID and sub.operation_id == "wf-1"
    urls = p.read_media_urls([f"https://{p.MEDIA_HOST}/video/{MID}", url], MID)
    assert urls.video and urls.image == url
    assert p.read_created_project([PID, ["My project"]]) == (PID, "My project")


def test_find_media_id_in_listing_window():
    entry = json.dumps([["asset", "folder", None, ["title", [1, 2], True, None, MID, "op-XYZ", True], PID],
                        ["asset2", "f", None, ["t", [1, 2], None, None, PID, "op-2", True], PID]],
                       separators=(",", ":"))
    escaped = json.dumps(entry)  # the listing usually arrives JSON-escaped
    assert p.find_media_id_in_text(entry, "op-XYZ") == MID
    assert p.find_media_id_in_text(escaped, "op-xyz") == MID
    assert p.find_media_id_in_text(entry, "missing") is None
    assert p.find_media_id_in_text(entry, "asset2") == PID  # matched by asset id


def test_read_project_media():
    entries = [["a", "f", None, ["t1", [100, 0], True, None, MID, "op1", True], PID],
               ["b", "f", None, ["t2", [200, 0], False, None, PID, "op2", True], PID]]
    items = p.read_project_media([entries])
    assert [i["media_id"] for i in items] == [PID, MID]
    assert items[1]["kind"] == "video"


def test_apply_captcha():
    env = p.image_request("x", PID)
    filled = p.apply_captcha(env, "TOKEN")
    assert p.CAPTCHA_SLOT not in filled and "TOKEN" in filled
