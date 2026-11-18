"""Behavioral acceptance tests; model calls are replaced by deterministic receipts."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_hub.core import Store, retry_task, run_job, select_agent, validate
from agent_hub.mcp import MCPServer


def agent(aid, capabilities=("general",), ready=True, model="configured-hint"):
    return {
        "id": aid,
        "capabilities": list(capabilities),
        "automation": {"status": "verified" if ready else "unavailable"},
        "model": {"value": model, "source": "configured_unverified"},
    }


def task(tid, **extra):
    return {
        "id": tid,
        "prompt": "Produce a verifiable result for " + tid,
        "kind": "general",
        "required_capabilities": ["general"],
        "validation": {"text_contains": ["verified"]},
        **extra,
    }


def receipt(output="verified", model="runtime-model"):
    return {
        "status": "completed",
        "output": output,
        "model": {"value": model, "source": "runtime_event", "confidence": "observed"},
        "elapsed_seconds": 1,
    }


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "hub")
        self.store.init({"agents": [agent("writer"), agent("reviewer")]})

    def states(self, job):
        return {row["id"]: row["status"] for row in self.store.task_rows(job)}

    def test_dag_handoff_requires_independent_review_and_verified_files(self):
        """Out-of-order plans still hand off only verified, copied producer files."""
        job = self.store.add_plan({"tasks": [
            task("review", dependencies=["write"], review_of="write",
                 validation={"review_accept": True}),
            task("write", agent="writer", allowed_tools=["files_write"],
                 validation={"artifacts": [{"path": "report.txt", "min_bytes": 8}]}),
        ]})
        calls = []

        def run(selected, spec, workspace, attempt_dir, stopped):
            calls.append((spec["id"], selected["id"]))
            if spec["id"] == "write":
                (workspace / "report.txt").write_text("verified report", encoding="utf-8")
                return receipt("verified report", model="same-runtime-model")
            self.assertEqual((workspace / "inputs/write/report.txt").read_text(), "verified report")
            self.assertIn("verified report", spec["context"])
            return receipt(json.dumps({"decision": "accept", "reason": "Artifact checked"}),
                           model="same-runtime-model")

        with patch("agent_hub.core.run_adapter", side_effect=run):
            outcome = run_job(self.store, job)
        self.assertEqual(calls, [("write", "writer"), ("review", "reviewer")])
        self.assertEqual(outcome["status"], "done")
        rows = {row["id"]: row for row in self.store.task_rows(job)}
        review = json.loads(rows["review"]["result"])
        self.assertTrue(review["review_independence"]["different_agent"])
        self.assertFalse(review["review_independence"]["different_model"])
        artifact = json.loads(rows["write"]["result"])["verification"]["artifacts"][0]
        self.assertEqual(artifact["path"], "report.txt")
        self.assertEqual(len(artifact["sha256"]), 64)
        self.assertTrue(Path(outcome["summary"]).is_file())

    def test_invalid_dependency_graphs_are_rejected_before_persistence(self):
        cases = [
            [task("a", dependencies=["b"]), task("b", dependencies=["a"])],
            [task("a", dependencies=["missing"])],
            [task("a"), task("b", review_of="a")],
            [task("a"), task("a")],
        ]
        for tasks in cases:
            with self.subTest(tasks=tasks):
                with self.assertRaises(ValueError):
                    self.store.add_plan({"tasks": tasks})
        self.assertEqual(self.store.snapshot()["jobs"], [])

    def test_inactive_malformed_validation_is_refused_and_file_contents_are_checked(self):
        invalid_specs = [
            {}, {"placeholder": True}, {"review_accept": False}, {"review_accept": 1},
            {"json_equals": {}}, {"json_equals": []},
            {"text_contains": []}, {"text_contains": "verified"}, {"text_contains": [""]},
            {"artifacts": []}, {"artifacts": ["report.txt"]},
            {"artifacts": [{"path": "report.txt", "min_bytes": 0}]},
            {"artifacts": [{"path": "report.txt", "min_bytes": True}]},
            {"artifacts": [{"path": "report.txt", "json_equals": {}}]},
        ]
        invalid_tasks = [task("invalid", validation=spec) for spec in invalid_specs]
        invalid_tasks += [task("invalid", dependencies="parent"),
                          task("invalid", allowed_tools={"files_read": True}),
                          task("invalid", required_capabilities="general")]
        for spec in invalid_tasks:
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    self.store.add_plan({"tasks": [spec]})
        self.assertEqual(self.store.snapshot()["jobs"], [])
        workspace = self.root / "content-check"
        workspace.mkdir()
        (workspace / "result.json").write_text('{"passed": false}', encoding="utf-8")
        checked = validate(task("artifact", validation={"artifacts": [
            {"path": "result.json", "json_equals": {"passed": True}}
        ]}), receipt(), workspace)
        self.assertFalse(checked["passed"])
        self.assertEqual(checked["artifacts"], [])
        self.assertTrue(any("JSON" in error for error in checked["errors"]))

    def test_routing_uses_observed_results_and_capability_availability_constraints(self):
        """A model-name hint must not make real capability tests disappear."""
        self.store.init({"agents": [agent("a_failed"), agent("z_passed"),
                                     agent("offline", ready=False)]})

        def run(selected, spec, workspace, attempt_dir, stopped):
            return receipt("wrong" if selected["id"] == "a_failed" else "verified",
                           model="actual-" + selected["id"])

        with patch("agent_hub.core.run_adapter", side_effect=run):
            for aid in ("a_failed", "z_passed"):
                job = self.store.add_plan({"tasks": [task("probe", agent=aid)]})
                run_job(self.store, job)
        selected, route = select_agent(self.store, task("next"), {})
        self.assertEqual(selected["id"], "z_passed")
        self.assertGreaterEqual(route["samples"], 1)
        unavailable, _ = select_agent(self.store, task("other", required_capabilities=["desktop"]), {})
        self.assertIsNone(unavailable)

    def test_artifact_validation_rejects_traversal_absolute_and_symlink_paths(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("verified secret")
        (workspace / "link.txt").symlink_to(outside)
        (workspace / "directory").symlink_to(self.root, target_is_directory=True)
        for path in ("../outside.txt", str(outside), "link.txt", "directory/outside.txt"):
            with self.subTest(path=path):
                result = validate(task("check"), {**receipt(), "artifacts": [path]}, workspace)
                self.assertFalse(result["passed"])
                self.assertEqual(result["artifacts"], [])
        with self.assertRaises(ValueError):
            self.store.add_plan({"tasks": [task("bad", validation={"artifacts": [{"path": "../escape"}]})]})

    def test_pause_resume_and_second_runner_do_not_duplicate_execution(self):
        job = self.store.add_plan({"tasks": [task("once")]})
        self.store.control(job, True)
        with patch("agent_hub.core.run_adapter", return_value=receipt()) as runner:
            self.assertEqual(run_job(self.store, job)["status"], "paused")
            runner.assert_not_called()
            self.store.control(job, False)
            self.assertEqual(run_job(self.store, job)["status"], "done")
            self.assertEqual(run_job(self.store, job)["status"], "done")
            self.assertEqual(runner.call_count, 1)
            with (self.store.root / "runner.lock").open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(RuntimeError):
                    run_job(self.store, job)
            self.assertEqual(runner.call_count, 1)

    def test_interrupted_attempt_requires_explicit_retry(self):
        job = self.store.add_plan({"tasks": [task("uncertain")]})
        workspace = self.store.root / "jobs" / job / "uncertain/attempt-1/workspace"
        workspace.mkdir(parents=True)
        with self.store.connect() as con:
            con.execute("UPDATE tasks SET status='running',agent='writer',attempt=1 WHERE job=?", (job,))
        with patch("agent_hub.core.run_adapter", return_value=receipt()) as runner:
            self.assertEqual(run_job(self.store, job)["status"], "needs_attention")
            runner.assert_not_called()
            self.assertEqual(self.states(job)["uncertain"], "failed")
            retry_task(self.store, job, "uncertain")
            self.assertEqual(run_job(self.store, job)["status"], "done")
            runner.assert_called_once()
        self.assertEqual(self.store.task_rows(job)[0]["attempt"], 2)

    def test_external_import_cannot_bypass_validation_or_dispatch_dependencies_early(self):
        self.store.init({"agents": [agent("manual", ready=False)]})
        job = self.store.add_plan({"tasks": [task("external", agent="manual"),
                                            task("dependent", dependencies=["external"])]})
        with self.assertRaises(ValueError):
            self.store.import_result(job, "external", receipt())
        with patch("agent_hub.core.run_adapter", return_value={"status": "waiting_external", "output": "handoff ready"}) as runner:
            self.assertEqual(run_job(self.store, job)["status"], "waiting_external")
            runner.assert_called_once()
        self.assertEqual(self.states(job), {"external": "waiting_external", "dependent": "pending"})
        imported = self.store.import_result(job, "external", receipt("unsubstantiated success"))
        self.assertFalse(imported["verification"]["passed"])
        self.assertEqual(self.states(job), {"external": "failed", "dependent": "pending"})
        self.assertEqual(imported["model"]["source"], "external_unverified")
        with self.assertRaises(ValueError):
            self.store.import_result(job, "external", receipt())

    def test_mcp_stdio_protocol_and_tool_failures_preserve_server(self):
        """Exercise the real wire entrypoint without launching any client/model."""
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_submit_plan", "arguments": {"plan": {"id": "mcp-job", "tasks": [task("wire")]}}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "hub_pause", "arguments": {"job": "mcp-job"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "hub_import_result", "arguments": {"job": "mcp-job", "task": "wire", "result": receipt()}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": {"malformed": True}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "hub_status"}},
        ]
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        completed = subprocess.run(
            [sys.executable, "-m", "agent_hub.mcp", "--root", str(self.root / "mcp")],
            input="\n".join(json.dumps(message) for message in messages) + "\n",
            capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = {message["id"]: message for message in map(json.loads, completed.stdout.splitlines())}
        self.assertEqual(set(responses), {1, 2, 3, 4, 5, 6, 7})
        self.assertEqual(responses[1]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(len(responses[2]["result"]["tools"]), 6)
        self.assertTrue(responses[5]["result"]["isError"])
        self.assertEqual(responses[6]["error"]["code"], -32602)
        status = json.loads(responses[7]["result"]["content"][0]["text"])
        self.assertEqual(status["jobs"][0]["status"], "paused")

    def test_mcp_background_worker_keeps_manual_client_waiting_for_real_results(self):
        """Launch the real scheduler process using only the file bridge transport."""
        self.store.init({"agents": [agent("manual", ready=False)]})
        job = self.store.add_plan({"tasks": [task("handoff", agent="manual")]})
        server = MCPServer(self.store)
        server.call("hub_pause", {"job": job})
        self.assertEqual(server.call("hub_run", {"job": job})["status"], "paused")
        started = server.call("hub_resume", {"job": job})
        self.assertEqual(started["status"], "started")
        child = server.children[job]
        try:
            self.assertEqual(child.wait(timeout=10), 0, Path(started["log"]).read_text())
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        row = self.store.task_rows(job)[0]
        self.assertEqual(row["status"], "waiting_external")
        self.assertEqual(row["attempt"], 1)
        evidence = json.loads(row["result"])
        self.assertIsNone(evidence["model"]["value"])
        self.assertEqual(evidence["transport"], "file_bridge")
        self.assertTrue(Path(evidence["handoff"]).is_file())

    def test_background_jobs_queue_behind_running_scheduler(self):
        self.store.init({"agents": [agent("manual", ready=False)]})
        jobs = [self.store.add_plan({"tasks": [task("queued", agent="manual")]}) for _ in range(2)]
        server = MCPServer(self.store)
        children = []
        try:
            with (self.store.root / "runner.lock").open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for job in jobs:
                    self.assertEqual(server.call("hub_run", {"job": job})["status"], "started")
                    children.append(server.children[job])
                time.sleep(0.25)
                self.assertTrue(all(child.poll() is None for child in children), "queued workers must wait for the lock")
                self.assertTrue(all(self.states(job)["queued"] == "pending" for job in jobs))
            for child in children:
                self.assertEqual(child.wait(timeout=10), 0)
            for job in jobs:
                self.assertEqual(self.states(job)["queued"], "waiting_external")
                self.assertEqual(self.store.task_rows(job)[0]["attempt"], 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
