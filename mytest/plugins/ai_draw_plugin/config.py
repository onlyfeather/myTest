from .._shared.services import gpt_image, novelai
from .._shared.settings import SettingField, SettingSection, register

PROVIDERS = (("novelai", "NovelAI"), ("gpt", "GPT Image"))

draw_settings = register(
    SettingSection(
        id="ai_draw",
        title="AI 画图",
        description="指令：/画图 描述（默认后端）、/nai 描述、/gpt画图 描述。描述前加「横图」「方图」可以改尺寸，默认竖图。",
        order=30,
        fields=[
            SettingField("enabled", "启用", type="bool", default=True),
            SettingField(
                "default_provider", "/画图 使用", type="select", default="novelai", options=PROVIDERS
            ),
            SettingField(
                "cooldown",
                "每人冷却（秒）",
                type="int",
                default=30,
                minimum=0,
                maximum=3600,
                help="管理员（SUPERUSERS）不受限制",
            ),
            SettingField(
                "max_concurrent", "同时进行的任务数", type="int", default=2, minimum=1, maximum=10
            ),
            SettingField(
                "allowed_groups",
                "允许的群",
                type="lines",
                default=[],
                help="每行一个群号，留空表示所有群都能用",
            ),
            SettingField("allow_private", "允许私聊使用", type="bool", default=True),
            SettingField(
                "proxy",
                "代理",
                default="",
                placeholder="http://mihomo:7890",
                help="NovelAI 和 GPT Image 共用；留空表示直连",
            ),
        ],
    )
)

novelai_settings = register(
    SettingSection(
        id="novelai",
        title="NovelAI",
        description="二次元插画。中文描述会先用「大模型」翻译成英文 tag。",
        order=31,
        fields=[
            SettingField("token", "Persistent API Token", type="secret", env=("novelai_token",),
                         help="NovelAI 网页 → 设置 → Account → Get Persistent API Token"),
            SettingField("base_url", "画图接口地址", default="https://image.novelai.net",
                         help="使用中转站时改成中转地址"),
            SettingField("account_url", "账户接口地址", default="https://api.novelai.net",
                         help="只用于「测试连接」查询点数"),
            SettingField("model", "模型", type="select", default="nai-diffusion-4-5-full",
                         options=novelai.MODELS,
                         help="V5 节约模式固定 14 步和 Euler Ancestral，忽略下面的负面提示词（使用官方预设）"),
            SettingField("sampler", "采样器", type="select", default="k_euler_ancestral",
                         options=novelai.SAMPLERS),
            SettingField("steps", "步数", type="int", default=28, minimum=1, maximum=50,
                         help="Opus 订阅在 28 步以内、默认尺寸下不消耗 Anlas；V5 节约模式固定 14 步"),
            SettingField("scale", "提示词引导强度", type="float", default=5.0, minimum=0, maximum=10),
            SettingField(
                "positive_prefix",
                "正面质量词",
                type="textarea",
                default="best quality, amazing quality, very aesthetic, absurdres",
                help="自动加在描述前面",
            ),
            SettingField(
                "negative",
                "负面提示词",
                type="textarea",
                default=(
                    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
                    "fewer digits, cropped, worst quality, low quality, jpeg artifacts, signature, "
                    "watermark, username, blurry"
                ),
            ),
            SettingField("translate", "中文自动翻译成 tag", type="bool", default=True),
            SettingField("timeout", "超时（秒）", type="float", default=120.0, minimum=10, maximum=600),
        ],
    )
)

gpt_image_settings = register(
    SettingSection(
        id="gpt_image",
        title="GPT Image",
        description="写实、文字排版、复杂构图更擅长，直接理解中文。",
        order=32,
        fields=[
            SettingField("api_key", "API Key", type="secret", env=("gpt_image_api_key",)),
            SettingField("base_url", "接口地址", default="https://api.openai.com/v1",
                         help="填到 /v1 为止；官方接口需要先完成组织验证"),
            SettingField("model", "模型", default="gpt-image-2.5-flare", fetchable=True,
                         help="点「获取列表」读取接口上的图片模型；官方可选 "
                         + "、".join(m for m, _ in gpt_image.MODELS)),
            SettingField("quality", "质量", type="select", default="auto", options=gpt_image.QUALITIES),
            SettingField(
                "moderation",
                "审核强度",
                type="select",
                default="low",
                options=(("low", "宽松"), ("auto", "默认"), ("", "不传（中转站不支持时选这个）")),
            ),
            SettingField("timeout", "超时（秒）", type="float", default=300.0, minimum=10, maximum=900),
        ],
    )
)
