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
| `rinne_qzone_post` | 有人让她发说说（NoneBot 核对，只限 superuser） | 按人设写一条日常说说 + 画一张配图（自拍或随手拍的东西），返回给插件，再由 Maizone 发到 QQ 空间 |
| `rinne_music_share` | 有人点名让她发某首歌 | 三个平台按顺序搜，发音乐卡片（卡片里点播放就能听） |
| `rinne_music_recommend` | 有人让她推荐歌 | 按对方的要求选歌，并在平台上核对确实存在。本群开着一起听时加进队列，否则发音乐卡片 |
| `rinne_listen_status` | 有人问刚才放了什么、后面还有什么 | 返回本群一起听的队列、刚放过的歌、在听的人 |

## 一起听

需要 NoneBot 开启一起听（`mytest/plugins/listen_plugin`）。

- 回复前，插件会告诉凛音本群正在一起听什么（一两句话，`maisaka.replyer.before_request`）。她只在被问起时才提，不会主动说。
  查询超时 1 秒、结果缓存 10 秒，查不到就不注入，不会拖慢回复。可以在「开放哪些工具」里关掉。
- 推荐歌时，插件会把群里最近 20 条聊天带给 NoneBot，只当气氛参考，NoneBot 不保存。
  选歌的依据按优先级是：对方的要求（硬性条件）> 群里的气氛、本群放过的歌、对方自己的歌单（只是参考）。不推本群最近放过的歌。
- 会员歌：bot 配了会员账号（NoneBot 的 WebUI「一起听」里扫码）的平台能放；没配时会去别的平台找同一首能放的。

## 说说

说说写的是凛音自己的日常和工作（“营业空间”），不读群聊、不提群友。文案在 NoneBot 的 WebUI「凛音的说说」里调整，
写完会检查一遍 AI 腔（总结升华、排比、“加油”、波浪号、emoji 堆砌等），不合格就重写，连续 3 次不合格就放弃不发。

- 需要装 [Maizone](https://github.com/internetsb/Maizone)，发送用它的 `send_feed_api`。Maizone 自己的定时发送（`enable_auto_send`）要关掉。
- 定时发说说在插件配置的「说说」里打开，要填 `superuser_id`（NoneBot 的 superuser QQ 号）。
  每天三个时段：上午 10:00（工作中）、傍晚 18:00（刚下班）、深夜 23:00（睡前），各自前后随机浮动 40 分钟。
  到点后再按 `probability` 判定一次，通过了才让 NoneBot 按这个时段的状态拟一件事、写说说、画配图。
- 配图每条都有：默认 35% 是她随手拍的东西（茶杯、窗外之类，用「AI 画图」的默认后端），其余是穿着合时段的自拍。
  比例在 WebUI「凛音的说说」的「拍东西的比例」里改；画图关掉时全是自拍，没上传人设图时全拍东西。
- WebUI「凛音的说说」里填了城市，写说说时会查实时天气（Open-Meteo，不用 API Key）给她参考；留空不查，查不到也照常发。

所有工具都是核心工具，凛音每轮都能看到。确认的话由凛音自己回复，NoneBot 不发词库里的确认词。

## 安装

1. NoneBot 那边设置环境变量 `TOOLS_API_TOKEN`（至少 24 位随机字符），重建容器。
2. 复制插件：`cp -r ~/myTest/maibot_plugins/rinne_tools ~/maibot/plugins/`
3. 重启 MaiBot，在插件配置里填 `nonebot_url` 和 `token`。

更新时只复制代码，不要覆盖 `config.toml`：

```bash
cp ~/myTest/maibot_plugins/rinne_tools/{plugin.py,_manifest.json} ~/maibot/plugins/rinne_tools/
```
