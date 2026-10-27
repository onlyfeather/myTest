"""参考图、画师串、引导参数。运行：python -m pytest tests -q"""

from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest
from PIL import Image

from mytest.plugins._shared import qq_images
from mytest.plugins._shared.services import gpt_image, images, novelai
from mytest.plugins._shared.services.http import ServiceError
from mytest.plugins.ai_draw_plugin.artists import ArtistStore, clean, compose, is_clear_word
from mytest.plugins.ai_draw_plugin.prompting import parse_request


def png(width: int, height: int, color=(255, 0, 0), mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), color).save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------- 指令参数


def test_plain_command_has_no_flags():
    req = parse_request("横图 海边的白发少女")
    assert (req.size, req.prompt, req.flags) == ("横图", "海边的白发少女", frozenset())
    assert not req.wants_reference and not req.wants_artist


def test_flags_anywhere_and_removed_from_prompt():
    req = parse_request("-参考 横图 海边 -画师 -画风")
    assert req.wants_reference and req.wants_artist and req.with_style
    assert (req.size, req.prompt) == ("横图", "海边")
    req = parse_request("横图 海边 －参考图")
    assert req.wants_reference and req.prompt == "海边", "full-width dash and alias"
    assert parse_request("-ref -ARTIST 1girl").flags == frozenset({"参考", "画师"})


def test_hyphens_inside_prompt_are_not_flags():
    req = parse_request("x-ray glasses, 1-2 girls, 参考书")
    assert req.flags == frozenset() and req.prompt == "x-ray glasses, 1-2 girls, 参考书"
    assert parse_request("海边-参考").flags == frozenset(), "flag must be a separate word"


# ---------------------------------------------------------------- 画师串


def test_artist_store(tmp_path):
    store = ArtistStore(tmp_path / "a.json")
    assert store.get("1") == ""
    assert store.set("1", " artist:wlop，{artist:ask}，\n 0.8::artist:x::, ") == "artist:wlop, {artist:ask}, 0.8::artist:x::"
    assert ArtistStore(tmp_path / "a.json").get("1") == "artist:wlop, {artist:ask}, 0.8::artist:x::", "persisted"
    assert store.get("2") == ""
    with pytest.raises(ValueError):
        store.set("1", " , ，")
    with pytest.raises(ValueError):
        store.set("1", "a" * 2000)
    assert store.clear("1") is True and store.clear("1") is False and store.get("1") == ""
    assert is_clear_word("默认") and is_clear_word(" Reset ") and not is_clear_word("artist:默认")


def test_compose_order_and_empty_parts():
    assert compose("best quality", "artist:wlop", "1girl") == "best quality, artist:wlop, 1girl"
    assert compose("best quality,", "", "1girl") == "best quality, 1girl"
    assert compose("", "artist:wlop", "1girl") == "artist:wlop, 1girl"
    assert clean("a，b\nc") == "a, b, c"


# ---------------------------------------------------------------- 图片预处理


def test_letterbox_picks_closest_canvas_and_pads_black():
    data, size = images.letterbox(png(400, 600), novelai.REFERENCE_CANVASES)
    assert size == (1024, 1536)
    img = Image.open(io.BytesIO(data))
    assert img.size == (1024, 1536) and img.mode == "RGB"

    data, size = images.letterbox(png(1000, 1000), novelai.REFERENCE_CANVASES)
    assert size == (1472, 1472)

    data, size = images.letterbox(png(1600, 400), novelai.REFERENCE_CANVASES)
    img = Image.open(io.BytesIO(data))
    assert size == (1536, 1024)
    assert img.getpixel((10, 10)) == (0, 0, 0), "padding is black"
    assert img.getpixel((768, 512)) == (255, 0, 0), "content stays centered"


def test_letterbox_transparent_and_bad_input():
    data, _ = images.letterbox(png(500, 500, (0, 255, 0, 0), mode="RGBA"), novelai.REFERENCE_CANVASES)
    assert Image.open(io.BytesIO(data)).mode == "RGB"
    with pytest.raises(ServiceError, match="无法识别"):
        images.letterbox(b"not an image", novelai.REFERENCE_CANVASES)


def test_normalize_shrinks_large_images():
    data, mime = images.normalize(png(4000, 1000), max_side=2048)
    assert mime == "image/png" and Image.open(io.BytesIO(data)).size == (2048, 512)
    data, _ = images.normalize(png(300, 200))
    assert Image.open(io.BytesIO(data)).size == (300, 200)


# ---------------------------------------------------------------- NovelAI 角色参考


def _req(model: str, ref: novelai.CharacterReference | None) -> novelai.NovelAIRequest:
    return novelai.NovelAIRequest("1girl", "lowres", model, 832, 1216, 28, 5.0, "k_euler_ancestral", 1, ref)


def test_character_reference_payload():
    ref = novelai.CharacterReference(image=b"PNGDATA", with_style=False, strength=0.8, fidelity=0.75)
    p = novelai.build_payload(_req("nai-diffusion-4-5-full", ref))["parameters"]
    assert p["director_reference_images"] == ["UE5HREFUQQ=="]
    assert p["director_reference_descriptions"][0]["caption"] == {"base_caption": "character", "char_captions": []}
    assert p["director_reference_strength_values"] == [0.8]
    assert p["director_reference_secondary_strength_values"] == [0.25], "fidelity is sent as 1 - fidelity"
    assert p["director_reference_information_extracted"] == [1]
    assert "reference_image_multiple" not in p, "vibe transfer is not used"

    styled = novelai.build_payload(_req("nai-diffusion-4-5-curated", novelai.CharacterReference(b"x", with_style=True)))
    assert styled["parameters"]["director_reference_descriptions"][0]["caption"]["base_caption"] == "character&style"

    assert "director_reference_images" not in novelai.build_payload(_req("nai-diffusion-4-5-full", None))["parameters"]


@pytest.mark.parametrize("model", ["nai-diffusion-5-full", "nai-diffusion-5-full-medium", "nai-diffusion-3"])
def test_character_reference_rejected_on_unsupported_models(model):
    with pytest.raises(ServiceError, match="V4.5"):
        novelai.build_payload(_req(model, novelai.CharacterReference(b"x")))


# ---------------------------------------------------------------- GPT Image 参考图


def test_gpt_image_uses_edits_with_references(monkeypatch):
    from contextlib import asynccontextmanager

    calls: list[httpx.Request] = []

    @asynccontextmanager
    async def fake_client(proxy="", timeout=60.0):
        def handler(request):
            calls.append(request)
            import base64

            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"OUT").decode()}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield client

    monkeypatch.setattr(gpt_image, "http_client", fake_client)
    kwargs = dict(api_key="sk", base_url="https://api.openai.com/v1", model="gpt-image-2.5-flare", size="1024x1536")

    assert asyncio.run(gpt_image.generate("猫", **kwargs)) == b"OUT"
    assert calls[-1].url.path == "/v1/images/generations"
    assert json.loads(calls[-1].content)["prompt"] == "猫"

    assert asyncio.run(gpt_image.generate("让她穿上和服", references=[b"IMG1", b"IMG2"], **kwargs)) == b"OUT"
    req = calls[-1]
    assert req.url.path == "/v1/images/edits"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert body.count(b'name="image[]"') == 2 and b"IMG1" in body and b"IMG2" in body
    assert b'name="prompt"' in body and "让她穿上和服".encode() in body
    assert b'name="model"' in body and b"gpt-image-2.5-flare" in body

    with pytest.raises(ServiceError, match="最多"):
        asyncio.run(gpt_image.generate("x", references=[b"x"] * 17, **kwargs))


# ---------------------------------------------------------------- QQ 图片


def test_qq_image_urls_from_message_and_reply():
    class Seg:
        def __init__(self, type_, data):
            self.type, self.data = type_, data

    class Reply:
        message = [Seg("image", {"url": "https://multimedia.nt.qq.com.cn/download?a=1"})]

    class Ev:
        message = [Seg("text", {"text": "/nai 猫"}), Seg("image", {"file": "abc.jpg", "url": "https://gchat.qpic.cn/x"})]
        reply = Reply()

    assert qq_images.event_image_urls(Ev()) == [
        "https://gchat.qpic.cn/x", "https://multimedia.nt.qq.com.cn/download?a=1",
    ]
    assert qq_images.image_urls([{"type": "image", "data": {"file": "local.jpg"}}]) == []


@pytest.mark.parametrize("url,ok", [
    ("https://gchat.qpic.cn/gchatpic_new/1/2", True),
    ("https://multimedia.nt.qq.com.cn/download?x", True),
    ("http://c2cpicdw.qpic.cn/offpic_new/1", True),
    ("http://127.0.0.1:6099/secret", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("https://qpic.cn.evil.com/x", False),
    ("https://evilqq.com/x", False),
    ("file:///etc/passwd", False),
])
def test_qq_image_host_allowlist(url, ok):
    assert qq_images.host_allowed(url) is ok


def test_download_rejects_disallowed_host():
    with pytest.raises(ServiceError, match="QQ"):
        asyncio.run(qq_images.download("http://127.0.0.1/x"))
