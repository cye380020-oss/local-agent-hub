import argparse
import json
from pathlib import Path
import shlex
import sys

from .core import Store, collect_ready_results, dump, retry_task, run_job
from .discovery import discover
from .plans import handoff_plan, probe_plan


def confirm_attachments(store, paths, recipients):
    from .attachments import review_inputs

    review, _ = review_inputs(store, [], paths, recipients)
    print("所选文件和衍生成果可发送给以下接收者（客户端可能调用云服务）")
    for r in review["recipients"]:
        print(" · ".join(str(r[k]) for k in ("id", "transport", "destination", "provider", "model") if r[k]))
    files = list(review["files"])
    for i, path in enumerate(files, 1):
        print(i, path)
    try:
        answer = input("输入允许共享的文件编号（逗号分隔），或输入 ALL 确认全部：").strip()
    except EOFError:
        raise ValueError("未确认附件，本次操作已取消")
    numbers = list(range(1, len(files)+1)) if answer == "ALL" else [int(n) for n in answer.split(",") if n.strip()]
    if not numbers or any(n < 1 or n > len(files) for n in numbers):
        raise ValueError("未选择有效附件，本次操作已取消")
    review["files"] = {files[n-1]: review["files"][files[n-1]] for n in numbers}
    return review


def main():
    parser = argparse.ArgumentParser(description="本地多 Agent 跨模型协作工作台：任务编排、实时跟踪、自动回收、暂停与恢复")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1] / "workspace"), help="共享数据目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="检查已安装客户端并初始化")
    sub.add_parser("discover", help="重新检查公开入口及配置")
    sub.add_parser("status", help="查看全部任务、Agent 与实时进度状态")
    p = sub.add_parser("submit", help="提交可信 JSON 任务计划")
    p.add_argument("plan", type=Path)
    for name in ("run", "pause", "resume"):
        p = sub.add_parser(name)
        p.add_argument("job")
        if name == "run":
            p.add_argument("--wait", action="store_true", help="进入队列，等待已有任务释放执行锁")
    p = sub.add_parser("retry")
    p.add_argument("job")
    p.add_argument("task")
    p = sub.add_parser("import-result", help="导入外部客户端的真实成果，重新验收")
    p.add_argument("job")
    p.add_argument("task")
    p.add_argument("result", type=Path)
    p = sub.add_parser("collect", help="自动扫描并回收已完成的 Worker 队列与 result.json 成果")
    p.add_argument("--no-run", action="store_true", help="仅回收并验收，不自动继续后续步骤")
    p = sub.add_parser("probe", help="执行真实能力小测，消耗现有客户端额度")
    p.add_argument("--agent", default="codex")
    sub.add_parser("handoff-demo", help="创建包含 ChatGPT 实质工作的交接计划")
    sub.add_parser("setup-gpt", help="新增使用现有登录的 GPT 指挥与 Codex 执行角色")
    p = sub.add_parser("setup-dsh", help="一键启用 DeepSeek Harness 官方无头 CLI 执行入口")
    p.add_argument("--use-dsh-credentials", action="store_true", help="复用 DeepSeek Harness 本机已保存的凭据/账号插件")
    p.add_argument("--provider", default=None, help="指定 DeepSeek Harness provider（如 deepseek-official 或 bailian-dashscope）")
    p.add_argument("--model", default=None, help="指定模型标识（如 deepseek-flash 或 deepseek-v4.1-flash-1017）")
    p = sub.add_parser("sync-models", help="同步指定 API Agent 的可用模型列表")
    p.add_argument("agent")
    p = sub.add_parser("refresh-models", help="自动识别并更新桌面端与 CLI Agent 的可用模型列表")
    p.add_argument("--agent", default=None, help="仅刷新指定 Agent ID（留空刷新全部）")
    p.add_argument("--include-api", action="store_true", help="同时在线刷新 HTTP API Agent 的 /models 列表")
    p = sub.add_parser("set-model", help="自主切换指定 Agent 当前使用的模型")
    p.add_argument("agent")
    p.add_argument("model")
    p.add_argument("--provider", default=None, help="可选：同时指定 DeepSeek Harness provider")
    p = sub.add_parser("collaborate", help="多 Agent 跨模型自动分工、并行执行、复核和交付")
    p.add_argument("goal")
    p.add_argument("--requirements", default="成果应完整、准确，不得编造执行结果")
    p.add_argument("--commander", default="hub-gpt")
    p.add_argument("--participants", default="", help="逗号分隔的参与 Agent ID（留空则自动选择全部就绪入口）")
    p.add_argument("--files", action="store_true", help="允许隔离工作区文件操作")
    p.add_argument("--cross-model", action="store_true", help="强制要求至少两个不同模型/供应商参与及跨模型复核")
    p.add_argument("--confirm-plan", action="store_true", help="总指挥规划完成后先暂停等待人工确认或微调分工")
    p.add_argument("--attach", action="append", default=[], help="挂载本地文件或目录快照（可多次指定）")
    p.add_argument("--sequential", action="store_true", help="禁用独立步骤并行执行，改为顺序执行")
    p.add_argument("--max-concurrency", type=int, default=3)
    p.add_argument("--max-steps", type=int, default=3)
    p.add_argument("--max-revisions", type=int, default=1)
    p.add_argument("--max-calls", type=int, default=16)
    p = sub.add_parser("approve-plan", help="批准待确认的分工计划并继续执行")
    p.add_argument("job")
    p.add_argument("--note", default="", help="附加给执行者的补充说明")
    p = sub.add_parser("inject-note", help="向运行中或暂停的任务追加指令或补充挂载资料")
    p.add_argument("job")
    p.add_argument("--note", default="")
    p.add_argument("--attach", action="append", default=[])
    p = sub.add_parser("approve-draft", help="人工拍板直接采纳当前整合草稿并完成交付")
    p.add_argument("job")
    p.add_argument("--note", default="")
    p = sub.add_parser("request-revision", help="基于现有草稿或交付按人工意见发起新一轮返工修订")
    p.add_argument("job")
    p.add_argument("--feedback", required=True)
    p.add_argument("--steps", default="", help="可选：逗号分隔的需重跑步骤 ID（留空仅重做汇总）")
    p = sub.add_parser("check-accessibility", help="检测受控桌面 UI 自动操控所需的 macOS 辅助功能权限")
    p.add_argument("--prompt", action="store_true", help="请求 macOS 弹出官方辅助功能授权提示")
    p = sub.add_parser("dashboard", help="启动本机中文工作台")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    p = sub.add_parser("mcp", help="启动可供支持 MCP 的客户端连接的本地工具入口")
    p.add_argument("--extended", action="store_true", help="启用扩展工作流与外部 Worker 自动接单工具")
    p = sub.add_parser("worker", help="启动本机自动 Worker 桥接进程，自动接收任务、上报进度并回传结果")
    p.add_argument("--agent", required=True, help="绑定到工作台中的 worker_bridge Agent ID")
    p.add_argument("--url", default="http://127.0.0.1:8765", help="工作台地址")
    p.add_argument("--token", default="", help="Worker 令牌（留空则从工作区 .worker-token 自动读取）")
    p.add_argument("--exec", dest="exec_cmd", required=True, help="收到任务后执行的本地命令（stdin 传入任务 JSON，stdout 返回结果）")
    p.add_argument("--model", default=None, help="上报模型名称")
    p.add_argument("--poll-interval", type=float, default=1.0)
    p.add_argument("--once", action="store_true")

    args = parser.parse_args()
    store = Store(args.root)
    command = args.command
    if command in {"init", "discover"}:
        registry = discover()
        store.init(registry)
        print(dump(registry))
    elif command == "status":
        print(dump(store.snapshot()))
    elif command == "submit":
        print(store.add_plan(json.loads(args.plan.read_text(encoding="utf-8"))))
    elif command == "run":
        print(dump(run_job(store, args.job, wait_for_lock=args.wait)))
    elif command in {"pause", "resume"}:
        store.control(args.job, command == "pause")
        if command == "resume":
            print(dump(run_job(store, args.job, wait_for_lock=True)))
    elif command == "retry":
        print(dump(retry_task(store, args.job, args.task)))
    elif command == "import-result":
        print(dump(store.import_result(args.job, args.task, json.loads(args.result.read_text(encoding="utf-8")))))
        print(dump(run_job(store, args.job, wait_for_lock=True)))
    elif command == "collect":
        jobs = collect_ready_results(store)
        outcomes = []
        if not args.no_run:
            for jid in jobs:
                outcomes.append(run_job(store, jid, wait_for_lock=True))
        print(dump({"collected_jobs": jobs, "outcomes": outcomes}))
    elif command == "probe":
        job = store.add_plan(probe_plan(args.agent))
        print("能力小测: " + job, flush=True)
        print(dump(run_job(store, job, wait_for_lock=True)))
    elif command == "handoff-demo":
        job = store.add_plan(handoff_plan())
        print(dump(run_job(store, job, wait_for_lock=True)))
    elif command == "setup-gpt":
        from .workflow import setup_gpt

        setup_gpt(store)
        print("已启用 GPT / Codex 协作入口；不恢复已删除客户端")
    elif command == "setup-dsh":
        from .management import setup_dsh

        aid = setup_dsh(
            store,
            use_dsh_credentials=args.use_dsh_credentials,
            provider=args.provider,
            model=args.model,
        )
        print("已启用 DeepSeek Harness 无头执行入口：" + aid)
    elif command == "sync-models":
        from .management import sync_api_models

        print(dump(sync_api_models(store, args.agent)))
    elif command == "refresh-models":
        from .model_catalog import refresh_agent_models

        print(dump(refresh_agent_models(store, agent_id=args.agent, include_http_api=args.include_api)))
    elif command == "set-model":
        from .model_catalog import select_agent_model

        print(dump(select_agent_model(store, args.agent, args.model, provider=args.provider)))
    elif command == "collaborate":
        from .workflow import create_flow, flow_recipients

        participants = [x.strip() for x in args.participants.split(",") if x.strip()] if args.participants else None
        data = {
            "goal": args.goal,
            "requirements": args.requirements,
            "commander": args.commander,
            "participants": participants,
            "files_allowed": args.files,
            "require_cross_model": args.cross_model,
            "confirm_plan": args.confirm_plan,
            "local_paths": args.attach,
            "parallel_steps": not args.sequential,
            "max_concurrency": args.max_concurrency,
            "max_steps": args.max_steps,
            "max_revisions": args.max_revisions,
            "max_calls": args.max_calls,
        }
        if args.attach:
            data["attachment_approval"] = confirm_attachments(store, args.attach, flow_recipients(store, data))
        job = create_flow(store, data)
        print("协作任务：" + job, flush=True)
        print(dump(run_job(store, job, wait_for_lock=True)))
    elif command == "approve-plan":
        from .workflow import approve_plan

        approve_plan(store, args.job, user_note=args.note)
        print(dump(run_job(store, args.job, wait_for_lock=True)))
    elif command == "inject-note":
        from .workflow import inject_workflow_note, read_flow

        approval = None
        if args.attach:
            flow = read_flow(store, args.job)
            if not flow:
                raise ValueError("找不到此工作流")
            approval = confirm_attachments(store, args.attach, flow[0]["participants"])
        print(dump(inject_workflow_note(store, args.job, note=args.note, local_paths=args.attach, attachment_approval=approval)))
    elif command == "approve-draft":
        from .workflow import approve_draft

        print(dump(approve_draft(store, args.job, note=args.note)))
    elif command == "request-revision":
        from .workflow import request_revision

        steps = [s.strip() for s in args.steps.split(",") if s.strip()] if args.steps else None
        request_revision(store, args.job, args.feedback, steps=steps)
        print(dump(run_job(store, args.job, wait_for_lock=True)))
    elif command == "check-accessibility":
        from .desktop_ui import check_accessibility

        print(dump(check_accessibility(prompt=args.prompt)))
    elif command == "dashboard":
        from .dashboard import serve

        serve(store, args.port, not args.no_open)
    elif command == "mcp":
        from .mcp import serve

        serve(store, extended=args.extended)
    elif command == "worker":
        from .worker_bridge import ensure_worker_token, run_worker_loop

        token = args.token.strip() or ensure_worker_token(store)
        run_worker_loop(
            args.url,
            token,
            args.agent,
            shlex.split(args.exec_cmd),
            model=args.model,
            poll_interval=args.poll_interval,
            once=args.once,
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print("错误：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
