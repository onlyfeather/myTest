# Dice Girl Plugin Modules

只负责投骰：投出结果，再由角色（白鸟凛音）用 AI 点评一句。
聊天和角色扮演交给 MaiBot；之前在这里实现的群聊、记忆、群员档案等代码保存在 git 提交 `3be1346` 中。

## Boundary

- `handlers/`: 命令入口。`dice.py` 投骰（`/dice 投骰`、`.rd`），`status.py` 状态和帮助，
  `admin.py` AI 开关和设置好感（SUPERUSER 私聊），`role.py` 切换点评角色。
- `dice/`: 纯骰子核心：指令解析、随机投骰、成功状态判定，不依赖 LLM。
- `llm/`: OpenAI 兼容接口调用、点评 prompt、输出解析和离线兜底文案。
- `persona/`: 读取 `personas.json`，计算上班 / 下班状态和私聊专属设定。
- `storage/`: SQLite：好感度、群 / 私聊的点评角色。

## 角色卡（`personas.json`）

点评 prompt 用到的字段：`meta`、`persona`、`private_only`、`duty`、`stages`、`dice_reactions`。

- 群里永远是工作中；私聊里好感度 ≥ 86 下班，北京时间 21 点到次日 6 点门槛降到 60。
- 下班时用 `stages[].off_duty_call` 作为称呼，“呆货”只在最高阶段出现。
- `private_only` 只在私聊投骰时注入。

## 好感度

好感度只影响点评的称呼和语气，不影响骰点，也不会再自动变化（原来随聊天变化）。
用 `/dice 设置好感 [user_id] [数值]` 手动调整。

## 和 MaiBot 共用一个 QQ 号时

MaiBot 侧需要过滤掉命令消息，避免两边都回复，例如在 `bot_config.toml`：

```toml
[message_receive]
ban_msgs_regex = ["^[./。]", "^/?p站", "^(搜图|最新|美图|热门|每日一图)(\\s|$)"]
```

## Tests

`python mytest/plugins/dice_girl_plugin/llm/evals/run_local.py`
