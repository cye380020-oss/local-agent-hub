"""Durable loopback Worker / MCP broker for automatic task dispatch, live status tracking, and result collection."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from .progress import ProgressTracker


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(ts: Optional[str]) -> float:
    if not ts:
        return 0.0
    try:
        parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return 0.0


def ensure_worker_token(store) -> str:
    path = store.root / ".worker-token"
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return token
    token = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(dir=str(store.root), prefix=".worker-token.")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return token


def register_heartbeat(
    store,
    agent_id: str,
    worker_id: str,
    model: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    status: str = "online",
) -> Dict[str, Any]:
    from .core import dump, identifier

    identifier(agent_id)
    worker_id = str(worker_id or "worker-" + uuid.uuid4().hex[:8])[:80]
    ts = _now_iso()
    with store.connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO worker_heartbeats(agent,worker_id,model,capabilities,status,last_seen) VALUES(?,?,?,?,?,?)",
            (agent_id, worker_id, model or "", dump(capabilities or []), str(status)[:40], ts),
        )
    return {
        "agent": agent_id,
        "worker_id": worker_id,
        "model": model,
        "capabilities": capabilities or [],
        "status": status,
        "last_seen": ts,
    }


def online_workers(store, ttl_seconds: float = 45.0) -> Dict[str, Dict[str, Any]]:
    now_ts = time.time()
    result: Dict[str, Dict[str, Any]] = {}
    with store.connect() as con:
        rows = con.execute("SELECT * FROM worker_heartbeats").fetchall()
    for row in rows:
        age = max(0.0, now_ts - _parse_iso(row["last_seen"]))
        if age <= ttl_seconds:
            result[row["agent"]] = {
                "agent": row["agent"],
                "worker_id": row["worker_id"],
                "model": row["model"] or None,
                "capabilities": json.loads(row["capabilities"]) if row["capabilities"] else [],
                "status": row["status"],
                "last_seen": row["last_seen"],
                "age_seconds": round(age, 1),
            }
    return result


def is_worker_online(store, agent_id: str, ttl_seconds: float = 45.0) -> bool:
    return agent_id in online_workers(store, ttl_seconds=ttl_seconds)


def enqueue_claim(store, job: str, task: str, attempt: int, agent_id: str) -> None:
    from .core import dump

    ts = _now_iso()
    with store.connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO worker_claims(job,task,attempt,agent,worker_id,status,progress,result,claimed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job, task, int(attempt), agent_id, "", "queued", dump({"stage": "worker:queued", "message": "等待外部 Worker 认领"}), "", "", ts),
        )


def read_claim(store, job: str, task: str, attempt: int) -> Optional[Dict[str, Any]]:
    with store.connect() as con:
        row = con.execute(
            "SELECT * FROM worker_claims WHERE job=? AND task=? AND attempt=?",
            (job, task, int(attempt)),
        ).fetchone()
    if not row:
        return None
    return {
        "job": row["job"],
        "task": row["task"],
        "attempt": row["attempt"],
        "agent": row["agent"],
        "worker_id": row["worker_id"],
        "status": row["status"],
        "progress": json.loads(row["progress"]) if row["progress"] else {},
        "result": json.loads(row["result"]) if row["result"] else None,
        "claimed_at": row["claimed_at"],
        "updated_at": row["updated_at"],
    }


def poll_tasks(store, agent_id: str, worker_id: Optional[str] = None, model: Optional[str] = None) -> List[Dict[str, Any]]:
    from .core import identifier

    identifier(agent_id)
    if worker_id:
        register_heartbeat(store, agent_id, worker_id, model=model)
    items: List[Dict[str, Any]] = []
    with store.connect() as con:
        rows = con.execute(
            "SELECT c.* FROM worker_claims c "
            "JOIN jobs j ON j.id = c.job "
            "JOIN tasks t ON t.job = c.job AND t.id = c.task "
            "WHERE c.agent=? AND c.status='queued' AND j.deleted=0 AND j.paused=0 "
            "AND t.status IN ('running', 'waiting_external') "
            "ORDER BY c.updated_at ASC",
            (agent_id,),
        ).fetchall()
    from .attachments import authorized_for, job_attachments_manifest
    agent = next((a for a in store.agents() if a["id"] == agent_id), None)
    for row in rows:
        if not authorized_for(job_attachments_manifest(store, row["job"]), agent):
            continue
        attempt_dir = store.root / "jobs" / row["job"] / row["task"] / ("attempt-%d" % row["attempt"])
        dispatch_file = attempt_dir / "dispatch.json"
        if dispatch_file.is_file():
            try:
                items.append(json.loads(dispatch_file.read_text(encoding="utf-8")))
                continue
            except (OSError, ValueError):
                pass
        handoff_file = attempt_dir / "handoff.json"
        if handoff_file.is_file():
            try:
                handoff = json.loads(handoff_file.read_text(encoding="utf-8"))
                items.append({
                    "job": row["job"],
                    "task": row["task"],
                    "attempt": row["attempt"],
                    "agent": agent_id,
                    "workspace": handoff.get("workspace"),
                    "attempt_dir": str(attempt_dir),
                    "task_spec": handoff.get("task"),
                    "prompt": (handoff.get("task") or {}).get("prompt", ""),
                })
            except (OSError, ValueError):
                pass
    return items


def claim_task(store, job: str, task: str, attempt: int, agent_id: str, worker_id: str) -> Dict[str, Any]:
    from .core import dump, identifier

    identifier(job)
    identifier(task)
    identifier(agent_id)
    ts = _now_iso()
    progress = {"stage": "worker:claimed", "message": "已被 Worker %s 认领并开始执行" % worker_id}
    with store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT status, agent FROM worker_claims WHERE job=? AND task=? AND attempt=?",
            (job, task, int(attempt)),
        ).fetchone()
        if not row or row["agent"] != agent_id:
            raise ValueError("派发任务不存在或不属于该 Agent")
        if row["status"] != "queued":
            raise ValueError("任务已被其他 Worker 认领或已结束")
        con.execute(
            "UPDATE worker_claims SET worker_id=?, status='claimed', progress=?, claimed_at=?, updated_at=? "
            "WHERE job=? AND task=? AND attempt=?",
            (worker_id, dump(progress), ts, ts, job, task, int(attempt)),
        )
    register_heartbeat(store, agent_id, worker_id, status="busy")
    attempt_dir = store.root / "jobs" / job / task / ("attempt-%d" % int(attempt))
    if attempt_dir.is_dir():
        ProgressTracker(attempt_dir, agent_id, "worker_bridge", task).update(
            status="running", stage=progress["stage"], message=progress["message"], event="claimed:" + worker_id, force=True
        )
    store.event(job, task, "worker.claimed", {"agent": agent_id, "worker_id": worker_id, "attempt": int(attempt)})
    return read_claim(store, job, task, int(attempt))


def report_progress(
    store,
    job: str,
    task: str,
    attempt: int,
    worker_id: str,
    stage: str,
    message: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    from .core import dump, identifier

    identifier(job)
    identifier(task)
    ts = _now_iso()
    progress = {"stage": str(stage)[:120], "message": str(message)[:500], "model": model}
    with store.connect() as con:
        row = con.execute(
            "SELECT agent, worker_id, status FROM worker_claims WHERE job=? AND task=? AND attempt=?",
            (job, task, int(attempt)),
        ).fetchone()
        if not row:
            raise ValueError("派发记录不存在")
        if row["status"] in ("completed", "failed"):
            raise ValueError("任务已结束，不能再上报进度")
        con.execute(
            "UPDATE worker_claims SET status='running', progress=?, updated_at=? WHERE job=? AND task=? AND attempt=?",
            (dump(progress), ts, job, task, int(attempt)),
        )
        agent_id = row["agent"]
    register_heartbeat(store, agent_id, worker_id or row["worker_id"] or "worker", model=model, status="busy")
    attempt_dir = store.root / "jobs" / job / task / ("attempt-%d" % int(attempt))
    if attempt_dir.is_dir():
        ProgressTracker(attempt_dir, agent_id, "worker_bridge", task).update(
            status="running",
            stage=progress["stage"],
            message=progress["message"],
            model_reported=model,
            event=progress["stage"] + ": " + progress["message"],
            force=True,
        )
    return read_claim(store, job, task, int(attempt))


def complete_task(
    store,
    job: str,
    task: str,
    attempt: int,
    worker_id: str,
    output: str = "",
    artifacts: Optional[List[str]] = None,
    model: Optional[str] = None,
    failed: bool = False,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    from .core import dump, identifier

    identifier(job)
    identifier(task)
    ts = _now_iso()
    status = "failed" if failed else "completed"
    payload = {
        "output": str(output or ""),
        "artifacts": [a for a in (artifacts or []) if isinstance(a, str)],
        "model": model,
        "error": str(error) if error else None,
        "worker_id": worker_id,
    }
    progress = {"stage": "worker:" + status, "message": error if failed else "Worker 已提交成果"}
    with store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT agent, status FROM worker_claims WHERE job=? AND task=? AND attempt=?",
            (job, task, int(attempt)),
        ).fetchone()
        if not row:
            raise ValueError("派发记录不存在")
        if row["status"] in ("completed", "failed"):
            raise ValueError("任务结果已提交，请勿重复提交")
        agent_id = row["agent"]
        con.execute(
            "UPDATE worker_claims SET worker_id=?, status=?, progress=?, result=?, updated_at=? "
            "WHERE job=? AND task=? AND attempt=?",
            (worker_id or "", status, dump(progress), dump(payload), ts, job, task, int(attempt)),
        )
    register_heartbeat(store, agent_id, worker_id or "worker", model=model, status="online")
    store.event(job, task, "worker." + status, {"agent": agent_id, "worker_id": worker_id, "attempt": int(attempt)})
    return read_claim(store, job, task, int(attempt))


def _http_post(base_url: str, path: str, token: str, body: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Agent-Hub-Worker-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_worker_loop(
    base_url: str,
    token: str,
    agent_id: str,
    exec_command: List[str],
    model: Optional[str] = None,
    poll_interval: float = 1.0,
    once: bool = False,
) -> None:
    """Run a local automatic worker daemon that polls, executes, tracks, and returns results."""
    worker_id = "worker-" + uuid.uuid4().hex[:8]
    print("Worker 已启动：%s -> %s (%s)" % (worker_id, agent_id, base_url), flush=True)
    while True:
        try:
            polled = _http_post(base_url, "/api/worker/poll", token, {"agent": agent_id, "worker_id": worker_id, "model": model})
            tasks = polled.get("tasks") or []
            for item in tasks:
                job, task, attempt = item["job"], item["task"], item["attempt"]
                _http_post(
                    base_url,
                    "/api/worker/claim",
                    token,
                    {"job": job, "task": task, "attempt": attempt, "agent": agent_id, "worker_id": worker_id},
                )
                _http_post(
                    base_url,
                    "/api/worker/progress",
                    token,
                    {
                        "job": job,
                        "task": task,
                        "attempt": attempt,
                        "worker_id": worker_id,
                        "stage": "worker:executing",
                        "message": "正在执行外部命令",
                        "model": model,
                    },
                )
                proc = subprocess.run(
                    exec_command,
                    input=json.dumps(item, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    cwd=item.get("workspace") or None,
                    timeout=min(3600, float((item.get("task_spec") or {}).get("timeout_seconds", 300))),
                )
                if proc.returncode != 0:
                    _http_post(
                        base_url,
                        "/api/worker/complete",
                        token,
                        {
                            "job": job,
                            "task": task,
                            "attempt": attempt,
                            "worker_id": worker_id,
                            "failed": True,
                            "error": (proc.stderr or proc.stdout or "退出码 %d" % proc.returncode)[-1000:],
                        },
                    )
                else:
                    out_text = proc.stdout.strip()
                    artifacts = []
                    reported_model = model
                    try:
                        parsed = json.loads(out_text)
                        if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                            out_text = parsed["output"]
                            artifacts = parsed.get("artifacts") or []
                            if isinstance(parsed.get("model"), str):
                                reported_model = parsed["model"]
                    except (ValueError, TypeError):
                        pass
                    _http_post(
                        base_url,
                        "/api/worker/complete",
                        token,
                        {
                            "job": job,
                            "task": task,
                            "attempt": attempt,
                            "worker_id": worker_id,
                            "output": out_text,
                            "artifacts": artifacts,
                            "model": reported_model,
                        },
                    )
                if once:
                    return
            if once:
                return
        except KeyboardInterrupt:
            print("Worker 已停止。", flush=True)
            return
        except (urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError) as exc:
            print("Worker 轮询异常：%s" % exc, file=sys.stderr, flush=True)
            if once:
                raise
        time.sleep(max(0.2, poll_interval))
