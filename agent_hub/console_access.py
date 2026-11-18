"""Per-process, user-private credentials for the loopback dashboard."""
import hashlib
import http.client
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile

from . import __version__


def entry_path(root, port):
    return Path(root) / ".console-auth" / ("%d.html" % port)


def private_write(path, content):
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def publish_access(root, port, token):
    path = entry_path(root, port)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid():
        raise ValueError("控制台凭证目录归属无效")
    path.parent.chmod(0o700)
    private_write(path.with_suffix(".key"), token)
    url = "http://127.0.0.1:%d/#%s" % (port, token)
    private_write(path, '<!doctype html><meta name="referrer" content="no-referrer">'
                  '<script>location.replace(%s)</script>' % json.dumps(url))
    return path


def matching_server(root, port):
    """Prove that the process using this port owns this workspace's private key."""
    try:
        token = entry_path(root, port).with_suffix(".key").read_bytes()
        challenge = secrets.token_hex(16)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            conn.request("GET", "/api/health?challenge=" + challenge)
            data = json.loads(conn.getresponse().read(4096))
        finally:
            conn.close()
        proof = hmac.new(token, challenge.encode(), hashlib.sha256).hexdigest()
        return data.get("version") == __version__ and hmac.compare_digest(proof, data.get("proof", ""))
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return False


def check_for_legacy_dashboard(root, ports):
    """An older daemon sharing this workspace could dispatch without attachment consent."""
    for port in ports:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        try:
            conn.request("GET", "/api/health")
            response = conn.getresponse()
            if response.status != 200:
                continue
            health = json.loads(response.read(4096))
        except (OSError, ValueError, http.client.HTTPException):
            continue
        finally:
            conn.close()
        if isinstance(health, dict) and health.get("app") == "agent_hub" and health.get("root") == str(root):
            raise ValueError("此工作区的旧版后台仍在运行，请先结束旧服务后重新启动")
