# ============================================================================
# intent_parser.py — LLM 意图分类器（LLM 优先架构）
# ============================================================================
# 本模块是插件的核心智能组件。所有非 /pixiv 指令的自然语言消息，
# 都会交由 LLM 分析意图，而非依赖关键词匹配。
#
# 设计原则:
#   1. LLM 优先 — 默认通过 LLM 判断用户意图，不做关键词绕过
#   2. 指令直通 — /pixiv id|tag|r18 指令不经 LLM，直接解析
#   3. 上下文感知 — 在反问流程中，LLM 能结合上一轮对话理解用户
#   4. 安全回退 — LLM 不可用时，回退到关键词匹配（降级方案）
#
# 意图分类流程:
#   /pixiv 指令 → 直接路由（不调 LLM）
#   其他消息   → LLM 分类 → 6 种意图之一
#
# 支持的意图类型:
#   FIND_BY_ID         — 按作品 ID 搜索
#   FIND_BY_TAG        — 按标签搜索
#   TOGGLE_R18         — 切换 R18 过滤
#   HELP               — 查看帮助
#   UNKNOWN            — 无法判断（触发反问）
#   NOT_IMAGE_REQUEST  — 非图片请求（透传给 AstrBot）
# ============================================================================

import json
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from astrbot.api import logger


class IntentType(Enum):
    """用户意图类型枚举。"""
    FIND_BY_ID = auto()
    FIND_BY_TAG = auto()
    TOGGLE_R18 = auto()
    HELP = auto()
    UNKNOWN = auto()
    NOT_IMAGE_REQUEST = auto()


@dataclass
class IntentResult:
    """意图分类结果。"""
    intent_type: IntentType = IntentType.NOT_IMAGE_REQUEST
    params: dict = field(default_factory=dict)
    confidence: float = 0.0
    raw_message: str = ""


# ============================================================================
# LLM Prompt 模板
# ============================================================================

# 意图分类 prompt —— 让 LLM 作为唯一的意图分析器
# 设计要点:
#   - 明确区分"图片请求"和"普通聊天"
#   - 对模糊请求输出 UNKNOWN（而不是强行猜测）
#   - 提取参数时保留用户原文（特别是标签搜索）
INTENT_CLASSIFY_PROMPT = """你是 Pixiv 插画搜索插件的意图分析器。
你的任务是判断用户消息是否与 Pixiv 插画/艺术作品搜索有关。

## ⚠️ 核心原则
**Pixiv 是一个插画/漫画/艺术作品平台，不是通用图片搜索引擎。**
只有明确涉及艺术插画、动漫角色、游戏同人、绘画风格等创作类图片的请求才是搜索意图。
表情包、梗图、截图、照片、logo、UI设计等不属于 Pixiv 搜索范围。

## 意图定义

### FIND_BY_ID — 按作品ID查图
用户明确要查找某个 Pixiv 作品 ID。
关键词: "id", "作品", "编号", Pixiv URL 中的数字ID
示例: "找id 12345678"、"帮我查作品119293921"、"https://www.pixiv.net/artworks/12345678"

### FIND_BY_TAG — 按标签搜索插画
用户想搜索 Pixiv 上的插画/艺术作品，给出了主题、角色、风格等创作类关键词。
搜索对象必须是插画/绘画/艺术创作，包含以下关键词之一："图"、"画"、"插画"、"插图"、"pixiv"、"作品"、"同人"、"壁纸"、"头像"。
或者给出了明确的作品名/角色名（如"原神"、"nikke"、"初音未来"）。
**以下不属于 FIND_BY_TAG：**
- 表情包/梗图/meme/sticker/emoji
- 照片/自拍/截图/聊天记录
- 跟"图"无关的日常聊天
示例: "找nikke的图"、"有没有原神的插画"、"来张猫耳少女"、"找张风景画"、"壁纸"

### TOGGLE_R18 — 切换R18过滤
示例: "关掉R18过滤"、"开启成人内容"、"r18 off"

### HELP — 查看帮助
示例: "怎么用"、"有什么功能"、"帮助"

### UNKNOWN — 意图模糊
提到了"图"但没给具体内容（如"来张图"、"发个图片"）。不带"图"字的消息不算UNKNOWN。

### NOT_IMAGE_REQUEST — 与插画搜索无关
**这是默认意图。** 以下一律判为 NOT_IMAGE_REQUEST：
- 日常聊天（"你好"、"今天吃什么"）
- 表情包/梗图请求（"发个表情包"、"来张表情包"、"有没有熊猫头"）
- 照片/视频请求（"拍照"、"录屏"、"截图"）
- 其他非插画请求

## 重要规则
1. **不确定时优先判 NOT_IMAGE_REQUEST**，宁可漏过不可误拦。
2. 参数 "tag" 保留用户原始搜索词。
3. 参数 "count": "来张"/"一张"→1，"两张"→2，"多张"/"来点"→0。

## 输出格式
{"intent": "<意图类型>", "params": {}, "confidence": <0.0-1.0>}

## 示例
用户: "有没有猫耳少女的图"
{"intent": "FIND_BY_TAG", "params": {"tag": "猫耳少女", "count": 1}, "confidence": 0.9}

用户: "来两张nikke的"
{"intent": "FIND_BY_TAG", "params": {"tag": "nikke", "count": 2}, "confidence": 0.9}

用户: "发个表情包"
{"intent": "NOT_IMAGE_REQUEST", "params": {}, "confidence": 0.95}

用户: "来张表情包"
{"intent": "NOT_IMAGE_REQUEST", "params": {}, "confidence": 0.95}

用户: "来张图"
{"intent": "UNKNOWN", "params": {}, "confidence": 0.3}

用户: "今天吃什么"
{"intent": "NOT_IMAGE_REQUEST", "params": {}, "confidence": 0.95}

用户: "帮我写代码"
{"intent": "NOT_IMAGE_REQUEST", "params": {}, "confidence": 0.95}

用户: "有没有风景画"
{"intent": "FIND_BY_TAG", "params": {"tag": "風景"}, "confidence": 0.9}

现在分析以下用户消息（只返回 JSON，不要其他文字）：
"""
# 标签富化 prompt —— 多维标签结构化输出
# 将中文描述拆解为 game / character / attributes 三个维度，
# attributes 使用同义词组格式：每组含多个日文标签 + 一个中文描述。
TAG_ENRICH_PROMPT = """你是 Pixiv 标签专家。将用户的中文搜索需求拆解为多维度标签，用于 Pixiv 搜索。

## 输出格式
{"game": ["游戏/作品名(日文)"], "character": ["角色名(日文)"], "attributes": [{"tags": ["日文标签","日文标签"], "label": "中文描述"}]}

## 维度说明
- game: 游戏或动漫作品名。没有则为空数组。
- character: 角色名。没有则为空数组。
- attributes: 每个元素是一个同义词组。
    - tags: 该特征的 Pixiv 日文标签（1~4个），同一含义的不同写法
    - label: 该特征的中文描述（简短，2~4字）
- 不同含义的特征必须分为不同的组（如大胸和黑丝是两个组）
- 不要输出 R-18/NSFW 分级标签（插件已有独立的 R18 过滤机制）

## 规则
1. 所有标签优先使用日文(Pixiv常用标签)，其次英文，最后中文
2. 只返回JSON，不要其他文字

## 示例
用户搜索: "碧蓝航线大胸色图"
{"game": ["アズールレーン"], "character": [], "attributes": [{"tags": ["巨乳", "おっぱい", "爆乳"], "label": "大胸"}]}

用户搜索: "原神角色天使尼可"
{"game": ["原神", "Genshin Impact"], "character": ["ニコ"], "attributes": []}

用户搜索: "猫耳少女"
{"game": [], "character": [], "attributes": [{"tags": ["猫耳", "獣耳"], "label": "猫耳"}, {"tags": ["少女"], "label": "少女"}]}

用户搜索: "碧蓝黑丝大胸色图"
{"game": ["アズールレーン"], "character": [], "attributes": [{"tags": ["巨乳", "おっぱい", "爆乳"], "label": "大胸"}, {"tags": ["黒タイツ", "パンスト", "黒ストッキング"], "label": "黑丝"}]}

用户搜索: "长发贫胸水手服"
{"game": [], "character": [], "attributes": [{"tags": ["ロングヘア", "長髪"], "label": "长发"}, {"tags": ["貧乳", "つるぺた"], "label": "贫乳"}, {"tags": ["セーラー服", "水手服"], "label": "水手服"}]}

用户搜索: "初音未来"
{"game": [], "character": ["初音ミク", "Hatsune Miku"], "attributes": []}

现在转换以下搜索词（只返回JSON）：
"""

# 反问生成 prompt
HUMANIZED_SEARCH_PROMPT = """你是 Pixiv 插画搜索助手。用户正在等待搜索结果，你需要用自然、拟人的语气告诉他们正在搜索什么。

## 规则
- 用日常对话的语气，不要说"正在搜索标签xxx"
- 像人类一样表达"我帮你找找xxx的图"
- 简短，不超过30字
- 用中文

用户请求: "{user_query}"
搜索关键词: "{search_tags}"

请生成一句拟人化的搜索提示："""
CLARIFY_PROMPT = """你是 Pixiv 图片搜索助手。用户表达了一个模糊的图片请求，你需要友好地反问以明确需求。

## 规则
- 引导用户说出搜索关键词/标签
- 简短、自然，不超过40字
- 给1-2个具体例子提示用户
- 用中文

用户说: "{user_message}"
请生成反问："""


# ============================================================================
# IntentParser
# ============================================================================

class IntentParser:
    """
    LLM 优先的意图分类器。

    默认通过 LLM 分析每条消息的意图。
    /pixiv 指令直接路由，不经过 LLM。
    仅在 LLM 不可用时回退到关键词匹配。
    """

    # 回退用关键词（仅 LLM 不可用时使用）
    _FALLBACK_IMAGE_KW = [
        "图", "图片", "插画", "pixiv", "イラスト", "illust",
        "来张", "找", "搜", "看看",
    ]

    # 正则
    _PIXIV_ID_RE = re.compile(r'\b(\d{6,10})\b')
    _PIXIV_CMD_RE = re.compile(r'/pixiv\s+(id|tag|r18|help|config)', re.IGNORECASE)

    def __init__(self, config_mgr, context=None) -> None:
        """
        Args:
            config_mgr: ConfigManager 实例。
            context:    AstrBot Context（用于 llm_generate 调用独立 LLM）。
        """
        self._config = config_mgr
        self._context = context
        self._persona_prompt: str | None = None

    # ==================================================================
    # 主入口
    # ==================================================================

    async def parse(
        self, message: str, session_id: str, event=None
    ) -> IntentResult:
        """
        解析用户消息意图 —— LLM 优先。

        流程:
          1. /pixiv 指令 → 直接路由
          2. LLM 分类（主要方式）
          3. LLM 不可用 → 关键词回退
        """
        message = message.strip()
        result = IntentResult(raw_message=message)

        if not message:
            result.intent_type = IntentType.NOT_IMAGE_REQUEST
            return result

        # ---- Step 1: /pixiv 显式指令 → 不经 LLM ----
        if self._PIXIV_CMD_RE.search(message):
            return self._parse_command(message, result)

        # ---- Step 2: LLM 分类（主要通路）----
        if event is not None:
            try:
                llm_result = await self._llm_classify(message, event)
                if llm_result is not None:
                    logger.info(
                        f"[pixiv:intent] LLM → {llm_result.intent_type.name} "
                        f"(conf={llm_result.confidence:.2f})"
                    )
                    return llm_result
            except Exception as e:
                logger.warning(f"[pixiv:intent] LLM 分类异常: {e}")

        # ---- Step 3: 回退（LLM 不可用时）----
        logger.warning("[pixiv:intent] LLM 不可用，使用关键词回退")
        return self._fallback_match(message, result)

    # ==================================================================
    # /pixiv 指令解析（不调 LLM）
    # ==================================================================

    def _parse_command(self, message: str, result: IntentResult) -> IntentResult:
        parts = message.split(maxsplit=3)

        if len(parts) < 2:
            result.intent_type = IntentType.HELP
            result.confidence = 1.0
            return result

        cmd = parts[1].lower()

        if cmd == "id" and len(parts) >= 3:
            illust_id = self._extract_illust_id(parts[2])
            if illust_id:
                result.intent_type = IntentType.FIND_BY_ID
                result.params = {"illust_id": illust_id}
                result.confidence = 1.0
            else:
                result.intent_type = IntentType.UNKNOWN
                result.confidence = 0.5

        elif cmd == "tag" and len(parts) >= 3:
            result.intent_type = IntentType.FIND_BY_TAG
            result.params = {"tag": parts[2]}
            result.confidence = 1.0

        elif cmd == "r18" and len(parts) >= 3:
            val = parts[2].lower()
            if val in ("on", "开", "开启", "启用", "true", "1"):
                result.intent_type = IntentType.TOGGLE_R18
                result.params = {"enable": False}
                result.confidence = 1.0
            elif val in ("off", "关", "关闭", "禁用", "false", "0"):
                result.intent_type = IntentType.TOGGLE_R18
                result.params = {"enable": True}
                result.confidence = 1.0

        elif cmd in ("help", "帮助"):
            result.intent_type = IntentType.HELP
            result.confidence = 1.0

        elif cmd == "config":
            result.intent_type = IntentType.UNKNOWN
            result.confidence = 1.0  # 由 main.py 的 cmd_pixiv_config 处理

        else:
            result.intent_type = IntentType.HELP
            result.confidence = 0.8

        return result

    # ==================================================================
    # LLM 分类（核心通路）
    # ==================================================================

    async def _llm_classify(self, message: str, event) -> Optional[IntentResult]:
        """调用 LLM 分析意图。优先用插件专用 LLM，否则用默认。"""
        prompt = INTENT_CLASSIFY_PROMPT + f"\n用户消息: {message}"
        response_text = await self._call_llm(
            prompt=prompt,
            system_prompt="你是精确的意图分类器。只返回JSON，不要任何其他内容。",
        )
        if not response_text:
            return None
        try:
            json_str = self._extract_json(response_text)
            data = json.loads(json_str)
            return self._dict_to_result(data, message)
        except json.JSONDecodeError as e:
            logger.warning(f"[pixiv:intent] LLM JSON 解析失败: {e}")
            return None

    # ==================================================================
    # 角色智能解析（含联网搜索）
    # ==================================================================

    # 角色解析 prompt —— 识别游戏/作品 + 角色名，判断是否需要联网搜索
    CHARACTER_RESOLVE_PROMPT = """你是二次元角色识别专家。分析用户的搜索意图，提取游戏/作品名和角色名。

## 输出格式
{"game": "游戏/作品名（没有则为空字符串）", "character": "角色名（没有则为空字符串）", "need_web_search": false, "note": "简短说明"}

## 规则
1. 如果能确定角色属于哪个游戏/作品，填写 game 字段。
2. 如果角色名是中文昵称/简称（如"尼可"、"胡桃"），填写你知道的日文原名到 character。
3. **关键**：先判断搜索词中是否包含具体角色名。如果只有作品名/属性/风格（如"碧蓝航线 大胸 黑丝"、"原神 风景"），character 留空，need_web_search 必须为 false。不要强行猜测或编造角色名。
4. need_web_search: 仅当你**识别到了角色名**但**不确定**是谁/属于哪个作品/日文名时，才设为 true。没有角色名时一律 false。
5. 对于热门角色（原神、崩铁、FGO、NIKKE 等知名游戏角色），你通常能直接识别，need_web_search 应为 false。
6. 对于非常冷门、新出、或你完全不知道的角色，need_web_search 应为 true。

## 示例
用户搜索: "原神角色天使尼可"
{"game": "原神", "character": "ニコ", "need_web_search": false, "note": "原神角色，日文名ニコ（天使のニコ）"}

用户搜索: "nikke灰姑娘"
{"game": "NIKKE", "character": "アナキオール", "need_web_search": false, "note": "NIKKE角色灰姑娘，日文名アナキオール"}

用户搜索: "碧蓝航线 大胸 黑丝"
{"game": "アズールレーン", "character": "", "need_web_search": false, "note": "只有作品名和属性，没有具体角色"}

用户搜索: "猫耳少女"
{"game": "", "character": "", "need_web_search": false, "note": "通用标签，非特定角色"}

用户搜索: "原神 神里綾華"
{"game": "原神", "character": "神里綾華", "need_web_search": false, "note": "知名角色，可直接识别"}

用户搜索: "xxx2025新番女主"
{"game": "", "character": "xxx", "need_web_search": true, "note": "不确定这个角色，需要联网确认"}

现在分析以下搜索词（只返回 JSON）：
"""

    async def resolve_search_intent(self, user_tag: str, event=None, umo: str = "") -> dict:
        """
        解析用户搜索意图：识别游戏/作品 + 角色名，必要时联网搜索。

        流程：
          1. LLM 初步识别 → 提取 game/character + 是否需要联网
          2. 若 need_web_search=true → 调用 AstrBot 内置联网搜索确认角色
          3. 合并结果，返回 {"game": ..., "character": ..., "resolved": bool}

        Args:
            user_tag: 用户原始搜索词。
            event:    AstrMessageEvent（用于工具执行上下文）。
            umo:      unified_msg_origin。

        Returns:
            {"game": str, "character": str, "resolved": bool, "note": str}
        """
        result = {"game": "", "character": "", "resolved": False, "note": ""}
        need_web = False  # 提前声明，Step 2 门控用

        # ---- Step 1: LLM 初步识别 ----
        prompt = self.CHARACTER_RESOLVE_PROMPT + f"\n用户搜索: {user_tag}"
        response_text = await self._call_llm(
            prompt=prompt,
            system_prompt="你是二次元角色识别专家。只返回 JSON，不要其他内容。",
        )
        if response_text:
            try:
                json_str = self._extract_json(response_text)
                data = json.loads(json_str)
                result["game"] = data.get("game", "")
                result["character"] = data.get("character", "")
                result["note"] = data.get("note", "")
                need_web = data.get("need_web_search", False)

                # 情况 A: 有角色名且 LLM 确定 → 直接返回，无需联网
                if not need_web and result["character"]:
                    result["resolved"] = True
                    logger.info(
                        f"[pixiv:intent] 🎯 角色识别: game='{result['game']}', "
                        f"character='{result['character']}' → {result['note']}"
                    )
                    return result

                # 情况 B: 无角色名（如"碧蓝航线 大胸 黑丝"）→ 无需联网
                if not need_web and not result["character"]:
                    logger.info(
                        f"[pixiv:intent] 🏷️ 无角色名，跳过联网搜索: "
                        f"'{user_tag}' → game='{result.get('game', '')}', "
                        f"note='{result.get('note', '')}'"
                    )
                    return result

                # 情况 C: 有角色名但 LLM 不确定 → 进入 Step 2 联网搜索
                if need_web:
                    logger.info(
                        f"[pixiv:intent] 🔍 LLM 不确定角色，尝试联网搜索: "
                        f"'{user_tag}' → {result.get('note', '')}"
                    )
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"[pixiv:intent] 角色解析 JSON 失败: {e}")

        # ---- Step 2: 联网搜索确认角色（仅在 need_web=True 时执行）----
        if need_web:
            try:
                resolved = await self._web_search_character(user_tag, event, umo)
                if resolved:
                    result["game"] = resolved.get("game", result["game"])
                    result["character"] = resolved.get("character", result["character"])
                    result["resolved"] = True
                    result["note"] = resolved.get("note", "联网搜索确认")
                    logger.info(
                        f"[pixiv:intent] 🌐 联网搜索完成: game='{result['game']}', "
                        f"character='{result['character']}'"
                    )
            except Exception as e:
                logger.warning(f"[pixiv:intent] 联网搜索失败: {e}")

        return result

    async def _web_search_character(self, user_tag: str, event, umo: str = "") -> dict | None:
        """
        使用 AstrBot 内置联网搜索工具查找角色信息。

        实现手动工具循环：
          1. LLM(带工具) 决定搜索策略 → 返回 tool_call 或直接文本
          2. 若 tool_call → 插件执行搜索工具 → 格式化结果 → 回传 LLM 分析
          3. 若直接文本 → 提取 JSON

        这避免了 llm_generate 不执行工具的问题（completion_text=None），
        由插件自行完成 「LLM → 搜索 → 格式化 → LLM 分析」的闭环。

        Args:
            user_tag: 用户原始搜索词。
            event:    AstrMessageEvent（创建工具执行上下文所需）。
            umo:      unified_msg_origin。

        Returns:
            {"game": str, "character": str, "note": str} 或 None。
        """
        if not self._context:
            return None

        # ---- 获取 AstrBot 内置 web_search 工具 ----
        tool_manager = self._context.get_llm_tool_manager()

        cfg = self._context.get_config(umo=umo) if umo else self._context.get_config()
        prov_settings = cfg.get("provider_settings", {})
        provider = prov_settings.get("websearch_provider", "tavily")

        from astrbot.core.tools.web_search_tools import (
            BaiduWebSearchTool,
            BochaWebSearchTool,
            BraveWebSearchTool,
            FirecrawlWebSearchTool,
            TavilyWebSearchTool,
        )
        tool_class_map = {
            "tavily": TavilyWebSearchTool,
            "bocha": BochaWebSearchTool,
            "brave": BraveWebSearchTool,
            "baidu_ai_search": BaiduWebSearchTool,
            "firecrawl": FirecrawlWebSearchTool,
        }
        tool_cls = tool_class_map.get(provider)
        web_tool = tool_manager.get_builtin_tool(tool_cls) if tool_cls else None

        if not web_tool:
            logger.info(
                f"[pixiv:intent] 🌐 无可用联网搜索工具 "
                f"(provider={provider or '未配置'})，跳过"
            )
            return None

        logger.info(
            f"[pixiv:intent] 🌐 使用联网工具: {web_tool.name} "
            f"(provider={provider})"
        )

        # ---- 获取 LLM provider ----
        providers = self._context.get_all_providers()
        provider_id = self._config.get("llm_provider_id", "")
        if not provider_id and providers:
            provider_id = providers[0].meta().id

        system_prompt = (
            "你是二次元角色搜索专家。使用搜索工具查找角色信息，只返回 JSON。"
        )

        search_prompt = (
            f"请搜索「{user_tag}」是哪个游戏/动漫作品的哪个角色。\n"
            f"找到后，请用工具搜索确认角色的日文原名。\n"
            f"最后用 JSON 回复: "
            f'{{"game": "作品名", "character": "日文角色名", "note": "来源说明"}}'
        )

        try:
            from astrbot.api import ToolSet
            tool_set = ToolSet()
            tool_set.add_tool(web_tool)

            # ============================================================
            # Phase 1: LLM 决定搜索策略（带工具）
            # ============================================================
            resp = await self._context.llm_generate(
                chat_provider_id=provider_id,
                prompt=search_prompt,
                system_prompt=system_prompt,
                tools=tool_set,
            )

            # ============================================================
            # Phase 2: 检测 LLM 是否想要调用工具
            # ============================================================
            if resp.tools_call_name:
                logger.info(
                    f"[pixiv:intent] 🔧 LLM 请求调用工具: "
                    f"{resp.tools_call_name} "
                    f"args={resp.tools_call_args[0] if resp.tools_call_args else '{}'}"
                )

                # ---- 2a: 插件执行搜索工具 ----
                tool_args = resp.tools_call_args[0] if resp.tools_call_args else {}
                raw_results = await self._execute_web_search_tool(
                    web_tool, tool_args, event
                )

                # ---- 2b: 格式化原始结果（转换为 LLM 易读的纯文本）----
                formatted = self._format_web_search_results(raw_results)
                logger.info(
                    f"[pixiv:intent] 📋 搜索结果已格式化 "
                    f"(长度={len(formatted)} 字符)"
                )

                # ---- 2c: 构建工具调用上下文消息 ----
                contexts = self._build_tool_result_contexts(resp, formatted)

                # ============================================================
                # Phase 3: 将搜索结果回传 LLM 分析
                # ============================================================
                analyze_prompt = (
                    f"请根据以上搜索结果，分析「{user_tag}」是哪个作品的哪个角色，"
                    f"并确认日文原名。最后用 JSON 回复: "
                    f'{{"game": "作品名", "character": "日文角色名", "note": "来源说明"}}'
                )
                resp2 = await self._context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=analyze_prompt,
                    contexts=contexts,
                    system_prompt=system_prompt,
                    tools=None,
                )
                text = (resp2.completion_text or "").strip()
            else:
                # ---- LLM 直接返回文本（未调用工具）----
                text = (resp.completion_text or "").strip()

            # ============================================================
            # Phase 4: 提取 JSON 结果
            # ============================================================
            if text:
                json_str = self._extract_json(text)
                result = json.loads(json_str)
                logger.info(
                    f"[pixiv:intent] 🌐 联网搜索角色解析结果: "
                    f"game='{result.get('game', '')}', "
                    f"character='{result.get('character', '')}'"
                )
                return result

        except Exception as e:
            logger.warning(
                f"[pixiv:intent] 联网搜索调用失败: {e}",
                exc_info=True,
            )

        return None

    # ==================================================================
    # 手动工具循环 —— 辅助方法
    # ==================================================================

    async def _execute_web_search_tool(
        self, web_tool, tool_args: dict, event
    ) -> str:
        """
        直接执行 AstrBot 内置联网搜索工具。

        创建工具所需的 ContextWrapper[AstrAgentContext] 后调用 tool.call()。

        Args:
            web_tool:  FunctionTool 实例（如 TavilyWebSearchTool）。
            tool_args: LLM 返回的工具参数 dict（含 query 等）。
            event:     AstrMessageEvent。

        Returns:
            搜索结果字符串（JSON 格式或错误信息）。
        """
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.astr_agent_context import AstrAgentContext

        agent_ctx = AstrAgentContext(context=self._context, event=event)
        run_context = ContextWrapper(context=agent_ctx)

        # 提取核心参数，过滤掉 LLM 可能传的无关参数
        query = tool_args.get("query", "")
        valid_params = {"query": query}
        for key in ("max_results", "search_depth", "topic", "days"):
            if key in tool_args:
                valid_params[key] = tool_args[key]

        logger.info(
            f"[pixiv:intent] 🔍 执行搜索: query='{query}' "
            f"params={valid_params}"
        )

        result = await web_tool.call(context=run_context, **valid_params)

        # ToolExecResult = str | mcp.types.CallToolResult
        # 对于 web_search 工具，返回值是 JSON 字符串
        if isinstance(result, str):
            return result

        # 处理 CallToolResult 格式
        try:
            if hasattr(result, 'content'):
                parts = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        parts.append(item.text)
                return "\n".join(parts)
        except Exception:
            pass

        return str(result)

    def _format_web_search_results(self, raw_result: str) -> str:
        """
        将原始搜索结果格式化为 LLM 易读的纯文本。

        原始格式（JSON）:
          {"results": [{"title":..., "url":..., "snippet":..., "index":...}, ...]}

        输出格式:
          [1] 标题
          URL: ...
          摘要: ...

          [2] ...
        """
        try:
            data = json.loads(raw_result)
            results = data.get("results", [])
            if not results:
                return raw_result

            lines = []
            for item in results:
                idx = item.get("index", "?")
                title = item.get("title", "无标题")
                url = item.get("url", "")
                snippet = item.get("snippet", "")

                lines.append(f"[{idx}] {title}")
                if url:
                    lines.append(f"URL: {url}")
                if snippet:
                    lines.append(f"摘要: {snippet}")
                lines.append("")  # 空行分隔

            return "\n".join(lines)
        except (json.JSONDecodeError, TypeError, AttributeError):
            # 不是 JSON 或格式不符预期，原样返回
            return raw_result

    def _build_tool_result_contexts(
        self, llm_response, formatted_results: str
    ) -> list:
        """
        构建包含工具调用和结果的消息上下文列表。

        模拟 OpenAI 工具调用协议:
          - AssistantMessage(tool_calls=[...], content=None)
          - ToolMessage(tool_call_id=..., content=...)

        这些消息作为 contexts 参数传给第二次 llm_generate 调用，
        让 LLM 看到「自己调用了工具 + 工具返回了什么」。

        Args:
            llm_response:      第一次 LLM 响应（含 tools_call_name/args/ids）。
            formatted_results:  格式化后的搜索结论文本。

        Returns:
            list[Message] 上下文消息列表。
        """
        from astrbot.core.agent.message import Message, ToolCall

        tool_call_id = (
            (llm_response.tools_call_ids or ["call_1"])[0]
        )
        tool_name = (
            (llm_response.tools_call_name or ["web_search"])[0]
        )
        tool_args = (
            llm_response.tools_call_args[0]
            if llm_response.tools_call_args
            else {}
        )

        contexts = [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id=tool_call_id,
                        function=ToolCall.FunctionBody(
                            name=tool_name,
                            arguments=json.dumps(
                                tool_args, ensure_ascii=False
                            ),
                        ),
                    )
                ],
                content=None,
            ),
            Message(
                role="tool",
                tool_call_id=tool_call_id,
                content=formatted_results,
            ),
        ]
        return contexts

    # ==================================================================
    # 标签富化
    # ==================================================================

    async def enrich_tags(self, user_tag: str) -> dict:
        """
        将用户的中文搜索词转换为 Pixiv 多维标签。

        用 LLM 生成 game / character / attributes 三个维度的标签（日文优先），
        并合并为扁平列表供搜索使用。

        Args:
            user_tag: 用户原始搜索词（如 "碧蓝黑丝大胸色图"）。

        Returns:
            {
                "flat":       ["アズールレーン", "巨乳", "黒タイツ", ...],
                "game":       ["アズールレーン"],
                "character":  [],
                "attributes": ["巨乳", "黒タイツ", "おっぱい"]
            }
            LLM 不可用或关闭富化时返回 {"flat": [user_tag], "game":[], "character":[], "attributes":[]}。
        """
        empty = {"flat": [user_tag], "game": [], "character": [], "attributes": []}
        if not self._config.get("tag_enrichment_enabled", True):
            return empty

        prompt = TAG_ENRICH_PROMPT + f"\n用户搜索: {user_tag}"
        response_text = await self._call_llm(
            prompt=prompt,
            system_prompt="你是 Pixiv 标签专家。只返回 JSON 对象，不要其他内容。",
        )
        if not response_text:
            return empty

        try:
            json_str = self._extract_json(response_text)
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return empty

            game = data.get("game", []) or []
            character = data.get("character", []) or []
            raw_attrs = data.get("attributes", []) or []

            # 兼容旧格式（扁平列表）和新格式（同义词组）
            # 旧格式: ["巨乳", "黒タイツ"] → 每组一个标签
            # 新格式: [{"tags":["巨乳","おっぱい"],"label":"大胸"}, ...]
            attr_groups = []
            if raw_attrs and isinstance(raw_attrs[0], dict):
                attr_groups = [
                    {"tags": g.get("tags", [g.get("label", "")]), "label": g.get("label", "")}
                    for g in raw_attrs if g.get("tags")
                ]
            elif raw_attrs and isinstance(raw_attrs[0], str):
                # 旧格式回退：每个标签独立成组
                attr_groups = [{"tags": [t], "label": t} for t in raw_attrs]

            # 合并为扁平列表（game → character → 所有 tags 顺序）
            flat = []
            seen = set()
            for tag in game + character:
                if tag and tag not in seen:
                    flat.append(tag)
                    seen.add(tag)
            for g in attr_groups:
                for tag in g["tags"]:
                    if tag and tag not in seen:
                        flat.append(tag)
                        seen.add(tag)

            result = {
                "flat": flat if flat else [user_tag],
                "game": game,
                "character": character,
                "attributes": attr_groups,  # 新格式: 同义词组列表
            }
            logger.info(
                f"[pixiv:intent] 标签富化: '{user_tag}' → flat={flat}, "
                f"game={game}, attr_groups={[g['label'] for g in attr_groups]}"
            )
            return result
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"[pixiv:intent] 标签富化 JSON 解析失败: {e}")

        return empty

    # ==================================================================
    # 统一 LLM 调用
    # ==================================================================

    async def _call_llm(self, prompt: str, system_prompt: str = "") -> str | None:
        """
        调用 LLM，优先使用插件专用 LLM（llm_provider_id），否则用 AstrBot 默认。

        Returns:
            LLM 响应文本，或 None。
        """
        provider_id = self._config.get("llm_provider_id", "")

        # ---- 优先使用插件专用 LLM ----
        if provider_id and self._context:
            try:
                resp = await self._context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                return resp.completion_text
            except Exception as e:
                logger.warning(
                    f"[pixiv:intent] 专用 LLM({provider_id}) 调用失败: {e}，"
                    f"回退到默认 LLM"
                )

        # ---- 回退: 使用 AstrBot 默认 LLM ----
        if self._context:
            try:
                # 获取默认 provider ID（AstrBot v4.5+ 要求必传 chat_provider_id）
                providers = self._context.get_all_providers()
                default_id = providers[0].meta().id if providers else ""
                resp = await self._context.llm_generate(
                    chat_provider_id=default_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                return resp.completion_text
            except Exception as e:
                logger.warning(
                    f"[pixiv:intent] 默认 LLM 调用失败: {e}"
                )

        logger.debug("[pixiv:intent] LLM 不可用（无 context）")
        return None

    # ==================================================================
    # 回退分类（LLM 不可用时）
    # ==================================================================

    def _fallback_match(self, message: str, result: IntentResult) -> IntentResult:
        """关键词回退 —— 仅 LLM 不可用时作为降级方案。"""
        msg_lower = message.lower()

        # 检测 ID
        id_match = self._PIXIV_ID_RE.search(message)
        if id_match and any(kw in msg_lower for kw in ["id", "编号", "作品"]):
            result.intent_type = IntentType.FIND_BY_ID
            result.params = {"illust_id": int(id_match.group(1))}
            result.confidence = 0.7
            return result

        # 检测 R18
        if any(kw in msg_lower for kw in ["r18", "成人", "过滤", "色图", "涩图"]):
            result.intent_type = IntentType.TOGGLE_R18
            result.confidence = 0.7
            return result

        # 图片相关
        if any(kw in msg_lower for kw in self._FALLBACK_IMAGE_KW):
            tag = self._extract_tag(message)
            if tag:
                result.intent_type = IntentType.FIND_BY_TAG
                result.params = {"tag": tag}
                result.confidence = 0.5
            else:
                result.intent_type = IntentType.UNKNOWN
                result.confidence = 0.2
        else:
            result.intent_type = IntentType.NOT_IMAGE_REQUEST

        return result

    # ==================================================================
    # 反问生成
    # ==================================================================

    async def generate_clarification(self, message: str, event=None) -> str:
        """为模糊图片请求生成反问。"""
        prompt = CLARIFY_PROMPT + f"\n用户说: {message}\n请生成反问："
        response = await self._call_llm(
            prompt=prompt,
            system_prompt=self._persona_prompt or "你是友好的 Pixiv 搜索助手，用中文简短反问。",
        )
        if response and len(response.strip()) >= 3:
            return response.strip()

        import random
        defaults = [
            "请问想搜什么主题呢？比如「猫耳」「风景」「初音未来」~",
            "想找什么样的图？告诉我关键词吧！",
        ]
        return random.choice(defaults)

    # ==================================================================
    # 拟人化搜索回复
    # ==================================================================

    async def generate_search_reply(self, user_query: str, search_tags: list[str]) -> str:
        """生成拟人化搜索提示。"""
        prompt = HUMANIZED_SEARCH_PROMPT.format(
            user_query=user_query, search_tags=", ".join(search_tags),
        )
        response = await self._call_llm(
            prompt=prompt,
            system_prompt=self._persona_prompt or "你是友好的 Pixiv 搜索助手，用中文简短回复。",
        )
        if response and len(response.strip()) >= 3:
            return response.strip()
        return f"🔍 帮你找找 {search_tags[0] if search_tags else user_query} 的图~"

    # ==================================================================
    # 注入人格提示词（由 main.py 调用）
    # ==================================================================

    def set_persona(self, persona_prompt: str | None) -> None:
        """设置当前会话的人格提示词。"""
        self._persona_prompt = persona_prompt

    def _extract_illust_id(self, text: str) -> Optional[int]:
        text = text.replace("https://www.pixiv.net/artworks/", "")
        text = text.replace("pixiv.net/artworks/", "")
        match = self._PIXIV_ID_RE.search(text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_tag(message: str) -> Optional[str]:
        """从自然语言中提取搜索标签。"""
        # 噪声词按长度降序排列，避免短词先匹配破坏长词
        noise = [
            "pixiv的", "Pixiv的", "的图片", "来一张",
            "有没有", "pixiv", "Pixiv", "插画",
            "我想看", "一张", "一个", "一些",
            "来张", "给张", "的图", "图片",
            "搜索", "帮我", "看看", "想要",
            "有吗", "找", "搜", "图", "求",
        ]
        tag = message
        for w in noise:
            tag = tag.replace(w, "")
        tag = re.sub(r'[，。！？、；：""''（）【】《》\s]+', ' ', tag).strip()
        # 过滤残留的短噪声字（"的"、"吗"等），但保留有意义的单字标签
        short_noise = {"的", "吗", "呢", "吧", "啊", "呀"}
        words = [w for w in tag.split() if w not in short_noise]
        tag = " ".join(words)
        return tag if len(tag) >= 1 else None

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 响应中提取 JSON。"""
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            return m.group(1)
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return m.group(0)
        return text

    @staticmethod
    def _dict_to_result(data: dict, raw_message: str) -> IntentResult:
        result = IntentResult(raw_message=raw_message)
        intent_str = data.get("intent", "NOT_IMAGE_REQUEST")
        result.confidence = float(data.get("confidence", 0.0))

        mapping = {
            "FIND_BY_ID": IntentType.FIND_BY_ID,
            "FIND_BY_TAG": IntentType.FIND_BY_TAG,
            "TOGGLE_R18": IntentType.TOGGLE_R18,
            "HELP": IntentType.HELP,
            "UNKNOWN": IntentType.UNKNOWN,
            "NOT_IMAGE_REQUEST": IntentType.NOT_IMAGE_REQUEST,
        }
        result.intent_type = mapping.get(intent_str, IntentType.NOT_IMAGE_REQUEST)

        params = data.get("params", {})
        if isinstance(params, dict):
            result.params = params

        return result
