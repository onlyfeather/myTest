"""音乐卡片：OneBot 的 music 自定义消息段，由 NapCat 去签名（DESIGN.md 第 11 节）。

实测（2026-10-29）NapCat 默认签名服务只接受自定义格式（url、audio、title、image、singer），只给 ID 会报「缺少 title」。
type 填 qq / 163 / kugou 时，卡片角标是 QQ音乐 / 网易云音乐 / 酷狗音乐。
audio 是 QQ 里点播放时直接放的地址，取发卡片那一刻的直链：网易云用官方外链（长期有效），
QQ 音乐约 2 小时、酷狗至少 40 分钟有效，会员歌用 bot 的会员账号取（用户确认过会员歌卡片可以完整播放）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .sources.base import Track

CARD_TYPE = {"qq": "qq", "netease": "163", "kugou": "kugou"}


def jump_url(track: Track) -> str:
    """点卡片打开的歌曲页。"""
    if track.platform == "qq":
        return f"https://y.qq.com/n/ryqq/songDetail/{track.id}"
    if track.platform == "netease":
        return f"https://music.163.com/#/song?id={track.id}"
    return f"https://www.kugou.com/song/#hash={track.id}"


def netease_outer(track: Track) -> str:
    return f"https://music.163.com/song/media/outer/url?id={track.id}.mp3"


@dataclass(frozen=True)
class Card:
    type: str
    url: str
    audio: str
    title: str
    singer: str
    image: str

    def segment_data(self) -> dict[str, str]:
        data = {"type": self.type, "url": self.url, "audio": self.audio, "title": self.title[:60],
                "content": self.singer[:60], "image": self.image}
        return {k: v for k, v in data.items() if v}

    def fallback_text(self) -> str:
        """签名服务挂了时退回的普通消息。"""
        return f"{self.title} - {self.singer}\n{self.url}" if self.singer else f"{self.title}\n{self.url}"


def build_card(track: Track, audio: str) -> Card:
    # 没有 audio 时签名服务返回图文卡片（com.tencent.tuwen.lua）：有标题、歌手、封面和平台角标，点开跳到歌曲页，
    # 只是没有播放按钮。会员歌在没配会员账号时就是这样。
    # 没有封面时 NapCat 拒绝自定义卡片（image 必填），用平台图标兜底
    image = track.cover or {
        "qq": "https://y.gtimg.cn/mediastyle/yqq/img/album_300.png",
        "netease": "https://p1.music.126.net/6y-UleORITEDbvrOLV0Q8A==/5639395138885805.jpg",
        "kugou": "https://www.kugou.com/yy/static/images/play/default.jpg",
    }[track.platform]
    return Card(CARD_TYPE[track.platform], jump_url(track), audio, track.title, track.artists, image)
