"""Small real-model probes, and an honest multi-client handoff demonstration."""
import uuid


def probe_plan(agent="codex"):
    return {"id": "probe-" + uuid.uuid4().hex[:8], "title": "真实客户端能力小测（不代表综合模型排名）", "tasks": [
        {"id": "reasoning", "agent": agent, "kind": "reasoning", "prompt": '不要调用工具。计算 1 至 4 的平方和，只返回 JSON {"sum":数字,"sorted":[3,1,2排序后的数字]}。',
         "validation": {"json_equals": {"sum": 30, "sorted": [1, 2, 3]}}, "timeout_seconds": 240},
        {"id": "writing", "agent": agent, "kind": "writing_zh", "prompt": '不要调用工具。用简体中文写三条清晰的项目交接要求，必须包含“目标”“成果”“验收”这三个词。',
         "validation": {"text_contains": ["目标", "成果", "验收"]}, "timeout_seconds": 240},
        {"id": "files", "agent": agent, "kind": "coding", "allowed_tools": ["files_read", "files_write"],
         "prompt": '只在当前工作区创建 result.json，内容为 {"sum":30,"verified":true}。最后只返回 JSON {"file":"result.json"}。不要访问工作区以外的文件，不联网，不安装依赖。',
         "validation": {"json_equals": {"file": "result.json"}, "artifacts": [{"path": "result.json", "min_bytes": 20, "json_equals": {"sum": 30, "verified": True}}]}, "timeout_seconds": 240}
    ]}


def handoff_plan():
    return {"id": "handoff-" + uuid.uuid4().hex[:8], "title": "ChatGPT 指挥并执行 → Codex 实作 → ChatGPT 审核汇总", "tasks": [
        {"id": "commander_analysis", "agent": "chatgpt", "kind": "reasoning",
         "prompt": '你是总指挥，同时必须执行实质分析。为本地多 Agent 系统写出一份短评：现有证据、可立即运行的入口、未知项、验收标准。包含“证据”“入口”“验收”三个词。不要只宣布分工。',
         "validation": {"text_contains": ["证据", "入口", "验收"]}},
        {"id": "implementation", "agent": "codex", "dependencies": ["commander_analysis"], "kind": "coding", "allowed_tools": ["files_read", "files_write"],
         "prompt": '根据 inputs 和任务上下文写一份运行检查清单到 checklist.md，包含“启动”“暂停”“恢复”“验收”。最后仅返回 JSON {"file":"checklist.md"}。只操作当前工作区。',
         "validation": {"json_equals": {"file": "checklist.md"}, "artifacts": [{"path": "checklist.md", "min_bytes": 40, "text_contains": ["启动", "暂停", "恢复", "验收"]}]}},
        {"id": "cross_review", "agent": "chatgpt", "dependencies": ["implementation"], "review_of": "implementation", "kind": "review",
         "prompt": '实质审查上一任务的成果与验收证据。若满足要求只返回 JSON {"decision":"accept","reason":"具体理由"}；否则 decision 为 reject 并说明问题。不要假定已通过。',
         "validation": {"review_accept": True}},
        {"id": "commander_summary", "agent": "chatgpt", "dependencies": ["commander_analysis", "implementation", "cross_review"], "kind": "writing_zh",
         "prompt": '依据全部已验证成果，执行最终中文综合分析，说明“已完成”“限制”“下一步”。只陈述有证据支持的结论。',
         "validation": {"text_contains": ["已完成", "限制", "下一步"]}}
    ]}
