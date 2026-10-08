"""ComfyUI 客户端：提交、进度、下载和取消。

`submit` 在 `POST /prompt` 返回 `prompt_id` 后立即返回，不等到出图结束。
进度按 `prompt_id` 分开。WebSocket 断开时用 `/history/{prompt_id}` 补状态。
错误里保留 ComfyUI 的原文，这里不做翻译。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


DEFAULT_BASE = "http://127.0.0.1:8188"


class ComfyFailure(Exception):
    def __init__(self, message: str, payload=None, prompt_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload
        self.prompt_id = prompt_id


def ws_url(base: str, client_id: str) -> str:
    parsed = urllib.parse.urlparse(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunparse(
        (scheme, parsed.netloc, "/ws", "", urllib.parse.urlencode({"clientId": client_id}), "")
    )


def original_error_text(payload) -> str:
    """抽出 ComfyUI 自己的错误句子。不收录 traceback，也不改写原文。"""
    parts: list[str] = []

    def add(value) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if text and text not in parts:
            parts.append(text)

    if isinstance(payload, str):
        add(payload)
        return "\n".join(parts)
    if not isinstance(payload, dict):
        return ""
    add(payload.get("exception_message"))
    error = payload.get("error")
    if isinstance(error, dict):
        add(error.get("message"))
        add(error.get("details"))
        add(error.get("exception_message"))
    elif isinstance(error, str):
        add(error)
    node_errors = payload.get("node_errors")
    if isinstance(node_errors, dict):
        for info in node_errors.values():
            if not isinstance(info, dict):
                continue
            for item in info.get("errors") or []:
                if isinstance(item, dict):
                    message = str(item.get("message") or "").strip()
                    details = str(item.get("details") or "").strip()
                    if message and details:
                        add(f"{message}: {details}")
                    else:
                        add(message or details)
                else:
                    add(str(item) if item is not None else "")
    status = payload.get("status") if isinstance(payload.get("status"), dict) else payload
    messages = status.get("messages") if isinstance(status, dict) else None
    if isinstance(messages, list):
        for item in messages:
            data = None
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                data = item[1]
            elif isinstance(item, dict):
                data = item
            if isinstance(data, dict):
                add(data.get("exception_message"))
    return "\n".join(parts)


def parse_ws_message(raw) -> dict | None:
    if isinstance(raw, (bytes, bytearray)) or not isinstance(raw, str):
        return None
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind not in {
        "progress",
        "executing",
        "executed",
        "execution_error",
        "execution_success",
        "execution_interrupted",
    }:
        return None
    data = message.get("data") or {}
    if not isinstance(data, dict):
        return None
    event = {
        "type": kind,
        "prompt_id": data.get("prompt_id") or None,
        "node": data.get("node") if "node" in data else data.get("node_id"),
        "raw": data,
    }
    if kind == "progress":
        value = data.get("value")
        maximum = data.get("max")
        event["value"] = value
        event["max"] = maximum
        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and not isinstance(value, bool) and not isinstance(maximum, bool) and maximum:
            event["percent"] = int(round(100 * float(value) / float(maximum)))
        else:
            event["percent"] = None
    return event


def bind_prompt_id(event: dict, active_ids: set[str]) -> dict | None:
    """有 prompt_id 就留在自己的桶里。没有 id 时，只有一个在跟踪的任务才能认领。"""
    bound = dict(event)
    prompt_id = bound.get("prompt_id")
    if prompt_id:
        return bound
    if len(active_ids) == 1:
        bound["prompt_id"] = next(iter(active_ids))
        return bound
    return None


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


def status_of(entry: dict) -> tuple[str, bool]:
    status = entry.get("status") or {}
    return str(status.get("status_str") or ""), bool(status.get("completed"))


def message_types(entry: dict) -> list[str]:
    status = entry.get("status") or {}
    found = []
    for item in status.get("messages") or []:
        if isinstance(item, (list, tuple)) and item:
            found.append(str(item[0]))
        elif isinstance(item, dict) and item.get("type"):
            found.append(str(item["type"]))
    return found


def job_state(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return "missing"
    if "execution_interrupted" in message_types(entry):
        return "interrupted"
    status_str, completed = status_of(entry)
    if status_str == "error":
        return "error"
    if output_files(entry) and (status_str == "success" or completed):
        return "success"
    if status_str == "success":
        return "success_without_files"
    return "running"


class ComfyClient:
    def __init__(self, base: str | None = None, client_id: str | None = None, urlopen=None, connect=None):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.client_id = client_id or str(uuid.uuid4())
        self._urlopen = urlopen or urllib.request.urlopen
        self._connect = connect
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._start_lock = threading.Lock()
        self._events: dict[str, list[dict]] = {}
        self._inbox: dict[str, list[dict]] = {}
        self._tracked: set[str] = set()
        self._cancelled: set[str] = set()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ws_dead = threading.Event()
        self._ws_dead_reported = False

    def submit(self, prompt: dict) -> str:
        """提交工作流并立刻返回 prompt_id。不在这里等待采样结束。"""
        self._ensure_listener()
        result = self._request_json(
            self.base + "/prompt",
            {"prompt": prompt, "client_id": self.client_id},
        )
        node_errors = result.get("node_errors") or {}
        prompt_id = result.get("prompt_id")
        if node_errors or not prompt_id:
            text = original_error_text(result) or "ComfyUI 拒绝了这张工作流。"
            raise ComfyFailure(text, result, prompt_id if isinstance(prompt_id, str) else None)
        self._track(str(prompt_id))
        return str(prompt_id)

    def events(self, prompt_id: str) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._events.get(prompt_id, [])]

    def history(self, prompt_id: str) -> dict | None:
        payload = self._request_json(f"{self.base}/history/{urllib.parse.quote(prompt_id)}")
        if not isinstance(payload, dict):
            return None
        entry = payload.get(prompt_id)
        if isinstance(entry, dict):
            return entry
        return None

    def queued(self, prompt_id: str) -> bool:
        try:
            payload = self._request_json(self.base + "/queue")
        except ComfyFailure:
            return True
        if not isinstance(payload, dict):
            return True
        for key in ("queue_running", "queue_pending"):
            for item in payload.get(key) or []:
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                    return True
        return False

    def cancel(self, prompt_id: str) -> None:
        """取消这一次运行。先移出等待队列，再 POST /interrupt。结果不会记为成功。"""
        with self._cond:
            self._cancelled.add(prompt_id)
            self._tracked.add(prompt_id)
            self._cond.notify_all()
        self._request_json(self.base + "/queue", {"delete": [prompt_id]})
        self._request_json(self.base + "/interrupt", {"prompt_id": prompt_id})

    def wait(self, prompt_id: str, timeout: int, on_event=None) -> dict:
        deadline = time.monotonic() + timeout
        seen = 0
        last_poll = 0.0
        entry = None
        while time.monotonic() < deadline:
            with self._lock:
                events = list(self._events.get(prompt_id, []))
                cancelled = prompt_id in self._cancelled
                ws_dead = self._ws is None or self._ws_dead.is_set()
            new = events[seen:]
            seen = len(events)
            for event in new:
                if on_event is not None:
                    on_event(event)
                self._raise_if_terminal_event(event)
            now = time.monotonic()
            saw_done = any(event.get("type") in {"execution_success", "execution_interrupted"} for event in new)
            saw_finished_node = any(event.get("type") == "executing" and event.get("node") is None for event in new)
            gap = 0.0 if (saw_done or saw_finished_node or cancelled) else (0.5 if ws_dead else 2.0)
            if now - last_poll >= gap:
                last_poll = now
                entry = self.history(prompt_id)
                self._raise_if_terminal_history(prompt_id, entry, cancelled)
                if isinstance(entry, dict) and job_state(entry) == "success":
                    return entry
                if cancelled and not isinstance(entry, dict) and not self.queued(prompt_id):
                    raise ComfyFailure("这次运行已取消，不会记为成功。", None, prompt_id)
            time.sleep(0.05)
        raise ComfyFailure(
            f"等待超时（{timeout} 秒）。视频和图片使用各自的超时，这次没有拿到输出。",
            (entry or {}).get("status") if isinstance(entry, dict) else None,
            prompt_id,
        )

    def download(self, item: dict, output_dir: str) -> str:
        filename = item["filename"]
        subfolder = item.get("subfolder") or ""
        folder_type = item.get("type") or "output"
        query = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": folder_type}
        )
        url = f"{self.base}/view?{query}"
        relative = os.path.join(subfolder, filename) if subfolder else filename
        destination = os.path.join(output_dir, relative)
        os.makedirs(os.path.dirname(destination) or output_dir, exist_ok=True)
        req = urllib.request.Request(url, method="GET")
        try:
            with self._urlopen(req, timeout=120) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            text = original_error_text(raw) or f"下载输出失败，HTTP {exc.code} {filename}"
            raise ComfyFailure(text, raw) from exc
        except urllib.error.URLError as exc:
            raise ComfyFailure(f"下载输出失败：{filename} ({exc.reason})") from exc
        if not data:
            raise ComfyFailure(f"下载到的文件是空的：{filename}")
        with open(destination, "wb") as handle:
            handle.write(data)
        return os.path.abspath(destination)

    def save_outputs(self, entry: dict, output_dir: str) -> list[str]:
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
        saved = [self.download(item, output_dir) for item in chosen]
        if not saved:
            raise ComfyFailure("执行成功，但历史记录里没有可下载的文件。", entry.get("outputs"))
        return saved

    def upload_image(self, path: str) -> str:
        if not os.path.isfile(path):
            raise ComfyFailure(f"参考图不存在：{path}")
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1].lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(ext, "application/octet-stream")
        boundary = "----ComfyClient" + uuid.uuid4().hex
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
            self.base + "/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._urlopen(req, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            text = original_error_text(raw) or f"上传参考图失败，HTTP {exc.code}"
            raise ComfyFailure(text, raw) from exc
        except urllib.error.URLError as exc:
            raise ComfyFailure(f"上传参考图失败：{exc.reason}") from exc
        name = payload.get("name")
        if not name:
            raise ComfyFailure("上传参考图后 ComfyUI 没有返回文件名。", payload)
        subfolder = payload.get("subfolder") or ""
        if subfolder:
            return subfolder.replace("\\", "/") + "/" + name
        return name

    def close(self) -> None:
        self._stop.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None

    def _track(self, prompt_id: str) -> None:
        with self._cond:
            self._tracked.add(prompt_id)
            buffered = self._inbox.pop(prompt_id, [])
            if buffered:
                self._events.setdefault(prompt_id, []).extend(buffered)
            self._cond.notify_all()

    def _ensure_listener(self) -> None:
        with self._start_lock:
            if self._thread is not None or self._ws_dead.is_set() or self._stop.is_set():
                return
            try:
                ws = self._open_ws()
            except Exception as exc:
                self._mark_ws_dead(f"WebSocket 连不上，改为轮询 /history：{exc}")
                return
            self._ws = ws
            self._thread = threading.Thread(target=self._listen, name="comfy-ws", daemon=True)
            self._thread.start()

    def _open_ws(self):
        if self._connect is not None:
            ws = self._connect(ws_url(self.base, self.client_id), self.client_id)
        else:
            from websocket import create_connection

            ws = create_connection(
                ws_url(self.base, self.client_id),
                timeout=10,
                header=[f"Origin: {self.base}"],
            )
        if hasattr(ws, "settimeout"):
            ws.settimeout(1)
        return ws

    def _listen(self) -> None:
        from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except WebSocketTimeoutException:
                continue
            except WebSocketConnectionClosedException:
                self._mark_ws_dead("WebSocket 已断开，改为轮询 /history")
                return
            except Exception:
                if self._stop.is_set():
                    return
                self._mark_ws_dead("WebSocket 已断开，改为轮询 /history")
                return
            event = parse_ws_message(raw)
            if event is None:
                continue
            self._accept(event)

    def _accept(self, event: dict) -> None:
        with self._cond:
            bound = bind_prompt_id(event, set(self._tracked))
            if bound is None:
                return
            prompt_id = bound.get("prompt_id")
            if not prompt_id:
                return
            if prompt_id in self._tracked:
                self._events.setdefault(prompt_id, []).append(bound)
            else:
                bucket = self._inbox.setdefault(prompt_id, [])
                if len(bucket) < 200:
                    bucket.append(bound)
            self._cond.notify_all()

    def _mark_ws_dead(self, message: str) -> None:
        with self._cond:
            self._ws_dead.set()
            first = not self._ws_dead_reported
            self._ws_dead_reported = True
            self._cond.notify_all()
        if first:
            print(message, file=sys.stderr, flush=True)

    def _raise_if_terminal_event(self, event: dict) -> None:
        kind = event.get("type")
        prompt_id = event.get("prompt_id")
        raw = event.get("raw")
        if kind == "execution_error":
            text = original_error_text(raw) or "ComfyUI 执行失败。"
            raise ComfyFailure(text, raw, prompt_id)
        if kind == "execution_interrupted":
            raise ComfyFailure("这次运行已中断，不会记为成功。", raw, prompt_id)

    def _raise_if_terminal_history(self, prompt_id: str, entry, cancelled: bool) -> None:
        state = job_state(entry)
        if state == "interrupted":
            raise ComfyFailure("这次运行已中断，不会记为成功。", entry.get("status"), prompt_id)
        if state == "error":
            text = original_error_text(entry) or "ComfyUI 执行失败。"
            raise ComfyFailure(text, entry.get("status"), prompt_id)
        if cancelled and state == "success":
            raise ComfyFailure("已取消的运行不能记为成功。", entry.get("status"), prompt_id)

    def _request_json(self, url: str, payload: dict | None = None, timeout: int = 120):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        try:
            with self._urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            text = original_error_text(parsed) or f"ComfyUI HTTP {exc.code}"
            raise ComfyFailure(text, parsed) from exc
        except urllib.error.URLError as exc:
            raise ComfyFailure(f"连不上 ComfyUI：{url} ({exc.reason})") from exc
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ComfyFailure(f"ComfyUI 返回的不是 JSON：{url}", body[:2000]) from exc
        return parsed


def upload(base: str, path: str) -> str:
    return ComfyClient(base).upload_image(path)


def run(base: str, template: dict, workflow: dict, fields: dict, timeout: int, output_dir: str) -> list[str]:
    import comfy_poc

    return comfy_poc.execute_workflow(base, template, workflow, fields, timeout, output_dir)


def _check_fail(message: str) -> None:
    raise ComfyFailure(message)


def _check_routing() -> None:
    progress_a = parse_ws_message(json.dumps({
        "type": "progress",
        "data": {"value": 1, "max": 20, "prompt_id": "prompt-a", "node": "5"},
    }))
    progress_b = parse_ws_message(json.dumps({
        "type": "progress",
        "data": {"value": 9, "max": 4, "prompt_id": "prompt-b", "node": "12"},
    }))
    executed = parse_ws_message(json.dumps({
        "type": "executed",
        "data": {"node": "7", "prompt_id": "prompt-a"},
    }))
    anonymous = parse_ws_message(json.dumps({
        "type": "progress",
        "data": {"value": 3, "max": 10, "node": "5"},
    }))
    if not progress_a or progress_a["prompt_id"] != "prompt-a":
        _check_fail("进度事件没有留下 prompt_id。")
    if progress_a.get("value") != 1 or progress_a.get("max") != 20 or progress_a.get("percent") != 5:
        _check_fail(f"进度事件的步数不对：{progress_a}")
    if not executed or executed["type"] != "executed" or executed["prompt_id"] != "prompt-a":
        _check_fail("executed 事件没有按 prompt_id 解析。")
    if bind_prompt_id(anonymous, {"prompt-a", "prompt-b"}) is not None:
        _check_fail("两个任务同时在跟踪时，没有 prompt_id 的事件被认领了。")
    claimed = bind_prompt_id(anonymous, {"prompt-a"})
    if not claimed or claimed["prompt_id"] != "prompt-a":
        _check_fail("只有一个任务时，没有 prompt_id 的进度应该归到它。")
    if bind_prompt_id(progress_b, {"prompt-a"})["prompt_id"] != "prompt-b":
        _check_fail("别的 prompt_id 被并进了当前任务。")


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeSocket:
    def __init__(self, messages=None, close_on_recv: bool = False):
        self.messages = list(messages or [])
        self.close_on_recv = close_on_recv
        self.timeout = 1
        self.closed = False

    def settimeout(self, value) -> None:
        self.timeout = value

    def recv(self):
        from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

        if self.close_on_recv or self.closed:
            raise WebSocketConnectionClosedException("closed")
        if self.messages:
            return self.messages.pop(0)
        time.sleep(0.01)
        raise WebSocketTimeoutException("timed out")

    def close(self) -> None:
        self.closed = True


def _http_error(payload: dict):
    import email.message
    import io

    raw = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        "http://127.0.0.1:8188/prompt",
        400,
        "Bad Request",
        email.message.Message(),
        io.BytesIO(raw),
    )


def _check_error_text() -> None:
    payload = {
        "error": {
            "type": "prompt_outputs_failed_validation",
            "message": "Prompt outputs failed validation",
            "details": "ckpt_name: 'missing-model-for-client-check.safetensors' not in list",
        },
        "node_errors": {
            "1": {
                "errors": [{
                    "message": "Value not in list",
                    "details": "ckpt_name: 'missing-model-for-client-check.safetensors' not in list",
                }],
                "class_type": "CheckpointLoaderSimple",
            }
        },
    }
    calls = {"prompt": 0}

    def urlopen(req, timeout=120):
        if req.get_method() == "POST" and req.full_url.endswith("/prompt"):
            calls["prompt"] += 1
            raise _http_error(payload)
        raise ComfyFailure(f"校验不该访问 {req.full_url}")

    client = ComfyClient("http://127.0.0.1:8188", urlopen=urlopen, connect=lambda *_args: _FakeSocket())
    try:
        try:
            client.submit({"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}}})
        except ComfyFailure as exc:
            if "missing-model-for-client-check.safetensors" not in exc.message:
                _check_fail(f"错误没有保留模型名原文：{exc.message}")
            if "Prompt outputs failed validation" not in exc.message:
                _check_fail(f"错误没有保留 ComfyUI 原文：{exc.message}")
            if not isinstance(exc.payload, dict) or exc.payload.get("error", {}).get("message") != "Prompt outputs failed validation":
                _check_fail("错误载荷里的 ComfyUI 原文被改写了。")
            if calls["prompt"] != 1:
                _check_fail("拒绝提交时没有把原文错误留在这一次 POST 上。")
        else:
            _check_fail("缺模型时提交被当成了成功。")
    finally:
        client.close()


def _check_submit_and_cancel() -> None:
    state = {
        "phase": "running",
        "ids": [],
        "calls": [],
    }
    sockets = []

    def connect(_url, _client_id):
        socket = _FakeSocket()
        sockets.append(socket)
        return socket

    def urlopen(req, timeout=120):
        method = req.get_method()
        url = req.full_url
        body = {}
        if req.data:
            body = json.loads(req.data.decode("utf-8"))
        if method == "POST" and url.endswith("/prompt"):
            prompt_id = f"00000000-0000-0000-0000-{len(state['ids']) + 1:012d}"
            state["ids"].append(prompt_id)
            state["calls"].append(("prompt", prompt_id))
            return _FakeResponse(json.dumps({
                "prompt_id": prompt_id,
                "number": len(state["ids"]),
                "node_errors": {},
            }).encode("utf-8"))
        if method == "GET" and "/history/" in url:
            prompt_id = url.rstrip("/").rsplit("/", 1)[-1]
            state["calls"].append(("history", prompt_id))
            if state["phase"] == "interrupted" and prompt_id == state["ids"][0]:
                entry = {
                    "status": {
                        "status_str": "error",
                        "completed": False,
                        "messages": [["execution_interrupted", {"prompt_id": prompt_id}]],
                    },
                    "outputs": {},
                }
                return _FakeResponse(json.dumps({prompt_id: entry}).encode("utf-8"))
            if state["phase"] == "success" and prompt_id == state["ids"][0]:
                entry = {
                    "status": {"status_str": "success", "completed": True, "messages": [["execution_success", {}]]},
                    "outputs": {"7": {"images": [{"filename": "poc.png", "subfolder": "", "type": "output"}]}},
                }
                return _FakeResponse(json.dumps({prompt_id: entry}).encode("utf-8"))
            return _FakeResponse(b"{}")
        if method == "GET" and url.endswith("/queue"):
            if state["phase"] == "running" and state["ids"]:
                running = [[0, state["ids"][0]]]
            else:
                running = []
            return _FakeResponse(json.dumps({"queue_running": running, "queue_pending": []}).encode("utf-8"))
        if method == "POST" and url.endswith("/queue"):
            state["calls"].append(("delete", body.get("delete")))
            return _FakeResponse(b"")
        if method == "POST" and url.endswith("/interrupt"):
            state["calls"].append(("interrupt", body.get("prompt_id")))
            state["phase"] = "interrupted"
            return _FakeResponse(b"")
        raise ComfyFailure(f"校验不该访问 {method} {url}")

    client = ComfyClient("http://127.0.0.1:8188", urlopen=urlopen, connect=connect)
    try:
        started = time.monotonic()
        first = client.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        second = client.submit({"2": {"class_type": "SaveImage", "inputs": {}}})
        elapsed = time.monotonic() - started
        if elapsed > 2:
            _check_fail(f"提交没有立即返回，用了 {elapsed:.2f} 秒。")
        if state["phase"] != "running":
            _check_fail("提交返回时任务已经不在运行。")
        if not client.queued(first):
            _check_fail("提交返回时任务已经不在队列里。")
        if client.history(first) is not None:
            _check_fail("提交返回时历史里已经有结果。")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not sockets:
            time.sleep(0.01)
        if not sockets:
            _check_fail("提交前没有连上 WebSocket。")
        sockets[0].messages.append(json.dumps({
            "type": "progress",
            "data": {"value": 1, "max": 20, "prompt_id": first, "node": "5"},
        }))
        sockets[0].messages.append(json.dumps({
            "type": "progress",
            "data": {"value": 4, "max": 4, "prompt_id": second, "node": "12"},
        }))
        sockets[0].messages.append(json.dumps({
            "type": "progress",
            "data": {"value": 2, "max": 20, "prompt_id": first, "node": "5"},
        }))
        sockets[0].messages.append(json.dumps({
            "type": "executed",
            "data": {"node": "7", "prompt_id": first},
        }))
        seen = []
        limit = time.monotonic() + 2
        while time.monotonic() < limit:
            seen = client.events(first)
            other = client.events(second)
            if len(seen) >= 3 and len(other) >= 1:
                break
            time.sleep(0.02)
        else:
            _check_fail(f"没有在过程中收到进度：{len(client.events(first))} / {len(client.events(second))}")
        values = [item.get("value") for item in client.events(first) if item.get("type") == "progress"]
        other_values = [item.get("value") for item in client.events(second) if item.get("type") == "progress"]
        if values != [1, 2] or other_values != [4]:
            _check_fail(f"进度串到了别的 prompt_id：{values} / {other_values}")
        if any(item.get("prompt_id") != first for item in client.events(first)):
            _check_fail("事件桶里出现了别的 prompt_id。")
        client.cancel(first)
        if ("interrupt", first) not in state["calls"]:
            _check_fail(f"取消没有把 interrupt 发到这个 prompt_id：{state['calls']}")
        try:
            client.wait(first, timeout=3)
        except ComfyFailure as exc:
            if exc.prompt_id != first:
                _check_fail("取消错误没有带上这次的 prompt_id。")
            if "成功" not in exc.message and "中断" not in exc.message and "取消" not in exc.message:
                _check_fail(f"取消后的错误不像一次失败：{exc.message}")
            if job_state(client.history(first)) == "success":
                _check_fail("取消后的运行被记成了成功。")
        else:
            _check_fail("取消后的等待返回了结果。")
    finally:
        client.close()


def _check_history_fallback() -> None:
    state = {"phase": "running", "prompt_id": None}

    def connect(_url, _client_id):
        return _FakeSocket(close_on_recv=True)

    def urlopen(req, timeout=120):
        method = req.get_method()
        url = req.full_url
        if method == "POST" and url.endswith("/prompt"):
            state["prompt_id"] = "00000000-0000-0000-0000-000000000099"
            return _FakeResponse(json.dumps({
                "prompt_id": state["prompt_id"],
                "number": 1,
                "node_errors": {},
            }).encode("utf-8"))
        if method == "GET" and "/history/" in url:
            prompt_id = state["prompt_id"]
            if state["phase"] != "success":
                return _FakeResponse(b"{}")
            entry = {
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [[
                        "execution_error",
                        {"prompt_id": prompt_id, "exception_message": "Node 'MissingNodeForIssue4' not found."},
                    ]],
                },
                "outputs": {},
            }
            return _FakeResponse(json.dumps({prompt_id: entry}).encode("utf-8"))
        if method == "GET" and url.endswith("/queue"):
            return _FakeResponse(b'{"queue_running":[],"queue_pending":[]}')
        raise ComfyFailure(f"校验不该访问 {method} {url}")

    client = ComfyClient("http://127.0.0.1:8188", urlopen=urlopen, connect=connect)
    try:
        prompt_id = client.submit({"1": {"class_type": "MissingNodeForIssue4", "inputs": {}}})
        limit = time.monotonic() + 2
        while time.monotonic() < limit and not client._ws_dead.is_set():
            time.sleep(0.02)
        if not client._ws_dead.is_set():
            _check_fail("WebSocket 断开后没有改去查历史。")
        state["phase"] = "success"
        try:
            client.wait(prompt_id, timeout=3)
        except ComfyFailure as exc:
            if "MissingNodeForIssue4" not in exc.message:
                _check_fail(f"断线后的历史错误没有保留原文：{exc.message}")
        else:
            _check_fail("历史里的失败被当成了成功。")
    finally:
        client.close()


def _check_history_success_after_disconnect() -> None:
    state = {"ready": False}

    def connect(_url, _client_id):
        return _FakeSocket(close_on_recv=True)

    def urlopen(req, timeout=120):
        method = req.get_method()
        url = req.full_url
        if method == "POST" and url.endswith("/prompt"):
            return _FakeResponse(json.dumps({
                "prompt_id": "00000000-0000-0000-0000-000000000077",
                "number": 1,
                "node_errors": {},
            }).encode("utf-8"))
        if method == "GET" and "/history/" in url:
            if not state["ready"]:
                return _FakeResponse(b"{}")
            prompt_id = "00000000-0000-0000-0000-000000000077"
            entry = {
                "status": {"status_str": "success", "completed": True, "messages": [["execution_success", {}]]},
                "outputs": {"7": {"images": [{"filename": "poc.png", "subfolder": "", "type": "output"}]}},
            }
            return _FakeResponse(json.dumps({prompt_id: entry}).encode("utf-8"))
        if method == "GET" and url.endswith("/queue"):
            return _FakeResponse(b'{"queue_running":[],"queue_pending":[]}')
        raise ComfyFailure(f"校验不该访问 {method} {url}")

    client = ComfyClient("http://127.0.0.1:8188", urlopen=urlopen, connect=connect)
    try:
        prompt_id = client.submit({"7": {"class_type": "SaveImage", "inputs": {}}})
        limit = time.monotonic() + 2
        while time.monotonic() < limit and not client._ws_dead.is_set():
            time.sleep(0.02)
        if not client._ws_dead.is_set():
            _check_fail("成功路径里 WebSocket 断开后没有改去查历史。")
        state["ready"] = True
        entry = client.wait(prompt_id, timeout=3)
        if job_state(entry) != "success":
            _check_fail("断线后的成功历史没有被认出来。")
        names = [item.get("filename") for item in output_files(entry)]
        if names != ["poc.png"]:
            _check_fail(f"断线后没有从历史里拿到文件：{names}")
    finally:
        client.close()


def self_check() -> None:
    _check_routing()
    _check_error_text()
    _check_submit_and_cancel()
    _check_history_fallback()
    _check_history_success_after_disconnect()
    print("client_check ok")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ComfyUI 客户端。check 不连接本机 ComfyUI。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="校验提交、进度隔离、断线回退和取消。不连接 ComfyUI。")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            self_check()
    except ComfyFailure as exc:
        print(exc.message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
