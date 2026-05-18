import random
import re
from nonebot.adapters import Event


class DiceLogic:
    def parse_command(self, text: str):
        """
        解析指令，支持多种格式：
        1. /dice rd                  -> 100, None, "随机数"
        2. /dice rd 20               -> 20, None, "随机数"
        3. /dice rd 100 50           -> 100, 50, "判定"
        4. /dice rd 100 50 侦查      -> 100, 50, "侦查"
        5. .rd 100 50 侦查           -> 100, 50, "侦查"
        """
        clean_text = re.sub(r"^[.。]rd", "", text, flags=re.IGNORECASE).strip()

        # 按空格分割参数
        args = clean_text.split()

        max_val = 100  # 默认面数
        target_val = None  # 默认无目标 (表示不进行判定)
        event_name = "随机数"  # 默认事件名

        if len(args) == 0:
            # Case: /dice rd (默认为 1d100 随机)
            pass

        elif len(args) == 1:
            # Case: /dice rd 20 (只可能是面数，或者事件名?)
            if args[0].isdigit():
                max_val = int(args[0])
            else:
                # Case: /dice rd 吃饭 (默认 d100，事件名为吃饭)
                event_name = args[0]

        elif len(args) >= 2:
            # Case: /dice rd 100 50 ...
            if args[0].isdigit() and args[1].isdigit():
                max_val = int(args[0])
                target_val = int(args[1])
                if len(args) > 2:
                    event_name = " ".join(args[2:])
            elif args[0].isdigit():
                # Case: /dice rd 100 事件 (无目标)
                max_val = int(args[0])
                event_name = " ".join(args[1:])
            else:
                # Case: /dice rd 事件 (参数乱填，全当事件)
                event_name = " ".join(args)

        # 限制 max_val 至少为 1
        max_val = max(1, max_val)

        return max_val, target_val, event_name

    def _get_status(self, score, target, max_val):
        # 如果没有目标值，直接返回无判定状态
        if target is None:
            return "无判定"

        # 动态 5% 判定
        threshold = max(1, int(max_val * 0.05))
        if score <= threshold: return "大失败"
        if score >= (max_val - threshold + 1): return "大成功"
        if score >= target: return "成功"
        return "失败"

    def execute_roll(self, user_id: str, favorability: int, max_val: int, target: int, event_name: str,
                     event: Event = None):
        # 物理投骰：好感度只影响 AI 语言风格，不影响骰点。
        raw_roll = random.randint(1, max_val)

        modifier = 0
        final_roll = raw_roll
        is_revealed = False
        raw_status = "无判定"
        final_status = "无判定"

        if target is not None:
            raw_status = self._get_status(raw_roll, target, max_val)
            final_status = raw_status

        return {
            "max": max_val,
            "target": target,  # 可能为 None
            "event": event_name,
            "raw_roll": raw_roll,
            "raw_status": raw_status,
            "mod": modifier,
            "final_roll": final_roll,
            "final_status": final_status,
            "is_revealed": is_revealed
        }


dice_logic = DiceLogic()
