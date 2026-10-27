# Dice Girl Plugin Modules

## Boundary

- `__init__.py`: 插件入口，声明 localstore 依赖和 `__plugin_meta__`。
- `handlers/`: NoneBot 命令和消息入口。`chat.py` 里群消息和私聊分两条路；`group_admin.py` 是群梗、昵称、档案、群开关命令。
- `group/`: 群聊。
  - `context.py`: 把消息段转成文字（@ 换成昵称、图片换成 [图片]），存取群聊上下文。
  - `members.py`: 群员识别、多昵称匹配、档案 / 好感度的隔离方式。
  - `profiles.py`: 档案转成 prompt 文字；把模型学到的昵称、事实、印象合并回档案。
  - `trigger.py`: 决定要不要开口（见下）。
  - `service.py`: 群聊主流程。
- `dice/`: 纯骰子核心，不依赖 LLM。
- `chat/`: 私聊短期记忆和长期摘要。
- `llm/`: 模型调用、prompt、输出解析。`group_service.py` 是群聊调用。
- `persona/`: 角色卡（`schema.py` 处理私聊 / 群聊差异）、世界书（`lore.py`）。
- `storage/`: SQLite。`group_repository.py` 是群聊相关的表。

## 她什么时候在群里说话

1. 必须回复：@ 她、回复她的消息、叫她的名字或别名（`meta.aliases`）。
2. 可能回复：命中 `topic: true` 的世界书词条或群梗，由模型决定说不说，宁可不说。
   同一词条在同一群冷却 `DICE_GIRL_TOPIC_COOLDOWN_SECONDS`（默认 1800），
   每群每小时最多 `DICE_GIRL_TOPIC_REPLIES_PER_HOUR` 次（默认 3）。
3. 其他消息只记进上下文。以 `.`、`。`、`/` 开头的命令不进上下文。
4. 冷场时主动开话题：没有实现。

## 角色卡

当前只有一个角色：白鸟凛音（`rinne`）。切换角色的命令还在，但不在帮助里显示；
以后在 `personas.json` 里加角色就能直接用。

### 上班 / 下班（`duty` 字段）

- 群聊：永远是工作中。
- 私聊：好感度 ≥ `off_duty_min_favorability`（86）时下班；
  北京时间 `night_hours`（21 点到次日 6 点）里，门槛降到 `night_off_duty_min_favorability`（60）。
- 下班时使用 `stages[].off_duty_call` 作为称呼、`off_duty_examples` 作为示例。
  “呆货”只写在最高阶段的 `off_duty_call` 里，所以只有很亲近的人在她下班后才会被这么叫。

### 字段

旧字段全部照常生效。新增可选字段：

- `meta.aliases`: 群员对她的称呼，用于判断"有人提到她"。
- `modes.private` / `modes.group`: `reply_max_chars`、`actions`（是否写动作描写）、`rules`、`stance`（群里的定位）。
- `private_only`: 只在私聊注入的 `behavior_habits` / `emotional_triggers` / `intimacy_boundaries`。
- `stages[].directive`: 这个好感阶段的行为指令；不写时用通用兜底。
- 顶层 `default_role`: 默认角色。

## 世界书

- `lorebooks.json`：角色深层设定和全局设定。字段 `keys`、`content`、可选 `roles`、`scene`（all/private/group）、`topic`。
- 群梗：`/dice 群梗 添加|列表|删除`，存在数据库里，只在本群生效，默认可触发接话。
- 匹配：英文 / 数字关键词按整词匹配，中文按包含匹配；每次最多注入 3 条，群梗优先。
- 改了文件后用 `/dice 重载`（SUPERUSER）生效，不用重启。

## 群员档案

- 每人一份：显示名、多个称呼（群名片、QQ 昵称、别人起的外号、手动设置）、印象、记住的事。
- 自动学到的称呼每人最多 8 个，按最近使用淘汰；手动设置的不会被淘汰。
- 隔离方式 `DICE_GIRL_MEMBER_SCOPE`：`group`（默认，印象只留在这个群）或 `global`（所有群和私聊共享）。
- 好感度隔离 `DICE_GIRL_AFFINITY_SCOPE`：`global`（默认，与原来一致）或 `group`。

## 群聊上下文

- prompt 里放最近 `DICE_GIRL_GROUP_CONTEXT_SIZE` 条（默认 30）。
- 数据库每群最多保留 `DICE_GIRL_GROUP_KEEP_MESSAGES` 条（默认 200，硬上限）。
- 超过 `DICE_GIRL_GROUP_RETENTION_HOURS`（默认 72）小时的消息，整理进群记忆之后才删除。
- 群里切换角色会清空该群的上下文。

## 群记忆（`group/memory.py`）

按群隔离，不跨群共享。

- 整理：每攒 `DICE_GIRL_DIGEST_EVERY_MESSAGES` 条新消息（默认 40），或者有至少 5 条未整理消息、
  且最旧一条超过 `DICE_GIRL_DIGEST_IDLE_HOURS`（默认 6）小时，就在后台调一次模型，产出：
  - 近期群况：一段滚动更新的摘要，每次群聊都会放进 prompt；
  - 事件：值得长期记住的事，带参与者、关键词、重要度 1~5；
  - 新群梗：直接写进本群群梗（`created_by = auto`），已有的不会重复。
- 回忆：用当前消息和最近 3 句话去找相关事件，最多 4 条。配置了 Embedding 时按语义相似度为主，
  没配置或请求失败时退回关键词 + 被提到的人。分数太低的不放进 prompt。
- 生命周期：事件重要度越高衰减越慢（重要度 1 约 15 天减半，5 约 75 天）；被想起会重新计时；
  每群活跃事件超过 `DICE_GIRL_GROUP_EVENTS_KEEP`（默认 300）条时，保留价值最低的被归档（不删除）。
- 命令：`/dice 群记忆 [搜索 关键词 | 删除 编号 | 整理 | 清空]`。

### Embedding（可选）

OpenAI 兼容接口，默认硅基流动：

```
DICE_GIRL_EMBEDDING_API_KEY=sk-...          # 不填则只用关键词检索
DICE_GIRL_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
DICE_GIRL_EMBEDDING_MODEL=BAAI/bge-m3
```

向量存在 SQLite 里（float32 BLOB），不需要额外的向量数据库。换模型后旧向量会在下次整理时自动重算。

## Tests

每个测试脚本单独运行，例如 `python mytest/plugins/dice_girl_plugin/group/tests_group.py`。
