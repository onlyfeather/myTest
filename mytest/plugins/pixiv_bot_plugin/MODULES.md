# Pixiv Bot Plugin Modules

## Boundary

- `__init__.py`: 插件入口，声明依赖（alconna、localstore）和 `__plugin_meta__`，导入 `handlers`。
- `common/`: 常量、命令名、事件文本提取、失败原因文案、`report_error`（写完整日志并给用户发简短提示）。
- `parser/`: 搜图、最新、美图、热门等命令文本解析。
- `pixiv/client.py`: 进程内唯一的 `PixivSpider`。首次使用时加载 Cookie、标签池、关注作者和设置，关闭时释放 HTTP 会话。
- `spiderPixiv.py`: Pixiv API 客户端和内存状态（Cookie、标签池、关注、圈名、质量阈值）。失败原因 `last_error_reason` 存在 `ContextVar` 里，按请求隔离。
- `pixiv_storage.py`: SQLite 持久化。
- `renderers/`: 图片 URL 提取；图片用共享 HTTP 客户端下载后以二进制发给 NapCat（不发远程 URL，避免 NapCat 拉图时缺少 Pixiv Referer）。
- `handlers/`: matcher 和消息发送。
- `services/`: 搜索、推荐、作者作品的选图策略和回退逻辑。

## Config (.env)

- `PIXIV_BOT_DATA_DIR`: 数据目录；不设置时使用 localstore 的插件数据目录。启动日志会打印实际路径。
- `PIXIV_BOT_IMAGE_PROXY`: 替换 `i.pximg.net` 的反代域名，默认 `i.isekai-inn.uno`。服务器能直连 Pixiv 时可设为空字符串。
