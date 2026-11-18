# 本地 Agent 协同工作台（Local Agent Hub v2.1）

打开控制台请使用项目启动器。无浏览器模式会打印当前用户专用的本地入口文件。升级前请先结束同一工作区的旧版后台服务；已安装的 Mac 应用需用更新后的源码重新构建并替换，才能使用新的登录方式。

一个运行在本机（macOS / Linux / Python 3.9+，**纯 Python 标准库零第三方依赖**）的多 Agent 跨模型协作工作台：支持**多 Agent 并行联动、真正跨模型协作、资料上传与本地目录挂载、人机交互审改（Human-in-the-Loop）、受控 macOS 桌面 UI 自动操控、实时状态跟踪与自动回收验收**，完整保留 v1.0–v2.0 全部功能与历史数据兼容。

> 📖 **新手上手请先看**：[完整中文使用说明（使用说明.md）](使用说明.md) —— 含零基础启动、各客户端接入、自动识别切模型、上传资料与人机审改、常见问答。

---

## 核心特性（v2.1 新增 + 完整保留 v1.0–v2.0 全部能力）

### 1. 告别“单向批处理”：完整资料输入与人机交互协作（v2.1 新增）
- **上传参考文件**：上传先保存在本机。开始任务或追加附件时，逐项勾选要共享的文件，并核对接收者、模型和接口地址。每次最多 50 个文件、合计 50 MiB，单文件不超过 20 MiB。
- **目录与文件快照**：工作台会过滤常见凭据及符号链接。请检查勾选文件中的个人信息。确认覆盖这些文件及其衍生成果，获准的 CLI、桌面端和共享 Worker 队列也可能连接云服务。
- **执行前人工审核与修改分工计划（`confirm_plan`）**：可勾选“执行前先暂停让我审核 / 修改分工计划”，总指挥拆解完成后自动暂停在 `awaiting_approval` 状态；用户可在页面直接调整每步负责的 Agent 和任务要求后点**批准执行**，或填意见**打回重规划**。
- **中途追加指令与补传资料（`inject_note`）**：任务执行或暂停期间可随时追加新指令或补传资料，自动注入后续尚未执行的步骤、整合阶段与复核阶段。
- **人工拍板与定向返工（`approve_draft` / `request_revision`）**：当达到自动返工上限或需要多轮打磨时，可一键**人工拍板采纳草稿交付**，或指定重跑某些步骤 / 仅修订汇总稿继续迭代；交付成果提供**网页一键直接下载**（`/api/download`）。
- **自动识别桌面端可用模型与自主切换（v2.1.1 新增）**：通过 [model_catalog.py](agent_hub/model_catalog.py) 无凭据自动扫描本机 Codex 官方模型缓存（`~/.codex/models_cache.json`）、DeepSeek Harness 官方内置 Schema（`dsh --profile headless --dump-config-schema`）与公开 profile 配置、以及各桌面端与 HTTP API `/models` 列表；在顶部「总指挥 Agent」、各「参与协作 Agent」及「Agent 卡片」均支持一键刷新并下拉自由切换模型（亦支持手动输入自定义模型 ID）。
- **Token 消耗追踪与统一数据目录**：自动采集 OpenAI / Anthropic / Gemini API 及 CLI 的 Token 用量并汇总展示；网页启动器（`launch.py`）与桌面应用默认共享同一份 `~/Library/Application Support/LocalAgentHub/workspace`（可用 `--local` 或 `AGENT_HUB_ROOT` 切换）。

### 2. 受控桌面 UI 自动操控（`desktop_ui`，v2.1 新增）
- **覆盖无 CLI/API 的纯桌面 AI 客户端**：通过原生编译的 macOS 辅助功能驱动 `DesktopDriver`（Cocoa + `ApplicationServices` 官方 API），可将 **ChatGPT 桌面版、Gemini 桌面版、WorkBuddy、DeepSeek Harness 桌面版、豆包桌面版**等接入自动派发与结果收回。
- **工程化安全护栏**：
  - 必须由用户显式授予 macOS `系统设置 → 隐私与安全性 → 辅助功能` 权限（`AXIsProcessTrusted`），绝不绕过系统权限；
  - 锁屏自动检测并中止；全局文件互斥锁 `desktop_ui.lock` 串行化前台操控，避免多任务并发抢焦点；
  - 自动保存与恢复剪贴板；使用唯一起止标记 `<<<HUB_OUT_...>>>...<<<HUB_END_...>>>` + 窗口变更回退 + `result.json` 回传；
  - **诚实标注边界**：桌面 UI 通道只能识别界面配置，不会伪造后端隐藏模型验证证据（标记为 `desktop_ui_unverified`）。

### 3. 多 Agent 并行联动与真正跨模型协作（v2.0 核心）
- **9 种传输通道统一调度**：`codex_cli`、`dsh_cli`（DeepSeek Harness 官方无头 CLI）、`claude_cli`、`gemini_cli`、`codebuddy_cli`、`custom_cli`、`http_api`（OpenAI / Claude / Gemini 原生 + 11 个服务商预设 + `/models` 模型同步）、`worker_bridge`（本机 Worker / MCP 队列）、`desktop_ui`（受控桌面 UI 操控）以及 `file_bridge`。
- **并发 DAG 与强制跨模型复核**：互不依赖步骤自动并发执行；审查阶段优先选择与总指挥和执行者不同模型/供应商的独立 Agent，支持强制跨模型开关与小体积文本成果自动预览。
- **实时追踪与自动回收**：`progress.json` 实时流式事件 + 后台 2 秒自动回收 Worker 队列和 `result.json` + 扩展 12 工具 MCP Server（默认保持 6 工具兼容）。

---

## 快速开始

### 方式 A：双击启动（macOS 推荐）
1. **原生 Mac 桌面应用**：运行或双击 `打包桌面应用.command` 构建 `macos-app/本地Agent工作台.app`（内置 `ClientProbe`、`DesktopDriver` 与 `WKUIDelegate` 文件选择/下载支持）。
2. **网页工作台**：双击目录中的 `启动工作台.command`，自动复用同工作区运行中的服务或顺延空闲端口（`8765–8785`）并打开浏览器。
3. **运行完整自检**：
   ```sh
   python3 -B -m unittest discover -s tests -v
   ```

### 方式 B：命令行使用

```sh
# 1. 扫描本机已安装客户端与 CLI
python3 -B -m agent_hub init

# 2. 一键启用 GPT / Codex 与 DeepSeek Harness 无头入口
python3 -B -m agent_hub setup-gpt
python3 -B -m agent_hub setup-dsh --use-dsh-credentials

# 3. 发起带附件资料且“执行前人工审计划”的多 Agent 跨模型协作
python3 -B -m agent_hub collaborate "基于附件生成系统升级方案" \
  --requirements "包含架构对比、风险与实施步骤" \
  --attach /path/to/spec.docx \
  --confirm-plan --cross-model --files

# 4. 审核通过分工计划 / 中途注入补充指令 / 人工拍板或返工
python3 -B -m agent_hub approve-plan <JOB_ID> --note "保留原指标口径"
python3 -B -m agent_hub inject-note <JOB_ID> --note "补充成本对比表"
python3 -B -m agent_hub approve-draft <JOB_ID> --note "人工验收通过"
python3 -B -m agent_hub request-revision <JOB_ID> --feedback "补充回滚方案"

# 5. 检测受控桌面 UI 自动操控所需的 macOS 辅助功能权限
python3 -B -m agent_hub check-accessibility

# 6. 启动本机自动 Worker / 扩展 MCP / 中文控制面板
python3 -B -m agent_hub worker --agent <WORKER_AGENT_ID> --exec "python3 your_connector.py"
python3 -B -m agent_hub mcp --extended
python3 -B -m agent_hub dashboard --port 8765
```

---

## 目录结构

```text
.
├── agent_hub/
│   ├── __init__.py          # 版本信息 (2.1.0)
│   ├── __main__.py          # CLI 入口
│   ├── core.py              # SQLite 状态存储、并发 DAG 调度、Token 汇总、自动回收、成果验收
│   ├── workflow.py          # 多 Agent 跨模型并行协作与 HITL 交互审改状态机
│   ├── attachments.py       # 安全文件上传、纯标准库 Office/文本提取与本地目录快照挂载
│   ├── desktop_ui.py        # 受控 macOS 桌面 UI 自动操控适配器（互斥锁 + 起止标记 + 权限检查）
│   ├── adapters.py          # 9 种执行传输适配器
│   ├── api_worker.py        # 隔离 HTTP 推理、Token 统计与 /models 模型同步
│   ├── worker_bridge.py     # 本机 Worker / MCP 任务队列、心跳与自动回传守护进程
│   ├── progress.py          # 原子实时进度追踪（progress.json）
│   ├── management.py        # Agent / API / CLI / 桌面 UI / 密钥隔离与回收站管理
│   ├── discovery.py         # 保守本机发现（绝不读取任何凭据/会话文件）
│   ├── desktop_clients.py   # macOS 桌面应用绑定与启动检查
│   ├── dashboard.py         # 本机 Web 控制面板、文件上传/下载 API、Worker Broker 与自动回收
│   ├── mcp.py               # MCP stdio Server（核心 6 工具 + --extended 12 工具）
│   └── plans.py             # 内置能力小测与交接演示计划
├── macos-app/               # macOS 原生窗口（Objective-C WKWebView）、ClientProbe 与 DesktopDriver 源码
├── tests/                   # 完整自动化测试套件
├── examples/                # 示例 DAG 任务计划
├── docs/                    # 架构、适配器、MCP 与验收文档
├── launch.py                # Finder 一键启动器（默认共享桌面端数据目录）
├── pyproject.toml           # 零依赖标准 Python 包配置
├── LICENSE                  # MIT 开源协议
└── .gitignore               # 严格忽略本地数据库、密钥、日志与构建产物
```

---

## 安全与透明边界
- **绝不窃取凭据或绕过系统权限**：本机发现不读取任何其他应用的密钥/数据库；桌面 UI 自动操控严格依赖 macOS 官方 `AXIsProcessTrusted` 授权。
- **密钥隔离保护**：API 密钥与本机 Worker 令牌分别保存在 `workspace/.api-secrets/`（`0700`/`0600`）和 `workspace/.worker-token`（`0600`），绝不写入任务日志、命令参数或前端页面。
- **附件共享授权**：服务端按文件内容和接收者配置核验授权，再为对应步骤准备输入。接收者或内容变化时需要重新确认。
- **模型证据诚实分层**：明确区分「真实运行回执」「配置模型」「用户确认」与「桌面 UI 未校验身份」。

---

## 开源协议
本项目采用 [MIT License](LICENSE) 开源。
