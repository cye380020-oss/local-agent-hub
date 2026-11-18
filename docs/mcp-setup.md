# 连接支持 MCP 的客户端

本项目提供本机 MCP stdio 工具入口，支持协议版本 `2024-11-05`。入口使用 Python 标准库，不开启网络端口，不读取密钥，也不会自动修改任何客户端配置。

目前**没有自动连接 ChatGPT**。只有在客户端明确支持本地 MCP stdio，并且你在客户端设置中启用后，ChatGPT 或其他 Agent 才能调用这里的工具。普通聊天窗口、已安装应用以及本项目中的同名 Agent 注册项，都不能证明已建立连接。没有连接时，使用任务文件交接。

支持通用 `mcpServers` 配置格式的客户端，可参考以下样例。客户端各自的配置位置和字段要求需要按其官方说明确认。

```json
{
  "mcpServers": {
    "local-agent-hub": {
      "command": "/usr/bin/python3",
      "args": [
        "-m", "agent_hub",
        "--root", "<WORKSPACE_DIR>",
        "mcp",
        "--extended"
      ],
      "env": {
        "PYTHONPATH": "<PROJECT_DIR>"
      }
    }
  }
}
```

> 默认 `mcp` 暴露 6 个核心调度工具；加上 `--extended` 可开启共 12 个工具，新增 `hub_list_agents`、`hub_create_workflow`、`hub_poll_tasks`、`hub_claim_task`、`hub_report_progress`、`hub_complete_task`，支持任意 MCP 客户端直接作为多 Agent 自动接单 Worker。

若系统 Python 不可用，把 `command` 改成当前可用 Python 3 的绝对路径。移动项目后同步修改两处项目路径。此配置只展示接入方式，尚未写入任何应用。

## 工具与实际行为

| 工具 | 行为 |
|---|---|
| `hub_status` | 读取当前 Agent 接入证据、任务状态、验收记录和能力统计 |
| `hub_submit_plan` | 保存有依赖关系和验收条件的计划；不会立即执行 |
| `hub_run` | 后台排队执行已有任务组；真实完成状态需再读取 |
| `hub_pause` | 请求暂停；适配器协作取消，已发生的操作不保证撤销 |
| `hub_resume` | 恢复并后台继续；失败或中断的任务仍需显式重试 |
| `hub_import_result` | 导入成果对象，重新验收后继续依赖任务；不接受任意文件读取入口 |

执行可能使用已登录客户端的现有订阅额度。不会申请或购买额外模型 API。模型名称只有存在相应来源证据时才用于报告，外部自报模型保持未验证。

## 让 ChatGPT 实际参与

连接成功后，可在 ChatGPT 中指定：先调用 `hub_status`，根据实际接入和已验收的小测结果拆解任务；ChatGPT 自己承担需求分析或方案、独立审查和最终分析任务；Codex 承担适合的实现与测试。每个任务必须有可检查的验收条件。使用 `hub_submit_plan` 保存计划，再按授权运行。

ChatGPT 完成等待外部交接的任务后，调用 `hub_import_result` 提交自己的 `output`，有文件时先把文件放在交接说明指定的任务工作区，再提交相对路径。工具会验收实际成果，不把一句“已完成”直接当作成功。系统生成的 `summary.md` 只是收集已有证据；ChatGPT 的最终分析应作为独立任务提交。

运行中的任务由独立后台进程执行，MCP 仍可接受暂停和状态读取。多个任务组按进程锁串行排队，后提交的任务不会因为调度器忙碌而丢失。关闭 MCP 客户端不会自动终止已经启动的后台任务；再次连接后可读取状态或暂停。调度器持有进程锁，阻止重复派发；意外中断的运行记录要求检查后显式重试。

官方资料：[Codex 开发命令](https://learn.chatgpt.com/docs/developer-commands)说明了非交互执行与 ChatGPT 网页聊天的入口区别；[Computer Use](https://learn.chatgpt.com/docs/computer-use)说明了经过权限设置的界面操作。它们并不能证明本机的普通 ChatGPT 聊天已连接到本项目。
