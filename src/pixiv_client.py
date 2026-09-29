# ============================================================================
# pixiv_client.py — Pixiv API 封装层
# ============================================================================
# 封装 pixivpy3 的 AppPixivAPI，为插件提供简洁、健壮的 Pixiv 访问接口。
#
# 核心能力:
#   login(refresh_token)       — 使用 refresh_token 认证
#   search_by_id(illust_id)    — 按作品 ID 获取详情和图片 URL
#   search_by_tag(tag, ...)    — 按标签搜索作品列表
#   download_image(image_url)  — 下载图片为 bytes
#
# 错误处理策略:
#   - 登录过期: 自动尝试刷新 token 并重试 (最多 max_retry 次)
#   - API 限流: 等待后重试
#   - 网络错误: 指数退避重试
#   - 所有错误都会以明确的异常类型抛出
#
# Pixiv API 要点:
#   - 作品 URL 格式: https://www.pixiv.net/artworks/{illust_id}
#   - 图片 CDN URL: 需带 Referer: https://www.pixiv.net 头
#   - R18 过滤: x_restrict 字段 (0=全年龄, 1=R18, 2=R18G)
#               search_illust 的 filter="for_ios" 可过滤大部分 R18
#
# 依赖: pip install pixivpy3 httpx
# ============================================================================

import asyncio
import time
from typing import Optional

import httpx
from pixivpy3 import AppPixivAPI

from astrbot.api import logger


# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------

class PixivAuthError(Exception):
    """Pixiv 认证失败（token 无效或过期且无法刷新）。"""
    pass


class PixivNotFoundError(Exception):
    """请求的作品 ID 不存在或已被删除。"""
    pass


class PixivRateLimitError(Exception):
    """Pixiv API 请求频率过高，被限流。"""
    pass


class PixivNetworkError(Exception):
    """网络请求失败（超时、DNS 错误等）。"""
    pass


# ---------------------------------------------------------------------------
# 返回数据模型
# ---------------------------------------------------------------------------

class IllustInfo:
    """
    Pixiv 作品信息数据类。

    从 pixivpy3 的 illust 字典中提取关键字段，方便插件其他模块使用。

    Attributes:
        illust_id:   作品 ID
        title:       作品标题
        author_name: 作者名
        author_id:   作者 ID
        image_url:   图片 URL（large 尺寸，可直接下载）
        page_url:    作品页面 URL
        tags:        标签列表 (str)
        x_restrict:  R18 级别 (0=安全, 1=R18, 2=R18G)
        bookmarks:   收藏数
        views:       浏览数
        page_count:  作品总页数（漫画/图集 > 1）
        all_page_urls: 多图作品各页的 image_urls 列表
        width:       图片宽度
        height:      图片高度
    """

    __slots__ = (
        "illust_id", "title", "author_name", "author_id",
        "image_url", "page_url", "tags", "x_restrict",
        "bookmarks", "views",
        "url_original", "url_large", "url_medium", "url_square",
        "page_count", "all_page_urls",
        "width", "height",
    )

    def __init__(self, illust_data: dict) -> None:
        self.illust_id: int = illust_data.get("id", 0)
        self.title: str = illust_data.get("title", "无标题")
        self.author_name: str = illust_data.get("user", {}).get("name", "未知作者")
        self.author_id: int = illust_data.get("user", {}).get("id", 0)
        self.page_url: str = f"https://www.pixiv.net/artworks/{self.illust_id}"
        self.x_restrict: int = illust_data.get("x_restrict", 0)
        self.bookmarks: int = illust_data.get("total_bookmarks", 0)
        self.views: int = illust_data.get("total_view", 0)

        # 多级图片 URL
        urls = illust_data.get("image_urls", {}) if isinstance(illust_data.get("image_urls"), dict) else {}
        self.image_url: str = urls.get("large", "") or urls.get("medium", "")
        self.url_original: str = illust_data.get("meta_single_page", {}).get("original_image_url", "") if isinstance(illust_data.get("meta_single_page"), dict) else ""
        self.url_large: str = urls.get("large", "")
        self.url_medium: str = urls.get("medium", "")
        self.url_square: str = urls.get("square_medium", "")

        # 标签列表
        tags_list = illust_data.get("tags", [])
        self.tags: str = " ".join(
            tag.get("name", "") for tag in tags_list if tag.get("name")
        )

        self.width: int = illust_data.get("width", 0)
        self.height: int = illust_data.get("height", 0)

        # 多图作品（漫画/图集）
        self.page_count: int = illust_data.get("page_count", 1)
        meta_pages = illust_data.get("meta_pages", [])
        if isinstance(meta_pages, list) and meta_pages:
            self.all_page_urls: list[dict] = []
            for page in meta_pages:
                if isinstance(page, dict):
                    self.all_page_urls.append(page.get("image_urls", {}))
        else:
            self.all_page_urls: list[dict] = []

    @property
    def is_r18(self) -> bool:
        """该作品是否为 R18 内容。"""
        return self.x_restrict > 0

    @property
    def quality_score(self) -> int:
        """质量分（bookmarks 为主 + views/1000 为辅）。"""
        return self.bookmarks + self.views // 1000

    def get_image_url_for_quality(self, quality: str) -> str:
        """
        根据质量设置返回对应的图片 URL。

        Args:
            quality: "original" | "large" | "medium"

        Returns:
            对应的图片 URL。
        """
        if quality == "original" and self.url_original:
            return self.url_original
        if quality == "medium" and self.url_medium:
            return self.url_medium
        # 默认 fallback: large → medium → square
        return self.url_large or self.url_medium or self.url_square or self.image_url

    @property
    def is_multi_page(self) -> bool:
        """是否为多图作品（漫画/图集）。"""
        return self.page_count > 1 and len(self.all_page_urls) > 0

    def get_page_urls_for_quality(self, quality: str, max_pages: int = 3) -> list[str]:
        """
        返回多图作品各页的图片 URL，按质量筛选，最多 max_pages 张。

        meta_pages[].image_urls 不含 original 键（仅 meta_single_page 有），
        取 original 时去掉 CDN 尺寸前缀 /c/WxH_Q/，保留 img-master 完整分辨率。
        （漫画不支持 img-original 路径，绝不能往那转）

        Args:
            quality:    "original" | "large" | "medium"
            max_pages:  最多返回页数

        Returns:
            各页图片 URL 列表。
        """
        urls = []
        for page_urls in self.all_page_urls[:max_pages]:
            if quality == "original" and page_urls.get("original"):
                urls.append(page_urls["original"])
            elif quality == "original":
                # 漫画无 original → 去 CDN 前缀，取 img-master 完整分辨率
                large_url = page_urls.get("large") or ""
                if large_url:
                    urls.append(self._strip_cdn_sizing(large_url))
            elif quality == "large" and page_urls.get("large"):
                urls.append(page_urls["large"])
            elif quality == "medium" and page_urls.get("medium"):
                urls.append(page_urls["medium"])
            else:
                # fallback: large → medium → square
                url = page_urls.get("large") or page_urls.get("medium") or page_urls.get("square_medium") or ""
                if url:
                    urls.append(url)
        return urls

    @staticmethod
    def _strip_cdn_sizing(url: str) -> str:
        """去掉 Pixiv CDN 尺寸/质量前缀 /c/WxH_Q/ → 保留 img-master 完整分辨率。

        large:   https://i.pximg.net/c/600x1200_90/img-master/.../p0_master1200.jpg
        result:  https://i.pximg.net/img-master/.../p0_master1200.jpg
        """
        import re
        return re.sub(r'/c/\d+x\d+_\d+(_webp)?/', '/', url)

    def to_message_text(self, info_config: dict | None = None) -> str | None:
        """
        生成作品信息文本。根据 info_config 中的 bool 开关控制显示内容。

        Args:
            info_config: {"show_title": True, "show_author": True, ...}
                         为 None 时默认全部显示。

        Returns:
            信息文本；如果所有开关都关闭则返回 None（不显示任何信息）。
        """
        if info_config is None:
            info_config = {
                "show_title": True, "show_author": True,
                "show_bookmarks": True, "show_tags": True, "show_link": True,
            }

        lines = []
        if info_config.get("show_title", True):
            lines.append(f"🎨 {self.title}")
        if info_config.get("show_author", True):
            lines.append(f"👤 作者: {self.author_name}")
        if self.is_r18:
            lines.insert(0, "⚠️ [R18 内容]")
        if info_config.get("show_bookmarks", True) and self.bookmarks > 0:
            lines.append(f"❤️ {self.bookmarks} 收藏")
        if info_config.get("show_tags", True) and self.tags:
            lines.append(f"🏷️ 标签: {self.tags}")
        if info_config.get("show_link", True):
            lines.append(f"🔗 {self.page_url}")

        if not lines:
            return None  # 全部关闭，不显示任何信息

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 辅助函数: 规范化 meta_pages
# ---------------------------------------------------------------------------

def _normalize_meta_pages(illust) -> list[dict]:
    """从 pixivpy3 的 illust 对象中提取 meta_pages（多图作品各页 URL）。"""
    pages = []
    raw_pages = getattr(illust, 'meta_pages', None) if hasattr(illust, 'meta_pages') else None
    if not raw_pages:
        return pages
    for p in raw_pages:
        if hasattr(p, 'image_urls'):
            pages.append({
                "image_urls": {
                    "large": getattr(p.image_urls, 'large', '') if hasattr(p.image_urls, 'large') else '',
                    "medium": getattr(p.image_urls, 'medium', '') if hasattr(p.image_urls, 'medium') else '',
                    "square_medium": getattr(p.image_urls, 'square_medium', '') if hasattr(p.image_urls, 'square_medium') else '',
                }
            })
        elif isinstance(p, dict):
            pages.append({"image_urls": p.get("image_urls", {})})
    return pages


# ---------------------------------------------------------------------------
# Pixiv 客户端
# ---------------------------------------------------------------------------

class PixivClient:
    """
    Pixiv API 客户端。

    封装 pixivpy3.AppPixivAPI，提供异步友好的接口和健壮的错误处理。

    使用示例:
        client = PixivClient(config_mgr)
        await client.login("my_refresh_token")
        info = await client.search_by_id(12345678)
        if info:
            image_bytes = await client.download_image(info.image_url)
    """

    # API 请求间隔（秒），避免触发限流
    _REQUEST_INTERVAL = 0.6

    # 下载图片时的请求头
    _DOWNLOAD_HEADERS = {
        "Referer": "https://www.pixiv.net",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    # Access Token（由 refresh_token 换取）有效期约 1 小时，每 10 分钟自动刷新（失败也按此间隔重试）
    _AUTO_REFRESH_INTERVAL = 600  # 秒

    def __init__(self, config_mgr) -> None:
        """
        Args:
            config_mgr: ConfigManager 实例，读取 refresh_token 和重试配置。
        """
        self._config = config_mgr
        self._api: Optional[AppPixivAPI] = None
        self._is_logged_in: bool = False
        self._last_request_time: float = 0.0
        # HTTP 客户端（用于下载图片，复用连接池）
        self._http: Optional[httpx.AsyncClient] = None
        # 后台 token 自动刷新任务
        self._auto_refresh_task: Optional[asyncio.Task] = None
        # 连续空搜索计数（成功搜索后清零）
        self._consecutive_empty: int = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def login(self, refresh_token: str, start_refresh_loop: bool = True) -> bool:
        """
        使用 refresh_token 登录 Pixiv。

        Args:
            refresh_token: Pixiv 的 refresh_token 字符串。
            start_refresh_loop: 是否启动后台自动刷新任务（首次登录传 True，
                                自动刷新内部调用时传 False 避免递归）。

        Returns:
            True 表示登录成功。

        Raises:
            PixivAuthError: 登录失败（token 无效或网络问题）。
        """
        self._api = AppPixivAPI()

        try:
            # pixivpy3 的 auth 方法是同步的，在线程池中执行
            await asyncio.to_thread(
                self._api.auth,
                refresh_token=refresh_token,
            )
            self._is_logged_in = True
            if self._http:
                await self._http.aclose()
            self._http = httpx.AsyncClient(
                headers=self._DOWNLOAD_HEADERS,
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
            logger.info("[pixiv:client] 登录成功")

            # 启动后台 token 自动刷新（仅首次登录）
            if start_refresh_loop:
                self._start_auto_refresh()

            return True
        except Exception as e:
            self._is_logged_in = False
            raise PixivAuthError(f"Pixiv 登录失败: {e}") from e

    async def close(self) -> None:
        """关闭 HTTP 客户端连接和后台刷新任务。"""
        self._cancel_auto_refresh()
        if self._http:
            await self._http.aclose()
            self._http = None
        self._is_logged_in = False
        logger.debug("[pixiv:client] 客户端已关闭")

    @property
    def is_logged_in(self) -> bool:
        """是否已登录。"""
        return self._is_logged_in and self._api is not None

    # ------------------------------------------------------------------
    # 核心 API: 按 ID 搜索
    # ------------------------------------------------------------------

    async def search_by_id(self, illust_id: int) -> Optional[IllustInfo]:
        """
        按作品 ID 获取作品详情。

        Args:
            illust_id: Pixiv 作品 ID（数字，如 12345678）。

        Returns:
            IllustInfo 对象；如果作品不存在返回 None。

        Raises:
            PixivAuthError: 认证失败。
            PixivNotFoundError: 作品不存在。
            PixivRateLimitError: API 限流。
            PixivNetworkError: 网络错误。
        """
        self._ensure_logged_in()

        max_retries = self._config.get("max_retry_count", 3)
        for attempt in range(max_retries):
            try:
                await self._rate_limit_wait()
                result = await asyncio.to_thread(
                    self._api.illust_detail, illust_id
                )

                logger.debug(
                    f"[pixiv:client] illust_detail result type={type(result).__name__}"
                )

                if result is None:
                    raise PixivNotFoundError(f"作品 ID {illust_id} 不存在或已被删除")

                # pixivpy3 可能返回对象而非 dict
                if hasattr(result, 'illust'):
                    illust = result.illust
                elif isinstance(result, dict) and "illust" in result:
                    illust = result["illust"]
                else:
                    logger.warning(
                        f"[pixiv:client] 无法解析 illust_detail 结果: "
                        f"type={type(result)}, keys={list(result.keys()) if isinstance(result, dict) else 'N/A'}"
                    )
                    raise PixivNotFoundError(f"作品 ID {illust_id} 不存在或已被删除")

                # illust 可能是对象或 dict
                if hasattr(illust, 'id'):
                    # 对象 → 转为 dict
                    illust_dict = {
                        "id": illust.id,
                        "title": getattr(illust, 'title', ''),
                        "user": {
                            "id": getattr(illust.user, 'id', 0) if hasattr(illust, 'user') else 0,
                            "name": getattr(illust.user, 'name', '') if hasattr(illust, 'user') else '',
                        },
                        "image_urls": {
                            "large": getattr(illust.image_urls, 'large', '') if hasattr(illust, 'image_urls') else '',
                            "medium": getattr(illust.image_urls, 'medium', '') if hasattr(illust, 'image_urls') else '',
                        },
                        "tags": [{"name": t.name} for t in getattr(illust, 'tags', [])] if hasattr(illust, 'tags') else [],
                        "x_restrict": getattr(illust, 'x_restrict', 0),
                        "width": getattr(illust, 'width', 0),
                        "height": getattr(illust, 'height', 0),
                        "page_count": getattr(illust, 'page_count', 1),
                        "meta_pages": _normalize_meta_pages(illust),
                    }
                    return IllustInfo(illust_dict)
                else:
                    return IllustInfo(illust)

            except PixivNotFoundError:
                raise
            except PixivAuthError:
                raise
            except Exception as e:
                logger.warning(
                    f"[pixiv:client] search_by_id({illust_id}) "
                    f"第 {attempt+1}/{max_retries} 次尝试失败: {e}"
                )
                if attempt == max_retries - 1:
                    raise PixivNetworkError(
                        f"按ID搜索失败（已重试 {max_retries} 次）: {e}"
                    ) from e
                await asyncio.sleep(2 ** attempt)

        return None

    # ------------------------------------------------------------------
    # 核心 API: 按标签搜索
    # ------------------------------------------------------------------

    async def search_by_tag(
        self,
        tag: str,
        page: int = 1,
        r18_mode: str = "safe",
    ) -> list[IllustInfo]:
        """按标签搜索作品。"""
        self._ensure_logged_in()

        offset = (page - 1) * 30
        max_retries = 1  # 减少重试，快速失败以便诊断

        for attempt in range(max_retries):
            try:
                await self._rate_limit_wait()

                # ---- 调用 pixivpy3 搜索 API ----
                result = await asyncio.to_thread(
                    self._api.search_illust,
                    word=tag,
                    search_target="partial_match_for_tags",
                    sort=self._config.get("search_sort", "popular_desc"),
                    duration=None,
                    offset=offset,
                )

                # ---- 诊断: 打印 result 详情 ----
                rtype = type(result).__name__
                logger.info(f"[pixiv:client] search_illust('{tag}') raw type={rtype}")

                if result is None:
                    logger.warning(f"[pixiv:client] ❌ result 为 None")
                    return []

                # 兼容对象和 dict 两种返回类型
                if hasattr(result, 'illusts'):
                    # 先检查是否有错误字段（token 过期时可能静默返回空）
                    if hasattr(result, 'error') and result.error:
                        logger.error(
                            f"[pixiv:client] ⚠️ API 返回错误 (token可能已过期): "
                            f"tag='{tag}', error={result.error}"
                        )
                        return []
                    illusts = result.illusts
                    logger.info(f"[pixiv:client] (attr) illusts count={len(illusts) if illusts else 0}")
                elif isinstance(result, dict):
                    keys = list(result.keys())
                    logger.info(f"[pixiv:client] (dict) keys={keys}")
                    if "error" in result:
                        logger.error(
                            f"[pixiv:client] ⚠️ API 返回错误 (token可能已过期): "
                            f"tag='{tag}', error={result['error']}"
                        )
                        return []
                    illusts = result.get("illusts", [])
                    logger.info(f"[pixiv:client] (dict) illusts count={len(illusts)}")
                else:
                    logger.warning(f"[pixiv:client] 未知返回类型: {rtype}, value={str(result)[:200]}")
                    return []

                if not illusts:
                    if page == 1:
                        logger.warning(
                            f"[pixiv:client] ⚠️ 首页无结果: tag='{tag}' → "
                            f"尝试刷新 Access Token 后重试..."
                        )
                        # 即时自愈: 刷新 token 后重试一次
                        token = self._config.get("pixiv_refresh_token", "")
                        if token:
                            try:
                                await self.login(token, start_refresh_loop=False)
                                await self._rate_limit_wait()
                                result = await asyncio.to_thread(
                                    self._api.search_illust,
                                    word=tag,
                                    search_target="partial_match_for_tags",
                                    sort=self._config.get("search_sort", "popular_desc"),
                                    duration=None,
                                    offset=offset,
                                )
                                if hasattr(result, 'illusts'):
                                    illusts = result.illusts
                                elif isinstance(result, dict):
                                    illusts = result.get("illusts", [])
                                else:
                                    illusts = []
                                if illusts:
                                    logger.info(
                                        f"[pixiv:client] 🔄 token 刷新后重试成功: "
                                        f"tag='{tag}', count={len(illusts)}"
                                    )
                                    # 继续下面的过滤逻辑（不 return）
                                else:
                                    self._consecutive_empty += 1
                                    logger.warning(
                                        f"[pixiv:client] ⚠️ token 刷新后仍无结果: "
                                        f"tag='{tag}' (连续空搜索 {self._consecutive_empty} 次)"
                                    )
                                    return []
                            except Exception as e:
                                self._consecutive_empty += 1
                                logger.error(
                                    f"[pixiv:client] ⚠️ token 即时刷新失败: {e}"
                                )
                                return []
                        else:
                            self._consecutive_empty += 1
                            logger.warning(
                                f"[pixiv:client] ⚠️ 首页无结果且未配置 refresh_token: "
                                f"tag='{tag}'"
                            )
                            return []
                    else:
                        logger.debug(f"[pixiv:client] 第{page}页无结果: tag='{tag}'")
                        return []

                # 转换结果 + 质量过滤 + 标签黑名单
                max_results = self._config.get("search_max_results", 10)
                min_bookmarks = self._config.get("min_bookmarks", 50)
                blacklist_raw = self._config.get("tag_blacklist", "futa")
                blacklist = set(
                    t.strip().lower() for t in blacklist_raw.split(",") if t.strip()
                )
                infos = []
                for illust_data in illusts[: max_results * 4]:
                    d = self._normalize_illust(illust_data)
                    if d is None:
                        continue
                    info = IllustInfo(d)
                    if r18_mode == "safe" and info.is_r18:
                        continue
                    if r18_mode == "r18_only" and not info.is_r18:
                        continue
                    if min_bookmarks > 0 and info.bookmarks < min_bookmarks:
                        continue
                    if blacklist:
                        ill_tags = set(t.lower() for t in info.tags.split())
                        if ill_tags & blacklist:
                            continue
                    infos.append(info)

                if not infos:
                    return []

                # 按收藏降序排列（不随机，随机在 find_fresh_illust 去重后做）
                infos.sort(key=lambda x: x.bookmarks, reverse=True)

                logger.info(
                    f"[pixiv:client] 标签'{tag}' → 候选{len(infos)}个 "
                    f"(max❤️{infos[0].bookmarks})"
                )
                self.note_search_success()
                return infos

            except PixivAuthError:
                raise
            except Exception as e:
                logger.error(f"[pixiv:client] search_by_tag 异常: {type(e).__name__}: {e}")
                return []

        return []

    # ------------------------------------------------------------------
    # 多标签搜索: 依次尝试多个标签，找到结果即返回
    # ------------------------------------------------------------------

    async def search_by_tags(
        self,
        tags: list[str],
        session_id: str,
        dedup_mgr,
        r18_mode: str = "safe",
        max_pages_per_tag: int = 0,
        enrichment: dict | None = None,
    ) -> Optional[IllustInfo]:
        """
        多维标签智能搜索。

        当提供 enrichment（结构化标签维度）时，采用分级采集+池化随机策略：
          阶段 0: game + 每组随机1个tag 全量组合（最多5次随机尝试）→ 命中即返回
          阶段 1: game × 每组随机1个tag 逐一组合 → 池化 → 加权随机
          阶段 2: 单个维度标签搜索 → 池化 → 加权随机
          阶段 3: 扁平标签逐个短路搜索（兜底）

        无 enrichment 时回退为简单的扁平标签逐个短路搜索。

        Pixiv API 以空格分隔的词做 AND 搜索，能精准定位多条件作品。
        """
        import random

        if max_pages_per_tag <= 0:
            max_pages_per_tag = self._config.get("search_max_pages", 10)

        # ---- 结构化多维搜索（有 enrichment 时） ----
        if enrichment:
            game_tags = enrichment.get("game", []) or []
            char_tags = enrichment.get("character", []) or []
            # 同义词组：[{"tags":["巨乳","おっぱい"],"label":"大胸"}, ...]
            attr_groups = enrichment.get("attributes", []) or []

            # 辅助: 从每组随机抽1个tag
            def _pick_random_tag(group: dict) -> str:
                tags = group.get("tags", [])
                return random.choice(tags) if tags else ""

            pool: list[IllustInfo] = []
            # 基础标签：游戏 + 角色（有就加上，无则为空）
            base_tags = game_tags + char_tags
            # 池积累目标（≥此停止）和最低接受（<此则降级继续）
            threshold = max(5, int(self._config.get("stage0_pool_threshold", 30) or 30))
            min_pool = max(5, int(self._config.get("stage0_min_pool", 10) or 10))
            if threshold < min_pool:
                threshold = min_pool

            # ============================================================
            # 阶段 0: 降维梯度搜索（k=N → 2）
            #   全局池跨 k 级积累，满 threshold 立即返回
            #   当前 k 搜完 < min_pool → 降级 k-1 继续积累
            #   k=2 搜完有图即返回（最后一层不降级）
            #   k=1 由阶段 1 池化处理，不重复
            # ============================================================
            if attr_groups:
                N = len(attr_groups)
                global_pool: list[IllustInfo] = []
                global_seen: set[int] = set()
                for k in range(N, 1, -1):
                    tried = set()
                    for attempt in range(5):
                        selected = attr_groups if k == N else random.sample(attr_groups, k=k)
                        picked = [_pick_random_tag(g) for g in selected]
                        picked = [t for t in picked if t]
                        if not picked:
                            break
                        combined = " ".join(base_tags + picked)
                        if combined in tried or not combined.strip():
                            continue
                        tried.add(combined)
                        sel_labels = [g.get("label", "?") for g in selected]
                        logger.info(
                            f"[pixiv:client] 🔗 阶段0(k={k}/{N}) 第{attempt+1}次: '{combined}' "
                            f"(维度: {sel_labels})"
                        )
                        fresh = await self.collect_fresh_illusts(
                            tag=combined, session_id=session_id,
                            dedup_mgr=dedup_mgr, r18_mode=r18_mode,
                            max_pages=max_pages_per_tag,
                        )
                        for info in fresh:
                            if info.illust_id not in global_seen:
                                global_seen.add(info.illust_id)
                                global_pool.append(info)

                        if len(global_pool) >= threshold:
                            logger.info(
                                f"[pixiv:client] 阶段0(k={k}) 池已达{len(global_pool)}张(≥{threshold})，停止"
                            )
                            break

                    # 判断是否接受当前结果
                    if len(global_pool) >= threshold:
                        break  # 够了，跳出 k 循环
                    if k == 2:
                        break  # 最后一层，不再降级
                    if len(global_pool) < min_pool:
                        logger.info(
                            f"[pixiv:client] 阶段0(k={k}/{N}) 池仅{len(global_pool)}张"
                            f"(<{min_pool})，降级到 k={k-1} 继续积累"
                        )
                    else:
                        break  # 虽未达阈值但 ≥ 最低接受，不降级

                if global_pool:
                    weights = [info.bookmarks + 1 for info in global_pool]
                    picked = random.choices(global_pool, weights=weights, k=1)[0]
                    logger.info(
                        f"[pixiv:client] ✅ 阶段0 从全局池({len(global_pool)}张) "
                        f"加权选中: id={picked.illust_id}, ❤️{picked.bookmarks}"
                    )
                    return picked

                logger.info(f"[pixiv:client] 阶段0 全局池0结果，进入阶段1")

            # ============================================================
            # 阶段 1: base × 每组随机1个tag 逐一组合 → 池化
            # ============================================================
            if base_tags and attr_groups:
                for ag in attr_groups:
                    tag = _pick_random_tag(ag)
                    if not tag:
                        continue
                    combined = " ".join(base_tags + [tag])
                    logger.info(f"[pixiv:client] 🔗 阶段1 组合搜索: '{combined}'")
                    result = await self.find_fresh_illust(
                        tag=combined, session_id=session_id,
                        dedup_mgr=dedup_mgr, r18_mode=r18_mode,
                        max_pages=max_pages_per_tag,
                    )
                    if result:
                        pool.append(result)
                if pool:
                    weights = [info.bookmarks + 1 for info in pool]
                    picked = random.choices(pool, weights=weights, k=1)[0]
                    logger.info(
                        f"[pixiv:client] ✅ 阶段1 从池({len(pool)}张) 加权随机选中: "
                        f"id={picked.illust_id}, ❤️{picked.bookmarks}"
                    )
                    return picked

            # ============================================================
            # 阶段 1b: character × game 组合（如有角色）
            # ============================================================
            if char_tags and game_tags:
                for c in char_tags:
                    for g in game_tags:
                        combined = f"{g} {c}"
                        logger.info(f"[pixiv:client] 🔗 阶段1b 组合搜索: '{combined}'")
                        result = await self.find_fresh_illust(
                            tag=combined, session_id=session_id,
                            dedup_mgr=dedup_mgr, r18_mode=r18_mode,
                            max_pages=max_pages_per_tag,
                        )
                        if result:
                            pool.append(result)
                if pool:
                    weights = [info.bookmarks + 1 for info in pool]
                    picked = random.choices(pool, weights=weights, k=1)[0]
                    logger.info(
                        f"[pixiv:client] ✅ 阶段1b 从池({len(pool)}张) 加权随机选中: "
                        f"id={picked.illust_id}, ❤️{picked.bookmarks}"
                    )
                    return picked

            # ============================================================
            # 阶段 2: game + character 单个标签搜索 → 池化
            #          属性标签不单独搜（太泛），只搜 game 和 character
            # ============================================================
            for tag_list_name, tag_list in [("game", game_tags), ("character", char_tags)]:
                for single_tag in tag_list:
                    logger.info(f"[pixiv:client] 🔗 阶段2 单标签搜索({tag_list_name}): '{single_tag}'")
                    result = await self.find_fresh_illust(
                        tag=single_tag, session_id=session_id,
                        dedup_mgr=dedup_mgr, r18_mode=r18_mode,
                        max_pages=max_pages_per_tag,
                    )
                    if result:
                        pool.append(result)
                if pool:
                    weights = [info.bookmarks + 1 for info in pool]
                    picked = random.choices(pool, weights=weights, k=1)[0]
                    logger.info(
                        f"[pixiv:client] ✅ 阶段2 从池({len(pool)}张) 加权随机选中: "
                        f"id={picked.illust_id}, ❤️{picked.bookmarks}"
                    )
                    return picked

            # 阶段 2 也搜属性标签的第一个 tag（作为兜底）
            for ag in attr_groups:
                first_tag = ag.get("tags", [None])[0] if ag.get("tags") else None
                if not first_tag:
                    continue
                logger.info(f"[pixiv:client] 🔗 阶段2 单标签搜索(attr): '{first_tag}'")
                result = await self.find_fresh_illust(
                    tag=first_tag, session_id=session_id,
                    dedup_mgr=dedup_mgr, r18_mode=r18_mode,
                    max_pages=max_pages_per_tag,
                )
                if result:
                    pool.append(result)
            if pool:
                weights = [info.bookmarks + 1 for info in pool]
                picked = random.choices(pool, weights=weights, k=1)[0]
                logger.info(
                    f"[pixiv:client] ✅ 阶段2(attr) 从池({len(pool)}张) 加权随机选中: "
                    f"id={picked.illust_id}, ❤️{picked.bookmarks}"
                )
                return picked

            # ============================================================
            # 阶段 3: 扁平标签逐个短路搜索（兜底）
            # ============================================================
            flat_tags = enrichment.get("flat", tags)
            for tag in flat_tags:
                logger.info(f"[pixiv:client] 阶段3 尝试标签: '{tag}'")
                result = await self.find_fresh_illust(
                    tag=tag, session_id=session_id,
                    dedup_mgr=dedup_mgr, r18_mode=r18_mode,
                    max_pages=max_pages_per_tag,
                )
                if result:
                    logger.info(f"[pixiv:client] ✅ 标签 '{tag}' 找到作品: {result.illust_id}")
                    return result
            return None

        # ---- 无 enrichment: 扁平标签逐个短路搜索（向后兼容） ----
        empty_tag_count = 0
        for tag in tags:
            logger.info(f"[pixiv:client] 尝试标签: '{tag}'")
            result = await self.find_fresh_illust(
                tag=tag, session_id=session_id,
                dedup_mgr=dedup_mgr, r18_mode=r18_mode,
                max_pages=max_pages_per_tag,
            )
            if result is not None:
                logger.info(f"[pixiv:client] ✅ 标签 '{tag}' 找到作品: {result.illust_id}")
                return result
            empty_tag_count += 1
            logger.debug(f"[pixiv:client] 标签 '{tag}' 未找到新鲜作品，尝试下一个...")

            if empty_tag_count >= 3:
                logger.error(
                    f"[pixiv:client] 🔴 连续 {empty_tag_count} 个标签首页无结果！"
                    f" 极可能是 Access Token 已失效（由 Refresh Token 换取的短期凭证），"
                    f"请在 WebUI 插件设置中重新填写 refresh_token"
                )
        return None

    # ------------------------------------------------------------------
    # 诊断: 测试 API 连通性
    # ------------------------------------------------------------------

    async def test_connection(self) -> dict:
        """
        测试 Pixiv API 连通性 —— 尝试获取排行榜。
        用于诊断 API 是否正常工作。

        Returns:
            {"ok": True/False, "sample_id": ..., "error": ...}
        """
        self._ensure_logged_in()
        try:
            await self._rate_limit_wait()
            result = await asyncio.to_thread(self._api.illust_ranking, mode="day")
            rtype = type(result).__name__
            logger.info(f"[pixiv:client] illust_ranking raw type={rtype}")

            if hasattr(result, 'illusts'):
                illusts = result.illusts
            elif isinstance(result, dict):
                illusts = result.get("illusts", [])
            else:
                return {"ok": False, "error": f"未知返回类型: {rtype}"}

            if illusts:
                first = illusts[0]
                sid = getattr(first, 'id', 0) if hasattr(first, 'id') else first.get('id', 0)
                return {"ok": True, "sample_count": len(illusts), "sample_id": sid}
            return {"ok": False, "error": "排行榜返回空列表"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 内部: 规范化 illust 数据（对象→dict）
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_illust(illust_data) -> dict | None:
        """将 pixivpy3 返回的 illust 对象或 dict 统一转为 dict。"""
        try:
            if hasattr(illust_data, 'id'):
                # 对象类型
                return {
                    "id": illust_data.id,
                    "title": getattr(illust_data, 'title', ''),
                    "user": {
                        "id": getattr(illust_data.user, 'id', 0) if hasattr(illust_data, 'user') else 0,
                        "name": getattr(illust_data.user, 'name', '') if hasattr(illust_data, 'user') else '',
                    },
                    "image_urls": {
                        "large": getattr(illust_data.image_urls, 'large', '') if hasattr(illust_data, 'image_urls') else '',
                        "medium": getattr(illust_data.image_urls, 'medium', '') if hasattr(illust_data, 'image_urls') else '',
                    },
                    "tags": [{"name": t.name} for t in getattr(illust_data, 'tags', [])] if hasattr(illust_data, 'tags') else [],
                    "x_restrict": getattr(illust_data, 'x_restrict', 0),
                    "total_bookmarks": getattr(illust_data, 'total_bookmarks', 0),
                    "total_view": getattr(illust_data, 'total_view', 0),
                    "meta_single_page": {
                        "original_image_url": getattr(
                            getattr(illust_data, 'meta_single_page', None), 'original_image_url', ''
                        ) if hasattr(illust_data, 'meta_single_page') and illust_data.meta_single_page else ''
                    },
                    "width": getattr(illust_data, 'width', 0),
                    "height": getattr(illust_data, 'height', 0),
                    "page_count": getattr(illust_data, 'page_count', 1),
                    "meta_pages": _normalize_meta_pages(illust_data),
                }
            elif isinstance(illust_data, dict):
                return illust_data
        except Exception as e:
            logger.debug(f"[pixiv:client] _normalize_illust 失败: {e}")
        return None

    # ------------------------------------------------------------------
    # 核心 API: 下载图片
    # ------------------------------------------------------------------

    async def download_image(self, image_url: str) -> Optional[bytes]:
        """
        下载图片并返回字节数据。

        Args:
            image_url: 图片 URL（来自 IllustInfo.image_url）。

        Returns:
            图片的 bytes 数据；失败返回 None。

        Raises:
            PixivNetworkError: 网络错误（重试耗尽后）。
        """
        if not self._http:
            self._http = httpx.AsyncClient(
                headers=self._DOWNLOAD_HEADERS,
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )

        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = await self._http.get(image_url)
                response.raise_for_status()
                logger.debug(
                    f"[pixiv:client] 图片下载成功: {len(response.content)} bytes"
                )
                return response.content
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.error(f"[pixiv:client] 图片 404: {image_url}")
                    return None
                if attempt == max_retries - 1:
                    raise PixivNetworkError(f"图片下载失败 (HTTP {e.response.status_code})") from e
                await asyncio.sleep(1)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise PixivNetworkError(f"图片下载失败: {e}") from e
                await asyncio.sleep(2 ** attempt)

        return None

    # ------------------------------------------------------------------
    # 便捷方法: 搜索并找到第一张未发送过的图片
    # ------------------------------------------------------------------

    async def find_fresh_illust(
        self,
        tag: str,
        session_id: str,
        dedup_mgr,
        r18_mode: str = "safe",
        max_pages: int = 0,
    ) -> Optional[IllustInfo]:
        """按标签搜索，收集所有新鲜作品后加权随机选一张。"""
        import random

        if max_pages <= 0:
            max_pages = self._config.get("search_max_pages", 10)
        min_bookmarks = self._config.get("min_bookmarks", 50)

        # 第一轮: 质量过滤 → 收集全部新鲜作品入池
        fresh_pool: list[IllustInfo] = []
        first_page_empty = False
        for page in range(1, max_pages + 1):
            results = await self.search_by_tag(tag, page=page, r18_mode=r18_mode)
            if page == 1 and not results:
                first_page_empty = True
                # 首页空 → 该标签在 Pixiv 不存在，跳过后续翻页
                logger.info(f"[pixiv:client] 标签'{tag}' 首页无结果，跳过翻页")
                break
            for info in results:
                if not dedup_mgr.is_duplicate(info.illust_id, session_id):
                    fresh_pool.append(info)

        if fresh_pool:
            # 加权随机从池中选一张
            weights = [info.bookmarks + 1 for info in fresh_pool]
            picked = random.choices(fresh_pool, weights=weights, k=1)[0]
            logger.info(
                f"[pixiv:client] 从{fresh_pool[0].bookmarks}~{fresh_pool[-1].bookmarks}❤️"
                f" 池({len(fresh_pool)}张) 随机选中: id={picked.illust_id}, ❤️{picked.bookmarks}"
            )
            self.note_search_success()
            return picked

        # 回退: 无结果 → 关闭质量过滤再搜
        # 但如果首页本就为空（标签不存在），跳过回退
        if first_page_empty:
            logger.warning(f"[pixiv:client] 标签'{tag}' 在 Pixiv 无结果，跳过回退")
            return None

        if min_bookmarks > 0:
            logger.info(f"[pixiv:client] 标签'{tag}' 高质量无结果，回退找最佳...")
            best, best_bm = None, -1
            for page in range(1, max_pages + 1):
                try:
                    await self._rate_limit_wait()
                    result = await asyncio.to_thread(
                        self._api.search_illust, word=tag,
                        search_target="partial_match_for_tags",
                        sort=self._config.get("search_sort", "popular_desc"),
                        offset=(page - 1) * 30,
                    )
                    # 检测 token 过期等 API 错误（与 search_by_tag 保持一致）
                    if hasattr(result, 'error') and result.error:
                        logger.error(f"[pixiv:client] 回退搜索 API 错误: {result.error}")
                        continue
                    if isinstance(result, dict) and result.get("error"):
                        logger.error(f"[pixiv:client] 回退搜索 API 错误: {result['error']}")
                        continue
                    illusts = result.illusts if hasattr(result, 'illusts') else result.get("illusts", []) if isinstance(result, dict) else []
                    for d in illusts[:20]:
                        d = self._normalize_illust(d)
                        if not d:
                            continue
                        info = IllustInfo(d)
                        if r18_mode == "safe" and info.is_r18:
                            continue
                        if r18_mode == "r18_only" and not info.is_r18:
                            continue
                        blacklist_raw = self._config.get("tag_blacklist", "futa")
                        blacklist = set(t.strip().lower() for t in blacklist_raw.split(",") if t.strip())
                        if blacklist:
                            ill_tags = set(t.lower() for t in info.tags.split())
                            if ill_tags & blacklist:
                                continue
                        if dedup_mgr.is_duplicate(info.illust_id, session_id):
                            continue
                        if info.bookmarks > best_bm:
                            best_bm, best = info.bookmarks, info
                except Exception:
                    continue
            if best:
                logger.info(f"[pixiv:client] 回退: illust_id={best.illust_id}, ❤️{best.bookmarks}")
                return best

        logger.warning(f"[pixiv:client] 搜索 {max_pages} 页未找到新鲜作品: tag='{tag}'")
        return None

    # ------------------------------------------------------------------
    # 便捷方法: 收集所有未发送的新鲜作品（不做加权随机）
    # ------------------------------------------------------------------

    async def collect_fresh_illusts(
        self,
        tag: str,
        session_id: str,
        dedup_mgr,
        r18_mode: str = "safe",
        max_pages: int = 0,
    ) -> list[IllustInfo]:
        """按标签搜索，收集所有新鲜作品列表（不做加权随机）。用于阶段0池积累。"""
        if max_pages <= 0:
            max_pages = self._config.get("search_max_pages", 10)

        fresh_pool: list[IllustInfo] = []
        for page in range(1, max_pages + 1):
            results = await self.search_by_tag(tag, page=page, r18_mode=r18_mode)
            if page == 1 and not results:
                logger.info(f"[pixiv:client] collect: 标签'{tag}' 首页无结果，跳过翻页")
                break
            for info in results:
                if not dedup_mgr.is_duplicate(info.illust_id, session_id):
                    fresh_pool.append(info)

        if fresh_pool:
            self.note_search_success()
        return fresh_pool

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _ensure_logged_in(self) -> None:
        """确保已登录，否则抛出异常。"""
        if not self.is_logged_in:
            raise PixivAuthError(
                "未登录 Pixiv。请先配置 pixiv_refresh_token。\n"
                "使用指令: /pixiv config set pixiv_refresh_token <your_token>"
            )

    def note_search_success(self) -> None:
        """搜索成功后重置连续空搜索计数。"""
        if self._consecutive_empty > 0:
            logger.debug(f"[pixiv:client] 搜索恢复，重置空搜索计数 (之前 {self._consecutive_empty} 次)")
        self._consecutive_empty = 0

    def get_empty_search_hint(self) -> str:
        """根据连续空搜索次数返回用户友好的建议。"""
        n = self._consecutive_empty
        if n <= 0:
            return ""
        if n == 1:
            return "💡 提示：如果持续无结果，可能是 Access Token 已过期，等待自动刷新或重启插件。"
        if n == 2:
            return (
                "💡 已连续 2 次搜索无结果。建议：\n"
                "• 检查 Pixiv 是否可正常访问\n"
                "• 尝试在 WebUI 重新填写 Refresh Token\n"
                "• 或等待插件自动刷新（最长 10 分钟）"
            )
        # n >= 3
        return (
            f"⚠️ 已连续 {n} 次搜索无结果！强烈建议：\n"
            f"• 立即在 WebUI 插件设置中重新填写 Refresh Token\n"
            f"• 使用 /pixiv test 诊断 API 连通性\n"
            f"• 检查网络是否能访问 pixiv.net"
        )

    # ------------------------------------------------------------------
    # 后台 token 自动刷新
    # ------------------------------------------------------------------

    def _start_auto_refresh(self) -> None:
        """启动后台 token 自动刷新任务（幂等：已存在则跳过）。"""
        if self._auto_refresh_task and not self._auto_refresh_task.done():
            return
        self._auto_refresh_task = asyncio.create_task(self._auto_refresh_loop())
        logger.debug(
            f"[pixiv:client] 后台 token 自动刷新已启动 "
            f"(每 {self._AUTO_REFRESH_INTERVAL}s)"
        )

    def _cancel_auto_refresh(self) -> None:
        """取消后台 token 自动刷新任务。"""
        if self._auto_refresh_task and not self._auto_refresh_task.done():
            self._auto_refresh_task.cancel()
            logger.debug("[pixiv:client] 后台 token 自动刷新已取消")
        self._auto_refresh_task = None

    async def _auto_refresh_loop(self) -> None:
        """
        后台循环：每隔 _AUTO_REFRESH_INTERVAL 秒用 refresh_token 重新认证。

        Pixiv Access Token（由用户配置的 Refresh Token 换取）有效期约 1 小时，过期后搜索 API 会静默返回空列表。
        提前定时刷新可避免用户突然搜不到图的"幽灵故障"。
        """
        while True:
            await asyncio.sleep(self._AUTO_REFRESH_INTERVAL)
            token = self._config.get("pixiv_refresh_token", "")
            if not token:
                logger.warning("[pixiv:client] ⚠️ 未配置 refresh_token，跳过自动刷新")
                continue
            try:
                # start_refresh_loop=False 避免自己递归调用自己
                await self.login(token, start_refresh_loop=False)
                logger.info("[pixiv:client] 🔄 token 自动刷新成功")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"[pixiv:client] ⚠️ token 自动刷新失败（将在 "
                    f"{self._AUTO_REFRESH_INTERVAL}s 后重试）: {e}"
                )

    async def _rate_limit_wait(self) -> None:
        """
        限流控制：确保两次 API 调用之间至少有 _REQUEST_INTERVAL 秒间隔。
        """
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._REQUEST_INTERVAL:
            wait = self._REQUEST_INTERVAL - elapsed
            await asyncio.sleep(wait)
        self._last_request_time = time.monotonic()
