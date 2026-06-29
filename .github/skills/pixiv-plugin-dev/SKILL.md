---
name: pixiv-plugin-dev
description: 'Development workflow for astrbot_plugin_search_pixiv_pic. Enforces: read AstrBot docs before dev, NEVER git push without user approval, sync docs before each push.'
argument-hint: 'Describe the task (e.g., "start fresh setup", "prepare git push", "review changes for docs")'
user-invocable: true
disable-model-invocation: false
---

# Pixiv Plugin Development Workflow

本 skill 定义了 `astrbot_plugin_search_pixiv_pic` 项目的开发工作流规范。**仅限本项目使用**。

---

## 1. 新环境初始化（Fresh Setup）

> ⚠️ **一次性流程**：以下步骤**仅在**新环境首次开发时执行一次。之后的开发任务中，直接使用已建立的项目认知进行修改，**无需**重复阅读所有文档。
>
> 触发条件：全新工作区、首次接触本项目、或用户明确说"初始化"/"从头了解"。

当在**新的开发环境**中首次打开本项目时，必须按以下顺序了解上下文：

### 1.1 了解 AstrBot 框架

先阅读 AstrBot 框架文档和插件开发文档，建立框架认知：

#### 必读文档（按顺序）

| 优先级 | 文档 | 链接 |
|--------|------|------|
| 🔴 必读 | AstrBot 主仓库 | https://github.com/AstrBotDevs/AstrBot |
| 🔴 必读 | 插件开发指南（官方文档，新版） | https://docs.astrbot.app/dev/star/plugin-new.html |
| 🟡 推荐 | 插件开发指南（旧版，v4.5.7 前） | https://docs.astrbot.app/dev/star/plugin.html |
| 🟡 推荐 | AstrBot 源码 `star/star.py` | https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/star.py |
| 🟡 推荐 | AstrBot 源码 `star/context.py` | https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/context.py |
| 🟢 参考 | AstrBot 源码 `platform/astr_message_event.py` | https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/astr_message_event.py |

#### 关键 API 速查

- `Star.__init__(context, config)` → `initialize()` → `terminate()`
- `Context`: `llm_generate()`, `persona_manager`, `add_llm_tools()`, `provider_manager`
  - 注：`register_llm_tool()` 已弃用，新插件请继承 `FunctionTool` 类并通过 `add_llm_tools()` 注册
- `AstrMessageEvent`: `plain_result()`, `make_result()`, `get_session_id()`, `stop_event()`, `continue_event()`
- `@command_group()`, `.command()`, `@custom_filter()`, `@on_plugin_error()`
- 配置: `_conf_schema.json` → WebUI 自动表单 → `AstrBotConfig` 注入

### 1.2 了解本项目

按以下顺序阅读项目文档：

1. **`DEVELOPMENT.md`** — 项目架构、核心数据流、模块职责清单
2. **`README.md`** — 用户文档、功能特性、配置项说明
3. **`CHANGELOG.md`** — 版本历史、了解最近改动
4. **`metadata.yaml`** — 插件元信息
5. **`_conf_schema.json`** — 配置项 schema
6. **源码**（按依赖关系）:
   - `src/config_manager.py` → `src/dedup_manager.py` → `src/conversation_state.py`
   - `src/pixiv_client.py` → `src/intent_parser.py` → `main.py`

> ✅ 初始化完成后，后续的开发对话中直接基于已有认知工作，**不再**重复加载这些文档。如果你修改了模块职责、数据流、接口、依赖版本、配置项结构或命令行为，必须重新确认 DEVELOPMENT.md / README.md。否则不需要重复阅读所有文档。

---

## 2. Git 工作流规则

### 🚫 严禁私自 git push

> **绝对禁止**在没有用户明确指令的情况下执行 `git push`。

- 所有 git push 操作**必须**由用户明确授权
- 即使代码已 commit，也不得自行 push
- 如果用户说"提交代码"，只执行 `git add` + `git commit`，**不**执行 `git push`
- 只有当用户明确说"push"、"推送到远程"、"git push"等指令时，才可以执行 push

### 提交流程

```
用户说"提交"/"commit" → git add + git commit（不 push）
用户说"push"/"推送"   → 先同步文档（见第3节），再 git push
```

### ⚠️ 异常处理

> **失败即停止，不要猜测。**

- **无待提交变更**：如果 `git status` 显示没有改动，回复"当前没有待提交的变更"，不要执行空提交。
- **commit 失败**：如果 `git commit` 失败（如 pre-commit hook 拦截、网络问题），停止并说明错误原因，**不要**继续执行 `git push`。
- **push 失败**：如果 `git push` 被拒绝（如远程冲突、权限不足），停止并报告具体错误，不要反复重试或猜测。
- **检查项未通过**：如果 push 前的文档同步检查（第 3 节清单）任何一步失败或未完成，先处理问题，**不要**跳过检查强行 push。

---

## 3. 文档同步规则（Push 前必做）

### Push 前检查清单（按顺序执行，全部必做，不得跳过）

> ⛔ **以下 6 步全部为强制步骤，不可跳过任何一步。** 即使你认为某一步"没有变化"，也必须打开文件确认，并在回复中明确陈述"第 X 步已检查：无须更新"。

执行 git push 前，按以下顺序完成：

1. **✍️ 更新 `CHANGELOG.md`** — 在文件顶部添加本次改动条目（见下方 3.1 节）
2. **🔢 更新插件版本号** — `metadata.yaml` 中的 `version` 需与 CHANGELOG 最新版本保持一致
3. **📐 检查并更新 `DEVELOPMENT.md`** — 逐项比对模块职责/数据流/架构/依赖/Phase 进度，有差异则更新（见下方 3.2 节）
4. **📖 检查并更新 `README.md`** — 逐条对照决策表判断是否需要更新（见下方 3.3 节）
5. **🔗 验证 `_conf_schema.json` ↔ README 一致性** — 两者配置项必须同步（见下方 3.4 节）
6. **📦 检查 `requirements.txt`** — 如依赖版本变更则同步更新（见下方 3.5 节）

---

**每次 git push 之前**，必须完成以下文档同步：

### 3.1 更新 `CHANGELOG.md`

在文件顶部（`# What's Changed` 下方、最新版本记录上方）添加本次改动条目：

```markdown
## 🔧 优化 / 🐛 修复 / ✨ 新增

- **改动简述**：具体说明改了什么、为什么改。
```

分类标签：
- `✨ 新增` — 新功能
- `🔧 优化` — 改进现有功能
- `🐛 修复` — Bug 修复
- `📝 文档` — 纯文档更新

> ⚠️ **版本递增规则**：如果 CHANGELOG 中最新的版本记录已经 push 到远程仓库，则必须递增版本号（如 v1.0.3 → v1.0.4），新建一个版本条目，不能往回追加到已发布的版本中。判断方法：`git log --oneline origin/master` 查看远程是否已有该版本对应的 commit。

### 3.2 检查并更新 `DEVELOPMENT.md`

> ⛔ **每次 push 前必须逐项比对，不得跳过。** 即使最终无须修改，也必须打开 `DEVELOPMENT.md` 逐项确认。

打开 `DEVELOPMENT.md`，逐项比对以下内容，有差异则立即更新：

- **模块职责清单**（新增/删除/重命名模块）
- **核心数据流**（流程变更）
- **架构概览**（架构调整）
- **外部依赖**（依赖版本变更）
- **状态说明**（Phase 进度更新）

如果确认无需更新，在回复中明确陈述「第 3 步已检查：DEVELOPMENT.md 无须更新」。

### 3.3 检查并更新 `README.md`

> ⛔ **每次 push 前必须逐条对照决策表，不得跳过。** 即使最终判定无须更新，也必须走完决策流程。

打开 `README.md`，对照下方决策表逐条判断是否需要更新：

| 改动类型 | 是否更新 README |
|---------|---------------|
| 新增/删除功能特性 | ✅ 必须更新 |
| 新增/删除配置项 | ✅ 必须更新 |
| 新增/删除指令 | ✅ 必须更新 |
| 使用方式变更 | ✅ 必须更新 |
| 安装步骤变更 | ✅ 必须更新 |
| 修改 `_conf_schema.json` | ✅ 必须同步更新 README 配置表 |
| Bug 修复（无功能变化） | ❌ 无需更新 |
| 内部重构（无行为变化） | ❌ 无需更新 |
| 性能优化（无行为变化） | ❌ 无需更新 |
| 纯文档/注释更新 | ❌ 无需更新 |

更新 README 时同步更新以下章节：
- `✨ 功能特性` — 新增/删除功能点
- `⚙️ 配置` — 配置项变更（**特别说明**：修改 `_conf_schema.json` 中的配置项名称、类型、默认值、选项时，必须同步更新 README 中的「完整配置项」表格）
- `📖 使用方法` — 指令或用法变更
- `📦 安装` — 安装步骤变更

### 3.4 检查 `_conf_schema.json` ↔ README 一致性

`_conf_schema.json` 是 WebUI 配置表单的定义文件，README 中的「完整配置项」表格是其面向用户的说明。**两者必须保持一致**。

修改 `_conf_schema.json` 时，检查以下字段是否在 README 配置表中同步：
- 配置项 key 名称
- `type` / `default` / `description`
- 下拉选项的 `choices` 列表
- 嵌套配置项（如 `show_image_info` 的子开关）

### 3.5 检查 `requirements.txt`

> ⛔ **每次 push 前必须确认。** 如果新增/移除/变更了 Python 依赖版本，同步更新 `requirements.txt`。如无须更新，在回复中陈述「第 6 步已检查：requirements.txt 无须更新」。

---

### ✅ 清单完成标准

**所有 6 步检查完毕并确认后**，在回复中汇总报告：

```
📋 Push 前检查完成：
  ✅ 1. CHANGELOG.md — 已更新（v1.0.4）
  ✅ 2. metadata.yaml — 版本号已同步
  ✅ 3. DEVELOPMENT.md — 已检查，无须更新
  ✅ 4. README.md — 已更新配置表
  ✅ 5. _conf_schema.json ↔ README — 一致
  ✅ 6. requirements.txt — 无须更新

  🚀 准备 push，等待用户确认...
```

> ⛔ **只有全部 6 步通过后，才能执行 `git push`。任何一步未完成或报错，先解决问题，禁止跳过。**

## 4. 代码修改规范

### 修改前

- 确认理解了 `DEVELOPMENT.md` 中的架构设计
- 确认修改不会破坏已有的核心数据流
- 涉及配置项修改时，同步更新 `_conf_schema.json` **和** README 配置表（见 3.4 节）

### 修改后

- 确保模块间接口兼容
- 不引入新的硬编码（使用 `ConfigManager` 管理配置）
- 遵循现有代码风格（类型注解、docstring、日志级别）

---

## 5. 常用参考

| 内容 | 文件 |
|------|------|
| 架构与数据流 | `DEVELOPMENT.md` |
| 用户文档 | `README.md` |
| 版本历史 | `CHANGELOG.md` |
| 模块职责 | `DEVELOPMENT.md` → 模块职责清单 |
| 配置项定义 | `_conf_schema.json` |
| 插件元数据 | `metadata.yaml` |
