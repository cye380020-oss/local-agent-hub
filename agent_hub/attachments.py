"""Safe task input attachments: file uploads, stdlib Office/text extraction, and local folder snapshots."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, List, Optional, Tuple


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_MOUNT_FILES = 50
MAX_MOUNT_TOTAL_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_CHARS_PER_FILE = 24000
MAX_EXTRACTED_TOTAL_CHARS = 80000

TEXT_EXTS = {
    ".md", ".txt", ".json", ".csv", ".tsv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".css", ".yml", ".yaml", ".xml", ".sql", ".sh", ".toml", ".ini", ".log", ".m", ".h", ".c", ".swift",
}
SKIP_DIR_NAMES = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
                  ".api-secrets", ".trash", ".ssh", ".aws", ".gnupg", ".kube", ".docker",
                  ".azure", ".config", ".codex", ".console-auth"}
SENSITIVE_NAME_RE = re.compile(
    r"(?i)(^\.env(?:\.|$)|credential|secret|(?:^|[._-])tokens?(?:[._-]|$)|auth\.json$|"
    r"id_(?:rsa|ecdsa|ed25519|dsa)|\.(?:pem|p12|pfx|key|keychain-db)$|"
    r"^\.(?:netrc|npmrc|pypirc|git-credentials)$|^application_default_credentials)"
)
SENSITIVE_CONTENT = re.compile(
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:ghp_|github_pat_|sk-(?:proj-|ant-)?)[A-Za-z0-9_-]{20,}"
    rb"|(?:AKIA|ASIA)[A-Z0-9]{16}|(?im:^[ \t]*[\"']?(?:api[_-]?key|access[_-]?token|"
    rb"client_secret|password|aws_secret_access_key)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=_.-]{8,})"
)


def safe_filename(name: str) -> str:
    base = Path(str(name or "file")).name.strip()
    base = re.sub(r"[\x00-\x1f/\\]+", "_", base)
    if not base or base in (".", ".."):
        base = "attachment"
    return base[:160]


def _extract_docx(path: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "word/document.xml" not in zf.namelist():
                return None
            root = ET.fromstring(zf.read("word/document.xml"))
        paras = []
        for p in root.iter():
            if p.tag.endswith("}p") or p.tag == "p":
                texts = [t.text for t in p.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text]
                line = "".join(texts).strip()
                if line:
                    paras.append(line)
        return "\n".join(paras) if paras else None
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError):
        return None


def _extract_xlsx(path: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            shared: List[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.iter():
                    if si.tag.endswith("}si") or si.tag == "si":
                        shared.append("".join(t.text or "" for t in si.iter() if t.tag.endswith("}t") or t.tag == "t"))
            sheets = sorted(n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
            parts: List[str] = []
            for sname in sheets[:6]:
                sroot = ET.fromstring(zf.read(sname))
                rows: List[str] = []
                for row in sroot.iter():
                    if not (row.tag.endswith("}row") or row.tag == "row"):
                        continue
                    cells: List[str] = []
                    for c in list(row):
                        ctype = c.attrib.get("t", "")
                        val = ""
                        for child in list(c):
                            if child.tag.endswith("}v") and child.text is not None:
                                if ctype == "s":
                                    idx = int(child.text)
                                    val = shared[idx] if 0 <= idx < len(shared) else child.text
                                else:
                                    val = child.text
                            elif child.tag.endswith("}is"):
                                val = "".join(t.text or "" for t in child.iter() if t.tag.endswith("}t"))
                        cells.append(val.replace("\t", " ").replace("\n", " "))
                    if any(x.strip() for x in cells):
                        rows.append("\t".join(cells))
                    if len(rows) >= 200:
                        rows.append("...(表格行数已截断)")
                        break
                if rows:
                    parts.append("### %s\n%s" % (Path(sname).stem, "\n".join(rows)))
            return "\n\n".join(parts) if parts else None
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError, KeyError):
        return None


def _extract_pptx(path: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            slides = sorted(n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
            parts: List[str] = []
            for sname in slides[:30]:
                root = ET.fromstring(zf.read(sname))
                texts = [t.text.strip() for t in root.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text and t.text.strip()]
                if texts:
                    parts.append("### %s\n%s" % (Path(sname).stem, "\n".join(texts)))
            return "\n\n".join(parts) if parts else None
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError):
        return None


def extract_text_preview(path: Path, max_chars: int = MAX_EXTRACTED_CHARS_PER_FILE) -> Optional[str]:
    suffix = path.suffix.lower()
    text: Optional[str] = None
    if suffix in TEXT_EXTS:
        if path.stat().st_size <= 2 * 1024 * 1024:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    elif suffix == ".docx":
        text = _extract_docx(path)
    elif suffix == ".xlsx":
        text = _extract_xlsx(path)
    elif suffix == ".pptx":
        text = _extract_pptx(path)
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(内容已截断)"
    return text


def save_upload(store, filename: str, raw_bytes: bytes) -> Dict[str, Any]:
    from .core import dump, now

    if not isinstance(raw_bytes, (bytes, bytearray)) or len(raw_bytes) == 0:
        raise ValueError("上传文件为空")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("单个上传文件不能超过 20 MiB")
    uid = "up-" + uuid.uuid4().hex[:12]
    safe_name = safe_filename(filename)
    if SENSITIVE_NAME_RE.search(safe_name) or SENSITIVE_CONTENT.search(raw_bytes):
        raise ValueError("请将疑似凭据移除后再上传")
    folder = store.root / "uploads" / uid
    folder.mkdir(parents=True, exist_ok=True)
    payload = folder / "data"
    payload.mkdir()
    dest = payload / safe_name
    dest.write_bytes(raw_bytes)
    preview = extract_text_preview(dest, max_chars=4000)
    meta = {
        "id": uid,
        "name": safe_name,
        "bytes": len(raw_bytes),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "uploaded_at": now(),
        "has_text_preview": bool(preview),
        "preview_excerpt": (preview[:300] if preview else ""),
    }
    (folder / "meta.json").write_text(dump(meta), encoding="utf-8")
    return meta


def list_uploads(store) -> List[Dict[str, Any]]:
    root = store.root / "uploads"
    if not root.is_dir():
        return []
    items = []
    for sub in sorted(root.iterdir(), reverse=True):
        meta_path = sub / "meta.json"
        if meta_path.is_file():
            try:
                items.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return items[:40]


def recipient(agent):
    """Display the destination and bind consent to its routing configuration."""
    model = agent.get("execution_model") or agent.get("model")
    if isinstance(model, dict):
        model = model.get("value")
    fields = ("id", "transport", "endpoint", "protocol", "dsh_provider", "executable",
              "app_path", "desktop_bundle_id", "cli_args", "dsh_api_key_env", "use_dsh_credentials")
    routing = {key: agent.get(key) for key in fields}
    routing["model"] = model
    signature = hashlib.sha256(json.dumps(routing, sort_keys=True).encode()).hexdigest()
    dest = agent.get("endpoint") or agent.get("app_path") or agent.get("executable") or ""
    return {"id": agent["id"], "name": agent.get("name") or agent["id"], "transport": agent.get("transport"),
            "destination": dest, "model": model, "provider": agent.get("dsh_provider"), "signature": signature}


def snapshot_inputs(store, upload_ids, local_paths):
    """Read bounded input bytes once so approval covers the exact bytes copied."""
    from .core import identifier

    files = {}
    total = 0

    def add(path, rel, source):
        nonlocal total
        if len(files) >= MAX_MOUNT_FILES:
            raise ValueError("一次最多选择 50 个文件，请缩小目录范围")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("附件必须为普通文件")
            raw = stream.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES or total + len(raw) > MAX_MOUNT_TOTAL_BYTES:
            raise ValueError("附件超过单文件 20 MiB 或总计 50 MiB 上限")
        if SENSITIVE_CONTENT.search(raw):
            if source == "local_dir":
                return
            raise ValueError("附件含疑似凭据，请脱敏后重选：" + path.name)
        original = Path(rel)
        suffix = 1
        while rel in files:
            suffix += 1
            rel = original.with_name(original.stem + "-" + str(suffix) + original.suffix).as_posix()
        files[rel] = (raw, source)
        total += len(raw)

    for uid in upload_ids or []:
        folder = store.root / "uploads" / identifier(uid)
        name = safe_filename(json.loads((folder / "meta.json").read_text())["name"])
        if SENSITIVE_NAME_RE.search(name):
            raise ValueError("拒绝共享疑似凭据文件：" + name)
        payload = folder / "data" / name if (folder / "data").is_dir() else folder / name
        add(payload, "uploaded/" + name, "upload:" + uid)

    for raw_path in local_paths or []:
        p = Path(raw_path).expanduser()
        if not p.is_absolute() or p.is_symlink() or not p.exists():
            raise ValueError("请提供真实存在、非符号链接的绝对路径")
        # Resolve platform aliases (e.g. /var); check both names and resolved ancestors.
        for parent in p.parents:
            if parent.is_symlink() and str(parent) not in {"/var", "/tmp"}:
                raise ValueError("拒绝挂载符号链接目录中的文件")
        resolved = p.resolve()
        if any(part.lower() in SKIP_DIR_NAMES for part in (*p.parts, *resolved.parts)) or SENSITIVE_NAME_RE.search(p.name):
            raise ValueError("拒绝挂载凭据或受保护目录：" + p.name)
        if resolved == store.root or store.root in resolved.parents:
            raise ValueError("不能挂载工作台运行数据")
        if resolved.is_file():
            add(resolved, "mounted/" + safe_filename(p.name), "local_file")
        elif resolved.is_dir():
            for base, dirs, names in os.walk(resolved, followlinks=False):
                parent = Path(base)
                dirs[:] = [d for d in sorted(dirs) if d.lower() not in SKIP_DIR_NAMES
                           and not (parent / d).is_symlink() and parent / d != store.root]
                for name in sorted(names):
                    fp = parent / name
                    if SENSITIVE_NAME_RE.search(name) or fp.is_symlink() or not fp.is_file():
                        continue
                    if 0 < fp.stat().st_size <= MAX_UPLOAD_BYTES:
                        rel = "/".join(safe_filename(x) for x in fp.relative_to(resolved).parts)
                        add(fp, "mounted/" + safe_filename(p.name) + "/" + rel, "local_dir")
    if not files:
        raise ValueError("没有可共享的文件，请检查所选路径及凭据过滤规则")
    return files


def review_inputs(store, upload_ids, local_paths, recipients):
    agents = {a["id"]: a for a in store.agents()}
    if not recipients or any(a not in agents for a in recipients):
        raise ValueError("请先选择具体的附件接收者")
    files = snapshot_inputs(store, upload_ids, local_paths)
    review = {"files": {rel: hashlib.sha256(raw).hexdigest() for rel, (raw, _) in files.items()},
              "recipients": [recipient(agents[aid]) for aid in sorted(set(recipients))]}
    return review, files


def approved_inputs(store, data, recipients):
    if not (data.get("upload_ids") or data.get("local_paths")):
        return None
    approval = data.get("attachment_approval")
    if not isinstance(approval, dict) or not approval.get("files"):
        raise ValueError("发送附件前，请确认文件清单和接收者")
    review, files = review_inputs(store, data.get("upload_ids"), data.get("local_paths"), recipients)
    selected = approval["files"]
    if (not isinstance(selected, dict) or approval.get("recipients") != review["recipients"]
            or any(rel not in files or review["files"][rel] != digest for rel, digest in selected.items())):
        raise ValueError("附件内容或接收者已变化，请重新确认")
    return {rel: files[rel] for rel in selected}, {r["id"]: r["signature"] for r in review["recipients"]}


def attach_job_inputs(store, job, bundle):
    from .core import identifier, inside, now

    files, recipients = bundle
    folder = store.root / "jobs" / identifier(job) / "attachments"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".manifest.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = job_attachments_manifest(store, job)
        for rel, (raw, source) in files.items():
            target = inside(folder, rel)
            if target.exists():
                target = target.with_name(target.stem + "-" + uuid.uuid4().hex + target.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            manifest.append({"rel_path": target.relative_to(folder).as_posix(), "name": target.name,
                             "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                             "source": source, "attached_at": now(), "recipients": recipients,
                             "text_extractable": extract_text_preview(target, max_chars=200) is not None})
        write_manifest(folder, manifest)
    store.event(job, None, "job.attachments_added", {"count": len(files)})
    return manifest


def write_manifest(folder, manifest):
    from .core import dump

    with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False, encoding="utf-8") as out:
        out.write(dump(manifest))
    os.replace(out.name, folder / "manifest.json")


def job_attachments_manifest(store, job: str) -> List[Dict[str, Any]]:
    manifest_path = store.root / "jobs" / job / "attachments" / "manifest.json"
    if not manifest_path.is_file():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("附件授权记录损坏，请检查工作区")
    return data


def copy_job_attachments_to_workspace(store, job: str, workspace: Path, agent) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Copy verified job attachments into workspace/inputs/attachments/ and return (manifest, extracted_previews)."""
    from .core import inside

    manifest = job_attachments_manifest(store, job)
    if not manifest and (store.root / "jobs" / job / "attachments").exists():
        raise ValueError("附件授权记录为空，停止派发，请在新任务中重新选择资料")
    if not manifest:
        return [], {}
    if not authorized_for(manifest, agent):
        raise ValueError("当前接收者缺少附件授权或配置已变化，请重新选择接收者并确认附件")
    src_root = store.root / "jobs" / job / "attachments"
    previews: Dict[str, str] = {}
    remaining = MAX_EXTRACTED_TOTAL_CHARS
    verified_entries: List[Dict[str, Any]] = []
    for item in manifest:
        rel = item["rel_path"]
        src = inside(src_root, rel)
        raw = src.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("任务附件内容发生篡改：" + rel)
        target = inside(workspace, "inputs/attachments/" + rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        verified_entries.append({
            "path": "inputs/attachments/" + rel,
            "name": item["name"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        })
        if remaining > 0:
            extracted = extract_text_preview(target, max_chars=min(MAX_EXTRACTED_CHARS_PER_FILE, remaining))
            if extracted:
                previews["inputs/attachments/" + rel] = extracted
                remaining -= len(extracted)
    return verified_entries, previews


def authorized_for(manifest, agent):
    if not manifest:
        return True
    if not agent:
        return False
    sig = recipient(agent)["signature"]
    return all(item.get("recipients", {}).get(agent["id"]) == sig for item in manifest)


def review_saved_inputs(store, job, approval=None):
    """Older jobs require explicit consent before their next dispatch."""
    from .core import identifier, inside
    from .workflow import read_flow

    folder = store.root / "jobs" / identifier(job) / "attachments"
    if not folder.is_dir():
        raise ValueError("此任务没有已保存的附件")
    flow = read_flow(store, job)
    if flow:
        ids = flow[0]["participants"]
    else:
        ids = [json.loads(row["spec"]).get("agent") or row["agent"] for row in store.task_rows(job)]
    agents = {a["id"]: a for a in store.agents()}
    if not ids or any(aid not in agents for aid in ids):
        raise ValueError("旧任务缺少确定接收者，请在新任务中选择接收者并重新附加文件")
    recipients = [recipient(agents[aid]) for aid in sorted(set(ids))]
    with (folder / ".manifest.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = job_attachments_manifest(store, job)
        if not manifest or all(item.get("recipients") for item in manifest):
            raise ValueError("此任务没有待补充确认的历史附件")
        files = {}
        for item in manifest:
            rel = item["rel_path"]
            path = inside(folder, rel)
            if (SENSITIVE_NAME_RE.search(path.name) or any(part.lower() in SKIP_DIR_NAMES for part in Path(rel).parts)
                    or path.stat().st_size > MAX_UPLOAD_BYTES):
                raise ValueError("历史附件含受限文件，请在新任务中重新选择")
            raw = path.read_bytes()
            if SENSITIVE_CONTENT.search(raw) or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("历史附件需重新选择或脱敏，请新建任务")
            files[rel] = item["sha256"]
        review = {"files": files, "recipients": recipients}
        if approval is not None:
            if approval != review:
                raise ValueError("历史步骤可能已引用全部附件。要排除文件，请创建新任务")
            for item in manifest:
                if not item.get("recipients"):
                    item["recipients"] = {r["id"]: r["signature"] for r in recipients}
            write_manifest(folder, manifest)
        return review
