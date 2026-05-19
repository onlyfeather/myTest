import asyncio
import json
import httpx
import random
import time
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List, Set, Union
from nonebot import get_driver, logger
from .pixiv_storage import PixivStorage


@get_driver().on_shutdown
async def _shutdown_pixiv_session():
    if PixivSpider._shared_session:
        await PixivSpider._shared_session.aclose()
        PixivSpider._shared_session = None


class PixivSpider:
    """Pixiv爬虫类，支持Cookie登录"""

    _shared_session: Optional[httpx.AsyncClient] = None
    _shared_loaded = False
    _shared_state: Dict[str, Any] = {}
    _file_lock = asyncio.Lock()
    _rate_limit_lock = asyncio.Lock()
    _last_request_time = 0.0
    _detail_semaphore = asyncio.Semaphore(3)
    _original_url_cache: Dict[str, List[str]] = {}
    
    def __init__(self, storage: Optional[PixivStorage] = None):
        self.session = None
        self.base_url = "https://www.pixiv.net"
        self.storage = storage or PixivStorage()
        self.full_cookie = None
        self.user_id = None
        self.is_logged_in = False
        self.last_error_reason = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Content-Type': 'application/json',
            'Origin': 'https://www.pixiv.net',
            'Referer': 'https://www.pixiv.net/'
        }
        
        # 限流配置
        self.last_request_time = 0
        self.min_request_interval = 0.5  # 最小请求间隔（秒）- 降低到0.5秒提高性能
        self.rate_limit_enabled = True  # 是否启用限流
        
        # 翻译缓存
        self.translation_cache: Dict[str, Dict[str, str]] = {}  # 缓存翻译结果
        self.cache_max_size = 1000  # 最大缓存数量
        
        # Tag池配置
        self.preferred_tags: Set[str] = set()
        self.blocked_tags: Set[str] = set()
        
        # 喜欢作者配置
        self.favorite_authors: Dict[str, Dict[str, Any]] = {}  # {user_id: {name, aliases, added_time, last_check}}
        
        # 圈名到作者ID的映射缓存
        self.alias_to_user_id: Dict[str, str] = {}  # {alias: user_id}
        
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

    def _publish_state(self):
        PixivSpider._shared_state = {
            "full_cookie": self.full_cookie,
            "user_id": self.user_id,
            "is_logged_in": self.is_logged_in,
            "last_error_reason": self.last_error_reason,
            "preferred_tags": self.preferred_tags,
            "blocked_tags": self.blocked_tags,
            "favorite_authors": self.favorite_authors,
            "alias_to_user_id": self.alias_to_user_id,
            "translation_cache": self.translation_cache,
        }

    def _use_shared_state(self):
        state = PixivSpider._shared_state
        self.full_cookie = state.get("full_cookie")
        self.user_id = state.get("user_id")
        self.is_logged_in = state.get("is_logged_in", False)
        self.last_error_reason = state.get("last_error_reason")
        self.preferred_tags = state.get("preferred_tags", set())
        self.blocked_tags = state.get("blocked_tags", set())
        self.favorite_authors = state.get("favorite_authors", {})
        self.alias_to_user_id = state.get("alias_to_user_id", {})
        self.translation_cache = state.get("translation_cache", {})
        
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
            self._publish_state()
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
        self._publish_state()
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
            self._publish_state()
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
    
    def get_random_preferred_tag(self) -> Optional[str]:
        """从喜好tag池中随机选择一个tag"""
        try:
            if not self.preferred_tags:
                logger.debug("喜好tag池为空")
                return None
            return random.choice(list(self.preferred_tags))
        except Exception as e:
            logger.debug(f"随机选择喜好tag失败: {e}")
            return None
    
    def get_tag_pools_info(self) -> Dict[str, Any]:
        """获取tag池信息"""
        return {
            'preferred_tags': list(self.preferred_tags),
            'blocked_tags': list(self.blocked_tags),
            'preferred_count': len(self.preferred_tags),
            'blocked_count': len(self.blocked_tags)
        }

    async def get_tag_translation(self, tag: str) -> Dict[str, str]:
        """
        获取tag的翻译信息（带缓存优化）
        
        Args:
            tag: 标签名称
            
        Returns:
            翻译字典，失败返回空字典
        """
        try:
            if not tag or not tag.strip():
                return {}
            
            clean_tag = tag.strip()
            
            # 检查缓存
            if clean_tag in self.translation_cache:
                logger.debug(f"使用翻译缓存: {clean_tag}")
                return self.translation_cache[clean_tag]
            
            # 限流等待
            await self._rate_limit_wait()
            
            url = f"{self.base_url}/ajax/search/tags/{clean_tag}?lang=zh"
            
            # 设置认证头（如果有的话）
            headers = self.headers.copy()
            if self.full_cookie:
                headers['Cookie'] = self.full_cookie
            
            if not self.session:
                raise RuntimeError("Session not initialized")
            
            logger.debug(f"请求翻译API: {clean_tag}")
            response = await self.session.get(url, headers=headers)
            response.raise_for_status()
            
            tag_data = response.json()
            
            if tag_data.get('error'):
                return {}
            
            body = tag_data.get('body', {})
            tag_translation = body.get('tagTranslation', {})
            
            # 处理不同的返回格式
            translations = {}
            if isinstance(tag_translation, dict):
                # 标准格式：{tag: {translations}}
                translations = tag_translation.get(clean_tag, {})
            elif isinstance(tag_translation, list):
                # 列表格式：[{tag: translations}, ...]
                for item in tag_translation:
                    if isinstance(item, dict) and clean_tag in item:
                        translations = item[clean_tag]
                        break
            
            # 确保translations是字典格式
            if not isinstance(translations, dict):
                translations = {}
            
            # 构建翻译结果
            translation_result = {
                'en': translations.get('en', ''),
                'zh': translations.get('zh', ''),
                'ko': translations.get('ko', ''),
                'zh_tw': translations.get('zh_tw', ''),
                'romaji': translations.get('romaji', '')
            }
            
            # 缓存结果（限制缓存大小）
            if len(self.translation_cache) >= self.cache_max_size:
                # 清理最旧的缓存项（简单的FIFO策略）
                oldest_key = next(iter(self.translation_cache))
                del self.translation_cache[oldest_key]
                logger.debug(f"清理翻译缓存: {oldest_key}")
            
            self.translation_cache[clean_tag] = translation_result
            logger.debug(f"缓存翻译结果: {clean_tag}")
            
            return translation_result
            
        except Exception as e:
            logger.debug(f"获取tag翻译异常: {e}")
            return {}

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
                            import re
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

    def _should_block_image(self, illust_tags: List[str]) -> bool:
        """
        检查图片是否应该被屏蔽（基于tag，不包含翻译检查）
        
        Args:
            illust_tags: 图片的tag列表
            
        Returns:
            True表示应该屏蔽，False表示不屏蔽
        """
        try:
            if not illust_tags or not self.blocked_tags:
                return False
            
            # 将图片tag转换为小写集合，便于比较
            illust_tags_lower = {tag.lower().strip() for tag in illust_tags if tag and tag.strip()}
            blocked_tags_lower = {tag.lower().strip() for tag in self.blocked_tags if tag and tag.strip()}
            
            # 检查是否有任何屏蔽tag匹配
            for blocked_tag in blocked_tags_lower:
                # 精确匹配
                if blocked_tag in illust_tags_lower:
                    return True
                
                # 模糊匹配（检查是否包含屏蔽tag作为子字符串）
                for illust_tag in illust_tags_lower:
                    if blocked_tag in illust_tag or illust_tag in blocked_tag:
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
            self._publish_state()
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
    
    def get_favorite_authors(self) -> Dict[str, Dict[str, Any]]:
        """获取所有喜欢作者"""
        return self.favorite_authors.copy()
    
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
                self._publish_state()
                return True
            return False
        except Exception as e:
            logger.exception(f"更新作者检查时间失败: {e}")
            return False
    
    def _update_alias_mapping(self) -> None:
        """更新圈名到作者ID的映射缓存"""
        try:
            self.alias_to_user_id.clear()
            
            for user_id, author_info in self.favorite_authors.items():
                aliases = author_info.get('aliases', [])
                for alias in aliases:
                    if alias and alias.strip():
                        clean_alias = alias.strip()
                        # 检查圈名冲突
                        if clean_alias in self.alias_to_user_id:
                            existing_user_id = self.alias_to_user_id[clean_alias]
                            if existing_user_id != user_id:
                                logger.warning(f"圈名 '{clean_alias}' 冲突 - 已存在作者 {existing_user_id}，将被作者 {user_id} 覆盖")
                        self.alias_to_user_id[clean_alias] = user_id
            
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
            
            if user_id not in self.favorite_authors:
                logger.info(f"作者 {user_id} 不在喜欢列表中")
                return False
            
            # 检查圈名冲突
            if alias in self.alias_to_user_id and self.alias_to_user_id[alias] != user_id:
                existing_user_id = self.alias_to_user_id[alias]
                logger.info(f"圈名 '{alias}' 已被作者 {existing_user_id} 使用")
                return False
            
            # 添加圈名
            author_info = self.favorite_authors[user_id]
            aliases = author_info.get('aliases', [])
            
            if alias not in aliases:
                aliases.append(alias)
                author_info['aliases'] = aliases
                self.alias_to_user_id[alias] = user_id
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
            
            if user_id not in self.favorite_authors:
                logger.info(f"作者 {user_id} 不在喜欢列表中")
                return False
            
            # 移除圈名
            author_info = self.favorite_authors[user_id]
            aliases = author_info.get('aliases', [])
            
            if alias in aliases:
                aliases.remove(alias)
                author_info['aliases'] = aliases
                
                # 从映射中移除
                if alias in self.alias_to_user_id and self.alias_to_user_id[alias] == user_id:
                    del self.alias_to_user_id[alias]
                
                if not self.save_favorite_authors():
                    return False
                logger.info(f"已为作者 {user_id} 移除圈名: {alias}")
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
            
            return self.alias_to_user_id.get(alias)
        except Exception as e:
            logger.debug(f"通过圈名搜索作者失败: {e}")
            return None

    def get_author_aliases(self, user_id: str) -> List[str]:
        """获取作者的所有圈名"""
        try:
            user_id = str(user_id).strip()
            if not user_id:
                return []
            
            author_info = self.favorite_authors.get(user_id, {})
            return author_info.get('aliases', []).copy()
        except Exception as e:
            logger.debug(f"获取作者圈名失败: {e}")
            return []

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
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        if not PixivSpider._shared_loaded:
            async with PixivSpider._file_lock:
                if not PixivSpider._shared_loaded:
                    self.load_cookie()
                    self.load_tag_pools()
                    self.load_favorite_authors()
                    self._update_alias_mapping()
                    self._publish_state()
                    PixivSpider._shared_loaded = True
        else:
            self._use_shared_state()

        if not PixivSpider._shared_session:
            PixivSpider._shared_session = httpx.AsyncClient(
                headers=self.headers,
                timeout=30.0,
                follow_redirects=True
            )
        self.session = PixivSpider._shared_session
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        self._publish_state()
        self.session = None
    
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
            
            logger.debug(f"成功获取作者作品数据")
            return author_data.get('body')
            
        except Exception as e:
            logger.debug(f"获取作者作品异常: {e}")
            return None
    
    async def _rate_limit_wait(self):
        """限流等待"""
        if not self.rate_limit_enabled:
            return

        async with PixivSpider._rate_limit_lock:
            current_time = time.time()
            time_since_last_request = current_time - PixivSpider._last_request_time

            if time_since_last_request < self.min_request_interval:
                wait_time = self.min_request_interval - time_since_last_request
                logger.debug(f"[Pixiv] Rate limit wait: {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

            PixivSpider._last_request_time = time.time()

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
                self._publish_state()
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
                import re
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
                        'timestamp': __import__('datetime').datetime.now().isoformat()
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
                    'timestamp': __import__('datetime').datetime.now().isoformat()
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
                    'timestamp': __import__('datetime').datetime.now().isoformat()
                }
            }

    def _get_daily_seed(self, user_qq: str) -> int:
        """
        生成用户每日固定种子
        
        Args:
            user_qq: 用户QQ号
            
        Returns:
            确定性的种子数字
        """
        today = datetime.now().strftime("%Y%m%d")  # 如: 20241121
        seed_string = f"{user_qq}_{today}"
        
        # 使用MD5哈希确保确定性
        hash_obj = hashlib.md5(seed_string.encode())
        hash_hex = hash_obj.hexdigest()
        
        # 取前8位十六进制转为整数
        return int(hash_hex[:8], 16)

    def _select_tag_by_seed(self, preferred_tags: List[str], seed: int) -> Optional[str]:
        """
        根据种子选择tag
        
        Args:
            preferred_tags: 喜好tag列表
            seed: 种子数字
            
        Returns:
            选中的tag，失败返回None
        """
        if not preferred_tags:
            return None
        
        tag_index = seed % len(preferred_tags)
        return preferred_tags[tag_index]

    def _select_image_by_seed(self, popular_images: List[Dict], seed: int) -> Optional[Dict]:
        """
        根据种子选择图片
        
        Args:
            popular_images: 热门图片列表
            seed: 种子数字
            
        Returns:
            选中的图片信息
        """
        if not popular_images:
            return None
        
        image_index = seed % len(popular_images)
        return popular_images[image_index]

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
            if not create_date_str:
                return 24 * 30  # 默认30天
            
            # 解析时间字符串 (格式: "2025-11-21T06:12:32+09:00")
            from datetime import datetime
            import re
            
            # 提取主要时间部分，忽略时区
            time_match = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', create_date_str)
            if not time_match:
                return 24 * 30
            
            create_time = datetime.fromisoformat(time_match.group(1))
            current_time = datetime.now()
            
            # 计算时间差（小时）
            time_diff = current_time - create_time
            hours = time_diff.total_seconds() / 3600
            
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
        async with PixivSpider._detail_semaphore:
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
        async with PixivSpider._detail_semaphore:
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
        try:
            if not url or not isinstance(url, str):
                return url
            
            # 将 i.pximg.net 替换为 i.yuki.sh
            replaced_url = url.replace('i.pximg.net', 'i.yuki.sh')
            
            # 如果发生了替换，记录日志
            if replaced_url != url:
                logger.debug(f"URL替换: {url} -> {replaced_url}")
            
            return replaced_url
        except Exception as e:
            logger.debug(f"URL替换异常: {e}")
            return url

    def _ensure_proxy_url(self, url_or_urls: Union[str, List[str]]) -> List[str]:
        """
        确保URL使用反代，统一返回URL列表
        
        Args:
            url_or_urls: 单个URL字符串或URL列表
            
        Returns:
            经过反代处理的URL列表
        """
        try:
            if not url_or_urls:
                return []
            
            result_urls = []
            
            if isinstance(url_or_urls, str):
                # 单个URL字符串
                if url_or_urls.strip():
                    replaced_url = self._replace_image_url(url_or_urls.strip())
                    result_urls.append(replaced_url)
            elif isinstance(url_or_urls, list):
                # URL列表
                for url in url_or_urls:
                    if isinstance(url, str) and url.strip():
                        replaced_url = self._replace_image_url(url.strip())
                        result_urls.append(replaced_url)
                    elif url:  # 非字符串类型，尝试转换
                        url_str = str(url)
                        if url_str.strip():
                            replaced_url = self._replace_image_url(url_str.strip())
                            result_urls.append(replaced_url)
                            logger.debug(f"URL类型转换: {type(url)} -> str, 值: {replaced_url}")
            else:
                # 其他类型，尝试转换为字符串
                url_str = str(url_or_urls)
                if url_str.strip():
                    replaced_url = self._replace_image_url(url_str.strip())
                    result_urls.append(replaced_url)
                    logger.debug(f"URL类型转换: {type(url_or_urls)} -> str, 值: {replaced_url}")
            
            return result_urls
            
        except Exception as e:
            logger.debug(f"确保反代URL异常: {e}")
            return []

    async def get_illust_original_urls(self, illust_id: str) -> Optional[List[str]]:
        """
        获取图片的原始URL列表
        
        Args:
            illust_id: 图片ID
            
        Returns:
            原始图片URL列表，失败返回None
        """
        try:
            if illust_id in PixivSpider._original_url_cache:
                return PixivSpider._original_url_cache[illust_id]

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
            
            PixivSpider._original_url_cache[illust_id] = original_urls
            if len(PixivSpider._original_url_cache) > 1000:
                oldest_key = next(iter(PixivSpider._original_url_cache))
                del PixivSpider._original_url_cache[oldest_key]

            logger.debug(f"获取到 {len(original_urls)} 个原始图片URL - ID: {illust_id}")
            return original_urls
            
        except Exception as e:
            logger.debug(f"获取原始图片URL异常: {e}")
            return None

    async def search_images(self, 
                           tags: Union[str, List[str]], 
                           mode: str = "random", 
                           count: int = 1, 
                           min_quality_score: float = 60.0,
                           max_attempts: int = 10,
                           min_bookmarks: int = 0) -> Optional[Dict[str, Any]]:
        """
        搜图机器人核心功能
        
        Args:
            tags: 搜索标签，可以是字符串（单tag）或列表（多tag）
            mode: 搜图模式
                - "random": 随机图（默认），在搜索结果中随机选择，检查质量评分
                - "recent": 近日美图，从popular.recent中选择
                - "popular": 美图，从popular.permanent中选择
            count: 返回图片数量，1-5张，默认1张
            min_quality_score: 最低质量评分要求，仅对random模式有效，默认60分
            max_attempts: 最大尝试次数，仅对random模式有效，默认10次
            
        Returns:
            搜图结果字典，包含图片列表、统计信息等，失败返回None
        """
        try:
            # 参数验证
            if count < 1 or count > 5:
                logger.debug(f"图片数量必须在1-5之间，当前值: {count}")
                return None
            
            if mode not in ["random", "recent", "popular"]:
                logger.debug(f"不支持的搜图模式: {mode}，支持的模式: random, recent, popular")
                return None
            
            # 处理标签
            if isinstance(tags, str):
                tags = [tags.strip()] if tags.strip() else []
            elif isinstance(tags, list):
                tags = [tag.strip() for tag in tags if tag and tag.strip()]
            else:
                logger.debug("标签参数必须是字符串或字符串列表")
                return None
            
            if not tags:
                logger.debug("搜索标签不能为空")
                return None
            
            search_keyword = ' '.join(tags)
            logger.debug(f"开始搜图 - 关键词: '{search_keyword}', 模式: {mode}, 数量: {count}, 收藏门槛: >{min_bookmarks}")
            
            # 执行搜索
            search_result = await self.search_illustrations(search_keyword)
            if not search_result:
                logger.debug(f"搜索失败: {search_keyword}")
                if not self.last_error_reason:
                    self.last_error_reason = "search_failed"
                return None
            
            # 根据模式获取图片
            if mode == "random":
                images = await self._get_random_images(
                    search_result, count, min_quality_score, max_attempts, min_bookmarks
                )
            elif mode == "recent":
                images = await self._get_recent_popular_images(search_result, count)
            elif mode == "popular":
                images = await self._get_permanent_popular_images(search_result, count)
            else:
                images = []
            
            if not images:
                logger.debug(f"未找到符合条件的图片")
                self.last_error_reason = "no_qualified_images"
                return None
            
            # 构建返回结果
            result = {
                'search_info': {
                    'tags': tags,
                    'search_keyword': search_keyword,
                    'mode': mode,
                    'requested_count': count,
                    'actual_count': len(images),
                    'min_quality_score': min_quality_score if mode == "random" else None,
                    'min_bookmarks': min_bookmarks,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                },
                'images': images,
                'statistics': {
                    'total_search_results': search_result.get('total', 0),
                    'popular_recent_count': len(search_result.get('popular', {}).get('recent', [])),
                    'popular_permanent_count': len(search_result.get('popular', {}).get('permanent', [])),
                    'regular_illust_count': len(search_result.get('illusts', []))
                }
            }
            
            logger.debug(f"搜图完成 - 找到 {len(images)} 张图片")
            self.last_error_reason = None
            return result
            
        except Exception as e:
            reason = self._record_http_error(e)
            logger.debug(f"搜图异常: {e} ({reason})")
            return None

    async def _get_random_images(self, 
                                search_result: Dict[str, Any], 
                                count: int, 
                                min_quality_score: float,
                                max_attempts: int,
                                min_bookmarks: int = 0) -> List[Dict[str, Any]]:
        """
        获取随机图片（带质量评分检查，支持小众tag降级策略）
        
        Args:
            search_result: 搜索结果
            count: 需要的图片数量
            min_quality_score: 最低质量评分
            max_attempts: 最大尝试次数
            
        Returns:
            符合条件的图片列表
        """
        try:
            images = []
            attempts = 0
            used_image_ids = set()
            failed_candidates = []  # 存储不符合要求的候选图片
            
            # 优先从热门作品中选择
            all_candidates = []
            
            # 添加热门作品到候选池
            popular_recent = search_result.get('popular', {}).get('recent', [])
            popular_permanent = search_result.get('popular', {}).get('permanent', [])
            regular_illusts = search_result.get('illusts', [])
            
            # 候选池优先级：热门近期 > 热门永久 > 普通作品
            all_candidates.extend(popular_recent)
            all_candidates.extend(popular_permanent)
            all_candidates.extend(regular_illusts)
            
            if not all_candidates:
                logger.debug("没有找到任何候选图片")
                return []
            
            logger.debug(f"候选图片池: {len(all_candidates)} 张（热门近期: {len(popular_recent)}, 热门永久: {len(popular_permanent)}, 普通: {len(regular_illusts)}）")
            
            # 第一阶段：尝试找到符合质量要求的图片
            while len(images) < count and attempts < max_attempts:
                attempts += 1
                
                # 随机选择候选图片
                if not all_candidates:
                    break
                
                candidate = random.choice(all_candidates)
                image_id = candidate.get('id')
                
                if not image_id or image_id in used_image_ids:
                    continue
                
                used_image_ids.add(image_id)
                
                logger.debug(f"尝试第 {attempts} 次 - 检查图片 ID: {image_id}")
                
                # 获取详细信息和质量评分
                quality_score = None
                try:
                    illust_details = await self.get_illust_details(image_id)
                    if illust_details:
                        quality_score = self.calculate_quality_score(illust_details)
                        logger.debug(f"质量评分: {quality_score['total_score']} ({quality_score['quality_level']})")
                    else:
                        logger.debug("获取图片详情失败，跳过质量评分")
                        quality_score = None
                except Exception as e:
                    logger.debug(f"质量评分计算异常: {e}")
                    quality_score = None
                
                bookmark_count = 0
                if illust_details:
                    bookmark_count = int(illust_details.get('bookmarkCount', 0) or 0)

                # 检查质量评分和收藏数
                if (
                    quality_score
                    and quality_score['total_score'] >= min_quality_score
                    and bookmark_count > min_bookmarks
                ):
                    # 获取原始图片URL
                    original_urls = await self.get_illust_original_urls(image_id)
                    primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                    
                    # 构建图片信息（urls字段返回URL数组）
                    image_info = {
                        'id': image_id,
                        'title': candidate.get('title', ''),
                        'urls': original_urls if original_urls else [primary_url],  # 🔥 改造为URL数组
                        'tags': candidate.get('tags', []),
                        'userId': candidate.get('userId', ''),
                        'userName': candidate.get('userName', ''),
                        'pageCount': candidate.get('pageCount', 1),
                        'width': candidate.get('width', 0),
                        'height': candidate.get('height', 0),
                        'illustType': candidate.get('illustType', 0),
                        'xRestrict': candidate.get('xRestrict', 0),
                        'description': candidate.get('description', ''),
                        'createDate': candidate.get('createDate', ''),
                        'aiType': candidate.get('aiType', 0),
                        'profileImageUrl': candidate.get('profileImageUrl', ''),
                        'source': 'random_selection',
                        'bookmarkCount': bookmark_count
                    }
                    
                    images.append(image_info)
                    logger.debug(f"✅ 图片符合要求 - ID: {image_id}, 评分: {quality_score['total_score']}, 收藏: {bookmark_count}")
                else:
                    # 存储不符合要求的候选图片，用于降级处理
                    failed_candidates.append({
                        'candidate': candidate,
                        'quality_score': quality_score,
                        'reason': 'low_bookmarks' if bookmark_count <= min_bookmarks else ('low_quality' if quality_score else 'no_score'),
                        'bookmark_count': bookmark_count
                    })
                    score_text = f"{quality_score['total_score']}" if quality_score else "无评分"
                    logger.debug(f"❌ 图片不符合要求 - ID: {image_id}, 评分: {score_text} < {min_quality_score} 或收藏 {bookmark_count} <= {min_bookmarks}")
            
            # 第二阶段：无收藏门槛时保留旧降级策略；有收藏门槛时不降级破坏质量要求
            if min_bookmarks <= 0 and len(images) < count and failed_candidates:
                logger.debug(f"🔄 启用降级策略 - 高质量图片不足，从剩余候选中选择最佳图片")
                
                # 按质量评分排序失败的候选图片（有评分的优先）
                failed_candidates.sort(key=lambda x: (
                    0 if x['quality_score'] else 1,  # 有评分的优先
                    -(x['quality_score']['total_score'] if x['quality_score'] else 0)  # 按评分降序
                ))
                
                # 补充剩余需要的图片
                needed = count - len(images)
                for i, failed_item in enumerate(failed_candidates[:needed]):
                    candidate = failed_item['candidate']
                    quality_score = failed_item['quality_score']
                    image_id = candidate.get('id', '')
                    
                    # 🔥 降级时也获取原始图片URL
                    original_urls = await self.get_illust_original_urls(image_id)
                    primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                    
                    image_info = {
                        'id': image_id,
                        'title': candidate.get('title', ''),
                        'urls': original_urls if original_urls else [primary_url],  # 🔥 返回实际图片URL数组
                        'tags': candidate.get('tags', []),
                        'userId': candidate.get('userId', ''),
                        'userName': candidate.get('userName', ''),
                        'pageCount': candidate.get('pageCount', 1),
                        'width': candidate.get('width', 0),
                        'height': candidate.get('height', 0),
                        'illustType': candidate.get('illustType', 0),
                        'xRestrict': candidate.get('xRestrict', 0),
                        'description': candidate.get('description', ''),
                        'createDate': candidate.get('createDate', ''),
                        'aiType': candidate.get('aiType', 0),
                        'profileImageUrl': candidate.get('profileImageUrl', ''),
                        'source': 'fallback_selection'
                    }
                    
                    images.append(image_info)
                    score_text = f"{quality_score['total_score']}" if quality_score else "无评分"
                    logger.debug(f"🔄 降级选择 - ID: {candidate.get('id')}, 评分: {score_text} (原因: {failed_item['reason']})")
            
            # 第三阶段：如果仍然不足，直接从剩余候选中随机选择
            if min_bookmarks <= 0 and len(images) < count:
                logger.debug(f"🔄 最终降级 - 仍然不足，从剩余候选中随机选择")
                
                # 获取未使用的候选图片
                remaining_candidates = [
                    candidate for candidate in all_candidates 
                    if candidate.get('id') not in [img['id'] for img in images]
                ]
                
                needed = count - len(images)
                selected = random.sample(remaining_candidates, min(needed, len(remaining_candidates)))
                
                for candidate in selected:
                    image_id = candidate.get('id', '')
                    
                    # 🔥 最终降级时也获取原始图片URL
                    original_urls = await self.get_illust_original_urls(image_id)
                    primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                    
                    image_info = {
                        'id': image_id,
                        'title': candidate.get('title', ''),
                        'url': original_urls if original_urls else [primary_url],  # 🔥 返回实际图片URL
                        'tags': candidate.get('tags', []),
                        'userId': candidate.get('userId', ''),
                        'userName': candidate.get('userName', ''),
                        'pageCount': candidate.get('pageCount', 1),
                        'width': candidate.get('width', 0),
                        'height': candidate.get('height', 0),
                        'illustType': candidate.get('illustType', 0),
                        'xRestrict': candidate.get('xRestrict', 0),
                        'description': candidate.get('description', ''),
                        'createDate': candidate.get('createDate', ''),
                        'aiType': candidate.get('aiType', 0),
                        'profileImageUrl': candidate.get('profileImageUrl', ''),
                        'source': 'author_final'
                    }
                    
                    images.append(image_info)
                    logger.debug(f"🔄 最终选择 - ID: {candidate.get('id')}, 无质量评分")
            
            logger.debug(f"随机模式完成 - 尝试 {attempts} 次，找到 {len(images)} 张图片")
            
            # 统计不同来源的图片数量
            source_count = {}
            for img in images:
                source = img.get('source', 'unknown')
                source_count[source] = source_count.get(source, 0) + 1
            
            logger.debug(f"图片来源统计: {dict(source_count)}")
            
            return images
            
        except Exception as e:
            logger.debug(f"获取随机图片异常: {e}")
            return []

    async def _get_recent_popular_images(self, search_result: Dict[str, Any], count: int) -> List[Dict[str, Any]]:
        """
        获取近日美图（从popular.recent中选择）
        
        Args:
            search_result: 搜索结果
            count: 需要的图片数量
            
        Returns:
            图片列表
        """
        try:
            popular_recent = search_result.get('popular', {}).get('recent', [])
            
            if not popular_recent:
                logger.debug("没有找到近日热门作品")
                return []
            
            logger.debug(f"近日热门作品池: {len(popular_recent)} 张")
            
            # 随机选择指定数量的图片
            selected_count = min(count, len(popular_recent))
            selected_images = random.sample(popular_recent, selected_count)
            
            # 构建图片信息
            images = []
            for candidate in selected_images:
                # 获取原始图片URL
                original_urls = await self.get_illust_original_urls(candidate.get('id', ''))
                primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                
                image_info = {
                    'id': candidate.get('id', ''),
                    'title': candidate.get('title', ''),
                    'urls': original_urls if original_urls else [primary_url],  # 🔥 改造为URL数组
                    'tags': candidate.get('tags', []),
                    'userId': candidate.get('userId', ''),
                    'userName': candidate.get('userName', ''),
                    'pageCount': candidate.get('pageCount', 1),
                    'width': candidate.get('width', 0),
                    'height': candidate.get('height', 0),
                    'illustType': candidate.get('illustType', 0),
                    'xRestrict': candidate.get('xRestrict', 0),
                    'description': candidate.get('description', ''),
                    'createDate': candidate.get('createDate', ''),
                    'aiType': candidate.get('aiType', 0),
                    'profileImageUrl': candidate.get('profileImageUrl', ''),
                    'source': 'popular_recent'
                }
                images.append(image_info)
            
            logger.debug(f"近日美图模式完成 - 选择 {len(images)} 张图片")
            return images
            
        except Exception as e:
            logger.debug(f"获取近日美图异常: {e}")
            return []

    async def _get_permanent_popular_images(self, search_result: Dict[str, Any], count: int) -> List[Dict[str, Any]]:
        """
        获取美图（从popular.permanent中选择）
        
        Args:
            search_result: 搜索结果
            count: 需要的图片数量
            
        Returns:
            图片列表
        """
        try:
            popular_permanent = search_result.get('popular', {}).get('permanent', [])
            
            if not popular_permanent:
                logger.debug("没有找到永久热门作品")
                return []
            
            logger.debug(f"永久热门作品池: {len(popular_permanent)} 张")
            
            # 随机选择指定数量的图片
            selected_count = min(count, len(popular_permanent))
            selected_images = random.sample(popular_permanent, selected_count)
            
            # 构建图片信息
            images = []
            for candidate in selected_images:
                # 获取原始图片URL
                original_urls = await self.get_illust_original_urls(candidate.get('id', ''))
                primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                
                image_info = {
                    'id': candidate.get('id', ''),
                    'title': candidate.get('title', ''),
                    'urls': original_urls if original_urls else [primary_url],  # 🔥 改造为URL数组
                    'tags': candidate.get('tags', []),
                    'userId': candidate.get('userId', ''),
                    'userName': candidate.get('userName', ''),
                    'pageCount': candidate.get('pageCount', 1),
                    'width': candidate.get('width', 0),
                    'height': candidate.get('height', 0),
                    'illustType': candidate.get('illustType', 0),
                    'xRestrict': candidate.get('xRestrict', 0),
                    'description': candidate.get('description', ''),
                    'createDate': candidate.get('createDate', ''),
                    'aiType': candidate.get('aiType', 0),
                    'profileImageUrl': candidate.get('profileImageUrl', ''),
                    'source': 'popular_permanent'
                }
                images.append(image_info)
            
            logger.debug(f"美图模式完成 - 选择 {len(images)} 张图片")
            return images
            
        except Exception as e:
            logger.debug(f"获取美图异常: {e}")
            return []

    async def get_random_image(
        self,
        user_qq: Optional[str] = None,
        max_attempts: int = 5,
        min_bookmarks: int = 100,
    ) -> Optional[Dict[str, Any]]:
        """
        获取随机图片推荐（保持向后兼容）
        
        Args:
            user_qq: 用户QQ号（可选，用于日志记录）
            max_attempts: 最大尝试次数（不同tag），默认5次
            
        Returns:
            图片推荐信息，包含tag、图片、质量评分等，失败返回None
        """
        try:
            user_info = f"用户 {user_qq}" if user_qq else "匿名用户"
            logger.debug(f"开始为{user_info}获取随机图片推荐")
            
            # 1. 获取可用tag
            available_tags = list(self.preferred_tags)
            if not available_tags:
                logger.debug("喜好tag池为空，无法获取图片推荐")
                return None
            
            seed = self._get_daily_seed(user_qq or "anonymous")
            start_index = seed % len(available_tags)
            ordered_tags = available_tags[start_index:] + available_tags[:start_index]

            # 2. 尝试多个tag，直到找到有热门作品的tag
            attempted_tags = []
            
            for attempt, selected_tag in enumerate(ordered_tags[:max_attempts]):
                if selected_tag in attempted_tags:
                    logger.debug("已尝试所有可用tag，都没有找到热门作品")
                    break

                attempted_tags.append(selected_tag)
                
                logger.debug(f"尝试第 {attempt + 1} 次，选中tag: {selected_tag}")
                
                # 3. 搜索该tag的热门作品
                search_result = await self.search_illustrations(selected_tag)
                if not search_result:
                    logger.debug(f"搜索tag '{selected_tag}' 失败，尝试下一个tag")
                    continue
                
                # 4. 检查是否有热门作品
                popular_images = search_result.get('popular', {}).get('recent', [])
                if not popular_images:
                    logger.debug(f"tag '{selected_tag}' 没有热门作品，尝试下一个tag")
                    continue
                
                logger.debug(f"tag '{selected_tag}' 找到热门作品 {len(popular_images)} 张")
                
                # 5. 根据每日种子稳定选择，并要求收藏数达标
                offset = seed % len(popular_images)
                ordered_images = popular_images[offset:] + popular_images[:offset]
                for selected_image in ordered_images:
                    if not selected_image:
                        continue

                    quality_score = None
                    illust_details = None
                    illust_id = selected_image.get("id")
                    if illust_id:
                        illust_details = await self.get_illust_details(illust_id)
                        if illust_details:
                            quality_score = self.calculate_quality_score(illust_details)
                            logger.debug(f"质量评分: {quality_score['total_score']} ({quality_score['quality_level']})")

                    bookmark_count = int(illust_details.get("bookmarkCount", 0) or 0) if illust_details else 0
                    if bookmark_count <= min_bookmarks:
                        logger.debug(f"每日一图跳过低收藏作品: ID={illust_id}, 收藏={bookmark_count}")
                        continue

                    selected_image["bookmarkCount"] = bookmark_count
                    result = {
                        'user_qq': user_qq,
                        'tag': selected_tag,
                        'image': selected_image,
                        'date': datetime.now().strftime("%Y-%m-%d"),
                        'available_tags_count': len(available_tags),
                        'available_images_count': len(popular_images),
                        'attempted_tags': attempted_tags,
                        'quality_score': quality_score
                    }
                
                    logger.debug(f"图片推荐获取成功: tag={selected_tag}, image_id={selected_image.get('id')}, 收藏={bookmark_count}, 质量评分={quality_score['total_score'] if quality_score else 'N/A'}")
                    return result
            
            # 如果所有尝试都失败了
            logger.debug(f"尝试了 {len(attempted_tags)} 个tag都没有找到热门作品: {', '.join(attempted_tags)}")
            return None
            
        except Exception as e:
            logger.debug(f"获取图片推荐异常: {e}")
            return None

    async def get_tag_latest_images(self, 
                                   tag: Union[str, List[str]], 
                                   count: int = 5,
                                   hours_limit: int = 24,
                                   min_bookmarks: int = 0) -> Optional[Dict[str, Any]]:
        """
        获取某个标签的最新更新图片（重新设计版本 - 专注时效性）
        
        Args:
            tag: 搜索标签，可以是字符串（单tag）或列表（多tag）
            count: 返回图片数量，1-10张，默认5张
            hours_limit: 时间限制（小时），只获取指定小时内的更新，默认24小时
            
        Returns:
            最新图片结果字典，包含图片列表、统计信息等，失败返回None
        """
        try:
            # 参数验证
            if count < 1 or count > 10:
                logger.debug(f"图片数量必须在1-10之间，当前值: {count}")
                return None
            
            if hours_limit < 1 or hours_limit > 168:  # 最大7天
                logger.debug(f"时间限制必须在1-168小时之间，当前值: {hours_limit}")
                return None
            
            # 处理标签
            if isinstance(tag, str):
                tags = [tag.strip()] if tag.strip() else []
            elif isinstance(tag, list):
                tags = [tag.strip() for tag in tag if tag and tag.strip()]
            else:
                logger.debug("标签参数必须是字符串或字符串列表")
                return None
            
            if not tags:
                logger.debug("搜索标签不能为空")
                return None
            
            search_keyword = ' '.join(tags)
            logger.debug(f"开始获取标签最新图片 - 关键词: '{search_keyword}', 数量: {count}, 时间限制: {hours_limit}小时, 收藏门槛: >{min_bookmarks}")
            
            # 计算时间阈值（修复时区问题）
            from datetime import datetime, timedelta, timezone
            time_threshold = datetime.now(timezone.utc) - timedelta(hours=hours_limit)
            time_threshold_str = time_threshold.strftime("%Y-%m-%dT%H:%M:%S UTC")
            
            logger.debug(f"时间阈值: {time_threshold_str} 之后的作品")
            
            # 逐张检查图片，数量足够后立即返回（最多只查询第一页）
            found_images = []
            max_pages = 1  # 最多搜索1页
            page = 1
            total_checked = 0
            
            while len(found_images) < count and page <= max_pages:
                logger.debug(f"搜索第 {page} 页...")
                
                # 搜索当前页
                search_result = await self._search_illustrations_simple(search_keyword, page=page)
                if not search_result:
                    logger.debug(f"第 {page} 页搜索失败")
                    break
                
                # 获取当前页的图片
                page_images = search_result.get('illusts', [])
                if not page_images:
                    logger.debug(f"第 {page} 页没有图片")
                    break
                
                logger.debug(f"第 {page} 页找到 {len(page_images)} 张图片")
                
                # 逐张检查图片
                page_valid_count = 0
                for image in page_images:
                    total_checked += 1
                    
                    create_date_str = image.get('createDate', '')
                    if not create_date_str:
                        continue
                    
                    # 时间解析
                    create_time = self._parse_pixiv_time(create_date_str)
                    if not create_time:
                        continue
                    
                    # 检查是否在时间范围内
                    if create_time >= time_threshold:
                        illust_details = await self.get_illust_details(image.get('id', ''))
                        bookmark_count = int(illust_details.get('bookmarkCount', 0) or 0) if illust_details else 0
                        if bookmark_count <= min_bookmarks:
                            logger.debug(f"最新图片跳过低收藏作品: ID={image.get('id')}, 收藏={bookmark_count} <= {min_bookmarks}")
                            continue

                        # 计算上传小时数
                        current_time = datetime.now(timezone.utc)
                        hours_since_upload = (current_time - create_time).total_seconds() / 3600
                        image['hours_since_upload'] = hours_since_upload
                        image['bookmarkCount'] = bookmark_count
                        found_images.append(image)
                        page_valid_count += 1
                        
                        logger.debug(f"找到有效图片 {len(found_images)}/{count}: ID={image.get('id')}, 上传时间={hours_since_upload:.1f}小时前")
                        
                        # 数量足够，立即返回
                        if len(found_images) >= count:
                            logger.debug(f"已找到足够图片，停止搜索")
                            break
                    else:
                        # 如果图片不在时间范围内，且这是按时间排序的，说明后面的图片更旧
                        hours_since_upload = (datetime.now(timezone.utc) - create_time).total_seconds() / 3600
                        logger.debug(f"图片 {image.get('id')} 不在时间范围内（{hours_since_upload:.1f}小时前），可能已到时间边界")
                        # 可以选择继续检查几张，或者直接跳到下一页
                
                logger.debug(f"第 {page} 页检查完成: 找到 {page_valid_count} 张有效图片，累计 {len(found_images)} 张")
                
                # 如果当前页没有新图片，可能已经到了最早的页面
                if page_valid_count == 0:
                    logger.debug(f"第 {page} 页没有符合时间范围的图片，停止搜索")
                    break
                
                # 如果数量已足够，退出循环
                if len(found_images) >= count:
                    break
                
                page += 1
                
                # 短暂延迟避免请求过快
                await asyncio.sleep(0.1)
            
            if not found_images:
                logger.debug(f"在最近 {hours_limit} 小时内没有找到标签 '{search_keyword}' 的图片")
                self.last_error_reason = "no_recent_images"
                return None
            
            logger.debug(f"总共找到 {len(found_images)} 张最新图片")
            
            # 按时间排序（最新的在前）
            found_images.sort(key=lambda x: x.get('createDate', ''), reverse=True)
            
            # 选择指定数量的图片
            selected_images = found_images[:count]
            
            # 构建简化的图片信息（专注时效性，不获取质量评分）
            enhanced_images = []
            for image in selected_images:
                # 🔥 获取实际图片URL而不是缩略图
                original_urls = await self.get_illust_original_urls(image.get('id', ''))
                primary_url = original_urls[0] if original_urls else image.get('url', '')
                
                enhanced_image = {
                    'id': image.get('id', ''),
                    'title': image.get('title', ''),
                    'urls': original_urls if original_urls else [primary_url],  # 🔥 返回实际图片URL数组
                    'tags': image.get('tags', []),
                    'userId': image.get('userId', ''),
                    'userName': image.get('userName', ''),
                    'pageCount': image.get('pageCount', 1),
                    'width': image.get('width', 0),
                    'height': image.get('height', 0),
                    'illustType': image.get('illustType', 0),
                    'xRestrict': image.get('xRestrict', 0),
                    'description': image.get('description', ''),
                    'createDate': image.get('createDate', ''),
                    'aiType': image.get('aiType', 0),
                    'profileImageUrl': image.get('profileImageUrl', ''),
                    'hours_since_upload': image.get('hours_since_upload', 0),
                    'source': 'tag_latest',
                    'bookmarkCount': image.get('bookmarkCount', 0)
                }
                enhanced_images.append(enhanced_image)
            
            # 构建返回结果
            result = {
                'search_info': {
                    'tags': tags,
                    'search_keyword': search_keyword,
                    'requested_count': count,
                    'actual_count': len(enhanced_images),
                    'hours_limit': hours_limit,
                    'time_threshold': time_threshold_str,
                    'total_candidates': len(found_images),
                    'pages_searched': page - 1,
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                },
                'images': enhanced_images,
                'statistics': {
                    'total_found': len(found_images),
                    'time_filtered': len(found_images),
                    'avg_hours_since_upload': sum(img.get('hours_since_upload', 0) for img in enhanced_images) / len(enhanced_images) if enhanced_images else 0
                }
            }
            
            logger.debug(f"标签最新图片获取完成 - 找到 {len(enhanced_images)} 张图片")
            self.last_error_reason = None
            return result
            
        except Exception as e:
            reason = self._record_http_error(e)
            logger.debug(f"获取标签最新图片异常: {e} ({reason})")
            return None

    def _parse_pixiv_time(self, time_str: str) -> Optional[datetime]:
        """
        解析Pixiv时间字符串（修复时区问题）
        
        Args:
            time_str: 时间字符串，如 "2025-11-22T12:49:30+09:00"
            
        Returns:
            UTC时间的datetime对象，失败返回None
        """
        try:
            from datetime import datetime, timezone, timedelta
            
            if not time_str:
                return None
            
            # 处理Pixiv的日本时间格式 (+09:00)
            if '+09:00' in time_str:
                # 解析为带时区的时间
                create_time = datetime.fromisoformat(time_str)
                # 转换为UTC时间
                utc_time = create_time.astimezone(timezone.utc)
                return utc_time
            elif '+00:00' in time_str or time_str.endswith('Z'):
                # 处理UTC时间
                if time_str.endswith('Z'):
                    time_str = time_str[:-1] + '+00:00'
                create_time = datetime.fromisoformat(time_str)
                return create_time
            else:
                # 处理无时区信息的时间，假设为UTC
                create_time = datetime.fromisoformat(time_str)
                return create_time.replace(tzinfo=timezone.utc)
                
        except Exception as e:
            logger.debug(f"时间解析失败: {time_str}, 错误: {e}")
            return None

    async def _search_illustrations_simple(self, 
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
            return await self._extract_simplified_data_simple(raw_data, search_keyword)
            
        except Exception as e:
            reason = self._record_http_error(e)
            logger.debug(f"简化搜索图片异常: {e} ({reason})")
            return None

    async def _extract_simplified_data_simple(self, 
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
                        'timestamp': __import__('datetime').datetime.now().isoformat()
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
                
                if await self._should_block_image_with_translation(illust_tags, tag_translation):
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
                    'timestamp': __import__('datetime').datetime.now().isoformat()
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
                    'timestamp': __import__('datetime').datetime.now().isoformat()
                }
            }

    async def get_author_images(self, 
                             user_id: str, 
                             mode: str = "recent", 
                             count: int = 3) -> Optional[Dict[str, Any]]:
        """
        获取特定作者的图片（支持作者ID或圈名）
        
        Args:
            user_id: 作者ID或圈名
            mode: 获取模式
                - "recent": 最新作品，按时间排序选择（默认）
                - "popular": 热门作品，按收藏数排序选择
                - "random": 随机选择（不检查质量评分，因为喜欢作者的用户想看所有更新）
            count: 返回图片数量，1-5张，默认3张
            
        Returns:
            作者图片结果字典，包含图片列表、作者信息等，失败返回None
        """
        try:
            # 参数验证
            if not user_id or not user_id.strip():
                logger.debug("作者ID/圈名不能为空")
                return None
            
            if count < 1 or count > 5:
                logger.debug(f"图片数量必须在1-5之间，当前值: {count}")
                return None
            
            if mode not in ["random", "recent", "popular"]:
                logger.debug(f"不支持的获取模式: {mode}，支持的模式: random, recent, popular")
                return None
            
            original_input = user_id.strip()
            user_id = str(user_id).strip()
            
            # 尝试通过圈名查找作者ID
            resolved_user_id = self.search_author_by_alias(user_id)
            if resolved_user_id:
                logger.debug(f"通过圈名 '{user_id}' 找到作者ID: {resolved_user_id}")
                user_id = resolved_user_id
                used_alias = True
            else:
                # 如果不是圈名，直接使用作为作者ID
                used_alias = False
            
            logger.debug(f"开始获取作者图片 - ID: {user_id}, 模式: {mode}, 数量: {count}")
            if used_alias:
                logger.debug(f"使用圈名查询: '{original_input}' -> ID: {user_id}")
            
            # 检查是否为喜欢作者
            is_favorite = user_id in self.favorite_authors
            author_info = self.favorite_authors.get(user_id, {})
            author_name = author_info.get('name', '未知作者')
            
            logger.debug(f"作者信息: {author_name} ({user_id}), 是否喜欢: {is_favorite}")
            
            # 获取作者信息和作品列表
            author_data = await self.get_author_profile(user_id)
            if not author_data:
                logger.debug(f"获取作者作品失败: 无数据")
                return None
            
            # 提取作者作品 - 从正确的API路径获取
            # 新的API返回格式：body.illusts 是一个字典，键为作品ID
            illusts_dict = author_data.get('illusts', {})
            illusts = list(illusts_dict.values()) if isinstance(illusts_dict, dict) else []
            if not illusts:
                logger.debug(f"作者 {author_name} 没有作品")
                return None
            
            logger.debug(f"作者 {author_name} 共有 {len(illusts)} 个作品")
            
            # 根据模式获取图片
            if mode == "random":
                images = await self._get_author_random_images(
                    illusts, count, min_quality_score=60.0, max_attempts=10
                )
            elif mode == "recent":
                images = await self._get_author_recent_images(illusts, count)
            elif mode == "popular":
                images = await self._get_author_popular_images(illusts, count)
            else:
                images = []
            
            if not images:
                logger.debug(f"未找到符合条件的作者图片")
                return None
            
            # 更新作者最后检查时间
            if is_favorite:
                self.update_author_last_check(user_id)
            
            # 构建返回结果
            result = {
                'author_info': {
                    'user_id': user_id,
                    'name': author_name,
                    'is_favorite': is_favorite,
                    'total_works': len(illusts),
                    'profile_image_url': author_data.get('image', ''),
                    'comment': author_data.get('comment', ''),
                    'followable': author_data.get('followable', False)
                },
                'search_info': {
                    'mode': mode,
                    'requested_count': count,
                    'actual_count': len(images),
                    'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                },
                'images': images
            }
            
            logger.debug(f"作者图片获取完成 - 找到 {len(images)} 张图片")
            return result
            
        except Exception as e:
            logger.debug(f"获取作者图片异常: {e}")
            return None

    async def _get_author_random_images(self, 
                                    illusts: List[Dict[str, Any]], 
                                    count: int,
                                    min_quality_score: float = 60.0,
                                    max_attempts: int = 10) -> List[Dict[str, Any]]:
        """
        获取作者随机图片（带质量评分检查）
        
        Args:
            illusts: 作者作品列表
            count: 需要的图片数量
            min_quality_score: 最低质量评分
            max_attempts: 最大尝试次数
            
        Returns:
            符合条件的图片列表
        """
        try:
            images = []
            attempts = 0
            used_image_ids = set()
            failed_candidates = []
            
            if not illusts:
                logger.debug("作者作品列表为空")
                return []
            
            logger.debug(f"作者作品池: {len(illusts)} 个作品")
            
            # 第一阶段：尝试找到符合质量要求的图片
            while len(images) < count and attempts < max_attempts:
                attempts += 1
                
                # 随机选择作品
                if not illusts:
                    break
                
                candidate = random.choice(illusts)
                image_id = candidate.get('id')
                
                if not image_id or image_id in used_image_ids:
                    continue
                
                used_image_ids.add(image_id)
                
                logger.debug(f"尝试第 {attempts} 次 - 检查作品 ID: {image_id}")
                
                # 获取详细信息和质量评分
                quality_score = None
                try:
                    illust_details = await self.get_illust_details(image_id)
                    if illust_details:
                        quality_score = self.calculate_quality_score(illust_details)
                        logger.debug(f"质量评分: {quality_score['total_score']} ({quality_score['quality_level']})")
                    else:
                        logger.debug("获取作品详情失败，跳过质量评分")
                        quality_score = None
                except Exception as e:
                    logger.debug(f"质量评分计算异常: {e}")
                    quality_score = None
                
                # 检查质量评分
                if quality_score and quality_score['total_score'] >= min_quality_score:
                    # 🔥 获取实际图片URL而不是缩略图
                    original_urls = await self.get_illust_original_urls(image_id)
                    primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                    
                    # 构建图片信息
                    image_info = {
                        'id': image_id,
                        'title': candidate.get('title', ''),
                        'url': original_urls if original_urls else [primary_url],  # 🔥 返回实际图片URL数组
                        'tags': candidate.get('tags', []),
                        'userId': candidate.get('userId', ''),
                        'userName': candidate.get('userName', ''),
                        'pageCount': candidate.get('pageCount', 1),
                        'width': candidate.get('width', 0),
                        'height': candidate.get('height', 0),
                        'illustType': candidate.get('illustType', 0),
                        'xRestrict': candidate.get('xRestrict', 0),
                        'description': candidate.get('description', ''),
                        'createDate': candidate.get('createDate', ''),
                        'aiType': candidate.get('aiType', 0),
                        'profileImageUrl': candidate.get('profileImageUrl', ''),
                        'quality_score': quality_score,
                        'source': 'author_random'
                    }
                    
                    images.append(image_info)
                    logger.debug(f"✅ 作品符合要求 - ID: {image_id}, 评分: {quality_score['total_score']}")
                else:
                    # 存储不符合要求的候选作品
                    failed_candidates.append({
                        'candidate': candidate,
                        'quality_score': quality_score,
                        'reason': 'low_quality' if quality_score else 'no_score'
                    })
                    score_text = f"{quality_score['total_score']}" if quality_score else "无评分"
                    logger.debug(f"❌ 作品不符合要求 - ID: {image_id}, 评分: {score_text} < {min_quality_score}")
            
            # 第二阶段：如果没找到足够的高质量图片，启用降级策略
            if len(images) < count and failed_candidates:
                logger.debug(f"🔄 启用降级策略 - 高质量作品不足，从剩余候选中选择最佳作品")
                
                # 按质量评分排序失败的候选作品
                failed_candidates.sort(key=lambda x: (
                    0 if x['quality_score'] else 1,  # 有评分的优先
                    -(x['quality_score']['total_score'] if x['quality_score'] else 0)  # 按评分降序
                ))
                
                # 补充剩余需要的图片
                needed = count - len(images)
                for i, failed_item in enumerate(failed_candidates[:needed]):
                    candidate = failed_item['candidate']
                    quality_score = failed_item['quality_score']
                    image_id = candidate.get('id', '')
                    
                    # 🔥 降级时也获取原始图片URL
                    original_urls = await self.get_illust_original_urls(image_id)
                    primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                    
                    image_info = {
                        'id': image_id,
                        'title': candidate.get('title', ''),
                        'url': original_urls if original_urls else [primary_url],  # 🔥 返回实际图片URL
                        'tags': candidate.get('tags', []),
                        'userId': candidate.get('userId', ''),
                        'userName': candidate.get('userName', ''),
                        'pageCount': candidate.get('pageCount', 1),
                        'width': candidate.get('width', 0),
                        'height': candidate.get('height', 0),
                        'illustType': candidate.get('illustType', 0),
                        'xRestrict': candidate.get('xRestrict', 0),
                        'description': candidate.get('description', ''),
                        'createDate': candidate.get('createDate', ''),
                        'aiType': candidate.get('aiType', 0),
                        'profileImageUrl': candidate.get('profileImageUrl', ''),
                        'source': 'author_fallback'
                    }
                    
                    images.append(image_info)
                    score_text = f"{quality_score['total_score']}" if quality_score else "无评分"
                    logger.debug(f"🔄 降级选择 - ID: {candidate.get('id')}, 评分: {score_text} (原因: {failed_item['reason']})")
            
            # 第三阶段：如果仍然不足，直接从剩余作品中随机选择
            if len(images) < count:
                logger.debug(f"🔄 最终降级 - 仍然不足，从剩余作品中随机选择")
                
                # 获取未使用的作品
                remaining_illusts = [
                    illust for illust in illusts 
                    if illust.get('id') not in [img['id'] for img in images]
                ]
                
                needed = count - len(images)
                selected = random.sample(remaining_illusts, min(needed, len(remaining_illusts)))
                
                for candidate in selected:
                    # 🔥 获取实际图片URL而不是缩略图
                    original_urls = await self.get_illust_original_urls(candidate.get('id', ''))
                    primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                    
                    image_info = {
                        'id': candidate.get('id', ''),
                        'title': candidate.get('title', ''),
                        'url': original_urls if original_urls else [primary_url],  # 🔥 返回实际图片URL数组
                        'tags': candidate.get('tags', []),
                        'userId': candidate.get('userId', ''),
                        'userName': candidate.get('userName', ''),
                        'pageCount': candidate.get('pageCount', 1),
                        'width': candidate.get('width', 0),
                        'height': candidate.get('height', 0),
                        'illustType': candidate.get('illustType', 0),
                        'xRestrict': candidate.get('xRestrict', 0),
                        'description': candidate.get('description', ''),
                        'createDate': candidate.get('createDate', ''),
                        'aiType': candidate.get('aiType', 0),
                        'profileImageUrl': candidate.get('profileImageUrl', ''),
                        'source': 'author_final'
                    }
                    
                    images.append(image_info)
                    logger.debug(f"🔄 最终选择 - ID: {candidate.get('id')}, 无质量评分")
            
            logger.debug(f"作者随机模式完成 - 尝试 {attempts} 次，找到 {len(images)} 张图片")
            return images
            
        except Exception as e:
            logger.debug(f"获取作者随机图片异常: {e}")
            return []

    async def _get_author_recent_images(self, illusts: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
        """
        获取作者最新作品
        
        Args:
            illusts: 作者作品列表
            count: 需要的图片数量
            
        Returns:
            图片列表
        """
        try:
            # 按创建时间排序（最新的在前）
            sorted_illusts = sorted(
                illusts, 
                key=lambda x: x.get('createDate', ''), 
                reverse=True
            )
            
            # 选择最新的作品
            selected_count = min(count, len(sorted_illusts))
            selected_illusts = sorted_illusts[:selected_count]
            
            logger.debug(f"作者最新作品模式完成 - 选择 {len(selected_illusts)} 张最新作品")
            
            # 构建图片信息
            images = []
            for candidate in selected_illusts:
                # 获取原始图片URL
                original_urls = await self.get_illust_original_urls(candidate.get('id', ''))
                primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                
                image_info = {
                    'id': candidate.get('id', ''),
                    'title': candidate.get('title', ''),
                    'url': original_urls if original_urls else [primary_url],  # 🔥 改造为URL数组
                    'tags': candidate.get('tags', []),
                    'userId': candidate.get('userId', ''),
                    'userName': candidate.get('userName', ''),
                    'pageCount': candidate.get('pageCount', 1),
                    'width': candidate.get('width', 0),
                    'height': candidate.get('height', 0),
                    'illustType': candidate.get('illustType', 0),
                    'xRestrict': candidate.get('xRestrict', 0),
                    'description': candidate.get('description', ''),
                    'createDate': candidate.get('createDate', ''),
                    'aiType': candidate.get('aiType', 0),
                    'profileImageUrl': candidate.get('profileImageUrl', ''),
                    'source': 'author_recent'
                }
                images.append(image_info)
            
            return images
            
        except Exception as e:
            logger.debug(f"获取作者最新作品异常: {e}")
            return []

    async def _get_author_popular_images(self, illusts: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
        """
        获取作者热门作品
        
        Args:
            illusts: 作者作品列表
            count: 需要的图片数量
            
        Returns:
            图片列表
        """
        try:
            # 按收藏数排序（收藏数多的在前）
            sorted_illusts = sorted(
                illusts, 
                key=lambda x: x.get('bookmarkCount', 0), 
                reverse=True
            )
            
            # 选择最热门的作品
            selected_count = min(count, len(sorted_illusts))
            selected_illusts = sorted_illusts[:selected_count]
            
            logger.debug(f"作者热门作品模式完成 - 选择 {len(selected_illusts)} 张热门作品")
            
            # 构建图片信息
            images = []
            for candidate in selected_illusts:
                # 获取原始图片URL
                original_urls = await self.get_illust_original_urls(candidate.get('id', ''))
                primary_url = original_urls[0] if original_urls else candidate.get('url', '')
                
                image_info = {
                    'id': candidate.get('id', ''),
                    'title': candidate.get('title', ''),
                    'url': original_urls if original_urls else [primary_url],  # 🔥 改造为URL数组
                    'tags': candidate.get('tags', []),
                    'userId': candidate.get('userId', ''),
                    'userName': candidate.get('userName', ''),
                    'pageCount': candidate.get('pageCount', 1),
                    'width': candidate.get('width', 0),
                    'height': candidate.get('height', 0),
                    'illustType': candidate.get('illustType', 0),
                    'xRestrict': candidate.get('xRestrict', 0),
                    'description': candidate.get('description', ''),
                    'createDate': candidate.get('createDate', ''),
                    'aiType': candidate.get('aiType', 0),
                    'profileImageUrl': candidate.get('profileImageUrl', ''),
                    'source': 'author_popular'
                }
                images.append(image_info)
            
            return images
            
        except Exception as e:
            logger.debug(f"获取作者热门作品异常: {e}")
            return []

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
