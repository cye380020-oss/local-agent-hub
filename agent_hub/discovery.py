"""Conservative local discovery; never inspect credentials, sessions, or app databases.

Only public app metadata, documented model defaults, and harmless CLI help/version
are inspected. A discovered CLI is not evidence of authentication or task success.
"""
from __future__ import annotations

import datetime as dt
import os
import platform
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from xml.parsers.expat import ExpatError


CODEX_RELATIVE = "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
DSH_RELATIVE = "Contents/Resources/runtime/cli/bin/dsh"
SOURCES = {
    "codex": "https://learn.chatgpt.com/docs/non-interactive-mode",
    "deepseek_desktop": "https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md",
    "deepseek_headless": "https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md",
    "deepseek_models": "https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-default-model/README.md",
    "workbuddy_automation": "https://www.workbuddy.ai/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Automation-Guide",
    "codebuddy_cli": "https://www.workbuddy.ai/docs/zh/cli/headless",
    "doubao": "https://www.doubao.com/",
    "claude_cli": "https://docs.anthropic.com/en/docs/claude-code/cli-reference",
    "gemini_cli": "https://github.com/google-gemini/gemini-cli",
}


def _unknown(reason: str) -> Dict[str, Any]:
    return {"value": None, "source": reason, "confidence": "unknown"}


def _safe_scalar(value: str) -> Optional[str]:
    # Deliberately supports simple model IDs only, never YAML tags or expressions.
    value = value.strip().split(" #", 1)[0].strip()
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        value = value[1:-1]
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) else None


def _codex_model(home: Path) -> Dict[str, Any]:
    path = home / "config.toml"
    try:
        # Stop at the first TOML section. Profiles/session overrides are separate.
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.lstrip().startswith("["):
                    break
                match = re.fullmatch(r"\s*model\s*=\s*(['\"])([^'\"]+)\1\s*(?:#.*)?", line.strip())
                if match and _safe_scalar(match.group(2)):
                    return {"value": match.group(2), "source": str(path) + " top-level model", "confidence": "configured_default"}
    except (OSError, UnicodeError):
        pass
    return _unknown("No simple top-level model in documented Codex config; actual run may override it.")


def _dsh_default(home: Path, profile: str) -> Dict[str, Any]:
    """Extract ONLY model/provider in the documented agent-default-model row.

    This is a narrow line reader, not a YAML loader: it never evaluates custom tags
    and never exports unrelated config fields or provider credential references.
    """
    path = home / "profiles" / profile / "cordis.patch.yml"
    block_indent: Optional[int] = None
    config_indent: Optional[int] = None
    selected = False
    values: Dict[str, str] = {}
    candidates: List[Dict[str, str]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip(" "))
                text = line.strip()
                entry = re.fullmatch(r"-\s+(id|name):\s*(.+)", text)
                if entry and (block_indent is None or indent <= block_indent):
                    if selected and values.get("model"):
                        candidates.append(values)
                    block_indent, config_indent, selected, values = indent, None, False, {}
                    name = entry.group(2).strip().strip("'\"")
                    selected = name in ("agent-default-model", "@deepseek-ai/dsh-agent-default-model")
                    continue
                if block_indent is not None and indent <= block_indent and not text.startswith("-"):
                    if selected and values.get("model"):
                        candidates.append(values)
                    block_indent, config_indent, selected, values = None, None, False, {}
                if block_indent is None:
                    continue
                name_match = re.fullmatch(r"name:\s*(.+)", text)
                if name_match and indent == block_indent + 2 and name_match.group(1).strip().strip("'\"") == "@deepseek-ai/dsh-agent-default-model":
                    selected = True
                if selected and indent == block_indent + 2 and text == "config:":
                    config_indent = indent
                    continue
                if selected and config_indent is not None and indent == config_indent + 2:
                    match = re.fullmatch(r"(model|provider):\s*(.+)", text)
                    if match:
                        safe = _safe_scalar(match.group(2))
                        if safe:
                            values[match.group(1)] = safe
        if selected and values.get("model"):
            candidates.append(values)
    except (OSError, UnicodeError):
        pass
    if len(candidates) == 1:
        selected_values = candidates[0]
        return {
            "value": selected_values["model"],
            "provider": selected_values.get("provider"),
            "source": str(path) + " agent-default-model config",
            "confidence": "configured_default",
            "profile": profile,
        }
    return _unknown("No unambiguous simple model default in documented %s profile patch." % profile)


def _app_inventory(roots: Sequence[Path]) -> List[Dict[str, Any]]:
    result = []
    for root in roots:
        try:
            entries = sorted(root.glob("*.app"))
        except OSError:
            continue
        for app_path in entries:
            try:
                with (app_path / "Contents/Info.plist").open("rb") as stream:
                    info = plistlib.load(stream)
            except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
                continue
            if not isinstance(info, dict):
                continue
            # Do not carry the full plist into output.
            raw_types = info.get("CFBundleURLTypes", [])
            schemes = []
            for row in raw_types if isinstance(raw_types, list) else []:
                raw_schemes = row.get("CFBundleURLSchemes", []) if isinstance(row, dict) else []
                if isinstance(raw_schemes, list):
                    schemes.extend(s for s in raw_schemes if isinstance(s, str))
            label = info.get("CFBundleDisplayName", info.get("CFBundleName", app_path.stem))
            result.append({
                "path": str(app_path),
                "name": label if isinstance(label, str) else app_path.stem,
                "bundle_id": info.get("CFBundleIdentifier") if isinstance(info.get("CFBundleIdentifier"), str) else None,
                "version": info.get("CFBundleShortVersionString") if isinstance(info.get("CFBundleShortVersionString"), str) else None,
                "build": str(info.get("CFBundleVersion")) if isinstance(info.get("CFBundleVersion"), (str, int)) else None,
                "url_schemes": schemes,
            })
    return result


def _find_app(apps: List[Dict[str, Any]], ids: Sequence[str], names: Sequence[str]) -> Optional[Dict[str, Any]]:
    for app in apps:
        if app["bundle_id"] in ids:
            return app
    for app in apps:
        if str(app["name"]).lower() in [name.lower() for name in names]:
            return app
    return None


def _executable(app: Optional[Dict[str, Any]], relative: str, command: str) -> Optional[str]:
    if app:
        candidate = Path(app["path"]) / relative
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    found = shutil.which(command)
    return found if found and Path(found).is_file() else None


def _probe(argv: Sequence[str], token: Optional[str] = None) -> Dict[str, Any]:
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=8, check=False)
        text = completed.stdout[:24000]
        result = {
            "ok": completed.returncode == 0 and (token is None or token in text),
            "exit_code": completed.returncode,
            "command": list(argv),
            "detail": "Help/version only; login and model execution were not tested.",
        }
        if argv[-1] == "--version":
            version = re.search(r"^codex-cli ([A-Za-z0-9.+-]+)$", text, re.MULTILINE)
            if version:
                result["version"] = version.group(1)
            else:
                generic = re.search(r"([0-9]+\.[0-9]+\.[0-9A-Za-z.+-]*)", text)
                if generic:
                    result["version"] = generic.group(1)
        return result
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "command": list(argv), "detail": "Help/version did not complete successfully."}


def discover(
    probe_cli: bool = True,
    app_roots: Optional[Sequence[Path]] = None,
    user_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return JSON-safe machine/agent facts without submitting any model task.

    Optional paths support reproducible tests. No writes, network requests, app
    launches, config dumps, or credential/session reads are performed by this code.
    A vendor CLI may perform its own incidental startup bookkeeping for --help.
    """
    home = Path(user_home) if user_home is not None else Path.home()
    roots = list(app_roots) if app_roots is not None else [Path("/Applications"), home / "Applications"]
    apps = _app_inventory([Path(p) for p in roots])
    openai_app = _find_app(apps, ["com.openai.codex", "com.openai.chat"], ["ChatGPT", "Codex"])
    workbuddy_app = _find_app(apps, ["com.tencent.workbuddy.mac"], ["WorkBuddy"])
    dsh_app = _find_app(apps, ["com.deepseek.dsh"], ["DeepSeek Harness"])
    doubao_app = _find_app(apps, ["com.bot.pc.doubao"], ["Doubao", "豆包"])
    gemini_app = _find_app(apps, ["com.google.GeminiMacOS"], ["Gemini"])
    antigravity_app = _find_app(apps, ["com.google.antigravity"], ["Antigravity"])

    codex = _executable(openai_app, CODEX_RELATIVE, "codex")
    dsh = _executable(dsh_app, DSH_RELATIVE, "dsh")
    claude_bin = shutil.which("claude")
    gemini_bin = shutil.which("gemini")
    codebuddy_bin = shutil.which("codebuddy") or shutil.which("cbc")

    codex_help = _probe([codex, "exec", "--help"], "--json") if codex and probe_cli else None
    dsh_help = _probe([dsh, "--help"], "--profile") if dsh and probe_cli else None
    dsh_headless_help = _probe([dsh, "--profile", "headless", "--help"], "--json") if dsh and probe_cli else None
    claude_probe = _probe([claude_bin, "--version"]) if claude_bin and probe_cli else None
    gemini_probe = _probe([gemini_bin, "--help"]) if gemini_bin and probe_cli else None
    codebuddy_probe = _probe([codebuddy_bin, "--help"]) if codebuddy_bin and probe_cli else None

    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) if user_home is None else home / ".codex"
    dsh_home = Path(os.environ.get("DSH_HOME", str(home / ".dsh"))) if user_home is None else home / ".dsh"

    def agent(
        identity: str,
        name: str,
        app: Optional[Dict[str, Any]],
        model: Dict[str, Any],
        status: str,
        entrypoint: Any,
        reason: str,
        capabilities: List[str],
        evidence: List[Any],
        transport: str = "file_bridge",
        executable: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = {
            "id": identity,
            "name": name,
            "installed": app is not None or executable is not None,
            "version": app.get("version") if app else None,
            "app_path": app.get("path") if app else None,
            "model": model,
            "automation": {"status": status, "entrypoint": entrypoint, "reason": reason},
            "transport": transport,
            "executable": executable,
            "capabilities": capabilities,
            "tests": [],
            "evidence": evidence,
        }
        if app:
            data["evidence"].insert(
                0,
                {
                    "source": str(Path(app["path"]) / "Contents/Info.plist"),
                    "bundle_id": app["bundle_id"],
                    "build": app["build"],
                    "url_schemes": app["url_schemes"],
                    "note": "A URL scheme is not proof of task submission support.",
                },
            )
        return data

    dsh_evidence = [
        {"source": SOURCES["deepseek_desktop"]},
        {"source": SOURCES["deepseek_headless"]},
        {"source": SOURCES["deepseek_models"], "headless_default": _dsh_default(dsh_home, "headless")},
    ]
    if dsh_help:
        dsh_evidence.append(dsh_help)
    if dsh_headless_help:
        dsh_evidence.append(dsh_headless_help)

    agents = [
        agent(
            "chatgpt",
            "ChatGPT",
            openai_app,
            _unknown("Desktop chat model is not exposed through public app metadata."),
            "session_only",
            None,
            "当前聊天可统筹、分析和交付；尚未验证可由本地进程持续调用的独立 ChatGPT 桌面入口。",
            [],
            [{"source": SOURCES["codex"], "note": "Codex CLI is a separate execution route; it is not desktop ChatGPT session control."}],
        ),
        agent(
            "codex",
            "Codex",
            openai_app,
            _codex_model(codex_home),
            "cli_available" if codex and (not probe_cli or (codex_help and codex_help["ok"])) else "not_verified",
            [codex, "exec"] if codex else None,
            "已找到官方命令入口；登录和真实任务仍需实测。模型默认值可被单次任务覆盖。",
            ["reasoning", "coding", "writing_zh", "files_read", "files_write", "review"] if codex and codex_help and codex_help["ok"] else [],
            [{"source": SOURCES["codex"]}] + ([codex_help] if codex_help else []),
            transport="codex_cli" if codex else "file_bridge",
            executable=codex,
        ),
        agent(
            "workbuddy",
            "WorkBuddy",
            workbuddy_app,
            _unknown("Selected or auto-routed Desktop model was not read from undocumented state."),
            "not_verified",
            None,
            "尚未验证外部派单入口，暂用文件交接；官方应用内支持自动化，另有独立 CodeBuddy 命令行产品。",
            [],
            [
                {"source": SOURCES["workbuddy_automation"], "scope": "In-app scheduled tasks"},
                {"source": SOURCES["codebuddy_cli"], "scope": "Separate CodeBuddy CLI", "path_binary_found": bool(codebuddy_bin)},
            ],
        ),
        agent(
            "deepseek",
            "DeepSeek Harness",
            dsh_app,
            _dsh_default(dsh_home, "desktop"),
            "cli_setup_required" if dsh and (not probe_cli or (dsh_help and dsh_help["ok"])) else "not_verified",
            [dsh, "--profile", "headless"] if dsh else None,
            "已找到官方无头入口（支持 --json 与沙箱权限预设）；可在管理面板一键启用无头自动执行。",
            [],
            dsh_evidence,
        ),
        agent(
            "doubao",
            "豆包",
            doubao_app,
            _unknown("No supported public model discovery interface verified."),
            "not_verified",
            None,
            "尚未验证官方外部派单入口，暂用文件交接；也可通过火山方舟 API 或 Worker 桥接接入。",
            [],
            [{"source": SOURCES["doubao"], "note": "Official site checked; absence of discovered docs is not proof that no automation exists."}],
        ),
    ]
    if codex:
        agents[1]["installed"] = True
        version_probe = _probe([codex, "--version"]) if probe_cli else None
        if version_probe:
            agents[1]["evidence"].append(version_probe)
            agents[1]["cli_version"] = version_probe.get("version")
    agents[1]["capability_basis"] = "Advertised general task/tool support, not measured performance; require probes before scoring."
    agents[3]["candidate_executable"] = dsh
    if dsh_headless_help:
        agents[3]["dsh_headless_supported"] = bool(dsh_headless_help.get("ok"))

    # Optional additional installed CLI / desktop clients (only appended when present)
    if claude_bin and (not probe_cli or (claude_probe and claude_probe.get("ok"))):
        agents.append(
            agent(
                "claude-cli",
                "Claude Code CLI",
                None,
                _unknown("Claude CLI default model is resolved at runtime."),
                "cli_available",
                [claude_bin, "-p", "--output-format", "stream-json"],
                "已检测到可运行的 Claude Code 命令行入口，支持自动执行与工作区文件操作。",
                ["general", "reasoning", "coding", "writing_zh", "review", "files_read", "files_write"],
                [{"source": SOURCES["claude_cli"]}] + ([claude_probe] if claude_probe else []),
                transport="claude_cli",
                executable=claude_bin,
            )
        )
    if gemini_bin and (not probe_cli or (gemini_probe and gemini_probe.get("ok"))):
        agents.append(
            agent(
                "gemini-cli",
                "Gemini CLI",
                gemini_app,
                _unknown("Gemini CLI default model is resolved at runtime."),
                "cli_available",
                [gemini_bin, "-p", "--output-format", "json"],
                "已检测到可运行的 Gemini CLI 入口，支持跨模型自动执行。",
                ["general", "reasoning", "coding", "writing_zh", "review", "files_read", "files_write"],
                [{"source": SOURCES["gemini_cli"]}] + ([gemini_probe] if gemini_probe else []),
                transport="gemini_cli",
                executable=gemini_bin,
            )
        )
    elif gemini_app:
        agents.append(
            agent(
                "gemini-app",
                "Gemini（桌面应用）",
                gemini_app,
                _unknown("Desktop app model is not exposed through public metadata."),
                "not_verified",
                None,
                "已安装 Gemini 桌面端；可使用 Google Gemini API 或安装 Gemini CLI 实现自动跨模型协作。",
                [],
                [{"source": SOURCES["gemini_cli"]}],
            )
        )
    if codebuddy_bin and (not probe_cli or (codebuddy_probe and codebuddy_probe.get("ok"))):
        agents.append(
            agent(
                "codebuddy-cli",
                "CodeBuddy CLI",
                workbuddy_app,
                _unknown("CodeBuddy CLI model is resolved at runtime."),
                "cli_available",
                [codebuddy_bin, "-p", "--output-format", "json"],
                "已检测到可运行的 CodeBuddy / WorkBuddy CLI 入口。",
                ["general", "reasoning", "coding", "writing_zh", "review", "files_read", "files_write"],
                [{"source": SOURCES["codebuddy_cli"]}] + ([codebuddy_probe] if codebuddy_probe else []),
                transport="codebuddy_cli",
                executable=codebuddy_bin,
            )
        )
    if antigravity_app:
        agents.append(
            agent(
                "antigravity",
                "Antigravity",
                antigravity_app,
                _unknown("Desktop app model is not exposed through public metadata."),
                "not_verified",
                None,
                "已安装 Antigravity；可通过扩展 MCP（--extended）或 Worker 桥接实现自动接单与回收。",
                [],
                [],
            )
        )

    system = {
        "os": platform.system(),
        "os_version": platform.mac_ver()[0] or platform.release(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "discovered_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "safety": "No credentials, session databases, or private app state inspected; no model requests submitted.",
    }
    if platform.system() == "Darwin":
        try:
            cpu = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2, check=False)
            if cpu.returncode == 0:
                system["cpu"] = cpu.stdout.strip()[:100]
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not system.get("cpu"):
            try:
                import json

                # Only chip_type is retained; machine identifiers are never returned.
                hardware = subprocess.run(
                    ["/usr/sbin/system_profiler", "SPHardwareDataType", "-json"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                if hardware.returncode == 0:
                    for row in json.loads(hardware.stdout).get("SPHardwareDataType", []):
                        chip = row.get("chip_type") if isinstance(row, dict) else None
                        if isinstance(chip, str) and re.fullmatch(r"[A-Za-z0-9 .()+-]{1,100}", chip):
                            system["cpu"] = chip
                            break
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
                pass
    return {"system": system, "agents": agents}


if __name__ == "__main__":
    import json

    print(json.dumps(discover(), ensure_ascii=False, indent=2))
