from .character import render_character_block
from .common import build_character_context


def build_dice_prompt(
    role_data,
    current_fav: int,
    user_name: str,
    user_text: str = "",
) -> str:
    ctx = build_character_context(role_data, current_fav, user_text)
    dice_reactions = role_data.get("dice_reactions", {})

    prompt = f"""
<task>
你是骰娘角色「{ctx.role_name}」，只根据给定骰点结果生成一句到两句点评。
点评要有角色味，但不能改变或暗示改变骰点。
</task>

{render_character_block(ctx)}

<dice_reaction_style>
- 大成功: {dice_reactions.get("critical_success", "激动")}
- 成功: {dice_reactions.get("success", "得意")}
- 失败: {dice_reactions.get("failure", "嘲讽")}
- 大失败: {dice_reactions.get("critical_failure", "大笑")}
</dice_reaction_style>

<rules>
- 只能评价用户已经投出的结果。
- 不宣称操控骰点、修改命运、作弊或补正。
- 避免使用“保佑”“加护”“我让你成功”“托我的福”等暗示影响结果的词。
- 好感度只影响称呼、距离感和语气，不影响骰点。
- 点评短一点，优先一句有表现力的角色台词，可带一个简短动作。
- 不输出 Markdown、标题、列表、代码块或额外说明。
</rules>

<json_output_contract>
只返回一个合法 json object，不要包裹代码块，不要输出 json 以外文本。
schema:
{{"reply":"点评文本","delta":0}}
delta 固定为 0。
</json_output_contract>
""".strip()

    return prompt.replace("{user}", user_name).replace("{char}", ctx.role_name)


def build_dice_user_prompt(user_fav: int, data: dict) -> str:
    base_info = f"用户好感度：{user_fav}。动作：{data['event']}。"

    if data["target"] is None:
        return (
            f"{base_info}\n"
            f"投骰类型：纯随机投掷，无成功/失败判定。\n"
            f"投骰范围：1-{data['max']}。\n"
            f"最终结果：{data['final_roll']}。\n"
            f"请根据数字大小和角色性格做简短点评。"
        )

    return (
        f"{base_info}\n"
        f"投骰类型：目标值检定。\n"
        f"最终结果：{data['final_roll']}/{data['target']}。\n"
        f"判定状态：{data['final_status']}。\n"
        f"请根据判定状态和角色性格做简短点评。"
    )
