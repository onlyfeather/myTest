# Dice Girl Plugin Modules

当前仍然是一个 NoneBot 插件，但内部已经按职责拆成可独立演进的模块。

## Boundary

- `handlers/`: NoneBot 命令和消息入口，只做参数提取、权限判断和回复。
- `dice/`: 纯骰子核心，负责指令解析、随机投骰和成功状态判定，不依赖 LLM。
- `chat/`: 聊天侧状态，包含按会话作用域、角色、用户隔离的短期上下文记忆；消息窗口持久化到 SQLite，窗口溢出时合并为长期摘要。
- `llm/`: OpenAI-compatible 调用、组件化 prompt、结构化输出校验和离线兜底回复。
- `persona/`: 角色、人设、世界书读取，以及当前群聊/私聊角色解析。
- `storage/`: SQLite 持久化，包含好感度、群角色、私聊角色配置。

## Compatibility

旧模块名仍保留为兼容转发：

- `ai.py` -> `llm/`
- `logic.py` -> `dice/`
- `memory.py` -> `chat/`
- `persona_manager.py` -> `persona/`
- `data_source.py` -> `storage/`

后续如果决定把 AI 聊天拆成独立插件，可以优先抽出 `chat/`、`llm/`，
并让骰娘插件只通过一个明确的服务接口请求“角色点评”。

## LLM Prompt Layout

- `llm/prompts/common.py`: 关系阶段、世界书、示例和角色上下文提取。
- `llm/prompts/character.py`: 通用角色信息块。
- `llm/prompts/chat.py`: 日常聊天 prompt。
- `llm/prompts/dice.py`: 骰点点评 prompt 和骰点用户消息。
- `llm/schemas.py`: `ChatReply`、`DiceReaction` 输出结构校验。
- `llm/completions.py`: 通用 OpenAI-compatible chat completions 调用。
- `llm/chat_service.py`: 角色聊天编排。
- `llm/dice_service.py`: 骰点点评编排。
- `llm/summary_service.py`: LLM 长期记忆摘要器；失败时由规则摘要兜底。
- `llm/evals/`: 本地 prompt/schema 回归样例，不发真实 LLM 请求。
