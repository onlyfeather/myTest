from .._shared.services import gpt_image, novelai
from .._shared import proxy
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
            proxy.mode_field(),
            proxy.custom_field("NovelAI 和 GPT Image 共用。连接方式选「用下面单独填的代理」或「跟以前一样」时生效；留空表示直连"),
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
            SettingField("base_url", "接口地址", default=novelai.OFFICIAL_HOST,
                         help="画图和查询点数都用这个地址；使用中转站时改成中转地址。"
                         "旧地址 api.novelai.net 已不再对第三方开放，会自动改用 image.novelai.net"),
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
                "default_artists",
                "默认画师串",
                type="textarea",
                default="",
                placeholder="artist:wlop, {artist:ask (askzy)}, 0.8::artist:mika pikazo::",
                help="没设置过自己画师串的人都用这个，加在质量词后面；留空表示不加。"
                "群友可以用 /nai -画师 描述 换成自己的（会记住），用 /nai画师 默认 恢复",
            ),
            SettingField("reference_strength", "角色参考强度", type="float", default=1.0, minimum=0, maximum=1,
                         help="越高越贴近参考图；太高时表情和姿势也会跟参考图一样"),
            SettingField("reference_fidelity", "角色参考保真度", type="float", default=1.0, minimum=0, maximum=1,
                         help="越高越难被描述改掉角色特征"),
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
            SettingField("img2img_strength", "改图强度", type="float", default=0.55, minimum=0.01, maximum=0.99,
                         help="/改图 时原图能被改动多少。越高越听描述的、越不像原图；0.4-0.6 一般能保住构图"),
            SettingField("img2img_noise", "改图噪声", type="float", default=0.0, minimum=0, maximum=0.99,
                         help="加一点（0.05-0.2）能让改出来的地方多些新细节，太高画面会变脏"),
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
