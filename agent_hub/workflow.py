"""Bounded, durable multi-agent cross-model planning -> parallel execution -> review -> revision -> delivery.

Model output is a proposal, never permission to expand tools, providers or paths.
All mutations below run under the scheduler's existing single-writer runner lock,
with fine-grained thread locking during bounded parallel wave execution.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from urllib.parse import urlsplit

from .core import (
    _MAX_PREVIEW_FILE_BYTES,
    _MAX_PREVIEW_TOTAL_BYTES,
    _TEXT_ARTIFACT_EXTS,
    dump,
    identifier,
    inside,
    now,
    prepare_inputs,
    response_json,
)


READY = {"verified", "available", "cli_available", "ready"}
KINDS = {"general", "reasoning", "coding", "writing_zh", "review"}
AUTOMATED_TRANSPORTS = {
    "codex_cli",
    "dsh_cli",
    "claude_cli",
    "gemini_cli",
    "codebuddy_cli",
    "custom_cli",
    "http_api",
    "worker_bridge",
    "desktop_ui",
}
FILE_TRANSPORTS = {
    "codex_cli",
    "dsh_cli",
    "claude_cli",
    "gemini_cli",
    "codebuddy_cli",
    "custom_cli",
    "worker_bridge",
    "desktop_ui",
}


def eligible(agent):
    if agent.get("deleted"):
        return False
    transport = agent.get("transport")
    if transport not in AUTOMATED_TRANSPORTS:
        return False
    if transport == "dsh_cli" and not agent.get("dsh_enabled"):
        return False
    if transport == "desktop_ui" and not agent.get("desktop_ui_enabled"):
        return False
    return agent.get("automation", {}).get("status") in READY


def agent_model_key(agent):
    configured = agent.get("execution_model") or (agent.get("model", {}).get("value") if isinstance(agent.get("model"), dict) else agent.get("model"))
    observed = (agent.get("observed_model") or {}).get("value") if isinstance(agent.get("observed_model"), dict) else None
    if agent.get("execution_model"):
        return agent["execution_model"]
    if isinstance(agent.get("model"), dict) and agent["model"].get("source") == "user_selected" and agent["model"].get("value"):
        return agent["model"]["value"]
    return observed or configured


def agent_provider_key(agent):
    transport = agent.get("transport") or "unknown"
    if transport == "http_api":
        host = urlsplit(agent.get("endpoint") or "").netloc or "unknown"
        return "http_api:%s:%s" % (agent.get("protocol", "openai"), host)
    if transport == "dsh_cli":
        return "dsh_cli:%s" % (agent.get("dsh_provider") or "default")
    if transport == "worker_bridge":
        return "worker_bridge:%s" % agent.get("id", "")
    if transport == "custom_cli":
        return "custom_cli:%s" % (agent.get("executable") or agent.get("id", ""))
    return transport


def assess_cross_model(agents_list):
    models = sorted({m for a in agents_list for m in [agent_model_key(a)] if m})
    providers = sorted({agent_provider_key(a) for a in agents_list})
    return {
        "distinct_models": models,
        "distinct_providers": providers,
        "is_cross_model_configured": len(models) >= 2 or len(providers) >= 2,
    }


def read_flow(store, job):
    with store.connect() as con:
        row = con.execute("SELECT config,state FROM workflows WHERE job=?", (job,)).fetchone()
    return (json.loads(row[0]), json.loads(row[1])) if row else None


def save_state(store, job, state):
    with store.connect() as con:
        con.execute("UPDATE workflows SET state=? WHERE job=?", (dump(state), job))


def catalog(store):
    measures = store.snapshot()["measurements"]
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "transport": a.get("transport"),
            "provider_key": agent_provider_key(a),
            "can_run": eligible(a),
            "model": a.get("execution_model") or (a.get("model", {}).get("value") if isinstance(a.get("model"), dict) else None),
            "endpoint": a.get("endpoint"),
            "protocol": a.get("protocol"),
            "executable": a.get("executable"),
            "dsh_provider": a.get("dsh_provider"),
            "app_path": a.get("app_path"),
            "desktop_ui_enabled": a.get("desktop_ui_enabled"),
            "model_source": (a.get("model") or {}).get("source", "unknown") if isinstance(a.get("model"), dict) else "unknown",
            "observed_model": a.get("observed_model"),
            "capabilities": a.get("capabilities", []),
            "strengths": a.get("strengths", ""),
            "measurements": [m for m in measures if m["agent"] == a["id"]],
        }
        for a in store.agents()
    ]


def setup_gpt(store):
    """Add explicit roles, without restoring any agent the user deleted."""
    from .discovery import discover
    from .management import idle

    with idle(store):
        original = next(a for a in discover()["agents"] if a["id"] == "codex")
        if original.get("transport") != "codex_cli" or original["automation"]["status"] != "cli_available":
            raise ValueError("没有找到可用的 Codex 执行入口，请先安装并登录客户端")
        model = original.get("model", {}).get("value")
        with store.connect() as con:
            for aid, name, role in [
                ("hub-gpt", "GPT（规划与汇总）", "commander"),
                ("hub-codex", "Codex（执行与复核）", "worker"),
            ]:
                if con.execute("SELECT 1 FROM agents WHERE id=?", (aid,)).fetchone():
                    continue
                a = dict(
                    original,
                    id=aid,
                    name=name,
                    custom=True,
                    role=role,
                    deleted=False,
                    strengths="沿用现有 GPT 配置；优势须由实际任务验证",
                    auto_route=True,
                )
                if model:
                    a["execution_model"] = model
                con.execute("INSERT INTO agents VALUES(?,?)", (aid, dump(a)))
    store.event(None, None, "workflow.gpt_roles_added", {"note": "独立任务上下文，共用现有登录；不是独立模型的证明"})
    try:
        from .model_catalog import refresh_agent_models

        for aid in ("hub-gpt", "hub-codex"):
            if any(a["id"] == aid for a in store.agents()):
                refresh_agent_models(store, aid)
    except Exception:
        pass


def flow_recipients(store, data):
    agents = {a["id"]: a for a in store.agents()}
    commander = data.get("commander", "hub-gpt")
    chosen = data.get("participants")
    if chosen is None:
        chosen = [
            a["id"]
            for a in agents.values()
            if eligible(a) and (a.get("transport") != "http_api" or a.get("auto_route"))
        ]
    if not isinstance(chosen, list) or not all(isinstance(x, str) for x in chosen):
        raise ValueError("参与者必须是 Agent 列表")
    chosen = sorted(set(chosen) | {commander})
    return chosen


def create_flow(store, data):
    goal = data.get("goal", "")
    requirements = data.get("requirements", "")
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 20000:
        raise ValueError("请填写任务目标，最多 20000 字")
    if not isinstance(requirements, str) or len(requirements) > 10000:
        raise ValueError("验收要求最多 10000 字")
    agents = {a["id"]: a for a in store.agents()}
    commander = data.get("commander", "hub-gpt")
    if commander not in agents or not eligible(agents[commander]):
        raise ValueError("请先启用协作入口，或选择已接通的指挥 Agent")
    chosen = flow_recipients(store, data)
    if any(a not in agents or not eligible(agents[a]) for a in chosen):
        raise ValueError("参与者含未接通客户端；请先完成任务入口验证")
    reviewers = [a for a in chosen if a != commander]
    if not reviewers:
        raise ValueError("至少需要另一个已接通 Agent 进行执行和复核")
    # API opt-in applies to autonomous selection, including reviewers.
    if any(agents[a].get("transport") == "http_api" and not agents[a].get("auto_route") for a in chosen):
        raise ValueError("请先为参与协作的 API 开启“允许自动分配任务”")
    require_cross_model = data.get("require_cross_model") is True
    parallel_steps = data.get("parallel_steps", True) is True
    confirm_plan = data.get("confirm_plan") is True
    max_concurrency = data.get("max_concurrency", 3)
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= 4:
        raise ValueError("最大并发数必须为 1–4 的整数")
    chosen_agents = [agents[a] for a in chosen]
    cross_info = assess_cross_model(chosen_agents)
    if require_cross_model and not cross_info["is_cross_model_configured"]:
        raise ValueError("已启用“强制跨模型协作”，但所选参与者缺少至少两个不同模型/供应商；请添加并勾选不同模型的 Agent")
    steps, revisions, calls = data.get("max_steps", 3), data.get("max_revisions", 1), data.get("max_calls", 16)
    for value, lo, hi in [(steps, 1, 6), (revisions, 0, 2), (calls, 4, 32)]:
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError("步骤、返工轮次或调用上限无效")
    if calls < steps + 3:
        raise ValueError("调用上限应至少为最大步骤数加 3")
    config = {
        "goal": goal.strip(),
        "requirements": requirements.strip(),
        "commander": commander,
        "participants": chosen,
        "max_steps": steps,
        "max_revisions": revisions,
        "max_calls": calls,
        "files_allowed": data.get("files_allowed") is True,
        "require_cross_model": require_cross_model,
        "parallel_steps": parallel_steps,
        "confirm_plan": confirm_plan,
        "max_concurrency": max_concurrency,
        "cross_model_assessment": cross_info,
        "catalog": [a for a in catalog(store) if a["id"] in chosen],
        "created_at": now(),
    }
    from .attachments import approved_inputs, attach_job_inputs

    bundle = approved_inputs(store, data, chosen)
    # Create the durable job paused until its workflow metadata and attachments are committed.
    job = store.add_plan({"title": goal.strip()[:80], "tasks": [plan_task(config)]}, paused=True)
    if bundle:
        attach_job_inputs(store, job, bundle)
    with store.connect() as con:
        con.execute(
            "INSERT INTO workflows VALUES(?,?,?)",
            (
                job,
                dump(config),
                dump({"phase": "planning", "cycle": 0, "calls": 0, "parallel_waves": 0, "user_notes": []}),
            ),
        )
        con.execute("UPDATE jobs SET paused=0,status='pending' WHERE id=?", (job,))
    store.event(
        job,
        None,
        "workflow.created",
        {
            "participants": chosen,
            "max_calls": calls,
            "require_cross_model": require_cross_model,
            "parallel_steps": parallel_steps,
            "confirm_plan": confirm_plan,
            "cross_model_configured": cross_info["is_cross_model_configured"],
        },
    )
    return job


def base_task(tid, role, aid, prompt, dependencies=None, tools=None):
    return {
        "id": tid,
        "workflow_role": role,
        "agent": aid,
        "prompt": prompt,
        "dependencies": dependencies or [],
        "allowed_tools": tools or [],
        "kind": "reasoning" if role == "planning" else "review" if role == "review" else "writing_zh",
        "timeout_seconds": 300,
        "validation": {"min_chars": 20},
    }


def plan_task(config):
    commander_entry = next((a for a in config["catalog"] if a["id"] == config["commander"]), {})
    commander_name = commander_entry.get("name") or config["commander"]
    parallel_note = (
        "独立互不依赖的子步骤请将 dependencies 置为空数组，调度器会自动并行派发给多个 Agent 同步协作。"
        if config.get("parallel_steps", True)
        else "步骤将按顺序串行执行。"
    )
    prompt = (
        "你是总指挥（%s），同时承担实质分析。先分析需求、方法和风险，再把可执行工作分配给下列已接通 Agent。"
        "按各 Agent 的不同模型、传输通道、实测验收表现和工具权限进行跨模型优势互补分工；没有测试不能虚构优势。"
        "尽量把具体执行交给非指挥 Agent，多模型参与时优先让不同模型承担互补子任务，自己负责分析和最终整合。"
        "%s"
        "只输出 JSON，不要 Markdown 围栏。格式："
        '{"analysis":"有实质内容的需求分析和解决方案", "steps":['
        '{"id":"step1","agent":"候选 ID","kind":"reasoning",'
        '"prompt":"具体执行要求及成果标准","reason":"分配依据",'
        '"dependencies":[],"needs_files":false,"artifacts":[]}]}。'
        "steps 需要 1 到 %d 项，id 使用 step1、step2 等；依赖只能引用之前的步骤。"
        "kind 只能 general/reasoning/coding/writing_zh/review。"
        "artifacts 仅为需生成的相对文件路径列表；needs_files 为 false 时必须为空。"
        "注意：transport 为 http_api 的 Agent 只能执行文字任务（needs_files 必须为 false）。"
        "不要请求联网、安装软件、访问个人文件、发送消息或扩大权限。"
        "请基于任务目标、验收要求以及已附加在工作区 inputs/attachments/ 或上下文中的用户资料（如有）进行规划或在授权工作区生成文件；无法完成的部分明确说明。\n"
        "允许工作区文件操作：%s\n任务：%s\n验收要求：%s\n候选档案：%s"
        % (
            commander_name,
            parallel_note,
            config["max_steps"],
            config["files_allowed"],
            config["goal"],
            config["requirements"],
            dump(config["catalog"]),
        )
    )
    t = base_task("planning", "planning", config["commander"], prompt)
    t["workflow_contract"] = config
    return t


def parse_plan(output, config):
    plan = response_json(output)
    if not isinstance(plan, dict) or not isinstance(plan.get("analysis"), str) or len(plan["analysis"].strip()) < 20:
        raise ValueError("规划缺少实质分析")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= config["max_steps"]:
        raise ValueError("规划步骤数不符合限制")
    known = set()
    candidates = {a["id"]: a for a in config["catalog"]}
    normalized = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or s.get("id") != "step%d" % i:
            raise ValueError("步骤标识必须依次为 step1、step2 等")
        a = candidates.get(s.get("agent"))
        if not a or s.get("agent") not in config["participants"] or not a["can_run"]:
            raise ValueError("规划选择了未授权或未接通的 Agent")
        for key in ("prompt", "reason"):
            if not isinstance(s.get(key), str) or not 1 <= len(s[key].strip()) <= 12000:
                raise ValueError("每一步必须有具体要求和分配理由")
        deps = s.get("dependencies", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) and d in known for d in deps):
            raise ValueError("步骤依赖不存在、循环或顺序错误")
        kind = s.get("kind", "general")
        if kind not in KINDS or (kind != "general" and kind not in a["capabilities"]):
            raise ValueError("所选 Agent 未声明此任务能力")
        needs_files = s.get("needs_files", False)
        if type(needs_files) is not bool:
            raise ValueError("needs_files 必须是布尔值")
        if needs_files and (
            not config["files_allowed"]
            or a["transport"] not in FILE_TRANSPORTS
            or not {"files_read", "files_write"} <= set(a["capabilities"])
        ):
            raise ValueError("规划文件操作超出用户或 Agent 权限")
        artifacts = s.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 10 or (artifacts and not needs_files):
            raise ValueError("成果清单无效")
        for rel in artifacts:
            if not isinstance(rel, str) or len(rel) > 240:
                raise ValueError("成果路径无效")
            inside(Path("/tmp/hub-plan-path-check"), rel)
        normalized.append(
            {k: s[k] for k in ("id", "agent", "prompt", "reason")}
            | {"kind": kind, "dependencies": deps, "needs_files": needs_files, "artifacts": artifacts}
        )
        known.add(s["id"])
    if not any(s["agent"] != config["commander"] for s in normalized):
        raise ValueError("必须安排至少一个非指挥 Agent 执行实质工作")
    return {"analysis": plan["analysis"], "steps": normalized}


def parse_review(output, steps):
    review = response_json(output)
    if not isinstance(review, dict) or review.get("decision") not in ("accept", "revise"):
        raise ValueError("审查必须明确 accept 或 revise")
    if not isinstance(review.get("reason"), str) or len(review["reason"].strip()) < 5:
        raise ValueError("审查必须提供具体理由")
    issues = review.get("issues", [])
    if not isinstance(issues, list) or len(issues) > 10:
        raise ValueError("审查问题列表无效")
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("step") not in set(steps) | {"synthesis"}:
            raise ValueError("审查问题必须指向已有步骤或 synthesis")
        if not isinstance(issue.get("instruction"), str) or not 1 <= len(issue["instruction"]) <= 4000:
            raise ValueError("返工必须有明确修改要求")
    if review["decision"] == "revise" and not issues:
        raise ValueError("要求返工时必须列出问题")
    if review["decision"] == "accept" and issues:
        raise ValueError("存在待修正问题时不能通过验收")
    return review


def validate_contract(task, output):
    role = task.get("workflow_role")
    if role == "planning":
        parse_plan(output, task["workflow_contract"])
    elif role == "review":
        parse_review(output, task["review_steps"])


def ensure_task(store, job, task):
    with store.connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO tasks(job,id,spec,status) VALUES(?,?,?,'pending')",
            (job, task["id"], dump(task)),
        )


def result_of(store, job, tid):
    row = next(r for r in store.task_rows(job) if r["id"] == tid)
    return row, json.loads(row["result"]) if row["result"] else {}


def _agent_matches_snapshot(a, expected):
    if not a or not eligible(a):
        return False
    if a.get("transport") != expected.get("transport"):
        return False
    for k in ("endpoint", "protocol", "executable", "dsh_provider"):
        if a.get(k) != expected.get(k):
            return False
    if a.get("transport") == "desktop_ui" and a.get("app_path") != expected.get("app_path"):
        return False
    current_model = a.get("execution_model") or (a.get("model", {}).get("value") if isinstance(a.get("model"), dict) else None)
    if current_model != expected.get("model"):
        return False
    if a.get("transport") == "http_api" and not a.get("auto_route"):
        return False
    return True


def execute(store, job, config, state, task, lock=None):
    from .core import run_adapter

    guard = lock if lock is not None else threading.Lock()
    with guard:
        ensure_task(store, job, task)
        row, prior = result_of(store, job, task["id"])
        if row["status"] == "done":
            return prior
        if row["status"] == "waiting_external":
            state["waiting_task"] = task["id"]
            save_state(store, job, state)
            return None
        if row["status"] in {"failed", "blocked", "paused"} or store.paused(job):
            return None
        if state["calls"] >= config["max_calls"]:
            state["limit_reason"] = "已达到本任务模型调用上限，未继续调用"
            save_state(store, job, state)
            return None
        agents = {a["id"]: a for a in store.agents()}
        a = agents.get(task["agent"])
        expected = next((x for x in config["catalog"] if x["id"] == task["agent"]), {})
        if not _agent_matches_snapshot(a, expected):
            with store.connect() as con:
                con.execute(
                    "UPDATE tasks SET status='blocked',result=? WHERE job=? AND id=?",
                    (dump({"error": "Agent 已删除、停用或模型配置已变化；未按旧计划执行"}), job, task["id"]),
                )
            return None
        row["attempt"] += 1
        row["agent"] = a["id"]
        state["calls"] += 1
        call_num = state["calls"]
        with store.connect() as con:
            con.execute(
                "UPDATE tasks SET status='running',agent=?,attempt=?,result=NULL WHERE job=? AND id=?",
                (a["id"], row["attempt"], job, task["id"]),
            )
            con.execute("UPDATE workflows SET state=? WHERE job=?", (dump(state), job))
            con.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
        directory = store.root / "jobs" / job / task["id"] / ("attempt-%d" % row["attempt"])
        workspace = directory / "workspace"
        store.event(job, task["id"], "workflow.dispatched", {"agent": a["id"], "role": task["workflow_role"], "call": call_num, "transport": a.get("transport")})
        rows_snapshot = {r["id"]: r for r in store.task_rows(job)}
        task_spec = json.loads(row["spec"])

    try:
        workspace.mkdir(parents=True, exist_ok=False)
        prepare_inputs(store, job, task_spec, rows_snapshot, workspace, a)
        (workspace / "task.json").write_text(dump(task_spec), encoding="utf-8")
        result = run_adapter(a, task_spec, workspace, directory, lambda: store.paused(job))
        result["workflow_role"] = task_spec["workflow_role"]
        if task_spec["workflow_role"] == "review":
            source = json.loads(rows_snapshot[task_spec["synthesis_task"]]["result"])
            first = source.get("model", {}).get("value")
            second = result.get("model", {}).get("value")
            author_models = sorted(
                {
                    json.loads(rows_snapshot[tid]["result"]).get("model", {}).get("value")
                    for tid in state.get("work_ids", {}).values()
                    if tid in rows_snapshot and rows_snapshot[tid].get("result")
                }
                - {None}
            )
            commander_transport = next((c["transport"] for c in config["catalog"] if c["id"] == config["commander"]), None)
            result["review_independence"] = {
                "different_agent": a["id"] != config["commander"],
                "different_model": bool(first and second and first != second),
                "different_transport": bool(commander_transport and a.get("transport") != commander_transport),
                "commander_model": first,
                "author_models": author_models,
                "reviewer_model": second,
            }
        with guard:
            store.record(job, row, result)
            (directory / "receipt.json").write_text(dump(result), encoding="utf-8")
    except Exception as exc:
        result = {"status": "failed", "output": "", "error": str(exc)}
        with guard:
            store.record(job, row, result)

    with guard:
        row, result = result_of(store, job, task["id"])
        if row["status"] == "waiting_external":
            state["waiting_task"] = task["id"]
            save_state(store, job, state)
            return None
        needs_repair = (
            row["status"] == "failed"
            and result.get("status") == "completed"
            and row["attempt"] < 2
            and not store.paused(job)
        )
        if needs_repair:
            corrected = json.loads(row["spec"])
            corrected["prompt"] += "\n上次结果未通过以下检查，请修正后重新提交：" + dump(result.get("verification", {}).get("errors", []))
            with store.connect() as con:
                con.execute("UPDATE tasks SET spec=?,status='pending' WHERE job=? AND id=?", (dump(corrected), job, task["id"]))
            store.event(job, task["id"], "workflow.response_repair")
    if needs_repair:
        return execute(store, job, config, state, corrected, lock=lock)
    return result if row["status"] == "done" else None


def _notes_suffix(state):
    notes = state.get("user_notes") or []
    if not notes:
        return ""
    lines = ["\n用户补充指令（按时间顺序，请务必遵循）："]
    for item in notes[-10:]:
        lines.append("- [%s] %s" % (item.get("at", ""), item.get("text", "")))
    return "\n".join(lines)


def finish_status(store, job, state):
    rows = store.task_rows(job)
    if store.paused(job):
        status = "paused"
    elif state["phase"] == "complete":
        status = "done"
    elif state["phase"] == "awaiting_approval":
        status = "awaiting_approval"
    elif any(r["status"] == "waiting_external" for r in rows):
        status = "waiting_external"
    else:
        status = "needs_attention"
    with store.connect() as con:
        con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job))
    return {"job": job, "status": status, "phase": state["phase"], "delivery": state.get("delivery")}


def _artifacts_all_previewable(store, job, state):
    total = 0
    for tid in state.get("work_ids", {}).values():
        row, res = result_of(store, job, tid)
        arts = res.get("verification", {}).get("artifacts", [])
        if not arts:
            continue
        source_ws = store.root / "jobs" / job / tid / ("attempt-%d" % row["attempt"]) / "workspace"
        for art in arts:
            p = inside(source_ws, art["path"])
            if p.suffix.lower() not in _TEXT_ARTIFACT_EXTS:
                return False
            size = p.stat().st_size
            if size > _MAX_PREVIEW_FILE_BYTES:
                return False
            total += size
            if total > _MAX_PREVIEW_TOTAL_BYTES:
                return False
    return True


def _build_work_task(config, state, step, cycle):
    sid = step["id"]
    deps = ["planning"] + [state["work_ids"][d] for d in step["dependencies"]]
    prompt = step["prompt"] + "\n原始目标：" + config["goal"] + _notes_suffix(state)
    if cycle:
        if state.get("work_ids", {}).get(sid):
            deps.append(state["work_ids"][sid])
        if state.get("review_id"):
            deps.append(state["review_id"])
        prompt += "\n根据最新审查意见与用户补充要求修正本步骤，不重复无关工作。"
    tools = ["files_read", "files_write"] if step["needs_files"] else []
    task = base_task("%s-r%d" % (sid, cycle), "work", step["agent"], prompt, list(dict.fromkeys(deps)), tools)
    task["kind"] = step["kind"]
    task["assignment_reason"] = step["reason"]
    if step["artifacts"]:
        task["validation"]["artifacts"] = [{"path": p, "min_bytes": 1} for p in step["artifacts"]]
        task["prompt"] += "\n必须在工作区生成以下成果：" + dump(step["artifacts"])
    return task


def run_locked(store, job):
    config, state = read_flow(store, job)
    state.pop("error", None)
    state.pop("waiting_task", None)
    lock = threading.Lock()
    while not store.paused(job):
        phase = state["phase"]
        if state.get("limit_reason") or phase in ("complete", "awaiting_approval"):
            break
        if phase == "planning":
            ptask = plan_task(config)
            if state.get("user_notes"):
                ptask["prompt"] += _notes_suffix(state)
            result = execute(store, job, config, state, ptask, lock=lock)
            if result is None:
                break
            parsed = parse_plan(result["output"], config)
            next_phase = "awaiting_approval" if (config.get("confirm_plan") and not state.get("plan_approved")) else "working"
            state.update(plan=parsed, phase=next_phase, work_ids={}, repair_steps=[])
            if next_phase == "awaiting_approval":
                store.event(job, None, "workflow.awaiting_approval", {"steps": len(parsed["steps"])})
                save_state(store, job, state)
                break
        elif phase == "working":
            cycle = state["cycle"]
            all_steps = state["plan"]["steps"]
            target_steps = [s for s in all_steps if not cycle or s["id"] in state["repair_steps"]]
            # Already completed steps in THIS cycle (e.g., resuming after pause/waiting_external)
            completed_this_cycle = set()
            for s in target_steps:
                expected_tid = "%s-r%d" % (s["id"], cycle)
                if state["work_ids"].get(s["id"]) == expected_tid:
                    completed_this_cycle.add(s["id"])
                else:
                    # Also check if task already finished in DB (e.g. auto-collected from waiting_external)
                    existing = next((r for r in store.task_rows(job) if r["id"] == expected_tid), None)
                    if existing and existing["status"] == "done":
                        state["work_ids"][s["id"]] = expected_tid
                        completed_this_cycle.add(s["id"])
            save_state(store, job, state)

            remaining = [s for s in target_steps if s["id"] not in completed_this_cycle]
            parallel_allowed = config.get("parallel_steps", True)
            max_workers = max(1, min(4, int(config.get("max_concurrency", 3))))

            while remaining and not store.paused(job):
                ready_sids = set(state["work_ids"].keys())
                # On revision cycle, a step depending on another repair step must wait for that repair step to finish in this cycle.
                wave = [
                    s
                    for s in remaining
                    if all(
                        (d in completed_this_cycle if (cycle and d in state["repair_steps"]) else d in ready_sids)
                        for d in s["dependencies"]
                    )
                ]
                if not wave:
                    state["error"] = "工作流步骤依赖无法满足，已停止"
                    save_state(store, job, state)
                    return finish_status(store, job, state)
                if len(wave) == 1 or not parallel_allowed or max_workers == 1:
                    step = wave[0]
                    task = _build_work_task(config, state, step, cycle)
                    res = execute(store, job, config, state, task, lock=lock)
                    if res is None:
                        return finish_status(store, job, state)
                    state["work_ids"][step["id"]] = task["id"]
                    completed_this_cycle.add(step["id"])
                    remaining = [s for s in remaining if s["id"] != step["id"]]
                    save_state(store, job, state)
                else:
                    # Execute independent steps concurrently across multiple Agents!
                    state["parallel_waves"] = state.get("parallel_waves", 0) + 1
                    store.event(
                        job,
                        None,
                        "workflow.parallel_wave",
                        {"cycle": cycle, "steps": [s["id"] for s in wave], "agents": [s["agent"] for s in wave]},
                    )
                    tasks_by_sid = {s["id"]: _build_work_task(config, state, s, cycle) for s in wave}
                    outcomes = {}
                    with ThreadPoolExecutor(max_workers=min(max_workers, len(wave))) as pool:
                        futs = {
                            pool.submit(execute, store, job, config, state, tasks_by_sid[s["id"]], lock): s
                            for s in wave
                        }
                        for fut in as_completed(futs):
                            step = futs[fut]
                            outcomes[step["id"]] = fut.result()
                    any_incomplete = False
                    for step in wave:
                        if outcomes.get(step["id"]) is not None:
                            state["work_ids"][step["id"]] = tasks_by_sid[step["id"]]["id"]
                            completed_this_cycle.add(step["id"])
                        else:
                            any_incomplete = True
                    save_state(store, job, state)
                    if any_incomplete:
                        return finish_status(store, job, state)
                    remaining = [s for s in remaining if s["id"] not in completed_this_cycle]
            if store.paused(job):
                break
            state["phase"] = "synthesizing"
        elif phase == "synthesizing":
            deps = ["planning"] + list(state["work_ids"].values())
            if state["cycle"] and state.get("review_id"):
                deps += [state["review_id"]]
            commander_entry = next(a for a in config["catalog"] if a["id"] == config["commander"])
            commander_name = commander_entry.get("name") or config["commander"]
            task = base_task(
                "synthesis-r%d" % state["cycle"],
                "synthesis",
                config["commander"],
                "你是总指挥（%s），请亲自分析并整合各步骤成果，写出可直接交付用户的完整 Markdown 正文。"
                "不是工作过程摘要，不是计划，不要仅罗列各 Agent 回复。纠正冲突，明确尚未证实的内容。"
                "不要声称存在未提供的文件或已执行未发生的操作。依照审查意见和用户补充要求完成修订（如有）。"
                "\n原始任务：%s\n验收要求：%s%s"
                % (commander_name, config["goal"], config["requirements"], _notes_suffix(state)),
                deps,
            )
            if (
                config["files_allowed"]
                and commander_entry["transport"] in FILE_TRANSPORTS
                and "files_read" in commander_entry.get("capabilities", [])
            ):
                task["allowed_tools"] = ["files_read"]
            result = execute(store, job, config, state, task, lock=lock)
            if result is None:
                break
            state.update(synthesis_id=task["id"], phase="reviewing")
        elif phase == "reviewing":
            candidates = [a for a in config["catalog"] if a["id"] != config["commander"]]
            has_files = any(result_of(store, job, tid)[1].get("verification", {}).get("artifacts") for tid in state["work_ids"].values())
            previewable_text_only = has_files and _artifacts_all_previewable(store, job, state)
            if has_files and not previewable_text_only:
                candidates = [
                    a
                    for a in candidates
                    if a["transport"] in FILE_TRANSPORTS and "files_read" in a.get("capabilities", [])
                ]
            if not candidates:
                state["error"] = "没有具备文件读取能力的独立复核 Agent，尚未交付"
                save_state(store, job, state)
                break
            commander_entry = next(a for a in config["catalog"] if a["id"] == config["commander"])
            cm = commander_entry.get("model")
            cp = commander_entry.get("provider_key")
            work_models = {
                next((c.get("model") for c in config["catalog"] if c["id"] == s["agent"]), None)
                for s in state["plan"]["steps"]
            } - {None}

            if config.get("require_cross_model"):
                cross_candidates = [
                    a
                    for a in candidates
                    if (cm and a.get("model") and a.get("model") != cm) or (cp and a.get("provider_key") != cp)
                ]
                if not cross_candidates:
                    state["error"] = "已启用“强制跨模型复核”，但没有与总指挥不同模型/供应商的可用复核 Agent"
                    save_state(store, job, state)
                    break
                candidates = cross_candidates

            # Prefer different model from BOTH commander and workers, then different model from commander,
            # then different provider/transport, then measured review pass-rate.
            def rank(a):
                am = a.get("model")
                ap = a.get("provider_key")
                samples = [m for m in a["measurements"] if m["kind"] == "review" and m["model"] == am]
                rate = sum(m["samples"] * m["success_rate"] for m in samples) / max(1, sum(m["samples"] for m in samples))
                return (
                    bool(am and cm and am != cm and am not in work_models),
                    bool(am and cm and am != cm),
                    bool(ap and cp and ap != cp),
                    rate,
                )

            reviewer_entry = sorted(candidates, key=rank, reverse=True)[0]
            reviewer = reviewer_entry["id"]
            task = base_task(
                "review-r%d" % state["cycle"],
                "review",
                reviewer,
                "作为独立任务上下文中的审查者，核对最终正文与各步骤证据（含附带的成果文件预览）是否满足原始目标及验收要求。"
                "不因作者声称成功就通过；不得要求超出任务授权的工具或服务。"
                '仅返回 JSON：{"decision":"accept 或 revise","reason":"具体审查理由",'
                '"issues":[{"step":"step1 或 synthesis","instruction":"具体修正要求"}]}。'
                "accept 时 issues 必须为空；revise 时必须列出问题。不得用模型自报身份作为证据。"
                "\n原始任务：" + config["goal"] + "\n验收要求：" + config["requirements"] + _notes_suffix(state),
                ["planning"] + list(state["work_ids"].values()) + [state["synthesis_id"]],
            )
            task.update(review_steps=list(state["work_ids"]), synthesis_task=state["synthesis_id"])
            if has_files and reviewer_entry["transport"] in FILE_TRANSPORTS and "files_read" in reviewer_entry.get("capabilities", []):
                task["allowed_tools"] = ["files_read"]
            result = execute(store, job, config, state, task, lock=lock)
            if result is None:
                break
            review = parse_review(result["output"], state["work_ids"])
            state["review_id"] = task["id"]
            if review["decision"] == "accept":
                state["phase"] = "delivering"
            elif state["cycle"] >= config["max_revisions"]:
                state["limit_reason"] = "审查未通过，已达到自动返工轮次上限；草稿和审查意见已保留"
            else:
                repair = {issue["step"] for issue in review["issues"] if issue["step"] != "synthesis"}
                # Recompute downstream work if any of its inputs is being revised.
                for step in state["plan"]["steps"]:
                    if repair.intersection(step["dependencies"]):
                        repair.add(step["id"])
                state.update(cycle=state["cycle"] + 1, repair_steps=sorted(repair), phase="working")
                store.event(job, None, "workflow.revision", {"cycle": state["cycle"], "steps": sorted(repair)})
        elif phase == "delivering":
            publish(store, job, state)
            state["phase"] = "complete"
            store.event(job, None, "workflow.delivered", state["delivery"])
        else:
            raise ValueError("未知工作流阶段")
        save_state(store, job, state)
    return finish_status(store, job, state)


def publish(store, job, state, human_approved=False, human_note=""):
    folder = inside(store.root, "jobs/" + job + "/delivery")
    folder.mkdir(exist_ok=True)
    _, synthesis = result_of(store, job, state["synthesis_id"])
    text = synthesis["output"]
    target = folder / "final.md"
    temp = folder / "final.md.tmp"
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)
    files = [
        {
            "path": "final.md",
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    ]
    for sid, tid in state.get("work_ids", {}).items():
        row, result = result_of(store, job, tid)
        source_ws = store.root / "jobs" / job / tid / ("attempt-%d" % row["attempt"]) / "workspace"
        for art in result.get("verification", {}).get("artifacts", []):
            source = inside(source_ws, art["path"])
            if hashlib.sha256(source.read_bytes()).hexdigest() != art["sha256"]:
                raise ValueError("交付前发现成果变化：" + art["path"])
            rel = sid + "/" + art["path"]
            dest = inside(folder, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            files.append(dict(art, path=rel))

    review = {}
    if state.get("review_id"):
        try:
            _, review = result_of(store, job, state["review_id"])
        except StopIteration:
            review = {}

    # Aggregate verified multi-agent & cross-model collaboration evidence across all done tasks.
    all_rows = store.task_rows(job)
    agents_used, observed_models, configured_models, transports_used = set(), set(), set(), set()
    for r in all_rows:
        if r["status"] != "done" or not r.get("result"):
            continue
        if r.get("agent"):
            agents_used.add(r["agent"])
        res = json.loads(r["result"])
        if res.get("transport"):
            transports_used.add(res["transport"])
        obs = (res.get("model") or {}).get("value") if isinstance(res.get("model"), dict) else None
        if obs:
            observed_models.add(obs)
        cfg = (res.get("configured_model") or {}).get("value") if isinstance(res.get("configured_model"), dict) else None
        if cfg:
            configured_models.add(cfg)

    state["delivery"] = {
        "directory": str(folder),
        "files": files,
        "review": review.get("output", ""),
        "independence": review.get("review_independence"),
        "human_approved": bool(human_approved),
        "human_note": str(human_note or "").strip(),
        "collaboration": {
            "agents_used": sorted(agents_used),
            "models_observed": sorted(observed_models),
            "models_configured": sorted(configured_models),
            "transports_used": sorted(transports_used),
            "cross_model_verified": len(observed_models) >= 2,
            "parallel_waves": state.get("parallel_waves", 0),
        },
        "completed_at": now(),
    }
    (folder / "manifest.json").write_text(dump(state["delivery"]), encoding="utf-8")


def _ensure_not_running(store, job):
    rows = store.task_rows(job)
    if any(r["status"] == "running" for r in rows):
        raise ValueError("该任务目前有步骤正在执行，请等待该步骤完成或先暂停后再操作")


def approve_plan(store, job, edited_steps=None, user_note=""):
    """Human-in-the-loop approval (and optional step editing) for a plan paused in awaiting_approval."""
    identifier(job)
    store.paused(job)
    flow = read_flow(store, job)
    if not flow:
        raise ValueError("该任务不是协作工作流")
    config, state = flow
    if state.get("phase") != "awaiting_approval":
        raise ValueError("当前任务不在“等待人工确认分工”阶段")
    _ensure_not_running(store, job)

    plan = state.get("plan")
    if not isinstance(plan, dict) or not plan.get("steps"):
        raise ValueError("尚无待确认的规划方案")

    if edited_steps is not None:
        if not isinstance(edited_steps, list):
            raise ValueError("修改后的步骤必须为列表")
        merged_steps = []
        orig_by_id = {s["id"]: s for s in plan["steps"]}
        for i, item in enumerate(edited_steps, 1):
            if not isinstance(item, dict):
                raise ValueError("步骤配置无效")
            sid = item.get("id") or ("step%d" % i)
            base = dict(orig_by_id.get(sid, {}))
            base.update(item)
            base["id"] = "step%d" % i
            base.setdefault("reason", "人工确认并微调分工")
            base.setdefault("dependencies", [])
            base.setdefault("needs_files", False)
            base.setdefault("artifacts", [])
            base.setdefault("kind", "general")
            merged_steps.append(base)
        # Re-validate strictly through parse_plan so no tool/agent/path invariant is ever bypassed.
        plan = parse_plan(dump({"analysis": plan["analysis"], "steps": merged_steps}), config)

    clean_note = str(user_note or "").strip()
    if clean_note:
        if len(clean_note) > 4000:
            raise ValueError("补充说明最多 4000 字")
        state.setdefault("user_notes", []).append({"at": now(), "text": clean_note, "kind": "plan_approval"})

    state["plan"] = plan
    state["plan_approved"] = True
    state["phase"] = "working"
    state.pop("error", None)
    with store.connect() as con:
        con.execute("UPDATE workflows SET state=? WHERE job=?", (dump(state), job))
        con.execute("UPDATE jobs SET paused=0,status='pending' WHERE id=?", (job,))
    store.event(job, None, "workflow.plan_approved", {"steps": len(plan["steps"]), "edited": edited_steps is not None})
    return state


def revise_plan(store, job, feedback):
    """Reject the proposed plan and ask the commander to re-plan with explicit user feedback."""
    identifier(job)
    store.paused(job)
    flow = read_flow(store, job)
    if not flow:
        raise ValueError("该任务不是协作工作流")
    config, state = flow
    if state.get("phase") != "awaiting_approval":
        raise ValueError("当前任务不在“等待人工确认分工”阶段")
    _ensure_not_running(store, job)
    clean = str(feedback or "").strip()
    if not 1 <= len(clean) <= 4000:
        raise ValueError("请填写规划修改意见（1–4000 字）")

    state.setdefault("user_notes", []).append({"at": now(), "text": "【重规划要求】" + clean, "kind": "plan_feedback"})
    state["phase"] = "planning"
    state["plan_approved"] = False
    state.pop("plan", None)
    state.pop("error", None)
    state.pop("limit_reason", None)
    if state.get("calls", 0) >= config["max_calls"]:
        config["max_calls"] = state["calls"] + 4

    new_ptask = plan_task(config)
    new_ptask["prompt"] += _notes_suffix(state)
    with store.connect() as con:
        con.execute("UPDATE workflows SET config=?,state=? WHERE job=?", (dump(config), dump(state), job))
        con.execute(
            "UPDATE tasks SET spec=?,status='pending',result=NULL WHERE job=? AND id='planning'",
            (dump(new_ptask), job),
        )
        con.execute("UPDATE jobs SET paused=0,status='pending' WHERE id=?", (job,))
    store.event(job, None, "workflow.plan_revised", {"feedback": clean[:200]})
    return state


def inject_workflow_note(store, job, note="", upload_ids=None, local_paths=None, attachment_approval=None):
    """Attach mid-flight instructions and/or supplementary files to an active or paused job."""
    identifier(job)
    store.paused(job)
    clean = str(note or "").strip()
    upload_ids = list(upload_ids or [])
    local_paths = list(local_paths or [])
    if not clean and not upload_ids and not local_paths:
        raise ValueError("请至少填写补充指令，或上传/挂载至少一份资料")
    if len(clean) > 4000:
        raise ValueError("补充指令最多 4000 字")

    flow = read_flow(store, job)
    attachments = []
    if upload_ids or local_paths:
        from .attachments import approved_inputs, attach_job_inputs

        if not flow:
            raise ValueError("追加附件需要已创建的工作流")
        bundle = approved_inputs(store, {"local_paths": local_paths, "upload_ids": upload_ids,
                                         "attachment_approval": attachment_approval}, flow[0]["participants"])
        attachments = attach_job_inputs(store, job, bundle)

    if flow:
        config, state = flow
        if clean:
            state.setdefault("user_notes", []).append({"at": now(), "text": clean, "kind": "note"})
            save_state(store, job, state)
    store.event(
        job,
        None,
        "workflow.user_note_injected",
        {"has_note": bool(clean), "attachments_total": len(attachments)},
    )
    return {"note_added": bool(clean), "attachments": attachments}


def approve_draft(store, job, note=""):
    """Human-in-the-loop acceptance: publish the existing synthesis draft directly as final delivery."""
    identifier(job)
    store.paused(job)
    flow = read_flow(store, job)
    if not flow:
        raise ValueError("该任务不是协作工作流")
    _, state = flow
    if state.get("phase") == "complete":
        raise ValueError("该任务已完成交付")
    _ensure_not_running(store, job)
    syn_id = state.get("synthesis_id")
    if not syn_id:
        raise ValueError("当前尚未生成可供拍板的汇总草稿")
    row, _ = result_of(store, job, syn_id)
    if row["status"] != "done":
        raise ValueError("汇总草稿尚未完成，不能直接采纳")

    clean_note = str(note or "").strip()
    if len(clean_note) > 2000:
        raise ValueError("人工拍板备注最多 2000 字")
    publish(store, job, state, human_approved=True, human_note=clean_note)
    state["phase"] = "complete"
    state.pop("limit_reason", None)
    state.pop("error", None)
    with store.connect() as con:
        con.execute("UPDATE workflows SET state=? WHERE job=?", (dump(state), job))
        con.execute("UPDATE jobs SET paused=0,status='done' WHERE id=?", (job,))
    store.event(job, None, "workflow.human_approved", {"note": clean_note[:200]})
    store.event(job, None, "workflow.delivered", state["delivery"])
    return state["delivery"]


def request_revision(store, job, feedback, steps=None):
    """Trigger a human-directed revision cycle on selected steps (or synthesis only) with explicit feedback."""
    identifier(job)
    store.paused(job)
    flow = read_flow(store, job)
    if not flow:
        raise ValueError("该任务不是协作工作流")
    config, state = flow
    if not state.get("plan"):
        raise ValueError("尚未生成规划步骤，无法发起返工")
    _ensure_not_running(store, job)
    clean = str(feedback or "").strip()
    if not 1 <= len(clean) <= 4000:
        raise ValueError("请填写返工修改要求（1–4000 字）")

    valid_step_ids = {s["id"] for s in state["plan"]["steps"]}
    repair = set()
    if steps:
        if not isinstance(steps, list) or not all(isinstance(s, str) and s in valid_step_ids for s in steps):
            raise ValueError("指定的返工步骤无效")
        repair = set(steps)
        # Include downstream dependent steps so dependency artifacts stay consistent.
        changed = True
        while changed:
            changed = False
            for s in state["plan"]["steps"]:
                if s["id"] not in repair and repair.intersection(s["dependencies"]):
                    repair.add(s["id"])
                    changed = True

    if not repair and not state.get("work_ids"):
        repair = set(valid_step_ids)
    state.setdefault("user_notes", []).append({"at": now(), "text": "【人工返工要求】" + clean, "kind": "revision_feedback"})
    state["cycle"] = state.get("cycle", 0) + 1
    state["repair_steps"] = sorted(repair)
    state["phase"] = "working" if repair else "synthesizing"
    state.pop("limit_reason", None)
    state.pop("error", None)
    state.pop("delivery", None)

    needed_calls = len(repair) + 2
    if state.get("calls", 0) + needed_calls > config["max_calls"]:
        config["max_calls"] = min(64, state.get("calls", 0) + needed_calls + 2)
    if state["cycle"] > config["max_revisions"]:
        config["max_revisions"] = state["cycle"]

    with store.connect() as con:
        con.execute("UPDATE workflows SET config=?,state=? WHERE job=?", (dump(config), dump(state), job))
        con.execute("UPDATE jobs SET paused=0,status='pending' WHERE id=?", (job,))
    store.event(job, None, "workflow.human_revision_requested", {"cycle": state["cycle"], "steps": sorted(repair)})
    return state
