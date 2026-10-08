"""用一条提示词调用本地 ComfyUI，生成一张图片或一段视频。

地址、工作流路径和字段对照都来自配置文件或环境变量。
换模板时改 JSON 和 *.fields.json，不要改这个脚本。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASPECTS = ("1:1", "16:9", "9:16")
_talk = True


class ComfyFailure(Exception):
    def __init__(self, message: str, payload=None):
        super().__init__(message)
        self.message = message
        self.payload = payload


class talk_off:
    """工具调用时不把节点和 workflow 打到标准输出。"""

    def __enter__(self):
        global _talk
        self._previous = _talk
        _talk = False
        return self

    def __exit__(self, exc_type, exc, tb):
        global _talk
        _talk = self._previous
        return False


def note(message: str) -> None:
    if _talk:
        print(message, flush=True)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def fail(message: str, payload=None) -> None:
    raise ComfyFailure(message, payload)


def load_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(base: str, value: str) -> str:
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(base, value))


def load_config() -> dict:
    path = os.environ.get("COMFY_CONFIG")
    if not path:
        path = os.path.join(SCRIPT_DIR, "config.json")
    if not os.path.isfile(path):
        fail(
            "找不到配置文件。把 config.example.json 复制为 config.json，"
            "或设置 COMFY_CONFIG。"
        )
    config = load_json(path)
    base = os.path.dirname(os.path.abspath(path))
    config["_base"] = base
    if os.environ.get("COMFY_URL"):
        config["comfy_url"] = os.environ["COMFY_URL"]
    if os.environ.get("COMFY_OUTPUT_DIR"):
        config["output_dir"] = os.environ["COMFY_OUTPUT_DIR"]
    for key, env_name in (
        ("image", "COMFY_IMAGE_WORKFLOW"),
        ("video", "COMFY_VIDEO_WORKFLOW"),
    ):
        override = os.environ.get(env_name)
        if override:
            config.setdefault(key, {})["workflow"] = override
    url = str(config.get("comfy_url") or "").rstrip("/")
    if not url:
        fail("配置里没有 comfy_url，也没有环境变量 COMFY_URL。")
    config["comfy_url"] = url
    output_dir = config.get("output_dir")
    if not output_dir:
        fail("配置里没有 output_dir，也没有环境变量 COMFY_OUTPUT_DIR。")
    config["output_dir"] = resolve_path(base, output_dir)
    return config


def job_paths(config: dict, kind: str) -> tuple[dict, dict, int]:
    section = config.get(kind) or {}
    workflow_path = section.get("workflow")
    fields_path = section.get("fields")
    if not workflow_path or not fields_path:
        fail(f"配置的 {kind} 需要 workflow 和 fields。")
    base = config["_base"]
    workflow_path = resolve_path(base, workflow_path)
    fields_path = resolve_path(base, fields_path)
    if not os.path.isfile(workflow_path):
        fail(f"工作流不存在：{workflow_path}")
    if not os.path.isfile(fields_path):
        fail(f"字段对照不存在：{fields_path}")
    timeout = int(section.get("timeout_sec") or (900 if kind == "image" else 2700))
    return load_json(workflow_path), load_json(fields_path), timeout


def targets(spec) -> list[dict]:
    if isinstance(spec, list):
        return spec
    return [spec]


def set_inputs(prompt: dict, spec, value) -> None:
    for target in targets(spec):
        node_id = str(target["node"])
        field = target["input"]
        node = prompt.get(node_id)
        if node is None:
            fail(f"工作流里没有节点 {node_id}，字段对照和模板不一致。")
        if "inputs" not in node:
            fail(f"节点 {node_id} 没有 inputs。")
        node["inputs"][field] = value


def snap_frames(raw: int, step: int, limit: int) -> int:
    if step < 1:
        fail("frame_step 必须大于 0。")
    if limit < 1:
        fail("max_frames 必须大于 0。")
    count = max(1, int(raw))
    steps = int(round((count - 1) / step))
    snapped = steps * step + 1
    max_steps = (limit - 1) // step
    capped = max_steps * step + 1
    if snapped > capped:
        snapped = capped
    return max(1, snapped)


def apply_common(prompt: dict, fields: dict, args) -> None:
    aspects = fields.get("aspects") or {}
    size = aspects.get(args.aspect)
    if not isinstance(size, dict):
        fail(f"字段对照里没有画幅 {args.aspect}。允许的值：{', '.join(ASPECTS)}。")
    set_inputs(prompt, fields["positive_prompt"], args.prompt)
    if args.negative is not None:
        if "negative_prompt" not in fields:
            fail("这张工作流的字段对照没有 negative_prompt。")
        set_inputs(prompt, fields["negative_prompt"], args.negative)
    set_inputs(prompt, fields["width"], int(size["width"]))
    set_inputs(prompt, fields["height"], int(size["height"]))
    seed = args.seed if args.seed is not None else random.randrange(0, 2**32)
    if seed < 0:
        fail("seed 不能是负数。")
    set_inputs(prompt, fields["seed"], int(seed))
    positive = targets(fields["positive_prompt"])[0]
    written = prompt[str(positive["node"])]["inputs"][positive["input"]]
    if written != args.prompt:
        fail("替换后的正向提示词和输入不一致。", {"node": positive, "text": written})
    note(f"正向提示词写入节点 {positive['node']} 的 {positive['input']}：{written}")
    note(f"画幅 {args.aspect} → {int(size['width'])}x{int(size['height'])}，seed {seed}")


def apply_frames(prompt: dict, fields: dict, args) -> None:
    if "frames" not in fields:
        fail("视频字段对照里没有 frames。")
    step = int(fields.get("frame_step") or 4)
    limit = int(fields.get("max_frames") or 33)
    fps = float(fields.get("fps") or 16)
    if args.frames is not None:
        requested = args.frames
    elif args.seconds is not None:
        requested = int(round(args.seconds * fps))
    else:
        requested = 17
    if requested < 1:
        fail("帧数必须大于 0。")
    length = snap_frames(requested, step, limit)
    set_inputs(prompt, fields["frames"], length)
    note(f"帧数 {length}（请求 {requested}，上限 {limit}，步长 {step}，fps {fps:g}）")


def apply_reference(prompt: dict, fields: dict, uploaded_name: str | None) -> None:
    if not uploaded_name:
        return
    spec = fields.get("reference_image")
    if not isinstance(spec, dict):
        fail("视频字段对照里没有 reference_image，不能提交参考图。")
    node_id = str(spec["node"])
    prompt[node_id] = {
        "class_type": spec.get("class_type") or "LoadImage",
        "inputs": {spec["input"]: uploaded_name},
    }
    link = spec.get("link_to") or {}
    set_inputs(
        prompt,
        {"node": link["node"], "input": link["input"]},
        [node_id, int(link.get("output") or 0)],
    )
    note(f"参考图写入节点 {node_id} 的 {spec['input']}：{uploaded_name}")


def request_json(url: str, payload: dict | None = None, timeout: int = 120):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        fail(f"ComfyUI HTTP {exc.code} {url}", parsed)
    except urllib.error.URLError as exc:
        fail(f"连不上 ComfyUI：{url} ({exc.reason})")
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        fail(f"ComfyUI 返回的不是 JSON：{url}", body[:2000])


def upload_image(base: str, path: str) -> str:
    if not os.path.isfile(path):
        fail(f"参考图不存在：{path}")
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    boundary = "----ComfyPoc" + uuid.uuid4().hex
    with open(path, "rb") as handle:
        file_bytes = handle.read()

    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    file_head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    body = b"".join(
        [
            field("overwrite", "true"),
            field("type", "input"),
            file_head,
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    req = urllib.request.Request(
        base + "/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        fail(f"上传参考图失败，HTTP {exc.code}", raw)
    except urllib.error.URLError as exc:
        fail(f"上传参考图失败：{exc.reason}")
    name = payload.get("name")
    if not name:
        fail("上传参考图后 ComfyUI 没有返回文件名。", payload)
    subfolder = payload.get("subfolder") or ""
    if subfolder:
        return subfolder.replace("\\", "/") + "/" + name
    return name


def submit_prompt(base: str, prompt: dict, client_id: str) -> str:
    result = request_json(base + "/prompt", {"prompt": prompt, "client_id": client_id})
    node_errors = result.get("node_errors") or {}
    if node_errors or not result.get("prompt_id"):
        fail("ComfyUI 拒绝了这张工作流。", result)
    return result["prompt_id"]


def history_entry(base: str, prompt_id: str):
    payload = request_json(f"{base}/history/{urllib.parse.quote(prompt_id)}")
    if not isinstance(payload, dict):
        return None
    entry = payload.get(prompt_id)
    if isinstance(entry, dict):
        return entry
    return None


def status_of(entry: dict) -> tuple[str, bool]:
    status = entry.get("status") or {}
    return str(status.get("status_str") or ""), bool(status.get("completed"))


def output_files(entry: dict) -> list[dict]:
    found = []
    outputs = entry.get("outputs") or {}
    if not isinstance(outputs, dict):
        return found
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "gifs", "videos", "audio"):
            items = node_out.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("filename"):
                    found.append(item)
    return found


def download_file(base: str, item: dict, output_dir: str) -> str:
    filename = item["filename"]
    subfolder = item.get("subfolder") or ""
    folder_type = item.get("type") or "output"
    query = urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder, "type": folder_type}
    )
    url = f"{base}/view?{query}"
    relative = os.path.join(subfolder, filename) if subfolder else filename
    destination = os.path.join(output_dir, relative)
    os.makedirs(os.path.dirname(destination) or output_dir, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        fail(f"下载输出失败，HTTP {exc.code} {filename}", raw)
    except urllib.error.URLError as exc:
        fail(f"下载输出失败：{filename} ({exc.reason})")
    if not data:
        fail(f"下载到的文件是空的：{filename}")
    with open(destination, "wb") as handle:
        handle.write(data)
    return os.path.abspath(destination)


def ws_url(base: str, client_id: str) -> str:
    parsed = urllib.parse.urlparse(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunparse(
        (scheme, parsed.netloc, "/ws", "", urllib.parse.urlencode({"clientId": client_id}), "")
    )


def handle_ws_message(raw, prompt_id: str, prompt: dict) -> str | None:
    if isinstance(raw, bytes):
        return None
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None
    kind = message.get("type")
    data = message.get("data") or {}
    if not isinstance(data, dict):
        return None
    message_prompt = data.get("prompt_id")
    if message_prompt and message_prompt != prompt_id:
        return None
    if kind == "executing":
        node = data.get("node")
        if node is None:
            note("执行结束，正在取结果")
        else:
            class_type = (prompt.get(str(node)) or {}).get("class_type") or ""
            label = f"{node}（{class_type}）" if class_type else str(node)
            note(f"执行节点 {label}")
    elif kind == "progress":
        value = data.get("value")
        maximum = data.get("max") or 0
        node = data.get("node")
        if maximum:
            percent = int(round(100 * float(value) / float(maximum)))
            prefix = f"节点 {node} " if node else ""
            if _talk:
                print(f"进度 {prefix}{value}/{maximum} {percent}%", flush=True)
            else:
                print(f"进度 {percent}%", file=sys.stderr, flush=True)
    elif kind == "execution_error":
        fail("ComfyUI 执行失败。", data)
    elif kind == "execution_success":
        return "success"
    return None


def wait_for_result(base: str, prompt: dict, prompt_id: str, client_id: str, timeout: int) -> dict:
    ws = None
    try:
        from websocket import (  # type: ignore
            WebSocketConnectionClosedException,
            WebSocketTimeoutException,
            create_connection,
        )

        ws = create_connection(
            ws_url(base, client_id),
            timeout=10,
            header=[f"Origin: {base}"],
        )
        ws.settimeout(1)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"WebSocket 连不上，改为轮询 /history：{exc}", file=sys.stderr, flush=True)
        ws = None
        WebSocketTimeoutException = ()  # type: ignore
        WebSocketConnectionClosedException = ()  # type: ignore

    deadline = time.time() + timeout
    last_poll = 0.0
    entry = None
    try:
        while time.time() < deadline:
            if ws is not None:
                try:
                    signal = handle_ws_message(ws.recv(), prompt_id, prompt)
                    if signal == "success":
                        last_poll = 0.0
                except WebSocketTimeoutException:
                    pass
                except WebSocketConnectionClosedException:
                    print("WebSocket 已断开，改为轮询 /history", file=sys.stderr, flush=True)
                    ws = None
            now = time.time()
            if now - last_poll >= 2:
                last_poll = now
                entry = history_entry(base, prompt_id)
                if entry:
                    status_str, completed = status_of(entry)
                    if status_str == "error":
                        fail("ComfyUI 执行失败。", entry.get("status"))
                    if status_str == "success" or (completed and output_files(entry)):
                        if output_files(entry):
                            return entry
            if ws is None:
                time.sleep(1)
    finally:
        if ws is not None:
            ws.close()
    fail(
        f"等待超时（{timeout} 秒）。视频和图片使用各自的超时，这次没有拿到输出。",
        (entry or {}).get("status"),
    )


def confirm_history_prompt(entry: dict, prompt: dict, fields: dict) -> None:
    stored = entry.get("prompt")
    graph = stored[2] if isinstance(stored, list) and len(stored) >= 3 else None
    if not isinstance(graph, dict):
        note("历史记录里没有完整工作流，提交前已经核对过提示词。")
        return
    positive = targets(fields["positive_prompt"])[0]
    node_id = str(positive["node"])
    field = positive["input"]
    actual = ((graph.get(node_id) or {}).get("inputs") or {}).get(field)
    expected = prompt[node_id]["inputs"][field]
    if actual != expected:
        fail(
            "历史记录里的提示词和本次提交不一致。",
            {"node": node_id, "input": field, "expected": expected, "actual": actual},
        )
    note(f"已核对历史记录：节点 {node_id}.{field} 与本次提示词一致。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用一条提示词调用本地 ComfyUI，生成图片或视频。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--prompt", required=True, help="正向提示词，会写入 CLIP 文本节点。")
        command.add_argument("--negative", default=None, help="负向提示词。不传则保留模板里的默认值。")
        command.add_argument("--aspect", default="1:1", choices=ASPECTS, help="画幅。只允许这三项。")
        command.add_argument("--seed", type=int, default=None, help="随机种子。不传则本次随机生成。")
        command.add_argument("--print-prompt", action="store_true", help="只打印替换后的工作流，不提交。")

    image = subparsers.add_parser("image", help="文生图。")
    add_common(image)
    video = subparsers.add_parser("video", help="文生视频。传入 --image 时改为图生视频。")
    add_common(video)
    video.add_argument("--frames", type=int, default=None, help="帧数。会按字段对照里的步长对齐，并不超过上限。")
    video.add_argument("--seconds", type=float, default=None, help="时长（秒）。同时传了 --frames 时以帧数为准。")
    video.add_argument("--image", default=None, help="本地参考图。不传则不连接 LoadImage。")
    return parser


def execute_workflow(base: str, workflow: dict, fields: dict, timeout: int, output_dir: str) -> list[str]:
    client_id = str(uuid.uuid4())
    prompt_id = submit_prompt(base, workflow, client_id)
    stream = sys.stdout if _talk else sys.stderr
    print(f"prompt_id {prompt_id}", file=stream, flush=True)
    entry = wait_for_result(base, workflow, prompt_id, client_id, timeout)
    confirm_history_prompt(entry, workflow, fields)
    os.makedirs(output_dir, exist_ok=True)
    unique = []
    seen = set()
    for item in output_files(entry):
        key = (item.get("filename"), item.get("subfolder"), item.get("type"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    chosen = [item for item in unique if item.get("type") in (None, "output")]
    if not chosen:
        chosen = [item for item in unique if item.get("type") == "temp"]
    saved = [download_file(base, item, output_dir) for item in chosen]
    if not saved:
        fail("执行成功，但历史记录里没有可下载的文件。", entry.get("outputs"))
    return saved


def main(argv: list[str] | None = None) -> None:
    configure_stdio()
    try:
        args = build_parser().parse_args(argv)
        config = load_config()
        prompt, fields, timeout = job_paths(config, args.command)
        apply_common(prompt, fields, args)
        uploaded = None
        if args.command == "video":
            apply_frames(prompt, fields, args)
            if args.image:
                if args.print_prompt:
                    uploaded = os.path.basename(args.image)
                else:
                    uploaded = upload_image(config["comfy_url"], args.image)
            apply_reference(prompt, fields, uploaded)
        if args.print_prompt:
            print(json.dumps(prompt, ensure_ascii=False, indent=2))
            return
        saved = execute_workflow(config["comfy_url"], prompt, fields, timeout, config["output_dir"])
    except ComfyFailure as exc:
        print(exc.message, file=sys.stderr)
        if exc.payload is not None:
            print(json.dumps(exc.payload, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from exc
    for path in saved:
        print(f"输出 {path}", flush=True)


if __name__ == "__main__":
    main()
