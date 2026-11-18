"""Launch the bundled server independently of its native window, picking a free port if 8765 is occupied."""
import fcntl
from pathlib import Path
import socket
import subprocess
import sys
import time

root = Path(sys.argv[1]).expanduser().resolve()
server = Path(__file__).resolve().parent
root.mkdir(mode=0o700, parents=True, exist_ok=True)
if str(server) not in sys.path:
    sys.path.insert(0, str(server))

from agent_hub.console_access import matching_server

PORTS = list(range(8765, 8786))
PORT_FILE = root / "desktop-v2.port"

# Initialize a fresh workspace cleanly while leaving any existing state.sqlite3 untouched.
if not (root / "state.sqlite3").is_file():
    from agent_hub.core import Store
    from agent_hub.discovery import discover

    store = Store(root)
    if not store.agents():
        store.init(discover())


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


with (root / "desktop-start.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    for candidate in PORTS:
        if matching_server(root, candidate):
            PORT_FILE.write_text(str(candidate), encoding="utf-8")
            raise SystemExit(0)

    chosen = next((p for p in PORTS if _port_free(p)), None)
    if chosen is None:
        raise SystemExit("No free loopback port in 8765-8785")

    PORT_FILE.write_text(str(chosen), encoding="utf-8")
    with (root / "desktop-launch.log").open("ab") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "agent_hub", "--root", str(root), "dashboard", "--port", str(chosen), "--no-open"],
            cwd=str(server),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    for _ in range(40):
        if proc.poll() is not None:
            raise SystemExit("Service exited; check desktop-launch.log")
        if matching_server(root, chosen):
            break
        time.sleep(0.2)
    else:
        raise SystemExit("Service readiness timed out")
