# Agent 适配器与交接协议

本原型只自动运行已验证的 Codex CLI 入口。ChatGPT、WorkBuddy、豆包以及尚未完成独立登录和沙箱配置的 DeepSeek Harness 通过文件交接等待真实成果。文件交接的 `waiting_external` 表示仍需外部客户端执行，不代表自动调用成功。

## Codex 的授权边界

适配器使用已安装 CLI 和现有 ChatGPT 登录，不读取、复制或展示凭据。每次模型调用前先通过官方 `login status` 确认 ChatGPT 登录；检测到 API 登录或无法确认时直接停止。不会尝试更改登录方式。调用环境仅保留必要的系统路径和语言设置，排除 `OPENAI_API_KEY`、`CODEX_API_KEY` 及其他未列入允许清单的环境变量。

适配器先读取本机 CLI 帮助，要求支持 `--ignore-user-config`、`--ephemeral`、`--json`、`--skip-git-repo-check`、`--sandbox`、`--no-daemon` 和 `--ask-for-approval`。缺少必要选项时失败，不降级为无沙箱运行。支持时也使用 `--ignore-rules`。CLI 的用户配置不会加载，实际默认模型可能与桌面配置提示不同。

任务缺省采用 `read-only`；只有 `allowed_tools` 包含 `files_write` 才使用 `workspace-write`。禁止 `danger-full-access`、提升权限和绕过沙箱。工作区写入模式关闭命令网络访问，排除额外的系统临时目录写入授权。模型本身仍通过现有 ChatGPT 服务完成推理，会消耗现有账号额度。

这是 Codex 提供的写入沙箱，不能视为对整台 Mac 的文件读取隔离。`files_read`、`files_write`、一般命令执行等细分授权还会放入任务提示，但没有逐工具的强制隔离；提示约束也不等于安全边界。允许文件操作时，可使用必要且有界的列举、读取和写入命令；未授权一般命令执行时，不执行工作区代码、安装软件或启动服务。任务内容和依赖资料不得扩大授权范围。

官方依据：[CLI 选项](https://learn.chatgpt.com/docs/cli/reference)、[认证](https://learn.chatgpt.com/docs/auth)、[权限与沙箱](https://learn.chatgpt.com/docs/permissions)。正式使用时仍以已安装 CLI 的帮助和组织策略为准。

## 模型身份和能力证据

- `configured_model`：发现的配置线索，或显式 `execution_model` 请求，不证明实际模型。
- `model`：只采纳 CLI 结构化会话事件或 CLI 标准诊断头中的模型字段。没有字段时为 `value: null`、`confidence: unknown`。
- Agent 在回复中声称的模型名不作为运行时证据。文件导入的模型自报同样保持未核实。
- 适配器只报告执行状态和成果。调度器另行按可信计划验收，不因一句“完成”而通过。

`execution_model` 是可选的显式调用模型参数。普通 `agent.model` 是发现线索，适配器不会将它偷偷转成 `--model`。

## 暂停、退出与日志

每个 CLI 调用拥有独立进程组。暂停会终止整个组；恢复会产生新尝试，不恢复模型内存，也不重复派发已验收任务。执行时间缺省 300 秒，可由 Agent 配置指定，再由任务的 `timeout_seconds` 覆盖，范围限制为 1–3600 秒。标准输出与错误输出合计缺省上限 4 MiB，可配置但最多 16 MiB；超时或超限会停止进程组。

在调度器主线程执行时，适配器只在子进程存活期间临时处理 `SIGTERM`，通过异常清理停止子进程组，并恢复调用前的处理器。`SIGINT` 也沿异常清理路径停止子进程组。在后台线程内不安装全局信号处理器，需由调用方提供暂停回调并管理退出。

每次尝试保存：

| 文件 | 内容 |
|---|---|
| `invocation.json` | 实际参数、沙箱、限额、登录方式证据及配置模型提示；不记录环境变量值 |
| `process.json` | 调度器 PID、CLI PID 与进程组、状态和时间；用于异常退出审计 |
| `stdout.jsonl` | 有上限的结构化执行事件，常见凭据形式已遮蔽 |
| `stderr.log` | 有上限的诊断，常见凭据形式已遮蔽 |

强制 `SIGKILL`、系统崩溃或断电无法由 Python 清理器处理。`process.json` 可能留在 `running`，它只是历史线索，不能凭 PID 自动杀进程：PID 可能已被系统复用。当前调度器会把残留的执行中任务标记为中断，要求检查已有成果并显式重试，不自动重复执行。若以后要在 `SIGKILL` 后也确保子进程停止，应增加独立的父进程存活监护程序，并核实进程身份后清理；现版本尚未提供该保证。

关闭网页工作台不会停止已经派发的任务；请先使用“暂停”，等待状态变为暂停，再关闭运行窗口。

日志遮蔽只覆盖常见凭据格式，不是通用敏感信息过滤器。任务内不应主动包含密钥或无关个人数据。发生强制中断时，内存中的部分输出可能没有写入日志；工作区内已有文件应在重试前检查。

## 外部客户端文件交接

每个外部交接尝试生成 `handoff.json`、`INSTRUCTIONS.md` 和 `result-template.json`。打开说明，将其中任务交给指定客户端，并把真实成果保存到该尝试的 `workspace`。复制模板为 `result.json`，填写：

```json
{
  "output": "目标客户端的真实最终回复",
  "artifacts": ["report.md"],
  "model": null
}
```

`artifacts` 必须是该任务工作区内的相对文件路径；没有文件时使用空数组。不要在此填入其他应用的凭据。空白模板的 `output` 为空，不能直接作为完成成果导入。

在项目目录中执行下列命令，将 `JOB_ID`、`TASK_ID` 和结果路径替换为实际值；`--root` 应与创建任务时保持一致：

```sh
python3 -m agent_hub --root ./workspace import-result JOB_ID TASK_ID /absolute/path/result.json
```

也可以在本机工作台的“导入外部客户端成果”中提交包含任务组 ID、任务 ID 和上述 `result` 对象的表单。导入后仍按计划验收；导入失败需要修正成果，不能绕过检查。导入成功后 CLI 会继续执行已授权的后续任务。

DeepSeek Harness 虽存在官方 `dsh` 入口，但 Desktop 与 headless 的配置、模型选择和登录不能推定共享。在其安全执行边界和独立登录得到验证前，`dsh_cli` 仍转为文件交接。

## 适配器接口

```python
run_adapter(agent, task, workspace, attempt_dir, should_pause) -> dict
```

`workspace` 是本次尝试工作区，依赖成果由调度器预先放入；`attempt_dir` 保存审计和交接文件。返回 `completed`、`failed`、`paused` 或 `waiting_external`，以及文本、变动的普通文件相对路径、模型证据、耗时和传输方式。符号链接不作为成果导出。测试夹具会额外标注 `is_test_fixture`，不计为任何 AI 的完成记录。

适配器的本地测试覆盖真实无害进程的暂停、超时、输出限制、SIGINT/SIGTERM 子进程清理、登录方式拒绝、环境变量过滤和模型证据规则。这些测试证明调度传输行为，不证明模型能力或 Codex 沙箱实现本身。
