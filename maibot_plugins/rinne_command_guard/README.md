# 不接指令（MaiBot 插件）

/画图、.rd、搜图 这类指令是发给 NoneBot 的，MaiBot 也会收到，凛音就会对着指令接话。
这个插件让 MaiBot 不处理这些消息，也不把它们记进聊天记录。

实现：订阅 `chat.receive.before_process`（消息进入 MaiBot 之前），用阻塞处理器返回 `abort`。
自己出错时放行，不会把正常聊天吞掉。

## 哪些消息不处理

只看用户打的字，消息开头的「回复」和 @ 会跳过。

- 以 `/` 开头：/画图、/nai、/点歌、/说、/帮助、/p站 每日一图、/dice 投骰、跑团指令……
- 以 `.` 或 `。` 加字母开头：.rd、.ra、.sc、.st（「。。。」不算）
- Pixiv 不带 / 的指令：搜图、最新、美图、热门、每日一图，前面可以加「p站」
- **指令后续**：发了指令的人，在同一个群（或私聊）里 120 秒内的消息也不处理。
  这样 /画图 -多图 时发的图、「好了」，/点歌 回的序号，-画师 发的画师串，凛音都不会接话。
  代价是这个人在这 120 秒里正常聊天，凛音也不接；时长在插件配置里改，填 0 就关掉。

MaiBot 自己的指令照常处理：/好感度、/设置好感度、/clear（在「MaiBot 自己的指令」里，可以加）。

以后 NoneBot 加了不以 / 或 . 开头的新指令，在插件配置的「哪些消息算指令」里加一条正则。

## 安装

```bash
cp -r ~/myTest/maibot_plugins/rinne_command_guard ~/maibot/plugins/
```

重启 MaiBot。

## 第一次部署：先只记录

默认是 `observe`（只记录不拦截）。发几条指令，再正常聊几句：

1. MaiBot 日志里应出现 `[不接指令] 指令：/画图 …（只记录，没有拦截）`；
2. 插件数据目录下的 `skipped.jsonl` 里有对应记录，`reason` 是「指令」或「指令后续」；
3. 正常聊天的话不出现在记录里（指令后 120 秒内同一个人说的除外）。

三条都对，再把模式改成 `block`，确认凛音不再对指令接话，正常聊天照常回复。

如果第 1 条没有出现，说明你的 MaiBot 版本里没有调用这个 Hook，把日志发给我。

## 更新

只复制代码，不要覆盖 `config.toml`：

```bash
cp ~/myTest/maibot_plugins/rinne_command_guard/{plugin.py,_manifest.json} ~/maibot/plugins/rinne_command_guard/
```
