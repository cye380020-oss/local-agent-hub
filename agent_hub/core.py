"""Durable DAG scheduler, parallel execution, artifact verification, auto-collection, and capability routing (stdlib only)."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid

from .adapters import run_adapter
from .progress import read_progress


_TEXT_ARTIFACT_EXTS = {
    ".md", ".txt", ".json", ".csv", ".tsv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".css", ".yml", ".yaml", ".xml", ".sql", ".sh", ".toml", ".ini", ".log",
}
_MAX_PREVIEW_FILE_BYTES = 16 * 1024
_MAX_PREVIEW_TOTAL_BYTES = 64 * 1024


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("标识只能含字母、数字、下划线和连字符，最多 80 字符")
    return value


def inside(root, relative):
    root = Path(root).resolve()
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError("文件路径必须位于任务工作区内")
    result = root / p
    for parent in [result, *result.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("成果不接受符号链接")
    resolved = result.resolve()
    if root not in resolved.parents:
        raise ValueError("文件越出工作区")
    return resolved


def response_json(output):
    value = str(output).strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value)
    return json.loads(value)


def check_validation(spec, artifact=False):
    allowed = {"json_equals", "text_contains", "artifacts", "review_accept", "min_chars"}
    if artifact:
        allowed = {"path", "min_bytes", "json_equals", "text_contains"}
    if not isinstance(spec, dict) or not spec or set(spec) - allowed:
        raise ValueError("验收条件含未知字段或不是有效对象")
    active = False
    if "min_chars" in spec:
        if type(spec["min_chars"]) is not int or not 1 <= spec["min_chars"] <= 10000:
            raise ValueError("min_chars 必须为 1–10000 的整数")
        active = True
    if "json_equals" in spec:
        if not isinstance(spec["json_equals"], dict) or not spec["json_equals"]:
            raise ValueError("json_equals 必须是非空对象")
        active = True
    if "text_contains" in spec:
        phrases = spec["text_contains"]
        if not isinstance(phrases, list) or not phrases or not all(isinstance(p, str) and p.strip() for p in phrases):
            raise ValueError("text_contains 必须是非空文字数组")
        active = True
    if "review_accept" in spec:
        if spec["review_accept"] is not True:
            raise ValueError("review_accept 必须为 true")
        active = True
    if "artifacts" in spec:
        if not isinstance(spec["artifacts"], list) or not spec["artifacts"]:
            raise ValueError("artifacts 必须是非空成果要求数组")
        for requirement in spec["artifacts"]:
            check_validation(requirement, artifact=True)
        active = True
    if artifact:
        if not isinstance(spec.get("path"), str):
            raise ValueError("成果必须声明相对 path")
        if (
            not isinstance(spec.get("min_bytes", 1), int)
            or isinstance(spec.get("min_bytes", 1), bool)
            or not 1 <= spec.get("min_bytes", 1) <= 20 * 1024 * 1024
        ):
            raise ValueError("成果 min_bytes 必须为 1–20971520 的整数")
        active = True
    if not active:
        raise ValueError("至少需要一项实际验收条件")


def validate(task, result, workspace):
    """Checks are supplied by the trusted plan, never by an agent's success claim."""
    spec = task.get("validation", {})
    errors, artifacts = [], []
    output = result.get("output", "")
    if not isinstance(output, str) or not output.strip():
        errors.append("没有文本成果")
    if len(str(output).strip()) < spec.get("min_chars", 1):
        errors.append("成果内容过短")
    if task.get("workflow_role"):
        from .workflow import validate_contract

        try:
            validate_contract(task, output)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append("协作输出检查失败：" + str(exc))
    if "json_equals" in spec:
        try:
            actual = response_json(output)
            for key, value in spec["json_equals"].items():
                if not isinstance(actual, dict) or actual.get(key) != value:
                    errors.append("JSON 字段不符合要求: " + key)
        except (ValueError, TypeError):
            errors.append("输出不是合法 JSON")
    for phrase in spec.get("text_contains", []):
        if phrase not in output:
            errors.append("缺少必需内容: " + phrase)
    if spec.get("review_accept"):
        try:
            review = response_json(output)
            if not isinstance(review, dict) or review.get("decision") != "accept":
                errors.append("交叉审查未通过")
            if not isinstance(review, dict) or not review.get("reason"):
                errors.append("审查缺少理由")
        except (ValueError, TypeError):
            errors.append("审查必须返回含 decision 和 reason 的 JSON")
    requested = {a["path"]: a for a in spec.get("artifacts", [])}
    for rel in result.get("artifacts", []):
        requested.setdefault(rel, {"path": rel, "min_bytes": 1})
    for rel, requirement in requested.items():
        try:
            p = inside(workspace, rel)
            if not p.is_file() or p.stat().st_size < requirement.get("min_bytes", 1):
                raise ValueError("文件不存在或太小")
            if p.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("单个成果超过 20 MiB")
            if "json_equals" in requirement:
                actual = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in requirement["json_equals"].items()):
                    raise ValueError("文件 JSON 内容不符合要求")
            for phrase in requirement.get("text_contains", []):
                if phrase not in p.read_text(encoding="utf-8"):
                    raise ValueError("文件缺少必需内容: " + phrase)
            artifacts.append({
                "path": rel,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "bytes": p.stat().st_size,
            })
        except (ValueError, OSError, UnicodeError) as exc:
            errors.append("成果 %s: %s" % (rel, exc))
    return {
        "passed": not errors,
        "errors": errors,
        "artifacts": artifacts,
        "checks": spec,
        "checked_at": now(),
        "scope": "仅声明的验收条件；非通用正确性证明",
    }


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = self.root / "state.sqlite3"
        with self.connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,title TEXT,status TEXT,paused INTEGER DEFAULT 0,created TEXT);
                CREATE TABLE IF NOT EXISTS tasks(job TEXT,id TEXT,spec TEXT,status TEXT,agent TEXT,attempt INTEGER DEFAULT 0,result TEXT,PRIMARY KEY(job,id));
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,job TEXT,task TEXT,event TEXT,data TEXT);
                CREATE TABLE IF NOT EXISTS measures(agent TEXT,model TEXT,kind TEXT,passed INTEGER,elapsed REAL,at TEXT);
                CREATE TABLE IF NOT EXISTS workflows(job TEXT PRIMARY KEY,config TEXT NOT NULL,state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_heartbeats(agent TEXT PRIMARY KEY,worker_id TEXT,model TEXT,capabilities TEXT,status TEXT,last_seen TEXT);
                CREATE TABLE IF NOT EXISTS worker_claims(job TEXT,task TEXT,attempt INTEGER,agent TEXT,worker_id TEXT,status TEXT,progress TEXT,result TEXT,claimed_at TEXT,updated_at TEXT,PRIMARY KEY(job,task,attempt));
                """
            )

            columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
            if "deleted" not in columns:
                con.execute("ALTER TABLE jobs ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")

    def connect(self):
        con = sqlite3.connect(str(self.db), timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def event(self, job, task, event, data=None):
        with self.connect() as con:
            con.execute(
                "INSERT INTO events(at,job,task,event,data) VALUES(?,?,?,?,?)",
                (now(), job, task, event, dump(data or {})),
            )

    def init(self, registry):
        with self.connect() as con:
            old_agents = {r["id"]: json.loads(r["data"]) for r in con.execute("SELECT * FROM agents")}
            for previous in old_agents.values():
                if previous.get("custom") or previous.get("deleted"):
                    registry["agents"] = [a for a in registry["agents"] if a["id"] != previous["id"]]
                    registry["agents"].append(previous)
            con.execute("DELETE FROM agents")
            for agent in registry["agents"]:
                if agent["id"] in old_agents:
                    previous = old_agents[agent["id"]]
                    if previous.get("version") == agent.get("version") and previous.get("model") == agent.get("model") and previous.get("observed_model"):
                        agent["observed_model"] = previous["observed_model"]
                        if (
                            previous.get("automation", {}).get("status") == "verified"
                            and previous.get("executable") == agent.get("executable")
                            and agent.get("automation", {}).get("status") == "cli_available"
                        ):
                            agent["automation"].update({k: previous["automation"][k] for k in ("status", "reason", "last_verified_at") if k in previous["automation"]})
                    if previous.get("version") == agent.get("version") and previous.get("ui_model_observation"):
                        agent["ui_model_observation"] = previous["ui_model_observation"]
                    for keep_key in ("available_models", "models_refreshed_at", "models_sources", "execution_model"):
                        if previous.get(keep_key) and not agent.get(keep_key):
                            agent[keep_key] = previous[keep_key]
                con.execute("INSERT OR REPLACE INTO agents VALUES(?,?)", (identifier(agent["id"]), dump(agent)))
            con.execute("INSERT OR REPLACE INTO settings VALUES('discovery',?)", (dump(registry),))
        (self.root / "discovery.json").write_text(dump(registry), encoding="utf-8")
        self.event(None, None, "discovery.updated", {"agents": len(registry["agents"])})

    def agents(self, include_deleted=False):
        with self.connect() as con:
            values = [json.loads(row[0]) for row in con.execute("SELECT data FROM agents ORDER BY id")]
            return values if include_deleted else [a for a in values if not a.get("deleted")]

    def observe_ui_model(self, agent_id, label, source):
        identifier(agent_id)
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", label):
            raise ValueError("只接受简单模型标签")
        with self.connect() as con:
            row = con.execute("SELECT data FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise ValueError("Agent 不存在")
            agent = json.loads(row[0])
            agent["ui_model_observation"] = {
                "value": label,
                "source": source,
                "at": now(),
                "confidence": "ui_label",
                "note": "仅为当时公开界面显示，不能证明隐藏后端或之后会话的模型",
            }
            con.execute("UPDATE agents SET data=? WHERE id=?", (dump(agent), agent_id))
        self.event(None, None, "agent.ui_model_observed", {"agent": agent_id, "label": label})

    def add_plan(self, plan, paused=False):
        if not isinstance(plan, dict):
            raise ValueError("计划必须是 JSON 对象")
        tasks = plan.get("tasks", [])
        if not isinstance(tasks, list) or not tasks or len(tasks) > 100:
            raise ValueError("计划需要 1–100 个任务")
        job = identifier(plan.get("id") or "job-" + uuid.uuid4().hex[:10])
        ids = [identifier(t["id"]) for t in tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("任务标识重复")
        graph = {t["id"]: t.get("dependencies", []) for t in tasks}
        for task in tasks:
            if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
                raise ValueError("每个任务需要 prompt")
            check_validation(task.get("validation"))
            if not isinstance(graph[task["id"]], list) or not all(isinstance(d, str) for d in graph[task["id"]]):
                raise ValueError("dependencies 必须是任务标识数组")
            for field in ("allowed_tools", "required_capabilities"):
                if not isinstance(task.get(field, []), list) or not all(isinstance(v, str) for v in task.get(field, [])):
                    raise ValueError(field + " 必须是文字数组")
            if task.get("agent"):
                identifier(task["agent"])
            if task.get("review_of") and task["validation"].get("review_accept") is not True:
                raise ValueError("交叉审查必须使用 review_accept 验收")
            timeout = task.get("timeout_seconds", 300)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
                raise ValueError("timeout_seconds 必须为 1–3600")
            if not set(graph[task["id"]]) <= set(ids):
                raise ValueError("依赖任务不存在")
            if task.get("review_of") and task["review_of"] not in graph[task["id"]]:
                raise ValueError("review_of 必须同时是直接依赖")
            if not set(task.get("allowed_tools", [])) <= {"files_read", "files_write"}:
                raise ValueError("原型仅支持工作区文件权限")
            for art in task["validation"].get("artifacts", []):
                inside(self.root, art["path"])
        pending = set(ids)
        while pending:
            ready = {i for i in pending if not (set(graph[i]) & pending)}
            if not ready:
                raise ValueError("任务依赖存在循环")
            pending -= ready
        with self.connect() as con:
            con.execute(
                "INSERT INTO jobs(id,title,status,created,paused) VALUES(?,?,?,?,?)",
                (job, plan.get("title", job), "paused" if paused else "pending", now(), int(paused)),
            )
            for task in tasks:
                con.execute("INSERT INTO tasks(job,id,spec,status) VALUES(?,?,?,'pending')", (job, task["id"], dump(task)))
        directory = self.root / "jobs" / job
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(dump(plan), encoding="utf-8")
        self.event(job, None, "plan.created", {"tasks": ids})
        return job

    def task_rows(self, job):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM tasks WHERE job=? ORDER BY rowid", (job,))]

    def paused(self, job):
        with self.connect() as con:
            row = con.execute("SELECT paused FROM jobs WHERE id=? AND deleted=0", (job,)).fetchone()
            if not row:
                raise ValueError("任务组不存在")
            return bool(row[0])

    def control(self, job, paused):
        self.paused(job)
        with self.connect() as con:
            con.execute("UPDATE jobs SET paused=?,status=? WHERE id=?", (int(paused), "paused" if paused else "pending", job))
            if not paused:
                con.execute("UPDATE tasks SET status='pending' WHERE job=? AND status='paused'", (job,))
        self.event(job, None, "job.paused" if paused else "job.resumed")

    def snapshot(self):
        from .attachments import job_attachments_manifest, list_uploads
        from .worker_bridge import online_workers

        workers = online_workers(self)
        with self.connect() as con:
            jobs = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE deleted=0 ORDER BY created DESC")]
            measures = [
                dict(r)
                for r in con.execute(
                    "SELECT agent,model,kind,COUNT(*) samples,AVG(passed) success_rate,AVG(elapsed) mean_seconds "
                    "FROM measures GROUP BY agent,model,kind"
                )
            ]
            events = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM events WHERE job IS NULL OR job IN (SELECT id FROM jobs WHERE deleted=0) ORDER BY seq DESC LIMIT 100"
                )
            ]
        for job in jobs:
            with self.connect() as con:
                flow = con.execute("SELECT config,state FROM workflows WHERE job=?", (job["id"],)).fetchone()
            if flow:
                job["workflow"] = {"config": json.loads(flow[0]), "state": json.loads(flow[1])}
            job["attachments"] = job_attachments_manifest(self, job["id"])
            job["tasks"] = self.task_rows(job["id"])
            tok_in = tok_out = tok_tot = 0
            for task in job["tasks"]:
                task["spec"] = json.loads(task["spec"])
                task["result"] = json.loads(task["result"]) if task["result"] else None
                usage = (task["result"] or {}).get("usage")
                if isinstance(usage, dict):
                    tok_in += int(usage.get("input_tokens") or 0)
                    tok_out += int(usage.get("output_tokens") or 0)
                    tok_tot += int(usage.get("total_tokens") or 0)
                if task["attempt"] > 0:
                    attempt_dir = self.root / "jobs" / job["id"] / task["id"] / ("attempt-%d" % task["attempt"])
                    prog = read_progress(attempt_dir)
                    if prog:
                        task["progress"] = prog
            if tok_in or tok_out or tok_tot:
                job["token_usage"] = {"input_tokens": tok_in, "output_tokens": tok_out, "total_tokens": tok_tot or (tok_in + tok_out)}
        with self.connect() as con:
            trash = [dict(r) for r in con.execute("SELECT id,title,created FROM jobs WHERE deleted=1 ORDER BY created DESC")]
        agents = self.agents()
        for a in agents:
            if a["id"] in workers:
                a["worker_online"] = workers[a["id"]]
        return {
            "agents": agents,
            "jobs": jobs,
            "measurements": measures,
            "events": events,
            "online_workers": workers,
            "uploads": list_uploads(self),
            "deleted_agents": [a for a in self.agents(True) if a.get("deleted")],
            "deleted_jobs": trash,
        }

    def record(self, job, row, result):
        spec = json.loads(row["spec"])
        status = result.get("status")
        if status == "completed":
            workspace = self.root / "jobs" / job / row["id"] / ("attempt-%d" % row["attempt"]) / "workspace"
            result["verification"] = validate(spec, result, workspace)
            status = "done" if result["verification"]["passed"] else "failed"
        elif status not in {"paused", "failed", "waiting_external"}:
            raise ValueError("无效适配器状态")
        with self.connect() as con:
            con.execute("UPDATE tasks SET status=?,result=? WHERE job=? AND id=?", (status, dump(result), job, row["id"]))
            if status in {"done", "failed"}:
                model = result.get("model", {}).get("value") or (result.get("configured_model") or {}).get("value") or "unknown"
                con.execute(
                    "INSERT INTO measures VALUES(?,?,?,?,?,?)",
                    (row["agent"], model, spec.get("kind", "general"), int(status == "done"), result.get("elapsed_seconds", 0), now()),
                )
                saved = con.execute("SELECT data FROM agents WHERE id=?", (row["agent"],)).fetchone()
                if saved:
                    agent = json.loads(saved[0])
                    agent["observed_model"] = {**result.get("model", {}), "value": result.get("model", {}).get("value"), "at": now()}
                    transport = result.get("transport")
                    if status == "done" and not result.get("is_test_fixture"):
                        if transport == "codex_cli":
                            agent["automation"]["status"] = "verified"
                            agent["automation"]["reason"] = "已通过真实客户端任务与声明的验收条件；使用现有 ChatGPT 登录。"
                            agent["automation"]["last_verified_at"] = now()
                        elif transport == "desktop_ui":
                            agent["automation"]["status"] = "verified"
                            agent["automation"]["reason"] = "已通过桌面 UI 自动化实测验收（模型身份仍以界面配置为准）。"
                            agent["automation"]["last_verified_at"] = now()
                        elif transport in {"dsh_cli", "claude_cli", "gemini_cli", "codebuddy_cli", "custom_cli", "http_api", "worker_bridge"}:
                            agent["automation"]["status"] = "verified"
                            agent["automation"]["reason"] = "已通过真实任务验收（传输通道：%s）。" % transport
                            agent["automation"]["last_verified_at"] = now()
                    con.execute("UPDATE agents SET data=? WHERE id=?", (dump(agent), row["agent"]))
        self.event(job, row["id"], "task." + status, {"agent": row["agent"], "verification": result.get("verification"), "error": result.get("error")})
        return result

    def import_result(self, job, task_id, result, *, transport="file_bridge_import", preserve_model=False):
        """Explicit file handoff or auto-collected worker result; external claims still go through trusted validators."""
        self.paused(job)
        row = next((r for r in self.task_rows(job) if r["id"] == task_id), None)
        if not row or row["status"] != "waiting_external":
            raise ValueError("仅等待外部客户端的任务可以导入")
        result = dict(result)
        result["status"] = "completed"
        result["transport"] = transport
        if preserve_model and isinstance(result.get("model"), dict) and result["model"].get("value"):
            pass
        elif preserve_model and isinstance(result.get("model"), str) and re.fullmatch(r"[A-Za-z0-9._:/@-]{1,160}", result["model"]):
            result["model"] = {"value": result["model"], "source": transport, "confidence": "reported"}
        else:
            result["model"] = {
                "value": None,
                "source": "external_unverified",
                "confidence": "unknown",
                "reported": result.get("model"),
            }
        task = json.loads(row["spec"])
        if task.get("review_of"):
            source = next(r for r in self.task_rows(job) if r["id"] == task["review_of"])
            source_res = json.loads(source["result"]) if source.get("result") else {}
            first = source_res.get("model", {}).get("value")
            second = result.get("model", {}).get("value")
            result["review_independence"] = {
                "different_agent": source["agent"] != row["agent"],
                "different_model": bool(preserve_model and first and second and first != second),
                "note": "Worker 汇报模型" if preserve_model else "文件导入无法验证实际模型身份",
            }
        recorded = self.record(job, row, result)
        refresh_job(self, job)
        return recorded


def collect_ready_results(store):
    """Automatically collect completed worker_claims or dropped result.json files for waiting_external tasks."""
    collected_jobs = []
    with store.connect() as con:
        waiting = [
            dict(r)
            for r in con.execute(
                "SELECT t.* FROM tasks t JOIN jobs j ON j.id = t.job "
                "WHERE t.status='waiting_external' AND j.deleted=0 AND j.paused=0"
            )
        ]
    for row in waiting:
        job, tid, attempt = row["job"], row["id"], row["attempt"]
        if attempt <= 0:
            continue
        attempt_dir = store.root / "jobs" / job / tid / ("attempt-%d" % attempt)
        # 1. Check completed worker_claims
        with store.connect() as con:
            claim = con.execute(
                "SELECT * FROM worker_claims WHERE job=? AND task=? AND attempt=? AND status IN ('completed', 'failed')",
                (job, tid, attempt),
            ).fetchone()
        if claim:
            payload = json.loads(claim["result"]) if claim["result"] else {}
            try:
                if claim["status"] == "failed":
                    store.record(
                        job,
                        row,
                        {
                            "status": "failed",
                            "output": "",
                            "error": payload.get("error") or "外部 Worker 返回失败",
                            "transport": "worker_bridge",
                        },
                    )
                else:
                    store.import_result(
                        job,
                        tid,
                        {
                            "output": payload.get("output", ""),
                            "artifacts": payload.get("artifacts", []),
                            "model": payload.get("model"),
                            "worker_id": claim["worker_id"],
                        },
                        transport="worker_bridge",
                        preserve_model=True,
                    )
                with store.connect() as con:
                    con.execute(
                        "UPDATE worker_claims SET status='collected' WHERE job=? AND task=? AND attempt=?",
                        (job, tid, attempt),
                    )
                store.event(job, tid, "task.auto_collected", {"source": "worker_bridge", "attempt": attempt})
                if job not in collected_jobs:
                    collected_jobs.append(job)
                continue
            except Exception:
                continue
        # 2. Check dropped result.json file inside attempt_dir
        result_file = attempt_dir / "result.json"
        if result_file.is_file():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("output"), str) and payload["output"].strip():
                    store.import_result(job, tid, payload)
                    archived = attempt_dir / "result.collected.json"
                    try:
                        os.replace(result_file, archived)
                    except OSError:
                        pass
                    store.event(job, tid, "task.auto_collected", {"source": "result_file", "attempt": attempt})
                    if job not in collected_jobs:
                        collected_jobs.append(job)
            except Exception:
                continue
    return collected_jobs


def select_agent(store, task, rows):
    exclude = set()
    source_model = None
    source_transport = None
    if task.get("review_of"):
        source = rows[task["review_of"]]
        exclude.add(source["agent"])
        if source.get("result"):
            try:
                sres = json.loads(source["result"])
                source_model = sres.get("model", {}).get("value") or sres.get("configured_model", {}).get("value")
                source_transport = sres.get("transport")
            except (ValueError, TypeError):
                pass
    candidates = []
    for agent in store.agents():
        aid = agent["id"]
        transport = agent.get("transport")
        if transport == "http_api":
            if task.get("allowed_tools") or task.get("validation", {}).get("artifacts"):
                continue
            if not task.get("agent") and not agent.get("auto_route"):
                continue
        if transport == "dsh_cli" and not agent.get("dsh_enabled") and not task.get("agent"):
            continue
        if transport == "desktop_ui" and not agent.get("desktop_ui_enabled") and not task.get("agent"):
            continue
        if aid in exclude or (task.get("agent") and task["agent"] != aid):
            continue
        required = set(task.get("required_capabilities", []))
        if not required <= set(agent.get("capabilities", [])):
            continue
        automation = agent.get("automation", {}).get("status", "unavailable")
        # Only verified supported transports run automatically. Named clients may use the visible file bridge.
        if automation not in {"ready", "available", "supported", "cli_available", "verified"} and not task.get("agent"):
            continue
        kind = task.get("kind", "general")
        candidate_model = agent.get("observed_model", {}).get("value") or agent.get("execution_model") or (agent.get("model") or {}).get("value")
        configured = candidate_model or "unknown"
        with store.connect() as con:
            samples = con.execute(
                "SELECT passed,elapsed FROM measures WHERE agent=? AND kind=? AND model=?",
                (aid, kind, configured),
            ).fetchall()
        quality = (sum(r[0] for r in samples) + 1) / (len(samples) + 2)
        mean = sum(r[1] for r in samples) / len(samples) if samples else 0
        cross_model_bonus = 0
        if task.get("review_of"):
            if source_model and candidate_model and candidate_model != source_model:
                cross_model_bonus += 30
            if source_transport and transport and transport != source_transport:
                cross_model_bonus += 15
        score = quality * 100 - min(mean / 60, 10) + (5 if automation in {"ready", "verified"} else 0) + cross_model_bonus
        candidates.append((score, aid, agent, len(samples)))
    candidates.sort(key=lambda r: (-r[0], r[1]))
    return (
        (
            candidates[0][2],
            {
                "score": candidates[0][0],
                "samples": candidates[0][3],
                "basis": "权限硬约束 + 跨模型复核优先 + 同模型同任务类验收率 + 耗时；无样本为中性先验",
                "excluded_authors": sorted(exclude),
            },
        )
        if candidates
        else (None, {"reason": "没有满足能力、接入状态及独立审查条件的 Agent"})
    )


def refresh_job(store, job):
    rows = store.task_rows(job)
    states = {r["status"] for r in rows}
    if store.paused(job):
        status = "paused"
    elif states == {"done"}:
        status = "done"
    elif "running" in states:
        status = "running"
    elif "waiting_external" in states:
        status = "waiting_external"
    elif states & {"failed", "blocked"}:
        status = "needs_attention"
    else:
        status = "pending"
    with store.connect() as con:
        con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job))
    lines = ["# " + job, "", "状态：" + status, "", "| 任务 | 执行者 | 状态 |", "|---|---|---|"]
    for row in rows:
        lines.append("| %s | %s | %s |" % (row["id"], row["agent"] or "待分配", row["status"]))
        if row["result"]:
            receipt = json.loads(row["result"])
            lines += ["", "## " + row["id"], "", receipt.get("output", "")[:20000], "", "验收：" + dump(receipt.get("verification", {}))]
    lines += ["", "此文件由本地调度器汇集证据，不冒充总指挥的最终分析。总指挥的分析和汇总必须作为单独任务提交并验收。"]
    dest = store.root / "jobs" / job / "summary.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines), encoding="utf-8")
    return {"job": job, "status": status, "summary": str(dest)}


def _dispatch_dag_row(store, job, row, agent, route, rows, db_lock):
    with db_lock:
        row["attempt"] += 1
        row["agent"] = agent["id"]
        attempt_dir = store.root / "jobs" / job / row["id"] / ("attempt-%d" % row["attempt"])
        workspace = attempt_dir / "workspace"
        with store.connect() as con:
            con.execute(
                "UPDATE tasks SET status='running',agent=?,attempt=?,result=NULL WHERE job=? AND id=?",
                (agent["id"], row["attempt"], job, row["id"]),
            )
            con.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
        store.event(job, row["id"], "task.dispatched", {"agent": agent["id"], "route": route})
    task = json.loads(row["spec"])
    try:
        workspace.mkdir(parents=True, exist_ok=False)
        prepare_inputs(store, job, task, rows, workspace, agent)
        (workspace / "task.json").write_text(dump(task), encoding="utf-8")
        (attempt_dir / "routing.json").write_text(dump(route), encoding="utf-8")
        result = run_adapter(agent, task, workspace, attempt_dir, lambda: store.paused(job))
        if task.get("review_of"):
            source = json.loads(rows[task["review_of"]]["result"])
            first = source.get("model", {}).get("value")
            second = result.get("model", {}).get("value")
            result["review_independence"] = {
                "different_agent": True,
                "different_model": bool(first and second and first != second),
                "note": "模型未知或相同不算跨模型审查",
            }
        with db_lock:
            store.record(job, row, result)
    except Exception as exc:
        with db_lock:
            store.record(job, row, {"status": "failed", "error": str(exc), "output": ""})


def run_job(store, job, wait_for_lock=False):
    identifier(job)
    store.paused(job)
    lockpath = store.root / "runner.lock"
    with lockpath.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait_for_lock:
                    raise RuntimeError("已有调度器在运行；不重复派发")
                if store.paused(job):
                    return refresh_job(store, job)
                time.sleep(0.2)
        # A terminated scheduler leaves ambiguous effects. Require explicit retry, never automatically duplicate.
        with store.connect() as con:
            stale = con.execute("SELECT id FROM tasks WHERE job=? AND status='running'", (job,)).fetchall()
            for row in stale:
                con.execute(
                    "UPDATE tasks SET status='failed',result=? WHERE job=? AND id=?",
                    (dump({"status": "failed", "error": "上次执行中断，结果不确定；检查成果后显式 retry"}), job, row[0]),
                )
        for row in stale:
            store.event(job, row[0], "task.interrupted")
        from .workflow import finish_status, read_flow, run_locked, save_state

        if read_flow(store, job):
            try:
                return run_locked(store, job)
            except Exception as exc:
                _, state = read_flow(store, job)
                state["error"] = str(exc)
                save_state(store, job, state)
                store.event(job, None, "workflow.error", {"error": str(exc)})
                return finish_status(store, job, state)
        db_lock = threading.Lock()
        while not store.paused(job):
            rows = {r["id"]: r for r in store.task_rows(job)}
            ready = [
                r
                for r in rows.values()
                if r["status"] in {"pending", "blocked"}
                and all(rows[d]["status"] == "done" for d in json.loads(r["spec"]).get("dependencies", []))
            ]
            if not ready:
                break
            runnable = []
            for row in ready:
                task = json.loads(row["spec"])
                agent, route = select_agent(store, task, rows)
                if agent is None:
                    if row["status"] != "blocked":
                        with store.connect() as con:
                            con.execute("UPDATE tasks SET status='blocked',result=? WHERE job=? AND id=?", (dump(route), job, row["id"]))
                        store.event(job, row["id"], "task.blocked", route)
                    continue
                runnable.append((row, agent, route))
            if not runnable:
                break
            if len(runnable) == 1:
                row, agent, route = runnable[0]
                _dispatch_dag_row(store, job, row, agent, route, rows, db_lock)
            else:
                with ThreadPoolExecutor(max_workers=min(3, len(runnable))) as pool:
                    futures = [
                        pool.submit(_dispatch_dag_row, store, job, row, agent, route, rows, db_lock)
                        for row, agent, route in runnable
                    ]
                    for fut in as_completed(futures):
                        fut.result()
        return refresh_job(store, job)


def _preview_text_artifact(path, remaining_budget):
    if remaining_budget <= 0:
        return None
    if path.suffix.lower() not in _TEXT_ARTIFACT_EXTS:
        return None
    size = path.stat().st_size
    if size > _MAX_PREVIEW_FILE_BYTES or size > remaining_budget:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def prepare_inputs(store, job, task, rows, workspace, agent):
    from .attachments import copy_job_attachments_to_workspace

    contexts = []
    preview_budget = _MAX_PREVIEW_TOTAL_BYTES
    att_entries, att_previews = copy_job_attachments_to_workspace(store, job, workspace, agent)
    if att_entries:
        contexts.append({
            "task": "_job_attachments",
            "attachments": att_entries,
            "attachment_previews": att_previews,
            "note": "以上为用户提供的任务参考资料（文件已同步至工作区 inputs/attachments/ 目录）",
        })
    for dep_id in task.get("dependencies", []):
        dep = rows[dep_id]
        result = json.loads(dep["result"])
        entry = {
            "task": dep_id,
            "agent": dep["agent"],
            "output": result.get("output"),
            "model": result.get("model"),
            "verification": result.get("verification"),
        }
        previews = {}
        for art in result.get("verification", {}).get("artifacts", []):
            source_ws = store.root / "jobs" / job / dep_id / ("attempt-%d" % dep["attempt"]) / "workspace"
            source = inside(source_ws, art["path"])
            if hashlib.sha256(source.read_bytes()).hexdigest() != art["sha256"]:
                raise ValueError("已验收成果发生变化: " + dep_id)
            target = inside(workspace, "inputs/" + dep_id + "/" + art["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            text_preview = _preview_text_artifact(source, preview_budget)
            if text_preview is not None:
                previews[art["path"]] = text_preview
                preview_budget -= len(text_preview.encode("utf-8"))
        if previews:
            entry["artifact_previews"] = previews
        contexts.append(entry)
    task["context"] = dump(contexts)


def retry_task(store, job, task_id):
    store.paused(job)
    rows = {r["id"]: r for r in store.task_rows(job)}
    if task_id not in rows or rows[task_id]["status"] not in {"failed", "blocked"}:
        raise ValueError("仅失败或阻塞任务可以重试")
    with store.connect() as con:
        con.execute("UPDATE tasks SET status='pending',result=NULL WHERE job=? AND id=?", (job, task_id))
    store.event(job, task_id, "task.retry_requested")
    return refresh_job(store, job)
