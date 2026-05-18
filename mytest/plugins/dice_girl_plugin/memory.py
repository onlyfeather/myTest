from collections import defaultdict


class MemoryManager:
    def __init__(self, limit: int = 10):
        """
        :param limit: 记忆保留的最大条数 (推荐 6-10)
        """
        # 使用字典存储，key是user_id，value是消息列表
        self._cache = defaultdict(list)
        self.limit = limit

    def get_history(self, user_id: str) -> list:
        """获取某人的聊天记录"""
        return self._cache[user_id]

    def add_message(self, user_id: str, role: str, content: str):
        """
        追加一条记忆
        :param role: 'user' 或 'assistant'
        :param content: 文本内容
        """
        # 构建符合 API 标准的消息对象
        msg = {"role": role, "content": content}
        self._cache[user_id].append(msg)

        # 维护滑动窗口，超过限制移除最早的一条
        if len(self._cache[user_id]) > self.limit:
            self._cache[user_id].pop(0)

    def clear(self, user_id: str):
        """清空某人的记忆 (通常用于切换角色时)"""
        if user_id in self._cache:
            self._cache[user_id] = []


# 全局单例
memory_manager = MemoryManager(limit=8)