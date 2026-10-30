"""第三方出图：OpenAI 兼容的 /images/generations。一次调用只出一张。

配置来自 generate_image 工具包的 config（tools.json 或 MAS_IMAGE_API_* 环境变量）：
remote_base_url、remote_api_key、remote_model、remote_timeout_sec。
密钥只放进 Authorization 头，不进错误信息、事件或工件记录。
发出前后经过出站钩子（services.gateway.egress，渠道 remote_image）：记 planned / sent / failed，不记提示词和密钥（#57）。
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import urllib.error
import urllib.request

from services.gateway import egress

TOOL = "generate_image"
SIZES = {"1:1": (1024, 1024), "16:9": (1536, 1024), "9:16": (1024, 1536)}
LABEL = "第三方图片 API"


def settings() -> dict:
    from services.tools import packs

    pack = packs.find(TOOL)
    return packs.config_for(pack) if pack else {}


def _error(code: str, message: str, hint: str = "") -> dict:
    error = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    return {"ok": False, "tool": TOOL, "error": error, "backend": "remote"}


def _scrub(text: str, key: str) -> str:
    text = str(text or "")[:300]
    return text.replace(key, "***") if key else text


def check_ready(config: dict | None = None) -> dict | None:
    config = settings() if config is None else config
    if not str(config.get("remote_base_url") or "").strip():
        return _error(
            "remote_not_configured", "第三方出图还没配置地址。",
            "在 设置 → 工具 → generate_image 填 remote_base_url（例如 https://api.openai.com/v1），或设环境变量 MAS_IMAGE_API_BASE。",
        )
    if not str(config.get("remote_api_key") or "").strip():
        return _error(
            "remote_missing_key", "第三方出图缺少 API key。",
            "在 设置 → 工具 → generate_image 填 remote_api_key，或设环境变量 MAS_IMAGE_API_KEY；也可以改用本地 ComfyUI 出图。",
        )
    return None


def generate(prompt: str, aspect: str, config: dict | None = None) -> dict:
    import artifacts
    import comfy_poc

    config = settings() if config is None else config
    problem = check_ready(config)
    if problem:
        return problem
    base = str(config["remote_base_url"]).strip().rstrip("/")
    key = str(config["remote_api_key"]).strip()
    model = str(config.get("remote_model") or "gpt-image-1")
    timeout = float(config.get("remote_timeout_sec") or 120)
    width, height = SIZES.get(aspect, SIZES["1:1"])
    outgoing = {"model": model, "prompt": prompt, "n": 1, "size": f"{width}x{height}"}
    body = json.dumps(outgoing).encode("utf-8")
    ticket = egress.guard("remote_image", base, outgoing, body=body, exempt=())
    if ticket.hit:  # 出站策略拦下（#59）：字段不在策略里或一次要多张图，不打开连接
        return _error(ticket.reason_code or "egress_policy_denied", "出站策略拦下了这次第三方出图请求，没有发出。",
                      "一次 generate_image 只出一张图；字段要先登记到 EGRESS_POLICIES。")
    request = urllib.request.Request(
        base + "/images/generations", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ticket.sent(http_status=getattr(response, "status", None))
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        ticket.failed("http_error", http_status=exc.code)
        try:
            detail = exc.read().decode("utf-8", "replace")
        except OSError:
            detail = ""
        if exc.code in (401, 403):
            return _error("remote_auth", f"第三方出图服务拒绝了密钥（HTTP {exc.code}）。",
                          "检查 remote_api_key 是否正确、是否有出图权限。")
        return _error("remote_error", f"第三方出图服务返回 HTTP {exc.code}：{_scrub(detail, key)}",
                      "检查 remote_model 和账户额度；也可以改用本地 ComfyUI 出图。")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        ticket.failed("connect_error")
        reason = getattr(exc, "reason", None) or exc
        return _error("remote_unreachable", f"连不上第三方出图服务（{base}）：{_scrub(reason, key)}",
                      "检查 remote_base_url、网络或代理；也可以改用本地 ComfyUI 出图。")
    data = None
    items = payload.get("data") if isinstance(payload, dict) else None
    first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    try:
        if first.get("b64_json"):
            data = base64.b64decode(first["b64_json"])
        elif first.get("url"):
            with urllib.request.urlopen(str(first["url"]), timeout=timeout) as response:
                data = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _error("remote_error", f"取第三方生成的图片失败：{_scrub(exc, key)}")
    if not data:
        return _error("remote_error", "第三方出图服务没有返回图片。", "检查 remote_model 是否是出图模型。")
    try:
        comfy = comfy_poc.load_config()
    except comfy_poc.ComfyFailure:
        comfy = {}
    comfy = {**comfy, "image": {"workflow": f"remote:{model}"}}
    handle, path = tempfile.mkstemp(suffix=".png", prefix="remote-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        records = artifacts.remember_outputs(
            comfy, [path], kind="image", user_prompt=prompt, workflow_prompt=prompt,
            seed=0, width=width, height=height,
        )
    except artifacts.ArtifactError as exc:
        return _error("artifact_error", exc.message)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    result = {"ok": True, "tool": TOOL}
    result.update(records[0])
    result.update({"aspect_ratio": aspect, "backend": "remote", "model": model})
    return result
