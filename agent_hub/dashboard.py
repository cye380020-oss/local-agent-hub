"""Loopback-only multi-agent control panel with interactive HITL workflow, attachments, desktop UI automation, worker broker API, and auto-collection."""
import base64
import hashlib
import hmac
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.parse import parse_qs, quote, urlsplit
import webbrowser

from . import __version__
from .adapters import _environment
from .attachments import approved_inputs, attach_job_inputs, review_inputs, review_saved_inputs, save_upload
from .console_access import check_for_legacy_dashboard, publish_access
from .core import collect_ready_results, dump, identifier, inside, retry_task
from .desktop_clients import installed_apps, open_and_check
from .desktop_ui import DESKTOP_UI_PROFILES, check_accessibility
from .discovery import discover
from .management import (
    PROVIDER_PRESETS,
    agent_deleted,
    job_deleted,
    save_agent,
    save_profile,
    setup_dsh,
    sync_api_models,
)
from .model_catalog import refresh_agent_models, select_agent_model
from .plans import handoff_plan, probe_plan
from .worker_bridge import (
    claim_task,
    complete_task,
    ensure_worker_token,
    poll_tasks,
    register_heartbeat,
    report_progress,
)
from .workflow import (
    AUTOMATED_TRANSPORTS,
    READY,
    approve_draft,
    approve_plan,
    create_flow,
    flow_recipients,
    inject_workflow_note,
    read_flow,
    request_revision,
    revise_plan,
    setup_gpt,
)


LOGIN = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>打开工作台</title>
<p id="msg">请使用项目启动器或终端显示的私有入口打开工作台。</p>
<script>
(async()=>{
  const key=location.hash.slice(1)||sessionStorage.getItem('hub-session');
  history.replaceState(null,'','/');
  if(!key)return;
  const r=await fetch('/',{headers:{'X-Agent-Hub-Token':key},cache:'no-store'});
  if(!r.ok){sessionStorage.removeItem('hub-session');return}
  const page=await r.text();
  document.open();document.write(page);document.close();
})().catch(()=>{document.getElementById('msg').textContent='连接失败，请重新打开启动器。'});
</script></html>"""


HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>本地 Agent 工作台 2.1</title>
<style>
*{box-sizing:border-box}
:root{--bg:#0b1220;--panel:#131d31;--panel2:#0e1626;--border:#253552;--text:#e8eef9;--muted:#9ab0ce;--accent:#7dd3a7;--accent-dark:#0d2318;--blue:#60a5fa;--warn:#fbbf24;--danger:#f87171}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
main{max-width:1260px;margin:auto;padding:28px 22px 48px}
h1{font-size:28px;margin:0;letter-spacing:-.3px}
h2{font-size:19px;margin:30px 0 12px;display:flex;align-items:center;gap:10px}
.sub{color:var(--muted);margin-top:2px}
header{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap;padding-bottom:18px;border-bottom:1px solid var(--border)}
.badges{display:flex;gap:8px;flex-wrap:wrap}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:12px;margin:14px 0}
.card,.job,.panel{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px}
.card b{font-size:16.5px}
.tag{display:inline-block;border-radius:999px;background:#1e3150;padding:2px 10px;font-size:12px;color:#b8d7ff;border:1px solid #2d466f}
.tag.ok{background:#123626;color:#8ef0bc;border-color:#1f5e42}
.tag.warn{background:#3a2b10;color:#fde08a;border-color:#694d18}
.tag.live{background:#162d4d;color:#7cc4ff;border-color:#2b578f}
.tag.err{background:#431d28;color:#ffb4c0;border-color:#742f42}
.model{margin:10px 0 4px;word-break:break-all;font-family:ui-monospace,SFMono-Regular,monospace;font-size:13px;color:#c9e1ff}
.note{color:var(--muted);font-size:12.5px}
button{cursor:pointer;background:var(--accent);color:var(--accent-dark);border:0;border-radius:8px;padding:8px 14px;font:inherit;font-weight:600;margin:4px 6px 4px 0;transition:opacity .15s}
button:hover{opacity:.9}
button.alt{background:#213452;color:#dbe8ff;font-weight:500}
button.danger{background:#4e2334;color:#ffe2e7;font-weight:500}
button:disabled{opacity:.4;cursor:default}
.job{margin:16px 0}
.jobhead{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;margin-top:8px}
td,th{text-align:left;border-bottom:1px solid var(--border);padding:9px 6px;vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--panel2);padding:14px;border-radius:10px;max-height:420px;overflow:auto;font-size:12.5px;border:1px solid #1d2b44}
textarea{width:100%;min-height:110px;background:var(--panel2);color:var(--text);border:1px solid #334a6b;border-radius:8px;padding:10px 12px;font:13px/1.6 ui-monospace,monospace}
details{margin:10px 0}
summary{cursor:pointer;font-weight:500}
#message{position:sticky;top:10px;background:#1e3a5f;border:1px solid #3d6ca8;border-radius:10px;padding:10px 14px;display:none;z-index:20;margin-bottom:12px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
a{color:var(--accent)}
.footer{font-size:12px;color:#7e93b0;margin-top:28px;border-top:1px solid var(--border);padding-top:16px}
input:not([type=checkbox]):not([type=file]),select{background:var(--panel2);color:var(--text);border:1px solid #334a6b;border-radius:7px;padding:8px 10px;font:inherit;max-width:100%}
#agent-editor label{display:block;margin:10px 0}
#agent-editor input:not([type=checkbox]):not([type=file]),#agent-editor select{display:block;width:100%;margin:4px 0}
.manage-row{padding:12px 0;border-bottom:1px solid var(--border)}
.manage-row:last-child{border-bottom:0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.pipeline{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.step-pill{font-size:12px;padding:4px 10px;border-radius:8px;background:var(--panel2);border:1px solid var(--border);color:var(--muted)}
.step-pill.done{background:#123626;border-color:#226546;color:#95f2c2}
.step-pill.active{background:#183358;border-color:#4285d6;color:#a9d4ff;font-weight:600}
.banner{padding:10px 12px;border-radius:10px;margin:10px 0;font-size:13px;border:1px solid var(--border);background:var(--panel2)}
.banner.ok{background:#0f2b1f;border-color:#1f5e42;color:#a5f3cc}
.banner.warn{background:#2d220d;border-color:#694d18;color:#fde68a}
.live-box{margin-top:6px;padding:8px 10px;border-radius:8px;background:var(--panel2);border:1px solid #223554;font-size:12px;color:#b8d7ff}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#4ade80;margin-right:5px;vertical-align:middle}
.pill-row{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.chk{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border:1px solid var(--border);padding:6px 10px;border-radius:8px;font-size:13px}
.dropzone{border:1.5px dashed #3a5680;border-radius:10px;padding:12px 14px;background:var(--panel2);margin:8px 0;transition:border-color .15s,background .15s}
.dropzone.over{border-color:var(--accent);background:#12243b}
.chip{display:inline-flex;align-items:center;gap:6px;background:#1a2c48;border:1px solid #2d466f;border-radius:999px;padding:3px 10px;font-size:12px;margin:3px 4px 3px 0}
.chip button{padding:0 5px;margin:0;background:transparent;color:#ffb4c0;font-size:13px;font-weight:700}
.hitl-box{background:#101c31;border:1px solid #35588a;border-radius:12px;padding:14px;margin:12px 0}
.step-edit{background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin:8px 0}
.dl-links{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.dl-links a{display:inline-block;background:#1a2f4d;border:1px solid #2f5182;color:#b7e3ff;text-decoration:none;border-radius:8px;padding:5px 10px;font-size:12.5px}
.dl-links a:hover{border-color:var(--accent);color:var(--accent)}
.mini-sel{padding:4px 8px!important;font-size:12.5px!important;border-radius:6px!important;max-width:240px}
.card-model-sel{width:100%;margin:6px 0 4px;padding:6px 8px!important;font-size:12.5px!important}
.part-item{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;background:var(--panel2);border:1px solid var(--border);padding:6px 10px;border-radius:10px;font-size:13px}
@media(max-width:700px){header,.jobhead{display:block}td,th{padding:8px 3px;font-size:12px}}
</style>
<main>
<div id="message" role="status"></div>
<header>
  <div>
    <h1>本地 Agent 协同工作台 <span class="tag ok">v__VERSION__</span></h1>
    <div class="sub">多 Agent 并行联动 · 跨模型协作 · 资料上传与交互审改 · 受控桌面 UI 自动操控</div>
  </div>
  <div class="badges">
    <span class="tag">仅本机 127.0.0.1</span>
    <span class="tag live" id="live-indicator">自动监听中</span>
  </div>
</header>

<h2>多 Agent 跨模型协同任务</h2>
<div class="panel">
  <p class="note">支持上传参考文件（含 Word/Excel/PPT/Markdown/代码）、挂载本地目录快照，并可开启“执行前人工确认分工”以及中途追加指令、人工拍板或定向返工。</p>
  <label for="flow-goal">任务目标</label>
  <textarea id="flow-goal" aria-label="协作任务目标" placeholder="说明你要得到什么成果，以及已知背景、限制与交付格式"></textarea>
  <label for="flow-requirements">验收要求</label>
  <textarea id="flow-requirements" aria-label="协作验收要求" style="min-height:70px" placeholder="例如：结论清晰、包含可执行步骤、结合已上传资料核实数据、不得编造"></textarea>

  <div class="dropzone" id="upload-dropzone" ondragover="event.preventDefault();this.classList.add('over')" ondragleave="this.classList.remove('over')" ondrop="handleDrop(event)">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <div><b>📎 输入资料与附件</b> <span class="note">支持拖拽或选择文件（.docx / .xlsx / .pptx / .md / .txt / .json / .csv / 代码等，单文件上限 20 MiB；发送前逐项确认文件和接收者）</span></div>
      <input type="file" id="flow-file-input" multiple onchange="handleFileSelect(this.files)">
    </div>
    <div id="staged-uploads" style="margin-top:6px"></div>
    <details style="margin-top:6px">
      <summary class="note">或挂载本机已有文件 / 文件夹只读快照（每行一个绝对路径，预览实际清单后勾选文件，共享前请核对内容）</summary>
      <textarea id="flow-local-paths" style="min-height:56px;margin-top:6px" placeholder="/Users/yourname/Documents/project-spec&#10;/Users/yourname/Desktop/requirements.docx"></textarea>
    </details>
  </div>

  <div class="grid2" style="margin-top:10px">
    <div>
      <label>总指挥 Agent <select id="flow-commander" aria-label="指挥 Agent" onchange="renderCommanderModelSelect();updateCrossModelBanner()"></select></label>
      <div style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <label class="note" style="margin:0">总指挥使用模型：<select id="flow-commander-model" class="mini-sel" data-model-select="commander" onchange="switchCommanderModel(this)"></select></label>
        <button type="button" class="alt" style="padding:5px 10px;font-size:12.5px" onclick="act('refresh_models')">🔄 识别并更新桌面端模型</button>
      </div>
    </div>
    <div><div id="cross-model-banner" class="banner">正在检测跨模型配置…</div></div>
  </div>
  <div id="flow-participants"></div>
  <p class="note" id="flow-unavailable"></p>
  <div class="pill-row">
    <label class="chk"><input type="checkbox" id="flow-confirm-plan">执行前先暂停让我审核 / 修改分工计划（人机协同）</label>
    <label class="chk"><input type="checkbox" id="flow-parallel" checked>允许独立子步骤并行执行</label>
    <label class="chk"><input type="checkbox" id="flow-cross-model">强制跨模型协作与复核</label>
    <label class="chk"><input type="checkbox" id="flow-files">允许在本任务工作区生成文件</label>
  </div>
  <details>
    <summary>工作量与并发限制</summary>
    <div style="margin-top:8px">
      <label>最多执行步骤 <input id="flow-steps" type="number" min="1" max="6" value="3" style="width:76px"></label>
      <label style="margin-left:10px">最多返工轮次 <input id="flow-revisions" type="number" min="0" max="2" value="1" style="width:76px"></label>
      <label style="margin-left:10px">最多模型调用 <input id="flow-calls" type="number" min="4" max="32" value="16" style="width:76px"></label>
      <label style="margin-left:10px">最大并行数 <input id="flow-concurrency" type="number" min="1" max="4" value="3" style="width:76px"></label>
    </div>
    <p class="note">每次调用最长 5 分钟。支持 CLI（Codex / DeepSeek Harness / Claude / Gemini / CodeBuddy / 自定义脚本）、多协议 API（OpenAI / Claude / Gemini）、Worker / MCP 桥接以及受控 macOS 桌面 UI 自动操控。</p>
  </details>
  <div style="margin-top:10px">
    <button id="flow-start" onclick="startFlow()">开始多 Agent 自动协作</button>
    <button class="alt" onclick="act('setup_gpt')">启用 GPT / Codex 入口</button>
    <button class="alt" onclick="setupDshPrompt()">启用 DeepSeek Harness 无头入口</button>
    <button class="alt" onclick="act('collect_external')">立即扫描回收外部成果</button>
  </div>
  <p class="note">提示：若勾选了至少两个不同模型或供应商，系统会自动进行跨模型分工与独立跨模型复核，并在交付清单中给出真实模型回执核验与 Token 统计。</p>
</div>

<h2>任务进展、交互审改与交付</h2>
<div id="jobs"></div>

<h2>Agent 与跨模型状态</h2>
<div class="cards" id="agents"></div>
<div>
  <button onclick="act('refresh_models')" class="alt">🔄 自动识别并更新全部桌面端可用模型</button>
  <button onclick="act('discover')" class="alt">重新检查本机客户端与 CLI</button>
  <button onclick="act('probe')">运行 Codex 能力小测</button>
  <button onclick="act('handoff')" class="alt">创建 ChatGPT 交接演示</button>
</div>
<p class="note">能力小测会使用现有客户端额度。</p>

<h2>Agent、CLI、API、Worker 与桌面 UI 自动操控管理</h2>
<details class="panel" id="agent-editor">
  <summary>添加 / 编辑 Agent（支持跨模型 API、无头 CLI、自定义脚本、自动 Worker / MCP 桥接、受控桌面 UI 自动操控与文件交接）</summary>
  <input id="agent-id" type="hidden">
  <label>名称<input id="agent-name" aria-label="Agent 名称" placeholder="例如：DeepSeek 推理、Gemini 桌面版、Kimi 审校、WorkBuddy"></label>
  <label>接入方式
    <select id="agent-transport" onchange="toggleTransport()" aria-label="接入方式">
      <option value="http_api">多模型 API（OpenAI 兼容 / Claude / Google Gemini）</option>
      <option value="dsh_cli">DeepSeek Harness 官方无头 CLI（dsh --profile headless）</option>
      <option value="claude_cli">Claude Code 无头 CLI（claude -p）</option>
      <option value="gemini_cli">Gemini CLI 无头入口（gemini -p）</option>
      <option value="codebuddy_cli">CodeBuddy / WorkBuddy CLI 无头入口</option>
      <option value="custom_cli">自定义本机 CLI / 连接脚本（JSON stdin/stdout）</option>
      <option value="worker_bridge">外部自动 Worker / MCP 桥接（自动发任务 · 实时上报 · 自动回收）</option>
      <option value="desktop_ui">受控桌面 UI 自动操控（macOS 辅助功能 · ChatGPT / Gemini / WorkBuddy / 豆包等）</option>
      <option value="file_bridge">其他桌面客户端（文件交接 + 自动监听 result.json）</option>
    </select>
  </label>

  <div id="desktop-fields" hidden>
    <label>绑定本机应用<select id="agent-app" aria-label="绑定本机应用"><option value="">按名称自动匹配</option></select></label>
    <p class="note">保存后可点击“打开并检查”。执行任务时也会自动监听对应目录中的 result.json 实现自动回收。</p>
  </div>

  <div id="desktop-ui-fields" hidden>
    <div id="ax-status-banner" class="banner">正在检测 macOS 辅助功能权限…</div>
    <div style="margin-bottom:8px">
      <button type="button" class="alt" onclick="refreshAccessibility(false)">重新检测辅助功能权限</button>
      <button type="button" class="alt" onclick="refreshAccessibility(true)">弹出系统授权提示（系统设置 → 隐私与安全性 → 辅助功能）</button>
    </div>
    <label>快捷桌面客户端预设<select id="desktop-ui-preset" onchange="applyDesktopUiPreset()"><option value="">-- 选择常见桌面 AI 客户端一键填充 --</option></select></label>
    <label>绑定本机 Mac 应用<select id="agent-dui-app" aria-label="桌面 UI 绑定应用"><option value="">请选择已安装的 .app</option></select></label>
    <label>界面所用模型备注（供分工参考；桌面操控无法从后端校验隐藏模型）<input id="agent-dui-model" placeholder="例如：Gemini 2.5 Pro / 豆包 / WorkBuddy Kimi"></label>
    <div class="grid2">
      <label>新建对话快捷键
        <select id="agent-dui-new">
          <option value="cmd+n">Cmd + N（ChatGPT / Gemini / WorkBuddy / DeepSeek）</option>
          <option value="cmd+k">Cmd + K（豆包等）</option>
          <option value="cmd+shift+n">Cmd + Shift + N</option>
          <option value="cmd+l">Cmd + L</option>
          <option value="none">不自动新建，直接在当前对话输入</option>
        </select>
      </label>
      <label>发送快捷键
        <select id="agent-dui-send">
          <option value="return">Return（回车发送）</option>
          <option value="cmd+return">Cmd + Return</option>
        </select>
      </label>
    </div>
    <label>回复稳定判定秒数（2–15 秒，停止变化后自动抓取完整回复）<input id="agent-dui-settle" type="number" min="2" max="15" step="0.5" value="4"></label>
    <label><input id="agent-dui-files" type="checkbox"> 允许该桌面客户端读写任务隔离工作区文件（如带本地文件工具的客户端）</label>
    <label><input id="agent-dui-auto" type="checkbox" checked> 允许参与多 Agent 自动协作</label>
    <p class="note">诚实边界说明：受控桌面 UI 自动化通过 macOS 官方辅助功能 API（AXUIElement）执行，必须由你手动授权“辅助功能”且保持电脑未锁屏；执行期间会通过全局互斥锁串行占用前台窗口（不会并发抢鼠标键盘），并自动保存与恢复剪贴板。</p>
  </div>

  <div id="api-fields">
    <label>快捷服务商预设<select id="api-preset" onchange="applyPreset()"><option value="">-- 选择常见服务商一键填充 --</option></select></label>
    <label>协议
      <select id="agent-protocol" aria-label="API 协议">
        <option value="openai">OpenAI 兼容 Chat Completions（DeepSeek / OpenAI / Kimi / GLM / 百炼 / 豆包 / Ollama）</option>
        <option value="gemini">Google Gemini 原生 API（generateContent）</option>
        <option value="anthropic">Anthropic Claude Messages</option>
      </select>
    </label>
    <label>API 地址<input id="agent-endpoint" aria-label="API 地址" placeholder="https://服务域名/v1（也可填写完整接口地址；本机 Ollama 可填 http://127.0.0.1:11434/v1）"></label>
    <label>模型标识<input id="agent-model" aria-label="API 模型" placeholder="填写服务商提供的准确模型 ID（保存后可点“同步可用模型”自动获取）"></label>
    <label id="synced-model-wrap" hidden>从已同步列表快速选择<select id="synced-model-select" onchange="if(this.value)document.getElementById('agent-model').value=this.value"></select></label>
    <label>API 密钥<input id="agent-key" type="password" autocomplete="new-password" aria-label="API 密钥" placeholder="新建时填写；编辑留空则保留原密钥；本地 Ollama 可留空"></label>
    <label><input id="clear-key" type="checkbox"> 清除已保存密钥（本地无密钥服务可用）</label>
    <label><input id="auto-route" type="checkbox"> 允许自动分配任务到此 API（可能产生费用）</label>
    <p class="note">API 密钥保存在仅当前用户可读写的 0600 文件中，绝不回显到页面、命令参数或任务日志。API 模式支持自动附带已上传资料与成果文件的文本预览。</p>
  </div>

  <div id="cli-fields" hidden>
    <label>可执行文件绝对路径<input id="agent-executable" placeholder="/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"></label>
    <label>指定模型标识（可选）<input id="agent-cli-model" placeholder="如 deepseek-flash、deepseek-v4.1-flash-1017、claude-sonnet-4-5"></label>
    <div id="dsh-extra" hidden>
      <label>DeepSeek Harness Provider<input id="agent-dsh-provider" value="deepseek-official" placeholder="deepseek-official 或 bailian-dashscope"></label>
      <label><input id="agent-dsh-creds" type="checkbox"> 复用 DeepSeek Harness 本机已登录账号 / 凭据插件（通过 --patch 启用）</label>
      <label>或独立提供 API Key（可选，留空保留原密钥）<input id="agent-dsh-key" type="password" autocomplete="new-password" placeholder="仅注入该次 dsh 进程环境，不落盘日志"></label>
      <label>注入环境变量名<input id="agent-dsh-key-env" value="DEEPSEEK_API_KEY" placeholder="DEEPSEEK_API_KEY"></label>
    </div>
    <div id="custom-cli-extra" hidden>
      <label>附加启动参数（空格分隔，可选）<input id="agent-cli-args" placeholder="例如 --fast"></label>
      <label><input id="agent-cli-allow-files" type="checkbox" checked> 允许此脚本在任务隔离工作区内读写文件</label>
    </div>
    <label><input id="cli-auto-route" type="checkbox" checked> 允许参与多 Agent 自动协作</label>
  </div>

  <div id="worker-fields" hidden>
    <label>声明使用的模型（如 Kimi-K3 / Gemini-4-Argon / 豆包）<input id="agent-worker-model" placeholder="用于任务规划参考，实际以 Worker 回执为准"></label>
    <label>在线等待响应窗口（秒，0 表示发现在线 Worker 自动等待任务超时）<input id="agent-worker-wait" type="number" min="0" max="600" value="0"></label>
    <label><input id="agent-worker-files" type="checkbox" checked> 允许 Worker 读写隔离工作区文件</label>
    <label><input id="agent-worker-auto" type="checkbox" checked> 允许参与多 Agent 自动协作</label>
    <p class="note">保存后可点击“查看接入命令”，启动本机 Worker 进程或用支持 MCP 的客户端（如 Antigravity / Claude Desktop / 自定义脚本）连接，自动接收派单、上报状态并回传成果。</p>
  </div>

  <label>擅长领域 / 能力备注（供总指挥跨模型分工参考）<input id="agent-strengths" placeholder="例如：擅长中文长文撰写与逻辑审校 / 擅长 Python 与复杂排程"></label>
  <button onclick="saveAgent()">保存 Agent</button>
  <button class="alt" onclick="resetAgent()">清空表单 / 新建</button>
  <div id="agent-save-status" role="status" class="note" style="margin-top:6px"></div>
</details>
<div class="panel" id="manage-agents"></div>

<details class="panel">
  <summary>固定模型与能力备注</summary>
  <p class="note">桌面客户端的模型在此登记为“用户确认”，不会自动切换客户端。CLI 的准确模型 ID 会用于后续任务；API 模型请在其配置中修改。能力备注不是实测成绩。</p>
  <label>Agent <select id="profile-agent" aria-label="固定模型 Agent" onchange="loadProfile()"></select></label>
  <p><label>固定模型 <input id="profile-model" aria-label="固定模型名称"></label></p>
  <label>能力备注<textarea id="profile-strengths" aria-label="能力备注" style="min-height:70px"></textarea></label>
  <button onclick="act('save_profile',{agent:document.getElementById('profile-agent').value,model:document.getElementById('profile-model').value,strengths:document.getElementById('profile-strengths').value})">保存模型档案</button>
</details>

<details class="panel">
  <summary>高级：直接交给单个 Agent</summary>
  <div>
    <label for="goal">要完成什么？</label>
    <textarea id="goal" aria-label="任务目标" placeholder="例如：为我的项目写一份交接清单"></textarea>
    <label for="requirements">成果中必须出现的内容（每行一条）</label>
    <textarea id="requirements" aria-label="验收内容" style="min-height:80px" placeholder="目标&#10;成果&#10;验收"></textarea>
    <label for="client">交给谁： </label>
    <select id="client" aria-label="选择 Agent">
      <option value="auto">自动选择已接通 Agent</option>
      <option value="chatgpt">ChatGPT（文件交接）</option>
      <option value="codex">Codex</option>
      <option value="workbuddy">WorkBuddy（文件交接）</option>
      <option value="deepseek">DeepSeek Harness</option>
      <option value="doubao">豆包（文件交接）</option>
    </select>
    <button onclick="createTask()">开始任务</button>
    <p class="note">此快捷入口生成文本成果，按你填写的内容逐项检查；若顶部已上传附件，可一并附带。复杂步骤、并行多 Agent 和独立审查请使用顶部协作或下方 JSON 任务计划。</p>
  </div>
</details>

<details class="panel">
  <summary>高级：提交任务计划（DAG JSON）</summary>
  <p class="note">粘贴可信的 JSON 计划。独立无依赖任务会自动并行调度；review_of 会自动优先选择跨模型审查者。</p>
  <textarea id="plan" aria-label="任务计划 JSON"></textarea>
  <button onclick="submitPlan()">提交并开始</button>
</details>

<details class="panel">
  <summary>导入外部客户端成果</summary>
  <p class="note">除手动粘贴外，把填好的 result.json 放入任务目录也会在 2 秒内自动回收验收。模型自报名称不会作为身份验证证据。</p>
  <textarea id="import" aria-label="外部成果 JSON">{"job":"任务组标识","task":"任务标识","result":{"output":"真实回复","artifacts":[]}}</textarea>
  <button onclick="importResult()">导入并验收</button>
</details>

<details class="panel">
  <summary>回收站（已删除的 Agent 和任务）</summary>
  <p class="note">删除仅移出使用列表，记录与成果保留，可在这里恢复。恢复任务后保持暂停，需主动继续执行。</p>
  <div id="trash"></div>
</details>

<h2>已验证表现</h2>
<div id="measurements" class="panel"></div>

<details class="panel">
  <summary>最近系统与联动事件</summary>
  <pre id="events"></pre>
</details>

<p class="footer">本地 Agent 协同工作台 v__VERSION__ · 多 Agent 并行联动 · 人机交互审改 · 资料上传挂载 · 受控桌面 UI 自动操控 · 仅绑定本机 · 数据目录：__ROOT__</p>
</main>

<script>
const token=__TOKEN__;
sessionStorage.setItem('hub-session',token);
function hubFetch(path, options={}){
  return fetch(path,{...options,headers:{...options.headers,'X-Agent-Hub-Token':token}});
}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={pending:'待执行',running:'执行中',done:'已验收',waiting_external:'等待外部/Worker',awaiting_approval:'待确认分工',paused:'已暂停',failed:'失败',blocked:'等待可用 Agent',needs_attention:'需要处理'};
const transportLabels={codex_cli:'Codex CLI',dsh_cli:'DeepSeek Harness CLI',claude_cli:'Claude CLI',gemini_cli:'Gemini CLI',codebuddy_cli:'CodeBuddy CLI',custom_cli:'自定义 CLI',http_api:'HTTP API',worker_bridge:'Worker/MCP 桥接',desktop_ui:'桌面 UI 自动操控',file_bridge:'文件交接'};
const phases={planning:'分析与分工',awaiting_approval:'人工确认分工',working:'多 Agent 执行',synthesizing:'整合成果',reviewing:'独立复核',delivering:'整理交付',complete:'已完成交付'};
let knownAgents=[], presets=[], desktopUiProfiles={}, stagedUploads=[], flowChoices=new Map(), flowCatalogSignature='', profileSignature='', pollTimer=null, jobUploadMap={}, autoRefreshedModelsOnce=false;

function msg(t){const e=document.getElementById('message');e.textContent=t;e.style.display='block';clearTimeout(e._t);e._t=setTimeout(()=>e.style.display='none',9000)}

async function chooseAttachments(review){
  const dialog=document.createElement('dialog');
  const labels=review.recipients.map(r=>[r.name+' ('+r.id+')',r.transport,r.destination,r.provider,r.model,
    r.transport==='worker_bridge'?'共享 Worker/MCP 队列':''].filter(Boolean).join(' · '));
  dialog.innerHTML='<form method="dialog"><h3>确认附件接收者</h3><p>所选文件的内容和衍生成果可发送给以下接收者。客户端也可能调用其配置的云服务。</p><ul>'+
    labels.map(label=>'<li>'+esc(label)+'</li>').join('')+'</ul><fieldset><legend>勾选允许共享的文件</legend>'+
    Object.keys(review.files).map((path,i)=>'<label style="display:block"><input type="checkbox" value="'+i+'"> '+esc(path)+'</label>').join('')+
    '</fieldset><p><button value="cancel" autofocus>取消</button> <button value="share" disabled>确认共享</button></p></form>';
  dialog.style.maxHeight='80vh'; dialog.style.maxWidth='min(90vw,48em)';
  const boxes=[...dialog.querySelectorAll('input')], share=dialog.querySelector('[value="share"]');
  dialog.onchange=()=>{share.disabled=!boxes.some(box=>box.checked)};
  document.body.appendChild(dialog);
  const closed=new Promise(resolve=>{dialog.onclose=resolve});
  if(typeof dialog.showModal==='function'){
    dialog.showModal();
  }else{
    dialog.setAttribute('open','');
    Object.assign(dialog.style,{position:'fixed',top:'10vh',zIndex:'9999',overflow:'auto',background:'white'});
    dialog.querySelector('form').onsubmit=e=>{
      e.preventDefault();
      dialog.returnValue=e.submitter?.value||document.activeElement?.value||'cancel';
      dialog.onclose();
    };
  }
  await closed;
  const selected=Object.entries(review.files).filter((_,i)=>boxes[i].checked);
  dialog.remove();
  return dialog.returnValue==='share'&&selected.length
    ? {files:Object.fromEntries(selected),recipients:review.recipients} : null;
}

async function act(action,data={}){
  try{
    if(action==='approve_saved_inputs'||data.upload_ids?.length||data.local_paths?.length){
      const response=await hubFetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...data,action:'preview_attachments',operation:action})});
      const review=await response.json();
      if(!response.ok)throw Error(review.error);
      data.attachment_approval=await chooseAttachments(review);
      if(!data.attachment_approval)return false;
    }
    const r=await hubFetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Hub-Token':token},body:JSON.stringify({action,...data})});
    const v=await r.json();
    if(!r.ok)throw Error(v.error);
    msg(v.message||'操作已完成');
    if(v.worker_command){
      alert('本机 Worker / MCP 接入说明：\n\n'+v.worker_command);
    }
    await refresh(true);
    return v;
  }catch(e){msg(e.message);return false}
}

function arrayBufferToBase64(buf){
  const bytes=new Uint8Array(buf);
  let bin='';
  const chunk=0x8000;
  for(let i=0;i<bytes.length;i+=chunk){
    bin+=String.fromCharCode.apply(null,bytes.subarray(i,i+chunk));
  }
  return btoa(bin);
}

async function uploadOneFile(file){
  if(file.size>20*1024*1024)throw Error(`文件 ${file.name} 超过 20 MiB 上限`);
  const buf=await file.arrayBuffer();
  const r=await hubFetch('/api/upload',{
    method:'POST',
    headers:{'Content-Type':'application/json','X-Agent-Hub-Token':token},
    body:JSON.stringify({filename:file.name,content_base64:arrayBufferToBase64(buf)})
  });
  const v=await r.json();
  if(!r.ok)throw Error(v.error||'上传失败');
  return v;
}

function renderStagedUploads(){
  const el=document.getElementById('staged-uploads');
  if(!stagedUploads.length){el.innerHTML='';return}
  el.innerHTML=stagedUploads.map((u,i)=>`<span class="chip">📄 ${esc(u.name)} (${Math.max(1,Math.round(u.bytes/1024))} KB${u.has_text_preview?' · 已提文本':''}) <button type="button" onclick="stagedUploads.splice(${i},1);renderStagedUploads()" title="移除">×</button></span>`).join('');
}

async function handleFileSelect(files){
  if(!files||!files.length)return;
  for(const f of files){
    try{
      msg('正在上传并解析：'+f.name+' …');
      const meta=await uploadOneFile(f);
      stagedUploads.push(meta);
      renderStagedUploads();
      msg(`已上传 ${meta.name}${meta.has_text_preview?'（已自动提取文本预览）':''}`);
    }catch(e){msg(e.message)}
  }
  document.getElementById('flow-file-input').value='';
}

function handleDrop(ev){
  ev.preventDefault();
  document.getElementById('upload-dropzone').classList.remove('over');
  if(ev.dataTransfer?.files?.length)handleFileSelect(ev.dataTransfer.files);
}

async function handleJobExtraUpload(jobId, files){
  if(!files||!files.length)return;
  if(!jobUploadMap[jobId])jobUploadMap[jobId]=[];
  for(const f of files){
    try{
      const meta=await uploadOneFile(f);
      jobUploadMap[jobId].push(meta);
      const wrap=document.getElementById('job-up-'+jobId);
      if(wrap){
        wrap.innerHTML=jobUploadMap[jobId].map(u=>`<span class="chip">📎 ${esc(u.name)}</span>`).join('');
      }
      msg('已暂存补充资料：'+meta.name+'，点击“注入指令/资料”生效');
    }catch(e){msg(e.message)}
  }
}

async function loadPresets(){
  try{
    const r=await hubFetch('/api/presets');
    presets=await r.json();
    const sel=document.getElementById('api-preset');
    sel.innerHTML='<option value="">-- 选择常见服务商一键填充 --</option>'+presets.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}（${esc(p.model)}）</option>`).join('');
  }catch(e){}
}

async function loadDesktopUiStatus(){
  try{
    const r=await hubFetch('/api/desktop-ui-status');
    const data=await r.json();
    desktopUiProfiles=data.profiles||{};
    const sel=document.getElementById('desktop-ui-preset');
    sel.innerHTML='<option value="">-- 选择常见桌面 AI 客户端一键填充 --</option>'+Object.entries(desktopUiProfiles).map(([k,p])=>`<option value="${esc(k)}">${esc(p.label)}（${esc(p.new_chat_shortcut)} / ${esc(p.send_shortcut)}）</option>`).join('');
    renderAxBanner(data.accessibility);
  }catch(e){}
}

function renderAxBanner(ax){
  const el=document.getElementById('ax-status-banner');
  if(!el)return;
  if(ax?.trusted&&!ax?.screen_locked){
    el.className='banner ok';
    el.textContent='✅ macOS 辅助功能权限已授权，屏幕已解锁，可使用受控桌面 UI 自动操控。';
  }else if(ax?.screen_locked){
    el.className='banner warn';
    el.textContent='⚠️ 检测到锁屏状态；请解锁后再运行桌面 UI 自动操控。';
  }else{
    el.className='banner warn';
    el.textContent='⚠️ 尚未获得 macOS 辅助功能权限（AXIsProcessTrusted=false）。请点下方按钮弹出系统提示，并在“系统设置 → 隐私与安全性 → 辅助功能”中勾选“本地 Agent 工作台”或终端。'+(ax?.error?'（'+ax.error+'）':'');
  }
}

async function refreshAccessibility(prompt){
  const res=await act(prompt?'prompt_accessibility':'check_accessibility');
  if(res&&res.accessibility)renderAxBanner(res.accessibility);
}

function applyDesktopUiPreset(){
  const key=document.getElementById('desktop-ui-preset').value;
  const p=desktopUiProfiles[key];
  if(!p)return;
  if(!document.getElementById('agent-name').value)document.getElementById('agent-name').value=p.label+'（自动操控）';
  const appSel=document.getElementById('agent-dui-app');
  if([...appSel.options].some(o=>o.value===p.app_path))appSel.value=p.app_path;
  else appSel.dataset.pending=p.app_path;
  document.getElementById('agent-dui-new').value=p.new_chat_shortcut||'cmd+n';
  document.getElementById('agent-dui-send').value=p.send_shortcut||'return';
}

function applyPreset(){
  const id=document.getElementById('api-preset').value;
  const p=presets.find(x=>x.id===id);
  if(!p)return;
  if(!document.getElementById('agent-name').value)document.getElementById('agent-name').value=p.label.split('（')[0];
  document.getElementById('agent-protocol').value=p.protocol;
  document.getElementById('agent-endpoint').value=p.endpoint;
  document.getElementById('agent-model').value=p.model;
  document.getElementById('auto-route').checked=true;
}

function canRun(a){
  const t=a.transport;
  const automated=['codex_cli','dsh_cli','claude_cli','gemini_cli','codebuddy_cli','custom_cli','http_api','worker_bridge','desktop_ui'];
  if(!automated.includes(t))return false;
  if(t==='dsh_cli'&&!a.dsh_enabled)return false;
  if(t==='desktop_ui'&&!a.desktop_ui_enabled)return false;
  if(!['verified','available','cli_available','ready'].includes(a.automation?.status))return false;
  if(t==='http_api'&&!a.auto_route)return false;
  return true;
}

function modelOf(a){
  if(a.execution_model)return a.execution_model;
  if(a.model?.source==='user_selected'&&a.model?.value)return a.model.value;
  return a.observed_model?.value||a.model?.value||'';
}

function modelOptionsHtml(a){
  const cur=modelOf(a);
  const list=Array.isArray(a.available_models)?[...a.available_models]:[];
  if(cur&&!list.some(m=>m.id===cur)){
    list.unshift({id:cur,label:cur,provider:a.dsh_provider||'',source:'user_selected',runnable_directly:true});
  }
  const opts=list.map(m=>{
    let text=m.id;
    if(m.label&&m.label!==m.id)text+=`（${m.label}）`;
    if(m.runnable_directly===false)text+=` [${m.provider||'仅桌面配置'}]`;
    else if(m.source==='reference_catalog')text+=' [参考]';
    else if(m.source==='desktop_ui_scan')text+=' [窗口识别]';
    return `<option value="${esc(m.id)}" data-provider="${esc(m.provider||'')}" ${m.id===cur?'selected':''}>${esc(text)}</option>`;
  });
  if(!opts.length){
    opts.push('<option value="" selected>未识别（点下方手动输入或刷新）</option>');
  }
  opts.push('<option value="__custom__">＋ 手动输入其他模型 ID…</option>');
  return opts.join('');
}

async function switchAgentModel(agentId, selEl){
  const a=knownAgents.find(x=>x.id===agentId);
  const prev=a?modelOf(a):'';
  let chosen=selEl.value;
  let provider=selEl.selectedOptions?.[0]?.dataset?.provider||'';
  if(chosen==='__custom__'){
    const manual=prompt(`请输入「${a?.name||agentId}」要使用的模型 ID（如 gpt-6.2-sol、deepseek-v4-pro、gemini-2.5-pro）：`, prev);
    if(!manual||!manual.trim()){
      selEl.value=prev;
      return;
    }
    chosen=manual.trim();
    provider='';
  }
  if(!chosen||chosen===prev)return;
  const ok=await act('select_model',{agent:agentId,model:chosen,provider:provider||undefined});
  if(!ok){
    selEl.value=prev;
  }
}

function renderCommanderModelSelect(){
  const sel=document.getElementById('flow-commander-model');
  const cmdId=document.getElementById('flow-commander')?.value;
  const a=knownAgents.find(x=>x.id===cmdId);
  if(!sel)return;
  if(!a){
    sel.innerHTML='<option value="">无可用指挥</option>';
    sel.disabled=true;
    return;
  }
  sel.disabled=false;
  sel.innerHTML=modelOptionsHtml(a);
}

async function switchCommanderModel(selEl){
  const cmdId=document.getElementById('flow-commander')?.value;
  if(cmdId)await switchAgentModel(cmdId, selEl);
}
function providerOf(a){
  if(a.transport==='http_api'){try{return 'api:'+a.protocol+':'+new URL(a.endpoint).host}catch(e){return 'api:'+a.protocol}}
  if(a.transport==='dsh_cli')return 'dsh:'+(a.dsh_provider||'default');
  if(a.transport==='worker_bridge')return 'worker:'+a.id;
  if(a.transport==='desktop_ui')return 'desktop_ui:'+(a.desktop_bundle_id||a.app_path||a.id);
  return a.transport||'unknown';
}

function updateCrossModelBanner(){
  const banner=document.getElementById('cross-model-banner');
  const commander=document.getElementById('flow-commander').value;
  const checked=[...document.querySelectorAll('[data-flow-agent]:checked')].map(e=>e.dataset.flowAgent);
  const ids=[...new Set([commander,...checked].filter(Boolean))];
  const chosen=ids.map(id=>knownAgents.find(a=>a.id===id)).filter(Boolean);
  if(chosen.length<2){
    banner.className='banner warn';
    banner.textContent='请至少启用 2 个可执行 Agent 才能启动多 Agent 协作（可添加 API、启用 DeepSeek Harness、Worker 桥接或受控桌面 UI）。';
    return;
  }
  const models=[...new Set(chosen.map(modelOf).filter(Boolean))];
  const providers=[...new Set(chosen.map(providerOf))];
  if(models.length>=2||providers.length>=2){
    banner.className='banner ok';
    banner.textContent=`✅ 跨模型协同就绪：已选 ${chosen.length} 个 Agent · ${models.length||providers.length} 个不同模型/通道（${models.join(' + ')||providers.join(' + ')}）`;
  }else{
    banner.className='banner warn';
    banner.textContent=`⚠️ 当前所选 ${chosen.length} 个 Agent 共用同一模型/通道（${models[0]||'未指定'}），属于同模型独立上下文复核；添加 DeepSeek / Gemini / Claude / Kimi 等即可真正跨模型联动。`;
  }
}

async function setupDshPrompt(){
  const useCreds=confirm('点击“确定”将通过 --patch 复用本机 DeepSeek Harness 已保存账号/凭据（不读取密钥文件）；点击“取消”则创建待填 DEEPSEEK_API_KEY 的无头入口。');
  await act('setup_dsh',{use_dsh_credentials:useCreds});
}

function toggleTransport(){
  const t=document.getElementById('agent-transport').value;
  document.getElementById('api-fields').hidden=t!=='http_api';
  document.getElementById('desktop-fields').hidden=t!=='file_bridge';
  document.getElementById('desktop-ui-fields').hidden=t!=='desktop_ui';
  document.getElementById('cli-fields').hidden=!['dsh_cli','claude_cli','gemini_cli','codebuddy_cli','custom_cli'].includes(t);
  document.getElementById('dsh-extra').hidden=t!=='dsh_cli';
  document.getElementById('custom-cli-extra').hidden=t!=='custom_cli';
  document.getElementById('worker-fields').hidden=t!=='worker_bridge';
  if(t==='file_bridge'||t==='desktop_ui')loadApps();
  if(t==='desktop_ui')loadDesktopUiStatus();
  if(t==='dsh_cli'&&!document.getElementById('agent-executable').value){
    const dsh=knownAgents.find(a=>a.candidate_executable||a.id==='deepseek');
    if(dsh?.candidate_executable)document.getElementById('agent-executable').value=dsh.candidate_executable;
  }
}

async function loadApps(){
  try{
    const r=await hubFetch('/api/local-apps');
    const apps=await r.json();
    const opts=apps.map(a=>`<option value="${esc(a.path)}">${esc(a.name)} — ${esc(a.path)}</option>`).join('');
    for(const [id,placeholder] of [['agent-app','按名称自动匹配'],['agent-dui-app','请选择已安装的 .app']]){
      const select=document.getElementById(id),old=select.value;
      select.innerHTML=`<option value="">${placeholder}</option>`+opts;
      select.value=select.dataset.pending||old;select.dataset.pending='';
    }
  }catch(e){msg('无法读取应用列表')}
}

function resetAgent(){
  ['agent-id','agent-name','agent-endpoint','agent-model','agent-key','agent-executable','agent-cli-model','agent-dsh-key','agent-cli-args','agent-worker-model','agent-dui-model','agent-strengths'].forEach(id=>{const el=document.getElementById(id);if(el)el.value=''});
  document.getElementById('agent-dsh-provider').value='deepseek-official';
  document.getElementById('agent-dsh-key-env').value='DEEPSEEK_API_KEY';
  document.getElementById('agent-worker-wait').value='0';
  document.getElementById('agent-dui-new').value='cmd+n';
  document.getElementById('agent-dui-send').value='return';
  document.getElementById('agent-dui-settle').value='4';
  document.getElementById('agent-dui-files').checked=false;
  document.getElementById('agent-dui-auto').checked=true;
  document.getElementById('desktop-ui-preset').value='';
  ['agent-app','agent-dui-app'].forEach(id=>{const s=document.getElementById(id);s.value='';s.dataset.pending=''});
  document.getElementById('agent-transport').value='http_api';
  document.getElementById('agent-protocol').value='openai';
  document.getElementById('api-preset').value='';
  document.getElementById('clear-key').checked=false;
  document.getElementById('auto-route').checked=false;
  document.getElementById('agent-dsh-creds').checked=false;
  document.getElementById('agent-cli-allow-files').checked=true;
  document.getElementById('cli-auto-route').checked=true;
  document.getElementById('agent-worker-files').checked=true;
  document.getElementById('agent-worker-auto').checked=true;
  document.getElementById('synced-model-wrap').hidden=true;
  document.getElementById('agent-save-status').textContent='';
  toggleTransport();
}

function editAgent(id){
  const a=knownAgents.find(x=>x.id===id);if(!a)return;
  resetAgent();
  document.getElementById('agent-editor').open=true;
  document.getElementById('agent-id').value=a.id;
  document.getElementById('agent-name').value=a.name;
  document.getElementById('agent-transport').value=a.transport;
  document.getElementById('agent-strengths').value=a.strengths||'';
  if(a.transport==='http_api'){
    document.getElementById('agent-protocol').value=a.protocol||'openai';
    document.getElementById('agent-endpoint').value=a.endpoint||'';
    document.getElementById('agent-model').value=a.model?.value||'';
    document.getElementById('auto-route').checked=!!a.auto_route;
    if(a.synced_models?.length){
      const wrap=document.getElementById('synced-model-wrap'),sel=document.getElementById('synced-model-select');
      wrap.hidden=false;
      sel.innerHTML='<option value="">-- 从已同步模型选择 --</option>'+a.synced_models.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('');
    }
  }else if(a.transport==='file_bridge'){
    document.getElementById('agent-app').dataset.pending=a.app_path||'';
  }else if(a.transport==='desktop_ui'){
    document.getElementById('agent-dui-app').dataset.pending=a.app_path||'';
    document.getElementById('agent-dui-model').value=a.model?.value||'';
    document.getElementById('agent-dui-new').value=a.new_chat_shortcut||'cmd+n';
    document.getElementById('agent-dui-send').value=a.send_shortcut||'return';
    document.getElementById('agent-dui-settle').value=String(a.ui_settle_seconds||4);
    document.getElementById('agent-dui-files').checked=!!a.allow_files;
    document.getElementById('agent-dui-auto').checked=a.auto_route!==false;
  }else if(a.transport==='worker_bridge'){
    document.getElementById('agent-worker-model').value=a.model?.value||'';
    document.getElementById('agent-worker-wait').value=String(a.wait_for_worker_seconds||0);
    document.getElementById('agent-worker-files').checked=a.allow_files!==false;
    document.getElementById('agent-worker-auto').checked=a.auto_route!==false;
  }else{
    document.getElementById('agent-executable').value=a.executable||'';
    document.getElementById('agent-cli-model').value=a.execution_model||a.model?.value||'';
    document.getElementById('cli-auto-route').checked=a.auto_route!==false;
    if(a.transport==='dsh_cli'){
      document.getElementById('agent-dsh-provider').value=a.dsh_provider||'deepseek-official';
      document.getElementById('agent-dsh-creds').checked=!!a.use_dsh_credentials;
      document.getElementById('agent-dsh-key-env').value=a.dsh_api_key_env||'DEEPSEEK_API_KEY';
    }
    if(a.transport==='custom_cli'){
      document.getElementById('agent-cli-args').value=(a.cli_args||[]).join(' ');
      document.getElementById('agent-cli-allow-files').checked=!!a.allow_files;
    }
  }
  toggleTransport();
  document.getElementById('agent-editor').scrollIntoView({behavior:'smooth'});
}

async function saveAgent(){
  const t=document.getElementById('agent-transport').value;
  const d={id:document.getElementById('agent-id').value||undefined,name:document.getElementById('agent-name').value,transport:t,strengths:document.getElementById('agent-strengths').value};
  if(t==='http_api'){
    Object.assign(d,{protocol:document.getElementById('agent-protocol').value,endpoint:document.getElementById('agent-endpoint').value,model:document.getElementById('agent-model').value,key:document.getElementById('agent-key').value,clear_key:document.getElementById('clear-key').checked,auto_route:document.getElementById('auto-route').checked});
  }else if(t==='file_bridge'){
    d.app_path=document.getElementById('agent-app').value;
  }else if(t==='desktop_ui'){
    Object.assign(d,{
      app_path:document.getElementById('agent-dui-app').value,
      model:document.getElementById('agent-dui-model').value,
      new_chat_shortcut:document.getElementById('agent-dui-new').value,
      send_shortcut:document.getElementById('agent-dui-send').value,
      ui_settle_seconds:Number(document.getElementById('agent-dui-settle').value||4),
      allow_files:document.getElementById('agent-dui-files').checked,
      auto_route:document.getElementById('agent-dui-auto').checked
    });
  }else if(t==='worker_bridge'){
    Object.assign(d,{model:document.getElementById('agent-worker-model').value,wait_for_worker_seconds:Number(document.getElementById('agent-worker-wait').value||0),allow_files:document.getElementById('agent-worker-files').checked,auto_route:document.getElementById('agent-worker-auto').checked});
  }else{
    Object.assign(d,{executable:document.getElementById('agent-executable').value,model:document.getElementById('agent-cli-model').value,auto_route:document.getElementById('cli-auto-route').checked});
    if(t==='dsh_cli'){
      Object.assign(d,{dsh_provider:document.getElementById('agent-dsh-provider').value,use_dsh_credentials:document.getElementById('agent-dsh-creds').checked,key:document.getElementById('agent-dsh-key').value,dsh_api_key_env:document.getElementById('agent-dsh-key-env').value});
    }
    if(t==='custom_cli'){
      Object.assign(d,{cli_args:document.getElementById('agent-cli-args').value,allow_files:document.getElementById('agent-cli-allow-files').checked});
    }
  }
  if(await act('save_agent',{config:d})){
    resetAgent();
    document.getElementById('agent-save-status').textContent='已保存。可点击下方“测试连接 / 验证自动执行 / 验证桌面操控”核验真实可用性。';
  }
}

function renderManagement(s){
  knownAgents=s.agents;
  const select=document.getElementById('client'),old=select.value;
  select.innerHTML='<option value="auto">自动选择已接通 Agent</option>'+s.agents.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}（${esc(transportLabels[a.transport]||a.transport)}）</option>`).join('');
  if([...select.options].some(o=>o.value===old))select.value=old;
  document.getElementById('manage-agents').innerHTML=s.agents.map(a=>{
    const t=a.transport;
    const extra=t==='http_api'
      ? `${a.protocol||'openai'} · ${a.endpoint||''} ${a.has_key?'· 密钥已保存':'· 无密钥'}${a.auto_route?' · 自动分配':' · 仅手动'}${a.synced_models?.length?' · 已同步 '+a.synced_models.length+' 模型':''}`
      : t==='worker_bridge'
      ? (a.worker_online?`在线 Worker：${a.worker_online.worker_id}（${a.worker_online.age_seconds}s 前心跳）`:'未检测到在线 Worker（派单后可自动等待或监听 result.json）')
      : t==='desktop_ui'
      ? `${a.app_path||''} · 快捷键 ${a.new_chat_shortcut||'cmd+n'} / ${a.send_shortcut||'return'} · 串行互斥`
      : (a.executable||transportLabels[t]||t);
    return `<div class="manage-row">
      <b>${esc(a.name)}</b> <span class="tag">${esc(transportLabels[t]||t)}</span> ${a.worker_online?'<span class="tag ok"><span class="dot"></span>Worker 在线</span>':''}
      <div class="note">${esc(extra)} ${modelOf(a)?'· 模型：'+esc(modelOf(a)):''}</div>
      <div style="margin-top:4px">
        ${a.custom&&t!=='codex_cli'?`<button class="alt" data-edit="${esc(a.id)}">编辑</button>`:''}
        ${t==='http_api'?`<button data-action="test_api" data-agent="${esc(a.id)}">测试连接</button><button class="alt" data-action="sync_models" data-agent="${esc(a.id)}">同步可用模型</button>`:`<button class="alt" data-action="refresh_models" data-agent="${esc(a.id)}">识别可用模型${a.available_models?.length?'（'+a.available_models.length+'）':''}</button>`}
        ${t==='codex_cli'?`<button data-action="test_codex" data-agent="${esc(a.id)}">验证任务执行</button>`:''}
        ${['dsh_cli','claude_cli','gemini_cli','codebuddy_cli','custom_cli','worker_bridge'].includes(t)?`<button data-action="test_agent" data-agent="${esc(a.id)}">验证自动执行</button>`:''}
        ${t==='desktop_ui'?`<button data-action="test_desktop_ui" data-agent="${esc(a.id)}">验证桌面操控</button><button class="alt" data-action="check_accessibility">检测辅助功能权限</button>`:''}
        ${t==='worker_bridge'?`<button class="alt" data-action="get_worker_command" data-agent="${esc(a.id)}">查看接入命令</button>`:''}
        ${t==='file_bridge'?`<button class="alt" data-action="check_desktop" data-agent="${esc(a.id)}">打开并检查</button>`:''}
        <button class="danger" data-action="delete_agent" data-agent="${esc(a.id)}">删除到回收站</button>
      </div>
    </div>`;
  }).join('')||'暂无 Agent，可展开上方表单添加一个。';
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editAgent(b.dataset.edit));
  document.getElementById('trash').innerHTML=[
    ...(s.deleted_agents||[]).map(a=>`<div class="manage-row">Agent：${esc(a.name)} <button class="alt" data-action="restore_agent" data-agent="${esc(a.id)}">恢复 Agent</button></div>`),
    ...(s.deleted_jobs||[]).map(j=>`<div class="manage-row">任务：${esc(j.title)} <button class="alt" data-action="restore_job" data-job="${esc(j.id)}">恢复任务</button></div>`)
  ].join('')||'回收站为空。';
}

function renderWorkflowControls(s){
  const ready=s.agents.filter(canRun), sig=JSON.stringify(ready.map(a=>[a.id,a.name,modelOf(a),a.transport,!!a.worker_online,(a.available_models||[]).map(m=>m.id)]));
  if(sig!==flowCatalogSignature){
    document.querySelectorAll('[data-flow-agent]').forEach(e=>flowChoices.set(e.dataset.flowAgent,e.checked));
    const sel=document.getElementById('flow-commander'),old=sel.value;
    sel.innerHTML=ready.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}${modelOf(a)?' ['+esc(modelOf(a))+']':''}</option>`).join('');
    sel.value=ready.some(a=>a.id===old)?old:ready.some(a=>a.id==='hub-gpt')?'hub-gpt':ready[0]?.id||'';
    document.getElementById('flow-participants').innerHTML='<p style="margin:8px 0 4px">参与协作 Agent 与各自模型（可直接下拉自主切换每个 Agent 的模型）：</p><div class="pill-row">'+ready.map(a=>`<div class="part-item"><label style="display:inline-flex;align-items:center;gap:6px;margin:0"><input type="checkbox" data-flow-agent="${esc(a.id)}" onchange="updateCrossModelBanner()" ${flowChoices.get(a.id)!==false?'checked':''}><b>${esc(a.name)}</b></label><select class="mini-sel" data-model-select="${esc(a.id)}" aria-label="${esc(a.name)} 模型" onchange="switchAgentModel('${esc(a.id)}',this)">${modelOptionsHtml(a)}</select></div>`).join('')+'</div>';
    flowCatalogSignature=sig;
  }
  renderCommanderModelSelect();
  updateCrossModelBanner();
  const unavailable=s.agents.filter(a=>!canRun(a));
  document.getElementById('flow-unavailable').textContent=unavailable.length?'当前未参与自动协作：'+unavailable.map(a=>a.name).join('、')+'（需先启用无头入口、允许 API 自动分配、配置 Worker 桥接或启用受控桌面 UI）。':'';
  document.getElementById('flow-start').disabled=ready.length<2;
  const selectable=s.agents.filter(a=>a.transport!=='http_api'),ps=JSON.stringify(selectable.map(a=>[a.id,a.name]));
  if(ps!==profileSignature){
    const sel=document.getElementById('profile-agent'),old=sel.value;
    sel.innerHTML=selectable.map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
    if(selectable.some(a=>a.id===old))sel.value=old;
    profileSignature=ps;loadProfile();
  }
}

function loadProfile(){
  const a=knownAgents.find(a=>a.id===document.getElementById('profile-agent').value);
  document.getElementById('profile-model').value=a?.execution_model||a?.model?.value||'';
  document.getElementById('profile-strengths').value=a?.strengths||'';
}

async function startFlow(){
  const b=document.getElementById('flow-start');b.disabled=true;
  const localPaths=document.getElementById('flow-local-paths').value.split('\n').map(x=>x.trim()).filter(Boolean);
  const ok=await act('workflow',{
    goal:document.getElementById('flow-goal').value,
    requirements:document.getElementById('flow-requirements').value,
    commander:document.getElementById('flow-commander').value,
    participants:[...document.querySelectorAll('[data-flow-agent]:checked')].map(e=>e.dataset.flowAgent),
    files_allowed:document.getElementById('flow-files').checked,
    require_cross_model:document.getElementById('flow-cross-model').checked,
    parallel_steps:document.getElementById('flow-parallel').checked,
    confirm_plan:document.getElementById('flow-confirm-plan').checked,
    max_concurrency:Number(document.getElementById('flow-concurrency').value||3),
    max_steps:Number(document.getElementById('flow-steps').value),
    max_revisions:Number(document.getElementById('flow-revisions').value),
    max_calls:Number(document.getElementById('flow-calls').value),
    upload_ids:stagedUploads.map(u=>u.id),
    local_paths:localPaths
  });
  if(ok){
    stagedUploads=[];
    renderStagedUploads();
    document.getElementById('flow-local-paths').value='';
  }
  b.disabled=knownAgents.filter(canRun).length<2;
}

function renderPipeline(phase, confirmPlan){
  const order=confirmPlan
    ? ['planning','awaiting_approval','working','synthesizing','reviewing','delivering','complete']
    : ['planning','working','synthesizing','reviewing','delivering','complete'];
  const idx=order.indexOf(phase);
  return '<div class="pipeline">'+order.map((p,i)=>`<span class="step-pill ${i<idx||(phase==='complete'&&i===idx)?'done':i===idx?'active':''}">${i+1}. ${esc(phases[p]||p)}</span>`).join('')+'</div>';
}

async function submitApprovePlan(jobId){
  const box=document.getElementById('hitl-plan-'+jobId);
  if(!box)return;
  const steps=[...box.querySelectorAll('[data-edit-step]')].map(row=>({
    id:row.dataset.editStep,
    agent:row.querySelector('[data-step-agent]').value,
    prompt:row.querySelector('[data-step-prompt]').value
  }));
  const note=document.getElementById('hitl-approve-note-'+jobId)?.value||'';
  await act('approve_plan',{job:jobId,steps,note});
}

async function submitRevisePlan(jobId){
  const fb=document.getElementById('hitl-revise-fb-'+jobId)?.value||'';
  if(!fb.trim()){msg('请先填写让总指挥重新规划的修改意见');return}
  await act('revise_plan',{job:jobId,feedback:fb});
}

async function submitInjectNote(jobId){
  const note=document.getElementById('inject-note-'+jobId)?.value||'';
  const paths=(document.getElementById('inject-paths-'+jobId)?.value||'').split('\n').map(x=>x.trim()).filter(Boolean);
  const ups=(jobUploadMap[jobId]||[]).map(u=>u.id);
  const ok=await act('inject_note',{job:jobId,note,upload_ids:ups,local_paths:paths});
  if(ok){
    jobUploadMap[jobId]=[];
  }
}

async function submitApproveDraft(jobId){
  const note=document.getElementById('draft-note-'+jobId)?.value||'';
  await act('approve_draft',{job:jobId,note});
}

async function submitRequestRevision(jobId){
  const fb=document.getElementById('rev-fb-'+jobId)?.value||'';
  if(!fb.trim()){msg('请先填写返工修改意见');return}
  const steps=[...document.querySelectorAll(`[data-rev-step="${jobId}"]:checked`)].map(e=>e.value);
  await act('request_revision',{job:jobId,feedback:fb,steps});
}

function renderJobs(s){
  const name=id=>s.agents.find(a=>a.id===id)?.name||id||'待分配';
  document.getElementById('jobs').innerHTML=s.jobs.map(j=>{
    const f=j.workflow,st=f?.state,del=st?.phase==='complete',awaiting=st?.phase==='awaiting_approval';
    const final=j.tasks.find(t=>t.id===st?.synthesis_id)?.result?.output;
    const synDone=j.tasks.find(t=>t.id===st?.synthesis_id)?.status==='done';
    const collab=st?.delivery?.collaboration;
    const statusCls=j.status==='done'?'ok':j.status==='running'?'live':['waiting_external','awaiting_approval'].includes(j.status)?'warn':['failed','needs_attention'].includes(j.status)?'err':'';
    const attHtml=(j.attachments&&j.attachments.length)
      ? `<div style="margin:6px 0"><span class="note">📎 已附输入资料（${j.attachments.length}）：</span>${j.attachments.map(a=>`<span class="chip">${esc(a.rel_path)} (${Math.max(1,Math.round(a.bytes/1024))} KB${a.text_extractable?' · 可提取文本':''})</span>`).join('')}</div>`
      : '';
    const tokHtml=j.token_usage
      ? ` · Token 消耗：输入 ${j.token_usage.input_tokens} / 输出 ${j.token_usage.output_tokens} / 合计 ${j.token_usage.total_tokens}`
      : '';
    const notesHtml=(st?.user_notes&&st.user_notes.length)
      ? `<details><summary>用户补充指令与反馈（${st.user_notes.length} 条）</summary>${st.user_notes.map(n=>`<div class="note">• [${esc(n.at)}] ${esc(n.text)}</div>`).join('')}</details>`
      : '';

    let approvalHtml='';
    if(awaiting&&st?.plan){
      const opts=(f.config.catalog||[]).filter(c=>c.can_run);
      approvalHtml=`<div class="hitl-box" id="hitl-plan-${esc(j.id)}">
        <b>✋ 等待你审核分工计划（可直接修改每步负责 Agent 与要求，或打回重规划）</b>
        <details open><summary>总指挥需求分析</summary><pre>${esc(st.plan.analysis)}</pre></details>
        ${st.plan.steps.map(step=>`<div class="step-edit" data-edit-step="${esc(step.id)}">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
            <span><b>${esc(step.id)}</b> ${step.dependencies?.length?'<span class="tag">依赖 '+esc(step.dependencies.join(','))+'</span>':'<span class="tag live">可并行</span>'} <span class="note">${esc(step.reason)}</span></span>
            <label>负责 Agent：<select data-step-agent>${opts.map(o=>`<option value="${esc(o.id)}" ${o.id===step.agent?'selected':''}>${esc(o.name)}${o.model?' ['+esc(o.model)+']':''}</option>`).join('')}</select></label>
          </div>
          <textarea data-step-prompt style="min-height:68px">${esc(step.prompt)}</textarea>
        </div>`).join('')}
        <label class="note">批准时附加给执行者的补充说明（可选）：</label>
        <input id="hitl-approve-note-${esc(j.id)}" style="width:100%;margin:4px 0 8px" placeholder="例如：重点保留原始指标口径，输出表格放开头">
        <div>
          <button onclick="submitApprovePlan('${esc(j.id)}')">✅ 批准分工并立即执行</button>
        </div>
        <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:10px">
          <label class="note">如果不满意该分工，填写意见打回让总指挥重规划：</label>
          <input id="hitl-revise-fb-${esc(j.id)}" style="width:100%;margin:4px 0 6px" placeholder="例如：把架构评审改由 DeepSeek 执行，增加安全风险拆解步骤">
          <button class="alt" onclick="submitRevisePlan('${esc(j.id)}')">↩️ 打回总指挥重规划</button>
        </div>
      </div>`;
    }

    let injectHtml='';
    if(f&&!del){
      injectHtml=`<details class="hitl-box" style="padding:10px 14px">
        <summary>💬 中途追加指令或补充上传资料（自动注入后续尚未执行的步骤、整合与复核）</summary>
        <textarea id="inject-note-${esc(j.id)}" style="min-height:64px;margin-top:8px" placeholder="例如：请重点补充成本对比，并忽略旧版接口字段"></textarea>
        <div style="margin:6px 0;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <input type="file" multiple onchange="handleJobExtraUpload('${esc(j.id)}',this.files)">
          <span id="job-up-${esc(j.id)}"></span>
        </div>
        <input id="inject-paths-${esc(j.id)}" style="width:100%;margin:4px 0 8px" placeholder="可选：补挂本地文件/目录绝对路径（可留空）">
        <button class="alt" onclick="submitInjectNote('${esc(j.id)}')">注入指令 / 资料</button>
      </details>`;
    }

    let draftControlHtml='';
    if(f&&synDone&&!del){
      draftControlHtml=`<div class="hitl-box">
        <b>📝 已有整合草稿，你可以直接拍板采纳，或指定返工修改：</b>
        <div style="margin-top:8px">
          <input id="draft-note-${esc(j.id)}" style="width:62%;margin-right:6px" placeholder="人工拍板备注（可选）">
          <button onclick="submitApproveDraft('${esc(j.id)}')">✅ 采纳此草稿并完成交付</button>
        </div>
        <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:10px">
          <label class="note">返工修改意见（不勾选步骤则仅由总指挥修订整合稿，勾选步骤则重跑对应步骤及下游）：</label>
          <div class="pill-row">${(st.plan?.steps||[]).map(s=>`<label class="chk"><input type="checkbox" data-rev-step="${esc(j.id)}" value="${esc(s.id)}">重跑 ${esc(s.id)}（${esc(name(s.agent))}）</label>`).join('')}</div>
          <textarea id="rev-fb-${esc(j.id)}" style="min-height:64px" placeholder="写明需要修改的具体问题、新增章节或风格要求"></textarea>
          <button class="alt" onclick="submitRequestRevision('${esc(j.id)}')">🔁 按我的意见发起返工</button>
        </div>
      </div>`;
    }else if(f&&del&&st?.plan){
      draftControlHtml=`<details class="hitl-box" style="padding:10px 14px">
        <summary>🔁 基于当前交付继续提修改意见（多轮交互迭代）</summary>
        <div class="pill-row" style="margin-top:8px">${(st.plan?.steps||[]).map(s=>`<label class="chk"><input type="checkbox" data-rev-step="${esc(j.id)}" value="${esc(s.id)}">重跑 ${esc(s.id)}（${esc(name(s.agent))}）</label>`).join('')}</div>
        <textarea id="rev-fb-${esc(j.id)}" style="min-height:64px" placeholder="输入你希望继续完善或修改的内容"></textarea>
        <button class="alt" onclick="submitRequestRevision('${esc(j.id)}')">发起新一轮修订</button>
      </details>`;
    }

    const dlHtml=(del&&st.delivery?.files?.length)
      ? `<div class="dl-links">${st.delivery.files.map(df=>`<button type="button" data-download-job="${esc(j.id)}" data-download-file="${esc(df.path)}">⬇ 下载 ${esc(df.path)} (${Math.max(1,Math.round((df.bytes||0)/1024))} KB)</button>`).join('')}</div>`
      : '';

    return `<div class="job">
      <div class="jobhead">
        <div>
          <b>${esc(j.title)}</b>
          <div class="note">${esc(j.id)} · <span class="tag ${statusCls}">${esc(names[j.status]||j.status)}</span>${tokHtml}</div>
        </div>
        <div>
          <button class="alt" data-action="run" data-job="${esc(j.id)}">继续执行</button>
          <button class="alt" data-action="pause" data-job="${esc(j.id)}">暂停</button>
          <button data-action="resume" data-job="${esc(j.id)}">恢复</button>
          <button class="danger" data-action="delete_job" data-job="${esc(j.id)}" ${['running','pending'].includes(j.status)?'disabled':''}>删除到回收站</button>
        </div>
      </div>
      ${attHtml}
      ${j.attachments?.some(file=>!file.recipients)?`<button data-action="approve_saved_inputs" data-job="${esc(j.id)}">确认历史附件与接收者</button>`:''}
      ${f?renderPipeline(st.phase, f.config.confirm_plan)+`<p class="note">已调用 ${st.calls} / ${f.config.max_calls} 次 · 返工 ${st.cycle} / ${f.config.max_revisions} 轮${st.parallel_waves?' · 并行波次 '+st.parallel_waves:''}${f.config.require_cross_model?' · 已启用强制跨模型':''}</p>${st.limit_reason||st.error?`<div class="banner warn">${esc(st.limit_reason||st.error)}</div>`:''}${notesHtml}${approvalHtml}${!awaiting&&st.plan?`<details><summary>总指挥分析与多 Agent 分工依据（${st.plan.steps.length} 步）</summary><pre>${esc(st.plan.analysis)}</pre>${st.plan.steps.map(t=>`<p><b>${esc(t.id)} · ${esc(name(t.agent))}</b> ${t.dependencies?.length?'<span class="tag">依赖 '+esc(t.dependencies.join(','))+'</span>':'<span class="tag live">可并行</span>'}<br>${esc(t.reason)}<br><span class="note">${esc(t.prompt)}</span></p>`).join('')}</details>`:''}`:''}
      ${del?`<div class="banner ${collab?.cross_model_verified||st.delivery?.independence?.different_model?'ok':'warn'}">${st.delivery?.human_approved?'✅ 人工拍板确认交付'+(st.delivery.human_note?'（'+esc(st.delivery.human_note)+'）':'')+' · ':''}${collab?.cross_model_verified?'✅ 跨模型协作已核实：实际观测到多个不同模型（'+esc((collab.models_observed||[]).join(' + '))+'）':st.delivery?.independence?.different_model?'✅ 已取得不同模型的独立复核回执':'ℹ️ 独立上下文复核；不同模型回执未同时确认（配置模型：'+esc((collab?.models_configured||[]).join(' / ')||'未知')+'）'}</div><button data-action="open_delivery" data-job="${esc(j.id)}">打开交付文件夹</button><span class="note"> ${esc(st.delivery?.directory)}</span>${dlHtml}<details open><summary>最终交付正文</summary><pre>${esc(final)}</pre></details>`:final?`<details open><summary>待验收草稿</summary><pre>${esc(final)}</pre></details>`:''}
      ${draftControlHtml}
      ${injectHtml}
      <table>
        <tr><th>步骤</th><th>执行者 / 模型</th><th>状态与实时跟踪</th><th>尝试</th><th>操作</th></tr>
        ${j.tasks.map(t=>{
          const prog=t.progress;
          const mval=t.result?.model?.value||prog?.model_reported||t.result?.configured_model?.value||'';
          const u=t.result?.usage;
          const uStr=u&&u.total_tokens?` · ${u.total_tokens} tok`:'';
          const liveHtml=(t.status==='running'||t.status==='waiting_external')&&prog
            ? `<div class="live-box">⏱ ${esc(prog.elapsed_seconds)}s · <b>${esc(prog.stage)}</b>：${esc(prog.message)}${prog.recent_events?.length?'<br><span class="note">最近事件：'+esc(prog.recent_events.slice(-2).map(e=>e.text).join(' → '))+'</span>':''}</div>`
            : (t.result?.elapsed_seconds?`<div class="note">耗时 ${esc(t.result.elapsed_seconds)}s · ${esc(transportLabels[t.result.transport]||t.result.transport||'')}${uStr}</div>`:'');
          return `<tr>
            <td><b>${esc(t.id)}</b></td>
            <td>${esc(name(t.agent||t.spec.agent))}${mval?`<div class="note">${esc(mval)}</div>`:''}</td>
            <td><span class="tag ${t.status==='done'?'ok':t.status==='running'?'live':['waiting_external','awaiting_approval'].includes(t.status)?'warn':['failed','blocked'].includes(t.status)?'err':''}">${esc(names[t.status]||t.status)}</span>${liveHtml}</td>
            <td>${t.attempt}</td>
            <td>
              ${['failed','blocked'].includes(t.status)?`<button class="alt" data-action="retry" data-job="${esc(j.id)}" data-task="${esc(t.id)}">重试</button>`:''}
              ${t.status==='waiting_external'?`<button class="alt" data-action="open_handoff" data-job="${esc(j.id)}" data-task="${esc(t.id)}">打开交接目录</button>`:''}
            </td>
          </tr>`;
        }).join('')}
      </table>
      ${j.tasks.map(t=>`<details><summary>${esc(t.id)} · 成果、证据与验收详情</summary><pre>${esc(JSON.stringify(t.result||{},null,2))}</pre></details>`).join('')}
    </div>`;
  }).join('')||'<p class="sub">暂无任务。在上方填写目标（可附上传资料或勾选执行前审计划），启动多 Agent 跨模型协作。</p>';
}

function userEditingJobs(){
  const jobsEl=document.getElementById('jobs');
  if(!jobsEl)return false;
  const active=document.activeElement;
  if(active&&jobsEl.contains(active)&&['INPUT','TEXTAREA','SELECT'].includes(active.tagName))return true;
  for(const el of jobsEl.querySelectorAll('textarea[id^="inject-note-"],textarea[id^="rev-fb-"],input[id^="hitl-"],input[id^="draft-note-"]')){
    if(el.value&&el.value.trim())return true;
  }
  return false;
}

async function refresh(force=false){
  try{
    const r=await hubFetch('/api/state');
    const s=await r.json();
    const selectingModel=!force&&document.activeElement?.matches?.('[data-model-select]');
    renderManagement(s);
    if(!selectingModel){
      renderWorkflowControls(s);
      document.getElementById('agents').innerHTML=s.agents.map(a=>{
        const ready=canRun(a);
        const curModel=modelOf(a)||a.ui_model_observation?.value||'模型未知';
        const availCount=(a.available_models||[]).length;
        return `<div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">
            <b>${esc(a.name)}</b>
            <span class="tag ${ready?'ok':''}">${esc(transportLabels[a.transport]||a.transport)}</span>
          </div>
          <div class="model">${esc(curModel)}</div>
          <div class="note">${esc(a.observed_model?.value&&a.observed_model.value===curModel?'✅ 真实执行回执':a.execution_model?'已指定执行模型':a.model?.source==='user_selected'?'用户自主选择':a.model?.source==='user_confirmed'?'用户确认（尚非执行证据）':a.model?.value?'配置线索':'无可验证身份')}${availCount?` · 已识别 ${availCount} 个可选模型`:''}</div>
          <select class="card-model-sel" data-model-select="card-${esc(a.id)}" aria-label="切换 ${esc(a.name)} 模型" onchange="switchAgentModel('${esc(a.id)}',this)">${modelOptionsHtml(a)}</select>
          <p style="margin:6px 0"><span class="tag ${ready?'ok':'warn'}">${esc(a.automation?.status)}</span> ${a.worker_online?'<span class="tag ok"><span class="dot"></span>在线</span>':''}</p>
          <div class="note">版本 ${esc(a.version||'未知')}<br>${esc(a.automation?.reason||'')}</div>
          ${a.desktop_check?`<p class="note">${esc(a.desktop_check.started?'应用已启动 · 任务接入待验证':'应用启动未确认')}<br>${esc(a.desktop_check.checked_at)}</p>`:''}
        </div>`;
      }).join('');
    }
    if(!autoRefreshedModelsOnce&&s.agents.some(a=>['codex_cli','dsh_cli'].includes(a.transport)&&!(a.available_models&&a.available_models.length))){
      autoRefreshedModelsOnce=true;
      hubFetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Hub-Token':token},body:JSON.stringify({action:'refresh_models'})}).then(res=>{if(res.ok)refresh(true)}).catch(()=>{});
    }
    const editing=!force&&userEditingJobs();
    if(!editing){
      renderJobs(s);
    }
    document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>act(b.dataset.action,{job:b.dataset.job,task:b.dataset.task,agent:b.dataset.agent}));
    document.getElementById('measurements').innerHTML=s.measurements.length
      ? `<table><tr><th>Agent</th><th>模型证据</th><th>任务类别</th><th>样本数</th><th>通过率</th><th>平均耗时</th></tr>${s.measurements.map(m=>`<tr><td>${esc(m.agent)}</td><td>${esc(m.model)}</td><td>${esc(m.kind)}</td><td>${esc(m.samples)}</td><td>${Math.round(m.success_rate*100)}%</td><td>${Math.round(m.mean_seconds)} 秒</td></tr>`).join('')}</table><p class="note">仅反映声明的验收条件，不同模型与任务类别分别统计。</p>`
      : '尚无真实验收样本。';
    document.getElementById('events').textContent=s.events.map(e=>`${e.at} ${e.job||''} ${e.task||''} ${e.event}`).join('\n');
    const active=s.jobs.some(j=>['running','pending','waiting_external'].includes(j.status));
    document.getElementById('live-indicator').textContent=editing?'编辑中（暂停任务区刷新）':active?'实时追踪中 (1.5s)':'自动监听中';
    clearTimeout(pollTimer);
    pollTimer=setTimeout(()=>refresh(false), active?1500:4000);
  }catch(e){
    msg('工作台连接中断：'+e.message);
    clearTimeout(pollTimer);
    pollTimer=setTimeout(()=>refresh(false), 4000);
  }
}

async function createTask(){
  const ok=await act('create',{
    goal:document.getElementById('goal').value,
    requirements:document.getElementById('requirements').value,
    client:document.getElementById('client').value,
    upload_ids:stagedUploads.map(u=>u.id)
  });
  if(ok&&stagedUploads.length){stagedUploads=[];renderStagedUploads();}
}
function submitPlan(){try{act('submit',{plan:JSON.parse(document.getElementById('plan').value)})}catch(e){msg(e.message)}}
function importResult(){try{act('import',JSON.parse(document.getElementById('import').value))}catch(e){msg(e.message)}}

document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-download-file]');
  if(!b)return;
  try{
    const r=await hubFetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'download_ticket',job:b.dataset.downloadJob,file:b.dataset.downloadFile})});
    if(!r.ok)throw new Error('下载授权失败');
    location.assign((await r.json()).url);
  }catch(err){msg(err.message)}
});
loadPresets();
loadDesktopUiStatus();
refresh(true);
</script></html>'''


def spawn_job(store, job):
    identifier(job)
    store.paused(job)
    destination = store.root / "jobs" / job / "runner.log"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "agent_hub", "--root", str(store.root), "run", job, "--wait"],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process.pid


def serve(store, port=8765, open_browser=True):
    if not 1024 <= port <= 65535:
        raise ValueError("端口必须在 1024–65535 之间")
    check_for_legacy_dashboard(store.root, sorted(set(range(8765, 8786)) | {port}))
    token = secrets.token_urlsafe(32)
    worker_token = ensure_worker_token(store)
    origin = "http://127.0.0.1:%d" % port
    download_tickets = {}
    ticket_lock = threading.Lock()
    processes = []
    stop_watcher = threading.Event()

    def auto_collect_loop():
        while not stop_watcher.wait(2.0):
            try:
                for jid in collect_ready_results(store):
                    try:
                        processes.append(spawn_job(store, jid))
                    except Exception:
                        pass
            except Exception:
                pass

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, value, content="application/json; charset=utf-8"):
            body = value.encode("utf-8") if isinstance(value, str) else dump(value).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def authorized_host(self):
            return self.headers.get("Host") == "127.0.0.1:%d" % port

        def authorized_ui_post(self):
            return (
                self.authorized_host()
                and self.headers.get("Origin") in {origin, None}
                and secrets.compare_digest(self.headers.get("X-Agent-Hub-Token", ""), token)
            )

        def read_json_body(self, max_bytes=2 * 1024 * 1024):
            length = int(self.headers.get("Content-Length", 0))
            if length < 1 or length > max_bytes:
                raise ValueError("请求大小超出限制")
            return json.loads(self.rfile.read(length))

        def _handle_download(self, query):
            qs = parse_qs(query)
            raw_job = (qs.get("job") or [""])[0]
            raw_file = (qs.get("file") or [""])[0]
            try:
                job = identifier(raw_job)
                store.paused(job)
                delivery_dir = store.root / "jobs" / job / "delivery"
                target = inside(delivery_dir, raw_file)
            except (ValueError, KeyError):
                return self.send(400, {"error": "下载路径无效"})
            if not target.is_file() or target.is_symlink():
                return self.send(404, {"error": "交付文件不存在"})
            data = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in target.name) or "file"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Disposition",
                "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quote(target.name)),
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.authorized_host():
                return self.send(403, {"error": "仅允许本机地址"})
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/health":
                health = {"app": "agent_hub", "version": __version__}
                challenge = (parse_qs(parsed.query).get("challenge") or [""])[0]
                if re.fullmatch("[a-f0-9]{32}", challenge):
                    health["proof"] = hmac.new(token.encode(), challenge.encode(), hashlib.sha256).hexdigest()
                return self.send(200, health)
            if path == "/api/download" and parsed.query.startswith("ticket="):
                ticket = (parse_qs(parsed.query).get("ticket") or [""])[0]
                with ticket_lock:
                    item = download_tickets.pop(ticket, None)
                if item and item[0] > time.monotonic():
                    return self._handle_download(item[1])
                return self.send(403, {"error": "下载授权已失效"})
            if not self.authorized_ui_post():
                if path == "/" and not self.headers.get("X-Agent-Hub-Token"):
                    return self.send(200, LOGIN, "text/html; charset=utf-8")
                return self.send(403, {"error": "请从本机启动器打开工作台"})
            if path == "/":
                import html

                page = (
                    HTML.replace("__TOKEN__", json.dumps(token))
                    .replace("__ROOT__", html.escape(str(store.root)))
                    .replace("__VERSION__", html.escape(__version__))
                )
                return self.send(200, page, "text/html; charset=utf-8")
            if path == "/api/local-apps":
                return self.send(200, installed_apps())
            if path == "/api/presets":
                return self.send(200, PROVIDER_PRESETS)
            if path == "/api/desktop-ui-status":
                return self.send(200, {"accessibility": check_accessibility(False), "profiles": DESKTOP_UI_PROFILES})
            if path == "/api/download":
                return self._handle_download(parsed.query)
            if path == "/api/state":
                return self.send(200, store.snapshot())
            return self.send(404, {"error": "不存在"})

        def _handle_worker_api(self):
            if not self.authorized_host() or not secrets.compare_digest(
                self.headers.get("X-Agent-Hub-Worker-Token", ""), worker_token
            ):
                return self.send(403, {"error": "Worker 令牌校验失败"})
            try:
                body = self.read_json_body()
                if self.path == "/api/worker/register":
                    hb = register_heartbeat(
                        store,
                        body["agent"],
                        body.get("worker_id", "worker"),
                        model=body.get("model"),
                        capabilities=body.get("capabilities"),
                        status=body.get("status", "online"),
                    )
                    return self.send(200, hb)
                if self.path == "/api/worker/poll":
                    tasks = poll_tasks(store, body["agent"], worker_id=body.get("worker_id"), model=body.get("model"))
                    return self.send(200, {"tasks": tasks})
                if self.path == "/api/worker/claim":
                    claim = claim_task(store, body["job"], body["task"], body["attempt"], body["agent"], body["worker_id"])
                    return self.send(200, claim)
                if self.path == "/api/worker/progress":
                    claim = report_progress(
                        store,
                        body["job"],
                        body["task"],
                        body["attempt"],
                        body.get("worker_id", "worker"),
                        body["stage"],
                        body["message"],
                        model=body.get("model"),
                    )
                    return self.send(200, claim)
                if self.path == "/api/worker/complete":
                    claim = complete_task(
                        store,
                        body["job"],
                        body["task"],
                        body["attempt"],
                        body.get("worker_id", "worker"),
                        output=body.get("output", ""),
                        artifacts=body.get("artifacts"),
                        model=body.get("model"),
                        failed=body.get("failed", False),
                        error=body.get("error"),
                    )
                    resumed = []
                    for jid in collect_ready_results(store):
                        processes.append(spawn_job(store, jid))
                        resumed.append(jid)
                    return self.send(200, {"claim": claim, "resumed": resumed})
                return self.send(404, {"error": "未知 Worker 接口"})
            except (ValueError, KeyError, RuntimeError, OSError, TypeError) as exc:
                return self.send(400, {"error": str(exc)})

        def do_POST(self):
            if self.path.startswith("/api/worker/"):
                return self._handle_worker_api()
            if not self.authorized_ui_post():
                return self.send(403, {"error": "工作台请求校验失败"})
            if self.path == "/api/upload":
                try:
                    body = self.read_json_body(max_bytes=28 * 1024 * 1024)
                    filename = body.get("filename", "")
                    b64 = body.get("content_base64", "")
                    if not isinstance(b64, str) or not b64:
                        raise ValueError("缺少文件内容")
                    raw = base64.b64decode(b64, validate=True)
                    meta = save_upload(store, filename, raw)
                    return self.send(200, meta)
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    return self.send(400, {"error": str(exc)})
            if self.path != "/api/action":
                return self.send(404, {"error": "不存在"})
            try:
                body = self.read_json_body()
                action, job = body["action"], body.get("job")
                if action == "preview_attachments":
                    operation = body.get("operation")
                    if operation == "approve_saved_inputs":
                        return self.send(200, review_saved_inputs(store, job))
                    if operation == "workflow":
                        receivers = flow_recipients(store, body)
                    elif operation == "create":
                        receivers = [body.get("client")]
                    elif operation == "inject_note":
                        flow = read_flow(store, identifier(job))
                        if not flow:
                            raise ValueError("找不到此工作流")
                        receivers = flow[0]["participants"]
                    else:
                        raise ValueError("无效的附件操作")
                    review, _ = review_inputs(store, body.get("upload_ids"), body.get("local_paths"), receivers)
                    return self.send(200, review)
                if action == "approve_saved_inputs":
                    if not isinstance(body.get("attachment_approval"), dict):
                        raise ValueError("请先确认历史附件和接收者")
                    review_saved_inputs(store, job, body["attachment_approval"])
                    return self.send(200, {"message": "已确认历史附件，可继续任务或重试失败的步骤"})
                if action == "download_ticket":
                    file = body["file"]
                    query = "job=" + quote(identifier(job)) + "&file=" + quote(file)
                    ticket = secrets.token_urlsafe(32)
                    with ticket_lock:
                        now = time.monotonic()
                        for old in [k for k, v in download_tickets.items() if v[0] <= now]:
                            download_tickets.pop(old)
                        download_tickets[ticket] = (now + 45, query)
                    return self.send(200, {"url": "/api/download?ticket=" + ticket})
                if action == "setup_gpt":
                    setup_gpt(store)
                elif action == "setup_dsh":
                    aid = setup_dsh(
                        store,
                        use_dsh_credentials=body.get("use_dsh_credentials") is True,
                        model=body.get("model"),
                        provider=body.get("provider"),
                    )
                    return self.send(200, {"message": "已启用 DeepSeek Harness 无头执行入口：" + aid + "（可点击“验证自动执行”测试）"})
                elif action == "sync_models":
                    synced = sync_api_models(store, body["agent"])
                    return self.send(200, {"message": "已同步 %d 个可用模型；可在卡片或顶部协作区直接下拉切换" % synced["count"], **synced})
                elif action == "refresh_models":
                    refreshed = refresh_agent_models(
                        store,
                        agent_id=body.get("agent"),
                        include_http_api=body.get("include_http_api") is True,
                    )
                    return self.send(
                        200,
                        {
                            "message": "已自动识别并更新 %d 个 Agent 的可选模型列表（共 %d 个模型，可在下拉框自主切换）"
                            % (refreshed["updated_agents"], refreshed["total_models"]),
                            **refreshed,
                        },
                    )
                elif action == "select_model":
                    chosen = select_agent_model(
                        store,
                        body["agent"],
                        body["model"],
                        provider=body.get("provider"),
                    )
                    return self.send(
                        200,
                        {
                            "message": "已将 %s 切换为模型：%s%s"
                            % (chosen["agent"], chosen["model"], ("（%s）" % chosen["provider"]) if chosen.get("provider") else ""),
                            **chosen,
                        },
                    )
                elif action in {"check_accessibility", "prompt_accessibility"}:
                    ax = check_accessibility(prompt=(action == "prompt_accessibility"))
                    status_msg = (
                        "已获得 macOS 辅助功能权限"
                        if ax.get("trusted")
                        else ("已向 macOS 请求辅助功能授权提示，请在“系统设置 → 隐私与安全性 → 辅助功能”中授权" if action == "prompt_accessibility" else "尚未获得 macOS 辅助功能权限")
                    )
                    return self.send(200, {"message": status_msg, "accessibility": ax})
                elif action == "collect_external":
                    collected = collect_ready_results(store)
                    for jid in collected:
                        processes.append(spawn_job(store, jid))
                    return self.send(
                        200,
                        {"message": ("已自动回收并继续 %d 组任务" % len(collected)) if collected else "暂无新的外部成果可回收", "collected": collected},
                    )
                elif action == "get_worker_command":
                    aid = identifier(body["agent"])
                    proj = Path(__file__).resolve().parents[1]
                    cmd = (
                        "1) 启动本地自动 Worker 守护进程（收到任务自动执行脚本并回传结果）：\n"
                        "cd %s && python3 -B -m agent_hub --root %s worker --agent %s --url %s --exec \"python3 your_connector.py\"\n\n"
                        "2) 或将扩展 MCP Server 接入支持 MCP 的客户端（自动领单/汇报/回传）：\n"
                        "python3 -B -m agent_hub --root %s mcp --extended\n\n"
                        "（Worker 令牌已自动保存于 %s/.worker-token，权限 0600）"
                        % (proj, store.root, aid, origin, store.root, store.root)
                    )
                    return self.send(200, {"message": "已生成接入命令", "worker_command": cmd})
                elif action == "save_profile":
                    save_profile(store, body["agent"], body["model"], body.get("strengths", ""))
                elif action == "workflow":
                    job = create_flow(store, body)
                    processes.append(spawn_job(store, job))
                elif action == "approve_plan":
                    approve_plan(store, job, edited_steps=body.get("steps"), user_note=body.get("note", ""))
                    processes.append(spawn_job(store, job))
                    return self.send(200, {"message": "已批准分工计划并启动执行：" + job, "job": job})
                elif action == "revise_plan":
                    revise_plan(store, job, body.get("feedback", ""))
                    processes.append(spawn_job(store, job))
                    return self.send(200, {"message": "已按你的意见要求总指挥重新规划：" + job, "job": job})
                elif action == "inject_note":
                    res = inject_workflow_note(
                        store,
                        job,
                        note=body.get("note", ""),
                        upload_ids=body.get("upload_ids"),
                        local_paths=body.get("local_paths"),
                        attachment_approval=body.get("attachment_approval"),
                    )
                    return self.send(200, {"message": "已注入补充指令 / 资料，后续步骤将自动读取", "job": job, **res})
                elif action == "approve_draft":
                    delivery = approve_draft(store, job, note=body.get("note", ""))
                    return self.send(200, {"message": "已采纳当前草稿并完成交付：" + job, "job": job, "delivery": delivery})
                elif action == "request_revision":
                    request_revision(store, job, body.get("feedback", ""), steps=body.get("steps"))
                    processes.append(spawn_job(store, job))
                    return self.send(200, {"message": "已发起新一轮返工修订：" + job, "job": job})
                elif action == "open_delivery":
                    identifier(job)
                    store.paused(job)
                    flow = read_flow(store, job)
                    if not flow or flow[1]["phase"] != "complete":
                        raise ValueError("此任务尚未通过最终验收")
                    folder = store.root / "jobs" / job / "delivery"
                    if not folder.is_dir() or folder.is_symlink():
                        raise ValueError("交付目录不存在")
                    subprocess.run(["/usr/bin/open", str(folder)], check=True, timeout=10)
                elif action == "open_handoff":
                    identifier(job)
                    tid = identifier(body["task"])
                    row = next((r for r in store.task_rows(job) if r["id"] == tid), None)
                    if not row or row["attempt"] <= 0:
                        raise ValueError("找不到该任务的交接目录")
                    folder = store.root / "jobs" / job / tid / ("attempt-%d" % row["attempt"])
                    if not folder.is_dir() or folder.is_symlink():
                        raise ValueError("交接目录不存在")
                    subprocess.run(["/usr/bin/open", str(folder)], check=True, timeout=10)
                    return self.send(200, {"message": "已打开交接目录；写入 result.json 后将自动回收"})
                elif action == "check_desktop":
                    result = open_and_check(store, body["agent"])
                    return self.send(200, {"message": result["note"], "desktop_check": result})
                elif action == "test_codex":
                    aid = body["agent"]
                    agent = next((a for a in store.agents() if a["id"] == aid and a.get("transport") == "codex_cli"), None)
                    if not agent:
                        raise ValueError("此 Agent 未配置 Codex 自动执行入口")
                    job = store.add_plan(probe_plan(aid))
                    processes.append(spawn_job(store, job))
                elif action in {"test_agent", "test_desktop_ui"}:
                    aid = body["agent"]
                    agent = next((a for a in store.agents() if a["id"] == aid and a.get("transport") in AUTOMATED_TRANSPORTS), None)
                    if not agent:
                        raise ValueError("此 Agent 不支持自动执行验证")
                    if action == "test_desktop_ui" and agent.get("transport") != "desktop_ui":
                        raise ValueError("此 Agent 不是桌面 UI 自动操控类型")
                    job = store.add_plan(
                        {
                            "title": ("桌面 UI 操控验证 · " if action == "test_desktop_ui" else "自动执行连接验证 · ") + agent["name"],
                            "tasks": [
                                {
                                    "id": "verify",
                                    "agent": aid,
                                    "prompt": "不要调用任何工具，请只回复 HUB_OK",
                                    "kind": "connection_test",
                                    "timeout_seconds": 120 if action == "test_desktop_ui" else 90,
                                    "validation": {"text_contains": ["HUB_OK"]},
                                }
                            ],
                        }
                    )
                    processes.append(spawn_job(store, job))
                elif action == "save_agent":
                    save_agent(store, body["config"])
                elif action in {"delete_agent", "restore_agent"}:
                    agent_deleted(store, body["agent"], action == "delete_agent")
                elif action in {"delete_job", "restore_job"}:
                    job_deleted(store, job, action == "delete_job")
                elif action == "test_api":
                    aid = body["agent"]
                    agent = next((a for a in store.agents() if a["id"] == aid and a.get("transport") == "http_api"), None)
                    if agent is None:
                        raise ValueError("API Agent 不存在")
                    job = store.add_plan(
                        {
                            "title": "API 连接测试 · " + agent["name"],
                            "tasks": [
                                {
                                    "id": "connection",
                                    "agent": aid,
                                    "prompt": "请只回复 HUB_OK",
                                    "kind": "connection_test",
                                    "timeout_seconds": 45,
                                    "validation": {"text_contains": ["HUB_OK"]},
                                }
                            ],
                        }
                    )
                    processes.append(spawn_job(store, job))
                elif action == "discover":
                    store.init(discover())
                    try:
                        refresh_agent_models(store)
                    except Exception:
                        pass
                elif action in {"probe", "handoff", "submit", "create"}:
                    if action == "create":
                        goal, requirements, client = body["goal"], body["requirements"], body["client"]
                        if not isinstance(goal, str) or not goal.strip() or not isinstance(requirements, str):
                            raise ValueError("请填写任务目标和验收内容")
                        if client not in {"auto"} | {a["id"] for a in store.agents()}:
                            raise ValueError("未知 Agent")
                        phrases = [p.strip() for p in requirements.splitlines() if p.strip()]
                        if not phrases:
                            raise ValueError("至少填写一条成果中必须出现的内容")
                        task = {
                            "id": "deliver",
                            "kind": "general",
                            "prompt": goal + "\n成果需包含：" + "、".join(phrases),
                            "validation": {"text_contains": phrases},
                        }
                        if client != "auto":
                            task["agent"] = client
                        plan = {"title": goal.strip()[:80], "tasks": [task]}
                    else:
                        if action == "probe":
                            candidates = [a for a in store.agents() if a.get("transport") == "codex_cli"]
                            if not candidates:
                                raise ValueError("请先启用 GPT / Codex 协作入口")
                            plan = probe_plan(next((a["id"] for a in candidates if a["id"] == "hub-codex"), candidates[0]["id"]))
                        elif action == "handoff":
                            if not {"chatgpt", "codex"} <= {a["id"] for a in store.agents()}:
                                raise ValueError("此旧版文件交接演示需要原始 ChatGPT/Codex 配置；自动协作请使用页面顶部入口")
                            plan = handoff_plan()
                        else:
                            plan = body["plan"]
                    bundle = approved_inputs(store, body, [client]) if action == "create" else None
                    job = store.add_plan(plan, paused=bool(bundle))
                    if bundle:
                        attach_job_inputs(store, job, bundle)
                        store.control(job, False)
                    processes.append(spawn_job(store, job))
                elif action == "run":
                    processes.append(spawn_job(store, job))
                elif action in {"pause", "resume"}:
                    store.control(job, action == "pause")
                    if action == "resume":
                        processes.append(spawn_job(store, job))
                elif action == "retry":
                    retry_task(store, job, body["task"])
                    processes.append(spawn_job(store, job))
                elif action == "import":
                    store.import_result(job, body["task"], body["result"])
                    processes.append(spawn_job(store, job))
                else:
                    raise ValueError("未知操作")
                self.send(200, {"message": "已处理" + ("：" + job if job else ""), "job": job})
            except (ValueError, KeyError, RuntimeError, OSError, TypeError, subprocess.SubprocessError) as exc:
                self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    entry = publish_access(store.root, port, token)
    watcher = threading.Thread(target=auto_collect_loop, daemon=True)
    watcher.start()
    for job in store.snapshot()["jobs"]:
        if job["status"] in {"pending", "running"} and not job["paused"]:
            processes.append(spawn_job(store, job["id"]))
    if open_browser:
        webbrowser.open(entry.as_uri())
    print("请从私有入口打开工作台：" + str(entry), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("工作台已关闭；运行任务可通过暂停指令停止。", flush=True)
    finally:
        stop_watcher.set()
        server.server_close()
