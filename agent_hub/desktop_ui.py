"""Guarded macOS Accessibility UI automation adapter for desktop-only AI apps.

Requires explicit user Accessibility permission (AXIsProcessTrusted), an unlocked
screen, and holds desktop_ui.lock so concurrent GUI tasks never collide.
Never fakes backend model verification.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
from typing import Any, Callable, Dict, Optional

from .progress import ProgressTracker


DESKTOP_UI_PROFILES = {
    "chatgpt": {"label": "ChatGPT 桌面版", "bundle_id": "com.openai.codex", "app_path": "/Applications/ChatGPT.app", "new_chat_shortcut": "cmd+n", "send_shortcut": "return"},
    "gemini": {"label": "Gemini 桌面版", "bundle_id": "com.google.GeminiMacOS", "app_path": "/Applications/Gemini.app", "new_chat_shortcut": "cmd+n", "send_shortcut": "return"},
    "workbuddy": {"label": "WorkBuddy", "bundle_id": "com.tencent.workbuddy.mac", "app_path": "/Applications/WorkBuddy.app", "new_chat_shortcut": "cmd+n", "send_shortcut": "return"},
    "deepseek": {"label": "DeepSeek Harness 桌面版", "bundle_id": "com.deepseek.dsh", "app_path": "/Applications/DeepSeek Harness.app", "new_chat_shortcut": "cmd+n", "send_shortcut": "return"},
    "doubao": {"label": "豆包桌面版", "bundle_id": "com.bot.pc.doubao", "app_path": "/Applications/Doubao.app", "new_chat_shortcut": "cmd+k", "send_shortcut": "return"},
}

ALLOWED_SHORTCUTS = {"none", "cmd+n", "cmd+k", "cmd+shift+n", "cmd+l", "return", "cmd+return"}


def driver_binary() -> Optional[Path]:
    server = Path(__file__).resolve().parents[1]
    for candidate in (server.parent / "DesktopDriver", server / "macos-app" / "DesktopDriver"):
        if candidate.is_file():
            return candidate
    return None


def check_accessibility(prompt: bool = False) -> Dict[str, Any]:
    helper = driver_binary()
    if helper is None:
        return {"ok": False, "trusted": False, "screen_locked": False, "error": "DesktopDriver 未构建；请运行 macos-app/build.py"}
    args = [str(helper), "check"] + (["--prompt"] if prompt else [])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        data = json.loads(proc.stdout or "{}")
        return data if isinstance(data, dict) else {"ok": False, "trusted": False, "error": "权限检测输出无效"}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"ok": False, "trusted": False, "error": "无法运行 DesktopDriver 权限检查"}


def _read_window_text(helper: Path, bundle_id: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run([str(helper), "read", bundle_id], capture_output=True, text=True, timeout=10, check=False)
        return json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"ok": False, "error": "读取应用窗口超时"}


def _extract_marked_reply(text: str, begin_tag: str, end_tag: str) -> Optional[str]:
    # Find the LAST occurrence of begin_tag followed by end_tag (skipping the echoed prompt itself if present).
    matches = list(re.finditer(re.escape(begin_tag) + r"\s*(.*?)\s*" + re.escape(end_tag), text, re.DOTALL))
    for match in reversed(matches):
        body = match.group(1).strip()
        if body and body != "你的完整最终回答":
            return body
    return None


def _extract_diff_reply(before: str, after: str, sent_marker: str) -> str:
    idx = after.rfind(sent_marker)
    if idx >= 0:
        tail = after[idx + len(sent_marker) :].strip()
        if tail:
            return tail
    before_lines = set(before.splitlines())
    added = [line for line in after.splitlines() if line.strip() and line not in before_lines]
    return "\n".join(added).strip()


def run_desktop_ui(
    agent: dict,
    task: dict,
    workspace: Path,
    attempt_dir: Path,
    should_pause: Callable[[], bool],
    prompt_text: str,
    base: dict,
    tracker: ProgressTracker,
) -> dict:
    from .adapters import _snapshot, _unknown_model, sanitize
    from .desktop_clients import resolve_app

    started = time.monotonic()
    helper = driver_binary()
    if helper is None:
        base.update(status="failed", error="未安装 DesktopDriver 桌面自动操作组件，请重新构建应用")
        tracker.finish("failed", base["error"])
        return base

    access = check_accessibility(prompt=False)
    if not access.get("trusted"):
        base.update(
            status="failed",
            error="未获得 macOS 辅助功能权限：请在“系统设置 → 隐私与安全性 → 辅助功能”中授权“本地 Agent 工作台”或终端后再启用桌面 UI 自动化",
        )
        tracker.finish("failed", base["error"])
        return base
    if access.get("screen_locked"):
        base.update(status="failed", error="Mac 当前处于锁屏状态，桌面 UI 自动化无法操作窗口；请解锁后重试")
        tracker.finish("failed", base["error"])
        return base

    app = resolve_app(agent)
    bundle_id = app["bundle_id"]
    app_path = app["path"]
    new_shortcut = agent.get("new_chat_shortcut") or "cmd+n"
    send_shortcut = agent.get("send_shortcut") or "return"
    if new_shortcut not in ALLOWED_SHORTCUTS:
        new_shortcut = "none"
    if send_shortcut not in {"return", "cmd+return"}:
        send_shortcut = "return"
    settle_seconds = max(2.0, min(15.0, float(agent.get("ui_settle_seconds", 4.0))))
    timeout = min(600.0, max(15.0, float(task.get("timeout_seconds", agent.get("timeout_seconds", 180)))))

    nonce = uuid.uuid4().hex[:8].upper()
    begin_tag = "<<<HUB_OUT_%s>>>" % nonce
    end_tag = "<<<HUB_END_%s>>>" % nonce
    wrapped_prompt = (
        prompt_text
        + "\n\n【工作台自动回收格式要求】请将你的最终完整回答严格包裹在以下起止标记之间（不要改写标记）：\n"
        + begin_tag
        + "\n你的完整最终回答\n"
        + end_tag
    )

    lock_root = attempt_dir.parents[3] if len(attempt_dir.parents) >= 4 else attempt_dir
    lock_file = lock_root / "desktop_ui.lock"
    tracker.update(status="running", stage="desktop_ui:waiting_lock", message="等待桌面 UI 互斥锁（串行使用前台窗口）", force=True)

    with lock_file.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if should_pause():
                    tracker.finish("paused", "等待桌面窗口锁时已暂停")
                    base.update(status="paused", error="执行前已暂停", elapsed_seconds=round(time.monotonic() - started, 3))
                    return base
                if time.monotonic() - started >= timeout:
                    tracker.finish("failed", "等待桌面窗口互斥锁超时")
                    base.update(status="failed", error="等待其他桌面 UI 任务释放前台窗口超时", elapsed_seconds=round(time.monotonic() - started, 3))
                    return base
                time.sleep(0.25)

        if should_pause():
            tracker.finish("paused", "发送前已暂停")
            base.update(status="paused", error="执行前已暂停", elapsed_seconds=round(time.monotonic() - started, 3))
            return base

        before_ws = _snapshot(workspace)
        before_read = _read_window_text(helper, bundle_id)
        before_text = before_read.get("text", "") if before_read.get("ok") else ""

        tracker.update(
            status="running",
            stage="desktop_ui:sending",
            message="正在激活 %s 并自动输入任务提示…" % app["name"],
            event="activate:" + app["name"],
            force=True,
        )
        send_proc = subprocess.run(
            [str(helper), "send", app_path, bundle_id, new_shortcut, send_shortcut],
            input=wrapped_prompt,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        try:
            send_res = json.loads(send_proc.stdout or "{}")
        except ValueError:
            send_res = {"ok": False, "error": "DesktopDriver 发送输出无效"}
        if not send_res.get("ok"):
            err = send_res.get("error") or "桌面 UI 自动发送失败"
            tracker.finish("failed", err)
            base.update(status="failed", error=sanitize(err), elapsed_seconds=round(time.monotonic() - started, 3))
            return base

        tracker.update(
            status="running",
            stage="desktop_ui:observing",
            message="已发送至 %s，正在监测回复生成并等待稳定输出…" % app["name"],
            event="sent:" + bundle_id,
            force=True,
        )
        sent_mono = time.monotonic()
        last_text = before_text
        last_change = sent_mono
        last_candidate = ""
        deadline = started + timeout

        while time.monotonic() < deadline:
            if should_pause():
                tracker.finish("paused", "已暂停桌面 UI 监听")
                base.update(status="paused", error="执行中已暂停", elapsed_seconds=round(time.monotonic() - started, 3))
                return base

            # Also allow direct result.json drop (e.g., desktop coding client wrote result.json).
            rfile = attempt_dir / "result.json"
            if rfile.is_file():
                try:
                    rf = json.loads(rfile.read_text(encoding="utf-8"))
                    if isinstance(rf, dict) and isinstance(rf.get("output"), str) and rf["output"].strip():
                        after_ws = _snapshot(workspace)
                        base.update(
                            status="completed",
                            output=sanitize(rf["output"]),
                            artifacts=sorted(set(rf.get("artifacts") or []) | {rel for rel, sig in after_ws.items() if before_ws.get(rel) != sig}),
                            model={"value": None, "source": "desktop_ui_unverified", "confidence": "unknown"},
                            elapsed_seconds=round(time.monotonic() - started, 3),
                        )
                        tracker.finish("completed", "已通过工作区 result.json 回收桌面客户端成果")
                        return base
                except (OSError, ValueError, TypeError):
                    pass

            read_res = _read_window_text(helper, bundle_id)
            if read_res.get("screen_locked"):
                base.update(status="failed", error="执行期间 Mac 锁屏，桌面 UI 观测已中止", elapsed_seconds=round(time.monotonic() - started, 3))
                tracker.finish("failed", base["error"])
                return base
            if read_res.get("ok"):
                cur_text = read_res.get("text", "")
                if cur_text != last_text:
                    last_text = cur_text
                    last_change = time.monotonic()
                marked = _extract_marked_reply(cur_text, begin_tag, end_tag)
                if marked:
                    if marked != last_candidate:
                        last_candidate = marked
                        last_change = time.monotonic()
                        tracker.update(
                            status="running",
                            stage="desktop_ui:capturing",
                            message="已检测到起止标记回复（%d 字），等待输出稳定…" % len(marked),
                            event="marked_len:%d" % len(marked),
                        )
                    elif (time.monotonic() - last_change) >= settle_seconds:
                        after_ws = _snapshot(workspace)
                        base.update(
                            status="completed",
                            output=sanitize(marked),
                            artifacts=sorted(rel for rel, sig in after_ws.items() if before_ws.get(rel) != sig),
                            model={"value": None, "source": "desktop_ui_unverified", "confidence": "unknown"},
                            desktop_ui={"app_path": app_path, "bundle_id": bundle_id, "capture_mode": "marked_ax"},
                            elapsed_seconds=round(time.monotonic() - started, 3),
                        )
                        tracker.finish("completed", "已自动从 %s 收回完整回复（%d 字）" % (app["name"], len(marked)))
                        return base
                elif (time.monotonic() - sent_mono) >= max(6.0, settle_seconds + 2.0):
                    diff_reply = _extract_diff_reply(before_text, cur_text, end_tag)
                    if len(diff_reply) >= 20 and (time.monotonic() - last_change) >= (settle_seconds + 3.0):
                        after_ws = _snapshot(workspace)
                        base.update(
                            status="completed",
                            output=sanitize(diff_reply),
                            artifacts=sorted(rel for rel, sig in after_ws.items() if before_ws.get(rel) != sig),
                            model={"value": None, "source": "desktop_ui_unverified", "confidence": "unknown"},
                            desktop_ui={"app_path": app_path, "bundle_id": bundle_id, "capture_mode": "diff_ax"},
                            elapsed_seconds=round(time.monotonic() - started, 3),
                        )
                        tracker.finish("completed", "已从 %s 窗口变更自动收回回复（%d 字）" % (app["name"], len(diff_reply)))
                        return base
            time.sleep(1.0)

        base.update(
            status="failed",
            error="桌面 UI 自动操作等待回复超时（请确认 %s 未弹窗/排队且已登录）" % app["name"],
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        tracker.finish("failed", base["error"])
        return base
