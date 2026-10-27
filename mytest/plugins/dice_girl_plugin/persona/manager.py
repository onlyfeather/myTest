import json
from pathlib import Path

from nonebot import logger
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from ..storage import get_group_role, get_private_role
from .lore import LoreEntry, parse_file_entries
from .schema import role_aliases, role_name

PERSONA_PATH = Path(__file__).parent.parent / "personas.json"
LORE_PATH = Path(__file__).parent.parent / "lorebooks.json"
FALLBACK_ROLE = "rinne"


class PersonaManager:
    def __init__(self):
        self.reload()

    def _load_json(self, path: Path):
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Persona] 读取 {path} 失败: {e}")
            return {}

    def reload(self) -> tuple[int, int]:
        """重新读取角色卡和世界书，返回 (角色数, 世界书词条数)。"""
        self.data = self._load_json(PERSONA_PATH)
        self.lore_entries: list[LoreEntry] = parse_file_entries(self._load_json(LORE_PATH))
        return len(self.data.get("roles", {})), len(self.lore_entries)

    @property
    def default_role(self) -> str:
        roles = self.data.get("roles", {})
        preferred = self.data.get("default_role") or FALLBACK_ROLE
        if preferred in roles:
            return preferred
        return next(iter(roles), FALLBACK_ROLE)

    def list_roles(self):
        return list(self.data.get("roles", {}).keys())

    def get_current_role_id(self, event: Event = None) -> str:
        roles = self.data.get("roles", {})
        role_key = self.default_role

        if isinstance(event, PrivateMessageEvent):
            custom_role = get_private_role(event.get_user_id())
            if custom_role in roles:
                role_key = custom_role
        elif isinstance(event, GroupMessageEvent):
            group_role = get_group_role(str(event.group_id))
            if group_role in roles:
                role_key = group_role

        return role_key

    def get_role(self, role_key: str) -> dict:
        roles = self.data.get("roles", {})
        if role_key in roles:
            return roles[role_key]
        return next(iter(roles.values()), {})

    def get_persona(self, event: Event = None) -> dict:
        return self.get_role(self.get_current_role_id(event))

    def check_role_exists(self, role_key: str) -> bool:
        return role_key in self.data.get("roles", {})

    def get_role_name(self, role_key: str) -> str:
        roles = self.data.get("roles", {})
        if role_key in roles:
            return role_name(roles[role_key])
        return "未知"

    def get_role_aliases(self, role_key: str) -> list[str]:
        return role_aliases(self.get_role(role_key))


persona_manager = PersonaManager()
