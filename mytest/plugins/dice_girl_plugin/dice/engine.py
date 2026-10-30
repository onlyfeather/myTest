import random
import re

from .coc import CRITICAL, FAILURE, FUMBLE, SUCCESS, success_level

MAX_DICE_FACES = 1_000_000


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

        args = clean_text.split()

        max_val = 100
        target_val = None
        event_name = "随机数"

        if len(args) == 0:
            pass
        elif len(args) == 1:
            if args[0].isdigit():
                max_val = int(args[0])
            else:
                event_name = args[0]
        elif len(args) >= 2:
            if args[0].isdigit() and args[1].isdigit():
                max_val = int(args[0])
                target_val = int(args[1])
                if len(args) > 2:
                    event_name = " ".join(args[2:])
            elif args[0].isdigit():
                max_val = int(args[0])
                event_name = " ".join(args[1:])
            else:
                event_name = " ".join(args)

        max_val = min(max(1, max_val), MAX_DICE_FACES)

        return max_val, target_val, event_name

    def _get_status(self, score, target, max_val):
        """越小越好，和 CoC（.ra）一样：点数 ≤ 目标值算成功。

        d100 直接用 CoC 7 版的六档。其他面数：最低 5%（至少 1）是大成功，最高 5% 是大失败，其余按是否 ≤ 目标值。
        """
        if target is None:
            return "无判定"
        if max_val == 100:
            return success_level(score, target)

        band = max(1, int(max_val * 0.05))
        if score <= band:
            return CRITICAL
        if score > max_val - band:
            return FUMBLE
        return SUCCESS if score <= target else FAILURE

    def execute_roll(self, max_val: int, target: int | None, event_name: str) -> dict:
        # 好感度只影响 AI 语言风格，不影响骰点。
        roll = random.randint(1, max_val)
        return {
            "max": max_val,
            "target": target,
            "event": event_name,
            "final_roll": roll,
            "final_status": self._get_status(roll, target, max_val),
        }


dice_logic = DiceLogic()
