# Mac 独立应用（v2.0）

默认构建产物：`macos-app/本地Agent工作台.app`（可安装到 `~/Applications/本地Agent工作台.app` 并在桌面建立软链接）。
数据目录使用可移植路径 `~/Library/Application Support/LocalAgentHub/workspace`（运行时动态展开当前用户目录，不硬编码本机用户名；首次运行自动初始化，已有数据无缝保留）。

双击桌面图标打开独立窗口。应用会核验本机 8765 端口的服务身份和工作区；没有服务时自动启动。无需终端，也不依赖当前聊天。连接中断可点击窗口上方“重新连接”。

关闭窗口或退出应用不会暂停已运行任务；需停止任务时先在工作台点击“暂停”。未设置开机自启，重启 Mac 后双击应用即可重新启动。

程序代码随 app 打包，使用这台 Mac 已安装的 `/usr/bin/python3`。现有 AI 客户端仍须安装并登录。当前多客户端接入限制不变，封装不会自动接通尚未验证的客户端。

任务数据库、成果和日志现在存放在 `~/Library/Application Support/LocalAgentHub/workspace`。应用菜单“打开任务文件夹”可直接查看。

本机专用构建，使用本地 ad-hoc 签名；没有制作供其他 Mac 分发的公证安装包或内置 Python 运行时。移动任务数据前应更新应用中的 HubWorkspace 路径并重新签名。

构建源码：main.m、icon.m、launch_background.py、build.py。运行 `python3 -B macos-app/build.py` 可重新构建；生成的 app 位于 macos-app，已有安装需单独更新。
