# 跑团群静默（MaiBot 插件）

凛音在跑团群里当 KP 时，所有逻辑都在 NoneBot 的跑团插件里。这个插件让 MaiBot 在指定的群里不处理任何消息，免得两边同时回复。

实现：订阅 `chat.receive.before_process`（消息进入 MaiBot 之前），用阻塞处理器返回 `abort`。
自己出错时放行，不会把其他群的消息吞掉。

## 安装

```bash
cp -r ~/myTest/maibot_plugins/rinne_trpg_guard ~/maibot/plugins/
```

重启 MaiBot，在插件配置里填「跑团群的群号」。

## 第一次部署：先只记录

默认是 `observe`（只记录不拦截）。在跑团群里随便发几句：

1. MaiBot 日志里应出现 `[跑团群静默] 群 xxx 的消息（只记录，没有拦截）`；
2. 插件数据目录下的 `guard.jsonl` 里有对应记录，`text` 是你发的内容；
3. 其他群的消息不出现在记录里。

三条都对，再把模式改成 `block`，重新在跑团群发几句，确认 MaiBot 不再回复，日志里显示「已拦截」。
其他群照常聊天，确认没有被误拦。

如果第 1 条没有出现，说明你的 MaiBot 版本里没有调用这个 Hook，把日志发给我。

## 更新

只复制代码，不要覆盖 `config.toml`：

```bash
cp ~/myTest/maibot_plugins/rinne_trpg_guard/{plugin.py,_manifest.json} ~/maibot/plugins/rinne_trpg_guard/
```
