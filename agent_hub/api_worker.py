"""Bounded text-only HTTP inference and model catalog sync. No tools and no redirect credential forwarding."""
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _gemini_generate_url(endpoint, model):
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", model):
        raise ValueError("Gemini 模型标识格式不正确")
    safe_model = urllib.parse.quote(model, safe="")
    if "{model}" in endpoint:
        return endpoint.replace("{model}", safe_model)
    if ":generateContent" in endpoint:
        return endpoint
    base = endpoint.rstrip("/")
    if base.endswith("/models"):
        return base + "/" + safe_model + ":generateContent"
    if base.endswith("/v1beta") or base.endswith("/v1"):
        return base + "/models/" + safe_model + ":generateContent"
    return base + "/v1beta/models/" + safe_model + ":generateContent"


def _models_url(endpoint, protocol):
    u = urllib.parse.urlsplit(endpoint)
    path = u.path.rstrip("/")
    if protocol == "openai":
        if path.endswith("/chat/completions"):
            path = path[: -len("/chat/completions")] + "/models"
        elif not path.endswith("/models"):
            path = (path or "/v1") + "/models"
    elif protocol == "anthropic":
        if path.endswith("/messages"):
            path = path[: -len("/messages")] + "/models"
        elif not path.endswith("/models"):
            path = (path or "/v1") + "/models"
    elif protocol == "gemini":
        idx = path.find("/models")
        if idx >= 0:
            path = path[: idx + len("/models")]
        elif path.endswith("/v1beta") or path.endswith("/v1"):
            path = path + "/models"
        else:
            path = (path or "/v1beta") + "/models"
    else:
        raise ValueError("不支持的 API 协议")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, path, "", ""))


def infer(agent, key, prompt):
    protocol = agent.get("protocol", "openai")
    model_id = agent["model"]["value"]
    headers = {"Content-Type": "application/json"}
    if protocol == "anthropic":
        url = agent["endpoint"]
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(agent.get("max_tokens") or 4096),
            "stream": False,
        }
    elif protocol == "gemini":
        url = _gemini_generate_url(agent["endpoint"], model_id)
        if key:
            headers["x-goog-api-key"] = key
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
    else:
        url = agent["endpoint"]
        if key:
            headers["Authorization"] = "Bearer " + key
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }

    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=120) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("API 回复超过 2 MiB")
        value = json.loads(raw)
        usage = None
        if protocol == "anthropic":
            output = "\n".join(x["text"] for x in value["content"] if x.get("type") == "text")
            truncated = value.get("stop_reason") == "max_tokens"
            model = value.get("model")
            u = value.get("usage")
            if isinstance(u, dict):
                inp, out = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
                usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
        elif protocol == "gemini":
            candidates = value.get("candidates") or []
            if not candidates:
                raise ValueError("Gemini API 未返回候选结果")
            first = candidates[0]
            parts = first.get("content", {}).get("parts", [])
            output = "\n".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
            truncated = first.get("finishReason") == "MAX_TOKENS"
            model = value.get("modelVersion")
            u = value.get("usageMetadata")
            if isinstance(u, dict):
                inp = int(u.get("promptTokenCount") or 0)
                out = int(u.get("candidatesTokenCount") or 0)
                tot = int(u.get("totalTokenCount") or (inp + out))
                usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": tot}
        else:
            choice = value["choices"][0]
            output = choice["message"]["content"]
            truncated = choice.get("finish_reason") == "length"
            model = value.get("model")
            u = value.get("usage")
            if isinstance(u, dict):
                inp = int(u.get("prompt_tokens") or 0)
                out = int(u.get("completion_tokens") or 0)
                tot = int(u.get("total_tokens") or (inp + out))
                usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": tot}
        if not isinstance(output, str) or not output.strip():
            raise ValueError("API 没有返回文字成果")
        if truncated:
            raise ValueError("API 回复被长度限制截断，请缩小任务或调整服务设置")
        if key:
            output = output.replace(key, "[REDACTED]")
        if not isinstance(model, str):
            model = None
        if key and model:
            model = model.replace(key, "[REDACTED]")
        res = {
            "status": "completed",
            "output": output,
            "artifacts": [],
            "model": {
                "value": model,
                "source": "api_response_label",
                "confidence": "provider_reported",
            },
        }
        if usage:
            res["usage"] = usage
        return res
    except urllib.error.HTTPError as exc:
        return {
            "status": "failed",
            "output": "",
            "error": "API HTTP %d；请检查地址、密钥、模型和额度" % exc.code,
        }
    except (urllib.error.URLError, TimeoutError, OSError):
        return {
            "status": "failed",
            "output": "",
            "error": "API 连接失败或超时，请检查服务地址和网络",
        }
    except (ValueError, KeyError, IndexError, TypeError):
        return {
            "status": "failed",
            "output": "",
            "error": "API 返回格式不兼容、输出为空、超限或被截断",
        }


def list_models(agent, key):
    protocol = agent.get("protocol", "openai")
    headers = {"Accept": "application/json"}
    if protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif protocol == "gemini":
        if key:
            headers["x-goog-api-key"] = key
    else:
        if key:
            headers["Authorization"] = "Bearer " + key
    url = _models_url(agent["endpoint"], protocol)
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("模型列表响应超过 2 MiB")
        payload = json.loads(raw)
        models = []
        if protocol == "gemini":
            for item in payload.get("models", []):
                if not isinstance(item, dict):
                    continue
                methods = item.get("supportedGenerationMethods") or []
                if methods and "generateContent" not in methods:
                    continue
                name = str(item.get("name", ""))
                if name.startswith("models/"):
                    name = name[len("models/") :]
                if name:
                    models.append(name)
        else:
            items = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(items, list) and isinstance(payload, dict):
                items = payload.get("models")
            for item in items or []:
                mid = item.get("id") if isinstance(item, dict) else (item if isinstance(item, str) else None)
                if isinstance(mid, str) and mid.strip():
                    models.append(mid.strip())
        cleaned = []
        seen = set()
        for mid in models:
            if key and len(key) >= 4:
                mid = mid.replace(key, "[REDACTED]")
            if re.fullmatch(r"[A-Za-z0-9._:/@-]{1,160}", mid) and mid not in seen:
                seen.add(mid)
                cleaned.append(mid)
        cleaned.sort()
        return {"status": "completed", "models": cleaned[:200], "source_url": url}
    except urllib.error.HTTPError as exc:
        return {"status": "failed", "models": [], "error": "拉取模型列表失败（HTTP %d）" % exc.code}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"status": "failed", "models": [], "error": "拉取模型列表超时或连接失败"}
    except (ValueError, KeyError, TypeError):
        return {"status": "failed", "models": [], "error": "模型列表响应格式无法解析"}


def main():
    args = list(sys.argv[1:])
    mode = "infer"
    if args and args[0] == "--list-models":
        mode = "list_models"
        args.pop(0)
    root, aid = Path(args[0]), args[1]
    # Import from installed package; credentials never travel through argv or task files.
    from agent_hub.core import Store
    from agent_hub.management import secret_path

    store = Store(root)
    agent = next(a for a in store.agents() if a["id"] == aid and a["transport"] == "http_api")
    key_file = secret_path(store, aid)
    key = json.loads(key_file.read_text())["key"] if agent.get("has_key") else ""
    if mode == "list_models":
        print(json.dumps(list_models(agent, key), ensure_ascii=False))
        return
    prompt = sys.stdin.read(1024 * 1024 + 1)
    if len(prompt.encode()) > 1024 * 1024:
        raise ValueError("Prompt too large")
    print(json.dumps(infer(agent, key, prompt), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "output": "", "error": "API 配置或密钥无法读取，请重新保存设置"},
                ensure_ascii=False,
            )
        )
