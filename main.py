# ============================================================================
# astrbot_plugin_search_pixiv_pic — 插件入口 (main.py)
# ============================================================================
# Pixiv 图片检索插件，支持指令和自然语言两种交互方式。
#
# 设置方式：AstrBot WebUI → 插件管理 → astrbot_plugin_search_pixiv_pic → 设置
#   （基于 _conf_schema.json 自动生成设置表单，无需手动输指令配置）
#
# 触发条件：私聊直接触发，群聊需 @机器人。
# ============================================================================

import asyncio
import os
import re
import sys
from typing import Optional

# 确保插件目录在 Python 搜索路径中，使 src/ 子模块可被导入
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star
from astrbot.api import logger


# ---------------------------------------------------------------------------
# 自定义消息过滤器 —— 决定哪些消息进入自然语言处理流程
# ---------------------------------------------------------------------------

class PixivMessageFilter(CustomFilter):
    """
    拦截所有消息，私聊全部通过，群聊仅 @机器人 的消息通过。
    具体意图分类由 handler 内部完成。
    """

    def __init__(self, raise_error: bool = False):
        super().__init__(raise_error=raise_error)

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        """
        判断消息是否应被拦截处理。

        Returns:
            True: 消息进入 natural_language_handler 处理
            False: 消息被忽略
        """
        message = event.message_str.strip()
        if not message:
            return False

        # 跳过 /pixiv 显式指令（由 command 处理器单独处理）
        if message.startswith("/pixiv") or message.startswith("/PIXIV"):
            return False

        # 私聊：全部通过
        if event.is_private_chat():
            return True

        # 群聊：必须 @机器人
        if event.is_at_or_wake_command:
            return True

        return False


class AstrBotPixivPlugin(Star):
    """
    AstrBot Pixiv 图片检索插件主类。

    通过 AstrBot 的 filter 装饰器注册命令和消息拦截器。
    config 参数由 AstrBot 自动注入（基于 _conf_schema.json）。
    """

    def __init__(self, context: Context, config: dict = None) -> None:
        """
        Args:
            context: AstrBot 上下文对象。
            config:  AstrBot 注入的配置字典（AstrBotConfig），
                     基于 _conf_schema.json 由 WebUI 管理。
        """
        super().__init__(context)

        # 子模块实例（在 initialize() 中初始化）
        self.pixiv_client = None
        self.dedup_mgr = None
        self.config_mgr = None
        self.conv_state = None
        self.intent_parser = None

        # 保存原始 config 引用（供 R18 切换时持久化）
        self._raw_config = config

        self._cleanup_task: Optional[asyncio.Task] = None

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self) -> None:
        logger.info("=" * 50)
        logger.info("[pixiv] 🎨 astrbot_plugin_search_pixiv_pic 正在初始化...")
        logger.info("=" * 50)

        from src.config_manager import ConfigManager
        from src.pixiv_client import PixivClient
        from src.dedup_manager import DedupManager
        from src.conversation_state import ConversationStateManager
        from src.intent_parser import IntentParser

        # 1. 配置管理器（包装 AstrBotConfig）
        self.config_mgr = ConfigManager(self._raw_config)

        # 2. Pixiv 客户端
        self.pixiv_client = PixivClient(self.config_mgr)
        refresh_token = self.config_mgr.get("pixiv_refresh_token")
        if refresh_token:
            try:
                await self.pixiv_client.login(refresh_token)
                logger.info("[pixiv] ✅ Pixiv API 登录成功")
                # 诊断: 测试 API 连通性
                test_result = await self.pixiv_client.test_connection()
                if test_result.get("ok"):
                    logger.info(
                        f"[pixiv] ✅ API 连通性正常, "
                        f"排行榜样本数={test_result.get('sample_count')}, "
                        f"样本ID={test_result.get('sample_id')}"
                    )
                else:
                    logger.warning(
                        f"[pixiv] ⚠️ API 连通性异常: {test_result.get('error')}"
                    )
            except Exception as e:
                logger.warning(f"[pixiv] ⚠️ Pixiv API 登录失败: {e}")
        else:
            logger.warning("[pixiv] ⚠️ 未配置 pixiv_refresh_token")
            logger.warning("[pixiv] 请在 WebUI 插件设置页面配置 refresh_token")

        # 3. 去重管理器
        self.dedup_mgr = DedupManager(self.context, self.config_mgr)
        await self.dedup_mgr.initialize()

        # 4. 对话状态机
        self.conv_state = ConversationStateManager()

        # 5. 意图解析器（传入 context 以支持独立 LLM 调用）
        self.intent_parser = IntentParser(self.config_mgr, self.context)

        # 6. 注册 LLM Tools
        self._register_llm_tools()

        # 7. 超时清理
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info("[pixiv] ✅ 插件初始化完成！")
        logger.info("[pixiv] 设置: WebUI → 插件 → astrbot_plugin_search_pixiv_pic → 设置")
        logger.info("[pixiv] 指令: /pixiv id|tag|r18|help")
        logger.info("=" * 50)

    async def terminate(self) -> None:
        logger.info("[pixiv] 插件正在终止...")
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        if self.pixiv_client:
            await self.pixiv_client.close()
        if self.dedup_mgr:
            self.dedup_mgr.close()
        if self.conv_state:
            self.conv_state.clear_all()
        logger.info("[pixiv] ✅ 插件已终止")

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                if self.conv_state:
                    await self.conv_state.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[pixiv] 清理循环异常: {e}")

    # ==================================================================
    # LLM Tool 注册
    # ==================================================================

    def _register_llm_tools(self) -> None:
        try:
            self.context.register_llm_tool(
                name="pixiv_search_by_id",
                func_args=[
                    {"type": "integer", "name": "illust_id", "description": "Pixiv 作品 ID"}
                ],
                desc="按 Pixiv 作品 ID 搜索插画。illust_id 为数字 ID。",
                func_obj=self._tool_search_by_id,
            )
            self.context.register_llm_tool(
                name="pixiv_search_by_tag",
                func_args=[
                    {"type": "string", "name": "tag", "description": "搜索标签/关键词"}
                ],
                desc="按标签/关键词搜索 Pixiv 插画。tag 为搜索关键词。",
                func_obj=self._tool_search_by_tag,
            )
            self.context.register_llm_tool(
                name="pixiv_toggle_r18",
                func_args=[
                    {"type": "boolean", "name": "enable", "description": "true=过滤R18, false=不过滤"}
                ],
                desc="开启或关闭 Pixiv R18 内容过滤。enable=true 表示过滤。",
                func_obj=self._tool_toggle_r18,
            )
            logger.info("[pixiv] ✅ 已注册 3 个 LLM Tools")
        except Exception as e:
            logger.warning(f"[pixiv] LLM Tool 注册失败: {e}")

    async def _tool_search_by_id(self, event, context, illust_id: int = 0) -> str:
        if not illust_id:
            return "[pixiv] 请提供作品 ID"
        return f"[pixiv] 请使用 /pixiv id {illust_id} 指令来搜索该作品"

    async def _tool_search_by_tag(self, event, context, tag: str = "") -> str:
        if not tag:
            return "[pixiv] 请提供搜索标签"
        return f"[pixiv] 请使用 /pixiv tag {tag} 指令来搜索相关作品"

    async def _tool_toggle_r18(self, event, context, enable: bool = False) -> str:
        action = "开启" if enable else "关闭"
        return f"[pixiv] R18 过滤已{action}"

    # ==================================================================
    # /pixiv 命令组
    # ==================================================================

    @filter.command_group("pixiv")
    async def pixiv_group(self):
        pass

    # ------------------------------------------------------------------
    # /pixiv id <illust_id>
    # ------------------------------------------------------------------

    @pixiv_group.command("id")
    async def cmd_pixiv_id(self, event: AstrMessageEvent, illust_id: str = ""):
        """按 Pixiv 作品 ID 搜索插画。"""
        if not self.pixiv_client or not self.pixiv_client.is_logged_in:
            yield event.plain_result(
                "⚠️ Pixiv 未登录。请在 WebUI 插件设置页面配置 refresh_token。"
            )
            return

        illust_id_int = self.intent_parser._extract_illust_id(
            illust_id if illust_id else event.message_str
        )
        if not illust_id_int:
            yield event.plain_result("❌ 请提供有效的作品 ID。\n示例: /pixiv id 12345678")
            return

        yield event.plain_result(f"🔍 正在查找作品 ID: {illust_id_int} ...")

        try:
            info = await self.pixiv_client.search_by_id(illust_id_int)
            if info is None:
                yield event.plain_result(f"❌ 作品 ID {illust_id_int} 不存在或已被删除。")
                return

            r18_mode = self.config_mgr.r18_mode
            if r18_mode == "safe" and info.is_r18:
                yield event.plain_result(
                    f"⚠️ 该作品为 R18 内容，当前已开启 R18 过滤。\n"
                    f"如需查看，请在 WebUI 设置中关闭 R18 过滤，或使用 /pixiv r18 off"
                )
                return

            session_id = event.get_session_id()
            async for r in self._send_illust_images(event, info, session_id, 1, 1):
                yield r

        except Exception as e:
            logger.error(f"[pixiv] cmd_pixiv_id 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 搜索失败: {e}")

    # ------------------------------------------------------------------
    # /pixiv tag <tag>
    # ------------------------------------------------------------------

    @pixiv_group.command("tag")
    async def cmd_pixiv_tag(self, event: AstrMessageEvent, tag: str = "", count: int = 1):
        """按标签搜索 Pixiv 插画，支持多张。"""
        await self._search_and_send_images(event, tag, count)

    # ------------------------------------------------------------------
    # /pixiv r18 <on|off>
    # ------------------------------------------------------------------

    @pixiv_group.command("r18")
    async def cmd_pixiv_r18(self, event: AstrMessageEvent, action: str = ""):
        """R18 模式: safe / off / r18_only（切换需管理员权限）。"""
        action_lower = action.lower().strip() if action else ""
        mode_labels = {"safe": "过滤 R18（全年龄）🔒", "off": "关闭过滤（全部）🔓", "r18_only": "只看 R18 ⚠️"}
        current = self.config_mgr.r18_mode

        # 查看模式：任何人可看
        if not action_lower:
            yield event.plain_result(
                f"📋 当前 R18 模式: {mode_labels.get(current, current)}\n"
                f"切换: /pixiv r18 safe / off / r18_only"
            )
            return

        # 切换模式：需要管理员权限
        admin_id = self.config_mgr.get("r18_admin_id", "").strip()
        sender_id = event.get_sender_id()
        if admin_id and str(sender_id) != admin_id:
            yield event.plain_result("⛔ 只有插件管理员才能切换 R18 模式。")
            return

        if action_lower in ("safe", "过滤", "开启"):
            self.config_mgr.set("r18_mode", "safe")
            await self._save_config()
            yield event.plain_result("🔒 已切换为「过滤 R18」模式。")
        elif action_lower in ("off", "关闭", "全部"):
            self.config_mgr.set("r18_mode", "off")
            await self._save_config()
            yield event.plain_result("🔓 已切换为「关闭过滤」模式。")
        elif action_lower in ("r18_only", "仅r18", "只看r18"):
            self.config_mgr.set("r18_mode", "r18_only")
            await self._save_config()
            yield event.plain_result("⚠️ 已切换为「只看 R18」模式。")
        else:
            yield event.plain_result(
                f"❌ 无效参数「{action}」。\n"
                f"• /pixiv r18 safe    — 过滤 R18\n"
                f"• /pixiv r18 off     — 关闭过滤\n"
                f"• /pixiv r18 r18_only — 只看 R18"
            )

    # ------------------------------------------------------------------
    # /pixiv test — 诊断 API 连通性
    # ------------------------------------------------------------------

    @pixiv_group.command("test")
    async def cmd_pixiv_test(self, event: AstrMessageEvent):
        """测试 Pixiv API 连通性。"""
        if not self.pixiv_client or not self.pixiv_client.is_logged_in:
            yield event.plain_result("⚠️ 未登录，无法测试。")
            return
        yield event.plain_result("🔍 正在测试 Pixiv API 连通性...")
        test = await self.pixiv_client.test_connection()
        if test.get("ok"):
            yield event.plain_result(
                f"✅ API 正常！\n"
                f"排行榜样本: {test.get('sample_count')} 个\n"
                f"样本作品ID: {test.get('sample_id')}"
            )
        else:
            yield event.plain_result(f"❌ API 异常: {test.get('error')}")

    # ------------------------------------------------------------------
    # /pixiv help
    # ------------------------------------------------------------------

    @pixiv_group.command("help")
    async def cmd_pixiv_help(self, event: AstrMessageEvent):
        """显示帮助信息。"""
        help_text = (
            "🎨 **Pixiv 图片检索插件 使用帮助**\n\n"
            "**指令列表:**\n"
            "• `/pixiv id <作品ID>` — 按作品 ID 搜索\n"
            "  例: `/pixiv id 12345678`\n\n"
            "• `/pixiv tag <标签>` — 按标签搜索\n"
            "  例: `/pixiv tag 猫耳少女`\n\n"
            "• `/pixiv r18 <safe/off/r18_only>` — R18 模式切换\n"
            "  例: `/pixiv r18 off` 关闭过滤\n\n"
            "• `/pixiv help` — 显示本帮助\n\n"
            "**⚙️ 设置方式:**\n"
            "WebUI → 插件管理 → astrbot_plugin_search_pixiv_pic → 设置\n"
            "包括: Pixiv Token、R18 过滤、去重数量等\n\n"
            "**自然语言:**\n"
            "也可以直接说「找一张猫娘的图」自动搜索\n"
            "⚠️ 群聊中需 @我 才能触发"
        )
        yield event.plain_result(help_text)

    # ------------------------------------------------------------------
    # /pixiv config — 仅查看（设置请用 WebUI）
    # ------------------------------------------------------------------

    @pixiv_group.command("config")
    async def cmd_pixiv_config(self, event: AstrMessageEvent):
        """查看当前配置（只读）。如需修改请使用 WebUI 设置页面。"""
        all_config = self.config_mgr.get_all()
        lines = [
            "📋 **当前配置（只读）**",
            "💡 修改设置请前往: WebUI → 插件 → astrbot_plugin_search_pixiv_pic → 设置",
            "",
        ]
        for k, v in all_config.items():
            if "token" in k and v:
                display_v = str(v)[:8] + "****" + str(v)[-4:] if len(str(v)) > 12 else "****"
            else:
                display_v = v
            lines.append(f"• `{k}` = {display_v}")
        yield event.plain_result("\n".join(lines))

    # ==================================================================
    # 自然语言消息拦截器
    # ==================================================================

    @filter.custom_filter(PixivMessageFilter)
    async def natural_language_handler(self, event: AstrMessageEvent):
        """自然语言消息处理器。由 PixivMessageFilter 预过滤。"""
        message = event.message_str.strip()
        if not message:
            return

        # 群聊消息去除 @前缀
        if not event.is_private_chat():
            message = self._clean_at_prefix(message)
        if not message.strip():
            return

        # 跳过 pixiv 指令（已被 command handler 处理，避免重复执行）
        if message.startswith("pixiv ") or message.startswith("/pixiv"):
            event.continue_event()
            return

        # 跳过其他插件的指令（以 / 开头但不是 /pixiv）
        # 无需 LLM 识别，直接透传给 AstrBot
        if message.startswith("/"):
            event.clear_result()
            event.continue_event()
            return

        session_id = event.get_session_id()

        from src.intent_parser import IntentType

        intent_result = await self.intent_parser.parse(
            message=message, session_id=session_id, event=event,
        )
        logger.info(
            f"[pixiv] 意图: {intent_result.intent_type.name} "
            f"(conf={intent_result.confidence:.2f}) msg=\"{message[:40]}\""
        )

        intent_type = intent_result.intent_type
        if intent_type == IntentType.FIND_BY_ID:
            async for r in self._nl_find_by_id(intent_result, event, session_id):
                yield r
            event.stop_event()
        elif intent_type == IntentType.FIND_BY_TAG:
            async for r in self._nl_find_by_tag(intent_result, event, session_id):
                yield r
            event.stop_event()
        elif intent_type == IntentType.TOGGLE_R18:
            async for r in self._nl_toggle_r18(intent_result, event):
                yield r
            event.stop_event()
        elif intent_type == IntentType.HELP:
            async for r in self.cmd_pixiv_help(event):
                yield r
            event.stop_event()
        elif intent_type == IntentType.UNKNOWN:
            # 意图不明 → 直接放行，不反问不拦截
            event.clear_result()
            event.continue_event()
            # 清理可能残留的等待状态
            await self.conv_state.clear(session_id)
        else:
            # 非图片请求 → 清除插件空结果，原话交给 AstrBot
            event.clear_result()
            event.continue_event()

    # ==================================================================
    # 自然语言子处理
    # ==================================================================

    async def _nl_find_by_id(self, intent_result, event, session_id):
        illust_id = intent_result.params.get("illust_id")
        if not illust_id:
            # 缺少 ID → 静默放行，不反问
            event.clear_result()
            event.continue_event()
            return
        async for r in self.cmd_pixiv_id(event, str(illust_id)):
            yield r

    async def _nl_find_by_tag(self, intent_result, event, session_id):
        """自然语言 → 按标签搜索。"""
        tag = intent_result.params.get("tag")
        if not tag:
            # 缺少标签 → 静默放行，不反问
            event.clear_result()
            event.continue_event()
            return
        raw_count = intent_result.params.get("count", 0)
        max_n = self.config_mgr.get("max_images_per_request", 3)
        count = max_n if raw_count <= 0 else min(raw_count, max_n)
        async for r in self._search_and_send_images(event, tag, count):
            yield r

    async def _nl_toggle_r18(self, intent_result, event):
        enable = intent_result.params.get("enable")
        action = "on" if enable else "off"
        async for r in self.cmd_pixiv_r18(event, action):
            yield r

    # ==================================================================
    # 核心: 按标签搜索并发送多张图片
    # ==================================================================

    async def _search_and_send_images(
        self, event: AstrMessageEvent, tag: str, count: int = 1
    ):
        """
        搜索并发送指定数量的图片。

        Args:
            event: 消息事件。
            tag:   用户原始搜索标签。
            count: 需要的图片数量（不超过 max_images_per_request）。
        """
        if not self.pixiv_client or not self.pixiv_client.is_logged_in:
            yield event.plain_result(
                "⚠️ Pixiv 未登录。请在 WebUI 插件设置页面配置 refresh_token。"
            )
            return

        if not tag:
            yield event.plain_result("❌ 请提供搜索标签。")
            return

        r18_mode = self.config_mgr.r18_mode
        session_id = event.get_session_id()
        max_n = self.config_mgr.get("max_images_per_request", 3)
        count = max(1, min(count, max_n))

        # ---- 角色智能解析（识别游戏+角色，必要时联网搜索） ----
        resolve_result = await self.intent_parser.resolve_search_intent(tag, event, event.unified_msg_origin)
        search_tag = tag  # 用于标签富化的输入
        if resolve_result.get("resolved") and resolve_result.get("character"):
            game = resolve_result.get("game", "")
            char = resolve_result.get("character", "")
            if game and char:
                search_tag = f"{game} {char}"
                logger.info(f"[pixiv] 🎯 角色解析: '{tag}' → game='{game}', char='{char}'")
            elif char:
                search_tag = char
                logger.info(f"[pixiv] 🎯 角色解析: '{tag}' → char='{char}'")

        # 标签富化（返回结构化多维标签 dict）
        enrichment = await self.intent_parser.enrich_tags(search_tag)
        flat_tags = enrichment.get("flat", [tag])

        # 拟人化搜索提示
        if count > 1:
            hint = f"帮你找 {count} 张 {tag} 的图~"
        else:
            hint = f"帮你找找 {tag} 的图~"
        if self.config_mgr.get("humanized_reply_enabled", True):
            # 注入当前 AstrBot 人格提示词，让回复带有人格风味
            await self._inject_persona(event)
            reply = await self.intent_parser.generate_search_reply(tag, flat_tags)
            # 清洗 LLM 可能生成的 HTML 实体（如 &happy&）
            import html as _html
            reply = _html.unescape(reply)
            # 如果 LLM 返回了数量信息，保留它
            if "张" not in reply and count > 1:
                reply = hint
        else:
            reply = hint
        yield event.plain_result(reply)

        sent = 0
        for i in range(count):
            try:
                info = await self.pixiv_client.search_by_tags(
                    tags=flat_tags, session_id=session_id,
                    dedup_mgr=self.dedup_mgr, r18_mode=r18_mode,
                    enrichment=enrichment,
                )
                if info is None:
                    if sent == 0:
                        hint = self.pixiv_client.get_empty_search_hint()
                        msg = f"😔 没找到 {tag} 相关的图，换个关键词试试？"
                        if hint:
                            msg += f"\n\n{hint}"
                        yield event.plain_result(msg)
                    else:
                        yield event.plain_result(f"（共找到 {sent} 张，没有更多了）")
                    return

                async for r in self._send_illust_images(
                    event, info, session_id, sent + 1, count,
                    enrichment=enrichment,
                ):
                    yield r
                sent += 1

            except Exception as e:
                logger.error(f"[pixiv] _search_and_send_images 第{i}张异常: {e}")
                continue

    # ==================================================================
    # 核心: 发送单张作品（支持多页漫画/图集 + 定时撤回 + 内容审核）
    # ==================================================================

    async def _send_illust_images(
        self, event: AstrMessageEvent, info,
        session_id: str, index: int = 1, total: int = 1,
        enrichment: dict | None = None,
    ):
        """发送作品图片，支持定时撤回、内容审核、标签匹配度检查。"""
        quality = self.config_mgr.get("image_quality", "large")
        info_cfg = self.config_mgr.get("show_image_info", {})
        max_pages = self.config_mgr.get("max_pages_per_illust", 3)
        recall_sec = self.config_mgr.get("recall_after_seconds", 60)
        moderation_on = self.config_mgr.get("content_moderation_enabled", False)

        if info.is_multi_page:
            # 多图作品取 img-master 完整分辨率（去 CDN 尺寸前缀）
            page_urls = info.get_page_urls_for_quality("original", max_pages)
            total_pages = info.page_count
            for pi, page_url in enumerate(page_urls):
                page_bytes = await self.pixiv_client.download_image(page_url)
                if page_bytes is None:
                    continue
                img_path = self._save_temp_image(page_bytes, f"{info.illust_id}_p{pi+1}")
                page_label = f"({pi+1}/{min(total_pages, max_pages)})"
                if total > 1:
                    page_label = f"[{index}/{total}] {page_label}"
                msg_parts = [page_label]
                txt = info.to_message_text(info_cfg)
                if txt:
                    msg_parts.append(txt)
                text = "\n".join(msg_parts)

                msg_id = await self._send_image(event, img_path, text)
                if msg_id:
                    self._schedule_recall(event, msg_id, recall_sec)
                else:
                    # 底层发送失败，回退到标准 yield 方式
                    result = event.make_result()
                    result.file_image(img_path)
                    if text:
                        result.message(text)
                    yield result
                self._cleanup_temp_file(img_path)
                if moderation_on:
                    apology = await self._check_and_moderate(event, page_bytes, info, img_path, msg_id)
                    if apology:
                        yield event.plain_result(apology)

            self.dedup_mgr.mark_sent(info.illust_id, session_id)
            if total_pages > max_pages:
                yield event.plain_result(
                    f"📄 该作品共 {total_pages} 页，已发送前 {max_pages} 页。\n"
                    f"完整内容请访问: {info.page_url}"
                )
        else:
            dl_url = info.get_image_url_for_quality(quality)
            image_bytes = await self.pixiv_client.download_image(dl_url)
            if image_bytes is None:
                yield event.plain_result(f"❌ 图片下载失败，请稍后重试。\n{info.page_url}")
                return

            img_path = self._save_temp_image(image_bytes, info.illust_id)
            msg = info.to_message_text(info_cfg)
            if total > 1 and msg:
                msg = f"({index}/{total})\n{msg}"
            text = msg or ""

            msg_id = await self._send_image(event, img_path, text)
            if msg_id:
                self._schedule_recall(event, msg_id, recall_sec)
            else:
                # 底层发送失败，回退到标准 yield 方式
                result = event.make_result()
                result.file_image(img_path)
                if text:
                    result.message(text)
                yield result
            self._cleanup_temp_file(img_path)
            if moderation_on:
                apology = await self._check_and_moderate(event, image_bytes, info, img_path, msg_id)
                if apology:
                    yield event.plain_result(apology)

            self.dedup_mgr.mark_sent(info.illust_id, session_id)

        # ---- 标签匹配度检查 ----
        if enrichment:
            mismatch = await self._check_tag_coverage(event, info, enrichment)
            if mismatch:
                yield event.plain_result(mismatch)

    # ==================================================================
    # 标签匹配度检查
    # ==================================================================

    async def _check_tag_coverage(
        self, event: AstrMessageEvent, info, enrichment: dict,
    ) -> str | None:
        """
        对比图片标签与用户要求的维度，缺失属性时生成歉语。

        Returns:
            歉语文本，如果所有维度都覆盖则返回 None。
        """
        # 收集图片的标签集合（小写）
        img_tags = set(t.lower() for t in info.tags.split() if t.strip())

        # 检查 attributes 维度覆盖情况
        # 兼容两种格式：旧格式 ["巨乳", "黒タイツ"] 和新格式同义词组
        wanted_attrs = enrichment.get("attributes", []) or []
        missing = []
        if wanted_attrs and isinstance(wanted_attrs[0], dict):
            # 新格式: 同义词组 [{"tags":["巨乳","おっぱい"],"label":"大胸"}, ...]
            for group in wanted_attrs:
                tags = group.get("tags", [])
                label = group.get("label", "")
                # 任一个 tag 是图片标签的子串即覆盖
                #   例: 图片标签 "着衣巨乳" 被子串 "巨乳" 命中
                covered = False
                for tag in tags:
                    tag_low = tag.lower()
                    for it in img_tags:
                        if tag_low in it:
                            covered = True
                            break
                    if covered:
                        break
                if covered:
                    continue
                missing.append(label or tags[0] if tags else "?")
        else:
            # 旧格式: 扁平列表（向后兼容，子串匹配）
            for attr in wanted_attrs:
                if isinstance(attr, str):
                    if not any(attr.lower() in it for it in img_tags):
                        missing.append(attr)

        # 检查 game 维度（子串匹配，game 缺失不报告仅记录）
        wanted_games = enrichment.get("game", []) or []
        game_covered = any(
            g.lower() in it for g in wanted_games for it in img_tags
        )

        # 检查 character 维度（子串匹配，character 缺失不报告仅记录）
        wanted_chars = enrichment.get("character", []) or []
        char_covered = any(
            c.lower() in it for c in wanted_chars for it in img_tags
        )

        if not missing:
            return None

        logger.info(
            f"[pixiv] 🏷️ illust_id={info.illust_id}, "
            f"missing attributes: {missing}, "
            f"img_tags: {list(img_tags)[:15]}"
        )

        # 用人格预设 LLM 生成歉语
        try:
            await self._inject_persona(event)
            persona = self.intent_parser._persona_prompt or ""
            missing_str = "、".join(missing)
            prompt = (
                f"用户搜索时想要包含这些特征：{missing_str}，但找到的图片（{info.title}）"
                f"可能缺少这些元素。请以拟人化方式简短说明（不超过30字），"
                f"表示部分特征没找到但图片也不错。"
            )
            reply = await self.intent_parser._call_llm(
                prompt=prompt,
                system_prompt=persona or "你是友好的 Pixiv 搜索助手。",
            )
            if reply and len(reply.strip()) >= 3:
                import html as _html
                return f"💬 {_html.unescape(reply.strip())}"
        except Exception:
            pass

        # 回退：固定模板
        return f"💬 这张图可能缺少「{'、'.join(missing[:2])}」的元素，但我觉得也不错~"

    # ==================================================================
    # 图片发送（低级 API，返回 message_id 用于撤回）
    # ==================================================================

    async def _send_image(self, event: AstrMessageEvent, img_path: str, text: str) -> int | None:
        """
        通过 QQ 底层 API 发送图片+文字，返回 message_id。
        失败时回退到 yield 方式（无 message_id）。
        """
        try:
            import base64
            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            cq_msg = f"[CQ:image,file=base64://{b64}]"
            if text:
                cq_msg += f"\n{text}"

            bot = getattr(event, 'bot', None)
            if bot and hasattr(bot, 'call_action'):
                if event.is_private_chat():
                    resp = await bot.call_action('send_private_msg',
                        user_id=int(event.get_sender_id()),
                        message=cq_msg,
                    )
                else:
                    resp = await bot.call_action('send_group_msg',
                        group_id=int(event.get_group_id()),
                        message=cq_msg,
                    )
                logger.info(f"[pixiv] 📤 send_msg 响应: type={type(resp).__name__}, "
                            f"keys={list(resp.keys()) if isinstance(resp, dict) else 'N/A'}")
                # 兼容不同 OneBot 实现的 message_id 字段名
                msg_id = (resp.get('message_id') or resp.get('msg_id') or
                          resp.get('real_id') or resp.get('message_seq')) if isinstance(resp, dict) else None
                if msg_id:
                    logger.info(f"[pixiv] ✅ 获取 message_id={msg_id}，"
                                f"将在 {self.config_mgr.get('recall_after_seconds', 60)}s 后撤回")
                    return int(msg_id)
                else:
                    logger.warning(f"[pixiv] ⚠️ send_msg 响应中无 message_id: {str(resp)[:200]}")
        except Exception as e:
            logger.warning(f"[pixiv] ⚠️ 底层发送异常: {e}")

        # 回退: 返回 None（无法撤回，但图片仍会通过后续流程发送）
        return None

    # ==================================================================
    # 定时撤回
    # ==================================================================

    def _schedule_recall(self, event: AstrMessageEvent, message_id: int, seconds: int) -> None:
        """调度定时撤回。seconds=0 或缺 message_id 则跳过。"""
        if seconds <= 0 or not message_id:
            return
        asyncio.create_task(self._recall_after(event, message_id, seconds))

    async def _recall_after(self, event: AstrMessageEvent, message_id: int, seconds: int) -> None:
        """等待后通过 OneBot delete_msg API 撤回消息。"""
        await asyncio.sleep(seconds)
        try:
            bot = getattr(event, 'bot', None)
            if bot and hasattr(bot, 'delete_msg'):
                logger.info(f"[pixiv] 🔄 正在撤回 message_id={message_id}...")
                resp = await bot.delete_msg(message_id=message_id)
                logger.info(f"[pixiv] delete_msg 返回: {resp}")
            elif bot and hasattr(bot, 'call_action'):
                logger.info(f"[pixiv] 🔄 通过 call_action 撤回 message_id={message_id}")
                resp = await bot.call_action('delete_msg', message_id=message_id)
                logger.info(f"[pixiv] call_action delete_msg 返回: {resp}")
            else:
                logger.warning(f"[pixiv] ⚠️ 撤回失败：event.bot 不可用")
        except Exception as e:
            logger.warning(f"[pixiv] ⚠️ 撤回异常: {type(e).__name__}: {e}")

    # ==================================================================
    # 图片内容审核
    # ==================================================================

    async def _check_and_moderate(
        self, event: AstrMessageEvent, image_bytes: bytes,
        info, img_path: str, message_id: int | None = None,
    ) -> str | None:
        """用视觉模型审核图片，评分超过阈值则撤回并返回道歉消息。返回 None 表示无需处理。

        Args:
            message_id: 已发送图片的消息 ID，用于撤回。为 None 时无法撤回（如 yield 回退路径）。
        """
        try:
            score = await self._moderate_image(image_bytes)
            if score is None:
                return None

            threshold = self.config_mgr.get("nsfw_threshold", 8)
            logger.info(
                f"[pixiv] 内容审核: illust_id={info.illust_id}, "
                f"score={score}, threshold={threshold}"
            )

            if score > threshold:
                # 撤回图片（仅当有 message_id 时才能撤回）
                if message_id:
                    await self._recall_after(event, message_id, 1)
                else:
                    logger.warning(
                        f"[pixiv] 内容审核需撤回但无 message_id: "
                        f"illust_id={info.illust_id}"
                    )
                # 生成人格预设的道歉
                apology = await self._generate_moderation_apology(event, info)
                return apology
            return None
        except Exception as e:
            logger.debug(f"[pixiv] 内容审核异常（非关键）: {e}")
            return None

    async def _moderate_image(self, image_bytes: bytes) -> int | None:
        """
        压缩图片并发送给视觉模型，获取 0~10 的暴露程度评分。
        返回 None 表示审核失败（静默放过）。
        """
        try:
            # ---- 预检：所选模型是否支持图片识别 ----
            provider_id = self.config_mgr.get("content_moderation_provider", "")
            if provider_id and not self._check_provider_supports_image(provider_id):
                logger.info("[pixiv] 内容审核跳过：所选模型不支持图片识别")
                return None

            from PIL import Image
            import io

            # 压缩到约 100KB
            img = Image.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            quality = 85
            max_size = 100 * 1024
            buf = io.BytesIO()
            # 先缩放到合理尺寸
            if max(img.size) > 1024:
                ratio = 1024 / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            while True:
                buf.seek(0)
                buf.truncate()
                img.save(buf, format="JPEG", quality=quality)
                if buf.tell() <= max_size or quality <= 20:
                    break
                quality -= 10
            compressed = buf.getvalue()

            # 保存压缩后的图片为临时文件（比 base64 data URI 兼容性更好）
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(compressed)
            tmp.close()
            image_path = tmp.name

            # ---- 调用视觉模型审核 ----
            # 直接使用 AstrBot 的 llm_generate（不绕路），异常时静默放过
            provider_id = self.config_mgr.get("content_moderation_provider", "")
            model_name = (
                self._get_provider_model_name(provider_id)
                or provider_id or "默认视觉模型"
            )
            logger.info(
                f"[pixiv] 内容审核中... 模型: {model_name}, "
                f"图片压缩后: {len(compressed)} bytes"
            )

            prompt = (
                "你是内容安全审核员。请对这张图片进行色情/暴露程度评估，给出 0~10 的整数评分。\n\n"
                "## 评分标准\n"
                "- 0~2: 完全正常（普通插图、风景、人物着装正常）\n"
                "- 3~5: 轻度暴露（泳装、内衣、性感姿势但无性暗示）\n"
                "- 6~8: 明显暴露（裸体、性挑逗姿势、暗示性场景）\n"
                "- 9~10: 极端色情（直接性行为、露骨性器官特写）\n\n"
                "## 注意\n"
                "- 仅凭大胸部、紧身衣不构成色情，需看整体暴露程度和性暗示\n"
                "- R-18 标签不一定代表极端色情，请根据实际画面判断\n"
                "- 只回复一个数字，不要解释"
            )
            system_prompt = "你是专业的内容安全审核员。只回复数字，不带任何解释。"

            score = await self._call_moderation_llm(
                provider_id, model_name, image_path, prompt, system_prompt
            )
            return score
        except Exception as e:
            logger.warning(f"[pixiv] 视觉模型审核调用失败: {e}")
            return None
        finally:
            # 清理审核临时文件
            try:
                if 'image_path' in dir() and image_path:
                    os.remove(image_path)
            except OSError:
                pass

    async def _call_moderation_llm(
        self, provider_id: str, model_name: str,
        image_path: str, prompt: str, system_prompt: str,
    ) -> int | None:
        """
        调用 AstrBot llm_generate 审核图片，异常时返回 None（静默放过）。

        AstrBot v4.25.2 多模态响应处理有 usage=None 的 bug，
        但 llm_generate 内部会抛异常，此处捕获后返回 None，
        不影响图片正常发送（审核跳过）。
        """
        try:
            if not self.context:
                return None

            if provider_id:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    image_urls=[image_path],
                )
            else:
                providers = self.context.get_all_providers()
                default_id = providers[0].meta().id if providers else ""
                resp = await self.context.llm_generate(
                    chat_provider_id=default_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    image_urls=[image_path],
                )

            text = (getattr(resp, 'completion_text', None) or "").strip()
            logger.info(
                f"[pixiv] 📥 审核反馈 → \"{text}\" (长度: {len(text)} chars)"
            )

            import re
            match = re.search(r'\b(\d+)\b', text)
            if match:
                return max(0, min(10, int(match.group(1))))
            return None

        except Exception as e:
            logger.info(
                f"[pixiv] 审核调用异常（非关键，图片正常发送）: {type(e).__name__}: {e}"
            )
            return None

    def _check_provider_supports_image(self, provider_id: str) -> bool:
        """
        检查指定 provider 是否支持图片输入（通过 model_metadata.modalities.input）。
        无法判断时返回 True（宁可放过，不误判）。
        """
        try:
            manager = getattr(self.context, 'provider_manager', None)
            if not manager:
                return True  # 无法判断 → 假定支持
            inst = getattr(manager, 'inst_map', {}).get(provider_id)
            if not inst:
                return True
            meta = getattr(inst, 'meta', None)
            if meta:
                mm = getattr(meta(), 'model_metadata', None) if callable(meta) else getattr(meta, 'model_metadata', None)
            else:
                return True
            if not mm:
                return True
            if isinstance(mm, dict):
                inputs = mm.get('modalities', {}).get('input', [])
            elif hasattr(mm, 'modalities'):
                inputs = getattr(getattr(mm, 'modalities', None), 'input', None) or []
            else:
                return True
            return 'image' in (inputs or [])
        except Exception:
            return True  # 无法判断 → 假定支持

    def _get_provider_model_name(self, provider_id: str) -> str:
        """从 provider_id 获取模型名称，用于日志显示。"""
        try:
            manager = getattr(self.context, 'provider_manager', None)
            if manager:
                inst = getattr(manager, 'inst_map', {}).get(provider_id)
                if inst:
                    meta = getattr(inst, 'meta', None)
                    if meta:
                        m = meta() if callable(meta) else meta
                        return getattr(m, 'model', '') or provider_id
        except Exception:
            pass
        return provider_id

    async def _generate_moderation_apology(self, event: AstrMessageEvent, info) -> str:
        """生成内容审核触发的道歉回复（带人格预设）。"""
        try:
            await self._inject_persona(event)
            persona = self.intent_parser._persona_prompt or ""
            prompt = (
                f"用户请求的图片（{info.title}）因内容审核被撤回。"
                f"请以拟人化方式简短道歉（不超过40字），说明该图不合适。"
            )
            reply = await self.intent_parser._call_llm(
                prompt=prompt,
                system_prompt=persona or "你是友好的 Pixiv 搜索助手。",
            )
            if reply and len(reply.strip()) >= 3:
                return f"⛔ {reply.strip()}"
        except Exception:
            pass
        return "⛔ 抱歉，该图片可能包含不适宜内容，已自动撤回。"

    # ==================================================================
    # 工具方法
    # ==================================================================

    async def _save_config(self) -> None:
        """持久化配置。"""
        try:
            if hasattr(self._raw_config, 'save_config'):
                self._raw_config.save_config()
                logger.debug("[pixiv] 配置已保存 (save_config)")
            elif hasattr(self._raw_config, 'save'):
                self._raw_config.save()
                logger.debug("[pixiv] 配置已保存 (save)")
            else:
                logger.warning("[pixiv] 无法保存配置: _raw_config 无 save 方法")
        except Exception as e:
            logger.warning(f"[pixiv] 配置保存失败: {e}")

    async def _inject_persona(self, event: AstrMessageEvent) -> None:
        """
        从 AstrBot 获取当前会话的人格提示词，注入到 intent_parser，
        使 LLM 生成的拟人化回复带有人格风味。
        """
        try:
            persona_mgr = self.context.persona_manager
            persona = await persona_mgr.get_default_persona_v3(
                umo=event.unified_msg_origin
            )
            prompt = persona.get("prompt", "") if persona else ""
            if prompt:
                # 加上指示：按此人格风格回复
                self.intent_parser.set_persona(
                    f"请严格按以下人格设定来组织你的回复语气和风格：\n{prompt}"
                )
                logger.debug("[pixiv] 人格提示词已注入")
            else:
                self.intent_parser.set_persona(None)
        except Exception as e:
            logger.debug(f"[pixiv] 获取人格失败（非关键）: {e}")
            self.intent_parser.set_persona(None)

    @staticmethod
    def _clean_at_prefix(message: str) -> str:
        message = re.sub(r'\[CQ:at[^\]]*\]', '', message).strip()
        message = re.sub(r'^@\S+\s*', '', message).strip()
        return message

    @staticmethod
    def _save_temp_image(image_bytes: bytes, illust_id: int) -> str:
        """将图片 bytes 保存为临时文件，返回文件路径。"""
        import tempfile
        # 尝试从 bytes 头判断文件格式
        ext = ".jpg"
        if image_bytes[:4] == b'\x89PNG':
            ext = ".png"
        elif image_bytes[:4] == b'GIF8':
            ext = ".gif"
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, prefix=f"pixiv_{illust_id}_", delete=False
        )
        tmp.write(image_bytes)
        tmp.close()
        return tmp.name

    @staticmethod
    def _cleanup_temp_file(path: str) -> None:
        """删除临时图片文件，静默忽略错误（文件可能已被系统回收）。"""
        try:
            os.unlink(path)
        except OSError:
            pass

    # ==================================================================
    # 错误处理
    # ==================================================================

    @filter.on_plugin_error()
    async def on_error(self, event, plugin_name, handler_name, error, traceback_text):
        if plugin_name != "astrbot_plugin_search_pixiv_pic":
            return
        logger.error(f"[pixiv] 异常 | handler={handler_name} | error={error}\n{traceback_text}")
        if self.conv_state:
            await self.conv_state.clear(event.get_session_id())
