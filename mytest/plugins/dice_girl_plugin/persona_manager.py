import json
from pathlib import Path
from nonebot import logger
from nonebot.adapters import Event
# 引入适配器特定事件，用于判断是群聊还是私聊
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from .data_source import get_group_role, get_private_role

# 定义文件路径
PERSONA_PATH = Path(__file__).parent / "personas.json"
LORE_PATH = Path(__file__).parent / "lorebooks.json"


class PersonaManager:
    def __init__(self):
        # 初始化时加载两份数据
        self.data = self._load_json(PERSONA_PATH)
        self.lore_data = self._load_json(LORE_PATH)

    def _load_json(self, path: Path):
        """通用的 JSON 加载函数"""
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"[Error] Failed to load {path}: {e}")
            return {}

    def reload(self):
        """热重载配置 (可选功能)"""
        self.data = self._load_json(PERSONA_PATH)
        self.lore_data = self._load_json(LORE_PATH)

    def list_roles(self):
        """列出所有可用角色"""
        return list(self.data.get("roles", {}).keys())

    def get_global_lore(self) -> list:
        """获取全局世界书条目"""
        # 对应 lorebooks.json 中的 {"entries": [...]} 结构
        return self.lore_data.get("entries", [])

    def get_current_role_id(self, event: Event = None) -> str:
        """
        获取当前环境下的角色 ID (Key)
        """
        roles = self.data.get("roles", {})
        if not roles:
            return "ling"  # 默认兜底

        role_key = "ling"

        if event:
            # 1. 优先检查私聊
            if isinstance(event, PrivateMessageEvent):
                user_id = event.get_user_id()
                custom_role = get_private_role(user_id)
                if custom_role and custom_role in roles:
                    role_key = custom_role

            # 2. 其次检查群聊
            elif isinstance(event, GroupMessageEvent):
                group_id = str(event.group_id)
                group_role = get_group_role(group_id)
                if group_role and group_role in roles:
                    role_key = group_role

        return role_key

    def get_persona(self, event: Event = None) -> dict:
        """
        根据上下文获取当前角色配置
        """
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
