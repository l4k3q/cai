# CAI Web 操作台开发文档

> 基于 `stoltetetiana235-a11y/cai`（CAI — Cybersecurity AI 框架）开发的 Web 前端操作台
> 与完整开发记录。文档涵盖环境搭建、Web UI 实现、会话创建流程改造、以及所有踩坑与修复。

---

## 目录

- [1. 项目概述](#1-项目概述)
- [2. 环境搭建](#2-环境搭建)
- [3. Web UI 实现](#3-web-ui-实现)
- [4. 核心功能](#4-核心功能)
- [5. 后端改动清单](#5-后端改动清单)
- [6. 排障与修复记录](#6-排障与修复记录)
- [7. 启动与使用](#7-启动与使用)
- [8. 验证记录](#8-验证记录)
- [9. 后续方向](#9-后续方向)

---

## 1. 项目概述

CAI（Cybersecurity AI）是 Alias Robotics 开源的 AI 安全自动化框架：基于 Agent 架构，内置按 kill-chain 分组的安全工具（侦察 / 利用 / 提权 / 横向移动 / 数据外渗 / C2），通过 LiteLLM 支持 300+ 模型，集成 MCP 协议，支持人机协作（HITL）。

本项目在此框架上做了两件事：

1. **搭建完整开发环境**（WSL + Python 3.12 + uv + 模型连通）
2. **开发了一个 Neubrutalism 风格的 Web 操作台**（`webui/`），集成进 CAI 自带 HTTP API 服务，用于对话 + 实时 Agent 推理 trace，并支持**每会话自定义模型 provider（base URL + API key）**。

---

## 2. 环境搭建

### 2.1 环境选型

| 项 | 选择 | 说明 |
|---|---|---|
| 系统 | Windows 11 + WSL2（Ubuntu 22.04） | CAI 官方推荐 Windows 用 WSL |
| Python | 3.12.13（通过 `uv` 安装） | CAI 依赖 `>=3.10`，官方推荐 3.12 |
| 包管理 | `uv`（仓库自带 `uv.lock`） | 免 sudo，可编辑安装 |
| 模型 | `anthropic/claude-3-5-sonnet-20240620` @ mnapi | 通过 `.env` 配置 |

### 2.2 安装步骤（WSL）

```bash
# 1. 克隆 fork（浅克隆避免大仓库 TLS 断流）
git clone --depth 1 --single-branch --branch main \
  https://github.com/stoltetetiana235-a11y/cai.git ~/cai
cd ~/cai

# 2. 安装 uv + Python 3.12（免 sudo）
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12

# 3. 安装依赖（可编辑安装，改代码即时生效）
uv sync

# 4. 配置 .env
cp .env.example .env
# 关键变量：
#   ANTHROPIC_API_BASE / ANTHROPIC_API_KEY  → 自定义模型端点
#   CAI_MODEL                                → 默认模型（如 anthropic/claude-3-5-sonnet-20240620）
#   CAI_LICENSE_OFF=1                        → 开源模式，跳过 Alias license 检查
```

### 2.3 模型连通性验证

用 LiteLLM 直测（首次验证自定义端点 + key 是否可用）：

```bash
uv run python -c "
import litellm
resp = litellm.completion(
    model='anthropic/claude-3-5-sonnet-20240620',
    messages=[{'role':'user','content':'Reply exactly: OK'}],
    max_tokens=10)
print(resp.choices[0].message.content)
"
```

---

## 3. Web UI 实现

### 3.1 技术选型

**零构建 vanilla HTML/CSS/JS**（无 node/npm 依赖），理由：
- 安全工具场景下易改、易扩展、无供应链风险
- 后端 `cai --api`（FastAPI）直接挂载静态目录，同源零配置

### 3.2 设计规范（Neubrutalism，用户指定）

| 项 | 规范 |
|---|---|
| 标题字体 | Space Grotesk 700-900 |
| 正文字体 | IBM Plex Sans 400 |
| 主色 | `#000000`（纯黑） |
| 强调色 | `#FF6B00`（橙色） |
| 背景 | `#FFFDF0`（米白） |
| 边框 | 3px 黑色实线 |
| 圆角 | 全 0px（直角） |
| 阴影 | `box-shadow: 8px 8px 0 #000`（硬阴影） |
| 布局 | 不对称三栏，粗黑线分隔 |
| 动画 | 加载时从下方弹入；hover 按钮上移 2px；active 位移塌陷（3D 效果） |
| 纹理 | SVG feTurbulence 噪点 |

### 3.3 文件结构

```
webui/
├── index.html      # 页面骨架（header + 三栏 + composer + 弹窗）
├── css/style.css   # Neubrutalism 设计系统
├── js/app.js       # API 封装 / SSE 解析 / 会话管理 / 渲染
└── README.md       # 使用说明
```

### 3.4 页面结构

```
┌──────────────────────────────────────────────────────┐
│ Header: 品牌「CAI//OPS」 · 健康徽章 · API Key 设置      │
├──────────┬──────────────────────────┬─────────────────┤
│ Sidebar  │  Chat 面板                │  Activity Trace │
│ · NEW    │  · 对话历史（可滚动）       │  实时 tool_call │
│   SESSION│  · Composer (Ctrl+Enter)  │  /tool_output   │
│ · 会话列表│  · SEND/STOP              │  /handoff 等    │
│ · Agents │                           │                 │
│ · Models │                           │                 │
└──────────┴──────────────────────────┴─────────────────┘
```

- **左侧**：新建会话按钮、会话列表（agent/模型/消息数/时间/删除）、Agent 与模型目录（可展开）
- **中间**：会话对话区（历史 + 流式增量渲染）、底部 composer（Ctrl+Enter 发送，运行中转 STOP）
- **右侧**：实时 trace 面板，逐条渲染 SSE `reasoning_step`

---

## 4. 核心功能

### 4.1 会话管理
- 新建 / 切换 / 删除会话；支持 `stateful`（保留上下文）
- 切换会话时拉取 `GET /sessions/{id}/history` 回显历史

### 4.2 对话 + 实时 trace
- 发消息走 `POST /sessions/{id}/messages/stream`（SSE）
- 事件类型：`reasoning_step`（`tool_call` / `tool_output` / `handoff` / `agent_switched` / `message`）、`final`、`error`
- 工具调用/输出折叠展示（`<details>` + JSON 格式化）

### 4.3 每会话自定义 provider（重点改造）

创建会话流程改为：**选 agent → 填 BASE URL + API KEY → FETCH MODELS → 选模型 → CREATE**

```
NEW SESSION 弹窗
  1. AGENT         下拉（37 个 agent，传内部 id）
  2. BASE URL      文本框（如 https://api.deepseek.com）
  3. API KEY       密码框（留空 = 用 .env 全局配置）
  4. ⬆ FETCH MODELS 按钮（调 POST /api/v1/models/fetch 拉模型）
  5. MODEL         下拉（由 fetch 结果填充，自动补 provider 前缀）
  6. stateful 复选 + CREATE
```

**模型名前缀自动补全**（`normalizeModelId`）：
- `claude-*` / `anthropic-*` → `anthropic/{id}`
- `deepseek-*` → `deepseek/{id}`
- `gpt-*` / `o1` / `o3` / `o4` → `openai/{id}`
- `gemini-*` → `gemini/{id}`；`mistral-*` → `mistral/{id}`
- 其余按 base URL 的 host 推断；无法推断则保持裸名（依赖注入的 api_base）

---

## 5. 后端改动清单

| 文件 | 改动 |
|---|---|
| `src/cai/api/app.py` | ① `/` 静态挂载 webui（`CAI_WEBUI=0` 可关）；② 新增 `POST /api/v1/models/fetch`（httpx 调 `{base}/v1/models`，base/key 留空回退 `.env`，401/403 返回中文提示）；③ `create_session` 端点透传 `base_url`/`api_key` |
| `src/cai/api/schemas.py` | `CreateSessionRequest` 加 `base_url`/`api_key`；新增 `ProviderFetchRequest`/`ProviderFetchResponse`；`AgentMetadataModel` 加 `id`（内部名） |
| `src/cai/api/sessions.py` | `SessionState` 存 `provider_base`/`provider_key`，创建后调 `apply_provider_to_agent` 挂到 agent 树 |
| `src/cai/api/streaming.py` | `sse_stream_via_hooks` 在 `Runner.run` 异常时发 `error` SSE 事件（原来会让流崩溃导致前端永远 loading） |
| `src/cai/util/config_utils.py` | 新增 `apply_provider_to_agent`（递归给 agent 树含 handoffs 的 model 对象挂 `_provider_base`/`_provider_key`） |
| `src/cai/util/__init__.py` | 导出 `apply_provider_to_agent` |
| `src/cai/sdk/agents/models/openai_chatcompletions.py` | ① `_fetch_response` 在 kwargs 构造后注入 `api_base`/`api_key`（来自 model 对象的 override）；② deepseek 分支（两处）在非流式时 `pop("stream_options")` |

**关键机制链路**：`POST /sessions`(base_url/api_key) → `SessionState` → `apply_provider_to_agent` 给 model 挂 `_provider_base`/`_provider_key` → `Runner.run` → `_fetch_response` 注入 `api_base`/`api_key` 进 litellm kwargs → 覆盖 `.env` 全局配置。

---

## 6. 排障与修复记录

### 6.1 DeepSeek 一直加载中 + 服务器卡死（耗时最长的排障）

**症状**：用 `https://api.deepseek.com` + key 建会话发消息，前端永远 loading；随后 `fetch models`、health 全部超时（服务器卡死）。

**根因（三层叠加）**：

| 层 | 问题 | 表现 |
|---|---|---|
| 前端 | `normalizeModelId` 只给 claude 补前缀，deepseek 裸名 `deepseek-v4-flash` 没补 | litellm 报 `LLM Provider NOT provided` |
| 后端 API | `create_session` 端点**忘了透传** `base_url`/`api_key` 给 manager（schema 加了字段但端点没传） | override 从未挂到 model → 请求没带用户 key |
| 后端模型层 | DeepSeek 严格拒绝非流式下的 `stream_options` 参数 | 抛错 → 错误被误判为 401 governor |
| 后端 SSE | `sse_stream_via_hooks` 在异常时让流崩溃，无 `error` 事件 | 前端 `readSSE` 永远等 `final` → 无限 loading |

**定位过程**（关键手段）：
1. litellm 单独调用 deepseek 成功（带 key/base/前缀模型）→ 排除 DeepSeek 服务问题
2. 脚本里 `Runner.run` + `.env` 也成功 → 排除 CAI 模型层
3. 在 `_fetch_response` 加临时调试写文件 → 发现 `_provider_base=MISSING`（override 没挂上）
4. 对比 `create_session` 端点代码 → **发现端点没把 base_url/api_key 传给 manager**

**修复**：
- 前端 `normalizeModelId` 全面补前缀
- `create_session` 端点透传 base_url/api_key（**核心修复**）
- deepseek 分支 `pop("stream_options")`
- `sse_stream_via_hooks` 异常时发 `error` 事件
- 前端 `readSSE` 检测「流中断无 final」兜底报错

**验证**：DeepSeek 建会话 → 发消息 → `FINAL: 'DEEPSEEK_WORKS'`；mnapi 回归 `FINAL: 'OK'`。

### 6.2 mnapi 偶发超时（服务跑一段时间后）

**症状**：mnapi 建会话发消息，只出 `agent_switched` 就挂起；health 正常；重启服务后立即恢复正常。

**结论**：litellm 异步连接池在长跑后复用脏连接，非本项目改动引入。**重启服务可解**。后续可考虑请求级新连接或连接池清理。

### 6.3 对话框不能上下滚动

**根因**：`.chat-panel` 是 `.layout`（grid）子项，缺 `min-height: 0`。grid 子项默认 `min-height: auto`，内容多时撑开整个布局，`chat-log` 的 `flex: 1` + `overflow-y: auto` 失效，`body overflow: hidden` 裁掉溢出 → 页面和对话框都无法滚动。

**修复**：`.chat-panel` / `.sidebar` / `.trace-panel` 三个 grid 子项都加 `min-height: 0`。

**验证**（headless Chrome CDP 实测）：注入 30 条长消息后 `chatLog_canScroll: true`（clientH=183 vs scrollH=4527）。

### 6.4 其他已修复问题

- **agent 显示名 vs 内部名**：`/agents` 返回显示名（`Red Team Agent`），后端只认内部名（`redteam_agent`）。给 `AgentMetadataModel` 加 `id` 字段返回内部名，前端创建会话用它。
- **fetch models 留空 401**：UI 提示「留空用 .env」但后端没做回退。修复：`/api/v1/models/fetch` 在 base/key 留空时回退 `ANTHROPIC_API_BASE`/`OPENAI_API_BASE` + `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`。
- **webui 双份文件不同步**：Windows 工作区（源）+ WSL `~/cai/webui`（实际读取）。每次改动需手动同步。

---

## 7. 启动与使用

### 7.1 启动后端

```bash
cd ~/cai && uv run cai --api --api-port 8000
```

### 7.2 访问

浏览器打开 **http://127.0.0.1:8000**（WSL2 自动转发 localhost 到 Windows）。

### 7.3 使用流程

1. **NEW SESSION** → 选 Agent
2. 填 **BASE URL**（如 `https://api.deepseek.com`）+ **API KEY**
   - 留空 = 使用 `.env` 全局配置
3. 点 **⬆ FETCH MODELS** 拉取该 provider 的可用模型
4. 选 **MODEL** → CREATE
5. 中间面板输入指令（Ctrl+Enter 发送），右侧实时显示 trace
6. 运行中可点 **STOP** 取消

### 7.4 常用命令

- 关闭 Web UI：`CAI_WEBUI=0 uv run cai --api`
- 切换默认模型：改 `.env` 的 `CAI_MODEL`
- API 鉴权（可选）：设 `ALIAS_API_KEY`/`CAI_API_KEY` 后，前端右上角填 `X-CAI-API-Key`

---

## 8. 验证记录

### 8.1 环境
- Python 3.12.13，CAI `cai-framework` 1.1.5，`uv sync` 可编辑安装
- 端到端 `cai --prompt` 真实 agent 运行成功，日志落 `~/.cai/logs/`

### 8.2 Web UI
- 根路径 `/` 返回界面，静态资源 200
- `/api/*` 未被静态挂载遮挡
- 建会话 → 发消息 → SSE 流：`reasoning_step` + `final` 正常
- 历史回显、cancel 接口正常

### 8.3 自定义 provider
| 场景 | 结果 |
|---|---|
| DeepSeek 建会话发消息 | ✅ `DEEPSEEK_WORKS` |
| mnapi 建会话发消息 | ✅ `OK` |
| 默认（无 base/key） | ✅ 走 .env |
| 无效 base URL | ✅ 400 `ConnectError` |
| 错误 key | ✅ 401 + 中文提示 |
| 留空 base/key | ✅ 回退 .env |

---

## 9. 后续方向

- [ ] **token 级流式**：接 `/messages/stream_tokens` 更细粒度展示
- [ ] **命令面板**：`/commands` 执行、成本统计、历史搜索
- [ ] **多会话并行**：同时跑多个 agent 对比
- [ ] **provider 凭据持久化**：目前 key 仅存会话内存，可考虑加密存储
- [ ] **mnapi/litellm 连接池**：缓解长跑后偶发超时
- [ ] **一键同步脚本**：Windows 工作区 ↔ WSL 双份文件自动同步

---

*文档生成日期：2026-08-04*
