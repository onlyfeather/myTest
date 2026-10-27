from .._shared.services import fish_audio
from .._shared.settings import SettingField, SettingSection, register

tts_settings = register(
    SettingSection(
        id="tts",
        title="语音合成",
        description="Fish Audio。指令：/说 文字。骰娘设置里打开「同时发语音」后，投骰点评也会发语音。",
        order=40,
        fields=[
            SettingField("enabled", "启用", type="bool", default=True),
            SettingField("api_key", "API Key", type="secret", env=("fish_audio_api_key",),
                         help="fish.audio → API Keys"),
            SettingField("base_url", "接口地址", default="https://api.fish.audio",
                         help="不带 /v1"),
            SettingField("model", "模型", type="select", default="s2.1-pro", options=fish_audio.MODELS,
                         help="Drama 3 可以在文字里用半角方括号写演绎指导，例如 /说 [冷淡又有点不耐烦] 请您自重"),
            SettingField(
                "reference_id",
                "音色",
                default="",
                fetchable=True,
                help="点「获取列表」选择自己的音色或热门中文音色；也可以粘贴音色页地址最后那串 ID。留空使用默认音色",
            ),
            SettingField("speed", "语速", type="float", default=1.0, minimum=0.5, maximum=2.0),
            SettingField("volume", "音量增益（dB）", type="float", default=0.0, minimum=-20, maximum=20),
            SettingField("format", "音频格式", type="select", default="mp3", options=fish_audio.FORMATS),
            SettingField("max_chars", "单次最多字数", type="int", default=200, minimum=10, maximum=2000,
                         help="按 UTF-8 字节计费，限制长度可以防止被刷"),
            SettingField("cooldown", "每人冷却（秒）", type="int", default=10, minimum=0, maximum=3600,
                         help="管理员不受限制"),
            SettingField("allowed_groups", "允许的群", type="lines", default=[],
                         help="每行一个群号，留空表示所有群都能用"),
            SettingField("allow_private", "允许私聊使用", type="bool", default=True),
            SettingField("proxy", "代理", default="", placeholder="http://mihomo:7890",
                         help="留空表示直连"),
            SettingField("timeout", "超时（秒）", type="float", default=120.0, minimum=5, maximum=600),
        ],
    )
)
