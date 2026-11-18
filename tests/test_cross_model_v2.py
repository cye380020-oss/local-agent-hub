"""v2.0 tests: multi-agent parallel linkage, true cross-model collaboration, live tracking, CLI/API/Worker transports, and auto-collection."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from agent_hub.adapters import run_adapter
from agent_hub.api_worker import infer, list_models
from agent_hub.core import Store, collect_ready_results, run_job
from agent_hub.management import endpoint, save_agent, sync_api_models
from agent_hub.mcp import MCPServer
from agent_hub.progress import read_progress
from agent_hub.worker_bridge import (
    claim_task,
    complete_task,
    ensure_worker_token,
    poll_tasks,
    register_heartbeat,
    report_progress,
)
from agent_hub.workflow import create_flow, read_flow


class CrossModelAndTransportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "workspace")

    def _make_script(self, name, body):
        path = self.root / name
        path.write_text("#!" + sys.executable + "\n" + body + "\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def test_dsh_cli_adapter_headless_events_patch_sandbox_and_artifacts(self):
        exe = self._make_script(
            "dsh-fixture",
            """
import json, os, pathlib, sys
if '--help' in sys.argv:
    print('--profile --patch --json --permission-mode --approval-policy')
    sys.exit(0)
prompt = sys.stdin.read()
patch_text = ''
if '--patch' in sys.argv:
    patch_text = pathlib.Path(sys.argv[sys.argv.index('--patch') + 1]).read_text(encoding='utf-8')
pathlib.Path('dsh-out.md').write_text('verified dsh artifact', encoding='utf-8')
print(json.dumps({'type': 'session', 'sessionId': 'session-1', 'model': 'deepseek-v4.1-flash-1017'}), flush=True)
print(json.dumps({'type': 'status', 'phase': 'turn_start', 'turn': 1}), flush=True)
print(json.dumps({'type': 'status', 'phase': 'step_start', 'turn': 1, 'step': 1}), flush=True)
print(json.dumps({
    'type': 'final',
    'sessionId': 'session-1',
    'model': 'deepseek-v4.1-flash-1017',
    'reason': {'kind': 'completed'},
    'text': json.dumps({
        'ok': True,
        'mode': sys.argv[sys.argv.index('--permission-mode') + 1],
        'approval': sys.argv[sys.argv.index('--approval-policy') + 1],
        'has_key_in_env': os.environ.get('DEEPSEEK_API_KEY') == 'sk-dsh-secret-123456',
        'patch_has_model': 'deepseek-v4.1-flash-1017' in patch_text,
        'echo_secret': os.environ.get('DEEPSEEK_API_KEY', ''),
        'prompt_len': len(prompt),
    }),
}), flush=True)
""",
        )
        aid = save_agent(
            self.store,
            {
                "id": "hub-deepseek",
                "name": "DeepSeek Harness Fixture",
                "transport": "dsh_cli",
                "executable": exe,
                "dsh_provider": "deepseek-official",
                "model": "deepseek-v4.1-flash-1017",
                "key": "sk-dsh-secret-123456",
                "auto_route": True,
            },
        )
        agent = next(a for a in self.store.agents() if a["id"] == aid)
        agent["kind"] = "test_fixture"
        attempt = self.store.root / "jobs" / "j1" / "t1" / "attempt-1"
        ws = attempt / "workspace"
        res = run_adapter(
            agent,
            {"id": "t1", "prompt": "生成报告", "allowed_tools": ["files_read", "files_write"]},
            ws,
            attempt,
            lambda: False,
        )
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["sandbox"], "workspace-write")
        self.assertEqual(res["model"]["value"], "deepseek-v4.1-flash-1017")
        self.assertEqual(res["artifacts"], ["dsh-out.md"])
        self.assertNotIn("sk-dsh-secret-123456", res["output"])
        parsed = json.loads(res["output"])
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["mode"], "workspace-write")
        self.assertEqual(parsed["approval"], "never")
        self.assertTrue(parsed["has_key_in_env"])
        self.assertTrue(parsed["patch_has_model"])
        prog = read_progress(attempt)
        self.assertIsNotNone(prog)
        self.assertEqual(prog["status"], "completed")
        self.assertGreaterEqual(prog["events_count"], 2)

    def test_claude_cli_and_custom_cli_adapters_with_live_progress(self):
        claude_exe = self._make_script(
            "claude-fixture",
            """
import json, sys
if '--help' in sys.argv:
    print('-p --print --output-format --permission-mode --model')
    sys.exit(0)
_ = sys.stdin.read()
print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-4-5'}), flush=True)
print(json.dumps({'type': 'result', 'is_error': False, 'result': 'Claude verified output'}), flush=True)
""",
        )
        aid = save_agent(
            self.store,
            {"id": "claude-local", "name": "Claude", "transport": "claude_cli", "executable": claude_exe, "model": "claude-sonnet-4-5"},
        )
        agent = next(a for a in self.store.agents() if a["id"] == aid)
        attempt = self.store.root / "jobs" / "jc" / "t1" / "attempt-1"
        res = run_adapter(agent, {"id": "t1", "prompt": "分析", "allowed_tools": []}, attempt / "workspace", attempt, lambda: False)
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["model"]["value"], "claude-sonnet-4-5")
        self.assertEqual(res["output"], "Claude verified output")

        custom_exe = self._make_script(
            "custom-fixture",
            """
import json, pathlib, sys
env = json.loads(sys.stdin.read())
print(json.dumps({'type': 'progress', 'stage': 'custom:working', 'message': 'step 1', 'model': 'kimi-k3'}), flush=True)
pathlib.Path('custom.txt').write_text('custom artifact', encoding='utf-8')
print(json.dumps({'output': 'Custom connector ok for ' + env['task']['id'], 'model': 'kimi-k3'}), flush=True)
""",
        )
        cid = save_agent(
            self.store,
            {"id": "custom-kimi", "name": "Kimi Script", "transport": "custom_cli", "executable": custom_exe, "model": "kimi-k3", "allow_files": True},
        )
        cagent = next(a for a in self.store.agents() if a["id"] == cid)
        attempt2 = self.store.root / "jobs" / "jc" / "t2" / "attempt-1"
        res2 = run_adapter(
            cagent,
            {"id": "t2", "prompt": "写文件", "allowed_tools": ["files_read", "files_write"]},
            attempt2 / "workspace",
            attempt2,
            lambda: False,
        )
        self.assertEqual(res2["status"], "completed", res2)
        self.assertEqual(res2["model"]["value"], "kimi-k3")
        self.assertEqual(res2["artifacts"], ["custom.txt"])
        self.assertIn("t2", res2["output"])

    def test_parallel_multi_agent_workflow_and_cross_model_verification(self):
        agents = [
            {
                "id": "gpt",
                "name": "GPT 指挥",
                "transport": "codex_cli",
                "execution_model": "gpt-6-astra",
                "model": {"value": "gpt-6-astra"},
                "capabilities": ["general", "reasoning", "coding", "writing_zh", "review", "files_read", "files_write"],
                "automation": {"status": "verified"},
            },
            {
                "id": "dsh",
                "name": "DeepSeek 执行",
                "transport": "dsh_cli",
                "dsh_enabled": True,
                "dsh_provider": "deepseek-official",
                "execution_model": "deepseek-v4.1-flash-1017",
                "model": {"value": "deepseek-v4.1-flash-1017"},
                "capabilities": ["general", "reasoning", "coding", "writing_zh", "review", "files_read", "files_write"],
                "automation": {"status": "verified"},
            },
            {
                "id": "gemini",
                "name": "Gemini 审校",
                "transport": "http_api",
                "protocol": "gemini",
                "endpoint": "https://generativelanguage.googleapis.com/v1beta",
                "auto_route": True,
                "model": {"value": "gemini-2.5-pro"},
                "capabilities": ["general", "reasoning", "coding", "writing_zh", "review"],
                "automation": {"status": "available"},
            },
        ]
        self.store.init({"agents": agents})
        job = create_flow(
            self.store,
            {
                "goal": "跨模型协同完成系统架构方案与审校",
                "requirements": "结构清晰、包含独立分析与代码文件并经跨模型复核",
                "commander": "gpt",
                "participants": ["gpt", "dsh", "gemini"],
                "files_allowed": True,
                "require_cross_model": True,
                "parallel_steps": True,
                "max_steps": 3,
            },
        )

        # Barrier proves step1 (DeepSeek) and step2 (Gemini) truly run concurrently!
        parallel_barrier = threading.Barrier(2, timeout=5)
        parallel_confirmed = [False]
        seen_review_context = []
        model_map = {"gpt": "gpt-6-astra", "dsh": "deepseek-v4.1-flash-1017", "gemini": "gemini-2.5-pro"}

        def fake_adapter(a, task, workspace, directory, stopped):
            role = task["workflow_role"]
            aid = a["id"]
            if role == "planning":
                output = json.dumps(
                    {
                        "analysis": "将任务拆分为 DeepSeek 文件产出与 Gemini 理论分析两个并行步骤，再统一汇总与跨模型复核。",
                        "steps": [
                            {
                                "id": "step1",
                                "agent": "dsh",
                                "kind": "coding",
                                "prompt": "生成架构设计文档 analysis.md",
                                "reason": "DeepSeek Harness 支持工作区文件写入",
                                "dependencies": [],
                                "needs_files": True,
                                "artifacts": ["analysis.md"],
                            },
                            {
                                "id": "step2",
                                "agent": "gemini",
                                "kind": "reasoning",
                                "prompt": "分析跨模型风险与验收清单",
                                "reason": "Gemini 独立视角并行分析",
                                "dependencies": [],
                                "needs_files": False,
                                "artifacts": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                )
                return {
                    "status": "completed",
                    "output": output,
                    "artifacts": [],
                    "transport": a["transport"],
                    "model": {"value": model_map[aid], "source": "fixture"},
                    "is_test_fixture": True,
                    "elapsed_seconds": 0.02,
                }
            if role == "work":
                parallel_barrier.wait()
                parallel_confirmed[0] = True
                artifacts = []
                if task["id"].startswith("step1"):
                    (workspace / "analysis.md").write_text("# DeepSeek 架构分析\n完整内容", encoding="utf-8")
                    artifacts = ["analysis.md"]
                return {
                    "status": "completed",
                    "output": f"{aid} 已完成独立分工步骤 {task['id']}",
                    "artifacts": artifacts,
                    "transport": a["transport"],
                    "model": {"value": model_map[aid], "source": "fixture"},
                    "is_test_fixture": True,
                    "elapsed_seconds": 0.02,
                }
            if role == "synthesis":
                return {
                    "status": "completed",
                    "output": "# 最终整合交付\n综合了 DeepSeek 与 Gemini 的并行成果。",
                    "artifacts": [],
                    "transport": a["transport"],
                    "model": {"value": model_map[aid], "source": "fixture"},
                    "is_test_fixture": True,
                    "elapsed_seconds": 0.02,
                }
            if role == "review":
                seen_review_context.append((aid, task.get("context", "")))
                return {
                    "status": "completed",
                    "output": json.dumps({"decision": "accept", "reason": "跨模型核验文件预览与正文均符合要求", "issues": []}, ensure_ascii=False),
                    "artifacts": [],
                    "transport": a["transport"],
                    "model": {"value": model_map[aid], "source": "fixture"},
                    "is_test_fixture": True,
                    "elapsed_seconds": 0.02,
                }
            raise AssertionError("Unexpected role: " + role)

        with patch("agent_hub.core.run_adapter", side_effect=fake_adapter):
            outcome = run_job(self.store, job)

        self.assertEqual(outcome["status"], "done")
        self.assertTrue(parallel_confirmed[0], "Independent workflow steps must execute in parallel")
        _, state = read_flow(self.store, job)
        delivery = state["delivery"]
        self.assertTrue(delivery["independence"]["different_agent"])
        self.assertTrue(delivery["independence"]["different_model"])
        self.assertTrue(delivery["collaboration"]["cross_model_verified"])
        self.assertEqual(set(delivery["collaboration"]["models_observed"]), {"gpt-6-astra", "deepseek-v4.1-flash-1017", "gemini-2.5-pro"})
        self.assertEqual(delivery["collaboration"]["parallel_waves"], 1)
        # Verify small UTF-8 file preview was attached in review context so cross-model reviewers could inspect it.
        self.assertIn("DeepSeek 架构分析", seen_review_context[0][1])
        self.assertTrue((Path(delivery["directory"]) / "step1" / "analysis.md").is_file())

    def test_require_cross_model_rejects_same_model_participants(self):
        same_agents = [
            {
                "id": "gpt-a",
                "name": "GPT A",
                "transport": "codex_cli",
                "execution_model": "gpt-6-astra",
                "model": {"value": "gpt-6-astra"},
                "capabilities": ["reasoning", "writing_zh", "review"],
                "automation": {"status": "verified"},
            },
            {
                "id": "gpt-b",
                "name": "GPT B",
                "transport": "codex_cli",
                "execution_model": "gpt-6-astra",
                "model": {"value": "gpt-6-astra"},
                "capabilities": ["reasoning", "writing_zh", "review"],
                "automation": {"status": "verified"},
            },
        ]
        self.store.init({"agents": same_agents})
        with self.assertRaises(ValueError):
            create_flow(
                self.store,
                {"goal": "测试跨模型约束", "commander": "gpt-a", "participants": ["gpt-a", "gpt-b"], "require_cross_model": True},
            )

    def test_worker_bridge_auto_dispatch_live_tracking_and_auto_collect(self):
        wid = save_agent(
            self.store,
            {"id": "wb-kimi", "name": "WorkBuddy Worker", "transport": "worker_bridge", "model": "Kimi-K3", "allow_files": True},
        )
        register_heartbeat(self.store, wid, "worker-01", model="Kimi-K3")
        job = self.store.add_plan(
            {
                "title": "自动 Worker 派单回收测试",
                "tasks": [
                    {
                        "id": "w1",
                        "agent": wid,
                        "prompt": "创建 worker.md 并回复 ok",
                        "allowed_tools": ["files_read", "files_write"],
                        "validation": {"text_contains": ["WORKER_OK"], "artifacts": [{"path": "worker.md", "min_bytes": 5}]},
                    }
                ],
            }
        )

        def background_worker():
            for _ in range(50):
                tasks = poll_tasks(self.store, wid, worker_id="worker-01", model="Kimi-K3")
                if tasks:
                    t = tasks[0]
                    claim_task(self.store, t["job"], t["task"], t["attempt"], wid, "worker-01")
                    report_progress(self.store, t["job"], t["task"], t["attempt"], "worker-01", "worker:generating", "正在生成 worker.md", model="Kimi-K3")
                    (Path(t["workspace"]) / "worker.md").write_text("hello from worker", encoding="utf-8")
                    complete_task(
                        self.store,
                        t["job"],
                        t["task"],
                        t["attempt"],
                        "worker-01",
                        output="WORKER_OK 已完成",
                        artifacts=["worker.md"],
                        model="Kimi-K3",
                    )
                    return
                time.sleep(0.05)

        th = threading.Thread(target=background_worker, daemon=True)
        th.start()
        outcome = run_job(self.store, job)
        th.join(timeout=5)
        self.assertEqual(outcome["status"], "done")
        row = self.store.task_rows(job)[0]
        res = json.loads(row["result"])
        self.assertEqual(res["model"]["value"], "Kimi-K3")
        self.assertEqual(res["verification"]["artifacts"][0]["path"], "worker.md")

    def test_file_bridge_auto_collect_result_json(self):
        aid = save_agent(self.store, {"id": "manual-app", "name": "桌面客户端", "transport": "file_bridge"})
        job = self.store.add_plan(
            {
                "title": "自动监听 result.json 测试",
                "tasks": [
                    {"id": "step1", "agent": aid, "prompt": "输出 AUTO_DONE", "validation": {"text_contains": ["AUTO_DONE"]}},
                ],
            }
        )
        self.assertEqual(run_job(self.store, job)["status"], "waiting_external")
        attempt_dir = self.store.root / "jobs" / job / "step1" / "attempt-1"
        (attempt_dir / "result.json").write_text(json.dumps({"output": "AUTO_DONE 成果已完成", "artifacts": []}), encoding="utf-8")
        collected = collect_ready_results(self.store)
        self.assertEqual(collected, [job])
        self.assertEqual(self.store.task_rows(job)[0]["status"], "done")
        self.assertTrue((attempt_dir / "result.collected.json").is_file())

    def test_extended_mcp_server_preserves_default_6_and_exposes_12(self):
        default_srv = MCPServer(self.store)
        self.assertEqual(len(default_srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]), 6)
        ext_srv = MCPServer(self.store, extended=True)
        tools = ext_srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(len(tools), 12)
        names = {t["name"] for t in tools}
        self.assertTrue({"hub_create_workflow", "hub_poll_tasks", "hub_claim_task", "hub_report_progress", "hub_complete_task"} <= names)


class GeminiAndBrokerHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "workspace")
        self.requests = []
        outer = self

        class MockAPI(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers)))
                if self.path.endswith("/v1beta/models"):
                    payload = {"models": [{"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]}]}
                else:
                    payload = {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-chat"}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, dict(self.headers), body))
                payload = {
                    "modelVersion": "gemini-2.5-pro",
                    "candidates": [{"content": {"parts": [{"text": "HUB_OK gemini-secret-xyz"}]}, "finishReason": "STOP"}],
                }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def test_gemini_protocol_infer_and_sync_models(self):
        self.assertEqual(endpoint("https://generativelanguage.googleapis.com", "gemini"), "https://generativelanguage.googleapis.com/v1beta")
        aid = save_agent(
            self.store,
            {
                "name": "Gemini Pro",
                "transport": "http_api",
                "protocol": "gemini",
                "endpoint": self.base + "/v1beta",
                "model": "gemini-2.5-pro",
                "key": "gemini-secret-xyz",
                "auto_route": True,
            },
        )
        agent = next(a for a in self.store.agents() if a["id"] == aid)
        res = infer(agent, "gemini-secret-xyz", "Reply HUB_OK")
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["model"]["value"], "gemini-2.5-pro")
        self.assertIn("[REDACTED]", res["output"])
        self.assertNotIn("gemini-secret-xyz", res["output"])
        self.assertEqual(self.requests[-1][2].get("X-Goog-Api-Key"), "gemini-secret-xyz")

        synced = sync_api_models(self.store, aid)
        self.assertEqual(synced["models"], ["gemini-2.5-pro"])
        loaded = list_models({"protocol": "openai", "endpoint": self.base + "/v1/chat/completions"}, "k")
        self.assertEqual(loaded["models"], ["deepseek-chat", "deepseek-flash"])

    def test_dashboard_worker_http_api_auth_and_flow(self):
        root = Path(self.tmp.name) / "hub-http"
        store = Store(root)
        wid = save_agent(store, {"id": "wb-http", "name": "HTTP Worker", "transport": "worker_bridge", "model": "Kimi-K3"})
        wtoken = ensure_worker_token(store)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "agent_hub", "--root", str(root), "dashboard", "--port", str(port), "--no-open"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = "http://127.0.0.1:%d" % port
        try:
            for _ in range(60):
                try:
                    with urllib.request.urlopen(base + "/api/health", timeout=1) as r:
                        health = json.load(r)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("Dashboard v2 did not start")
            from agent_hub import __version__

            self.assertEqual(health["version"], __version__)
            key = (root / ".console-auth" / ("%d.key" % port)).read_text()
            req = urllib.request.Request(base + "/api/presets", headers={"X-Agent-Hub-Token": key})
            with urllib.request.urlopen(req, timeout=2) as r:
                presets = json.load(r)
            self.assertTrue(any(p["protocol"] == "gemini" for p in presets))

            def wpost(path, body, tok=wtoken):
                req = urllib.request.Request(
                    base + path,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json", "X-Agent-Hub-Worker-Token": tok},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3) as r:
                    return json.load(r)

            with self.assertRaises(urllib.error.HTTPError) as err:
                wpost("/api/worker/register", {"agent": wid}, tok="bad-token")
            self.assertEqual(err.exception.code, 403)
            reg = wpost("/api/worker/register", {"agent": wid, "worker_id": "w-http-1", "model": "Kimi-K3"})
            self.assertEqual(reg["agent"], wid)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
