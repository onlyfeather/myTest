# 凛音的工具（MaiBot 插件）

让凛音在聊天中调用 NoneBot 的功能。功能本身由同一台服务器上的 NoneBot 执行
（`mytest/plugins/tools_api_plugin`），这个插件只负责把请求转过去。

| 工具 | 凛音什么时候用 | NoneBot 做什么 |
|---|---|---|
| `rinne_draw` | 对方请她画图 | 画图 → 发图 |
| `rinne_selfie` | 对方想看她的自拍 | 按凛音想的情景 + 人设图画一张 |
| `rinne_voice` | 对方想听她用语音说、让她唱歌 | 合成语音发出（唱歌和大段语音只限 superuser） |
| `rinne_dice` | 跑团检定、随机决定 | 真随机投骰，结果发到群里 |
| `rinne_pixiv_search` / `rinne_pixiv_daily` | 对方想看某类插画 | Pixiv 搜图 / 每日推荐 |

所有工具都是核心工具，凛音每轮都能看到。确认的话由凛音自己回复，NoneBot 不发词库里的确认词。

## 安装

1. NoneBot 那边设置环境变量 `TOOLS_API_TOKEN`（至少 24 位随机字符），重建容器。
2. 复制插件：`cp -r ~/myTest/maibot_plugins/rinne_tools ~/maibot/plugins/`
3. 重启 MaiBot，在插件配置里填 `nonebot_url` 和 `token`。

更新时只复制代码，不要覆盖 `config.toml`：

```bash
cp ~/myTest/maibot_plugins/rinne_tools/{plugin.py,_manifest.json} ~/maibot/plugins/rinne_tools/
```
