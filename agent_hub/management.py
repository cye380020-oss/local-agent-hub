"""User-managed agents, private credentials, model sync, CLI setup, and reversible task deletion."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlsplit, urlunsplit


PROVIDER_PRESETS = [
    {"id": "deepseek", "label": "DeepSeek 官方 API", "protocol": "openai", "endpoint": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"id": "gemini", "label": "Google Gemini API", "protocol": "gemini", "endpoint": "https://generativelanguage.googleapis.com/v1beta", "model": "gemini-2.5-pro"},
    {"id": "openai", "label": "OpenAI 官方 API", "protocol": "openai", "endpoint": "https://api.openai.com/v1", "model": "gpt-5"},
    {"id": "anthropic", "label": "Anthropic Claude API", "protocol": "anthropic", "endpoint": "https://api.anthropic.com/v1", "model": "claude-sonnet-4-5"},
    {"id": "moonshot", "label": "Moonshot（Kimi）", "protocol": "openai", "endpoint": "https://api.moonshot.cn/v1", "model": "kimi-k2-0905-preview"},
    {"id": "zhipu", "label": "智谱 GLM", "protocol": "openai", "endpoint": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.5"},
    {"id": "dashscope", "label": "阿里百炼（通义千问 / DeepSeek）", "protocol": "openai", "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-max"},
    {"id": "ark", "label": "火山方舟（豆包）", "protocol": "openai", "endpoint": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-1-5-pro-32k-250115"},
    {"id": "openrouter", "label": "OpenRouter（多模型聚合）", "protocol": "openai", "endpoint": "https://openrouter.ai/api/v1", "model": "google/gemini-2.5-pro"},
    {"id": "ollama", "label": "本地 Ollama（免费本机模型）", "protocol": "openai", "endpoint": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b"},
    {"id": "lmstudio", "label": "本地 LM Studio", "protocol": "openai", "endpoint": "http://127.0.0.1:1234/v1", "model": "local-model"},
]

SUPPORTED_TRANSPORTS = (
    "http_api",
    "file_bridge",
    "dsh_cli",
    "claude_cli",
    "gemini_cli",
    "codebuddy_cli",
    "custom_cli",
    "worker_bridge",
    "desktop_ui",
)

_MODEL_RE = re.compile(r"[A-Za-z0-9._:/@-]{1,160}")
_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")


@contextmanager
def idle(store):
    with (store.root / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("有任务正在执行，请先暂停并等待执行停止后再修改")
        yield


def endpoint(value, protocol):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("请填写有效 API 地址")
    u = urlsplit(value.strip())
    if not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("API 地址不能含账号、密码、查询参数或片段")
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")):
        raise ValueError("远程 API 必须使用 HTTPS；本机地址可用 HTTP")
    if any(c.isspace() for c in value):
        raise ValueError("API 地址不能含空白")
    if u.port == 8765 and u.hostname in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("不能把工作台自身作为模型 API")
    path = u.path.rstrip("/")
    if protocol == "gemini":
        if not path:
            path = "/v1beta"
        return urlunsplit((u.scheme, u.netloc, path, "", ""))
    if protocol not in ("openai", "anthropic"):
        raise ValueError("不支持的 API 协议")
    suffix = "/chat/completions" if protocol == "openai" else "/messages"
    if not path.endswith(suffix):
        if not path:
            path = "/v1"
        path += suffix
    return urlunsplit((u.scheme, u.netloc, path, "", ""))


def secret_path(store, aid):
    from .core import identifier

    return store.root / ".api-secrets" / (identifier(aid) + ".json")


def save_secret(store, aid, value):
    path = secret_path(store, aid)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"key": value}, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _validate_executable(path_str, label="CLI"):
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError("请填写有效的 %s 可执行文件绝对路径" % label)
    p = Path(path_str.strip())
    if not p.is_absolute():
        raise ValueError("%s 路径必须为绝对路径" % label)
    if not p.is_file() or not os.access(str(p), os.X_OK):
        raise ValueError("找不到可执行的 %s：%s" % (label, p))
    return str(p)


def save_agent(store, data):
    from .core import dump, identifier

    if not isinstance(data, dict):
        raise ValueError("Agent 配置格式不正确")
    aid = identifier(data.get("id") or "custom-" + uuid.uuid4().hex[:10])
    name = data.get("name", "").strip()
    if not name or len(name) > 80:
        raise ValueError("名称需要 1–80 个字符")
    transport = data.get("transport")
    if transport not in SUPPORTED_TRANSPORTS:
        raise ValueError("不支持此接入方式")
    strengths = data.get("strengths", "")
    if not isinstance(strengths, str) or len(strengths) > 2000:
        raise ValueError("能力备注最多 2000 字")

    with idle(store), store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
        old = json.loads(row[0]) if row else {}
        if old and (not old.get("custom") or old.get("deleted")):
            raise ValueError("只能编辑使用中的自定义 Agent")
        agent = {
            "id": aid,
            "name": name,
            "custom": True,
            "deleted": False,
            "transport": transport,
            "version": "自定义",
            "capabilities": [],
            "strengths": strengths.strip() if strengths else old.get("strengths", ""),
            "automation": {"status": "not_verified", "reason": "文件交接，等待外部客户端真实成果"},
        }
        if transport == "file_bridge":
            path = data.get("app_path", "")
            if not isinstance(path, str):
                raise ValueError("绑定应用格式不正确")
            if path:
                from .desktop_clients import resolve_app

                app = resolve_app({"app_path": path})
                agent.update(app_path=app["path"], desktop_bundle_id=app["bundle_id"])
            if old.get("app_path") == agent.get("app_path") and old.get("desktop_check"):
                agent["desktop_check"] = old["desktop_check"]
            if old.get("model"):
                agent["model"] = old["model"]

        elif transport == "http_api":
            protocol = data.get("protocol", "openai")
            if protocol not in ("openai", "anthropic", "gemini"):
                raise ValueError("不支持的 API 协议")
            model = data.get("model", "").strip()
            if not model or len(model) > 160 or any(ord(c) < 32 for c in model):
                raise ValueError("请填写模型标识")
            url = endpoint(data.get("endpoint", ""), protocol)
            key = data.get("key", "")
            if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 for c in key):
                raise ValueError("密钥格式不正确")
            # Never silently send a retained credential to an edited destination.
            if old.get("endpoint") != url and not key and old.get("has_key") and not data.get("clear_key"):
                raise ValueError("更改 API 地址时请重新填写密钥，或选择清除密钥")
            has_key = bool(key) or (old.get("has_key", False) and not data.get("clear_key"))
            agent.update(
                protocol=protocol,
                endpoint=url,
                has_key=has_key,
                auto_route=data.get("auto_route") is True,
                model={"value": model, "source": "user_configured", "confidence": "configured"},
                capabilities=["general", "reasoning", "writing_zh", "review", "coding"],
                automation={"status": "available", "reason": "已配置 API，支持跨模型自动执行文字任务；真实连通性以测试结果为准"},
            )
            if old.get("synced_models"):
                agent["synced_models"] = old["synced_models"]
                agent["models_synced_at"] = old.get("models_synced_at")
            if key or data.get("clear_key"):
                save_secret(store, aid, key)

        elif transport == "dsh_cli":
            exe = _validate_executable(data.get("executable", ""), "DeepSeek Harness CLI")
            model = (data.get("model") or "").strip()
            if model and not _MODEL_RE.fullmatch(model):
                raise ValueError("模型标识格式不正确")
            provider = (data.get("dsh_provider") or "").strip() or "deepseek-official"
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", provider):
                raise ValueError("DeepSeek Harness Provider 格式不正确")
            key_env = (data.get("dsh_api_key_env") or "DEEPSEEK_API_KEY").strip()
            if not _ENV_RE.fullmatch(key_env):
                raise ValueError("环境变量名格式不正确（如 DEEPSEEK_API_KEY）")
            key = data.get("key", "")
            if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 for c in key):
                raise ValueError("密钥格式不正确")
            has_key = bool(key) or (old.get("has_key", False) and not data.get("clear_key"))
            use_creds = data.get("use_dsh_credentials") is True
            agent.update(
                executable=exe,
                dsh_enabled=True,
                dsh_provider=provider,
                dsh_api_key_env=key_env,
                use_dsh_credentials=use_creds,
                has_key=has_key,
                auto_route=data.get("auto_route", True) is True,
                execution_model=model or None,
                model={"value": model or None, "provider": provider, "source": "user_configured", "confidence": "configured" if model else "unknown"},
                capabilities=["general", "reasoning", "coding", "writing_zh", "review", "files_read", "files_write"],
                automation={"status": "cli_available", "reason": "已启用 DeepSeek Harness 无头 CLI（支持只读/工作区写沙箱与实时事件流）"},
            )
            if key or data.get("clear_key"):
                save_secret(store, aid, key)

        elif transport in ("claude_cli", "gemini_cli", "codebuddy_cli"):
            exe = _validate_executable(data.get("executable", ""), transport)
            model = (data.get("model") or "").strip()
            if model and not _MODEL_RE.fullmatch(model):
                raise ValueError("模型标识格式不正确")
            agent.update(
                executable=exe,
                execution_model=model or None,
                auto_route=data.get("auto_route", True) is True,
                model={"value": model or None, "source": "user_configured", "confidence": "configured" if model else "unknown"},
                capabilities=["general", "reasoning", "coding", "writing_zh", "review", "files_read", "files_write"],
                automation={"status": "cli_available", "reason": "已配置 %s 无头自动执行入口" % transport},
            )

        elif transport == "custom_cli":
            exe = _validate_executable(data.get("executable", ""), "自定义 CLI")
            raw_args = data.get("cli_args", [])
            if isinstance(raw_args, str):
                raw_args = [s for s in raw_args.split() if s]
            if not isinstance(raw_args, list) or not all(isinstance(x, str) for x in raw_args):
                raise ValueError("CLI 参数格式不正确")
            model = (data.get("model") or "").strip()
            if model and not _MODEL_RE.fullmatch(model):
                raise ValueError("模型标识格式不正确")
            allow_files = data.get("allow_files") is True
            caps = ["general", "reasoning", "coding", "writing_zh", "review"]
            if allow_files:
                caps += ["files_read", "files_write"]
            agent.update(
                executable=exe,
                cli_args=raw_args,
                allow_files=allow_files,
                execution_model=model or None,
                auto_route=data.get("auto_route", True) is True,
                model={"value": model or None, "source": "user_configured", "confidence": "configured" if model else "unknown"},
                capabilities=caps,
                automation={"status": "cli_available", "reason": "已配置自定义本机 CLI 连接器，支持自动派发与回收"},
            )

        elif transport == "worker_bridge":
            model = (data.get("model") or "").strip()
            if model and (len(model) > 160 or any(ord(c) < 32 for c in model)):
                raise ValueError("模型名称无效")
            allow_files = data.get("allow_files", True) is True
            caps = ["general", "reasoning", "coding", "writing_zh", "review"]
            if allow_files:
                caps += ["files_read", "files_write"]
            wait_sec = data.get("wait_for_worker_seconds", 0)
            try:
                wait_sec = max(0, min(600, int(wait_sec)))
            except (ValueError, TypeError):
                wait_sec = 0
            agent.update(
                allow_files=allow_files,
                auto_route=data.get("auto_route", True) is True,
                wait_for_worker_seconds=wait_sec,
                model={"value": model or None, "source": "user_confirmed", "confidence": "configured" if model else "unknown"},
                capabilities=caps,
                automation={"status": "ready", "reason": "已启用自动 Worker / MCP 桥接队列：支持自动发任务、实时状态跟踪与结果回收"},
            )

        elif transport == "desktop_ui":
            from .desktop_clients import resolve_app
            from .desktop_ui import ALLOWED_SHORTCUTS

            app_path = (data.get("app_path") or "").strip()
            if not app_path:
                raise ValueError("桌面 UI 自动操作必须选择并绑定一个已安装的本机 Mac 应用")
            app = resolve_app({"app_path": app_path})
            model = (data.get("model") or "").strip()
            if model and (len(model) > 160 or any(ord(c) < 32 for c in model)):
                raise ValueError("模型名称无效")
            new_shortcut = (data.get("new_chat_shortcut") or "cmd+n").strip().lower()
            if new_shortcut not in ALLOWED_SHORTCUTS:
                raise ValueError("不支持的新建会话快捷键")
            send_shortcut = (data.get("send_shortcut") or "return").strip().lower()
            if send_shortcut not in {"return", "cmd+return"}:
                raise ValueError("发送快捷键仅支持 return 或 cmd+return")
            try:
                settle = max(2.0, min(15.0, float(data.get("ui_settle_seconds", 4.0))))
            except (ValueError, TypeError):
                settle = 4.0
            allow_files = data.get("allow_files") is True
            caps = ["general", "reasoning", "coding", "writing_zh", "review"]
            if allow_files:
                caps += ["files_read", "files_write"]
            agent.update(
                app_path=app["path"],
                desktop_bundle_id=app["bundle_id"],
                desktop_ui_enabled=True,
                new_chat_shortcut=new_shortcut,
                send_shortcut=send_shortcut,
                ui_settle_seconds=settle,
                allow_files=allow_files,
                auto_route=data.get("auto_route", True) is True,
                model={"value": model or None, "source": "user_confirmed", "confidence": "configured" if model else "unknown"},
                capabilities=caps,
                automation={"status": "ready", "reason": "已启用受控桌面 UI 自动操作（需授辅助功能权限且保持屏幕解锁；串行互斥执行）"},
            )

        con.execute("INSERT OR REPLACE INTO agents VALUES(?,?)", (aid, dump(agent)))
    store.event(None, None, "agent.saved", {"agent": aid, "transport": transport})
    return aid


def sync_api_models(store, aid):
    from .adapters import _environment
    from .core import dump, identifier, now

    identifier(aid)
    agent = next((a for a in store.agents() if a["id"] == aid and a.get("transport") == "http_api"), None)
    if agent is None:
        raise ValueError("仅 API Agent 支持同步模型列表")
    code_root = Path(__file__).resolve().parents[1]
    worker = "import sys; sys.path.insert(0, sys.argv.pop(1)); from agent_hub.api_worker import main; main()"
    proc = subprocess.run(
        [sys.executable, "-B", "-c", worker, str(code_root), "--list-models", str(store.root), aid],
        capture_output=True,
        text=True,
        env=_environment(),
        timeout=35,
        check=False,
    )
    try:
        result = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise ValueError("模型列表同步进程未返回有效 JSON")
    if result.get("status") != "completed":
        raise ValueError(result.get("error") or "同步模型列表失败")
    models = result.get("models") or []
    with store.connect() as con:
        row = con.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
        if row:
            saved = json.loads(row[0])
            saved["synced_models"] = models
            saved["available_models"] = [
                {
                    "id": m,
                    "label": m,
                    "provider": saved.get("protocol") or "http_api",
                    "source": "http_api_models",
                    "runnable_directly": True,
                    "description": "通过 API /models 自动同步",
                }
                for m in models
            ]
            saved["models_synced_at"] = now()
            saved["models_refreshed_at"] = saved["models_synced_at"]
            con.execute("UPDATE agents SET data=? WHERE id=?", (dump(saved), aid))
    store.event(None, None, "agent.models_synced", {"agent": aid, "count": len(models)})
    return {"agent": aid, "models": models, "count": len(models)}


def setup_dsh(store, *, use_dsh_credentials=False, model=None, provider=None, api_key=None):
    """One-click enable DeepSeek Harness headless CLI role without restoring deleted built-ins."""
    from .discovery import discover

    found = discover()
    dsh_entry = next((a for a in found["agents"] if a["id"] == "deepseek"), None)
    exe = (dsh_entry or {}).get("candidate_executable")
    if not exe or not Path(exe).is_file():
        raise ValueError("未找到 DeepSeek Harness 无头命令行程序（dsh），请先安装 DeepSeek Harness")
    chosen_provider = (provider or "").strip() or "deepseek-official"
    chosen_model = (model or "").strip() or "deepseek-flash"
    payload = {
        "id": "hub-deepseek",
        "name": "DeepSeek Harness（无头自动执行）",
        "transport": "dsh_cli",
        "executable": exe,
        "dsh_provider": chosen_provider,
        "model": chosen_model,
        "use_dsh_credentials": bool(use_dsh_credentials),
        "auto_route": True,
        "strengths": "DeepSeek Harness 官方 headless 入口，支持沙箱文件操作与跨模型协作",
    }
    if api_key is not None:
        payload["key"] = api_key
    aid = save_agent(store, payload)
    try:
        from .model_catalog import refresh_agent_models

        refresh_agent_models(store, aid)
    except Exception:
        pass
    return aid


def agent_deleted(store, aid, deleted):
    from .core import dump, identifier

    identifier(aid)
    with idle(store), store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
        if not row:
            raise ValueError("Agent 不存在")
        a = json.loads(row[0])
        a["deleted"] = bool(deleted)
        con.execute("UPDATE agents SET data=? WHERE id=?", (dump(a), aid))
    store.event(None, None, "agent.deleted" if deleted else "agent.restored", {"agent": aid})


def save_profile(store, aid, model, strengths=""):
    from .core import dump, identifier, now

    identifier(aid)
    if not isinstance(model, str) or len(model) > 160 or any(ord(c) < 32 for c in model):
        raise ValueError("模型名称无效")
    if not isinstance(strengths, str) or len(strengths) > 2000:
        raise ValueError("能力备注最多 2000 字")
    with idle(store), store.connect() as con:
        row = con.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
        if not row:
            raise ValueError("Agent 不存在")
        a = json.loads(row[0])
        if a.get("deleted"):
            raise ValueError("请先恢复 Agent")
        if a.get("transport") == "http_api":
            raise ValueError("API 模型请在编辑 API 配置中修改")
        model = model.strip()
        if a.get("transport") in ("codex_cli", "dsh_cli", "claude_cli", "gemini_cli", "codebuddy_cli"):
            if model and not _MODEL_RE.fullmatch(model):
                raise ValueError("CLI 需要准确的模型 ID")
            if a.get("transport") == "codex_cli" and not model:
                raise ValueError("Codex 需要准确的模型 ID")
            a["execution_model"] = model or None
        if model != a.get("model", {}).get("value"):
            a.pop("observed_model", None)
            a.pop("ui_model_observation", None)
        a["model"] = {"value": model or None, "source": "user_confirmed", "confidence": "configured" if model else "unknown", "at": now()}
        a["strengths"] = strengths.strip()
        con.execute("UPDATE agents SET data=? WHERE id=?", (dump(a), aid))
    store.event(None, None, "agent.profile_saved", {"agent": aid, "model": model})


def job_deleted(store, job, deleted):
    from .core import identifier

    identifier(job)
    with idle(store), store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        if row["status"] in ("running", "pending") and not row["paused"]:
            raise ValueError("请先暂停此任务，再删除记录")
        # Restores remain paused so trash operations never execute a model.
        con.execute("UPDATE jobs SET deleted=?,paused=1,status='paused' WHERE id=?", (int(deleted), job))
    store.event(job, None, "job.deleted" if deleted else "job.restored")
