"""Atomic per-attempt live execution progress tracking (stdlib only)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, Optional


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _redact(text: str) -> str:
    value = str(text)
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    value = re.sub(
        r'''(?i)(["']?(?:OPENAI_API_KEY|CODEX_API_KEY|DEEPSEEK_API_KEY|GEMINI_API_KEY|ANTHROPIC_API_KEY|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|authorization)["']?\s*[:=]\s*)(["']?)([^\s,"'}]+)(["']?)''',
        lambda m: m.group(1) + m.group(2) + "[REDACTED]" + m.group(4),
        value,
    )
    return value


class ProgressTracker:
    """Writes live execution snapshots to attempt_dir/progress.json atomically."""

    def __init__(
        self,
        attempt_dir: Optional[Path],
        agent_id: str,
        transport: str,
        task_id: Optional[str] = None,
        min_interval: float = 0.25,
    ):
        self.path = (Path(attempt_dir) / "progress.json") if attempt_dir is not None else None
        self.started_mono = time.monotonic()
        self.min_interval = min_interval
        self._last_flush = 0.0
        self._lock = threading.Lock()
        self.state: Dict[str, Any] = {
            "task": task_id,
            "agent": agent_id,
            "transport": transport,
            "status": "starting",
            "stage": "initializing",
            "message": "正在准备执行环境",
            "model_reported": None,
            "pid": None,
            "started_at": _iso_now(),
            "updated_at": _iso_now(),
            "elapsed_seconds": 0.0,
            "events_count": 0,
            "recent_events": [],
        }
        self._flush(force=True)

    def update(
        self,
        *,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        model_reported: Optional[str] = None,
        pid: Optional[int] = None,
        event: Optional[str] = None,
        force: bool = False,
    ) -> None:
        with self._lock:
            now_iso = _iso_now()
            if status is not None:
                self.state["status"] = str(status)
            if stage is not None:
                self.state["stage"] = str(stage)[:120]
            if message is not None:
                self.state["message"] = _redact(message)[:500]
            if model_reported is not None:
                self.state["model_reported"] = _redact(model_reported)[:160]
            if pid is not None:
                self.state["pid"] = int(pid)
            if event:
                self.state["events_count"] += 1
                recent = self.state["recent_events"]
                recent.append({"at": now_iso, "text": _redact(event)[:240]})
                if len(recent) > 12:
                    del recent[:-12]
            self.state["updated_at"] = now_iso
            self.state["elapsed_seconds"] = round(time.monotonic() - self.started_mono, 2)
            self._flush(force=force)

    def finish(self, status: str, message: str = "", model_reported: Optional[str] = None) -> None:
        self.update(
            status=status,
            stage=status,
            message=message or status,
            model_reported=model_reported,
            event="finish:" + status,
            force=True,
        )

    def _flush(self, force: bool = False) -> None:
        if self.path is None:
            return
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_flush) < self.min_interval:
            return
        self._last_flush = now_mono
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


def read_progress(attempt_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(attempt_dir) / "progress.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None
