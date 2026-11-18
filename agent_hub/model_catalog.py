"""Automatic, credential-free model catalog discovery and user model selection for desktop & CLI agents.

Never reads auth tokens, credentials, cookies, or session databases.
Only reads documented public caches/configs (e.g. ~/.codex/models_cache.json,
~/.codex/config.toml, ~/.dsh/profiles/*/cordis.patch.yml non-secret fields),
official CLI schema/help output (e.g. `dsh --profile headless --dump-config-schema`),
and optional Accessibility window text when explicitly trusted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional

from .core import dump, identifier, now


_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}$")
_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SECRET_LINE_RE = re.compile(r"(?i)\b(api[_-]?key|secret|token|password|authorization|credential|cookie)\b")

# Reference lists for GUI-only clients that do not expose a machine-readable model API.
# Always tagged honestly as source="reference_catalog" (not runtime-verified).
DESKTOP_REFERENCE_MODELS: Dict[str, List[Dict[str, Any]]] = {
    "com.google.GeminiMacOS": [
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "google-gemini"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "google-gemini"},
    ],
    "com.openai.codex": [
        {"id": "gpt-6.2-astra", "label": "GPT-6.2-Astra", "provider": "openai-codex"},
        {"id": "gpt-6-astra", "label": "GPT-6-Astra", "provider": "openai-codex"},
        {"id": "o3", "label": "o3", "provider": "openai-codex"},
    ],
    "com.tencent.workbuddy.mac": [
        {"id": "kimi-k3", "label": "Kimi K3", "provider": "workbuddy"},
        {"id": "deepseek-v4", "label": "DeepSeek V4", "provider": "workbuddy"},
        {"id": "hunyuan-t1", "label": "混元 T1", "provider": "workbuddy"},
    ],
    "com.bot.pc.doubao": [
        {"id": "doubao-1.5-pro", "label": "豆包 1.5 Pro", "provider": "bytedance-doubao"},
        {"id": "doubao-1.5-thinking-pro", "label": "豆包深度思考 Pro", "provider": "bytedance-doubao"},
    ],
    "com.google.antigravity": [
        {"id": "gemini-4-argon", "label": "Gemini 4 Argon", "provider": "antigravity"},
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "antigravity"},
    ],
}

CLI_REFERENCE_MODELS: Dict[str, List[Dict[str, Any]]] = {
    "claude_cli": [
        {"id": "sonnet", "label": "Claude Sonnet（CLI 别名）", "provider": "anthropic"},
        {"id": "opus", "label": "Claude Opus（CLI 别名）", "provider": "anthropic"},
        {"id": "haiku", "label": "Claude Haiku（CLI 别名）", "provider": "anthropic"},
    ],
    "gemini_cli": [
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "google-gemini"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "google-gemini"},
    ],
    "codebuddy_cli": [
        {"id": "deepseek-v3", "label": "DeepSeek V3", "provider": "codebuddy"},
        {"id": "hunyuan-t1", "label": "混元 T1", "provider": "codebuddy"},
    ],
}

# Patterns for optional best-effort model label extraction from an unlocked Accessibility window.
_UI_MODEL_PATTERNS = [
    re.compile(r"\b(gpt-[0-9]+(?:\.[0-9]+)?(?:-[a-z0-9]+)*)\b", re.IGNORECASE),
    re.compile(r"\b(o[134](?:-(?:mini|pro|high))?)\b", re.IGNORECASE),
    re.compile(r"\b(claude-(?:sonnet|opus|haiku|3\.[57]|4(?:\.[0-9]+)?)[A-Za-z0-9._-]*)\b", re.IGNORECASE),
    re.compile(r"\b(gemini-[0-9]+(?:\.[0-9]+)?(?:-[a-z0-9]+)*)\b", re.IGNORECASE),
    re.compile(r"\b(deepseek-[a-z0-9._-]+)\b", re.IGNORECASE),
    re.compile(r"\b(kimi-[a-z0-9._-]+)\b", re.IGNORECASE),
    re.compile(r"\b(qwen[0-9a-z._-]*)\b", re.IGNORECASE),
    re.compile(r"\b(doubao-[a-z0-9._-]+)\b", re.IGNORECASE),
]


def _clean_scalar(raw: str) -> Optional[str]:
    val = str(raw or "").strip().split(" #", 1)[0].strip()
    if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
        val = val[1:-1].strip()
    return val if _SAFE_MODEL_RE.fullmatch(val) else None


def _normalize_entry(
    mid: str,
    *,
    label: Optional[str] = None,
    provider: Optional[str] = None,
    source: str = "detected",
    runnable_directly: bool = True,
    description: str = "",
) -> Optional[Dict[str, Any]]:
    clean_id = _clean_scalar(mid)
    if not clean_id:
        return None
    clean_provider = provider.strip() if isinstance(provider, str) and _SAFE_PROVIDER_RE.fullmatch(provider.strip()) else None
    clean_label = str(label or clean_id).strip()[:120]
    clean_desc = str(description or "").strip()[:240]
    return {
        "id": clean_id,
        "label": clean_label or clean_id,
        "provider": clean_provider,
        "source": source,
        "runnable_directly": bool(runnable_directly),
        "description": clean_desc,
    }


def _dedupe_models(entries: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    source_rank = {
        "codex_models_cache": 50,
        "dsh_schema_builtin": 50,
        "http_api_models": 50,
        "dsh_profile_config": 40,
        "codex_config_toml": 35,
        "desktop_ui_scan": 30,
        "runtime_observed": 25,
        "user_selected": 20,
        "reference_catalog": 10,
    }
    for item in entries:
        if not item:
            continue
        key = "%s::%s" % (item["id"], item.get("provider") or "")
        # Merge same model id if one has no provider and another does.
        alt_key = "%s::" % item["id"]
        if item.get("provider") and alt_key in by_key:
            del by_key[alt_key]
            order = [k for k in order if k != alt_key]
        elif not item.get("provider") and any(k.startswith(item["id"] + "::") for k in by_key):
            continue
        if key not in by_key:
            by_key[key] = item
            order.append(key)
        else:
            existing = by_key[key]
            if source_rank.get(item["source"], 0) > source_rank.get(existing["source"], 0):
                by_key[key] = item
            else:
                if existing["label"] == existing["id"] and item["label"] != item["id"]:
                    existing["label"] = item["label"]
                if not existing["description"] and item["description"]:
                    existing["description"] = item["description"]
    return [by_key[k] for k in order]


def discover_codex_models(codex_home: Optional[Path] = None) -> Dict[str, Any]:
    """Read official Codex public model cache (~/.codex/models_cache.json) and config.toml default."""
    home = Path(codex_home) if codex_home is not None else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    items: List[Optional[Dict[str, Any]]] = []
    sources: List[str] = []
    fetched_at: Optional[str] = None

    for cache_name in ("models_cache.json", "models_cache.codex_work_desktop.json"):
        cache_path = home / cache_name
        if not cache_path.is_file() or cache_path.is_symlink():
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            continue
        fetched_at = payload.get("fetched_at") if isinstance(payload.get("fetched_at"), str) else fetched_at
        visible = [m for m in raw_models if isinstance(m, dict) and m.get("visibility") != "hide"]
        chosen = visible if visible else [m for m in raw_models if isinstance(m, dict)]
        for m in chosen:
            slug = m.get("slug") or m.get("id")
            if not isinstance(slug, str):
                continue
            disp = m.get("display_name") if isinstance(m.get("display_name"), str) else slug
            desc = m.get("description") if isinstance(m.get("description"), str) else ""
            items.append(
                _normalize_entry(
                    slug,
                    label=disp,
                    provider="openai-codex",
                    source="codex_models_cache",
                    runnable_directly=True,
                    description=desc,
                )
            )
        sources.append(str(cache_path))
        if items:
            break

    # Also read top-level model in config.toml so configured default is always included.
    cfg_path = home / "config.toml"
    if cfg_path.is_file() and not cfg_path.is_symlink():
        try:
            with cfg_path.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.lstrip().startswith("["):
                        break
                    m = re.fullmatch(r"\s*model\s*=\s*(['\"])([^'\"]+)\1\s*(?:#.*)?", line.strip())
                    if m:
                        items.append(
                            _normalize_entry(
                                m.group(2),
                                label=m.group(2),
                                provider="openai-codex",
                                source="codex_config_toml",
                                runnable_directly=True,
                                description="~/.codex/config.toml 默认配置模型",
                            )
                        )
                        sources.append(str(cfg_path))
                        break
        except (OSError, UnicodeError):
            pass

    models = _dedupe_models(items)
    return {
        "models": models,
        "sources": sources,
        "fetched_at": fetched_at,
        "note": (
            "已从 Codex 官方模型缓存自动识别 %d 个可选模型" % len(models)
            if models
            else "未找到 Codex 官方模型缓存文件（~/.codex/models_cache.json），请先登录或运行一次 Codex"
        ),
    }


def _extract_dsh_schema_models(schema: Any) -> List[Dict[str, Any]]:
    found: List[Optional[Dict[str, Any]]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            props = obj.get("properties")
            if isinstance(props, dict) and isinstance(props.get("models"), dict):
                default_list = props["models"].get("default")
                if isinstance(default_list, list):
                    for entry in default_list:
                        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                            ctx_win = entry.get("contextWindow")
                            desc = str(entry.get("description") or "").strip()
                            if isinstance(ctx_win, int) and ctx_win > 0:
                                win_note = "上下文 %dK" % (ctx_win // 1024)
                                desc = ("%s；%s" % (win_note, desc)) if desc else win_note
                            found.append(
                                _normalize_entry(
                                    entry["id"],
                                    label=entry.get("name") or entry["id"],
                                    provider="deepseek-official",
                                    source="dsh_schema_builtin",
                                    runnable_directly=True,
                                    description=desc,
                                )
                            )
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for v in obj[:200]:
                if isinstance(v, (dict, list)):
                    walk(v)

    walk(schema)
    return _dedupe_models(found)


def _scan_dsh_profile_models(dsh_home: Path) -> List[Dict[str, Any]]:
    """Safely extract ONLY non-secret provider/model IDs from public cordis.patch.yml files."""
    found: List[Optional[Dict[str, Any]]] = []
    for prof in ("headless", "desktop", "web"):
        path = dsh_home / "profiles" / prof / "cordis.patch.yml"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        in_pi_ai = False
        pi_indent = -1
        in_providers = False
        prov_indent = -1
        cur_provider: Optional[str] = None
        cur_provider_indent = -1
        in_models = False
        models_indent = -1
        pending_id: Optional[str] = None
        pending_name: Optional[str] = None

        def flush_pending() -> None:
            nonlocal pending_id, pending_name
            if pending_id and cur_provider:
                found.append(
                    _normalize_entry(
                        pending_id,
                        label=pending_name or pending_id,
                        provider=cur_provider,
                        source="dsh_profile_config",
                        runnable_directly=(prof == "headless" or cur_provider == "deepseek-official"),
                        description="来自 DeepSeek Harness %s 配置（provider: %s）" % (prof, cur_provider),
                    )
                )
            pending_id = None
            pending_name = None

        for raw_line in lines:
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            if _SECRET_LINE_RE.search(raw_line):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            text = raw_line.strip()

            if re.match(r"-\s+id:\s*llm-pi-ai\b", text):
                flush_pending()
                in_pi_ai, pi_indent = True, indent
                in_providers = in_models = False
                cur_provider = None
                continue
            if in_pi_ai and indent <= pi_indent and text.startswith("- "):
                flush_pending()
                in_pi_ai = in_providers = in_models = False
                cur_provider = None

            if not in_pi_ai:
                continue

            if text == "providers:":
                flush_pending()
                in_providers, prov_indent = True, indent
                in_models = False
                cur_provider = None
                continue
            if in_providers and indent <= prov_indent:
                flush_pending()
                in_providers = in_models = False
                cur_provider = None
                continue
            if not in_providers:
                continue

            if indent == prov_indent + 2 and text.endswith(":") and not text.startswith("-"):
                flush_pending()
                cand_prov = text[:-1].strip().strip("'\"")
                cur_provider = cand_prov if _SAFE_PROVIDER_RE.fullmatch(cand_prov) else None
                cur_provider_indent = indent
                in_models = False
                continue

            if cur_provider and indent == cur_provider_indent + 2 and text == "models:":
                flush_pending()
                in_models, models_indent = True, indent
                continue
            if in_models and indent <= models_indent:
                flush_pending()
                in_models = False
                continue
            if not in_models:
                continue

            m_id = re.fullmatch(r"-\s+id:\s*(.+)", text)
            if m_id:
                flush_pending()
                pending_id = _clean_scalar(m_id.group(1))
                continue
            m_name = re.fullmatch(r"name:\s*(.+)", text)
            if m_name and pending_id:
                pending_name = m_name.group(1).strip().strip("'\"")

        flush_pending()
    return _dedupe_models(found)


def discover_dsh_models(executable: Optional[str] = None, dsh_home: Optional[Path] = None) -> Dict[str, Any]:
    """Read official DeepSeek Harness built-in models via --dump-config-schema + public profile configs."""
    home = Path(dsh_home) if dsh_home is not None else Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    items: List[Optional[Dict[str, Any]]] = []
    sources: List[str] = []

    if executable and Path(executable).is_file():
        try:
            proc = subprocess.run(
                [executable, "--profile", "headless", "--dump-config-schema"],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                schema = json.loads(proc.stdout)
                schema_models = _extract_dsh_schema_models(schema)
                items.extend(schema_models)
                if schema_models:
                    sources.append("dsh --profile headless --dump-config-schema")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

    profile_models = _scan_dsh_profile_models(home)
    if profile_models:
        items.extend(profile_models)
        sources.append(str(home / "profiles"))

    models = _dedupe_models(items)
    return {
        "models": models,
        "sources": sources,
        "note": (
            "已从 DeepSeek Harness 官方 Schema 与配置自动识别 %d 个模型" % len(models)
            if models
            else "未自动探测到 DeepSeek Harness 模型，请确认已安装 DeepSeek Harness"
        ),
    }


def _scan_desktop_window_models(bundle_id: Optional[str]) -> List[Dict[str, Any]]:
    """Best-effort, read-only model tag detection from an open desktop app when Accessibility is already granted."""
    if not bundle_id:
        return []
    from .desktop_ui import check_accessibility, driver_binary

    ax = check_accessibility(prompt=False)
    if not ax.get("trusted") or ax.get("screen_locked"):
        return []
    helper = driver_binary()
    if helper is None:
        return []
    try:
        proc = subprocess.run([str(helper), "read", bundle_id], capture_output=True, text=True, timeout=6, check=False)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    text = data.get("text", "") if isinstance(data, dict) and data.get("ok") else ""
    if not text:
        return []
    found: List[Optional[Dict[str, Any]]] = []
    for pat in _UI_MODEL_PATTERNS:
        for m in pat.finditer(text):
            token = m.group(1)
            found.append(
                _normalize_entry(
                    token,
                    label=token,
                    source="desktop_ui_scan",
                    runnable_directly=True,
                    description="从当前桌面应用窗口文本识别（仅供参考，不证明后端模型）",
                )
            )
    return _dedupe_models(found)


def _bundle_id_for_agent(agent: Dict[str, Any]) -> Optional[str]:
    if isinstance(agent.get("desktop_bundle_id"), str) and agent["desktop_bundle_id"]:
        return agent["desktop_bundle_id"]
    for ev in agent.get("evidence") or []:
        if isinstance(ev, dict) and isinstance(ev.get("bundle_id"), str):
            return ev["bundle_id"]
    app_path = agent.get("app_path")
    if isinstance(app_path, str) and app_path:
        low = app_path.lower()
        if "gemini" in low:
            return "com.google.GeminiMacOS"
        if "chatgpt" in low:
            return "com.openai.codex"
        if "workbuddy" in low:
            return "com.tencent.workbuddy.mac"
        if "doubao" in low:
            return "com.bot.pc.doubao"
        if "antigravity" in low:
            return "com.google.antigravity"
        if "deepseek" in low:
            return "com.deepseek.dsh"
    return None


def probe_models_for_agent(
    agent: Dict[str, Any],
    *,
    codex_home: Optional[Path] = None,
    dsh_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Discover available models for a single agent record (no network推理, no credentials read)."""
    transport = agent.get("transport") or "file_bridge"
    items: List[Optional[Dict[str, Any]]] = []
    sources: List[str] = []
    note = ""

    if transport == "codex_cli":
        res = discover_codex_models(codex_home=codex_home)
        items.extend(res["models"])
        sources.extend(res["sources"])
        note = res["note"]
    elif transport == "dsh_cli" or (agent.get("id") == "deepseek" and agent.get("candidate_executable")):
        exe = agent.get("executable") or agent.get("candidate_executable")
        res = discover_dsh_models(executable=exe, dsh_home=dsh_home)
        items.extend(res["models"])
        sources.extend(res["sources"])
        note = res["note"]
    elif transport == "http_api":
        for m in agent.get("synced_models") or []:
            if isinstance(m, str):
                items.append(_normalize_entry(m, label=m, provider=agent.get("protocol") or "http_api", source="http_api_models"))
        note = "使用 API /models 已同步模型列表（可点“同步可用模型”在线拉取最新）"
    elif transport in CLI_REFERENCE_MODELS:
        for ref in CLI_REFERENCE_MODELS[transport]:
            items.append(
                _normalize_entry(
                    ref["id"],
                    label=ref["label"],
                    provider=ref.get("provider"),
                    source="reference_catalog",
                    runnable_directly=True,
                    description="该 CLI 常用公开模型标识，可手动添加更多",
                )
            )
        sources.append("cli_reference_catalog")
        note = "已载入 %s 常用可选模型（支持随时手动输入自定义模型 ID）" % transport
    elif transport in ("desktop_ui", "file_bridge", "worker_bridge"):
        bundle_id = _bundle_id_for_agent(agent)
        if transport == "desktop_ui":
            scanned = _scan_desktop_window_models(bundle_id)
            if scanned:
                items.extend(scanned)
                sources.append("desktop_ui_window_scan")
        if bundle_id == "com.deepseek.dsh":
            dsh_exe = None
            if isinstance(agent.get("app_path"), str) and agent["app_path"]:
                cand = Path(agent["app_path"]) / "Contents/Resources/runtime/cli/bin/dsh"
                if cand.is_file():
                    dsh_exe = str(cand)
            d_res = discover_dsh_models(executable=dsh_exe, dsh_home=dsh_home)
            items.extend(d_res["models"])
            sources.extend(d_res["sources"])
        elif bundle_id == "com.openai.codex":
            c_res = discover_codex_models(codex_home=codex_home)
            items.extend(c_res["models"])
            sources.extend(c_res["sources"])
        if bundle_id and bundle_id in DESKTOP_REFERENCE_MODELS:
            for ref in DESKTOP_REFERENCE_MODELS[bundle_id]:
                items.append(
                    _normalize_entry(
                        ref["id"],
                        label=ref["label"],
                        provider=ref.get("provider"),
                        source="reference_catalog",
                        runnable_directly=True,
                        description="桌面客户端参考模型列表（可自主选择或输入自定义模型）",
                    )
                )
            sources.append("desktop_reference_catalog:" + bundle_id)
        note = (
            "已识别 %d 个桌面端可选模型（支持自主选择或补充自定义模型）" % len(_dedupe_models(items))
            if items
            else "该桌面客户端未暴露公开模型目录；可手动输入并保存自定义模型列表"
        )

    # Preserve existing user-added / runtime-observed models (drop stale reference_catalog copies).
    for existing in agent.get("available_models") or []:
        if isinstance(existing, dict) and isinstance(existing.get("id"), str) and existing.get("source") != "reference_catalog":
            items.append(
                _normalize_entry(
                    existing["id"],
                    label=existing.get("label"),
                    provider=existing.get("provider"),
                    source=existing.get("source") or "user_selected",
                    runnable_directly=existing.get("runnable_directly", True),
                    description=existing.get("description", ""),
                )
            )
    current = agent.get("execution_model") or (agent.get("model", {}).get("value") if isinstance(agent.get("model"), dict) else None)
    if isinstance(current, str) and current:
        items.append(
            _normalize_entry(
                current,
                label=current,
                provider=agent.get("dsh_provider"),
                source="user_selected",
                runnable_directly=True,
            )
        )
    observed = (agent.get("observed_model") or {}).get("value") if isinstance(agent.get("observed_model"), dict) else None
    if isinstance(observed, str) and observed:
        items.append(_normalize_entry(observed, label=observed, source="runtime_observed", runnable_directly=True))

    models = _dedupe_models(items)
    return {
        "agent": agent["id"],
        "models": models,
        "count": len(models),
        "sources": sources,
        "note": note,
        "refreshed_at": now(),
    }


def refresh_agent_models(
    store,
    agent_id: Optional[str] = None,
    *,
    include_http_api: bool = False,
    codex_home: Optional[Path] = None,
    dsh_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Scan and persist available model catalogs for one agent or all active agents."""
    from .management import sync_api_models

    if agent_id is not None:
        identifier(agent_id)
    agents = store.agents()
    targets = [a for a in agents if agent_id is None or a["id"] == agent_id]
    if agent_id is not None and not targets:
        raise ValueError("Agent 不存在")

    summary: List[Dict[str, Any]] = []
    for a in targets:
        aid = a["id"]
        if a.get("transport") == "http_api" and (include_http_api or agent_id is not None):
            try:
                sync_api_models(store, aid)
                a = next(x for x in store.agents() if x["id"] == aid)
            except Exception:
                pass
        probed = probe_models_for_agent(a, codex_home=codex_home, dsh_home=dsh_home)
        with store.connect() as con:
            row = con.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
            if row:
                saved = json.loads(row[0])
                saved["available_models"] = probed["models"]
                saved["models_refreshed_at"] = probed["refreshed_at"]
                saved["models_sources"] = probed["sources"]
                con.execute("UPDATE agents SET data=? WHERE id=?", (dump(saved), aid))
        summary.append({
            "agent": aid,
            "name": a.get("name", aid),
            "count": probed["count"],
            "models": [m["id"] for m in probed["models"]],
            "note": probed["note"],
        })

    total = sum(x["count"] for x in summary)
    store.event(
        None,
        None,
        "agents.models_refreshed",
        {"agent": agent_id or "all", "updated_agents": len(summary), "total_models": total},
    )
    return {
        "updated_agents": len(summary),
        "total_models": total,
        "results": summary,
    }


def select_agent_model(
    store,
    agent_id: str,
    model_id: str,
    *,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist the user's chosen model for any desktop, CLI, API, or Worker agent."""
    from .management import idle

    identifier(agent_id)
    clean_model = _clean_scalar(model_id)
    if not clean_model:
        raise ValueError("模型标识格式不正确（仅支持字母、数字及 . _ : / @ -）")
    clean_provider = None
    if provider is not None and str(provider).strip():
        p = str(provider).strip()
        if not _SAFE_PROVIDER_RE.fullmatch(p):
            raise ValueError("Provider 标识格式不正确")
        clean_provider = p

    with idle(store):
        with store.connect() as con:
            row = con.execute("SELECT data FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise ValueError("Agent 不存在")
            agent = json.loads(row[0])
            if agent.get("deleted"):
                raise ValueError("已删除的 Agent 需先恢复才能切换模型")

            transport = agent.get("transport")
            # Auto-fill provider from discovered catalog if caller did not explicitly pass one.
            catalog_match = next(
                (m for m in (agent.get("available_models") or []) if isinstance(m, dict) and m.get("id") == clean_model),
                None,
            )
            if clean_provider is None and catalog_match and catalog_match.get("provider"):
                clean_provider = catalog_match["provider"]

            if transport in ("codex_cli", "dsh_cli", "claude_cli", "gemini_cli", "codebuddy_cli", "custom_cli"):
                agent["execution_model"] = clean_model
                agent["model"] = {
                    "value": clean_model,
                    "source": "user_selected",
                    "confidence": "configured",
                }
                if transport == "dsh_cli" and clean_provider:
                    agent["dsh_provider"] = clean_provider
                    agent["model"]["provider"] = clean_provider
            elif transport == "http_api":
                agent["model"] = {"value": clean_model, "source": "user_selected", "confidence": "configured"}
            else:
                # file_bridge / desktop_ui / worker_bridge
                agent["model"] = {
                    "value": clean_model,
                    "source": "user_confirmed",
                    "confidence": "user_confirmed",
                }

            # Clear stale runtime observation from a different previous model so UI/cross-model checks stay honest.
            prev_obs = (agent.get("observed_model") or {}).get("value") if isinstance(agent.get("observed_model"), dict) else None
            if prev_obs and prev_obs != clean_model:
                agent.pop("observed_model", None)

            # Ensure the newly chosen model is preserved in available_models for future quick switching.
            existing = [m for m in (agent.get("available_models") or []) if isinstance(m, dict)]
            existing.append(
                _normalize_entry(
                    clean_model,
                    label=(catalog_match.get("label") if catalog_match else clean_model),
                    provider=clean_provider,
                    source=(catalog_match.get("source") if catalog_match else "user_selected"),
                    runnable_directly=(catalog_match.get("runnable_directly", True) if catalog_match else True),
                    description=(catalog_match.get("description", "") if catalog_match else "用户自主选择"),
                )
            )
            agent["available_models"] = _dedupe_models(existing)
            con.execute("UPDATE agents SET data=? WHERE id=?", (dump(agent), agent_id))

    store.event(
        None,
        None,
        "agent.model_selected",
        {"agent": agent_id, "model": clean_model, "provider": clean_provider},
    )
    return {
        "agent": agent_id,
        "model": clean_model,
        "provider": clean_provider,
    }
