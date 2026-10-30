# 提示词的来源

`intent.md`、`arbiter.md`、`push.md` 改写自 [autokeeper](https://github.com/satoshinji2992/autokeeper)
的 `src/autokeeper/agents/prompts.py`（`INTENT_HEAD`、`ACTION_RULES`、`ARBITER_SYSTEM`、`PUSH_SYSTEM`）：
删掉了我们不用的部分（施法、伤害表、规则书查询、即兴备场、交易、同行、AI 队友、装备），
字段改成我们自己的 JSON 格式，并加上了异步群聊团的规则。按 MIT 许可附上原许可声明：

```
MIT License

Copyright (c) 2026 satoshinji2992

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

其余提示词是本项目自己写的。
