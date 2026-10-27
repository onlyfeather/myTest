import asyncio
import hashlib
import re
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Union

import httpx
from nonebot import logger

from .config import config
from .pixiv_storage import PixivStorage

# 每日一图按北京时间换日，避免云服务器使用 UTC 时在早上 8 点才换图。
DAILY_TIMEZONE = timezone(timedelta(hours=8))
DEFAULT_MIN_QUALITY_SCORE = 60

# 失败原因按请求（asyncio 任务）隔离，避免并发请求互相覆盖。
_last_error_reason: ContextVar[Optional[str]] = ContextVar(
    "pixiv_last_error_reason", default=None
)


class PixivSpider:
    """Pixiv 客户端。进程内只应存在一个实例，由 pixiv.client 管理。"""

    def __init__(self, storage: Optional[PixivStorage] = None):
        self.session: Optional[httpx.AsyncClient] = None
        self.base_url = "https://www.pixiv.net"
        self.storage = storage or PixivStorage()
        self.full_cookie = None
        self.user_id = None
        self.is_logged_in = False
        self.min_quality_score: float = DEFAULT_MIN_QUALITY_SCORE
        self._rate_limit_lock = asyncio.Lock()
        self._last_request_time = 0.0
        self._detail_semaphore = asyncio.Semaphore(3)
        self._original_url_cache: Dict[str, List[str]] = {}
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Content-Type': 'application/json',
            'Origin': 'https://www.pixiv.net',
            'Referer': 'https://www.pixiv.net/'
        }
        
        # 最小请求间隔（秒）
        self.min_request_interval = 0.5
        
        # Tag池配置
        self.preferred_tags: Set[str] = set()
        self.blocked_tags: Set[str] = set()
        
        # 喜欢作者配置
        self.favorite_authors: Dict[str, Dict[str, Any]] = {}  # {user_id: {name, aliases, added_time, last_check}}
        
        # 圈名到作者ID的映射缓存
        self.alias_to_user_id: Dict[str, str] = {}  # {alias: user_id}
        self.normalized_alias_to_user_id: Dict[str, str] = {}  # {normalized_alias: user_id}
        
        # 默认喜好tag池
        self.default_preferred_tags = {
            "风景", "插画", "原创", "美少女", "动漫", "二次元", 
            "萌", "可爱", "唯美", "治愈", "清新", "温暖"
        }
        
        # 默认厌恶tag池
        self.default_blocked_tags = {
            "R-18", "R18", "成人", "血腥", "暴力", "恐怖", 
            "猎奇", "恶心", "重口", "黑暗", "抑郁"
        }

    @property
    def last_error_reason(self) -> Optional[str]:
        return _last_error_reason.get()

    @last_error_reason.setter
    def last_error_reason(self, value: Optional[str]) -> None:
        _last_error_reason.set(value)

    async def start(self) -> None:
        """加载持久化状态并创建 HTTP 会话，只在首次使用时调用一次。"""
        self.load_cookie()
        self.load_tag_pools()
        self.load_favorite_authors()
        self.load_settings()
        self.session = httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self.session:
            await self.session.aclose()
            self.session = None

    def load_settings(self) -> None:
        raw = self.storage.get_setting("min_quality_score")
        try:
            self.min_quality_score = float(raw) if raw is not None else DEFAULT_MIN_QUALITY_SCORE
        except ValueError:
            logger.warning(f"Pixiv 质量阈值配置无效: {raw!r}，使用默认值")
            self.min_quality_score = DEFAULT_MIN_QUALITY_SCORE

    def set_min_quality_score(self, score: int) -> int:
        score = min(max(int(score), 0), 100)
        self.storage.set_setting("min_quality_score", str(score))
        self.min_quality_score = score
        return score

    @staticmethod
    def _normalize_alias(alias: str) -> str:
        return " ".join(str(alias).strip().casefold().split())
        
    def save_cookie(self, cookie_string: str, user_id: Optional[str] = None, keep_existing_id: bool = True) -> bool:
        """
        保存Cookie到 SQLite
        
        Args:
            cookie_string: 从浏览器复制的完整Cookie字符串
            user_id: 用户ID，如果为None则尝试从现有数据中保留
            keep_existing_id: 是否保留现有的用户ID，默认为True
            
        Returns:
            保存成功返回True，失败返回False
        """
        try:
            if not cookie_string or not cookie_string.strip():
                logger.warning("Cookie字符串为空")
                return False

            final_user_id = user_id
            if final_user_id is None and keep_existing_id:
                existing_data = self.storage.load_cookie()
                if existing_data:
                    final_user_id = existing_data.get("user_id")

            self.storage.save_cookie(cookie_string, final_user_id)

            self.full_cookie = cookie_string
            self.user_id = final_user_id
            self.is_logged_in = True
            logger.info("Pixiv Cookie 已保存到 SQLite")
            return True
            
        except Exception as e:
            logger.exception(f"保存Cookie失败: {e}")
            return False
    
    def load_cookie(self) -> bool:
        """
        从 SQLite 加载Cookie
        
        Returns:
            加载成功返回True，失败返回False
        """
        try:
            cookie_data = self.storage.load_cookie()
            if not cookie_data:
                logger.info("Pixiv Cookie 尚未配置")
                self.last_error_reason = "missing_cookie"
                return False

            full_cookie = cookie_data.get("full_cookie")
            if not full_cookie:
                logger.warning("Pixiv Cookie 记录为空")
                self.last_error_reason = "empty_cookie"
                return False

            self.full_cookie = full_cookie
            self.user_id = cookie_data.get("user_id")
            self.is_logged_in = True
            self.last_error_reason = None
            logger.info("已从 SQLite 加载 Pixiv Cookie")
            return True
        except Exception as e:
            logger.exception(f"加载Cookie失败: {e}")
            self.last_error_reason = "cookie_load_error"
            return False

    def _record_http_error(self, exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code in {401, 403}:
                reason = "cookie_expired"
            elif status_code == 429:
                reason = "rate_limited"
            elif status_code >= 500:
                reason = "pixiv_server_error"
            else:
                reason = f"http_{status_code}"
        elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError)):
            reason = "network_error"
        elif isinstance(exc, RuntimeError) and "Session not initialized" in str(exc):
            reason = "session_not_initialized"
        else:
            reason = "unknown_error"

        self.last_error_reason = reason
        return reason
    
    def set_cookie(self, cookie_string: str) -> bool:
        """
        直接设置Cookie进行登录
        
        Args:
            cookie_string: 从浏览器复制的Cookie字符串
            
        Returns:
            设置成功返回True，失败返回False
        """
        try:
            if not cookie_string or not cookie_string.strip():
                logger.warning("Cookie字符串为空")
                return False
            
            self.save_cookie(cookie_string)
            logger.info("成功设置Cookie登录")
            return True
            
        except Exception as e:
            logger.exception(f"设置Cookie失败: {e}")
            return False
    
    def load_tag_pools(self) -> bool:
        """加载tag池配置"""
        try:
            self.preferred_tags = self.storage.load_tags("preferred")
            self.blocked_tags = self.storage.load_tags("blocked")

            if self.storage.get_setting("preferred_tags_initialized") != "1":
                self.preferred_tags = self.default_preferred_tags.copy()
                self.storage.replace_tags("preferred", self.preferred_tags)
                logger.info("Pixiv 喜好tag池为空，已写入默认值")

            if self.storage.get_setting("blocked_tags_initialized") != "1":
                self.blocked_tags = self.default_blocked_tags.copy()
                self.storage.replace_tags("blocked", self.blocked_tags)
                logger.info("Pixiv 屏蔽tag池为空，已写入默认值")

            logger.info(f"已加载Pixiv tag池: 喜好 {len(self.preferred_tags)} 个，屏蔽 {len(self.blocked_tags)} 个")
            return True
        except Exception as e:
            logger.exception(f"加载tag池失败: {e}")
            self.preferred_tags = self.default_preferred_tags.copy()
            self.blocked_tags = self.default_blocked_tags.copy()
            return False
    
    def save_tag_pools(self) -> bool:
        """保存tag池配置"""
        try:
            self.storage.replace_tags("preferred", self.preferred_tags)
            self.storage.replace_tags("blocked", self.blocked_tags)
            logger.info("Pixiv tag池已保存到 SQLite")
            return True
        except Exception as e:
            logger.exception(f"保存tag池失败: {e}")
            return False
    
    def add_preferred_tag(self, tag: str) -> bool:
        """添加喜好tag"""
        try:
            clean_tag = tag.strip()
            if not clean_tag:
                logger.warning("喜好tag不能为空")
                return False
            self.preferred_tags.add(clean_tag)
            if not self.save_tag_pools():
                return False
            logger.info(f"已添加喜好tag: {clean_tag}")
            return True
        except Exception as e:
            logger.exception(f"添加喜好tag失败: {e}")
            return False
    
    def remove_preferred_tag(self, tag: str) -> bool:
        """移除喜好tag"""
        try:
            clean_tag = tag.strip()
            self.preferred_tags.discard(clean_tag)
            if not self.save_tag_pools():
                return False
            logger.info(f"已移除喜好tag: {clean_tag}")
            return True
        except Exception as e:
            logger.exception(f"移除喜好tag失败: {e}")
            return False
    
    def add_blocked_tag(self, tag: str) -> bool:
        """添加厌恶tag"""
        try:
            clean_tag = tag.strip()
            if not clean_tag:
                logger.warning("屏蔽tag不能为空")
                return False
            self.blocked_tags.add(clean_tag)
            if not self.save_tag_pools():
                return False
            logger.info(f"已添加屏蔽tag: {clean_tag}")
            return True
        except Exception as e:
            logger.exception(f"添加屏蔽tag失败: {e}")
            return False
    
    def remove_blocked_tag(self, tag: str) -> bool:
        """移除厌恶tag"""
        try:
            clean_tag = tag.strip()
            self.blocked_tags.discard(clean_tag)
            if not self.save_tag_pools():
                return False
            logger.info(f"已移除屏蔽tag: {clean_tag}")
            return True
        except Exception as e:
            logger.exception(f"移除屏蔽tag失败: {e}")
            return False
    
    def get_tag_pools_info(self) -> Dict[str, Any]:
        """获取tag池信息"""
        return {
            'preferred_tags': list(self.preferred_tags),
            'blocked_tags': list(self.blocked_tags),
            'preferred_count': len(self.preferred_tags),
            'blocked_count': len(self.blocked_tags)
        }

    async def _should_block_image_with_translation(self, illust_tags: List[str], related_tags: Optional[List[str]] = None, tag_translation: Optional[Dict[str, Dict[str, str]]] = None) -> bool:
        """
        检查图片是否应该被屏蔽（基于tag和翻译，包括相关标签）
        优化版本：减少重复翻译检查，提高性能
        
        Args:
            illust_tags: 图片的tag列表
            related_tags: 搜索结果中的相关标签列表（可选）
            tag_translation: API返回的tagTranslation数据（relatedTags的翻译）
            
        Returns:
            True表示应该屏蔽，False表示不屏蔽
        """
        try:
            if not illust_tags or not self.blocked_tags:
                return False
            
            # 将图片tag转换为小写集合，便于比较
            illust_tags_lower = {tag.lower().strip() for tag in illust_tags if tag and tag.strip()}
            blocked_tags_lower = {tag.lower().strip() for tag in self.blocked_tags if tag and tag.strip()}
            
            logger.debug(f"检查标签屏蔽: 图片标签 {len(illust_tags_lower)} 个，屏蔽标签 {len(blocked_tags_lower)} 个")
            
            # 🔥 性能优化1：先进行快速直接匹配，避免不必要的翻译检查
            # 检查直接匹配（图片标签）
            for blocked_tag in blocked_tags_lower:
                # 精确匹配
                if blocked_tag in illust_tags_lower:
                    logger.debug(f"图片标签精确匹配屏蔽: '{blocked_tag}'")
                    return True
                
                # 模糊匹配（检查是否包含屏蔽tag作为子字符串）
                for illust_tag in illust_tags_lower:
                    if blocked_tag in illust_tag or illust_tag in blocked_tag:
                        logger.debug(f"图片标签模糊匹配屏蔽: '{blocked_tag}' in '{illust_tag}'")
                        return True
            
            # 🔥 性能优化2：如果没有翻译数据，跳过翻译检查
            if not tag_translation:
                # 检查相关标签的直接匹配（不检查翻译）
                if related_tags:
                    limited_related_tags = related_tags[:3]  # 进一步限制相关标签检查数量
                    for related_tag in limited_related_tags:
                        if not related_tag or not related_tag.strip():
                            continue
                        
                        related_tag_lower = related_tag.lower().strip()
                        
                        # 检查相关标签是否匹配屏蔽tag
                        for blocked_tag in blocked_tags_lower:
                            if related_tag_lower == blocked_tag or blocked_tag in related_tag_lower:
                                logger.debug(f"相关标签匹配屏蔽: '{related_tag}' 匹配屏蔽tag '{blocked_tag}'")
                                return True
                
                return False
            
            # 🔥 性能优化3：批量翻译检查，避免重复处理
            # 构建需要检查翻译的标签列表（优先检查图片标签）
            tags_to_check_translation = []
            
            # 添加图片标签（优先级高）
            for illust_tag in illust_tags:
                if illust_tag in tag_translation and illust_tag not in tags_to_check_translation:
                    tags_to_check_translation.append(illust_tag)
            
            # 添加少量相关标签（限制数量以提高性能）
            if related_tags:
                limited_related_tags = related_tags[:3]  # 只检查前3个相关标签
                for related_tag in limited_related_tags:
                    if (related_tag in tag_translation and 
                        related_tag not in tags_to_check_translation and
                        len(tags_to_check_translation) < 8):  # 总共最多检查8个标签的翻译
                        tags_to_check_translation.append(related_tag)
            
            logger.debug(f"翻译检查范围: {len(tags_to_check_translation)} 个标签（从 {len(illust_tags) + (len(related_tags) if related_tags else 0)} 个中筛选）")
            
            # 批量检查翻译
            for tag in tags_to_check_translation:
                if tag not in tag_translation:
                    continue
                
                translations = tag_translation[tag]
                if not isinstance(translations, dict):
                    continue
                
                # 检查所有语言的翻译
                for lang, translated in translations.items():
                    if not translated or not translated.strip():
                        continue
                    
                    translated_lower = translated.lower().strip()
                    
                    # 检查翻译是否匹配屏蔽tag
                    for blocked_tag in blocked_tags_lower:
                        # 精确匹配
                        if translated_lower == blocked_tag:
                            tag_type = "图片标签" if tag in illust_tags else "相关标签"
                            logger.debug(f"翻译匹配屏蔽: {tag_type} '{tag}' ({lang}: '{translated}') 精确匹配屏蔽tag '{blocked_tag}'")
                            return True
                        
                        # 词汇边界匹配（避免子字符串误判）
                        if len(blocked_tag) >= 2:  # 至少2个字符才考虑模糊匹配
                            # 使用更精确的边界匹配
                            pattern = r'(?<![a-zA-Z0-9\-_])' + re.escape(blocked_tag) + r'(?![a-zA-Z0-9\-_])'
                            if re.search(pattern, translated_lower, re.IGNORECASE):
                                tag_type = "图片标签" if tag in illust_tags else "相关标签"
                                logger.debug(f"翻译匹配屏蔽: {tag_type} '{tag}' ({lang}: '{translated}') 词汇匹配屏蔽tag '{blocked_tag}'")
                                return True
            
            return False
            
        except Exception as e:
            logger.debug(f"检查图片屏蔽异常: {e}")
            return False  # 出错时不屏蔽，避免误删

    def load_favorite_authors(self) -> bool:
        """加载喜欢作者配置"""
        try:
            self.favorite_authors = self.storage.load_favorite_authors()
            self._update_alias_mapping()
            logger.info(f"已加载喜欢作者: {len(self.favorite_authors)}个作者")
            return True
        except Exception as e:
            logger.exception(f"加载喜欢作者失败: {e}")
            self.favorite_authors = {}
            return False
    
    def save_favorite_authors(self) -> bool:
        """保存喜欢作者配置"""
        try:
            self.storage.replace_favorite_authors(self.favorite_authors)
            logger.info("喜欢作者已保存到 SQLite")
            return True
        except Exception as e:
            logger.exception(f"保存喜欢作者失败: {e}")
            return False
    
    def add_favorite_author(self, user_id: str, user_name: str = "", aliases: Optional[List[str]] = None) -> bool:
        """添加喜欢作者"""
        try:
            user_id = str(user_id).strip()
            if not user_id:
                logger.warning("作者ID不能为空")
                return False
            
            if user_id in self.favorite_authors:
                logger.info(f"作者 {user_id} 已在喜欢列表中")
                return False
            
            # 处理圈名列表
            alias_list = []
            if aliases:
                alias_list = [alias.strip() for alias in aliases if alias and alias.strip()]
            
            # 添加作者名作为默认圈名
            if user_name and user_name.strip():
                clean_name = user_name.strip()
                if clean_name not in alias_list:
                    alias_list.append(clean_name)
            
            self.favorite_authors[user_id] = {
                'name': user_name.strip(),
                'aliases': alias_list,
                'added_time': datetime.now().isoformat(),
                'last_check': None
            }
            
            # 更新圈名映射缓存
            self._update_alias_mapping()
            if not self.save_favorite_authors():
                return False

            logger.info(f"已添加喜欢作者: {user_name} ({user_id})")
            if alias_list:
                logger.info(f"圈名: {', '.join(alias_list)}")
            return True
        except Exception as e:
            logger.exception(f"添加喜欢作者失败: {e}")
            return False
    
    def remove_favorite_author(self, user_id: str) -> bool:
        """移除喜欢作者"""
        try:
            user_id = str(user_id).strip()
            if user_id in self.favorite_authors:
                author_info = self.favorite_authors[user_id]
                del self.favorite_authors[user_id]
                # 更新圈名映射缓存
                self._update_alias_mapping()
                if not self.save_favorite_authors():
                    return False
                logger.info(f"已移除喜欢作者: {author_info.get('name', '未知')} ({user_id})")
                return True
            else:
                logger.info(f"作者 {user_id} 不在喜欢列表中")
                return False
        except Exception as e:
            logger.exception(f"移除喜欢作者失败: {e}")
            return False
    
    def get_favorite_author_info(self, user_id: str) -> Optional[Dict[str, Any]]:
        """获取特定喜欢作者信息"""
        return self.favorite_authors.get(str(user_id))
    
    def update_author_last_check(self, user_id: str) -> bool:
        """更新作者最后检查时间"""
        try:
            user_id = str(user_id)
            if user_id in self.favorite_authors:
                checked_at = datetime.now().isoformat()
                self.favorite_authors[user_id]['last_check'] = checked_at
                self.storage.update_author_last_check(user_id, checked_at)
                return True
            return False
        except Exception as e:
            logger.exception(f"更新作者检查时间失败: {e}")
            return False
    
    def _update_alias_mapping(self) -> None:
        """更新圈名到作者ID的映射缓存"""
        try:
            self.alias_to_user_id.clear()
            self.normalized_alias_to_user_id.clear()
            
            for user_id, author_info in self.favorite_authors.items():
                aliases = author_info.get('aliases', [])
                for alias in aliases:
                    if alias and alias.strip():
                        clean_alias = alias.strip()
                        normalized_alias = self._normalize_alias(clean_alias)
                        # 检查圈名冲突
                        if clean_alias in self.alias_to_user_id:
                            existing_user_id = self.alias_to_user_id[clean_alias]
                            if existing_user_id != user_id:
                                logger.warning(f"圈名 '{clean_alias}' 冲突 - 已存在作者 {existing_user_id}，将被作者 {user_id} 覆盖")
                        if normalized_alias in self.normalized_alias_to_user_id:
                            existing_user_id = self.normalized_alias_to_user_id[normalized_alias]
                            if existing_user_id != user_id:
                                logger.warning(f"规范化圈名 '{clean_alias}' 冲突 - 已存在作者 {existing_user_id}，将被作者 {user_id} 覆盖")
                        self.alias_to_user_id[clean_alias] = user_id
                        self.normalized_alias_to_user_id[normalized_alias] = user_id
            
            logger.debug(f"圈名映射已更新: {len(self.alias_to_user_id)} 个映射")
        except Exception as e:
            logger.exception(f"更新圈名映射失败: {e}")

    def add_author_alias(self, user_id: str, alias: str) -> bool:
        """为作者添加圈名"""
        try:
            user_id = str(user_id).strip()
            alias = alias.strip()
            
            if not user_id:
                logger.warning("作者ID不能为空")
                return False
            
            if not alias:
                logger.warning("圈名不能为空")
                return False
            normalized_alias = self._normalize_alias(alias)
            
            if user_id not in self.favorite_authors:
                logger.info(f"作者 {user_id} 不在喜欢列表中")
                return False
            
            # 检查圈名冲突
            if alias in self.alias_to_user_id and self.alias_to_user_id[alias] != user_id:
                existing_user_id = self.alias_to_user_id[alias]
                logger.info(f"圈名 '{alias}' 已被作者 {existing_user_id} 使用")
                return False
            if normalized_alias in self.normalized_alias_to_user_id and self.normalized_alias_to_user_id[normalized_alias] != user_id:
                existing_user_id = self.normalized_alias_to_user_id[normalized_alias]
                logger.info(f"圈名 '{alias}' 与作者 {existing_user_id} 的既有圈名冲突")
                return False
            
            # 添加圈名
            author_info = self.favorite_authors[user_id]
            aliases = author_info.get('aliases', [])
            normalized_aliases = {self._normalize_alias(item) for item in aliases}
            
            if normalized_alias not in normalized_aliases:
                aliases.append(alias)
                author_info['aliases'] = aliases
                self.alias_to_user_id[alias] = user_id
                self.normalized_alias_to_user_id[normalized_alias] = user_id
                if not self.save_favorite_authors():
                    return False
                logger.info(f"已为作者 {user_id} 添加圈名: {alias}")
                return True
            else:
                logger.info(f"圈名 '{alias}' 已存在")
                return False
                
        except Exception as e:
            logger.exception(f"添加圈名失败: {e}")
            return False

    def remove_author_alias(self, user_id: str, alias: str) -> bool:
        """移除作者的圈名"""
        try:
            user_id = str(user_id).strip()
            alias = alias.strip()
            
            if not user_id:
                logger.warning("作者ID不能为空")
                return False
            
            if not alias:
                logger.warning("圈名不能为空")
                return False
            normalized_alias = self._normalize_alias(alias)
            
            if user_id not in self.favorite_authors:
                logger.info(f"作者 {user_id} 不在喜欢列表中")
                return False
            
            # 移除圈名
            author_info = self.favorite_authors[user_id]
            aliases = author_info.get('aliases', [])
            matched_alias = next(
                (item for item in aliases if self._normalize_alias(item) == normalized_alias),
                None,
            )
            
            if matched_alias:
                aliases.remove(matched_alias)
                author_info['aliases'] = aliases
                
                # 从映射中移除
                if matched_alias in self.alias_to_user_id and self.alias_to_user_id[matched_alias] == user_id:
                    del self.alias_to_user_id[matched_alias]
                if normalized_alias in self.normalized_alias_to_user_id and self.normalized_alias_to_user_id[normalized_alias] == user_id:
                    del self.normalized_alias_to_user_id[normalized_alias]
                
                if not self.save_favorite_authors():
                    return False
                logger.info(f"已为作者 {user_id} 移除圈名: {matched_alias}")
                return True
            else:
                logger.info(f"圈名 '{alias}' 不存在")
                return False
                
        except Exception as e:
            logger.exception(f"移除圈名失败: {e}")
            return False

    def search_author_by_alias(self, alias: str) -> Optional[str]:
        """通过圈名搜索作者ID"""
        try:
            alias = alias.strip()
            if not alias:
                return None
            
            return self.alias_to_user_id.get(alias) or self.normalized_alias_to_user_id.get(
                self._normalize_alias(alias)
            )
        except Exception as e:
            logger.debug(f"通过圈名搜索作者失败: {e}")
            return None

    def get_all_aliases(self) -> Dict[str, str]:
        """获取所有圈名映射"""
        return self.alias_to_user_id.copy()

    def get_favorite_authors_info(self) -> Dict[str, Any]:
        """获取喜欢作者统计信息"""
        total_count = len(self.favorite_authors)
        recent_added = 0
        never_checked = 0
        total_aliases = 0
        
        current_time = datetime.now()
        for author_info in self.favorite_authors.values():
            # 检查最近7天内添加的作者
            try:
                added_time = datetime.fromisoformat(author_info.get('added_time', ''))
                if (current_time - added_time).days <= 7:
                    recent_added += 1
            except:
                pass
            
            # 检查从未检查过的作者
            if not author_info.get('last_check'):
                never_checked += 1
            
            # 统计圈名数量
            aliases = author_info.get('aliases', [])
            total_aliases += len(aliases)
        
        return {
            'total_count': total_count,
            'recent_added': recent_added,
            'never_checked': never_checked,
            'total_aliases': total_aliases,
            'authors': self.favorite_authors,
            'alias_mapping': self.alias_to_user_id.copy()
        }
    
    async def get_user_info(self, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        获取用户信息（需要Cookie）
        
        Args:
            user_id: 用户ID，如果为None则尝试获取当前用户信息
            
        Returns:
            用户信息字典，失败返回None
        """
        try:
            # 限流等待
            await self._rate_limit_wait()
            
            if not self.full_cookie:
                logger.debug("没有可用的Cookie")
                return None
                
            # 设置认证头
            headers = self.headers.copy()
            # 使用完整的Cookie字符串
            headers['Cookie'] = self.full_cookie
            
            if user_id:
                # 获取指定用户信息
                url = f"{self.base_url}/ajax/user/{user_id}?full=1&lang=zh"
            else:
                # 获取当前用户信息
                url = f"{self.base_url}/ajax/user/self?full=1&lang=zh"
            
            if not self.session:
                raise RuntimeError("Session not initialized")
                
            logger.debug(f"请求用户信息URL: {url}")
            response = await self.session.get(url, headers=headers)
            response.raise_for_status()
            
            user_info = response.json()
            logger.debug(f"用户信息: {user_info}")
            
            return user_info
            
        except Exception as e:
            logger.debug(f"获取用户信息异常: {e}")
            return None

    async def get_author_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        获取作者作品列表（正确的API）
        
        Args:
            user_id: 作者ID
            
        Returns:
            作者作品信息，失败返回None
        """
        try:
            # 限流等待
            await self._rate_limit_wait()
            
            if not self.full_cookie:
                logger.debug("没有可用的Cookie，无法获取作者作品")
                return None
            
            # 构建正确的作者作品API URL
            url = f"{self.base_url}/ajax/user/{user_id}/profile/top?sensitiveFilterMode=userSetting&lang=zh"
            
            # 设置认证头
            headers = self.headers.copy()
            headers['Cookie'] = self.full_cookie
            
            if not self.session:
                raise RuntimeError("Session not initialized")
            
            logger.debug(f"获取作者作品 - ID: {user_id}, URL: {url}")
            
            response = await self.session.get(url, headers=headers)
            response.raise_for_status()
            
            author_data = response.json()
            
            if author_data.get('error'):
                logger.debug(f"获取作者作品失败: {author_data.get('message', '未知错误')}")
                return None
            
            logger.debug("成功获取作者作品数据")
            return author_data.get('body')
            
        except Exception as e:
            logger.debug(f"获取作者作品异常: {e}")
            return None
    
    async def _rate_limit_wait(self):
        """限流等待"""
        async with self._rate_limit_lock:
            current_time = time.time()
            time_since_last_request = current_time - self._last_request_time

            if time_since_last_request < self.min_request_interval:
                wait_time = self.min_request_interval - time_since_last_request
                logger.debug(f"[Pixiv] Rate limit wait: {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

            self._last_request_time = time.time()

    async def search_illustrations(self, tag: Union[str, List[str]], page: int = 1, page_size: int = 30) -> Optional[Dict[str, Any]]:
        """
        根据tag搜索图片（支持多tag搜索）
        
        Args:
            tag: 搜索标签，可以是字符串（单tag）或列表（多tag）
            page: 页码，默认为1
            page_size: 每页数量，默认为30
            
        Returns:
            精简的图片数据字典，失败返回None
        """
        try:
            # 限流等待
            await self._rate_limit_wait()
            
            # 检查是否有可用的Cookie
            if not self.full_cookie:
                logger.debug("没有可用的Cookie，无法搜索图片")
                self.last_error_reason = "missing_cookie"
                return None
            
            # 处理搜索标签
            if isinstance(tag, list):
                # 多tag搜索，使用空格连接
                search_keyword = ' '.join([t.strip() for t in tag if t and t.strip()])
                logger.debug(f"多tag搜索: {tag} -> 合并关键词: '{search_keyword}'")
            else:
                # 🔥 修复标签拆解逻辑
                tag_str = tag.strip() if tag else ""
                if not tag_str:
                    logger.debug("搜索关键词为空")
                    return None
                
                # 拆解标签：支持逗号、空格、中文逗号分隔
                # 使用正则表达式拆分标签，支持多种分隔符
                tags_list = re.split(r'[,，\s]+', tag_str)
                tags_list = [t.strip() for t in tags_list if t and t.strip()]
                
                if len(tags_list) > 1:
                    # 多个标签，用空格连接（Pixiv搜索API格式）
                    search_keyword = ' '.join(tags_list)
                    logger.debug(f"标签拆解: '{tag_str}' -> 拆分为 {tags_list} -> 合并关键词: '{search_keyword}'")
                else:
                    # 单个标签
                    search_keyword = tags_list[0] if tags_list else tag_str
                    logger.debug(f"单个标签: '{tag_str}' -> 搜索关键词: '{search_keyword}'")
            
            if not search_keyword:
                logger.debug("搜索关键词为空")
                self.last_error_reason = "empty_keyword"
                return None
            
            # 构建URL - 使用Pixiv的搜索API
            url = f"{self.base_url}/ajax/search/artworks/{search_keyword}"
            params = {
                'word': search_keyword,
                'order': 'date_d',
                'mode': 'all',
                'p': page,
                'csw': '0',
                's_mode': 's_tag',
                'type': 'all',
                'lang': 'zh',
                'ai_type': '1',
            }
            
            # 设置认证头
            headers = self.headers.copy()
            # 使用完整的Cookie字符串
            headers['Cookie'] = self.full_cookie
            
            if not self.session:
                raise RuntimeError("Session not initialized")
            
            logger.debug(f"搜索图片 - 关键词: '{search_keyword}', 页码: {page}")
            
            response = await self.session.get(url, params=params, headers=headers)
            response.raise_for_status()
            
            raw_data = response.json()
            self.last_error_reason = None
            
            # 直接返回精简的数据结构
            return await self._extract_simplified_data(raw_data, search_keyword)
            
        except Exception as e:
            reason = self._record_http_error(e)
            logger.debug(f"搜索图片异常: {e} ({reason})")
            return None

    async def _extract_simplified_data(self, raw_data: Dict[str, Any], search_keyword: str) -> Dict[str, Any]:
        """
        从原始API响应中提取精简的数据结构
        
        Args:
            raw_data: 原始API响应数据
            search_keyword: 搜索关键词
            
        Returns:
            精简后的数据结构
        """
        try:
            if not raw_data or 'body' not in raw_data:
                return {
                    'illusts': [],
                    'total': 0,
                    'lastPage': 1,
                    'relatedTags': [],
                    'tagTranslation': {},
                    'popular': {
                        'recent': [],
                        'permanent': []
                    },
                    'searchInfo': {
                        'keyword': search_keyword,
                        'timestamp': datetime.now().isoformat()
                    }
                }
            
            body = raw_data['body']
            illust_manga = body.get('illustManga', {})
            
            # 提取图片数据 - 只保留核心必要字段，并应用tag屏蔽
            simplified_illusts = []
            blocked_count = 0
            
            for illust in illust_manga.get('data', []):
                # 检查是否包含屏蔽的tag（使用翻译增强检查，包括相关标签）
                illust_tags = illust.get('tags', [])
                related_tags = body.get('relatedTags', [])
                tag_translation = body.get('tagTranslation', {})
                
                # 使用增强的标签屏蔽检查（包括相关标签和翻译）
                if await self._should_block_image_with_translation(illust_tags, related_tags, tag_translation):
                    blocked_count += 1
                    continue
                
                simplified_illust = {
                    # 🔴 核心必要数据
                    'id': illust.get('id'),
                    'title': illust.get('title'),
                    'url': self._replace_image_url(illust.get('url', '')),  # 🔥 对缩略图URL也进行反代处理
                    'tags': illust.get('tags', []),
                    'userId': illust.get('userId'),
                    'userName': illust.get('userName'),
                    'pageCount': illust.get('pageCount', 1),
                    'width': illust.get('width'),
                    'height': illust.get('height'),
                    'illustType': illust.get('illustType'),
                    'xRestrict': illust.get('xRestrict', 0),
                    
                    # 🟡 重要数据
                    'description': illust.get('description', ''),
                    'createDate': illust.get('createDate'),
                    'aiType': illust.get('aiType', 0),
                    'profileImageUrl': illust.get('profileImageUrl', '')
                }
                simplified_illusts.append(simplified_illust)
            
            # 记录屏蔽统计
            if blocked_count > 0:
                logger.debug(f"Tag屏蔽: 过滤了 {blocked_count} 个包含屏蔽tag的作品")
            
            # 构建精简的数据结构
            simplified_data = {
                # 🔴 核心数据
                'illusts': simplified_illusts,
                'total': illust_manga.get('total', 0),
                'lastPage': illust_manga.get('lastPage', 1),
                
                # 🟡 重要数据
                'relatedTags': body.get('relatedTags', []),
                'tagTranslation': body.get('tagTranslation', {}),
                
                # 🔥 热门推荐数据
                'popular': {
                    'recent': body.get('popular', {}).get('recent', []),
                    'permanent': body.get('popular', {}).get('permanent', [])
                },
                
                # 搜索元信息
                'searchInfo': {
                    'keyword': search_keyword,
                    'timestamp': datetime.now().isoformat()
                }
            }
            
            logger.debug(f"搜索完成: 找到 {len(simplified_illusts)} 个结果，总计 {simplified_data['total']} 个")
            return simplified_data
            
        except Exception as e:
            logger.debug(f"提取精简数据异常: {e}")
            return {
                'illusts': [],
                'total': 0,
                'lastPage': 1,
                'relatedTags': [],
                'tagTranslation': {},
                'popular': {
                    'recent': [],
                    'permanent': []
                },
                'searchInfo': {
                    'keyword': search_keyword,
                    'timestamp': datetime.now().isoformat()
                }
            }

    def get_daily_seed(self, user_qq: str) -> int:
        """
        生成用户每日固定种子
        
        Args:
            user_qq: 用户QQ号
            
        Returns:
            确定性的种子数字
        """
        today = datetime.now(DAILY_TIMEZONE).strftime("%Y%m%d")
        seed_string = f"{user_qq}_{today}"
        
        # 使用MD5哈希确保确定性
        hash_obj = hashlib.md5(seed_string.encode())
        hash_hex = hash_obj.hexdigest()
        
        # 取前8位十六进制转为整数
        return int(hash_hex[:8], 16)

    def calculate_quality_score(self, illust_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        计算图片质量评分
        
        Args:
            illust_data: 图片数据，包含统计信息
            
        Returns:
            质量评分结果，包含总分和各维度分数
        """
        try:
            # 基础数据提取
            bookmark_count = illust_data.get('bookmarkCount', 0)  # 收藏数
            like_count = illust_data.get('likeCount', 0)          # 点赞数
            comment_count = illust_data.get('commentCount', 0)    # 评论数
            view_count = illust_data.get('viewCount', 0)          # 浏览数
            response_count = illust_data.get('responseCount', 0)  # 回复数
            page_count = illust_data.get('pageCount', 1)          # 页数
            width = illust_data.get('width', 0)                   # 宽度
            height = illust_data.get('height', 0)                 # 高度
            create_date_str = illust_data.get('createDate', '')   # 创建时间
            
            # 计算作品发布时间（小时）
            hours_since_upload = self._calculate_hours_since_upload(create_date_str)
            
            # 1. 互动质量评分 (40%权重)
            interaction_score = self._calculate_interaction_score(
                bookmark_count, like_count, comment_count, response_count, hours_since_upload
            )
            
            # 2. 内容质量评分 (25%权重)
            content_score = self._calculate_content_score(
                page_count, width, height, bookmark_count, view_count
            )
            
            # 3. 时间衰减评分 (20%权重)
            time_score = self._calculate_time_score(hours_since_upload, bookmark_count)
            
            # 4. 参与度评分 (15%权重)
            engagement_score = self._calculate_engagement_score(
                bookmark_count, like_count, comment_count, view_count
            )
            
            # 计算加权总分
            total_score = (
                interaction_score * 0.4 +
                content_score * 0.25 +
                time_score * 0.2 +
                engagement_score * 0.15
            )
            
            # 限制分数范围 0-100
            total_score = max(0, min(100, total_score))
            
            return {
                'total_score': round(total_score, 2),
                'interaction_score': round(interaction_score, 2),
                'content_score': round(content_score, 2),
                'time_score': round(time_score, 2),
                'engagement_score': round(engagement_score, 2),
                'quality_level': self._get_quality_level(total_score),
                'details': {
                    'bookmark_count': bookmark_count,
                    'like_count': like_count,
                    'comment_count': comment_count,
                    'view_count': view_count,
                    'hours_since_upload': hours_since_upload,
                    'page_count': page_count,
                    'resolution': f"{width}x{height}"
                }
            }
            
        except Exception as e:
            logger.debug(f"计算质量评分异常: {e}")
            return {
                'total_score': 0,
                'interaction_score': 0,
                'content_score': 0,
                'time_score': 0,
                'engagement_score': 0,
                'quality_level': '未知',
                'details': {}
            }

    def _calculate_hours_since_upload(self, create_date_str: str) -> float:
        """计算作品发布至今的小时数"""
        try:
            create_time = self.parse_pixiv_time(create_date_str)
            if not create_time:
                return 24 * 30  # 默认30天

            hours = (datetime.now(timezone.utc) - create_time).total_seconds() / 3600
            return max(0.1, hours)  # 最小0.1小时，避免除零
            
        except Exception as e:
            logger.debug(f"计算时间差异常: {e}")
            return 24 * 30  # 默认30天

    def _calculate_interaction_score(self, bookmark_count: int, like_count: int, 
                                   comment_count: int, response_count: int, 
                                   hours_since_upload: float) -> float:
        """
        计算互动质量评分
        
        核心思路：收藏 > 评论 > 点赞 > 回复
        新作品考虑时间衰减，老作品直接看绝对数值
        """
        # 基础权重：收藏(40%) + 评论(30%) + 点赞(20%) + 回复(10%)
        base_score = (
            bookmark_count * 0.4 +
            comment_count * 0.3 +
            like_count * 0.2 +
            response_count * 0.1
        )
        
        if hours_since_upload <= 72:  # 3天内的新作品
            # 时间衰减：每小时的表现
            hourly_score = base_score / max(1, hours_since_upload)
            
            # 新作品评分标准（每小时）
            # 优秀: >10收藏/小时, 良好: 5-10, 一般: 2-5, 较差: <2
            if hourly_score >= 10:
                return 85 + min(15, hourly_score - 10)  # 85-100分
            elif hourly_score >= 5:
                return 70 + (hourly_score - 5) * 3  # 70-85分
            elif hourly_score >= 2:
                return 50 + (hourly_score - 2) * 6.67  # 50-70分
            else:
                return hourly_score * 25  # 0-50分
        else:
            # 老作品直接看绝对数值
            # 优秀: >1000收藏, 良好: 500-1000, 一般: 100-500, 较差: <100
            if bookmark_count >= 1000:
                return 85 + min(15, bookmark_count / 200)  # 85-100分
            elif bookmark_count >= 500:
                return 70 + (bookmark_count - 500) / 33.33  # 70-85分
            elif bookmark_count >= 100:
                return 50 + (bookmark_count - 100) / 13.33  # 50-70分
            else:
                return bookmark_count / 2  # 0-50分

    def _calculate_content_score(self, page_count: int, width: int, height: int, 
                                bookmark_count: int, view_count: int) -> float:
        """
        计算内容质量评分
        
        考虑因素：多页作品加分、高分辨率加分、收藏率加分
        """
        score = 50  # 基础分
        
        # 1. 多页作品加分 (0-20分)
        if page_count > 1:
            multi_page_bonus = min(20, page_count * 5)
            score += multi_page_bonus
        
        # 2. 分辨率加分 (0-20分)
        total_pixels = width * height
        if total_pixels >= 4000000:  # 4K以上
            score += 20
        elif total_pixels >= 2000000:  # 2K-4K
            score += 15
        elif total_pixels >= 1000000:  # 1M-2K
            score += 10
        elif total_pixels >= 500000:   # 500K-1M
            score += 5
        
        # 3. 收藏率加分 (0-10分)
        if view_count > 0:
            bookmark_rate = bookmark_count / view_count
            if bookmark_rate >= 0.1:  # 10%以上收藏率
                score += 10
            elif bookmark_rate >= 0.05:  # 5-10%
                score += 7
            elif bookmark_rate >= 0.02:  # 2-5%
                score += 4
            elif bookmark_rate >= 0.01:  # 1-2%
                score += 2
        
        return min(100, score)

    def _calculate_time_score(self, hours_since_upload: float, bookmark_count: int) -> float:
        """
        计算时间衰减评分
        
        新作品：时间越新分数越高
        老作品：根据收藏数给分
        """
        if hours_since_upload <= 1:  # 1小时内
            return 100
        elif hours_since_upload <= 6:  # 6小时内
            return 90
        elif hours_since_upload <= 24:  # 1天内
            return 80
        elif hours_since_upload <= 72:  # 3天内
            return 70
        elif hours_since_upload <= 168:  # 1周内
            return 60
        elif hours_since_upload <= 720:  # 1月内
            return 50
        else:
            # 老作品根据收藏数给分
            if bookmark_count >= 1000:
                return 40
            elif bookmark_count >= 500:
                return 30
            elif bookmark_count >= 100:
                return 20
            else:
                return 10

    def _calculate_engagement_score(self, bookmark_count: int, like_count: int, 
                                  comment_count: int, view_count: int) -> float:
        """
        计算参与度评分
        
        综合考虑各种互动数据与浏览量的比例
        """
        if view_count == 0:
            return 0
        
        # 计算各项参与度指标
        bookmark_rate = bookmark_count / view_count  # 收藏率
        like_rate = like_count / view_count          # 点赞率
        comment_rate = comment_count / view_count     # 评论率
        
        # 综合参与度评分
        engagement_score = (
            bookmark_rate * 50 +    # 收藏率权重50%
            like_rate * 30 +        # 点赞率权重30%
            comment_rate * 20       # 评论率权重20%
        ) * 100  # 转换为百分制
        
        # 根据收藏数额外加分
        if bookmark_count >= 1000:
            engagement_score += 20
        elif bookmark_count >= 500:
            engagement_score += 15
        elif bookmark_count >= 100:
            engagement_score += 10
        elif bookmark_count >= 50:
            engagement_score += 5
        
        return min(100, engagement_score)

    def _get_quality_level(self, score: float) -> str:
        """根据分数获取质量等级"""
        if score >= 90:
            return "S级 - 神作"
        elif score >= 80:
            return "A级 - 优秀"
        elif score >= 70:
            return "B级 - 良好"
        elif score >= 60:
            return "C级 - 一般"
        elif score >= 40:
            return "D级 - 较差"
        else:
            return "E级 - 低质"

    async def get_illust_details(self, illust_id: str) -> Optional[Dict[str, Any]]:
        """
        获取图片详细信息（包含统计数据）
        
        Args:
            illust_id: 图片ID
            
        Returns:
            图片详细信息，失败返回None
        """
        async with self._detail_semaphore:
            return await self._get_illust_details_unlimited(illust_id)

    async def _get_illust_details_unlimited(self, illust_id: str) -> Optional[Dict[str, Any]]:
        try:
            # 限流等待
            await self._rate_limit_wait()
            
            if not self.full_cookie:
                logger.debug("没有可用的Cookie，无法获取图片详情")
                return None
            
            # 构建URL
            url = f"{self.base_url}/ajax/illust/{illust_id}?lang=zh"
            
            # 设置认证头
            headers = self.headers.copy()
            headers['Cookie'] = self.full_cookie
            
            if not self.session:
                raise RuntimeError("Session not initialized")
            
            logger.debug(f"获取图片详情 - ID: {illust_id}")
            
            response = await self.session.get(url, headers=headers)
            response.raise_for_status()
            
            details_data = response.json()
            
            if details_data.get('error'):
                logger.debug(f"获取图片详情失败: {details_data.get('message', '未知错误')}")
                return None
            
            return details_data.get('body')
            
        except Exception as e:
            logger.debug(f"获取图片详情异常: {e}")
            return None

    async def get_illust_pages(self, illust_id: str) -> Optional[List[Dict[str, Any]]]:
        """
        获取图片的所有页面URL（真正的图片下载地址）
        
        Args:
            illust_id: 图片ID
            
        Returns:
            图片页面列表，每个页面包含urls和尺寸信息，失败返回None
        """
        async with self._detail_semaphore:
            return await self._get_illust_pages_unlimited(illust_id)

    async def _get_illust_pages_unlimited(self, illust_id: str) -> Optional[List[Dict[str, Any]]]:
        try:
            # 限流等待
            await self._rate_limit_wait()
            
            if not self.full_cookie:
                logger.debug("没有可用的Cookie，无法获取图片页面")
                return None
            
            # 构建URL
            url = f"{self.base_url}/ajax/illust/{illust_id}/pages?lang=zh"
            
            # 设置认证头
            headers = self.headers.copy()
            headers['Cookie'] = self.full_cookie
            
            if not self.session:
                raise RuntimeError("Session not initialized")
            
            logger.debug(f"获取图片页面 - ID: {illust_id}")
            
            response = await self.session.get(url, headers=headers)
            response.raise_for_status()
            
            pages_data = response.json()
            
            if pages_data.get('error'):
                logger.debug(f"获取图片页面失败: {pages_data.get('message', '未知错误')}")
                return None
            
            return pages_data.get('body', [])
            
        except Exception as e:
            logger.debug(f"获取图片页面异常: {e}")
            return None

    def _replace_image_url(self, url: str) -> str:
        """
        替换图片URL中的域名
        
        Args:
            url: 原始图片URL
            
        Returns:
            替换后的图片URL
        """
        if not url or not isinstance(url, str) or not config.image_proxy_host:
            return url
        return url.replace("i.pximg.net", config.image_proxy_host)

    async def get_illust_original_urls(self, illust_id: str) -> Optional[List[str]]:
        """
        获取图片的原始URL列表
        
        Args:
            illust_id: 图片ID
            
        Returns:
            原始图片URL列表，失败返回None
        """
        try:
            if illust_id in self._original_url_cache:
                return self._original_url_cache[illust_id]

            pages = await self.get_illust_pages(illust_id)
            if not pages:
                return None
            
            # 提取所有页面的original URL
            original_urls = []
            for page in pages:
                urls = page.get('urls', {})
                original_url = urls.get('original')
                if original_url:
                    # 🔥 替换URL域名
                    replaced_url = self._replace_image_url(original_url)
                    original_urls.append(replaced_url)
            
            self._original_url_cache[illust_id] = original_urls
            if len(self._original_url_cache) > 1000:
                oldest_key = next(iter(self._original_url_cache))
                del self._original_url_cache[oldest_key]

            logger.debug(f"获取到 {len(original_urls)} 个原始图片URL - ID: {illust_id}")
            return original_urls
            
        except Exception as e:
            logger.debug(f"获取原始图片URL异常: {e}")
            return None

    def parse_pixiv_time(self, time_str: str) -> Optional[datetime]:
        """
        解析Pixiv时间字符串（修复时区问题）
        
        Args:
            time_str: 时间字符串，如 "2025-11-22T12:49:30+09:00"
            
        Returns:
            UTC时间的datetime对象，失败返回None
        """
        if not time_str:
            return None
        try:
            if time_str.endswith("Z"):
                time_str = time_str[:-1] + "+00:00"
            parsed = datetime.fromisoformat(time_str)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError as e:
            logger.debug(f"时间解析失败: {time_str}, 错误: {e}")
            return None

    async def search_latest_illustrations(self, 
                                          search_keyword: str, 
                                          page: int = 1, 
                                          page_size: int = 30) -> Optional[Dict[str, Any]]:
        """
        简化版本的图片搜索（专注时效性，跳过复杂标签检查）
        
        Args:
            search_keyword: 搜索关键词
            page: 页码，默认为1
            page_size: 每页数量，默认为30
            
        Returns:
            精简的图片数据字典，失败返回None
        """
        try:
            # 限流等待
            await self._rate_limit_wait()
            
            if not search_keyword.strip():
                logger.debug("搜索关键词为空")
                self.last_error_reason = "empty_keyword"
                return None
            
            # 构建URL - 使用Pixiv的搜索API
            url = f"{self.base_url}/ajax/search/artworks/{search_keyword}"
            params = {
                'word': search_keyword,
                'order': 'date_d',  # 按日期降序排列
                'mode': 'all',
                'p': page,
                'csw': '0',
                's_mode': 's_tag',
                'type': 'all',
                'lang': 'zh',
                'ai_type': '1',
            }
            
            # 设置认证头
            headers = self.headers.copy()
            if self.full_cookie:
                headers['Cookie'] = self.full_cookie
            
            if not self.session:
                raise RuntimeError("Session not initialized")
            
            logger.debug(f"简化搜索图片 - 关键词: '{search_keyword}', 页码: {page}")
            
            response = await self.session.get(url, params=params, headers=headers)
            response.raise_for_status()
            
            raw_data = response.json()
            self.last_error_reason = None
            
            # 直接返回精简的数据结构（跳过复杂的标签检查）
            return await self._extract_latest_data(raw_data, search_keyword)
            
        except Exception as e:
            reason = self._record_http_error(e)
            logger.debug(f"简化搜索图片异常: {e} ({reason})")
            return None

    async def _extract_latest_data(self, 
                                             raw_data: Dict[str, Any], 
                                             search_keyword: str) -> Dict[str, Any]:
        """
        从原始API响应中提取精简的数据结构（简化版本）
        
        Args:
            raw_data: 原始API响应数据
            search_keyword: 搜索关键词
            
        Returns:
            精简后的数据结构
        """
        try:
            if not raw_data or 'body' not in raw_data:
                return {
                    'illusts': [],
                    'total': 0,
                    'lastPage': 1,
                    'searchInfo': {
                        'keyword': search_keyword,
                        'timestamp': datetime.now().isoformat()
                    }
                }
            
            body = raw_data['body']
            illust_manga = body.get('illustManga', {})
            
            # 提取图片数据 - 保留核心必要字段，加入翻译匹配的标签检查
            simplified_illusts = []
            blocked_count = 0
            
            for illust in illust_manga.get('data', []):
                # 使用现有的翻译匹配算法检查标签
                illust_tags = illust.get('tags', [])
                tag_translation = body.get('tagTranslation', {})
                
                if await self._should_block_image_with_translation(illust_tags, tag_translation=tag_translation):
                    blocked_count += 1
                    continue
                
                simplified_illust = {
                    # 🔴 核心必要数据
                    'id': illust.get('id'),
                    'title': illust.get('title'),
                    'url': self._replace_image_url(illust.get('url', '')),  # 🔥 对缩略图URL也进行反代处理
                    'tags': illust.get('tags', []),
                    'userId': illust.get('userId'),
                    'userName': illust.get('userName'),
                    'pageCount': illust.get('pageCount', 1),
                    'width': illust.get('width'),
                    'height': illust.get('height'),
                    'illustType': illust.get('illustType'),
                    'xRestrict': illust.get('xRestrict', 0),
                    
                    # 🟡 重要数据
                    'description': illust.get('description', ''),
                    'createDate': illust.get('createDate'),
                    'aiType': illust.get('aiType', 0),
                    'profileImageUrl': illust.get('profileImageUrl', '')
                }
                simplified_illusts.append(simplified_illust)
            
            # 记录屏蔽统计
            if blocked_count > 0:
                logger.debug(f"翻译Tag屏蔽: 过滤了 {blocked_count} 个包含屏蔽tag的作品")
            
            # 构建精简的数据结构
            simplified_data = {
                # 🔴 核心数据
                'illusts': simplified_illusts,
                'total': illust_manga.get('total', 0),
                'lastPage': illust_manga.get('lastPage', 1),
                
                # 搜索元信息
                'searchInfo': {
                    'keyword': search_keyword,
                    'timestamp': datetime.now().isoformat()
                }
            }
            
            logger.debug(f"简化搜索完成: 找到 {len(simplified_illusts)} 个结果，总计 {simplified_data['total']} 个")
            return simplified_data
            
        except Exception as e:
            logger.debug(f"提取精简数据异常: {e}")
            return {
                'illusts': [],
                'total': 0,
                'lastPage': 1,
                'searchInfo': {
                    'keyword': search_keyword,
                    'timestamp': datetime.now().isoformat()
                }
            }

    async def get_image_info(self, illust_id: str) -> Optional[Dict[str, Any]]:
        """
        根据图片ID返回图片信息（基础功能）
        
        Args:
            illust_id: 图片ID
            
        Returns:
            图片信息字典，包含基本信息和URL，失败返回None
        """
        try:
            if not illust_id or not str(illust_id).strip():
                logger.debug("图片ID不能为空")
                return None
            
            illust_id = str(illust_id).strip()
            logger.debug(f"获取图片信息 - ID: {illust_id}")
            
            # 获取图片详细信息
            illust_details = await self.get_illust_details(illust_id)
            if not illust_details:
                logger.debug(f"获取图片详情失败: {illust_id}")
                return None
            
            # 获取原始图片URL
            original_urls = await self.get_illust_original_urls(illust_details.get('id', ''))
            primary_url = original_urls[0] if original_urls else illust_details.get('url', '')
            
            # 🔥 修复：处理tags字段的复杂结构
            # 根据错误日志，tags可能是一个包含tags数组的复杂对象
            tags_data = illust_details.get('tags', [])
            if isinstance(tags_data, dict) and 'tags' in tags_data:
                # 如果tags是字典，提取其中的tags数组
                tags_array = tags_data.get('tags', [])
                if isinstance(tags_array, list):
                    # 提取tag名称
                    processed_tags = []
                    for tag_item in tags_array:
                        if isinstance(tag_item, dict):
                            tag_name = tag_item.get('tag', '')
                            if tag_name:
                                processed_tags.append(tag_name)
                        elif isinstance(tag_item, str):
                            processed_tags.append(tag_item)
                    tags_field = processed_tags
                else:
                    tags_field = []
            elif isinstance(tags_data, list):
                # 如果tags已经是数组，检查是否需要处理
                if tags_data and isinstance(tags_data[0], dict):
                    # 如果数组元素是字典，提取tag名称
                    tags_field = [item.get('tag', '') if isinstance(item, dict) else str(item) for item in tags_data]
                else:
                    # 如果数组元素是字符串，直接使用
                    tags_field = [str(tag) for tag in tags_data]
            else:
                tags_field = []

            urls_field = original_urls if original_urls else [primary_url]    
            
            # 🔥 修复：构建标准化的图片信息
            image_info = {
                'id': illust_details.get('id', ''),
                'title': illust_details.get('title', ''),
                'urls': urls_field,  # 🔥 统一使用urls字段
                'tags': tags_field,  # 🔥 处理后的tags数组
                'userId': illust_details.get('userId', ''),
                'userName': illust_details.get('userName', ''),
                'pageCount': illust_details.get('pageCount', 1),
                'width': illust_details.get('width', 0),
                'height': illust_details.get('height', 0),
                'illustType': illust_details.get('illustType', 0),
                'xRestrict': illust_details.get('xRestrict', 0),
                'description': illust_details.get('description', ''),
                'createDate': illust_details.get('createDate', ''),
                'aiType': illust_details.get('aiType', 0),
                'profileImageUrl': illust_details.get('profileImageUrl', ''),
                'source': 'image_info_api',
                'bookmarkCount': illust_details.get('bookmarkCount', 0),
                'likeCount': illust_details.get('likeCount', 0),
                'viewCount': illust_details.get('viewCount', 0),
                'commentCount': illust_details.get('commentCount', 0)
            }
            
            logger.debug(f"成功获取图片信息 - ID: {illust_id}, 标题: {image_info['title']}, URL数量: {len(urls_field)}, Tags数量: {len(tags_field)}")
            return image_info
            
        except Exception as e:
            logger.debug(f"获取图片信息异常: {e}")
            return None

    async def get_author_info(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        根据作者ID返回作者信息（基础功能）
        
        Args:
            user_id: 作者ID
            
        Returns:
            作者信息字典，包含基本信息和作品统计，失败返回None
        """
        try:
            if not user_id or not str(user_id).strip():
                logger.debug("作者ID不能为空")
                return None
            
            user_id = str(user_id).strip()
            logger.debug(f"获取作者信息 - ID: {user_id}")
            
            # 获取用户详细信息
            user_info = await self.get_user_info(user_id)
            if not user_info:
                logger.debug(f"获取用户信息失败: {user_id}")
                return None
            
            # 获取作者作品信息
            author_profile = await self.get_author_profile(user_id)
            
            # 提取作品统计信息
            total_works = 0
            if author_profile and 'illusts' in author_profile:
                illusts_dict = author_profile.get('illusts', {})
                if isinstance(illusts_dict, dict):
                    total_works = len(illusts_dict)
            
            # 获取作者最新5张图片的实际URL
            latest_images = []
            if author_profile and 'illusts' in author_profile:
                illusts_dict = author_profile.get('illusts', {})
                if isinstance(illusts_dict, dict):
                    # 转换为列表并按时间排序
                    all_illusts = list(illusts_dict.values())
                    sorted_illusts = sorted(
                        all_illusts,
                        key=lambda x: x.get('createDate', ''),
                        reverse=True
                    )
                    
                    # 获取最新5张图片的URL
                    for illust in sorted_illusts[:5]:
                        image_id = illust.get('id', '')
                        if image_id:
                            original_urls = await self.get_illust_original_urls(image_id)
                            image_info = {
                                'id': image_id,
                                'title': illust.get('title', ''),
                                'urls': original_urls if original_urls else [],
                                'createDate': illust.get('createDate', ''),
                                'pageCount': illust.get('pageCount', 1),
                                'tags': illust.get('tags', [])
                            }
                            latest_images.append(image_info)
            
            # 构建简化的作者信息
            author_info = {
                'userId': user_info.get('body', {}).get('userId', user_id),
                'name': user_info.get('body', {}).get('name', ''),
                'comment': user_info.get('body', {}).get('comment', ''),
                'image': user_info.get('body', {}).get('image', ''),
                'imageBig': user_info.get('body', {}).get('imageBig', ''),
                'background': user_info.get('body', {}).get('background', ''),
                'isFollowed': user_info.get('body', {}).get('isFollowed', False),
                'isMypixiv': user_info.get('body', {}).get('isMypixiv', False),
                'isPremium': user_info.get('body', {}).get('isPremium', False),
                'totalWorks': total_works,
                'followable': author_profile.get('followable', False) if author_profile else False,
                'acceptRequest': author_profile.get('acceptRequest', False) if author_profile else False,
                'latestImages': latest_images  # 添加最新5张图片信息
            }
            
            logger.debug(f"成功获取作者信息 - ID: {user_id}, 名称: {author_info['name']}, 作品数: {total_works}")
            return author_info
            
        except Exception as e:
            logger.debug(f"获取作者信息异常: {e}")
            return None
