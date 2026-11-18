"""Finder-friendly launcher; shares the unified desktop workspace by default and reuses any running matching server."""
import os
from pathlib import Path
import socket
import sys
import webbrowser

from agent_hub import __version__
from agent_hub.console_access import entry_path, matching_server
from agent_hub.core import Store
from agent_hub.dashboard import serve
from agent_hub.discovery import discover

project = Path(__file__).resolve().parent
if "--local" in sys.argv[1:]:
    root = (project / "workspace").resolve()
elif os.environ.get("AGENT_HUB_ROOT"):
    root = Path(os.environ["AGENT_HUB_ROOT"]).expanduser().resolve()
else:
    root = Path("~/Library/Application Support/LocalAgentHub/workspace").expanduser().resolve()
root.mkdir(mode=0o700, parents=True, exist_ok=True)
PORTS = list(range(8765, 8786))


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# 1. Reuse if this exact workspace & version is already running on any candidate port.
for p in PORTS:
    if matching_server(root, p):
        webbrowser.open(entry_path(root, p).as_uri())
        raise SystemExit(0)

# 2. Pick 8765 if free; otherwise automatically fall back to the next free port.
chosen = None
for p in PORTS:
    if _port_free(p):
        chosen = p
        break

if chosen is None:
    print("工作台没有启动：8765–8785 端口均已被占用。")
    input("按回车关闭窗口。")
    raise SystemExit(1)

if chosen != 8765:
    print("提示：8765 端口正被其他服务（例如旧版后台工作台）占用，已自动改用 %d 端口启动 v%s。" % (chosen, __version__), flush=True)

store = Store(root)
if not store.agents():
    store.init(discover())
try:
    serve(store, port=chosen, open_browser=True)
except OSError as exc:
    print("工作台没有启动：" + str(exc))
    print("如果端口被其他程序使用，可运行 python3 -B -m agent_hub dashboard --port 8790 指定端口。")
    input("按回车关闭窗口。")
