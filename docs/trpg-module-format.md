# 跑团模组格式（第 1 版）

模组是一个 JSON 文件，可以写 `//` 和 `/* */` 注释。完整的例子见 `mytest/plugins/trpg_plugin/modules/m_tea_house.json`。

导入时会校验，有问题会列出位置和原因，例如 `scenes[s1].exits[0].to：场景 s9 不存在`。「错误」必须改；「提醒」不改也能用，但跑起来可能出问题。

## 编号

场景、线索、NPC、事件、标记、结局都用无含义的编号：`s1`、`c1`、`n1`、`e1`、`f1`、`end1`。不要写 `f_found_ledger` 这种带含义的编号，免得编号本身剧透。编号在整个模组里不能重复。

模组自己的 `id` 可以有含义（例如 `m_tea_house`），小写字母开头，只用小写字母、数字、下划线。

## 谁能看到什么

| 内容 | 叙述（玩家看到的） | 裁决（KP 视角） |
|---|---|---|
| `intro`、场景 `name` / `public`、出口 `how`、NPC `name` / `public` | ✓ | ✓ |
| 已揭示线索的 `text`、已发生事件的 `public`、结局的 `public` | ✓ | ✓ |
| `truth`、场景 `hidden` / `hints`、NPC 的性格、`knows` / `secrets` / `lies`、未揭示的线索、标记 | | ✓ |

## 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `format` | ✓ | 固定写 `1` |
| `id`、`title` | ✓ | |
| `tone`、`era` | ✓ | 叙述基调、时代和地点 |
| `players` | | `[最少, 最多]`，1 到 5，默认 `[1, 5]` |
| `intro` | ✓ | 开场给玩家看的 |
| `truth` | ✓ | 真相，只给裁决环节 |
| `spoiler_words` | | 真相的关键词，叙述里出现就算漏了 |
| `start_scene` | ✓ | 开场所在的场景 |
| `scenes`、`endings` | ✓ | 至少一项 |
| `clues`、`npcs`、`events`、`flags`、`pregens` | | |

## 条件

出口的 `requires`、事件和结局的 `when` 都用同一种条件。几项同时满足才成立：

```jsonc
{"flags_all": ["f1", "f2"], "flags_any": ["f3", "f4"], "clues_all": ["c1"], "scene": "s2"}
```

- `flags_all`：这些标记都设置了；`flags_any`：至少设置了一个
- `clues_all`：这些线索都揭示了
- `scene`：调查员在这个场景

## 场景 `scenes`

`id`、`name`、`public` 必填。`hidden` 是只有 KP 知道的细节。`npcs` 是一开始在这里的 NPC。`hints` 是卡关时灵感检定成功后按顺序给的提示。

出口 `exits`：`{"to": "s2", "how": "穿过后门", "requires": {条件}}`。`requires` 不写就是随时能走。

## 线索 `clues`

| 字段 | 说明 |
|---|---|
| `scene`、`where`、`text` | 在哪个场景、跟着哪样东西、揭示时给玩家的内容（必填） |
| `how` | `{"skill": "侦查", "difficulty": "regular"}`，难度 `regular` / `hard` / `extreme`（也可以写 常规 / 困难 / 极难）。写 `null` 表示查对了地方直接给 |
| `key` | 推进剧情必需的线索 |
| `on_fail` | 检定失败时的内容（失败也能得到一部分，或失败另有后果）。大多留空 |
| `set_flags` | 揭示时设置的标记 |
| `spoiler_words` | 这条线索的关键词，揭示之前叙述里出现就算漏了 |

关键线索只能靠检定拿到、又没写 `on_fail` 时会提醒：失败后玩家可能卡死。

## NPC `npcs`

`name`、`public` 必填。`personality` 性格和说话方式；`knows` 愿意说的；`secrets` 会隐瞒的；`lies` 会说的谎；`attitude` 是 `friendly` / `neutral` / `wary` / `hostile`（也可以写 友好 / 中立 / 戒备 / 敌对）；`social` 是对他做社交检定时参考的数值，例如 `{"说服": 50}`；`spoiler_words` 是秘密的关键词。

## 事件 `events`

`when` 条件满足时发生（默认只发生一次，`"once": false` 可以重复）。`public` 是玩家看到的。`sanity` 是理智检定的损失，`{"success": "0", "fail": "1d3"}`，数值由模组写死，AI 不会改。`set_flags` 是发生后设置的标记。

## 标记 `flags`

`{"id": "f1", "desc": "能进储藏室了"}`。没有线索或事件设置的标记，由裁决环节按 `desc` 判断什么时候设置，所以 `desc` 要写清楚条件。

## 结局 `endings`

`when` 条件和 `public`（结局的叙述要点）都必填。至少要有一个结局能达成。

## 预设调查员 `pregens`

`name`、`occupation`、`sheet` 必填，`background` 可选。`sheet` 和骰娘的 `.st` 写法一样：`"侦查60 图书馆70 理智55 智力70 幸运50"`，也可以写成 `{"侦查": 60}`。必须有理智、智力、幸运。没写的技能按 CoC 7 版的基础值算。

## 剧透关键词

- 一开始就给玩家看的文字（开场、场景描述、出口、NPC 外貌）里不能出现任何剧透关键词，否则每轮都会被误判。
- 关键词至少两个字。
- 已经揭示的线索里出现过的词，之后叙述再用就不算漏。
