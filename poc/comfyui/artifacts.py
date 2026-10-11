"""按会话保存生成结果。模型和聊天只拿 artifact_id。

展示 URL 必须带对的会话。知道 ComfyUI 的 /view 文件名，也不能读到别的会话里的文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{32}$")
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
_VIDEO_EXTENSIONS = {".mp4", ".webm"}


class ArtifactError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def is_artifact_id(value: str) -> bool:
    return isinstance(value, str) and bool(_ARTIFACT_ID.fullmatch(value))


def check_token(value: str, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ArtifactError(f"{label}不合法。")
    return value


def template_label(config: dict, kind: str) -> str:
    raw = str((config.get(kind) or {}).get("workflow") or kind)
    if os.path.isabs(raw):
        return os.path.basename(raw)
    return raw.replace("\\", "/")


def open_store(config: dict) -> tuple["ArtifactStore", str]:
    base = config.get("_base") or os.getcwd()
    raw = os.environ.get("COMFY_ARTIFACT_DIR") or config.get("artifact_dir") or "artifacts"
    root = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(base, raw))
    store = ArtifactStore(root)
    session = os.environ.get("COMFY_SESSION") or config.get("session_id")
    if session:
        return store, check_token(str(session), "会话")
    return store, store.current_session()


def resolve_image_ref(config: dict, image_ref: str) -> tuple[str, str | None]:
    """给运行时取出本地文件。返回值里的路径不能写进模型请求。"""
    if is_artifact_id(image_ref or ""):
        store, session_id = open_store(config)
        path = store.file_for(session_id, image_ref)
        if not path:
            raise ArtifactError("找不到参考图。")
        return path, image_ref
    if image_ref and os.path.isfile(image_ref):
        return image_ref, None
    raise ArtifactError("找不到参考图。")


def remember_outputs(
    config: dict,
    paths: list[str],
    *,
    kind: str,
    user_prompt: str,
    workflow_prompt: str,
    seed: int,
    width: int,
    height: int,
    duration_sec: float | None = None,
) -> list[dict]:
    store, session_id = open_store(config)
    template = template_label(config, kind)
    saved = []
    for path in paths:
        saved.append(
            store.save(
                session_id=session_id,
                source_path=path,
                seed=int(seed),
                template=template,
                user_prompt=user_prompt,
                workflow_prompt=workflow_prompt,
                width=int(width),
                height=int(height),
                duration_sec=duration_sec,
            )
        )
    if not saved:
        raise ArtifactError("生成成功但没有工件。")
    return saved


def model_request(session_id: str, text: str, artifact_ids: list[str]) -> dict:
    """送给模型的请求体。媒体位置只有 artifact_id。"""
    check_token(session_id, "会话")
    content = []
    for artifact_id in artifact_ids:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ArtifactError("工件标识不合法。")
        content.append({"artifact_id": artifact_id})
    return {
        "session_id": session_id,
        "messages": [
            {"role": "user", "content": text},
            {"role": "tool", "content": content},
        ],
    }


def assert_model_text(body: dict, artifact_ids: list[str], banned_text: list[str], banned_bytes: list[bytes]) -> None:
    raw = json.dumps(body, ensure_ascii=False)
    for artifact_id in artifact_ids:
        if artifact_id not in raw:
            raise ArtifactError("模型请求里没有 artifact_id。")
    for secret in banned_text:
        if secret and secret in raw:
            raise ArtifactError("模型请求里出现了文件路径或正文。")
    import base64

    for blob in banned_bytes:
        if not blob:
            continue
        if blob[:8] in raw.encode("utf-8"):
            raise ArtifactError("模型请求里出现了文件字节。")
        encoded = base64.b64encode(blob).decode("ascii")
        if encoded[:48] in raw:
            raise ArtifactError("模型请求里出现了 base64。")
    if "base64" in raw.lower() or "data:image" in raw.lower() or "data:video" in raw.lower():
        raise ArtifactError("模型请求里出现了媒体正文。")


def probe_bytes(data: bytes) -> dict:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return {"type": "image", "width": int(width), "height": int(height), "media_type": "image/png"}
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return _probe_mp4(data)
    return {}


def probe_media(path: str) -> dict:
    with open(path, "rb") as handle:
        data = handle.read()
    found = probe_bytes(data)
    if found:
        return found
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXTENSIONS:
        kind = "video" if ext in _VIDEO_EXTENSIONS else "image"
        return {"type": kind, "media_type": _EXTENSIONS[ext]}
    return {}


def _walk_boxes(data: bytes) -> list[tuple[bytes, bytes]]:
    found = []

    def walk(buf: bytes) -> None:
        index = 0
        while index + 8 <= len(buf):
            size = struct.unpack_from(">I", buf, index)[0]
            kind = buf[index + 4 : index + 8]
            header = 8
            if size == 1:
                if index + 16 > len(buf):
                    return
                size = struct.unpack_from(">Q", buf, index + 8)[0]
                header = 16
            elif size == 0:
                size = len(buf) - index
            if size < header or index + size > len(buf):
                return
            payload = buf[index + header : index + size]
            found.append((kind, payload))
            if kind in _CONTAINERS:
                walk(payload)
            index += size

    walk(data)
    return found


def _probe_mp4(data: bytes) -> dict:
    width = None
    height = None
    duration = None
    for kind, payload in _walk_boxes(data):
        if kind == b"mvhd" and len(payload) >= 20 and duration is None:
            version = payload[0]
            if version == 1 and len(payload) >= 32:
                timescale = struct.unpack_from(">I", payload, 20)[0]
                ticks = struct.unpack_from(">Q", payload, 24)[0]
            elif version == 0:
                timescale = struct.unpack_from(">I", payload, 12)[0]
                ticks = struct.unpack_from(">I", payload, 16)[0]
            else:
                continue
            if timescale:
                duration = float(ticks) / float(timescale)
        elif kind == b"tkhd" and width is None and len(payload) >= 84:
            version = payload[0]
            offset = 88 if version == 1 else 76
            if len(payload) >= offset + 8:
                raw_width, raw_height = struct.unpack_from(">II", payload, offset)
                if raw_width and raw_height:
                    width = int(raw_width >> 16)
                    height = int(raw_height >> 16)
    found = {"type": "video", "media_type": "video/mp4"}
    if width and height:
        found["width"] = width
        found["height"] = height
    if duration is not None and duration > 0:
        found["duration_sec"] = duration
    return found


class ArtifactStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def current_session(self) -> str:
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, "session_id")
        if os.path.isfile(path):
            text = open(path, encoding="utf-8").read().strip()
            if _TOKEN.fullmatch(text):
                return text
        session_id = "ses_" + uuid.uuid4().hex
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(session_id)
        return session_id

    def save(
        self,
        *,
        session_id: str,
        source_path: str,
        seed: int,
        template: str,
        user_prompt: str,
        workflow_prompt: str,
        width: int,
        height: int,
        duration_sec: float | None = None,
    ) -> dict:
        session_id = check_token(session_id, "会话")
        if not os.path.isfile(source_path):
            raise ArtifactError("要保存的生成文件不存在。")
        if seed < 0:
            raise ArtifactError("seed 不能是负数。")
        probed = probe_media(source_path)
        kind = probed.get("type")
        if kind not in {"image", "video"}:
            raise ArtifactError("只能保存图片或视频。")
        width = int(probed.get("width") or width)
        height = int(probed.get("height") or height)
        if width < 1 or height < 1:
            raise ArtifactError("图片工件没有宽高。")
        record = {
            "artifact_id": "art_" + uuid.uuid4().hex,
            "session_id": session_id,
            "type": kind,
            "width": width,
            "height": height,
            "seed": int(seed),
            "template": template,
            "user_prompt": str(user_prompt),
            "workflow_prompt": str(workflow_prompt),
            "media_type": probed.get("media_type") or _EXTENSIONS.get(os.path.splitext(source_path)[1].lower(), "application/octet-stream"),
        }
        if kind == "video":
            duration = probed.get("duration_sec")
            if duration is None:
                duration = duration_sec
            if duration is None or float(duration) <= 0:
                raise ArtifactError("视频工件没有时长。")
            record["duration_sec"] = float(duration)
        ext = os.path.splitext(source_path)[1].lower()
        if ext not in _EXTENSIONS:
            ext = ".mp4" if kind == "video" else ".png"
        record["file"] = record["artifact_id"] + ext
        folder = self._session_dir(session_id)
        os.makedirs(os.path.join(folder, "files"), exist_ok=True)
        os.makedirs(os.path.join(folder, "records"), exist_ok=True)
        target = os.path.join(folder, "files", record["file"])
        shutil.copyfile(source_path, target)
        record_path = os.path.join(folder, "records", record["artifact_id"] + ".json")
        temporary = record_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, record_path)
        return self.publish(record)

    def publish(self, record: dict) -> dict:
        shown = {key: value for key, value in record.items() if key != "file"}
        shown["url"] = self.display_url(record["session_id"], record["artifact_id"])
        return shown

    def get(self, session_id: str, artifact_id: str) -> dict | None:
        path = self._record_path(session_id, artifact_id)
        if path is None or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        if not isinstance(record, dict) or record.get("session_id") != session_id:
            return None
        return record

    def public(self, session_id: str, artifact_id: str) -> dict | None:
        record = self.get(session_id, artifact_id)
        if record is None:
            return None
        return self.publish(record)

    def file_for(self, session_id: str, artifact_id: str) -> str | None:
        record = self.get(session_id, artifact_id)
        if record is None:
            return None
        name = record.get("file")
        if not isinstance(name, str) or os.path.basename(name) != name:
            raise ArtifactError("工件文件不在库里。")
        path = os.path.join(self._session_dir(session_id), "files", name)
        if not os.path.isfile(path):
            raise ArtifactError("工件文件不在库里。")
        return path

    def display_url(self, session_id: str, artifact_id: str) -> str:
        check_token(session_id, "会话")
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ArtifactError("工件标识不合法。")
        return f"/sessions/{session_id}/artifacts/{artifact_id}"

    def open_for_http(self, url_path: str) -> tuple[int, str, str | None]:
        parsed = urllib.parse.urlsplit(url_path)
        path = parsed.path
        if path == "/view" or path.startswith("/view/"):
            return 404, "text/plain", None
        match = re.fullmatch(r"/sessions/([^/]+)/artifacts/([^/]+)", path)
        if not match:
            return 404, "text/plain", None
        session_id, artifact_id = match.group(1), match.group(2)
        try:
            check_token(session_id, "会话")
        except ArtifactError:
            return 404, "text/plain", None
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            return 404, "text/plain", None
        record = self.get(session_id, artifact_id)
        if record is None:
            return 404, "text/plain", None
        try:
            file_path = self.file_for(session_id, artifact_id)
        except ArtifactError:
            return 404, "text/plain", None
        if not file_path:
            return 404, "text/plain", None
        media = str(record.get("media_type") or "application/octet-stream")
        return 200, media, file_path

    def _session_dir(self, session_id: str) -> str:
        session_id = check_token(session_id, "会话")
        return os.path.join(self.root, "sessions", session_id)

    def _record_path(self, session_id: str, artifact_id: str) -> str | None:
        try:
            check_token(session_id, "会话")
        except ArtifactError:
            return None
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            return None
        return os.path.join(self._session_dir(session_id), "records", artifact_id + ".json")


class ArtifactHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, media, path = self.server.store.open_for_http(self.path)  # type: ignore[attr-defined]
        if path is None:
            body = b"not found"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = os.path.getsize(path)
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private")
        self.end_headers()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def log_message(self, fmt: str, *args) -> None:
        return


def serve(store: ArtifactStore, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ArtifactHandler)
    server.store = store  # type: ignore[attr-defined]
    return server


def _png(width: int, height: int, color: tuple[int, int, int] = (180, 40, 40)) -> bytes:
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _mp4(version: int, width: int, height: int, timescale: int, ticks: int) -> bytes:
    if version == 1:
        mvhd = struct.pack(">I", 1 << 24)
        mvhd += struct.pack(">QQ", 0, 0)
        mvhd += struct.pack(">I", timescale)
        mvhd += struct.pack(">Q", ticks)
        tkhd = bytearray(96)
        tkhd[0] = 1
        struct.pack_into(">I", tkhd, 88, width << 16)
        struct.pack_into(">I", tkhd, 92, height << 16)
    else:
        mvhd = struct.pack(">III II", 0, 0, 0, timescale, ticks)
        tkhd = bytearray(84)
        struct.pack_into(">I", tkhd, 76, width << 16)
        struct.pack_into(">I", tkhd, 80, height << 16)
    moov = _box(b"mvhd", mvhd) + _box(b"trak", _box(b"tkhd", bytes(tkhd)))
    return _box(b"ftyp", b"isom\x00\x00\x00\x00isom") + _box(b"moov", moov)


def _check_fail(message: str) -> None:
    raise ArtifactError(message)


def _http_get(base: str, path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def self_check() -> None:
    import tempfile

    for version in (0, 1):
        found = probe_bytes(_mp4(version, 512, 288, 1000, 1500))
        if found.get("width") != 512 or found.get("height") != 288:
            _check_fail(f"mp4 宽高没有读出来：{found}")
        if abs(float(found.get("duration_sec") or 0) - 1.5) > 0.001:
            _check_fail(f"mp4 时长没有读出来：{found}")
    png = _png(8, 6)
    image = probe_bytes(png)
    if image.get("type") != "image" or image.get("width") != 8 or image.get("height") != 6:
        _check_fail(f"png 宽高没有读出来：{image}")

    with tempfile.TemporaryDirectory() as root:
        store = ArtifactStore(root)
        png_path = os.path.join(root, "source.png")
        mp4_path = os.path.join(root, "source.mp4")
        with open(png_path, "wb") as handle:
            handle.write(png)
        with open(mp4_path, "wb") as handle:
            handle.write(_mp4(0, 512, 288, 16, 17))
        image_record = store.save(
            session_id="session-a",
            source_path=png_path,
            seed=7,
            template="workflows/image_api.json",
            user_prompt="user says a red teapot",
            workflow_prompt="a red ceramic teapot on a wooden table",
            width=1,
            height=1,
        )
        if image_record["user_prompt"] == image_record["workflow_prompt"]:
            _check_fail("用户原句和送进工作流的 prompt 被写成了同一份。")
        if "duration_sec" in image_record:
            _check_fail("图片工件不应带时长。")
        if image_record["width"] != 8 or image_record["height"] != 6 or image_record["seed"] != 7:
            _check_fail(f"图片工件记录不对：{image_record}")
        if "file" in image_record or os.path.isabs(image_record["template"]):
            _check_fail("公开记录里出现了文件路径。")
        video_record = store.save(
            session_id="session-a",
            source_path=mp4_path,
            seed=8,
            template="workflows/video_api.json",
            user_prompt="user says push in",
            workflow_prompt="the camera slowly pushes in",
            width=1,
            height=1,
            duration_sec=None,
        )
        if abs(float(video_record["duration_sec"]) - (17 / 16)) > 0.001:
            _check_fail(f"视频时长不对：{video_record}")
        if video_record["width"] != 512 or video_record["height"] != 288:
            _check_fail(f"视频宽高不对：{video_record}")
        owned = store.file_for("session-a", image_record["artifact_id"])
        if owned is None or open(owned, "rb").read() != png:
            _check_fail("会话里取不回刚才的文件。")
        if store.file_for("session-b", image_record["artifact_id"]) is not None:
            _check_fail("别的会话取回了这张图。")
        if store.public("session-b", image_record["artifact_id"]) is not None:
            _check_fail("别的会话读到了工件记录。")
        body = model_request("session-a", "用刚才那张图", [image_record["artifact_id"], video_record["artifact_id"]])
        assert_model_text(body, [image_record["artifact_id"], video_record["artifact_id"]], [owned, png_path, mp4_path], [png, open(mp4_path, "rb").read()])
        server = serve(store, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            status, payload = _http_get(base, image_record["url"] if "url" in image_record else store.display_url("session-a", image_record["artifact_id"]))
            if status != 200 or payload != png:
                _check_fail(f"正确会话没有拿到图片：{status}")
            wrong = store.display_url("session-b", image_record["artifact_id"])
            status, payload = _http_get(base, wrong)
            if status == 200 or payload.startswith(b"\x89PNG"):
                _check_fail("错误会话的展示 URL 返回了图片。")
            status, payload = _http_get(base, "/view?filename=" + urllib.parse.quote(os.path.basename(owned)))
            if status == 200 or payload.startswith(b"\x89PNG"):
                _check_fail("ComfyUI 的 /view 文件名读到了工件。")
        finally:
            server.shutdown()
            server.server_close()
    print("artifact_check ok")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="会话工件库。check 不连接 ComfyUI。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="校验记录、会话隔离和展示 URL。")
    serve_command = subparsers.add_parser("serve", help="在本机提供带会话校验的读取 URL。")
    serve_command.add_argument("--host", default="127.0.0.1")
    serve_command.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            self_check()
            return
        root = os.environ.get("COMFY_ARTIFACT_DIR")
        if not root:
            root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
        server = serve(ArtifactStore(root), args.host, args.port)
        print(f"工件读取 http://{args.host}:{args.port}/sessions/{{session_id}}/artifacts/{{artifact_id}}")
        server.serve_forever()
    except ArtifactError as exc:
        print(exc.message)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
