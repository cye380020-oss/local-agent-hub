import base64
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from agent_hub import __version__
from agent_hub.attachments import approved_inputs, attach_job_inputs, extract_text_preview, review_inputs, save_upload
from agent_hub.core import Store, prepare_inputs, run_job
from agent_hub.dashboard import serve
from agent_hub.desktop_ui import run_desktop_ui
from agent_hub.management import save_agent
from agent_hub.progress import ProgressTracker
from agent_hub.workflow import (
    approve_draft,
    approve_plan,
    create_flow,
    inject_workflow_note,
    read_flow,
    request_revision,
    revise_plan,
)


def _make_docx(path: Path, paragraphs):
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


def _make_xlsx(path: Path):
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>指标</t></si><si><t>数值</t></si><si><t>延迟ms</t></si>"
        "</sst>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>42</v></c></row>'
        "</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


def _make_pptx(path: Path, slides):
    with zipfile.ZipFile(path, "w") as zf:
        for i, text in enumerate(slides, 1):
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>"
                "</p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)


def _agent(aid, model="model-a", transport="codex_cli"):
    return {
        "id": aid,
        "name": aid,
        "transport": transport,
        "execution_model": model,
        "model": {"value": model},
        "capabilities": ["general", "reasoning", "writing_zh", "coding", "review", "files_read", "files_write"],
        "automation": {"status": "verified"},
    }


class AttachmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "ws")
        self.store.init({"agents": [_agent("gpt"), _agent("worker", "model-b")]})

    def test_office_extraction_and_upload(self):
        docx = self.root / "spec.docx"
        xlsx = self.root / "metrics.xlsx"
        pptx = self.root / "deck.pptx"
        _make_docx(docx, ["本地工作台升级规格", "支持人机协同审改"])
        _make_xlsx(xlsx)
        _make_pptx(pptx, ["第一页：总体架构", "第二页：风险护栏"])

        self.assertIn("本地工作台升级规格", extract_text_preview(docx))
        self.assertIn("延迟ms\t42", extract_text_preview(xlsx))
        self.assertIn("第一页：总体架构", extract_text_preview(pptx))

        meta = save_upload(self.store, "spec.docx", docx.read_bytes())
        self.assertTrue(meta["id"].startswith("up-"))
        self.assertTrue(meta["has_text_preview"])
        self.assertIn("人机协同", meta["preview_excerpt"])

    def test_legacy_attachment_and_cli_confirmation(self):
        from agent_hub.attachments import review_saved_inputs
        from agent_hub.__main__ import confirm_attachments

        src = self.root / "public.md"
        src.write_text("reviewed")
        with patch("builtins.input", return_value="1"):
            review = confirm_attachments(self.store, [str(src)], ["gpt"])
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(ValueError):
                confirm_attachments(self.store, [str(src)], ["gpt"])
        data = {"local_paths":[str(src)], "attachment_approval":review}
        job = self.store.add_plan({"tasks":[{"id":"one","agent":"gpt","prompt":"p","validation":{"min_chars":1}}]})
        items = attach_job_inputs(self.store, job, approved_inputs(self.store, data, ["gpt"]))
        del items[0]["recipients"]
        manifest = self.store.root / "jobs" / job / "attachments" / "manifest.json"
        manifest.write_text(json.dumps(items))
        agent = next(a for a in self.store.agents() if a["id"] == "gpt")
        ws = self.root / "legacy-step"
        spec = {"id":"one"}
        with self.assertRaises(ValueError):
            prepare_inputs(self.store, job, spec, {}, ws, agent)
        draft = review_saved_inputs(self.store, job)
        with self.assertRaises(ValueError):
            review_saved_inputs(self.store, job, dict(draft, files={}))
        review_saved_inputs(self.store, job, draft)
        prepare_inputs(self.store, job, spec, {}, ws, agent)
        self.assertIn("reviewed", spec["context"])

    def test_local_mount_security_and_prepare_inputs(self):
        src_dir = self.root / "sample_repo"
        (src_dir / ".git").mkdir(parents=True)
        (src_dir / ".git" / "config").write_text("git-secret", encoding="utf-8")
        (src_dir / "node_modules").mkdir()
        (src_dir / "node_modules" / "pkg.js").write_text("ignored", encoding="utf-8")
        (src_dir / ".env").write_text("API_KEY=should_never_mount", encoding="utf-8")
        (src_dir / "notes.md").write_text("可挂载的公开需求文档", encoding="utf-8")
        link = src_dir / "escape.txt"
        link.symlink_to(src_dir / "notes.md")

        with self.assertRaises(ValueError):
            review_inputs(self.store, [], [str(src_dir / ".env")], ["gpt"])
        with self.assertRaises(ValueError):
            review_inputs(self.store, [], [str(link)], ["gpt"])

        job = self.store.add_plan({"title": "att", "tasks": [{"id": "t1", "prompt": "hi", "validation": {"min_chars": 1}}]})
        (src_dir / "credentials.json").write_text('{"service":"private"}')
        (src_dir / "config.yml").write_text('api_key: example_value_for_filter')
        (src_dir / "extra.txt").write_text('exclude this from the task')
        review, _ = review_inputs(self.store, [], [str(src_dir)], ["gpt"])
        self.assertEqual(set(review["files"]), {"mounted/sample_repo/notes.md", "mounted/sample_repo/extra.txt"})
        del review["files"]["mounted/sample_repo/extra.txt"]
        data = {"local_paths": [str(src_dir)], "attachment_approval": review}
        with self.assertRaises(ValueError):
            approved_inputs(self.store, {"local_paths": [str(src_dir)]}, ["gpt"])
        with self.assertRaises(ValueError):
            approved_inputs(self.store, data, ["worker"])
        attached = attach_job_inputs(self.store, job, approved_inputs(self.store, data, ["gpt"]))
        (src_dir / "notes.md").write_text("changed after approval")
        with self.assertRaises(ValueError):
            approved_inputs(self.store, data, ["gpt"])
        rels = [a["rel_path"] for a in attached]
        self.assertEqual(rels, ["mounted/sample_repo/notes.md"])

        ws = self.root / "run-ws"
        ws.mkdir()
        task = {"id": "t1", "dependencies": []}
        agents = {a["id"]: a for a in self.store.agents()}
        for unapproved in [agents["worker"], dict(agents["gpt"], endpoint="https://changed.example")]:
            with self.assertRaises(ValueError):
                prepare_inputs(self.store, job, task, {}, ws, unapproved)
        self.assertFalse((ws / "inputs").exists())
        prepare_inputs(self.store, job, task, {}, ws, agents["gpt"])
        copied = ws / "inputs" / "attachments" / "mounted" / "sample_repo" / "notes.md"
        self.assertTrue(copied.is_file())
        contexts = json.loads(task["context"])
        att_ctx = next(c for c in contexts if c["task"] == "_job_attachments")
        self.assertIn("inputs/attachments/mounted/sample_repo/notes.md", att_ctx["attachment_previews"])
        self.assertIn("可挂载的公开需求文档", att_ctx["attachment_previews"]["inputs/attachments/mounted/sample_repo/notes.md"])


class InteractiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "ws")
        self.store.init({"agents": [_agent("gpt", "gpt-6"), _agent("worker1", "ds-1"), _agent("worker2", "gem-2")]})
        self.seen_prompts = {}
        self.reject_review = False
        self.plan_rev_count = 0

    def _adapter(self, a, task, workspace, directory, stopped):
        self.seen_prompts[task["id"]] = task["prompt"]
        role = task["workflow_role"]
        if role == "planning":
            self.plan_rev_count += 1
            out = json.dumps(
                {
                    "analysis": "Detailed multi-agent analysis round %d based on requirements." % self.plan_rev_count,
                    "steps": [
                        {
                            "id": "step1",
                            "agent": "worker1",
                            "kind": "reasoning",
                            "prompt": "Draft architecture section",
                            "reason": "Cross-model worker",
                            "dependencies": [],
                            "needs_files": False,
                            "artifacts": [],
                        },
                        {
                            "id": "step2",
                            "agent": "worker2",
                            "kind": "writing_zh",
                            "prompt": "Write risk checklist",
                            "reason": "Secondary worker",
                            "dependencies": ["step1"],
                            "needs_files": False,
                            "artifacts": [],
                        },
                    ],
                }
            )
        elif role == "review":
            if self.reject_review:
                out = json.dumps(
                    {
                        "decision": "revise",
                        "reason": "Needs additional rollout steps",
                        "issues": [{"step": "synthesis", "instruction": "Add rollout checklist"}],
                    }
                )
            else:
                out = json.dumps({"decision": "accept", "reason": "All criteria verified.", "issues": []})
        else:
            out = f"Verified output for {task['id']} by {a['id']}."
        return {
            "status": "completed",
            "output": out,
            "artifacts": [],
            "transport": "codex_cli",
            "model": {"value": a["execution_model"], "source": "fixture"},
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            "is_test_fixture": True,
            "elapsed_seconds": 0.02,
        }

    def test_confirm_plan_revise_edit_approve_and_inject_note(self):
        job = create_flow(
            self.store,
            {
                "goal": "Design interactive workflow",
                "requirements": "Include plan approval",
                "commander": "gpt",
                "confirm_plan": True,
                "max_steps": 3,
            },
        )
        with patch("agent_hub.core.run_adapter", side_effect=self._adapter):
            res = run_job(self.store, job)
            self.assertEqual(res["status"], "awaiting_approval")
            self.assertEqual(res["phase"], "awaiting_approval")

            # Reject plan with feedback -> commander replans and pauses again for approval.
            revise_plan(self.store, job, "把安全拆解单独加强")
            res2 = run_job(self.store, job)
            self.assertEqual(res2["status"], "awaiting_approval")
            self.assertIn("把安全拆解单独加强", self.seen_prompts["planning"])

            # Malicious edit trying to enable files when files_allowed=False must be rejected.
            with self.assertRaises(ValueError):
                approve_plan(self.store, job, edited_steps=[{"id": "step1", "agent": "worker2", "prompt": "x", "needs_files": True}])

            # Valid edit: reassign step1 to worker2 and add an approval note.
            approve_plan(
                self.store,
                job,
                edited_steps=[
                    {"id": "step1", "agent": "worker2", "prompt": "Custom edited architecture prompt"},
                    {"id": "step2", "agent": "worker1", "prompt": "Custom edited risk prompt"},
                ],
                user_note="批准附注：请附带迁移清单",
            )
            inject_workflow_note(self.store, job, note="中途补充：保留兼容旧接口")

            final = run_job(self.store, job)
            self.assertEqual(final["status"], "done")
            self.assertIn("Custom edited architecture prompt", self.seen_prompts["step1-r0"])
            self.assertIn("批准附注：请附带迁移清单", self.seen_prompts["step1-r0"])
            self.assertIn("中途补充：保留兼容旧接口", self.seen_prompts["synthesis-r0"])

            snap = self.store.snapshot()
            jobj = next(j for j in snap["jobs"] if j["id"] == job)
            self.assertGreaterEqual(jobj["token_usage"]["total_tokens"], 600)

    def test_approve_draft_and_human_request_revision(self):
        self.reject_review = True
        job = create_flow(
            self.store,
            {
                "goal": "Produce launch memo",
                "requirements": "Must be concise",
                "commander": "gpt",
                "max_revisions": 0,
                "max_steps": 2,
            },
        )
        with patch("agent_hub.core.run_adapter", side_effect=self._adapter):
            res = run_job(self.store, job)
            self.assertEqual(res["status"], "needs_attention")
            delivery = approve_draft(self.store, job, note="业务方确认可直接使用")
            self.assertTrue(delivery["human_approved"])
            self.assertEqual(delivery["human_note"], "业务方确认可直接使用")
            self.assertTrue((Path(delivery["directory"]) / "final.md").is_file())

            # Now request another human revision cycle targeting step1 (must auto-include dependent step2).
            self.reject_review = False
            request_revision(self.store, job, "补充回滚应急预案", steps=["step1"])
            _, st = read_flow(self.store, job)
            self.assertEqual(st["repair_steps"], ["step1", "step2"])
            res2 = run_job(self.store, job)
            self.assertEqual(res2["status"], "done")
            self.assertIn("补充回滚应急预案", self.seen_prompts["step1-r1"])
            self.assertIn("补充回滚应急预案", self.seen_prompts["synthesis-r1"])


class DesktopUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.attempt_dir = self.root / "jobs" / "job-1" / "t1" / "attempt-1"
        self.task_ws = self.attempt_dir / "workspace"
        self.task_ws.mkdir(parents=True)

    def _make_fake_driver(self, script_body: str, name: str = "FakeDesktopDriver") -> Path:
        driver = self.root / name
        driver.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
        driver.chmod(driver.stat().st_mode | stat.S_IXUSR)
        return driver

    def test_accessibility_denied_and_screen_locked_fail_honestly(self):
        denied = self._make_fake_driver('import json; print(json.dumps({"ok": True, "trusted": False, "screen_locked": False}))\n', "FakeDenied")
        locked = self._make_fake_driver('import json; print(json.dumps({"ok": True, "trusted": True, "screen_locked": True}))\n', "FakeLocked")
        agent = {"id": "dui", "name": "Gemini", "transport": "desktop_ui", "app_path": "/Applications/Gemini.app"}
        base = {"status": "failed", "output": "", "artifacts": [], "transport": "desktop_ui", "model": {"value": None}}

        with patch("agent_hub.desktop_ui.driver_binary", return_value=denied):
            res = run_desktop_ui(agent, {"id": "t1"}, self.task_ws, self.attempt_dir, lambda: False, "hi", dict(base), ProgressTracker(self.attempt_dir, "dui", "desktop_ui", "t1"))
            self.assertEqual(res["status"], "failed")
            self.assertIn("辅助功能权限", res["error"])

        with patch("agent_hub.desktop_ui.driver_binary", return_value=locked):
            res = run_desktop_ui(agent, {"id": "t1"}, self.task_ws, self.attempt_dir, lambda: False, "hi", dict(base), ProgressTracker(self.attempt_dir, "dui", "desktop_ui", "t1"))
            self.assertEqual(res["status"], "failed")
            self.assertIn("锁屏", res["error"])

    def test_marked_reply_capture_and_mutex_serialization(self):
        state_file = self.root / "driver_state.json"
        log_file = self.root / "driver_log.txt"
        script = f"""
import json, re, sys, time
from pathlib import Path
state_path = Path({str(state_file)!r})
log_path = Path({str(log_file)!r})
cmd = sys.argv[1]
if cmd == "check":
    print(json.dumps({{"ok": True, "trusted": True, "screen_locked": False}}))
elif cmd == "send":
    prompt = sys.stdin.read()
    m1 = re.search(r"<<<HUB_OUT_([A-F0-9]+)>>>", prompt)
    nonce = m1.group(1) if m1 else "NONE"
    with log_path.open("a") as f:
        f.write(f"send:{{nonce}}:{{time.monotonic():.4f}}\\n")
    state_path.write_text(json.dumps({{"nonce": nonce}}))
    print(json.dumps({{"ok": True}}))
elif cmd == "read":
    if state_path.is_file():
        nonce = json.loads(state_path.read_text())["nonce"]
        text = f"user prompt...\\n<<<HUB_OUT_{{nonce}}>>>\\nHUB_OK desktop verified reply\\n<<<HUB_END_{{nonce}}>>>"
    else:
        text = "empty chat"
    print(json.dumps({{"ok": True, "screen_locked": False, "text": text}}))
"""
        driver = self._make_fake_driver(script)
        fake_app = {"name": "FakeAI", "path": "/Applications/FakeAI.app", "bundle_id": "com.example.fakeai"}
        agent = {
            "id": "dui",
            "name": "FakeAI",
            "transport": "desktop_ui",
            "app_path": "/Applications/FakeAI.app",
            "ui_settle_seconds": 2.0,
        }
        base = {"status": "failed", "output": "", "artifacts": [], "transport": "desktop_ui", "model": {"value": None}}

        with patch("agent_hub.desktop_ui.driver_binary", return_value=driver), patch(
            "agent_hub.desktop_clients.resolve_app", return_value=fake_app
        ):
            res = run_desktop_ui(
                agent,
                {"id": "t1", "timeout_seconds": 30},
                self.task_ws,
                self.attempt_dir,
                lambda: False,
                "Please reply HUB_OK",
                dict(base),
                ProgressTracker(self.attempt_dir, "dui", "desktop_ui", "t1"),
            )
            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["output"], "HUB_OK desktop verified reply")
            self.assertEqual(res["model"]["source"], "desktop_ui_unverified")
            self.assertIsNone(res["model"]["value"])

            # Verify two concurrent desktop_ui runs serialize cleanly via desktop_ui.lock without overlap.
            a2 = self.root / "jobs" / "job-1" / "t2" / "attempt-1"
            (a2 / "workspace").mkdir(parents=True)
            windows = []

            def run_one(tid, adir):
                t0 = time.monotonic()
                out = run_desktop_ui(
                    agent,
                    {"id": tid, "timeout_seconds": 30},
                    adir / "workspace",
                    adir,
                    lambda: False,
                    "Reply " + tid,
                    dict(base),
                    ProgressTracker(adir, "dui", "desktop_ui", tid),
                )
                windows.append((tid, out["status"], t0, time.monotonic()))

            th1 = threading.Thread(target=run_one, args=("t1", self.attempt_dir))
            th2 = threading.Thread(target=run_one, args=("t2", a2))
            th1.start()
            th2.start()
            th1.join()
            th2.join()
            self.assertEqual(len(windows), 2)
            self.assertTrue(all(w[1] == "completed" for w in windows))
            # Because each run takes >= 2.0s settle time and they serialize on desktop_ui.lock, total elapsed >= 3.8s.
            self.assertGreaterEqual(max(w[3] for w in windows) - min(w[2] for w in windows), 3.8)

    def test_save_agent_desktop_ui_profile(self):
        store = Store(self.ws)
        store.init({"agents": []})
        fake_app = {"name": "Gemini", "path": "/Applications/Gemini.app", "bundle_id": "com.google.GeminiMacOS"}
        with patch("agent_hub.desktop_clients.resolve_app", return_value=fake_app):
            aid = save_agent(
                store,
                {
                    "name": "Gemini 桌面自动",
                    "transport": "desktop_ui",
                    "app_path": "/Applications/Gemini.app",
                    "model": "Gemini-2.5",
                    "new_chat_shortcut": "cmd+n",
                    "send_shortcut": "return",
                    "ui_settle_seconds": 3.5,
                },
            )
        saved = next(a for a in store.agents() if a["id"] == aid)
        self.assertTrue(saved["desktop_ui_enabled"])
        self.assertEqual(saved["desktop_bundle_id"], "com.google.GeminiMacOS")
        self.assertEqual(saved["ui_settle_seconds"], 3.5)


class DashboardV21HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "ws")
        self.store.init({"agents": [_agent("gpt", "gpt-6"), _agent("worker", "ds-1")]})
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.thread = threading.Thread(target=serve, args=(self.store, self.port, False), daemon=True)
        self.thread.start()
        for _ in range(40):
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                c.request("GET", "/api/health")
                if c.getresponse().status == 200:
                    break
            except OSError:
                time.sleep(0.05)

    def _req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        c.request(method, path, body=raw, headers=h)
        resp = c.getresponse()
        data = resp.read()
        c.close()
        return resp.status, data, resp.headers

    def test_old_server_sharing_workspace_blocks_startup(self):
        from agent_hub.console_access import check_for_legacy_dashboard

        with patch("agent_hub.console_access.http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({"app":"agent_hub","root":str(self.store.root)}).encode()
            with self.assertRaisesRegex(ValueError, "旧版"):
                check_for_legacy_dashboard(self.store.root, [self.port])
            check_for_legacy_dashboard(self.store.root / "other", [self.port])

    def test_upload_download_health_and_desktop_ui_status(self):
        st, raw, _ = self._req("GET", "/api/health")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw)["version"], __version__)

        from agent_hub.console_access import entry_path, matching_server
        entry = entry_path(self.store.root, self.port)
        self.assertTrue(matching_server(self.store.root, self.port))
        self.assertEqual(entry.stat().st_mode & 0o777, 0o600)
        self.assertEqual(entry.parent.stat().st_mode & 0o777, 0o700)
        tok = entry.with_suffix(".key").read_text()
        auth = {"X-Agent-Hub-Token": tok}
        self.assertNotIn("root", json.loads(raw))
        for path in ("/api/state", "/api/local-apps", "/api/desktop-ui-status", "/api/download?job=demo&file=final.md"):
            self.assertEqual(self._req("GET", path)[0], 403)
        st, raw, _ = self._req("GET", "/api/desktop-ui-status", headers=auth)
        self.assertEqual(st, 200)
        dui = json.loads(raw)
        self.assertIn("chatgpt", dui["profiles"])
        self.assertIn("accessibility", dui)

        st, html_raw, _ = self._req("GET", "/")
        self.assertEqual(st, 200)
        self.assertNotIn(tok.encode(), html_raw)
        self.assertIn(b"const token=", self._req("GET", "/", headers=auth)[1])
        self.assertEqual(self._req("GET", "/api/state", headers=dict(auth, Origin="https://foreign.example"))[0], 403)

        # CSRF check on /api/upload
        st, _, _ = self._req("POST", "/api/upload", {"filename": "a.txt", "content_base64": base64.b64encode(b"hello").decode()})
        self.assertEqual(st, 403)

        # Valid docx upload
        docx_path = Path(self.tmp.name) / "up.docx"
        _make_docx(docx_path, ["合同审核条款 A"])
        st, raw, _ = self._req(
            "POST",
            "/api/upload",
            {"filename": "up.docx", "content_base64": base64.b64encode(docx_path.read_bytes()).decode()},
            {"X-Agent-Hub-Token": tok, "Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(st, 200)
        meta = json.loads(raw)
        self.assertTrue(meta["has_text_preview"])
        self.assertIn("合同审核条款 A", meta["preview_excerpt"])

        # Delivery download + traversal rejection
        job = self.store.add_plan({"title": "dl", "tasks": [{"id": "t1", "prompt": "p", "validation": {"min_chars": 1}}]})
        deliv = self.store.root / "jobs" / job / "delivery"
        deliv.mkdir(parents=True)
        (deliv / "final.md").write_text("# 最终交付\n内容正常", encoding="utf-8")

        st, raw, hdrs = self._req("GET", f"/api/download?job={job}&file=final.md", headers=auth)
        self.assertEqual(st, 200)
        self.assertIn("最终交付", raw.decode("utf-8"))
        self.assertIn("attachment;", hdrs.get("Content-Disposition", ""))

        st, _, _ = self._req("GET", f"/api/download?job={job}&file=../../../state.sqlite3", headers=auth)
        self.assertEqual(st, 400)

        st, raw, _ = self._req("POST", "/api/action", {"action":"download_ticket", "job":job, "file":"final.md"}, auth)
        self.assertEqual(st, 200)
        ticket_url = json.loads(raw)["url"]
        self.assertEqual(self._req("GET", ticket_url)[0], 200)
        self.assertEqual(self._req("GET", ticket_url)[0], 403)
        args = {"commander":"gpt", "participants":["worker"], "upload_ids":[meta["id"]], "goal":"test"}
        self.assertEqual(self._req("POST", "/api/action", dict(args,action="workflow"), auth)[0], 400)
        st, raw, _ = self._req("POST", "/api/action", dict(args,action="preview_attachments",operation="workflow"), auth)
        self.assertEqual(st, 200)
        review = json.loads(raw)
        self.assertEqual(set(review["files"]), {"uploaded/up.docx"})
        self.assertEqual({r["id"] for r in review["recipients"]}, {"gpt", "worker"})
        with patch("agent_hub.dashboard.spawn_job",return_value=1):
            st, raw, _ = self._req("POST", "/api/action", dict(args,action="workflow",attachment_approval=review), auth)
        self.assertEqual(st, 200, raw)
        from agent_hub.mcp import MCPServer
        snapshot = MCPServer(self.store).call("hub_status", {})
        self.assertNotIn("uploads", snapshot)
        private_job = next(j for j in snapshot["jobs"] if j["id"] == json.loads(raw)["job"])
        self.assertNotIn("tasks", private_job)

        # Model catalog refresh & user selection over HTTP
        st, raw, _ = self._req(
            "POST",
            "/api/action",
            {"action": "select_model", "agent": "gpt", "model": "gpt-6.2-sol"},
            {"X-Agent-Hub-Token": tok, "Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw)["model"], "gpt-6.2-sol")
        gpt_after = next(a for a in self.store.agents() if a["id"] == "gpt")
        self.assertEqual(gpt_after["execution_model"], "gpt-6.2-sol")


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_codex_and_dsh_auto_discovery_and_selection(self):
        from agent_hub.model_catalog import (
            discover_codex_models,
            discover_dsh_models,
            refresh_agent_models,
            select_agent_model,
        )

        # Fake Codex home (with hidden model + auth.json that must never be touched)
        codex_home = self.root / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text('{"token":"NEVER_READ"}', encoding="utf-8")
        (codex_home / "config.toml").write_text('model = "gpt-6-astra"\n[profiles.x]\nmodel = "ignored"\n', encoding="utf-8")
        (codex_home / "models_cache.json").write_text(
            json.dumps(
                {
                    "fetched_at": "2026-11-18T00:00:00Z",
                    "models": [
                        {"slug": "gpt-6.2-astra", "display_name": "GPT-6.2-Astra", "visibility": "list"},
                        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list"},
                        {"slug": "internal-hidden", "display_name": "Hidden", "visibility": "hide"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        c_res = discover_codex_models(codex_home=codex_home)
        c_ids = [m["id"] for m in c_res["models"]]
        self.assertIn("gpt-6.2-astra", c_ids)
        self.assertIn("gpt-6.1-sol", c_ids)
        self.assertIn("gpt-6-astra", c_ids)
        self.assertNotIn("internal-hidden", c_ids)

        # Fake DSH executable & profiles
        fake_dsh = self.root / "fake_dsh"
        schema_payload = {
            "$defs": {
                "cfg": {
                    "properties": {
                        "models": {
                            "default": [
                                {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash", "contextWindow": 1048576},
                                {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "contextWindow": 1048576},
                            ]
                        }
                    }
                }
            }
        }
        fake_dsh.write_text(f"#!/usr/bin/env python3\nimport json\nprint(json.dumps({schema_payload!r}))\n", encoding="utf-8")
        fake_dsh.chmod(fake_dsh.stat().st_mode | stat.S_IXUSR)

        dsh_home = self.root / ".dsh"
        (dsh_home / "profiles" / "desktop").mkdir(parents=True)
        (dsh_home / "profiles" / "desktop" / "cordis.patch.yml").write_text(
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      bailian-dashscope:\n"
            "        apiKey: SECRET_MUST_IGNORE\n"
            "        models:\n"
            "          - id: kimi-k3\n"
            "            name: Kimi K3\n",
            encoding="utf-8",
        )
        d_res = discover_dsh_models(executable=str(fake_dsh), dsh_home=dsh_home)
        d_by_id = {m["id"]: m for m in d_res["models"]}
        self.assertIn("deepseek-flash", d_by_id)
        self.assertIn("deepseek-v4-pro", d_by_id)
        self.assertTrue(d_by_id["deepseek-v4-pro"]["runnable_directly"])
        self.assertIn("kimi-k3", d_by_id)
        self.assertEqual(d_by_id["kimi-k3"]["provider"], "bailian-dashscope")
        self.assertNotIn("SECRET_MUST_IGNORE", json.dumps(d_res))

        # End-to-end store refresh & model switch
        store = Store(self.root / "ws")
        codex_a = _agent("hub-gpt", "gpt-6-astra", "codex_cli")
        codex_a["observed_model"] = {"value": "gpt-6-astra", "source": "runtime"}
        dsh_a = _agent("hub-deepseek", "deepseek-flash", "dsh_cli")
        dsh_a["executable"] = str(fake_dsh)
        dsh_a["dsh_enabled"] = True
        dsh_a["dsh_provider"] = "deepseek-official"
        store.init({"agents": [codex_a, dsh_a]})

        ref = refresh_agent_models(store, codex_home=codex_home, dsh_home=dsh_home)
        self.assertEqual(ref["updated_agents"], 2)
        self.assertGreaterEqual(ref["total_models"], 6)

        select_agent_model(store, "hub-gpt", "gpt-6.2-astra")
        updated_gpt = next(a for a in store.agents() if a["id"] == "hub-gpt")
        self.assertEqual(updated_gpt["execution_model"], "gpt-6.2-astra")
        self.assertNotIn("observed_model", updated_gpt)

        select_agent_model(store, "hub-deepseek", "deepseek-v4-pro")
        updated_dsh = next(a for a in store.agents() if a["id"] == "hub-deepseek")
        self.assertEqual(updated_dsh["execution_model"], "deepseek-v4-pro")
        self.assertEqual(updated_dsh["dsh_provider"], "deepseek-official")

        with self.assertRaises(ValueError):
            select_agent_model(store, "hub-gpt", "bad model with space")


if __name__ == "__main__":
    unittest.main()
