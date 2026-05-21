import nonebot
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

nonebot.init(log_level="INFO")

from mytest.plugins.pixiv_bot_plugin.renderers.image_message import extract_image_urls


def assert_equal(actual, expected):
    assert actual == expected, f"expected {expected!r}, got {actual!r}"


def test_extract_image_urls():
    assert_equal(extract_image_urls({"urls": ["a", "b"]}), ["a", "b"])
    assert_equal(extract_image_urls({"urls": "a"}), ["a"])
    assert_equal(extract_image_urls({"urls": [123, "", None, "b"]}), ["123", "b"])
    assert_equal(extract_image_urls({"url": ["a"]}), ["a"])
    assert_equal(extract_image_urls({"url": 456}), ["456"])
    assert_equal(extract_image_urls({"originalUrl": "o"}), ["o"])
    assert_equal(
        extract_image_urls({"urls": {"regular": "r", "original": "o"}}),
        ["o", "r"],
    )
    assert_equal(
        extract_image_urls({"urls": {"thumb": "t", "custom": ["c1", "c2"]}}),
        ["t", "c1", "c2"],
    )
    assert_equal(extract_image_urls({}), [])
    assert_equal(extract_image_urls(None), [])


if __name__ == "__main__":
    test_extract_image_urls()
    print("renderer image_message tests passed")
