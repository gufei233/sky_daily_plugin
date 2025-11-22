"""
光遇今日国服插件 - AstrBot版本
移植自 nonebot_plugin_sky 的 "/今日国服" 功能

数据源：微博@今天游离翻车了吗, 微博@陈陈努力不鸽
版本：1.3.0
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone, time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from pathlib import Path
import httpx
import base64
import os
import tempfile
import shutil
import json

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp


# ==================== 微博爬虫相关类 ====================

class SpiderException(Exception):
    """爬虫基础异常"""
    pass


class GetMblogsFailedError(SpiderException):
    """获取微博失败异常"""
    pass


class UnknownError(SpiderException):
    """未知错误异常"""
    pass


class Auth:
    """认证信息管理"""

    # 默认时区（北京时间）
    BEIJING_TZ = timezone(timedelta(hours=8))

    def __init__(self, config: Dict = None):
        """初始化认证信息

        Args:
            config: 插件配置字典，包含cookies配置
        """
        self.config = config or {}
        self.cookies_config = self.config.get("cookies", {})
        self.use_cookie = self.cookies_config.get("enabled", False)
        self._visitor_cookies = {}

    async def init_visitor_auth(self, session: httpx.AsyncClient):
        """初始化访客认证（无cookie方案）"""
        try:
            # Step 1: 获取SUB
            url = 'https://visitor.passport.weibo.cn/visitor/genvisitor2'
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://visitor.passport.weibo.cn',
                'Referer': 'https://visitor.passport.weibo.cn/visitor/visitor?entry=sinawap&a=enter&url=https%3A%2F%2Fm.weibo.cn%2F',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
            }
            data = {'cb': 'visitor_gray_callback', 'tid': '', 'new_tid': 'null'}

            resp = await session.post(url, headers=headers, data=data, timeout=10.0)
            match = re.search(r'visitor_gray_callback\((.*)\)', resp.text)
            if match:
                json_data = json.loads(match.group(1))
                if json_data.get('retcode') == 20000000:
                    sub = json_data['data']['sub']
                    subp = json_data['data']['subp']
                    session.cookies.set('SUB', sub, domain='.weibo.cn')
                    session.cookies.set('SUBP', subp, domain='.weibo.cn')

                    # Step 2: 获取XSRF-TOKEN
                    resp2 = await session.get('https://m.weibo.cn', headers={'Referer': 'https://visitor.passport.weibo.cn/'}, timeout=10.0)
                    xsrf_token = session.cookies.get('XSRF-TOKEN')
                    self._visitor_cookies = {'xsrf_token': xsrf_token}
                    logger.info("访客认证初始化成功")
                    return True
        except Exception as e:
            logger.error(f"访客认证初始化失败: {e}")
        return False

    def get_headers(self, use_mobile_api: bool = False) -> Dict[str, str]:
        """获取标准请求头"""
        if use_mobile_api:
            return {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "mweibo-pwa": "1",
                "x-requested-with": "XMLHttpRequest",
                "x-xsrf-token": self._visitor_cookies.get('xsrf_token', ''),
                "referer": "https://m.weibo.cn",
            }
        return {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome"
                "/119.0.0.0 Safari/537.36 Edg/119.0.0.0"
            ),
            "cookie": self._get_cookie(),
            "x-xsrf-token": self._get_xsrf_token(),
            "referer": "https://www.weibo.com",
            "sec-ch-ua": '"Microsoft Edge";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
            "sec-ch-ua-platform": '"Windows"',
        }

    def _get_cookie(self) -> str:
        """获取cookie"""
        sub_cookie = self.cookies_config.get("sub", "")
        return f"SUB={sub_cookie}" if sub_cookie else ""

    def _get_xsrf_token(self) -> str:
        """获取XSRF Token"""
        return self.cookies_config.get("xsrf_token", "")


@dataclass(frozen=True)
class Urls:
    """图片URL集合"""
    bmiddle: str
    large: str
    largecover: str
    largest: str
    mw2000: str
    original: str
    thumbnail: str

    def get_preferred_url(self, preference: List[str] = None) -> str:
        """获取首选图片URL"""
        preference = preference or ["large", "original", "largest"]
        for url_type in preference:
            if url := getattr(self, url_type, None):
                return url
        return self.thumbnail  # 最后兜底使用缩略图


@dataclass(frozen=True)
class Picture:
    """图片对象"""
    pic_id: str
    urls: Urls
    size: Optional[Dict[str, int]] = None
    type: str = "jpg"

    def get_url(self, preference: List[str] = None) -> str:
        """获取首选图片URL"""
        return self.urls.get_preferred_url(preference)

    async def get_binary(self, url: str, client=None, auth: Auth = None):
        """获取图片的二进制数据"""
        if auth is None:
            auth = Auth()

        async def _get_client():
            if client:
                yield client
            else:
                async with httpx.AsyncClient() as temp_client:
                    yield temp_client

        try:
            async for c in _get_client():
                response = await c.get(url, headers=auth.get_headers(), timeout=10.0)
                response.raise_for_status()
                path = httpx.URL(url).path
                filename = os.path.basename(path)
                return (filename, response.content)

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise GetMblogsFailedError(f"HTTP请求失败: {status}") from e
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise GetMblogsFailedError("网络连接异常") from e
        except Exception as e:
            raise UnknownError("图片数据获取异常") from e


@dataclass
class Blog:
    """微博条目"""
    mblogid: str
    text_raw: str
    url: str
    pic_list: List[Picture] = field(default_factory=list)
    created_at: str = ""
    is_long_text: bool = False
    use_mobile_api: bool = False

    # 缓存解析后的时间对象
    _parsed_datetime: Optional[datetime] = field(
        default=None, compare=False, repr=False
    )

    async def fetch_long_text(self, client: httpx.AsyncClient, max_attempts: int = 3, retry_delay: int = 2, auth: Auth = None) -> str:
        """获取长文本内容（支持重试）"""
        if not self.is_long_text:
            return self.text_raw

        if auth is None:
            auth = Auth()

        if self.use_mobile_api:
            api = "https://m.weibo.cn/statuses/extend"
            params = {"id": self.mblogid}
        else:
            api = "https://weibo.com/ajax/statuses/longtext"
            params = {"id": self.mblogid}

        for attempt in range(max_attempts):
            try:
                headers = auth.get_headers(use_mobile_api=self.use_mobile_api)
                response = await client.get(api, headers=headers, params=params, timeout=10.0)
                response.raise_for_status()
                content = response.json()

                if content.get("ok") != 1:
                    error_msg = content.get("msg", "数据加载失败")
                    raise GetMblogsFailedError(f"获取长文本失败: {error_msg}")

                long_text = content.get("data", ).get("longTextContent", "")
                if long_text:
                    # 将<br>标签转换为换行符
                    long_text = re.sub(r'<br\s*/?>', '\n', long_text)
                    # 清理其他HTML标签
                    return re.sub(r'<[^>]+>', '', long_text).strip()
                return self.text_raw

            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < max_attempts - 1:
                    logger.warning(f"获取长文本失败 (尝试 {attempt + 1}/{max_attempts}): {e}，{retry_delay}秒后重试...")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    logger.warning(f"获取长文本失败，使用短文本: {e}")
                    return self.text_raw
            except Exception as e:
                logger.warning(f"长文本获取异常，使用短文本: {e}")
                return self.text_raw

        logger.warning(f"重试{max_attempts}次后仍然失败，使用短文本")
        return self.text_raw

    async def fetch_images_binary_list(self, auth: Auth = None) -> List[bytes]:
        """获取当前文章内所有图片的二进制数据列表"""
        if auth is None:
            auth = Auth()

        tasks = [
            asyncio.create_task(pic.get_binary(pic.get_url(), auth=auth)) for pic in self.pic_list
        ]
        results = await asyncio.gather(*tasks)
        results = [i[1] for i in results if i is not None]
        return results


class Spider:
    """新浪微博爬虫"""

    _BEIJING_TZ = timezone(timedelta(hours=8))
    _DATE_FORMAT = "%a %b %d %H:%M:%S %z %Y"

    def __init__(self, uid: int, auth: Auth = None):
        """初始化爬虫实例"""
        self.uid = uid
        self.auth = auth or Auth()
        self._results: List[Blog] = []
        self._use_mobile_api = False
        self._client: Optional[httpx.AsyncClient] = None
        self._setup_apis()

    def _setup_apis(self) -> None:
        """配置API端点"""
        self._api = {
            "mblogs": "https://weibo.com/ajax/statuses/mymblog",
            "mobile_mblogs": f"https://m.weibo.cn/api/container/getIndex",
        }

    async def fetch(self, page: int = 0, max_attempts: int = 3, retry_delay: int = 2) -> "Spider":
        """获取指定页码的微博数据（支持重试和fallback）"""
        # 优先尝试用户配置的cookie方案
        if self.auth.use_cookie and self.auth._get_cookie():
            try:
                logger.info("使用用户配置的cookie获取微博数据")
                return await self._fetch_with_cookie(page, max_attempts, retry_delay)
            except Exception as e:
                logger.warning(f"使用cookie方案失败: {e}，切换到无cookie方案")

        # Fallback到无cookie方案
        logger.info("使用无cookie方案获取微博数据")
        return await self._fetch_without_cookie(page, max_attempts, retry_delay)

    async def _fetch_with_cookie(self, page: int, max_attempts: int, retry_delay: int) -> "Spider":
        """使用cookie方案获取数据（PC端API）"""
        last_exception = None
        headers = self.auth.get_headers(use_mobile_api=False)
        headers["referer"] = f"https://www.weibo.com/u/{self.uid}"

        for attempt in range(max_attempts):
            try:
                params = {"uid": self.uid, "page": page, "feature": 0}
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        self._api["mblogs"],
                        headers=headers,
                        params=params,
                        timeout=10.0,
                    )
                    response.raise_for_status()
                    content = response.json()

                    if content.get("ok") != 1:
                        error_msg = content.get("msg", "数据加载失败")
                        raise GetMblogsFailedError(f"获取微博列表失败: {error_msg}")
                    if not content.get("data"):
                        raise GetMblogsFailedError("获取微博列表失败, 超出最大能获取的索引")

                    self._parse_mblogs(content["data"]["list"])
                    self._use_mobile_api = False
                    return self

            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as e:
                last_exception = e
                if attempt < max_attempts - 1:
                    logger.warning(f"获取微博数据失败 (尝试 {attempt + 1}/{max_attempts}): {e}，{retry_delay}秒后重试...")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    if isinstance(e, httpx.HTTPStatusError):
                        raise GetMblogsFailedError(f"HTTP请求失败: {e.response.status_code}") from e
                    else:
                        raise GetMblogsFailedError("网络连接异常") from e
            except Exception as e:
                raise UnknownError("数据处理异常") from e

        raise GetMblogsFailedError(f"重试{max_attempts}次后仍然失败") from last_exception

    async def _fetch_without_cookie(self, page: int, max_attempts: int, retry_delay: int) -> "Spider":
        """使用无cookie方案获取数据（移动端API）"""
        last_exception = None

        for attempt in range(max_attempts):
            try:
                self._client = httpx.AsyncClient()
                # 初始化访客认证
                if not await self.auth.init_visitor_auth(self._client):
                    raise GetMblogsFailedError("访客认证初始化失败")

                # 使用移动端API获取数据
                containerid = f"230413{self.uid}"
                params = {"containerid": containerid, "page": page + 1, "count": 10}
                headers = self.auth.get_headers(use_mobile_api=True)
                headers["referer"] = f"https://m.weibo.cn/u/{self.uid}"

                response = await self._client.get(
                    self._api["mobile_mblogs"],
                    headers=headers,
                    params=params,
                    timeout=10.0,
                )
                response.raise_for_status()
                content = response.json()

                if content.get("ok") != 1:
                    error_msg = content.get("msg", "数据加载失败")
                    raise GetMblogsFailedError(f"获取微博列表失败: {error_msg}")

                # 解析移动端API响应
                self._parse_mobile_mblogs(content.get("data", {}))
                self._use_mobile_api = True
                return self

            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as e:
                last_exception = e
                if self._client:
                    await self._client.aclose()
                    self._client = None
                if attempt < max_attempts - 1:
                    logger.warning(f"获取微博数据失败 (尝试 {attempt + 1}/{max_attempts}): {e}，{retry_delay}秒后重试...")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    if isinstance(e, httpx.HTTPStatusError):
                        raise GetMblogsFailedError(f"HTTP请求失败: {e.response.status_code}") from e
                    else:
                        raise GetMblogsFailedError("网络连接异常") from e
            except Exception as e:
                if self._client:
                    await self._client.aclose()
                    self._client = None
                raise UnknownError("数据处理异常") from e

        raise GetMblogsFailedError(f"重试{max_attempts}次后仍然失败") from last_exception

    def _parse_mblogs(self, mblogs: List[Dict[str, Any]]) -> None:
        """解析微博数据（PC端API）"""
        for blog in mblogs:
            self._results.append(
                Blog(
                    mblogid=blog["mblogid"],
                    text_raw=blog["text_raw"],
                    url=f"https://www.weibo.com/{self.uid}/{blog['mblogid']}",
                    created_at=blog["created_at"],
                    pic_list=self._parse_pictures(blog),
                )
            )

    def _parse_mobile_mblogs(self, data: Dict[str, Any]) -> None:
        """解析移动端API的微博数据"""
        cards = data.get("cards", [])
        for card in cards:
            if card.get("card_type") == 11:
                card_group = card.get("card_group", [])
                for item in card_group:
                    if item.get("card_type") == 9 and "mblog" in item:
                        mblog = item["mblog"]
                        # 将<br>标签转换为换行符
                        text_raw = re.sub(r'<br\s*/?>', '\n', mblog.get("text", ""))
                        # 清理其他HTML标签
                        text_raw = re.sub(r'<[^>]+>', '', text_raw).strip()

                        self._results.append(
                            Blog(
                                mblogid=mblog.get("mid", ""),
                                text_raw=text_raw,
                                url=f"https://m.weibo.cn/status/{mblog.get('mid', '')}",
                                created_at=mblog.get("created_at", ""),
                                pic_list=self._parse_mobile_pictures(mblog),
                                is_long_text=mblog.get("isLongText", False),
                                use_mobile_api=True,
                            )
                        )

    def _parse_pictures(self, blog: Dict[str, Any]) -> List[Picture]:
        """解析微博中的图片数据（PC端API）"""
        pic_list = []
        pic_infos = blog.get("pic_infos", {})

        for pic_info in pic_infos.values():
            urls = Urls(
                bmiddle=pic_info.get("bmiddle", {}).get("url", ""),
                large=pic_info.get("large", {}).get("url", ""),
                largecover=pic_info.get("largecover", {}).get("url", ""),
                largest=pic_info.get("largest", {}).get("url", ""),
                mw2000=pic_info.get("mw2000", {}).get("url", ""),
                original=pic_info.get("original", {}).get("url", ""),
                thumbnail=pic_info.get("thumbnail", {}).get("url", ""),
            )
            pic_list.append(Picture(pic_info.get("pic_id"), urls))

        return pic_list

    def _parse_mobile_pictures(self, mblog: Dict[str, Any]) -> List[Picture]:
        """解析移动端API的图片数据"""
        pic_list = []
        pics = mblog.get("pics", [])

        for pic in pics:
            # 移动端API的图片URL结构
            large_url = pic.get("large", {}).get("url", "")
            urls = Urls(
                bmiddle=pic.get("url", ""),
                large=large_url,
                largecover="",
                largest=large_url,
                mw2000="",
                original=large_url,
                thumbnail=pic.get("url", ""),
            )
            pic_list.append(Picture(pic.get("pid", ""), urls))

        return pic_list

    def filter_by_time(
        self, start: Optional[datetime] = None, end: Optional[datetime] = None
    ) -> "Spider":
        """按时间范围过滤微博"""
        # 获取当前北京时间（用于确定"今天"和默认结束时间）
        now_beijing = datetime.now(tz=self._BEIJING_TZ)

        if start is None and end is None:
            # 无参数 → 今天的数据
            start, end = self._get_today_range(now_beijing)
        elif start is not None and end is None:
            # 单参数 → 从start到今天
            start = self._normalize_time(start)
            end = now_beijing
        else:
            # 双参数 → 指定范围
            if start is not None:
                start = self._normalize_time(start)
            if end is not None:
                end = self._normalize_time(end)

        # 执行时间过滤
        self._results = [
            blog
            for blog in self._results
            if self._is_in_time_range(blog.created_at, start, end)
        ]
        return self

    def _get_today_range(self, now: datetime):
        """获取今天的时间范围"""
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        return today_start, today_end

    def _normalize_time(self, dt: datetime) -> datetime:
        """标准化时间对象为北京时间"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self._BEIJING_TZ)
        return dt.astimezone(self._BEIJING_TZ)

    def _is_in_time_range(
        self, created_at: str, start: Optional[datetime], end: Optional[datetime]
    ) -> bool:
        """检查时间是否在指定范围内"""
        try:
            dt = self._parse_created_at(created_at)

            if start and end:
                return start <= dt <= end
            if start:
                return dt >= start
            if end:
                return dt <= end
            return True

        except (ValueError, TypeError):
            return False

    def _parse_created_at(self, created_at: str) -> datetime:
        """解析微博时间字符串"""
        # 处理可能的多余空格
        clean_time_str = re.sub(r"\s+", " ", created_at).strip()
        dt = datetime.strptime(clean_time_str, self._DATE_FORMAT)
        return dt.astimezone(self._BEIJING_TZ)

    def filter_by_regex(self, pattern: str, flags: int = 0) -> "Spider":
        """按正则表达式过滤微博"""
        compiled = re.compile(pattern, flags)
        self._results = [
            blog for blog in self._results if compiled.search(blog.text_raw)
        ]
        return self

    def one(self) -> Optional[Blog]:
        """获取第一条结果"""
        return self._results[0] if self._results else None

    def all(self, limit: Optional[int] = None) -> List[Blog]:
        """获取所有过滤结果"""
        return self._results[:limit] if limit is not None else self._results.copy()


# ==================== 核心数据获取类 ====================

class SkyDaily:
    """光遇每日任务数据获取"""

    # 缓存配置
    CACHE_DURATION = 3 * 60 * 60  # 3小时（秒）
    CACHE_DIR = "data"

    @staticmethod
    def set_cache_duration(hours: int):
        """设置缓存时长"""
        SkyDaily.CACHE_DURATION = hours * 60 * 60
        logger.info(f"缓存时长已设置为: {hours}小时")

    @staticmethod
    def _ensure_cache_dir():
        """确保缓存目录存在"""
        if not os.path.exists(SkyDaily.CACHE_DIR):
            os.makedirs(SkyDaily.CACHE_DIR, exist_ok=True)

    @staticmethod
    def _load_cache(cache_file_path: str):
        """加载缓存数据"""
        try:
            if os.path.exists(cache_file_path):
                with open(cache_file_path, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)

                # 检查缓存是否过期
                cache_time = datetime.fromisoformat(cache_data.get('timestamp', '1970-01-01T00:00:00'))
                current_time = datetime.now()

                if (current_time - cache_time).total_seconds() < SkyDaily.CACHE_DURATION:
                    remaining_hours = (SkyDaily.CACHE_DURATION - (current_time - cache_time).total_seconds()) / 3600
                    logger.info(f"使用缓存数据: {cache_file_path}，剩余: {remaining_hours:.1f}小时")
                    return cache_data.get('text'), cache_data.get('images', [])
                else:
                    logger.info(f"缓存已过期: {cache_file_path}")

        except Exception as e:
            logger.warning(f"加载缓存 {cache_file_path} 失败: {e}")

        return None, None

    @staticmethod
    def _save_cache(text: str, images: List[bytes], cache_file_path: str):
        """保存缓存数据"""
        try:
            SkyDaily._ensure_cache_dir()

            # 将二进制图片转换为base64存储
            images_b64 = [base64.b64encode(img).decode('utf-8') for img in images]

            cache_data = {
                'timestamp': datetime.now().isoformat(),
                'text': text,
                'images': images_b64
            }

            with open(cache_file_path, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            cache_hours = SkyDaily.CACHE_DURATION / 3600
            logger.info(f"数据已缓存至 {cache_file_path}，有效期{cache_hours:.1f}小时")

        except Exception as e:
            logger.error(f"保存缓存 {cache_file_path} 失败: {e}")

    @staticmethod
    def _decode_cached_images(images_b64: List[str]) -> List[bytes]:
        """解码缓存中的base64图片"""
        try:
            return [base64.b64decode(img) for img in images_b64]
        except Exception as e:
            logger.error(f"解码缓存图片失败: {e}")
            return []

    @classmethod
    async def get_daily_data(
        cls,
        uid: int,
        pattern: str,
        author_name: str,
        cache_file_name: str,
        use_cache: bool = True,
        max_attempts: int = 3,
        retry_delay: int = 2,
        auth: Auth = None
    ):
        """
        获取指定微博源的每日任务信息（通用方法，支持重试）

        Args:
            uid (int): 微博用户ID
            pattern (str): 微博内容匹配正则表达式
            author_name (str): 作者名（用于版权信息）
            cache_file_name (str): 缓存文件名（例如 "cache_youli.json"）
            use_cache (bool): 是否使用缓存
            max_attempts (int): 最大重试次数
            retry_delay (int): 重试间隔（秒）
        """
        cache_file_path = os.path.join(cls.CACHE_DIR, cache_file_name)

        # 1. 尝试从缓存加载
        if use_cache:
            cached_text, cached_images_b64 = cls._load_cache(cache_file_path)
            if cached_text is not None:
                images = cls._decode_cached_images(cached_images_b64)
                return cached_text, images

        # 2. 缓存未命中或禁用，从网络获取
        if use_cache:
            logger.info(f"缓存 {cache_file_name} 未命中，从微博(UID: {uid})获取...")
        else:
            logger.info(f"缓存已禁用，从微博(UID: {uid})获取...")

        # 3. 执行爬虫（支持重试）
        if auth is None:
            auth = Auth()
        spider = Spider(uid, auth)

        try:
            await spider.fetch(max_attempts=max_attempts, retry_delay=retry_delay)

            # 添加调试日志
            all_blogs = spider.filter_by_time().all()
            logger.info(f"获取到 {author_name} 今日微博 {len(all_blogs)} 条")

            blog = spider.filter_by_regex(pattern).one()

            if not blog:
                logger.warning(f"未找到 {author_name}(UID: {uid}) 匹配正则 '{pattern}' 的今日任务")
                return (f"【国服】{author_name} 的今日任务还未更新", [])

            # 4. 获取长文本和图片（支持重试），复用spider的client
            if spider._client:
                text = await blog.fetch_long_text(spider._client, max_attempts=max_attempts, retry_delay=retry_delay, auth=auth)
            else:
                text = blog.text_raw
            binary_images = await blog.fetch_images_binary_list(auth=auth)
            if not text:
                text = f"【国服】{author_name} 的今日任务还未更新"

            final_text = text + cls._generate_copyright(author_name, blog.url)

            # 5. 保存到缓存
            if use_cache:
                cls._save_cache(final_text, binary_images, cache_file_path)

            return (final_text, binary_images)
        finally:
            # 确保关闭client
            if spider._client:
                await spider._client.aclose()

    @classmethod
    def parse_data_source(cls, source_config: str):
        """
        解析数据源配置字符串

        Args:
            source_config (str): 格式为 "uid:pattern:author_name"

        Returns:
            tuple: (uid, pattern, author_name) 或 None（解析失败时）
        """
        try:
            parts = source_config.split(':', 2)  # 最多分割成3部分
            if len(parts) != 3:
                logger.error(f"数据源配置格式错误: {source_config}，应为 'uid:pattern:author_name'")
                return None

            uid_str, pattern, author_name = parts
            uid = int(uid_str.strip())
            pattern = pattern.strip()
            author_name = author_name.strip()

            if not uid or not pattern or not author_name:
                logger.error(f"数据源配置包含空值: {source_config}")
                return None

            return uid, pattern, author_name

        except ValueError as e:
            logger.error(f"数据源配置UID解析失败: {source_config}，错误: {e}")
            return None
        except Exception as e:
            logger.error(f"数据源配置解析异常: {source_config}，错误: {e}")
            return None

    @classmethod
    async def get_data_from_sources(cls, data_sources: List[str], use_cache: bool = True, max_attempts: int = 3, retry_delay: int = 2, auth: Auth = None):
        """
        从配置的数据源列表获取所有数据

        Args:
            data_sources (List[str]): 数据源配置列表
            use_cache (bool): 是否使用缓存
            max_attempts (int): 最大重试次数
            retry_delay (int): 重试间隔
            auth (Auth): 认证实例

        Returns:
            List[tuple]: [(text, images, author_name), ...] 成功获取的数据列表
        """
        results = []

        for i, source_config in enumerate(data_sources):
            try:
                parsed = cls.parse_data_source(source_config)
                if not parsed:
                    continue

                uid, pattern, author_name = parsed
                cache_file_name = f"sky_daily_cache_{uid}.json"

                logger.info(f"正在获取数据源 {i+1}/{len(data_sources)}: {author_name} (UID: {uid})")

                text, images = await cls.get_daily_data(
                    uid=uid,
                    pattern=pattern,
                    author_name=author_name,
                    cache_file_name=cache_file_name,
                    use_cache=use_cache,
                    max_attempts=max_attempts,
                    retry_delay=retry_delay,
                    auth=auth
                )

                results.append((text, images, author_name))
                logger.info(f"成功获取数据源: {author_name}")

            except Exception as e:
                logger.error(f"获取数据源失败: {source_config} ({author_name})，错误: {e}", exc_info=True)
                continue

        return results

    @classmethod
    async def get_youli_daily(cls, use_cache: bool = True, auth: Auth = None):
        """获取 '今天游离翻车了吗' 的国服每日任务信息（带缓存）"""
        return await cls.get_daily_data(
            uid=7360748659,
            pattern=r"^#[^#]*光遇[^#]*超话]#\s*\d{1,2}\.\d{1,2}\s*",
            author_name="今天游离翻车了吗",
            cache_file_name="sky_daily_cache_youli.json",
            use_cache=use_cache,
            auth=auth
        )

    @classmethod
    async def get_chenchen_daily(cls, use_cache: bool = True, auth: Auth = None):
        """获取 '陈陈努力不鸽' 的国服每日任务信息（带缓存）"""
        return await cls.get_daily_data(
            uid=5539106873,
            pattern=r"^【国服·每日任务攻略】",
            author_name="陈陈努力不鸽",
            cache_file_name="sky_daily_cache_chenchen.json",
            use_cache=use_cache,
            auth=auth
        )

    @staticmethod
    def _generate_copyright(user: str, url: str):
        """生成版权信息"""
        return "\n------------\n" f"【数据来源：微博@{user}】\n" f"原文链接：{url}"


# ==================== AstrBot插件主体 ====================

@register("sky_daily", "顾绯", "光遇今日国服攻略查询插件", "1.3.0", "https://github.com/Kaguya233qwq/nonebot_plugin_sky")
class SkyDailyPlugin(Star):
    """光遇今日国服插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.push_task = None  # 用于存储推送任务

        # 初始化Auth实例，传递配置
        self.auth = Auth(self.config)

        # 初始化缓存配置
        cache_config = self.config.get("cache", {})
        if cache_config.get("enabled", True):
            cache_duration = cache_config.get("duration", 3)
            SkyDaily.set_cache_duration(cache_duration)
        else:
            logger.info("智能缓存已禁用")

        # 验证数据源配置
        self._validate_data_sources()

        logger.info("光遇今日国服插件已加载（支持动态数据源配置）")

        # 如果启用了自动推送，启动定时任务
        if self.config.get("auto_push", {}).get("enabled", False):
            self.push_task = asyncio.create_task(self._daily_push_scheduler())
            logger.info("自动推送功能已启用")
        else:
            logger.info("自动推送功能未启用")

    def _validate_data_sources(self):
        """验证数据源配置"""
        data_sources = self.config.get("data_sources", [])
        if not data_sources:
            logger.warning("未配置数据源，将使用默认数据源")
            return

        valid_count = 0
        for source in data_sources:
            if SkyDaily.parse_data_source(source):
                valid_count += 1

        logger.info(f"数据源配置验证完成：{valid_count}/{len(data_sources)} 个有效")

    def _get_retry_config(self):
        """获取重试配置"""
        retry_config = self.config.get("retry", {})
        return {
            "enabled": retry_config.get("enabled", True),
            "max_attempts": retry_config.get("max_attempts", 3),
            "delay": retry_config.get("delay", 2)
        }

    def _create_forward_message(self, text: str, images: List[bytes], title: str = "光遇今日国服攻略"):
        """创建合并转发消息"""
        # 构建消息内容列表
        content = [Comp.Plain(text)]
        temp_files = []

        # 如果有图片，保存到临时文件并添加到内容中
        if images:
            try:
                # 创建一个唯一的临时目录
                temp_dir = tempfile.mkdtemp(prefix="sky_daily_")
                for i, image_bytes in enumerate(images):
                    # 使用.jpg后缀，因为大多数微博图片是jpg
                    temp_file_path = os.path.join(temp_dir, f"image_{i}.jpg")
                    with open(temp_file_path, "wb") as f:
                        f.write(image_bytes)
                    temp_files.append(temp_file_path)
                    content.append(Comp.Image.fromFileSystem(temp_file_path))
            except Exception as e:
                logger.error(f"处理图片时发生错误: {e}")
                # 即使图片处理失败，也尝试发送纯文本
                content.append(Comp.Plain(f"\n[图片加载失败: {e}]"))

        # 创建Node节点
        node = Comp.Node(
            uin=10001,  # 使用虚拟ID
            name=title,
            content=content
        )

        return node, temp_files

    def _cleanup_temp_files(self, temp_files: List[str]):
        """清理临时文件和目录"""
        try:
            temp_dirs = set()
            for temp_file in temp_files:
                if os.path.exists(temp_file):
                    temp_dirs.add(os.path.dirname(temp_file))
                    os.remove(temp_file)

            # 清理临时目录
            for temp_dir in temp_dirs:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            
            if temp_files:
                logger.debug(f"已清理临时文件: {len(temp_files)} 个")

        except Exception as e:
            logger.warning(f"清理临时文件/目录失败: {e}")

    async def _daily_push_scheduler(self):
        """每日自动推送调度器"""
        while True:
            try:
                # 获取推送时间配置
                push_config = self.config.get("auto_push", {})
                push_time_str = push_config.get("push_time", "08:00")
                targets = push_config.get("targets", [])

                if not targets:
                    logger.warning("自动推送已启用但未配置推送目标，跳过推送")
                    await asyncio.sleep(3600)  # 1小时后重新检查
                    continue

                # 解析推送时间
                try:
                    push_hour, push_minute = map(int, push_time_str.split(":"))
                except ValueError:
                    logger.error(f"推送时间格式错误: {push_time_str}，应为HH:MM格式")
                    await asyncio.sleep(3600)  # 1小时后重新检查
                    continue

                # 获取当前时间和目标时间（北京时间）
                now = datetime.now(tz=timezone(timedelta(hours=8)))
                today_push_time = now.replace(hour=push_hour, minute=push_minute, second=0, microsecond=0)

                # 如果今天的推送时间已过，计算明天的推送时间
                if now >= today_push_time:
                    next_push_time = today_push_time + timedelta(days=1)
                else:
                    next_push_time = today_push_time

                # 计算等待时间
                wait_seconds = (next_push_time - now).total_seconds()
                logger.info(f"下次自动推送时间: {next_push_time.strftime('%Y-%m-%d %H:%M:%S')} (等待 {wait_seconds:.0f} 秒)")

                # 等待到推送时间
                await asyncio.sleep(wait_seconds)

                # 执行推送
                await self._execute_auto_push(targets, push_config)

            except asyncio.CancelledError:
                logger.info("自动推送任务已取消")
                break
            except Exception as e:
                logger.error(f"自动推送调度器发生异常: {e}", exc_info=True)
                await asyncio.sleep(300)  # 5分钟后重试

    async def _execute_auto_push(self, targets: List[str], push_config: dict):
        """执行自动推送（支持动态数据源）"""
        all_temp_files = []
        try:
            logger.info(f"开始执行自动推送今日国服攻略到 {len(targets)} 个目标")

            # 获取配置
            retry_config = self._get_retry_config()
            data_sources = self.config.get("data_sources", [])

            # 自动推送强制获取最新数据，不使用缓存
            logger.info("自动推送强制获取最新数据，不使用缓存")

            # 获取数据
            nodes = []
            if not data_sources:
                # 使用默认数据源（兼容旧版本）
                logger.info("自动推送使用默认数据源")
                nodes = await self._get_default_push_nodes(False, all_temp_files)  # 不使用缓存
            else:
                # 使用配置的数据源
                logger.info(f"自动推送使用配置的数据源，共 {len(data_sources)} 个")

                if retry_config["enabled"]:
                    results = await SkyDaily.get_data_from_sources(
                        data_sources,
                        use_cache=False,  # 不使用缓存
                        max_attempts=retry_config["max_attempts"],
                        retry_delay=retry_config["delay"],
                        auth=self.auth
                    )
                else:
                    results = await SkyDaily.get_data_from_sources(
                        data_sources,
                        use_cache=False,  # 不使用缓存
                        max_attempts=1,
                        retry_delay=0,
                        auth=self.auth
                    )

                # 为每个数据源创建节点
                for text, images, author_name in results:
                    try:
                        forward_text = f"🌅 每日光遇国服攻略 (源: {author_name})\n\n{text}"
                        node, temp_files = self._create_forward_message(
                            forward_text, images, f"🌅 每日光遇攻略({author_name})"
                        )
                        nodes.append(node)
                        all_temp_files.extend(temp_files)
                    except Exception as e:
                        logger.error(f"自动推送创建[{author_name}]节点失败: {e}", exc_info=True)

            if not nodes:
                logger.error("自动推送失败：所有数据源均获取失败")
                return

            # 为每个目标发送消息
            success_count = 0
            for target in targets:
                target_success = False
                try:
                    for i, node in enumerate(nodes):
                        success = await self.context.send_message(target, MessageChain([node]))
                        if success:
                            logger.info(f"成功推送数据源{i+1}到: {target}")
                            target_success = True
                        else:
                            logger.error(f"推送数据源{i+1}失败: {target}")

                        # 稍作停顿，避免风控
                        if i < len(nodes) - 1:
                            await asyncio.sleep(1)

                    if target_success:
                        success_count += 1

                except Exception as e:
                    logger.error(f"向 {target} 推送失败: {e}", exc_info=True)

            logger.info(f"自动推送完成，成功推送到 {success_count}/{len(targets)} 个目标")

        except Exception as e:
            logger.error(f"自动推送执行失败: {e}", exc_info=True)
        finally:
            # 清理所有临时文件
            self._cleanup_temp_files(all_temp_files)

    async def _get_default_push_nodes(self, cache_enabled: bool, all_temp_files: List[str]):
        """获取默认数据源的推送节点（兼容旧版本）"""
        nodes = []

        # 获取数据源1 (游离) - 自动推送时强制不使用缓存
        try:
            text1, images1 = await SkyDaily.get_youli_daily(use_cache=False, auth=self.auth)
            forward_text1 = f"🌅 每日光遇国服攻略 (源: 游离)\n\n{text1}"
            node1, temp_files1 = self._create_forward_message(forward_text1, images1, "🌅 每日光遇攻略(游离)")
            nodes.append(node1)
            all_temp_files.extend(temp_files1)
        except Exception as e:
            logger.error(f"自动推送获取[游离]数据失败: {e}", exc_info=True)

        # 获取数据源2 (陈陈) - 自动推送时强制不使用缓存
        try:
            text2, images2 = await SkyDaily.get_chenchen_daily(use_cache=False, auth=self.auth)
            forward_text2 = f"🌅 每日光遇国服攻略 (源: 陈陈)\n\n{text2}"
            node2, temp_files2 = self._create_forward_message(forward_text2, images2, "🌅 每日光遇攻略(陈陈)")
            nodes.append(node2)
            all_temp_files.extend(temp_files2)
        except Exception as e:
            logger.error(f"自动推送获取[陈陈]数据失败: {e}", exc_info=True)

        return nodes

    @filter.command("今日国服", alias={"sky", "光遇今日国服", "国服今日", "今日光遇"})
    async def today_chinese_server(self, event: AstrMessageEvent):
        """获取光遇国服今日攻略（支持动态数据源）"""
        all_temp_files = []  # 用于跟踪所有临时文件，便于清理
        try:
            logger.info(f"用户 {event.get_sender_name()} 查询今日国服攻略 [会话: {event.unified_msg_origin}]")

            # 获取配置
            cache_enabled = self.config.get("cache", {}).get("enabled", True)
            retry_config = self._get_retry_config()
            data_sources = self.config.get("data_sources", [])

            # 如果没有配置数据源，使用默认数据源
            if not data_sources:
                logger.info("使用默认数据源")
                await self._handle_default_sources(event, cache_enabled, all_temp_files)
                return

            # 使用配置的数据源
            yield event.plain_result(f"正在获取今日国服攻略，共 {len(data_sources)} 个数据源...")

            if retry_config["enabled"]:
                results = await SkyDaily.get_data_from_sources(
                    data_sources,
                    use_cache=cache_enabled,
                    max_attempts=retry_config["max_attempts"],
                    retry_delay=retry_config["delay"],
                    auth=self.auth
                )
            else:
                results = await SkyDaily.get_data_from_sources(
                    data_sources,
                    use_cache=cache_enabled,
                    max_attempts=1,
                    retry_delay=0,
                    auth=self.auth
                )

            if not results:
                yield event.plain_result("所有数据源均获取失败，请稍后重试")
                return

            # 发送每个数据源的结果
            for i, (text, images, author_name) in enumerate(results):
                try:
                    node, temp_files = self._create_forward_message(
                        text, images, f"📋 光遇今日国服攻略 (源: {author_name})"
                    )
                    all_temp_files.extend(temp_files)
                    yield event.chain_result([node])
                    logger.info(f"今日国服攻略[{author_name}]发送成功")
                except Exception as e:
                    logger.error(f"发送[{author_name}]数据失败: {e}", exc_info=True)
                    yield event.plain_result(f"发送[{author_name}]数据失败：{e}")

            logger.info(f"今日国服攻略查询完成，成功获取 {len(results)}/{len(data_sources)} 个数据源")

        except Exception as e:
            logger.error(f"今日国服攻略查询出现异常: {e}", exc_info=True)
            yield event.plain_result(f"查询时发生未知错误：{e}")

        finally:
            # 清理所有临时文件
            self._cleanup_temp_files(all_temp_files)

    async def _handle_default_sources(self, event: AstrMessageEvent, cache_enabled: bool, all_temp_files: List[str]):
        """处理默认数据源（兼容旧版本）"""
        # 1. 获取数据源1 (游离)
        yield event.plain_result("正在获取今日国服攻略(1/2)... [源: 游离]")
        try:
            text1, images1 = await SkyDaily.get_youli_daily(use_cache=cache_enabled, auth=self.auth)
            node1, temp_files1 = self._create_forward_message(text1, images1, "📋 光遇今日国服攻略 (源: 游离)")
            all_temp_files.extend(temp_files1)
            yield event.chain_result([node1])
            logger.info("今日国服攻略[游离]发送成功")
        except Exception as e:
            logger.error(f"获取[游离]数据失败: {e}", exc_info=True)
            yield event.plain_result(f"获取[游离]数据失败：{e}")

        # 2. 获取数据源2 (陈陈)
        yield event.plain_result("正在获取今日国服攻略(2/2)... [源: 陈陈]")
        try:
            text2, images2 = await SkyDaily.get_chenchen_daily(use_cache=cache_enabled, auth=self.auth)
            node2, temp_files2 = self._create_forward_message(text2, images2, "📋 光遇今日国服攻略 (源: 陈陈)")
            all_temp_files.extend(temp_files2)
            yield event.chain_result([node2])
            logger.info("今日国服攻略[陈陈]发送成功")
        except Exception as e:
            logger.error(f"获取[陈陈]数据失败: {e}", exc_info=True)
            yield event.plain_result(f"获取[陈陈]数据失败：{e}")

    @filter.command("清除缓存", alias={"清空缓存", "清理缓存", "sky清缓存"})
    async def clear_cache(self, event: AstrMessageEvent):
        """清除所有缓存的任务数据"""
        try:
            cache_dir = SkyDaily.CACHE_DIR
            if not os.path.exists(cache_dir):
                yield event.plain_result("缓存目录不存在，无需清理")
                return

            # 查找所有缓存文件
            cache_files = []
            for filename in os.listdir(cache_dir):
                if filename.startswith("sky_daily_cache_") and filename.endswith(".json"):
                    cache_files.append(os.path.join(cache_dir, filename))

            if not cache_files:
                yield event.plain_result("未找到任何缓存文件")
                return

            # 删除缓存文件
            deleted_count = 0
            for cache_file in cache_files:
                try:
                    os.remove(cache_file)
                    deleted_count += 1
                    logger.info(f"已删除缓存文件: {cache_file}")
                except Exception as e:
                    logger.error(f"删除缓存文件失败: {cache_file}, 错误: {e}")

            yield event.plain_result(f"✅ 缓存清理完成！\n删除了 {deleted_count} 个缓存文件\n下次查询将重新获取最新数据")
            logger.info(f"用户 {event.get_sender_name()} 清除了 {deleted_count} 个缓存文件")

        except Exception as e:
            logger.error(f"清除缓存时发生异常: {e}", exc_info=True)
            yield event.plain_result(f"清除缓存时发生错误：{e}")

    @filter.command("光遇推送设置", alias={"sky设置", "推送设置"})
    async def push_settings(self, event: AstrMessageEvent):
        """显示推送设置帮助信息"""
        data_sources = self.config.get("data_sources", [])
        retry_config = self._get_retry_config()

        help_text = f"""🔧 光遇自动推送设置帮助

📍 当前会话标识: {event.unified_msg_origin}

🛠️ 配置步骤:
1. 在AstrBot管理面板找到"光遇今日国服插件"配置
2. 配置以下选项：

📊 数据源配置:
- 格式: 微博UID:正则表达式:作者名
- 示例: 7360748659:^#[^#]*光遇[^#]*超话]#\\s*\\d{{1,2}}\\.\\d{{1,2}}\\s*:今天游离翻车了吗
- 当前配置: {len(data_sources)} 个数据源

🔄 重试机制配置:
- 启用重试: {'是' if retry_config['enabled'] else '否'}
- 最大重试次数: {retry_config['max_attempts']}
- 重试间隔: {retry_config['delay']}秒

📅 自动推送配置:
- 启用自动推送: 开启
- 推送时间: 如 08:00 (24小时制)
- 推送目标列表: 添加上方的会话标识

📋 会话标识说明:
- 群聊: platform:GroupMessage:群号
- 私聊: platform:FriendMessage:用户ID
- 请复制上方显示的完整标识符

⚠️ 注意事项:
- 推送时间为北京时间
- 可添加多个推送目标和数据源
- 修改配置后需重载插件生效
- 支持失败重试机制，提高获取成功率

💡 新功能:
- 支持自定义数据源配置
- 网络请求失败自动重试
- 动态循环获取所有配置的数据源
- 兼容旧版本配置（未配置数据源时使用默认源）

🔍 数据源配置说明:
- UID: 微博用户的数字ID
- 正则表达式: 用于匹配微博内容的模式
- 作者名: 显示在攻略中的作者名称
- 多个数据源会依次获取并发送
"""
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时调用"""
        # 取消自动推送任务
        if self.push_task and not self.push_task.done():
            self.push_task.cancel()
            try:
                await self.push_task
            except asyncio.CancelledError:
                pass
            logger.info("自动推送任务已停止")

        logger.info("光遇今日国服插件已卸载")