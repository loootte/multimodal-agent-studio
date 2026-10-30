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


# ---------------------------------------------------------------- 13.D 桌面壳加固

_INJECTED = "https://attacker.example/steal?d=MARKER_LOCAL_PROMPT_777#frag"


def _events(base: str, kind: str) -> list[dict]:
    status, body = _http(base, "GET", "/api/telemetry?limit=5000")
    if status != 200:
        fail(f"读遥测失败：{status}")
    return [item for item in json.loads(body)["events"] if item.get("kind") == kind]


def _no_marker(items, what: str) -> None:
    dump = json.dumps(items, ensure_ascii=False)
    for bad in ("MARKER_LOCAL_PROMPT_777", "/steal", "d=", "#frag", TOKEN):
        if bad in dump:
            fail(f"{what} 审计里出现了完整地址 / 查询串 / 令牌：{bad}")


def navigation_blocked() -> dict:
    """13.D：注入「跳转到外部地址」的工具结果：WebView 不导航（拉回 sidecar），记 security.desktop_navigation_blocked；
    审计只有 scheme + 主机名；页面 CSP 收紧；Tauri 壳有同源导航白名单、release 关 devtools。"""
    import server
    import shell_guard

    with isolated(MAS_DESKTOP_RELEASE=None, MAS_DESKTOP_DEVTOOLS=None) as tmp:
        app = server.App(os.path.join(tmp, "data"), timeout_sec=5, desktop_token=TOKEN)
        site, base = _serve(app)
        try:
            home = base + "/"
            allowed = [home, base + "/static/app.js", base + "/?x=1", "about:blank", "blob:" + base + "/0b1c"]
            port = int(base.rsplit(":", 1)[1])
            blocked = [_INJECTED, "http://127.0.0.1:%d/" % (port + 1 if port < 65535 else port - 1),
                       "http://localhost:%d/" % port, "https://127.0.0.1:%d/" % port, "file:///etc/passwd",
                       "data:text/html,hi", "javascript:alert(1)", "http://127.0.0.1.attacker.example/",
                       "http://user@127.0.0.1:%d@attacker.example/" % port, "", None, 42]
            for url in allowed:
                if not shell_guard.navigation_allowed(url, home):
                    fail(f"sidecar 同源地址被误拦：{url}")
            for url in blocked:
                if shell_guard.navigation_allowed(url, home):
                    fail(f"不该放行的导航：{url!r}")
            audit = shell_guard.Audit(base, TOKEN)
            went: list = []
            guard = shell_guard.NavigationGuard(base, audit, went.append)
            if not guard.check(home) or went:
                fail("本机页面不该被拦")
            if guard.check(_INJECTED):
                fail("注入的外部地址没有被拦")
            if went != [home]:
                fail(f"被拦后应拉回 sidecar 首页：{went}")
            events = _events(base, "security.desktop_navigation_blocked")
            if len(events) != 1 or events[0].get("host") != "attacker.example" or events[0].get("scheme") != "https" \
                    or events[0].get("reason") != "not_sidecar_origin":
                fail(f"导航拦截审计不对：{events}")
            _no_marker(events, "导航拦截")
            # 审计接口：只认壳进程的请求头令牌；Cookie（WebView 脚本能带）、未知事件名都拒。
            req = urllib.request.Request(base + "/api/desktop/security_event", method="POST",
                                         data=json.dumps({"event": "security.desktop_navigation_blocked"}).encode(),
                                         headers={"Cookie": f"mas_desktop_token={TOKEN}",
                                                  "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5)
                fail("Cookie 也能写桌面安全审计（WebView 脚本可伪造）")
            except urllib.error.HTTPError as exc:
                if exc.code != 403:
                    fail(f"Cookie 写审计应 403：{exc.code}")
            status, _ = _http(base, "POST", "/api/desktop/security_event", {"event": "security.config_changed"})
            if status != 400:
                fail(f"白名单以外的事件名应 400：{status}")
            status, _ = _http(base, "POST", "/api/desktop/security_event",
                              {"event": "security.desktop_navigation_blocked", "url": _INJECTED, "host": "a b"})
            _no_marker(_events(base, "security.desktop_navigation_blocked"), "审计接口")
            # 页面 CSP：不许插件、改 base、表单外发、被外站嵌入。
            req = urllib.request.Request(home, headers={"X-MAS-Desktop-Token": TOKEN})
            with urllib.request.urlopen(req, timeout=5) as resp:
                csp = resp.headers.get("Content-Security-Policy") or ""
            for part in ("default-src 'self'", "script-src 'self'", "object-src 'none'", "base-uri 'none'",
                         "form-action 'self'", "frame-ancestors 'self'"):
                if part not in csp:
                    fail(f"页面 CSP 缺 {part}：{csp}")
        finally:
            site.shutdown()
            site.server_close()
        # devtools：默认关；开发时 MAS_DESKTOP_DEVTOOLS=1 才开；发布构建（RELEASE 标记 / 环境变量）一律关。
        fake_root = Path(tmp) / "repo"
        (fake_root / "apps" / "desktop").mkdir(parents=True)
        if shell_guard.devtools_enabled(fake_root):
            fail("devtools 默认应关")
        os.environ["MAS_DESKTOP_DEVTOOLS"] = "1"
        if not shell_guard.devtools_enabled(fake_root):
            fail("开发模式 MAS_DESKTOP_DEVTOOLS=1 应能开 devtools")
        (fake_root / "apps" / "desktop" / "RELEASE").write_text("release", encoding="utf-8")
        if shell_guard.devtools_enabled(fake_root):
            fail("发布构建不该开 devtools")
    launcher = (DESKTOP_DIR / "launcher.py").read_text(encoding="utf-8")
    for needle in ("NavigationGuard(", "events.loaded", "debug=shell_guard.devtools_enabled("):
        if needle not in launcher:
            fail(f"启动器没接上导航守卫 / devtools 开关：{needle}")
    build = (DESKTOP_DIR / "packaging" / "windows" / "build.ps1").read_text(encoding="utf-8")
    if "apps\\desktop\\RELEASE" not in build:
        fail("Windows 打包没写发布标记（devtools 关不掉）")
    lib = (DESKTOP_DIR / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    for needle in (".on_navigation(", "navigation_allowed(", "security.desktop_navigation_blocked",
                   ".devtools(cfg!(debug_assertions))"):
        if needle not in lib:
            fail(f"Tauri 壳缺导航白名单 / devtools 设置：{needle}")
    cargo = (DESKTOP_DIR / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    if re.search(r'tauri\s*=.*"devtools"', cargo):
        fail("Tauri 依赖开了 devtools 特性（release 也会有 devtools）")
    conf = json.loads((DESKTOP_DIR / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    csp = ((conf.get("app") or {}).get("security") or {}).get("csp")
    if not csp or "default-src 'self'" not in csp or "object-src 'none'" not in csp:
        fail(f"Tauri CSP 没收紧：{csp}")
    return {"blocked": len(blocked), "audit_fields": sorted(events[0]), "devtools_release": False}


def ipc_denied() -> dict:
    """13.D：js_api 白名单以外的调用（含直接走 pywebview 桥的）全部拒绝并记 security.desktop_ipc_denied；
    页面被导航到外站时，白名单方法也拒绝；Tauri 壳不开任何插件权限、不给远程页面 IPC。"""
    import integration
    import server
    import shell_guard

    with isolated() as tmp:
        app = server.App(os.path.join(tmp, "data"), timeout_sec=5, desktop_token=TOKEN)
        site, base = _serve(app)
        try:
            page = {"url": base + "/"}
            opened: list = []
            api = integration.DesktopApi(base, TOKEN, open_dialog=lambda: None, save_dialog=lambda n: None,
                                         audit=shell_guard.Audit(base, TOKEN), current_url=lambda: page["url"],
                                         open_folder=lambda: opened.append(1) or True)
            if sorted(integration.public_surface(api)) != sorted(integration.JS_METHODS):
                fail(f"js_api 暴露面不是白名单：{sorted(integration.public_surface(api))}")
            attempts = ("read_keychain", "keychain_get", "get_token", "set_config", "evaluate_js", "run_shell",
                        "open_path")
            for name in attempts:
                result = getattr(api, name)("provider")
                if result != {"ok": False, "code": "ipc_denied"}:
                    fail(f"白名单外的 IPC 没被拒：{name} -> {result}")
            for name in ("_token", "_base", "__secret__"):
                try:
                    getattr(api, name + "_nope")
                    fail(f"下划线属性不该被兜底：{name}")
                except AttributeError:
                    pass
            if api.open_data_dir() != {"ok": True} or opened != [1]:
                fail("白名单里的「打开数据目录」应可用且不回路径")
            if not api.capabilities().get("native_file_dialog"):
                fail("本机页面调用白名单方法应可用")
            page["url"] = _INJECTED
            for call in (lambda: api.capabilities(), lambda: api.pick_local_file("r_" + "1" * 32),
                         lambda: api.save_egress_export(), lambda: api.open_external("https://example.org/"),
                         lambda: api.open_data_dir()):
                result = call()
                if result != {"ok": False, "code": "ipc_denied"}:
                    fail(f"外站页面调用白名单方法没被拒：{result}")
            if opened != [1]:
                fail("外站页面打开了数据目录")
            events = _events(base, "security.desktop_ipc_denied")
            by_reason: dict = {}
            for item in events:
                by_reason.setdefault(item.get("reason"), []).append(item.get("method"))
            if sorted(by_reason.get("not_whitelisted") or []) != sorted(attempts):
                fail(f"越权 IPC 没逐条记录：{by_reason}")
            if sorted(by_reason.get("foreign_origin") or []) != sorted(
                    ["capabilities", "pick_local_file", "save_egress_export", "open_external", "open_data_dir"]):
                fail(f"外站页面调用没逐条记录：{by_reason}")
            _no_marker(events, "IPC 拒绝")
            for item in events:
                if item.get("reason") == "foreign_origin" and item.get("host") != "attacker.example":
                    fail(f"外站调用审计缺主机名：{item}")
        finally:
            site.shutdown()
            site.server_close()
    caps = json.loads((DESKTOP_DIR / "src-tauri" / "capabilities" / "default.json").read_text(encoding="utf-8"))
    if caps.get("permissions") or caps.get("remote"):
        fail(f"Tauri capabilities 应不开任何权限、不给远程页面 IPC：{caps}")
    lib = (DESKTOP_DIR / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    handlers = re.findall(r"generate_handler!\[([^\]]*)\]", lib)
    if [h.strip() for h in ",".join(handlers).split(",") if h.strip()] != ["desktop_info"]:
        fail(f"Tauri 自定义命令不是白名单：{handlers}")
    return {"denied": len(events), "whitelist": sorted(integration.JS_METHODS)}


def external_link_confirm() -> dict:
    """13.D：模型 / 工具结果里的链接：WebView 不打开；宿主确认卡（固定模板 + 完整地址）确认后才交给系统浏览器；
    javascript: / file: 等直接拒；取消不打开；都记 security.desktop_external_link_confirm（只有 scheme + 主机）。"""
    import integration
    import server
    import shell_guard

    with isolated() as tmp:
        app = server.App(os.path.join(tmp, "data"), timeout_sec=5, desktop_token=TOKEN)
        site, base = _serve(app)
        try:
            audit = shell_guard.Audit(base, TOKEN)
            asked: list = []
            opened: list = []
            answer = {"yes": False}

            def confirm(title, body):
                asked.append((title, body))
                return answer["yes"]

            links = shell_guard.ExternalLinks(confirm, opened.append, audit)
            api = integration.DesktopApi(base, TOKEN, audit=audit, links=links, current_url=lambda: base + "/")
            # 取消：不打开
            if api.open_external(_INJECTED) != {"ok": False, "code": "cancelled"} or opened:
                fail("用户取消后不该打开外链")
            title, body = asked[-1]
            if _INJECTED not in body or "attacker.example" not in body or title != "打开外部链接？":
                fail(f"确认卡没显示完整地址 / 主机：{title} {body}")
            if body != shell_guard.confirm_text(shell_guard.external_target(_INJECTED))[1]:
                fail("确认卡文字不是宿主模板")
            # 确认：只用系统浏览器打开同一个地址
            answer["yes"] = True
            if api.open_external(_INJECTED) != {"ok": True} or opened != [_INJECTED]:
                fail(f"确认后应用系统浏览器打开原地址：{opened}")
            # 不合法的链接：不弹确认卡、不打开
            before = len(asked)
            for bad in ("javascript:alert(1)", "file:///C:/Users/victim/secret.txt", "data:text/html,x",
                        "https://user:pw@evil.example/", "https://evil.example/\n", "https://e.example/\"x",
                        "ftp://e.example/", "x" * 3000, None, 7):
                result = api.open_external(bad)
                if result != {"ok": False, "code": "link_not_allowed"}:
                    fail(f"不合法外链没被拒：{bad!r} -> {result}")
            if len(asked) != before or opened != [_INJECTED]:
                fail("不合法外链弹了确认卡或被打开")
            if api.open_external("mailto:someone@example.org") != {"ok": True}:
                fail("mailto 外链确认后应可打开")
            events = _events(base, "security.desktop_external_link_confirm")
            outcomes = [item.get("outcome") for item in events]
            if outcomes[:2] != ["declined", "confirmed"] or outcomes.count("rejected") != 10 \
                    or outcomes[-1] != "confirmed":
                fail(f"外链确认审计不对：{outcomes}")
            _no_marker(events, "外链确认")
        finally:
            site.shutdown()
            site.server_close()
    js = (ROOT / "apps" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    for needle in ('addEventListener("click", guardDesktopLinks, true)', "native.open_external(",
                   "window.open = function", "event.preventDefault()"):
        if needle not in js:
            fail(f"界面没有拦截外链交给宿主确认：{needle}")
    md = (ROOT / "apps" / "web" / "static" / "markdown.js").read_text(encoding="utf-8")
    if "https?:\\/\\/|mailto:" not in md:
        fail("markdown 链接白名单变了")
    return {"outcomes": sorted(set(outcomes)), "confirm_title": "打开外部链接？"}


def token_env_only() -> dict:
    """13.D：桌面令牌只经环境变量 MAS_DESKTOP_TOKEN 传给 server，不进命令行（进程列表可见）；--desktop-token 已停用。"""
    import server

    src = (DESKTOP_DIR / "host.py").read_text(encoding="utf-8")
    if '"--desktop-token"' in src:
        fail("host.py 仍把令牌放在命令行里")
    rs = (DESKTOP_DIR / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    if '.args(["-c"' in rs or "{token}'" in rs:
        fail("Tauri 壳仍把令牌拼进子进程命令行")
    if "X-MAS-Desktop-Token: {token}" not in rs:
        fail("Tauri 壳应只在请求头里带令牌")
    try:
        server._resolve_desktop_token("x" * 64)
        fail("--desktop-token 应被拒")
    except SystemExit as exc:
        if "MAS_DESKTOP_TOKEN" not in str(exc):
            fail(f"--desktop-token 的拒绝提示不对：{exc}")
    saved = os.environ.get("MAS_DESKTOP_TOKEN")
    try:
        os.environ["MAS_DESKTOP_TOKEN"] = "e" * 64
        if server._resolve_desktop_token() != "e" * 64:
            fail("环境变量令牌没生效")
        os.environ.pop("MAS_DESKTOP_TOKEN")
        if server._resolve_desktop_token() is not None:
            fail("没令牌时应是 Web 模式")
    finally:
        if saved is None:
            os.environ.pop("MAS_DESKTOP_TOKEN", None)
        else:
            os.environ["MAS_DESKTOP_TOKEN"] = saved
    return {"token_via": "env", "cli_flag": "rejected"}


ALL = {
    "notify_no_body": notify_no_body,
    "keychain_not_in_webview": keychain_not_in_webview,
    "default_encrypted": default_encrypted,
    "file_handle_only": file_handle_only,
    "update_egress_no_user_data": update_egress_no_user_data,
    "navigation_blocked": navigation_blocked,
    "ipc_denied": ipc_denied,
    "external_link_confirm": external_link_confirm,
    "token_env_only": token_env_only,
}

if __name__ == "__main__":
    for name, func in ALL.items():
        print(name, json.dumps(func(), ensure_ascii=False))
