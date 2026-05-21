# Pixiv Bot Plugin Modules

当前仍然保持一个 NoneBot 插件，但已经建立模块边界，方便继续拆分。

## Boundary

- `pixiv_bot.py`: 兼容入口，只保留插件元数据和 matcher 转导。
- `common/`: 常量、命令名、事件文本提取、错误提示。
- `parser/`: 搜图、最新、美图、热门等命令文本解析。
- `pixiv/`: Pixiv 访问入口，当前包装旧 `PixivSpider`。
- `storage/`: 存储命名空间，当前包装旧 `PixivStorage`。
- `renderers/`: 图片 URL 提取、图片下载和 UniMessage 图片发送。
- `handlers/`: matcher 导出命名空间；`admin.py` 已承接登录、登录状态、质量设置等私聊 SUPERUSER 管理命令；`core.py` 已承接搜图、最新、美图、热门、每日一图等核心推荐命令；`author.py` 已承接作者作品、作者信息、关注、取关、关注列表等作者追踪命令；`preference.py` 已承接偏好和作者圈名管理命令；`stats.py` 已承接统计和热门标签；`info.py` 已承接图片信息；`help.py` 已承接帮助。
- `services/`: 业务编排层；`core.py` 承接搜图、最新、美图、热门、每日一图的策略和回退逻辑；`author.py` 承接作者作品解析、圈名解析后的作者查询和关注列表文案；`preference.py`、`stats.py`、`info.py` 分别承接偏好/圈名、统计、作品信息文案；`image_selection.py` 统一候选图筛选、质量/收藏过滤和原图 URL 补全。

## Compatibility

旧模块继续可用：

- `pixiv_bot.py` 仍转导所有 matcher，兼容旧导入路径。
- `spiderPixiv.py` 仍提供 `PixivSpider`。
- `pixiv_storage.py` 仍提供 `PixivStorage`。

第一阶段只迁移低风险公共模块，并让业务代码调用新的
`renderers/image_message.py` 图片发送实现。第二阶段已迁出所有 matcher handler。
第三阶段已把主链路上的纯业务编排拆到 `services/`：handler 只负责命令入口和消息发送，
`services/` 负责搜索/推荐/作者作品的业务策略，`spiderPixiv.py` 继续保留为 Pixiv API
客户端与本地状态/存储桥。旧的 spider 高层方法暂时保留兼容，主流程已不再依赖它们。
