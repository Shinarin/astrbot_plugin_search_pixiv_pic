# What's Changed

> 📢 v1.0.10 → v1.1.0

## 🔧 优化

- **联网搜索手动工具循环**：LLM 带工具调用后不再依赖 `llm_generate` 自动执行（它不支持），改为插件自行执行 `web_tool.call()` → 格式化原始 JSON 结果 → 构建 OpenAI 工具调用上下文消息 → 回传 LLM 分析。彻底解决 `completion_text=None` 时 `.strip()` 崩溃
- **角色联网搜索按需触发**：`CHARACTER_RESOLVE_PROMPT` 新增「无角色名判断」规则——只有作品名/属性词（如"碧蓝航线 大胸 黑丝"）时 `need_web_search=false`，不再浪费联网搜索。`resolve_search_intent()` 增加三级门控（有角色+确定 / 无角色 / 有角色+不确定）

## 🐛 修复

- **联网搜索 `completion_text=None` 崩溃**：`(resp.completion_text or "").strip()` 替代 `resp.completion_text.strip()`，防御 LLM 返回 tool_call 时文本为空的场景

---

> 📢 v1.0.9 → v1.0.10

## 🔧 优化

- **LLM 意图分类精确化**：重写 `INTENT_CLASSIFY_PROMPT`，明确 Pixiv 是插画艺术平台而非通用图片搜索。新增"表情包/梗图/照片/截图"等反面示例，LLM 不再误判非插画请求
- **静默放行非搜图消息**：`UNKNOWN` 意图不再反问用户，改为直接放行透传。`FIND_BY_TAG`/`FIND_BY_ID` 缺参数时同样放行，不再拦截正常聊天

## 🐛 修复

- **LLM Tool 函数签名**：`_tool_search_*` 改为 `async def` 且参数设默认值，修复 AstrBot `call_event_hook` 调用时报 `AssertionError` 和 `missing required positional argument` 的问题

---

> 📢 v1.0.8 → v1.0.9

## 🐛 修复

- **多图 original URL 构造错误**：v1.0.8 的 `_convert_to_original_url()` 错误地将漫画 URL 从 `img-master` 转为 `img-original`，但 Pixiv CDN 对漫画不提供 `img-original` 路径导致 404。改为 `_strip_cdn_sizing()` 仅去掉 CDN 尺寸前缀 `/c/WxH_Q/`，保留 `img-master` 完整分辨率

---

> 📢 v1.0.7 → v1.0.8

## 🐛 修复

- **多图作品发送原图**：Pixiv API 的 `meta_pages[].image_urls` 不含 `original` 键，导致漫画/图集始终发送压缩版。新增 `_convert_to_original_url()` 从 `large` URL 反推 `original` URL（`img-master`→`img-original`，去 `_master1200` 后缀）
- **内容审核图片传参修正**：`image=image_data`（单数 kwarg）→ `image_urls=[image_path]`（标准参数），确保图片进入 AstrBot 多模态处理流程
- **内容审核默认提供商视觉能力检查**：留空使用默认 LLM 时跳过视觉能力预检，导致纯文本模型收到图片始终返回 "0"

## 🔧 优化

- **内容审核图片传递方式**：base64 data URI → 临时文件路径，兼容性更好
- **内容审核代码精简**：删除 ~200 行无效的 SDK/HTTP 端点探测代码，统一为 `_call_moderation_llm()` 单入口 + try/except 防御
- **插件专用 LLM 提供商说明**：强烈建议使用 DeepSeek V4 模型

---

> 📢 v1.0.6 → v1.0.7

## ✨ 新增

- **搜索候选池配置**：新增 `stage0_pool_threshold`（目标张数，默认30）和 `stage0_min_pool`（最低接受张数，默认10）。阶段0全局池跨维度积累，满目标即停、不足最低则自动降维继续搜
- **`collect_fresh_illusts()`**：同 `find_fresh_illust` 的池收集逻辑但不做加权随机，供给阶段0池积累使用

## 🐛 修复

- **标签匹配度子串匹配**：`_check_tag_coverage()` 改为子串匹配（如"着衣巨乳"被"巨乳"命中），解决 Pixiv 复合标签误报缺失的问题

---

> 📢 v1.0.5 → v1.0.6

## ✨ 新增

- **多维标签富化（同义词组）**：`TAG_ENRICH_PROMPT` 改为结构化输出，attributes 使用同义词组格式 `[{"tags":["巨乳","おっぱい"],"label":"大胸"}]`。LLM 动态生成，覆盖身体特征/服装/发型/风格等所有维度，日语优先
- **降维梯度搜索**：`search_by_tags()` 阶段0 改为 k=N→2 逐级降维（每次随机-1个特征），每级最多5次随机组合。解决多属性全量搜不到时直接退到单属性的问题
- **标签匹配度检查 + 人格歉语**：发送前对比 `info.tags` 与用户要求的同义词组，缺失时 LLM 生成带人格预设的歉语，同组任一个 tag 命中即算覆盖

## 🔧 优化

- **阶段 1 池化搜索**：`base_tags`(游戏+角色) 自动参与，每组随机1个 tag 与 base 组合，结果汇入池加权随机，不短路
- **内容审核日志增强**：记录发送给视觉模型的 prompt/图片大小和模型反馈的原始文本（INFO 级别）
- **非本插件指令跳过 LLM**：收到 `/` 开头但不是 `/pixiv` 的消息直接透传 AstrBot

---

> 📢 v1.0.4 → v1.0.5

## 🐛 修复

- **修复组合搜索包含原始查询导致命中率为零**：`search_by_tags()` 组合搜索跳过 `enrich_tags()` 追加的原始短语（如"原神角色天使尼可"），只用真正的 Pixiv 标签做 AND 搜索（如"原神 ニコ"）
- **修复联网搜索工具获取失败**：照搬 AstrBot 源码 `_apply_web_search_tools` 做法，用 `get_config(umo=umo)` + `get_builtin_tool()` 替代错误的 `func_list` 遍历，确保获取到当前会话配置的网页搜索提供商

## 🔧 优化

- **非本插件指令跳过 LLM 识别**：收到 `/` 开头但不是 `/pixiv` 的消息直接透传给 AstrBot，不再浪费 LLM 调用

---

> 📢 v1.0.3 → v1.0.4

## 📝 文档

- **SKILL.md 工作流规范完善**：修正模糊表述（架构变更→具体条件、改动程度→用户界面影响）、新增 Push 前 6 步有序检查清单（含版本号同步）、新增 Git 异常处理规则（失败即停止）、新增 CHANGELOG 版本递增规则（已发布版本不可追加）

---

> 📢 v1.0.2 → v1.0.3

## 🐛 修复

- **修复 `context.llm_generate()` 缺少必传 `chat_provider_id`**：回退路径和审核回退路径通过 `get_all_providers()` 获取默认 provider ID，避免 TypeError
- **修复内容审核撤回 `message_id=None` 导致撤回失效**：`_check_and_moderate()` 新增 `message_id` 参数，`_send_illust_images()` 传递真实消息 ID，确保审核触发时能正确撤回
- **修复 LLM Tool 注册失败（v4.25.2+ JSON Schema 校验）**：`func_args` 中 `"int"` → `"integer"`、`"bool"` → `"boolean"`，符合 JSON Schema 类型规范

## ✨ 新增

- **多标签组合搜索**：`search_by_tags()` 优先尝试组合标签（AND 搜索）。如 "原神"+"ニコ" → "原神 ニコ"，精准定位"某游戏的某角色"，解决之前只搜单个标签命中不精准的问题
- **角色智能解析 + 联网搜索**：`resolve_search_intent()` 用 LLM 识别用户搜索中的游戏/作品+角色名。对不确定的冷门/新角色，自动调用 AstrBot 内置联网搜索工具（Tavily/Bocha/Baidu）确认角色信息，再用准确的日文名进行 Pixiv 搜索
- **内容审核日志增强**：记录发送给视觉模型的 prompt 和压缩后图片大小，以及模型返回的原始文本内容

## 🔧 优化

- **`conversation_state.is_waiting()` 锁安全**：移除查询路径中的无锁 `pop()` 操作，超时清理由 `cleanup_expired()` 统一负责
- **`config_manager.get_all()` 深拷贝**：改用 `copy.deepcopy()` 防止嵌套配置对象（如 `show_image_info`）被意外共享修改
- **`find_fresh_illust()` 回退路径增加 API 错误检测**：与 `search_by_tag()` 保持一致，检测 token 过期等 API 错误字段
- **`_extract_tag()` 噪声词优化**：按长度降序排列避免短词误伤，新增短噪声字集合精确过滤
- **临时图片文件自动清理**：新增 `_cleanup_temp_file()`，发送完成后立即删除临时文件，防止磁盘堆积
- **`DedupManager` 数据库自动重连**：新增 `_ensure_connection()` 健康检查，连接断开时自动恢复
- **修正 `astrbot_version` 最低要求**：`>=4.0.0` → `>=4.5.7`（插件依赖 `context.llm_generate()`）

## 📝 文档

- **DEVELOPMENT.md 文档完善**：修正架构图（LLM 优先→关键词回退）、补充缺失文件、新增核心方法速查表
- **新增 `.github/skills/pixiv-plugin-dev/SKILL.md`**：定义本项目专属的开发工作流规范（环境初始化、Git 规则、文档同步、代码规范）

---

> 📢 v1.0.1 → v1.0.2

## 🔧 优化

- **移除全局去重，改为纯会话独立去重**：每个会话（群聊/私聊）独立管理自己的去重记录（默认 20 条），互不干扰。避免「群A发过的图群B发不出」的问题。同时移除 `global_dedup_limit` 配置项。

## 🐛 修复

- **修复单图发送失败时误标记去重**：`mark_sent()` 从 `_send_image()` 之前移到之后，确保只有真正发送成功的作品才被标记为已发送，发送失败的作品下次搜索仍可被重新选中。

---

> 📢 v1.0.0 → v1.0.1

## 🐛 修复

- **修复 AstrBot 事件钩子 AssertionError**：`command_group("pixiv")` 父方法由 `def` 改为 `async def`，消除 `context_utils.py:94` 处的断言失败，该错误此前在每次消息处理时都会打印一条异常堆栈。

## 🔧 优化

- 完善插件元数据：更新 `repo` 字段为实际仓库地址，使插件支持 WebUI 自动更新检测。

---

<details>
<summary>📦 v1.0.0 初始版本</summary>

## ✨ 功能

- Pixiv 图片检索：支持按作品 ID (`/pixiv id`) 和标签 (`/pixiv tag`) 搜索
- 自然语言意图识别：基于 LLM 自动分类用户意图（找图/切R18/帮助/无关）
- 中文标签富化：LLM 自动将中文标签翻译为日语/英语以提升搜索命中率
- 加权随机去重：基于作品热度（bookmarks）加权随机选图，避免重复发送
- 多页漫画/图集支持：自动检测并发送多页作品，可配置最大页数
- R18 内容过滤：三种模式（safe/off/r18_only），支持管理员切换
- 定时消息撤回：可配置的自动撤回，通过 OneBot delete_msg API
- 视觉内容审核：可选启用，使用视觉 LLM 对图片进行 NSFW 评分
- 人格化回复：注入 AstrBot 当前人格提示词，生成带角色风味的自然语言回复
- 会话状态管理：支持追问澄清流程，对话上下文保持
- Token 自动刷新：50 分钟周期自动刷新 Pixiv access_token，搜索失败时自愈重试
- LLM Tool 注册：向 AstrBot Agent 注册 3 个工具（搜ID/搜标签/切R18）
- WebUI 配置面板：基于 `_conf_schema.json` 自动生成设置表单

</details>
