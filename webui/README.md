# CAI//OPS — Web UI（Neubrutalism）

CAI 框架的网页操作界面：对话 + 实时 Agent 推理 trace。

## 启动

```bash
cd ~/cai
uv run cai --api --api-port 8000
```

然后浏览器打开 **http://127.0.0.1:8000**（WSL2 会自动把 localhost 转发到 Windows）。

## 功能

- **会话管理**：左侧新建 / 切换 / 删除会话
- **创建流程**：选 Agent → 填 BASE URL + API Key → **FETCH MODELS**（从该 provider 拉取可用模型）→ 选模型 → CREATE
- **每会话独立 provider**：新建会话时可指定任意 OpenAI/Anthropic 兼容端点与密钥，不填则沿用 `.env` 全局配置
- **对话**：中间面板收发消息，Ctrl+Enter 发送
- **实时 trace**：右侧面板流式显示 Agent 的工具调用、工具输出、Agent 切换、最终消息
- **STOP**：任务运行时可取消（`POST /sessions/{id}/cancel`）
- **题库构建（Q BANK）**：顶部「⬡ Q BANK」→ 填题目 + 参考资料 + 选 agent/模型 → BUILD。系统让 CAI 真实复现解题过程，再基于真实过程总结整体思路 + 大步骤（目标/原因/原理/动作/结果），保存为题库条目
- **目录浏览**：侧栏可展开查看全部 Agent 及其工具、模型列表
- **API Key**：右上角可设置 `X-CAI-API-Key`（API 开启鉴权时使用）

## 依赖

- 前端：零构建 vanilla HTML/CSS/JS，无任何 npm/node 依赖
- 后端：`cai --api` 的 FastAPI 服务，静态文件由 `app.py` 的 `/` 挂载提供

## 结构

```
webui/
├── index.html      # 页面骨架
├── css/style.css   # Neubrutalism 设计系统
└── js/app.js       # API 封装 / SSE 流式解析 / 会话管理 / 渲染
```

## 设计规范

- 字体：标题 Space Grotesk 700-900，正文 IBM Plex Sans 400
- 配色：纯黑 `#000` / 强调橙 `#FF6B00` / 米白 `#FFFDF0`
- 3px 黑色实线边框，0 圆角，硬阴影 `8px 8px 0 #000`
- 不对称三栏布局；元素加载从下方弹入；按钮 hover 上移 2px；背景噪点纹理

## 常见问题

- **看不到 trace**：确认发消息时右侧面板可见；trace 仅在任务运行期间实时填充
- **创建会话报 500**：Agent 目录显示名与内部 id 不同，前端已自动使用内部 id 创建；若直接调 API，请用 `/api/v1/agents` 返回的 `id` 字段
- **FETCH MODELS 失败**：确认 BASE URL 是 OpenAI/Anthropic 兼容端点（`https://...`），且支持 `GET {base}/v1/models`；BASE URL / API KEY 留空会自动回退到 `.env` 全局配置
- **自定义 provider 发消息报错**：前端会自动给模型名补 provider 前缀（claude → `anthropic/`，deepseek → `deepseek/`，等），确保 litellm 能识别；若仍报 `LLM Provider NOT provided`，说明该 provider 的前缀没被识别，可在模型名手动补全
- **流式响应中断后按钮卡住**：后端现在会返回 `error` 事件、前端兜底检测，不会再无限 loading；若仍卡住请刷新页面
- **关闭 Web UI**：设 `CAI_WEBUI=0` 再启动 `cai --api`，则 `/` 不再挂载前端
