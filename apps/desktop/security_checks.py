"""13.B（#75）桌面系统集成的安全检查。apps/desktop/check.py 和 evals/run.py 共用，不访问外网，
不碰真实系统钥匙串和 ~/.mas（钥匙串一律用 MAS_KEYCHAIN=file:<临时文件>）。失败抛 SystemExit。"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
for _p in (ROOT, ROOT / "poc", ROOT / "poc" / "comfyui", ROOT / "apps" / "web", DESKTOP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TOKEN = "d" * 64
_ENV_KEYS = ("MAS_KEYCHAIN", "MAS_KEYCHAIN_NAMESPACE", "MAS_MASTER_KEY", "MAS_MASTER_KEY_FILE",
             "MAS_STORAGE_ENCRYPTION", "MAS_DESKTOP_NOTIFY", "LLM_API_KEY", "XAI_API_KEY",
             "MAS_IMAGE_API_KEY", "MAS_IMAGE_API_BASE")


def fail(message: str) -> None:
    raise SystemExit(message)


@contextlib.contextmanager
def isolated(**values):
    """临时环境：清掉会影响结果的变量，按需设置；钥匙串默认指向临时文件。"""
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    tmp = tempfile.mkdtemp(prefix="mas-desk-sec-")
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["MAS_KEYCHAIN"] = "file:" + os.path.join(tmp, "keychain.json")
    os.environ["MAS_MASTER_KEY_FILE"] = os.path.join(tmp, "master.key")
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield tmp
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


def _serve(app):
    import threading

    import server

    site = server.make_httpd(app, "127.0.0.1", 0)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    return site, f"http://127.0.0.1:{site.server_address[1]}"


def _http(base: str, method: str, path: str, body=None, token: str | None = TOKEN) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-MAS-Desktop-Token"] = token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _all_bytes(directory: str) -> bytes:
    chunks = []
    for folder, _dirs, files in os.walk(directory):
        for name in files:
            try:
                with open(os.path.join(folder, name), "rb") as handle:
                    chunks.append(name.encode("utf-8", "replace") + b"\0" + handle.read())
            except OSError:
                pass
    return b"\n".join(chunks)


# ---------------------------------------------------------------- 通知

def notify_no_body() -> dict:
    """通知只用宿主模板：工具名 + 状态 + 张数；不含提示词、正文、文件名、注入文本。"""
    import chat_run
    import desktop_notify
    import server

    markers = ["MARKER_PROMPT_7c1", "MARKER_FILE_secret_diary.txt", "C:\\Users\\victim", "MARKER_ANSWER_55",
               "IGNORE_PREVIOUS_MARKER", "MARKER_FAIL_DETAIL"]
    allowed_fields = {"id", "title", "body", "kind", "status", "tool", "count"}
    patterns = []
    for _status, template in desktop_notify.TEMPLATES.values():
        pattern = re.escape(template).replace(re.escape("{tool}"), r"(?:[a-z][a-z0-9_]{0,40}|工具|本轮回答)")
        patterns.append(re.compile(pattern.replace(re.escape("{count}"), r"\d{1,3}")))

    with isolated() as tmp:
        directory = os.path.join(tmp, "data")
        if server.App(directory, timeout_sec=5).notify is not None:
            fail("Web 模式不该启用桌面通知")
        app = server.App(directory, timeout_sec=5, desktop_token=TOKEN)
        if app.notify is None or app.runs.observer is None:
            fail("桌面模式没有挂通知观察者")
        site, base = _serve(app)
        try:
            run = chat_run.Run("s_" + "0" * 32, "m_" + "0" * 32)
            app.runs.add(run)
            events = [
                {"type": "run.started", "text": markers[0]},
                {"type": "tool_call", "tool": "generate_image", "status": "running",
                 "arguments": {"prompt": markers[0]}},
                {"type": "tool.confirm", "tool": "generate_image", "name": "generate_image",
                 "arguments": {"prompt": markers[0], "file": markers[2]}, "card": {"text": markers[4]}},
                {"type": "content_request", "kind": "file", "purpose": markers[1], "hint": markers[1],
                 "draft": markers[4]},
                {"type": "tool_call", "tool": markers[4] + " " + markers[1], "status": "success"},
                {"type": "artifact", "name": markers[1], "path": markers[2]},
                {"type": "message.completed", "text": markers[3] + markers[0]},
            ]
            for event in events:
                run.push(event)
            failing = chat_run.Run("s_" + "1" * 32, "m_" + "1" * 32)
            app.runs.add(failing)
            failing.push({"type": "tool_call", "tool": "generate_image", "status": "failed",
                          "error": {"message": markers[5]}})
            failing.push({"type": "run.failed", "code": "provider_error", "message": markers[5], "text": markers[3]})
            cancelled = chat_run.Run("s_" + "2" * 32, "m_" + "2" * 32)
            app.runs.add(cancelled)
            cancelled.push({"type": "run.failed", "code": "cancelled", "message": markers[0]})

            status, body = _http(base, "GET", "/api/desktop/notifications", token=None)
            if status != 401:
                fail(f"无令牌读通知应 401：{status}")
            status, body = _http(base, "GET", "/api/desktop/notifications?after=0")
            if status != 200:
                fail(f"读通知失败：{status} {body[:120]}")
            items = json.loads(body)["items"]
            kinds = [item["kind"] for item in items]
            if kinds != ["confirm", "content", "done_images", "failed"]:
                fail(f"通知种类不对：{kinds}")
            for marker in markers:
                if marker in body:
                    fail(f"通知里出现了正文 / 文件名 / 注入文本：{marker}")
            for item in items:
                if set(item) - allowed_fields:
                    fail(f"通知多了字段：{sorted(set(item) - allowed_fields)}")
                if item["title"] != desktop_notify.TITLE:
                    fail(f"通知标题不是固定模板：{item['title']}")
                if not any(p.fullmatch(item["body"]) for p in patterns):
                    fail(f"通知正文不是宿主模板：{item['body']}")
            # 最后一个工具名是注入文本：不在白名单，只写成「工具」。
            if items[2]["count"] != 1 or items[2]["tool"] != desktop_notify.GENERIC:
                fail(f"完成通知的工具 / 张数不对：{items[2]}")
            last = json.loads(body)["last"]
            status, body = _http(base, "GET", f"/api/desktop/notifications?after={last}")
            if json.loads(body)["items"]:
                fail("after 之后不该再有旧通知")
            os.environ["MAS_DESKTOP_NOTIFY"] = "0"
            quiet = chat_run.Run("s_" + "3" * 32, "m_" + "3" * 32)
            app.runs.add(quiet)
            quiet.push({"type": "message.completed", "text": "x"})
            if app.notify.since(last)["items"]:
                fail("MAS_DESKTOP_NOTIFY=0 还在发通知")
        finally:
            site.shutdown()
            site.server_close()
    return {"notifications": len(items), "kinds": kinds}


# ---------------------------------------------------------------- 钥匙串不进 WebView

def keychain_not_in_webview() -> dict:
    """API key 写进钥匙串：不落 provider.json / tools.json，任何页面接口都拿不到；js_api 只有白名单方法；
    删掉钥匙串条目后第三方出图报 remote_missing_key，且消息里没有 key。"""
    import integration
    import remote_image
    import server
    from packages import keychain

    key_llm = "sk-MARKERLLM-" + "a1b2c3d4" * 4
    key_img = "sk-MARKERIMG-" + "e5f6a7b8" * 4
    with isolated() as tmp:
        directory = os.path.join(tmp, "data")
        app = server.App(directory, timeout_sec=5, desktop_token=TOKEN)
        site, base = _serve(app)
        try:
            status, body = _http(base, "PUT", "/api/provider",
                                 {"base_url": "https://example.invalid/v1", "model": "m", "api_key": key_llm})
            if status != 200 or json.loads(body).get("key_source") != "keychain":
                fail(f"provider key 没进钥匙串：{status} {body[:160]}")
            status, body = _http(base, "PUT", "/api/tools/generate_image/config",
                                 {"values": {"remote_base_url": "https://example.invalid/v1", "remote_api_key": key_img}})
            if status != 200:
                fail(f"保存出图 key 失败：{status} {body[:160]}")
            view = json.loads(body)["values"]["remote_api_key"]
            if view.get("source") != "keychain" or not view.get("configured"):
                fail(f"出图 key 没进钥匙串：{view}")
            if keychain.get(keychain.PROVIDER_ENTRY) != key_llm or \
                    keychain.get(keychain.tool_entry("generate_image", "remote_api_key")) != key_img:
                fail("钥匙串里读不回刚保存的 key")
            disk = _all_bytes(directory)
            for key in (key_llm, key_img):
                if key.encode() in disk:
                    fail("API key 落进了数据目录（provider.json / tools.json）")
            if app.provider.resolve().get("api_key") != key_llm:
                fail("sidecar 读不到钥匙串里的 provider key")
            if remote_image.check_ready() is not None:
                fail(f"钥匙串里有出图 key 却报未就绪：{remote_image.check_ready()}")
            seen = []
            for path in ("/", "/api/provider", "/api/tools", "/api/tools/generate_image/config", "/api/storage",
                         "/api/identity", "/api/desktop/notifications", "/api/resources", "/api/egress",
                         "/static/app.js"):
                status, body = _http(base, "GET", path)
                seen.append((path, status))
                if key_llm in body or key_img in body or key_llm[-12:] in body or key_img[-12:] in body:
                    fail(f"{path} 把钥匙串里的 key 交给了页面")
            # WebView 能调用的 Python：只有白名单方法，令牌 / 地址都是私有属性。
            api = integration.DesktopApi(base, TOKEN, open_dialog=lambda: None, save_dialog=lambda name: None)
            surface = integration.public_surface(api)
            if sorted(surface) != sorted(integration.JS_METHODS):
                fail(f"js_api 暴露了白名单以外的东西：{sorted(surface)}")
            for name in surface:
                if re.search(r"key|secret|token|keychain|cred|password|provider|config", name, re.I):
                    fail(f"js_api 方法名可疑：{name}")
            for outcome in (api.capabilities(), api.pick_local_file("bad"), api.pick_local_file("r_" + "0" * 32),
                            api.save_egress_export("bad")):
                text = json.dumps(outcome)
                if TOKEN in text or key_llm in text or key_img in text:
                    fail("js_api 返回值里出现了令牌或 key")
            launcher = (DESKTOP_DIR / "launcher.py").read_text(encoding="utf-8")
            if "js_api=api" not in launcher or "DesktopApi(" not in launcher:
                fail("启动器没有用白名单 DesktopApi 作为 js_api")
            # 从钥匙串删掉 → remote_missing_key，消息里没有 key。
            keychain.delete(keychain.tool_entry("generate_image", "remote_api_key"))
            problem = remote_image.check_ready()
            if not problem or problem["error"]["code"] != "remote_missing_key":
                fail(f"删掉钥匙串里的出图 key 后应 remote_missing_key：{problem}")
            if key_img in json.dumps(problem, ensure_ascii=False):
                fail("remote_missing_key 消息里带了 key")
            keychain.delete(keychain.PROVIDER_ENTRY)
            status, body = _http(base, "GET", "/api/provider")
            if json.loads(body).get("configured"):
                fail("删掉钥匙串里的 provider key 后还显示已配置")
            status, body = _http(base, "PUT", "/api/provider",
                                 {"base_url": "https://example.invalid/v1", "model": "m", "clear_key": True})
            if status != 200:
                fail(f"清除 provider key 失败：{status}")
        finally:
            site.shutdown()
            site.server_close()
    return {"checked_paths": [p for p, _ in seen], "js_api": sorted(integration.JS_METHODS),
            "missing_key_code": "remote_missing_key"}


# ---------------------------------------------------------------- 默认加密

def default_encrypted() -> dict:
    """桌面默认 MAS_STORAGE_ENCRYPTION=on + 钥匙串：全新数据目录落盘是密文、主密钥不在文件里；
    已有明文数据时启动前预检拒绝并给迁移命令，不悄悄转换。"""
    import desktop_env
    import server

    marker = "MARKER_SESSION_BODY_4471"
    env: dict = {}
    desktop_env.apply_defaults(env)
    if env.get("MAS_STORAGE_ENCRYPTION") != "on" or env.get("MAS_KEYCHAIN") != "on":
        fail(f"桌面默认没有打开加密 / 钥匙串：{env}")
    with isolated() as tmp:
        fresh = os.path.join(tmp, "fresh")
        env = {key: os.environ[key] for key in ("MAS_KEYCHAIN", "MAS_MASTER_KEY_FILE")}
        desktop_env.apply_defaults(env)
        state = desktop_env.preflight(fresh, env)
        if not state["first_run"]:
            fail("全新数据目录应是首次启动")
        os.environ["MAS_STORAGE_ENCRYPTION"] = env["MAS_STORAGE_ENCRYPTION"]
        app = server.App(fresh, timeout_sec=5, desktop_token=TOKEN)
        public = app.storage.public()
        if public["mode"] != "encrypted" or public["key_source"] != "keychain":
            fail(f"全新安装没有加密或主密钥不在钥匙串：{public}")
        if os.path.exists(os.environ["MAS_MASTER_KEY_FILE"]):
            fail("主密钥落成了文件，应该在钥匙串里")
        user = app.identity.current()["user_id"]
        session = app.sessions.create(user)
        app.sessions.append_turn(session["session_id"], marker, "r_" + "9" * 32)
        if marker.encode() in _all_bytes(fresh):
            fail("全新安装的数据目录里有明文会话正文")
        if marker not in json.dumps(app.sessions.get(session["session_id"]), ensure_ascii=False):
            fail("加密后读不回会话")

        plain = os.path.join(tmp, "plain")
        os.environ["MAS_STORAGE_ENCRYPTION"] = "off"
        old = server.App(plain, timeout_sec=5)
        session = old.sessions.create(old.identity.current()["user_id"])
        old.sessions.append_turn(session["session_id"], marker, "r_" + "8" * 32)
        before = _all_bytes(plain)
        os.environ.pop("MAS_STORAGE_ENCRYPTION", None)
        env = {key: os.environ[key] for key in ("MAS_KEYCHAIN", "MAS_MASTER_KEY_FILE")}
        desktop_env.apply_defaults(env)
        try:
            desktop_env.preflight(plain, env)
        except desktop_env.PreflightError as exc:
            info = exc.public()
            if info["code"] != "storage_migration_required" or not any("storage_tool.py" in c and "encrypt" in c
                                                                        for c in info["commands"]):
                fail(f"明文数据目录的引导不对：{info}")
        else:
            fail("已有明文数据时桌面预检应拒绝并引导迁移")
        if _all_bytes(plain) != before:
            fail("预检悄悄改了明文数据目录")
    return {"fresh": "encrypted", "key_source": "keychain", "plaintext": "storage_migration_required"}


# ---------------------------------------------------------------- 原生文件对话框只得句柄

def file_handle_only() -> dict:
    """原生「打开文件」：Python 读字节走 /api/runs/<id>/content；JS 只拿 ok / 字节数，没有路径和文件名；
    提交给运行的只有字节和媒体类型。"""
    import chat_run
    import integration
    import server

    with isolated() as tmp:
        secret_name = "MARKER_private_diary_ZZ.png"
        path = os.path.join(tmp, secret_name)
        payload = b"\x89PNG\r\n\x1a\nMARKER-BYTES"
        with open(path, "wb") as handle:
            handle.write(payload)
        app = server.App(os.path.join(tmp, "data"), timeout_sec=5, desktop_token=TOKEN)
        site, base = _serve(app)
        try:
            run = chat_run.Run("s_" + "4" * 32, "m_" + "4" * 32)
            app.runs.add(run)
            run.begin_content_wait("file")
            api = integration.DesktopApi(base, TOKEN, open_dialog=lambda: path)
            result = api.pick_local_file(run.run_id)
            text = json.dumps(result, ensure_ascii=False)
            if result != {"ok": True, "bytes": len(payload)}:
                fail(f"原生选择文件的返回不对：{result}")
            if secret_name in text or tmp in text:
                fail("返回给页面的结果里有路径或文件名")
            got = run.wait_content(1)
            if not got or got.get("data") != payload or set(got) != {"kind", "data", "media_type"}:
                fail(f"提交给运行的内容不对：{sorted((got or {}).keys())}")
            again = api.pick_local_file(run.run_id)
            if again.get("ok") or again.get("code") != "not_waiting":
                fail(f"不在等待时应 not_waiting：{again}")
            cancelled = integration.DesktopApi(base, TOKEN, open_dialog=lambda: None).pick_local_file(run.run_id)
            if cancelled != {"ok": False, "code": "cancelled"}:
                fail(f"取消对话框的返回不对：{cancelled}")
            no_token = integration.DesktopApi(base, "", open_dialog=lambda: path)
            run.begin_content_wait("file")
            denied = no_token.pick_local_file(run.run_id)
            if denied.get("ok") or denied.get("code") != "desktop_token_required":
                fail(f"无令牌上传应被拒：{denied}")
        finally:
            site.shutdown()
            site.server_close()
    return {"returned": ["ok", "bytes"], "submitted": ["kind", "data", "media_type"]}



def update_egress_no_user_data() -> dict:
    """13.C / #76：更新检查默认关闭；开启后出站渠道 update 的载荷只有 version+platform，审计不含用户数据。"""
    import tempfile
    import desktop_config
    import updater
    from packages.contracts import events as contract
    from services.gateway import egress

    if "update" not in contract.EGRESS_CHANNELS:
        fail("缺少 update 出站渠道")
    policy = contract.EGRESS_POLICIES.get("update") or {}
    if set(policy.get("fields") or ()) != {"version", "platform"}:
        fail(f"update 渠道字段白名单不对：{policy.get('fields')}")

    markers = [
        "MARKER_USER_PROMPT_should_never_leave",
        "C:\\\\Users\\\\victim\\\\secret.txt",
        "sk-live-user-api-key-9f3a",
        "session_sess_deadbeef",
        "私人日记正文不应出站",
    ]

    with isolated() as tmp:
        data = os.path.join(tmp, "data")
        os.makedirs(data, exist_ok=True)
        # 1) 默认关闭：不建连接、不出站
        desktop_config.save({"wizard_completed": True, "auto_update": False, "update_url": "https://updates.example.invalid/check"}, data)
        log = os.path.join(tmp, "egress.jsonl")
        egress.configure(log)
        result = updater.maybe_check(data)
        if not result.get("skipped") or result.get("reason") != "auto_update_off":
            fail(f"自动更新默认应跳过：{result}")
        if os.path.isfile(log) and open(log, encoding="utf-8").read().strip():
            fail("自动更新关闭时不应写出站记录")

        # 2) 多字段载荷必须被策略拒绝
        bad = {"version": "0.1.0", "platform": "windows-x86_64", "prompt": markers[0], "api_key": markers[2]}
        ticket = egress.guard("update", "https://updates.example.invalid/check", bad)
        if not ticket.hit or ticket.reason_code != "egress_policy_denied":
            fail(f"带用户字段的更新载荷应被拒：{ticket.reason_code}")

        # 3) 合法载荷走 guard；用假 transport，断言发出的 body 只有白名单字段且不含 marker
        desktop_config.save({
            "wizard_completed": True,
            "auto_update": True,
            "update_url": "https://updates.example.invalid/check",
            "update_pubkey": "11" * 32,  # 占位；transport 返回的清单我们自己签不了就测到签名前
        }, data)
        sent = {}

        def transport(url, body, headers):
            sent["url"] = url
            sent["body"] = body
            sent["headers"] = headers
            # 返回无签名清单 → verify 失败，但出站 already planned/sent
            return 200, b'{"version":"9.9.9","url":"https://updates.example.invalid/pkg","sha256":"abcd","signature":"00"}'

        try:
            updater.check_for_update(data_dir=data, force=True, transport=transport)
            fail("无有效签名应失败")
        except updater.UpdateError as exc:
            if exc.code not in ("update_bad_signature", "update_bad_pubkey", "update_unsigned"):
                # 公钥长度不对也可能 update_bad_pubkey；占位 32 字节不是合法 ed25519 公钥
                if exc.code not in ("update_bad_pubkey", "update_bad_signature", "update_crypto_unavailable"):
                    fail(f"签名失败码不对：{exc.code}")

        if not sent.get("body"):
            fail("开启更新后应真正组包出站")
        payload = __import__("json").loads(sent["body"].decode("utf-8"))
        if set(payload) != {"version", "platform"}:
            fail(f"出站载荷字段不对：{payload}")
        blob = sent["body"] + sent["url"].encode()
        for marker in markers:
            if marker.encode("utf-8") in blob or marker in sent["body"].decode("utf-8", "replace"):
                fail(f"更新出站含用户数据标记：{marker}")

        # 审计记录里也不能有 marker
        records = []
        if os.path.isfile(log):
            for line in open(log, encoding="utf-8"):
                line = line.strip()
                if line:
                    records.append(__import__("json").loads(line))
        update_records = [r for r in records if r.get("channel") == "update"]
        if not update_records:
            fail("应有 update 渠道审计记录")
        for rec in update_records:
            dump = __import__("json").dumps(rec, ensure_ascii=False)
            for marker in markers:
                if marker in dump:
                    fail(f"审计记录含用户数据：{marker}")
            # 审计字段仍在 EGRESS_FIELDS 内
            extra = set(rec) - set(contract.EGRESS_FIELDS)
            if extra:
                fail(f"审计多了未登记字段：{extra}")
        return {
            "skipped_when_off": True,
            "policy_fields": sorted(policy["fields"]),
            "update_records": len(update_records),
            "payload_fields": sorted(payload),
        }



ALL = {
    "notify_no_body": notify_no_body,
    "keychain_not_in_webview": keychain_not_in_webview,
    "default_encrypted": default_encrypted,
    "file_handle_only": file_handle_only,
    "update_egress_no_user_data": update_egress_no_user_data,
}

if __name__ == "__main__":
    for name, func in ALL.items():
        print(name, json.dumps(func(), ensure_ascii=False))
