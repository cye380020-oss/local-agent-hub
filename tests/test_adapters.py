"""Transport checks use harmless local fixtures; they are not AI evaluations."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_hub.adapters import _environment, _extract_events, run_adapter, sanitize


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.attempt = self.root / "attempt"
        self.task = {"id": "task-one", "prompt": "输出测试结果", "kind": "test_fixture",
                     "allowed_tools": ["files_read"], "required_capabilities": ["reasoning"]}

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, body, login="Logged in using ChatGPT"):
        path = self.root / "codex-fixture"
        # The fixture advertises the same options solely to test transport safety.
        path.write_text("#!" + sys.executable + "\n" +
                        "import os, sys, json, time, subprocess\n" +
                        "if '--help' in sys.argv:\n" +
                        "    print('--sandbox --ask-for-approval --no-daemon --ignore-user-config --ephemeral --json --skip-git-repo-check --ignore-rules')\n" +
                        "    sys.exit(0)\n" +
                        "if sys.argv[1:] == ['login','status']:\n" +
                        "    print(" + repr(login) + ")\n    sys.exit(0)\n" + body + "\n", encoding="utf-8")
        path.chmod(0o700)
        return {"id": "codex-fixture", "kind": "test_fixture", "transport": "codex_cli",
                "executable": str(path), "timeout_seconds": 5}

    def run_fixture(self, agent, task=None, should_pause=lambda: False):
        return run_adapter(agent, task or self.task, self.workspace, self.attempt, should_pause)

    def test_file_bridge_waits_and_contains_contract(self):
        result = run_adapter({"id": "doubao", "transport": "file_bridge"}, self.task,
                             self.workspace, self.attempt, lambda: False)
        self.assertEqual(result["status"], "waiting_external")
        self.assertEqual(result["artifacts"], [])
        handoff = json.loads((self.attempt / "handoff.json").read_text())
        self.assertEqual(handoff["task"]["id"], "task-one")
        self.assertIsNone(handoff["model"]["value"])
        self.assertIn("尚未得到", (self.attempt / "INSTRUCTIONS.md").read_text())
        template = json.loads((self.attempt / "result-template.json").read_text())
        self.assertEqual(template, {"output": "", "artifacts": [], "model": None})

    def test_deepseek_candidate_never_runs_unverified_cli(self):
        result = run_adapter({"id": "deepseek", "transport": "dsh_cli", "executable": "/does/not/exist"},
                             self.task, self.workspace, self.attempt, lambda: False)
        self.assertEqual(result["status"], "waiting_external")
        self.assertIn("headless", result["reason"])

    def test_safe_argv_model_hint_and_sanitized_environment(self):
        agent = self.fixture("prompt = sys.stdin.read()\n"
                             "open('observed.json', 'w').write(json.dumps({'argv':sys.argv, 'keys':list(os.environ), 'prompt':prompt}))\n"
                             "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{\"ok\":true}'}}))")
        agent["model"] = {"value": "configuration-hint", "source": "settings", "confidence": "configured"}
        task = dict(self.task, allowed_tools=["files_read", "files_write"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-one", "CODEX_API_KEY": "secret-two", "OTHER_TOKEN": "secret-three"}):
            result = self.run_fixture(agent, task)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["is_test_fixture"])
        self.assertIsNone(result["model"]["value"])
        self.assertEqual(result["configured_model"]["value"], "configuration-hint")
        self.assertEqual(json.loads(result["output"]), {"ok": True})
        observed = json.loads((self.workspace / "observed.json").read_text())
        self.assertNotIn("OPENAI_API_KEY", observed["keys"])
        self.assertNotIn("CODEX_API_KEY", observed["keys"])
        self.assertNotIn("OTHER_TOKEN", observed["keys"])
        argv = observed["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "never")
        self.assertNotIn("--model", argv)
        for flag in ["--ignore-user-config", "--ephemeral", "--json", "--skip-git-repo-check", "--no-daemon"]:
            self.assertIn(flag, argv)
        self.assertNotIn("danger-full-access", argv)
        self.assertEqual(result["artifacts"], ["observed.json"])
        self.assertIn("不读取密钥", observed["prompt"])

    def test_read_only_default_and_explicit_model_override(self):
        agent = self.fixture("open('args.json', 'w').write(json.dumps(sys.argv))\n"
                             "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))")
        agent["execution_model"] = "explicit-model"
        result = self.run_fixture(agent)
        argv = json.loads((self.workspace / "args.json").read_text())
        self.assertEqual(result["sandbox"], "read-only")
        self.assertEqual(argv[argv.index("--model") + 1], "explicit-model")
        self.assertIsNone(result["model"]["value"])
        # The fake process is unsandboxed test machinery; it does not prove CLI sandbox behavior.

    def test_model_requires_runtime_metadata_not_assistant_claim(self):
        events = "\n".join(json.dumps(event) for event in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "model: invented-model"}},
            {"type": "item.completed", "model": "ignored-field", "item": {"type": "agent_message", "text": "final"}},
        ])
        output, model, failure = _extract_events(events, "")
        self.assertEqual(output, "final")
        self.assertIsNone(model["value"])
        self.assertIsNone(failure)
        _, model, _ = _extract_events(json.dumps({"type": "session.created", "session": {"model": "runtime-model"}}), "")
        self.assertEqual(model["value"], "runtime-model")
        self.assertEqual(model["confidence"], "reported")
        _, model, _ = _extract_events("", "model: reported-header\n")
        self.assertEqual(model["value"], "reported-header")

    def test_no_final_output_is_failure(self):
        agent = self.fixture("print(json.dumps({'type':'turn.completed','usage':{}}))")
        result = self.run_fixture(agent)
        self.assertEqual(result["status"], "failed")
        self.assertIn("最终文本", result["error"])

    def test_changed_artifacts_only_and_no_symlink_export(self):
        self.workspace.mkdir()
        (self.workspace / "input.txt").write_text("preserved")
        agent = self.fixture("open('new.txt','w').write('artifact')\n"
                             "os.symlink('/etc/hosts','link.txt')\n"
                             "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))")
        result = self.run_fixture(agent, dict(self.task, allowed_tools=["files_write"]))
        self.assertEqual(result["artifacts"], ["new.txt"])

    def test_pause_terminates_real_fixture_and_child(self):
        agent = self.fixture("child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                             "open('child.pid','w').write(str(child.pid))\n"
                             "sys.stdin.read()\n"
                             "time.sleep(60)")
        paused = threading.Event()
        def pause_after_child_starts():
            until = time.monotonic() + 3
            while time.monotonic() < until:
                if (self.workspace / "child.pid").exists():
                    paused.set()
                    return
                time.sleep(0.02)
            paused.set()
        thread = threading.Thread(target=pause_after_child_starts)
        thread.start()
        started = time.monotonic()
        result = self.run_fixture(agent, should_pause=paused.is_set)
        thread.join()
        self.assertEqual(result["status"], "paused", result)
        self.assertLess(time.monotonic() - started, 4)
        child_pid = int((self.workspace / "child.pid").read_text())
        # SIGKILL kills grandchildren even if the parent exits on SIGTERM first.
        for _ in range(50):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("fixture child process survived cancellation")

    def test_timeout_terminates_fixture(self):
        agent = self.fixture("sys.stdin.read()\ntime.sleep(60)")
        agent["timeout_seconds"] = 1
        started = time.monotonic()
        result = self.run_fixture(agent)
        self.assertEqual(result["status"], "failed")
        self.assertIn("超时", result["error"])
        self.assertLess(time.monotonic() - started, 4)

    def test_task_timeout_overrides_agent_default(self):
        agent = self.fixture("sys.stdin.read()\ntime.sleep(60)")
        agent["timeout_seconds"] = 60
        result = self.run_fixture(agent, dict(self.task, timeout_seconds=1))
        self.assertEqual(result["status"], "failed")
        self.assertIn("超时", result["error"])
        invocation = json.loads((self.attempt / "invocation.json").read_text())
        self.assertEqual(invocation["timeout_seconds"], 1)

    def test_api_authenticated_cli_refused_without_forced_logout(self):
        agent = self.fixture("open('should-not-exist','w').write('ran')", login="Logged in using an API key")
        result = self.run_fixture(agent)
        self.assertEqual(result["status"], "failed")
        self.assertIn("未更改现有登录", result["error"])
        self.assertFalse((self.workspace / "should-not-exist").exists())

    def test_sigterm_handler_restored_after_call(self):
        agent = self.fixture("print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))")
        previous = signal.getsignal(signal.SIGTERM)
        result = self.run_fixture(agent)
        self.assertEqual(result["status"], "completed")
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        state = json.loads((self.attempt / "process.json").read_text())
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["returncode"], 0)

    def assert_worker_signal_cleanup(self, termination_signal):
        agent = self.fixture("child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                             "open('child.pid','w').write(str(child.pid))\n"
                             "sys.stdin.read()\ntime.sleep(60)")
        script = ("import json,sys;from pathlib import Path;from agent_hub.adapters import run_adapter;"
                  "run_adapter(json.loads(sys.argv[1]),json.loads(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),lambda:False)")
        worker = subprocess.Popen([sys.executable, "-c", script, json.dumps(agent), json.dumps(self.task),
                                   str(self.workspace), str(self.attempt)],
                                  cwd=str(Path(__file__).resolve().parents[1]),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        child_pid = None
        try:
            until = time.monotonic() + 5
            while not (self.workspace / "child.pid").exists():
                if time.monotonic() >= until or worker.poll() is not None:
                    self.fail("fixture child did not start: " + worker.stderr.read().decode())
                time.sleep(0.02)
            child_pid = int((self.workspace / "child.pid").read_text())
            os.kill(worker.pid, termination_signal)
            worker.wait(timeout=4)
            self.assertNotEqual(worker.returncode, 0)
            state = json.loads((self.attempt / "process.json").read_text())
            self.assertEqual(state["state"], "interrupted")
            for _ in range(50):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("child survived worker signal cleanup")
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=2)
            worker.stderr.close()
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_worker_sigint_cleans_child_group(self):
        self.assert_worker_signal_cleanup(signal.SIGINT)

    def test_worker_sigterm_cleans_child_group(self):
        self.assert_worker_signal_cleanup(signal.SIGTERM)

    def test_output_limit_and_sanitized_logs(self):
        agent = self.fixture("print('api_key=fixture-secret-value',file=sys.stderr,flush=True)\n"
                             "sys.stdout.write('x'*100000)\nsys.stdout.flush()\ntime.sleep(60)")
        agent["max_output_bytes"] = 4096
        result = self.run_fixture(agent)
        self.assertEqual(result["status"], "failed")
        self.assertIn("输出超过", result["error"])
        self.assertLessEqual((self.attempt / "stdout.jsonl").stat().st_size, 4096)
        self.assertNotIn("fixture-secret-value", (self.attempt / "stderr.log").read_text())

    def test_pre_paused_transport_does_not_execute(self):
        agent = self.fixture("open('should-not-exist','w').write('ran')")
        result = self.run_fixture(agent, should_pause=lambda: True)
        self.assertEqual(result["status"], "paused")
        self.assertFalse((self.workspace / "should-not-exist").exists())

    def test_secret_redaction_and_env_allowlist(self):
        raw = 'Bearer abcDEF123\nOPENAI_API_KEY=fixture-secret\n{"access_token":"token-value"}\nsk-abcdefghijklmnopqrstuv'
        redacted = sanitize(raw)
        for secret in ["abcDEF123", "fixture-secret", "token-value", "sk-abcdefghijklmnopqrstuv"]:
            self.assertNotIn(secret, redacted)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "a", "CODEX_API_KEY": "b", "SOME_SECRET": "c"}):
            environment = _environment()
            self.assertFalse({"OPENAI_API_KEY", "CODEX_API_KEY", "SOME_SECRET"} & set(environment))


if __name__ == "__main__":
    unittest.main()
