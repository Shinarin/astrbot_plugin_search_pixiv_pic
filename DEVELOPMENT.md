# 🛠️ astrbot_plugin_search_pixiv_pic 开发文档

> 本文档旨在帮助新开发者快速了解项目结构、核心架构和开发约定。
> 如需更换开发 Agent，请先阅读本文档。

---

## 📂 项目结构一览

```
astrbot_plugin_search_pixiv_pic/
├── main.py                   # 🔴 插件入口 — Star 类 + 所有 handler
├── metadata.yaml             # 🟡 AstrBot 插件元信息
├── requirements.txt          # 🟡 Python 依赖
├── _conf_schema.json         # 🟡 WebUI 配置表单定义（20+ 配置项）
├── README.md                 # 🟢 用户文档
├── DEVELOPMENT.md            # 🟢 本文档
├── CHANGELOG.md              # 🟢 更新日志
└── src/                      # 🔴 核心逻辑
    ├── __init__.py            #   包初始化
    ├── pixiv_client.py        #   Pixiv API 封装
    ├── intent_parser.py       #   LLM 意图分类
    ├── dedup_manager.py       #   去重管理
    ├── config_manager.py      #   配置管理
    └── conversation_state.py  #   对话状态机
```

🔴 = 核心代码，修改需谨慎 | 🟡 = 配置文件 | 🟢 = 文档

---

## 🏛️ 架构概览

```
                        ┌─────────────────────┐
                        │    用户消息          │
                        └─────────┬───────────┘
                                  │
                        ┌─────────▼───────────┐
                        │   main.py            │
                        │   AstrBotPixivPlugin │
                        │   (Star 基类)        │
                        └──┬──────┬──────┬────┘
                           │      │      │
              ┌────────────▼┐  ┌──▼──┐  └──────────────┐
              │ /pixiv 指令  │  │ NL  │                 │
              │ 直接路由     │  │拦截器│                 │
              └──────┬──────┘  └──┬──┘                 │
                     │            │                     │
              ┌──────▼────────────▼──────┐              │
              │   intent_parser.py       │              │
              │   ┌──────────────────┐   │              │
              │   │ LLM 深度分类     │   │              │
              │   │   (优先通路)     │   │              │
              │   │       ↓          │   │              │
              │   │ 关键词回退匹配   │   │              │
              │   │   (降级方案)     │   │              │
              │   └──────────────────┘   │              │
              └──────────┬───────────────┘              │
                         │                              │
          ┌──────────────┼──────────────┐               │
          │              │              │               │
    ┌─────▼─────┐ ┌──────▼──────┐ ┌────▼─────┐        │
    │FIND_BY_ID │ │FIND_BY_TAG  │ │UNKNOWN   │        │
    └─────┬─────┘ └──────┬──────┘ └────┬─────┘        │
          │              │              │               │
    ┌─────▼──────────────▼──────┐ ┌────▼──────────┐    │
    │  pixiv_client.py          │ │conversation_   │    │
    │  ┌──────────────────┐    │ │state.py (反问) │    │
    │  │ search_by_id()   │    │ └────────────────┘    │
    │  │ search_by_tag()  │    │                        │
    │  │ download_image() │    │                        │
    │  └────────┬─────────┘    │                        │
    └───────────┼──────────────┘                        │
                │                                       │
    ┌───────────▼───────────────┐                      │
    │  dedup_manager.py         │                      │
    │  会话独立去重 (默认20)    │                      │
    └───────────┬───────────────┘                      │
                │                                       │
    ┌───────────▼───────────────┐                      │
    │  config_manager.py        │◄─────────────────────┘
    │  R18开关 / Token / 限制   │
    └───────────────────────────┘
```

### 搜索管线（enrich_tags → search_by_tags → 分级采集+加权随机）

```
enrich_tags(user_tag)               ← LLM 多维标签富化
  │  输出: {flat: [...], game: [...], character: [...],
  │         attributes: [{tags:["巨乳","おっぱい"],label:"大胸"}, ...]}
  ▼
search_by_tags(flat, enrichment)    ← 4 阶段分级搜索
  │
  ├─ 阶段 0: 降维梯度搜索 (k=N→2)
  │    全局池跨 k 级积累，每级最多5次随机组合
  │    满阈值(默认30)即停，不足最低(默认10)则降维继续
  │    k=3: game+char+金髪+巨乳+白タイツ → 池积累
  │    k=2: game+char+巨乳+白タイツ     → 池积累（金发被随机丢弃）
  │    → 加权随机选图
  │
  ├─ 阶段 1: base(游戏+角色) × 每组1随机tag → 池化→加权随机
  ├─ 阶段 2: 单维度标签搜索 → 池化→加权随机
  └─ 阶段 3: flat 标签逐个短路搜索（兜底）
```

> ⚠️ **架构要点**: 随机必须在去重之后。如果先去重前随机采样，可能抽到的全是已发送作品，
> 导致"明明 Pixiv 有图却搜不到"。当前设计确保新鲜池 = 全部可用的未发送作品，不会遗漏。
>
> ⚠️ **去重时机**: `mark_sent()` 必须在发送成功后调用，不能提前。如果发送失败（网络错误等），
> 作品不应被标记为已发送，以便下次搜索重试。单图和多图两条路径均遵循此原则。

---

## 🔄 核心数据流

### 1. 显式指令流程

```
用户: /pixiv id 12345678
  → @filter.command_group("pixiv") → cmd_pixiv_id()
  → pixiv_client.search_by_id(12345678)
  → _send_illust_images()
    → download_image()
    → _send_image() / fallback yield
    → _check_and_moderate()（如启用）
    → dedup_mgr.mark_sent()           ← 发送成功后才标记
```

### 2. 自然语言流程

```
用户: "找一张猫娘的 pixiv 图"
  → PixivMessageFilter 预过滤（群聊@检测）
  → natural_language_handler()
  → 清理 @前缀、跳过 /pixiv 指令
  → 检查 WAITING_CLARIFICATION 状态
  → intent_parser.parse() → LLM 分类 (6 种意图)
  → FIND_BY_TAG → _nl_find_by_tag()
    → 提取 count (LLM 识别张数)
    → resolve_search_intent() 角色解析（必要时联网搜索）
    → enrich_tags() 多维标签富化（同义词组格式）
    → inject_persona() 注入人格
    → generate_search_reply() 拟人化提示
    → search_by_tags() 4 阶段分级搜索
    → download_image() + 发送 + 审核 + 标签匹配度检查
  → NOT_IMAGE_REQUEST → clear_result() + continue_event() 透传
```

### 3. 反问流程

```
用户: "来张图" (模糊)
  → intent_parser.parse() → UNKNOWN
  → _nl_unknown() → generate_clarification()
  → conv_state.set_waiting() → WAITING_CLARIFICATION
  → 回复: "🤔 请问您想搜索什么主题/标签的图片呢？"

用户: "随便" (仍模糊)
  → natural_language_handler()
  → is_waiting(session_id) = True
  → _handle_clarification_response()
  → intent_parser.parse("随便") → 仍 UNKNOWN
  → 回复: "😔 抱歉，仍然无法理解..." → 终止
  → conv_state.clear()
```

---

## 📦 模块职责清单

| 模块 | 文件 | 核心类 | 职责 |
|------|------|--------|------|
| **入口** | `main.py` | `AstrBotPixivPlugin` | 命令注册、消息拦截、流程编排 |
| **Pixiv** | `src/pixiv_client.py` | `PixivClient` | API 封装、认证、下载、加权随机选图、标签黑名单、🔁 token 自动刷新 |
| **意图** | `src/intent_parser.py` | `IntentParser` | LLM 分类、多维标签富化、角色联网解析、反问/歉语/拟人化回复 |
| **去重** | `src/dedup_manager.py` | `DedupManager` | SQLite 记录、重复检查、自动重连 |
| **配置** | `src/config_manager.py` | `ConfigManager` | 配置读写、持久化 |
| **状态** | `src/conversation_state.py` | `ConversationStateManager` | 对话状态、超时清理 |

### 核心方法速查

| 方法 | 所在文件 | 说明 |
|------|---------|------|
| `_search_and_send_images()` | `main.py` | 标签搜索+多图发送的核心编排，串联角色解析→富化→分级搜索→发送→审核 |
| `_send_illust_images()` | `main.py` | 发送单作品图片（支持多页漫画/图集），处理定时撤回、内容审核、标签匹配度检查 |
| `_check_tag_coverage()` | `main.py` | 对比图片标签与用户要求的同义词组，缺失时 LLM 生成人格歉语 |
| `_send_image()` | `main.py` | 底层 OneBot API 发送图片+文字，返回 `message_id` |
| `_cleanup_temp_file()` | `main.py` | 发送完成后删除临时图片文件 |
| `_call_moderation_llm()` | `main.py` | 封装 llm_generate 审核调用，try/except 防御 AstrBot v4.25.2 多模态 usage bug |
| `resolve_search_intent()` | `intent_parser.py` | LLM 识别游戏+角色，不确定时调用 AstrBot 内置联网搜索 |
| `enrich_tags()` | `intent_parser.py` | 多维标签富化，返回 `{flat, game, character, attributes:[同义词组]}` 结构 |
| `search_by_tags()` | `pixiv_client.py` | 4 阶段分级搜索：降维梯度(每级5次随机)→base×attr池化→单维度池化→flat短路 |
| `collect_fresh_illusts()` | `pixiv_client.py` | 同 find_fresh_illust 池收集逻辑，返回完整列表供阶段0池积累 |
| `find_fresh_illust()` | `pixiv_client.py` | 遍历多页收集未发送作品，加权随机选图；高质量无结果时回退 |
| `_strip_cdn_sizing()` | `pixiv_client.py` | 去掉 Pixiv CDN 尺寸/质量前缀 /c/WxH_Q/，保留 img-master 完整分辨率（漫画无 img-original） |
| `_ensure_connection()` | `dedup_manager.py` | 数据库连接健康检查与自动重连 |

---

## 🔌 外部依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `pixivpy3` | ≥3.0.0 | Pixiv 非官方 API |
| `httpx` | ≥0.25.0 | 异步 HTTP 客户端（图片下载） |
| `Pillow` | ≥10.0.0 | 图片处理（可选） |
| `AstrBot` | ≥4.5.7 | 插件框架（隐式依赖，需要 `context.llm_generate()`） |

---

## 🧪 状态说明

### 当前进度

| Phase | 状态 | 完成内容 |
|-------|------|---------|
| Phase 1: 项目骨架 | ✅ 完成 | metadata.yaml, requirements.txt, main.py 骨架 |
| Phase 2: Pixiv API | ✅ 完成 | pixiv_client.py (search_by_id/tag, download) |
| Phase 3: 去重管理 | ✅ 完成 | dedup_manager.py (SQLite 会话去重) |
│ Phase 4: 配置管理 | ✅ 完成 | config_manager.py (20+ 配置项，WebUI 表单) |
| Phase 5: 状态机+意图 | ✅ 完成 | conversation_state.py + intent_parser.py |
| Phase 6: 主逻辑集成 | ✅ 完成 | main.py 完整版 (所有 handler + NL 拦截器) |
| Phase 7: 测试验证 | ⬜ 待做 | 单元测试 + 集成测试 |

### 已知限制

1. **LLM 调用依赖 AstrBot 环境**: `intent_parser.py` 通过 `context.llm_generate()` 调用 LLM（优先专用 provider，自动回退默认），无需 event
2. **Pixiv API 稳定性**: 依赖 pixivpy3 和非官方 API，可能因 Pixiv 更新而失效
3. **无图片缓存**: 每次从 Pixiv CDN 实时获取，无本地缓存
4. **配置存储**: 优先使用 AstrBot 内置配置，回退到本地 JSON
5. **热度排序需 Premium**: `popular_desc` 排序需要 Pixiv Premium，否则自动回退为时间排序

### Token 自动刷新

Pixiv 的 Access Token（由用户配置的 Refresh Token 换取）有效期约 1 小时，过期后搜索 API 会**静默返回空列表**（不报错）。
插件在 `PixivClient.login()` 成功后启动后台 `_auto_refresh_loop()`，每 10 分钟用 refresh_token 重新认证（失败也按此间隔重试），避免"幽灵故障"。

配套防御措施：
- `search_by_tag` 检测 API 返回的 `error` 字段（对象和字典两种类型均覆盖）
- `search_by_tag` 首页空 → **即时刷新 Access Token 并重试一次**（自愈机制）
- `find_fresh_illust` 首页空 → 立即跳过该标签翻页，不浪费后续 API 调用
- `search_by_tags` 连续 ≥3 个标签首页全空 → 红色告警"极可能是 token 过期"
- `_call_llm` 专用 LLM 不可用时自动回退到 AstrBot 默认 LLM（之前无此回退）
- `_consecutive_empty` 计数器 + `get_empty_search_hint()` → 搜索失败时按连续次数给出分级建议（1次提示/2次建议检查/3次+强烈建议刷新token）

### 多图作品发送（漫画/图集）

Pixiv 漫画和插画图集包含多张图片。`IllustInfo` 通过 `page_count` 和 `meta_pages`（`_normalize_meta_pages()` 提取）记录各页 URL。

发送逻辑集中在 `main.py` 的 `_send_illust_images()`：
- 单图：直接下载发送（原逻辑不变）
- 多图：逐页下载，按 `max_pages_per_illust`（默认 3）限制页数
- 超出上限时追加提示「该作品共 X 页，完整版请访问官网」

`cmd_pixiv_id` 和 `_search_and_send_images` 均通过此方法发送，确保指令搜索和自然语言搜索行为一致。

### 定时撤回

`_schedule_recall()` 在每张图片发送后创建后台 asyncio 任务，等待 `recall_after_seconds` 秒后尝试撤回。撤回通过 OneBot `bot.delete_msg()` 或 `bot.call_action('delete_msg', ...)` 实现，失败时静默忽略。设为 0 则禁用。

### 视觉模型内容审核

开启 `content_moderation_enabled` 后，`_check_and_moderate()` 在图片发送后执行：
1. `_moderate_image()` 用 Pillow 压缩图片到 ~100KB，保存为临时文件
2. 通过 `_call_moderation_llm()` → `context.llm_generate(image_urls=[file_path])` 发送给视觉 LLM
3. 解析模型返回的 0~10 评分
4. 若评分 > `nsfw_threshold`（默认 8），通过 `message_id` 撤回图片并调用 `_generate_moderation_apology()` 生成带人格预设的道歉回复

> **已知问题**: AstrBot v4.25.2 处理多模态响应时 `usage=None` 导致 `input_tokens` 崩溃。`_call_moderation_llm()` 已做 try/except 防御，异常时返回 None（审核跳过，图片正常发送）。

视觉模型可通过 `content_moderation_provider`（`_special: select_provider`）独立选择。
审核后发送的临时图片文件由 `_cleanup_temp_file()` 自动清理。
### 待扩展功能

- [x] 多图智能发送（LLM 识别数量）
- [x] 加权随机选图（去重后从新鲜池随机）
- [x] 标签黑名单
- [x] Token 自动刷新 + 过期告警
- [x] 首页空跳过翻页优化
- [x] LLM 调用双回退（专用→默认→关键词）
- [x] 多图作品支持（漫画/图集按页发送）
- [x] 即时自愈：搜索空→刷新token重试
- [x] 智能失败建议：按连续空搜索次数分级提示
- [x] 定时撤回：图片发送后 N 秒自动撤回
- [x] 视觉模型内容审核：压缩→视觉 LLM 评分→超阈值撤回+道歉
- [x] 多维标签富化：LLM 同义词组格式，日语优先
- [x] 降维梯度搜索：多特征自动 -1 继续，每级5次随机组合
- [x] 标签匹配度检查：缺失特征 LLM 人格歉语
- [x] 角色联网搜索：调用 AstrBot 内置网页搜索工具确认角色
- [x] 搜索候选池配置：目标张数 + 最低接受张数，跨维度积累
- [ ] 画师搜索
- [ ] 排行榜浏览
- [ ] WebUI 管理面板
- [ ] 本地图片缓存
- [ ] 搜索结果分页浏览

---

## 📝 代码约定

### 注释规范

- 所有类和方法都必须有 docstring（Google 风格）
- 复杂逻辑必须在注释中解释原因
- 使用 `# ---` 分隔代码段
- 日志标签统一为 `[pixiv:模块名]`

### 命名约定

- 类名: PascalCase (`PixivClient`, `DedupManager`)
- 方法/函数: snake_case (`search_by_id`, `is_duplicate`)
- 私有方法: 前缀 `_` (`_cleanup_session`, `_ensure_logged_in`)
- 常量: UPPER_SNAKE_CASE (`DEFAULT_CONFIG`, `STATE_TIMEOUT`)

### 错误处理

- Pixiv 相关的自定义异常在 `pixiv_client.py` 中定义
- 所有网络/API 异常都必须捕获并转换为友好提示
- 使用 `logger.error()` 记录完整 traceback

---

## 🚀 快速上手（新 Agent）

1. **阅读本文档**（你正在读 ✅）
2. **看 `main.py`** — 了解命令注册和消息拦截模式
3. **看 `src/pixiv_client.py`** — 了解 Pixiv API 交互
4. **看 `src/intent_parser.py`** — 了解 LLM prompt 设计
5. **运行测试** — `python -m pytest tests/`
6. **开始修改** — 修改前先创建新分支

---

## 📞 联系方式

- AstrBot 官方: https://github.com/AstrBotDevs/AstrBot
- pixivpy3 文档: https://github.com/upbit/pixivpy
- Pixiv: https://www.pixiv.net
