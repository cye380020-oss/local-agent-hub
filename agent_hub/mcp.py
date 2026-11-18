"""Small local MCP stdio server. JSON-RPC is the only content written to stdout.

Default mode exposes the 6 core orchestration tools. Passing --extended enables
additional multi-agent workflow creation and external Worker pull/push tools.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from .core import Store, collect_ready_results, dump, identifier
from .worker_bridge import claim_task, complete_task, poll_tasks, report_progress
from .workflow import create_flow


PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


def _object(properties=None, required=()):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


_job = {"type": "string", "description": "已存在的任务组标识"}
TOOLS = [
    {
        "name": "hub_status",
        "description": "读取本机 Agent 接入证据、任务状态、实时进度、验收记录和能力小测统计。",
        "inputSchema": _object(),
    },
    {
        "name": "hub_submit_plan",
        "description": "提交可信任务计划及验收条件。只保存计划，不自动执行。任务包含 id、prompt 和 validation。",
        "inputSchema": _object({"plan": {"type": "object", "description": "含 title 和 tasks 的任务 DAG"}}, ["plan"]),
    },
    {
        "name": "hub_run",
        "description": "在后台执行已提交的本机任务组。可能消耗现有客户端订阅额度；返回启动信息，随后用 hub_status 检查真实结果。",
        "inputSchema": _object({"job": _job}, ["job"]),
    },
    {
        "name": "hub_pause",
        "description": "暂停任务组；正在运行的适配器通过协作取消退出，完成中的外部应用操作不能保证撤销。",
        "inputSchema": _object({"job": _job}, ["job"]),
    },
    {
        "name": "hub_resume",
        "description": "恢复暂停的任务组并在后台继续。中断或失败的任务仍需人工检查和显式重试。",
        "inputSchema": _object({"job": _job}, ["job"]),
    },
    {
        "name": "hub_import_result",
        "description": "为等待外部客户端的任务导入真实成果对象，并应用计划验收条件。模型自报信息标记为未验证；成果文件必须已在该任务工作区内。",
        "inputSchema": _object(
            {
                "job": _job,
                "task": {"type": "string"},
                "result": {"type": "object", "description": "output 文本和可选 artifacts 相对路径列表"},
            },
            ["job", "task", "result"],
        ),
    },
]

EXTENDED_TOOLS = TOOLS + [
    {
        "name": "hub_list_agents",
        "description": "列出所有可用 Agent、模型配置、传输方式、能力标签及在线 Worker 状态。",
        "inputSchema": _object(),
    },
    {
        "name": "hub_create_workflow",
        "description": "创建并启动多 Agent 跨模型自动协作工作流（分析分工 → 并行执行 → 整合 → 跨模型复核 → 交付）。",
        "inputSchema": _object(
            {
                "goal": {"type": "string", "description": "任务目标"},
                "requirements": {"type": "string", "description": "验收要求"},
                "commander": {"type": "string", "description": "指挥 Agent ID"},
                "participants": {"type": "array", "description": "参与 Agent ID 列表"},
                "files_allowed": {"type": "boolean", "description": "是否允许工作区文件操作"},
                "require_cross_model": {"type": "boolean", "description": "是否强制跨模型协作与复核"},
                "parallel_steps": {"type": "boolean", "description": "是否并行执行独立子步骤"},
            },
            ["goal"],
        ),
    },
    {
        "name": "hub_poll_tasks",
        "description": "外部 MCP Agent 轮询分配给自己的待执行任务并刷新在线心跳。",
        "inputSchema": _object(
            {
                "agent": {"type": "string", "description": "目标 Agent ID"},
                "worker_id": {"type": "string", "description": "Worker 实例标识"},
                "model": {"type": "string", "description": "当前模型标识"},
            },
            ["agent"],
        ),
    },
    {
        "name": "hub_claim_task",
        "description": "认领一个队列中的派发任务并标记为执行中。",
        "inputSchema": _object(
            {
                "job": _job,
                "task": {"type": "string"},
                "attempt": {"type": "integer"},
                "agent": {"type": "string"},
                "worker_id": {"type": "string"},
            },
            ["job", "task", "attempt", "agent", "worker_id"],
        ),
    },
    {
        "name": "hub_report_progress",
        "description": "向工作台上报当前任务的实时阶段和进展说明。",
        "inputSchema": _object(
            {
                "job": _job,
                "task": {"type": "string"},
                "attempt": {"type": "integer"},
                "worker_id": {"type": "string"},
                "stage": {"type": "string"},
                "message": {"type": "string"},
                "model": {"type": "string"},
            },
            ["job", "task", "attempt", "stage", "message"],
        ),
    },
    {
        "name": "hub_complete_task",
        "description": "提交已认领任务的最终文本与成果路径，触发工作台自动验收并继续后续协作步骤。",
        "inputSchema": _object(
            {
                "job": _job,
                "task": {"type": "string"},
                "attempt": {"type": "integer"},
                "worker_id": {"type": "string"},
                "output": {"type": "string"},
                "artifacts": {"type": "array"},
                "model": {"type": "string"},
                "failed": {"type": "boolean"},
                "error": {"type": "string"},
            },
            ["job", "task", "attempt", "worker_id"],
        ),
    },
]

TOOL_MAP = {tool["name"]: tool for tool in TOOLS}


class MCPServer:
    def __init__(self, store, extended=False):
        self.store = store
        self.extended = bool(extended)
        self.tools = EXTENDED_TOOLS if self.extended else TOOLS
        self.tool_map = {tool["name"]: tool for tool in self.tools}
        self.children = {}

    def _background_run(self, job):
        identifier(job)
        if self.store.paused(job):
            return {"job": job, "status": "paused", "message": "先恢复任务组，再执行"}
        self.children = {key: child for key, child in self.children.items() if child.poll() is None}
        if job in self.children:
            return {"job": job, "status": "already_running", "pid": self.children[job].pid}
        logs = self.store.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / ("runner-" + job + "-" + uuid.uuid4().hex[:10] + ".log")
        project = Path(__file__).resolve().parents[1]
        allowed_env = {
            "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
            "LC_CTYPE", "TMPDIR", "TMP", "TEMP", "CODEX_HOME", "SYSTEMROOT",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed_env}
        environment["PYTHONPATH"] = str(project)
        command = [sys.executable, "-m", "agent_hub", "--root", str(self.store.root), "run", job, "--wait"]
        with log_path.open("ab") as log:
            child = subprocess.Popen(
                command,
                cwd=str(project),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        self.children[job] = child
        self.store.event(job, None, "mcp.runner_started", {"pid": child.pid, "log": str(log_path)})
        return {
            "job": job,
            "status": "started",
            "pid": child.pid,
            "log": str(log_path),
            "message": "已加入串行执行队列；后台启动不代表任务完成，请读取 hub_status",
        }

    def call(self, name, arguments):
        if not isinstance(name, str) or name not in self.tool_map:
            raise ValueError("未知工具: " + str(name))
        if not isinstance(arguments, dict):
            raise ValueError("arguments 必须是对象")
        schema = self.tool_map[name]["inputSchema"]
        if set(arguments) - set(schema["properties"]):
            raise ValueError("包含未知工具参数")
        if not set(schema["required"]) <= set(arguments):
            raise ValueError("缺少必需工具参数")
        for key, value in arguments.items():
            expected = schema["properties"][key]["type"]
            if expected == "string" and not isinstance(value, str):
                raise ValueError(key + " 必须是字符串")
            if expected == "object" and not isinstance(value, dict):
                raise ValueError(key + " 必须是对象")
            if expected == "array" and not isinstance(value, list):
                raise ValueError(key + " 必须是数组")
            if expected == "boolean" and type(value) is not bool:
                raise ValueError(key + " 必须是布尔值")
            if expected == "integer" and (type(value) is not int):
                raise ValueError(key + " 必须是整数")
        if name == "hub_status":
            snapshot = self.store.snapshot()
            # MCP clients may forward results to an independently configured provider.
            snapshot.pop("uploads", None)
            private_jobs = {j["id"] for j in snapshot["jobs"] if j.get("attachments")}
            for job in snapshot["jobs"]:
                if job["id"] in private_jobs:
                    public = {k: job[k] for k in ("id", "status", "paused")}
                    job.clear()
                    job.update(public)
            snapshot["events"] = [e for e in snapshot["events"] if e["job"] not in private_jobs]
            return snapshot
        if name == "hub_list_agents":
            snap = self.store.snapshot()
            return {"agents": snap["agents"], "online_workers": snap["online_workers"]}
        if name == "hub_submit_plan":
            return {"job": self.store.add_plan(arguments["plan"]), "status": "submitted"}
        if name == "hub_create_workflow":
            job = create_flow(self.store, arguments)
            return {"job": job, "continuation": self._background_run(job)}
        if name == "hub_poll_tasks":
            tasks = poll_tasks(
                self.store,
                arguments["agent"],
                worker_id=arguments.get("worker_id"),
                model=arguments.get("model"),
            )
            return {"tasks": tasks}
        if name == "hub_claim_task":
            return claim_task(
                self.store,
                arguments["job"],
                arguments["task"],
                arguments["attempt"],
                arguments["agent"],
                arguments["worker_id"],
            )
        if name == "hub_report_progress":
            return report_progress(
                self.store,
                arguments["job"],
                arguments["task"],
                arguments["attempt"],
                arguments.get("worker_id", "mcp-worker"),
                arguments["stage"],
                arguments["message"],
                model=arguments.get("model"),
            )
        if name == "hub_complete_task":
            claim = complete_task(
                self.store,
                arguments["job"],
                arguments["task"],
                arguments["attempt"],
                arguments["worker_id"],
                output=arguments.get("output", ""),
                artifacts=arguments.get("artifacts"),
                model=arguments.get("model"),
                failed=arguments.get("failed", False),
                error=arguments.get("error"),
            )
            resumed = []
            for jid in collect_ready_results(self.store):
                resumed.append(self._background_run(jid))
            return {"claim": claim, "resumed": resumed}
        job = identifier(arguments["job"])
        if name == "hub_run":
            return self._background_run(job)
        if name in {"hub_pause", "hub_resume"}:
            self.store.control(job, name == "hub_pause")
            return {"job": job, "status": "paused"} if name == "hub_pause" else self._background_run(job)
        imported = self.store.import_result(job, identifier(arguments["task"]), arguments["result"])
        return {"job": job, "result": imported, "continuation": self._background_run(job)}

    def handle(self, request):
        """Return a JSON-RPC response, or None for protocol notifications."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self.error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        if "id" not in request:
            return None
        if request_id is not None and (not isinstance(request_id, (str, int)) or isinstance(request_id, bool)):
            return self.error(None, -32600, "Invalid Request id")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "params must be an object")
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "local-agent-hub", "version": "2.0.0"},
                "instructions": "本机多 Agent 协同工作台 MCP 工具。接入状态以 hub_status 证据为准；启动进程不等于成功完成。",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            if not isinstance(params.get("name"), str) or params["name"] not in self.tool_map:
                return self.error(request_id, -32602, "Unknown tool")
            try:
                payload = self.call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": dump(payload)}]}
            except Exception as exc:
                result = {"content": [{"type": "text", "text": "工具执行失败：" + str(exc)}], "isError": True}
        else:
            return self.error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(store, extended=False):
    server = MCPServer(store, extended=extended)
    reader = sys.stdin.buffer
    while True:
        line = reader.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_MESSAGE_BYTES:
            # Never parse or execute a truncated oversized message.
            while line and not line.endswith(b"\n"):
                line = reader.readline(MAX_MESSAGE_BYTES + 1)
            response = server.error(None, -32600, "Message exceeds 2 MiB")
        else:
            try:
                response = server.handle(json.loads(line))
            except (ValueError, UnicodeError):
                response = server.error(None, -32700, "Parse error")
            except Exception:
                response = server.error(None, -32603, "Internal error")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Local Agent Hub MCP stdio")
    parser.add_argument("--root", required=True)
    parser.add_argument("--extended", action="store_true", help="启用扩展工作流和外部 Worker 接单工具")
    ns = parser.parse_args()
    serve(Store(ns.root), extended=ns.extended)
