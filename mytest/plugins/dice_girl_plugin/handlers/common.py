from nonebot.adapters import Event


def get_user_name(event: Event) -> str:
    sender = getattr(event, "sender", None)
    if sender:
        return getattr(sender, "card", "") or getattr(sender, "nickname", "") or "用户"
    return "用户"


def extract_plain_text(event: Event) -> str:
    if hasattr(event, "get_plaintext"):
        return event.get_plaintext()
    return event.get_plain_text()
