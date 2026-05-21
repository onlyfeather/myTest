import json
from pathlib import Path

from nonebot import logger
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from ..storage import get_group_role, get_private_role

PERSONA_PATH = Path(__file__).parent.parent / "personas.json"
LORE_PATH = Path(__file__).parent.parent / "lorebooks.json"


class PersonaManager:
    def __init__(self):
        self.data = self._load_json(PERSONA_PATH)
        self.lore_data = self._load_json(LORE_PATH)

    def _load_json(self, path: Path):
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"[Error] Failed to load {path}: {e}")
            return {}

    def reload(self):
        self.data = self._load_json(PERSONA_PATH)
        self.lore_data = self._load_json(LORE_PATH)

    def list_roles(self):
        return list(self.data.get("roles", {}).keys())

    def get_global_lore(self) -> list:
        return self.lore_data.get("entries", [])

    def get_current_role_id(self, event: Event = None) -> str:
        roles = self.data.get("roles", {})
        if not roles:
            return "ling"

        role_key = "ling"

        if event:
            if isinstance(event, PrivateMessageEvent):
                user_id = event.get_user_id()
                custom_role = get_private_role(user_id)
                if custom_role and custom_role in roles:
                    role_key = custom_role

            elif isinstance(event, GroupMessageEvent):
                group_id = str(event.group_id)
                group_role = get_group_role(group_id)
                if group_role and group_role in roles:
                    role_key = group_role

        return role_key

    def get_persona(self, event: Event = None) -> dict:
        role_key = self.get_current_role_id(event)
        roles = self.data.get("roles", {})
        return roles.get(role_key, list(roles.values())[0])

    def check_role_exists(self, role_key: str) -> bool:
        return role_key in self.data.get("roles", {})

    def get_role_name(self, role_key: str) -> str:
        roles = self.data.get("roles", {})
        if role_key in roles:
            meta = roles[role_key].get("meta", {})
            return meta.get("name", role_key)
        return "未知"


persona_manager = PersonaManager()
