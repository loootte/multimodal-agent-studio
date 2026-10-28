"""工具配置：每个工具声明自己的配置项，用户在网页上改，存到数据目录里的 tools.json。

- 声明：字段名、类型（string/int/enum/path/secret）、默认值、是否必填、是否保密、说明。
- 存储：只存用户改过的值；保密字段对外只给掩码提示（同 provider 的 key_hint）。
- 环境变量（COMFY_*）仍然优先，查看时标成 source=env。
- 运行时每次读取（comfy_poc.load_config 调用覆盖钩子），改完下一次运行就生效，不用重启。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(WEB_DIR, "..", ".."))
POC = os.path.join(ROOT, "poc", "comfyui")
WORKFLOW_DIR = os.path.join(POC, "workflows")

TYPES = ("string", "int", "enum", "path", "secret")
_KINDS = ("image", "image_i2i", "video", "video_i2v")
_KIND_LABEL = {"image": "文生图", "image_i2i": "图生图", "video": "文生视频", "video_i2v": "图生视频"}


class ConfigError(ValueError):
    def __init__(self, errors: dict):
        super().__init__("；".join(f"{name}：{reason}" for name, reason in errors.items()))
        self.errors = errors


# 12.E #61：放宽隐私默认值的配置项。改成这些值时，界面要明确写出后果，请求里要带 acknowledge 才能保存，
# 每次变更（含改回去）记一条遥测安全事件 security.policy_changed（只有工具名、字段名、前后取值）。
GUARDED = {
    ("generate_image", "remote_local_prompt"): {
        "confirm": "本地提示词将发往第三方出图服务（正文不发给对话模型）。每一轮出图前仍会单独请你确认。",
    },
}


class AckRequired(ConfigError):
    """改成放宽隐私的取值，但请求里没有确认（acknowledge）。"""

    def __init__(self, errors: dict, warnings: dict):
        super().__init__(errors)
        self.warnings = warnings


def guarded_warnings(name: str) -> dict:
    """{字段名: {取值: 警示}}，给设置页显示。"""
    return {field: dict(values) for (tool, field), values in GUARDED.items() if tool == name}


def _ensure_poc() -> None:
    if POC not in sys.path:
        sys.path.insert(0, POC)


def _base_file() -> tuple[dict, str]:
    """现有的 poc 配置（COMFY_CONFIG → config.json → config.example.json），作为默认值迁移过来。"""
    candidates = [os.environ.get("COMFY_CONFIG"), os.path.join(POC, "config.json"), os.path.join(POC, "config.example.json")]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return data, os.path.dirname(os.path.abspath(path))
    return {}, POC


def _rel(base: str, value) -> str:
    """路径默认值统一写成相对 poc/comfyui 的形式（在里面时），方便在下拉里选中。"""
    if not isinstance(value, str) or not value:
        return ""
    full = value if os.path.isabs(value) else os.path.normpath(os.path.join(base, value))
    try:
        rel = os.path.relpath(full, POC)
    except ValueError:
        return full
    return full if rel.startswith("..") else rel.replace(os.sep, "/")


def _workflow_choices(suffix: str) -> list[str]:
    try:
        names = sorted(os.listdir(WORKFLOW_DIR))
    except OSError:
        return []
    return [f"workflows/{name}" for name in names if name.endswith(suffix)]


def comfy_fields() -> list[dict]:
    data, base = _base_file()
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    fields = [
        {"name": "base_url", "type": "string", "default": str(data.get("comfy_url") or "http://127.0.0.1:8188"),
         "required": True, "secret": False, "env": "COMFY_URL", "description": "ComfyUI 地址，例如 http://127.0.0.1:8188。"},
        {"name": "auth_token", "type": "secret", "default": "", "required": False, "secret": True,
         "description": "可选。ComfyUI 前面有鉴权代理时的令牌，只保存在本机；每个 ComfyUI 请求（提交、历史、下载、WebSocket、取消）都带 Authorization: Bearer。"},
        {"name": "output_dir", "type": "path", "default": _rel(base, data.get("output_dir") or "output"),
         "required": True, "secret": False, "env": "COMFY_OUTPUT_DIR", "must_exist": False,
         "description": "ComfyUI 的输出目录（相对路径按 poc/comfyui 解析）。"},
    ]
    for kind in _KINDS:
        section = data.get(kind) if isinstance(data.get(kind), dict) else {}
        label = _KIND_LABEL[kind]
        workflow = {"name": f"{kind}_workflow", "type": "path", "default": _rel(base, section.get("workflow")),
                    "required": kind == "image", "secret": False, "choices": _workflow_choices("_api.json"),
                    "description": f"{label}工作流模板（API 格式 JSON），可从 workflows/ 选择或填路径。"}
        if kind == "image":
            workflow["env"] = "COMFY_IMAGE_WORKFLOW"
        if kind == "video":
            workflow["env"] = "COMFY_VIDEO_WORKFLOW"
        fields.append(workflow)
        fields.append({"name": f"{kind}_fields", "type": "path", "default": _rel(base, section.get("fields")),
                       "required": kind == "image", "secret": False, "choices": _workflow_choices(".fields.json"),
                       "description": f"{label}字段对照（*.fields.json），要和模板匹配。"})
        fields.append({"name": f"{kind}_timeout_sec", "type": "int",
                       "default": int(section.get("timeout_sec") or (900 if kind.startswith("image") else 2700)),
                       "required": True, "secret": False, "min": 10, "max": 86400, "description": f"{label}超时（秒）。"})
    fields += [
        {"name": "default_aspect_ratio", "type": "enum", "default": str(defaults.get("aspect_ratio") or "1:1"),
         "choices": ["1:1", "16:9", "9:16"], "required": True, "secret": False,
         "description": "默认尺寸：用户没说画幅时用这个。"},
        {"name": "steps", "type": "int", "default": int(defaults.get("steps") or 0), "min": 0, "max": 200,
         "required": False, "secret": False, "description": "文生图采样步数，0 表示用模板里的值。"},
        {"name": "seed_policy", "type": "enum", "default": str(defaults.get("seed_policy") or "random"),
         "choices": ["random", "fixed"], "required": True, "secret": False,
         "description": "种子策略：random 每次随机；fixed 没指定种子时用下面的固定种子。"},
        {"name": "seed", "type": "int", "default": int(defaults.get("seed") or 0), "min": 0, "max": 2**32 - 1,
         "required": False, "secret": False, "description": "seed_policy=fixed 时使用的种子。"},
    ]
    return fields


def _packs():
    tools_dir = os.path.join(ROOT, "poc")
    if tools_dir not in sys.path:
        sys.path.append(tools_dir)
    _ensure_poc()
    from tools import packs

    return packs


def backend_declarations() -> dict:
    """共享后端（toolpacks/backends/*.json）。ComfyUI 的字段沿用 #28 的声明。"""
    out = {}
    for name, data in _packs().backends().items():
        fields = comfy_fields() if data.get("config") == "builtin:comfyui" else list(data.get("config") or [])
        out[name] = {"kind": "backend", "title": data.get("title") or name, "description": data.get("description") or "",
                     "fields": fields, "testable": bool(data.get("testable"))}
    return out


def tool_declarations() -> dict:
    """每个工具（按清单）。工具自己的配置字段来自清单 config。"""
    packs = _packs()
    out = {}
    for pack in packs.discover():
        fields = []
        for field in pack.manifest.get("config") or []:
            item = {"name": field["name"], "type": field["type"], "default": field["default"],
                    "required": bool(field["required"]), "secret": bool(field["secret"]),
                    "description": field.get("description") or "", "must_exist": field.get("must_exist", False)}
            for key in ("choices", "min", "max", "env"):
                if key in field:
                    item[key] = field[key]
            fields.append(item)
        out[pack.name] = {"kind": "tool", "title": pack.name, "description": pack.manifest["description"],
                          "fields": fields, "testable": False, "pack": packs.summary(pack)}
    return out


def declarations() -> dict:
    out = backend_declarations()
    out.update(tool_declarations())
    return out


def _hint(value: str) -> str:
    return value[-4:] if len(value) >= 8 else ""


class ToolConfigStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    # ---- 文件 ----
    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def saved(self, name: str) -> dict:
        section = self._load().get(name)
        return dict(section) if isinstance(section, dict) else {}

    # ---- 视图 ----
    def effective(self, name: str) -> dict:
        """运行时用的值（含保密原值）：env > 已保存 > 默认。"""
        decl = declarations()[name]
        saved = self.saved(name)
        values = {}
        for field in decl["fields"]:
            env = field.get("env")
            if env and os.environ.get(env):
                value = os.environ[env]
            elif field["name"] in saved:
                value = saved[field["name"]]
            else:
                value = field["default"]
            values[field["name"]] = value
        return values

    def public(self, name: str) -> dict:
        decl = declarations()[name]
        saved = self.saved(name)
        values = {}
        missing = []
        for field in decl["fields"]:
            key = field["name"]
            env = field.get("env")
            if env and os.environ.get(env):
                source, value = "env", os.environ[env]
            elif key in saved:
                source, value = "saved", saved[key]
            else:
                source, value = "default", field["default"]
            entry = {"source": source}
            if field["secret"]:
                entry["configured"] = bool(value)
                entry["hint"] = _hint(str(value or ""))
            else:
                entry["value"] = value
            if field["required"] and value in ("", None):
                missing.append(key)
            values[key] = entry
        return {"name": name, "title": decl["title"], "description": decl["description"],
                "fields": decl["fields"], "values": values, "complete": not missing, "missing": missing,
                "testable": decl.get("testable", False), "kind": decl.get("kind", "backend"),
                **({"warnings": guarded_warnings(name)} if guarded_warnings(name) else {}),
                **({"pack": self._pack_view(decl["pack"])} if decl.get("pack") else {})}

    def _pack_view(self, pack: dict) -> dict:
        view = dict(pack)
        status = []
        backends = backend_declarations()
        for name in pack.get("backends") or []:
            if name in backends:
                missing = self.public(name)["missing"]
                status.append({"name": name, "title": backends[name]["title"], "complete": not missing, "missing": missing})
            else:
                status.append({"name": name, "title": name, "complete": False, "missing": ["(没有这个后端)"]})
        view["backend_status"] = status
        return view

    def listing(self) -> dict:
        """下拉框：工具按实际工具名列出；后端单独一组。"""
        backends = []
        for name, decl in backend_declarations().items():
            view = self.public(name)
            backends.append({"name": name, "title": decl["title"], "description": decl["description"],
                             "complete": view["complete"], "missing": view["missing"]})
        complete = {item["name"]: item["complete"] for item in backends}
        tools = []
        for name, decl in tool_declarations().items():
            view = self.public(name)
            pack = decl["pack"]
            ready = view["complete"] and all(complete.get(b, False) for b in pack["backends"]) \
                and not pack["missing"] and not pack["denied"]
            tools.append({"name": name, "title": name, "description": decl["description"],
                          "version": pack["version"], "builtin": pack["builtin"], "exposed": pack["exposed"],
                          "permissions": pack["permissions"], "backends": pack["backends"],
                          "configurable": pack["configurable"], "complete": view["complete"],
                          "missing": view["missing"], "ready": ready})
        return {"tools": tools, "backends": backends}

    # ---- 写入 ----
    def update(self, name: str, values: dict, reset: bool = False, clear: list | None = None,
               acknowledge: list | None = None) -> dict:
        """保存配置。返回公开视图；视图里的 changed_guarded 列出这次被改动的受保护字段（给调用方记审计）。"""
        if name not in declarations():
            raise KeyError(name)
        if not isinstance(values, dict):
            raise ConfigError({"values": "必须是对象。"})
        decl = declarations()[name]
        by_name = {field["name"]: field for field in decl["fields"]}
        with self._lock:
            data = self._load()
            section = {} if reset else dict(data.get(name) or {})
            errors = {}
            for key, raw in values.items():
                field = by_name.get(key)
                if field is None:
                    errors[key] = "没有这个配置项。"
                    continue
                if raw is None:
                    section.pop(key, None)
                    continue
                if field["secret"] and raw == "":
                    continue  # 留空 = 保留原值
                value, problem = _coerce(field, raw)
                if problem:
                    errors[key] = problem
                    continue
                if value == field["default"] and not field["secret"]:
                    section.pop(key, None)
                else:
                    section[key] = value
            for key in clear or []:
                if key in by_name and by_name[key]["secret"]:
                    section.pop(key, None)
            merged = {}
            for field in decl["fields"]:
                merged[field["name"]] = section.get(field["name"], field["default"])
            for key, problem in _cross_check(name, merged).items():
                errors.setdefault(key, problem)
            if errors:
                raise ConfigError(errors)
            before = dict(data.get(name) or {})
            changed, unacked = [], {}
            acked = set(acknowledge) if isinstance(acknowledge, list) else set()
            for field, warnings in guarded_warnings(name).items():
                default = by_name[field]["default"] if field in by_name else None
                old, new = before.get(field, default), section.get(field, default)
                if old == new:
                    continue
                changed.append({"field": field, "from": old, "to": new})
                if new in warnings and field not in acked:
                    unacked[field] = warnings[new]
            if unacked:
                raise AckRequired({key: "要先确认：" + text for key, text in unacked.items()}, unacked)
            if section:
                data[name] = section
            else:
                data.pop(name, None)
            self._save(data)
        view = self.public(name)
        view["changed_guarded"] = changed
        return view


def _resolve(value: str) -> str:
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(POC, value))


def _coerce(field: dict, raw):
    kind = field["type"]
    if kind == "int":
        if isinstance(raw, bool):
            return None, "必须是整数。"
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None, "必须是整数。"
        if str(raw).strip() not in (str(value), f"{value}.0") and not isinstance(raw, int):
            return None, "必须是整数。"
        if "min" in field and value < field["min"]:
            return None, f"不能小于 {field['min']}。"
        if "max" in field and value > field["max"]:
            return None, f"不能大于 {field['max']}。"
        return value, None
    if not isinstance(raw, str):
        return None, "必须是文字。"
    value = raw.strip()
    if field["required"] and not value:
        return None, "必填。"
    if kind == "enum" and value not in field.get("choices", []):
        return None, f"只能是 {'、'.join(field.get('choices', []))}。"
    if field["name"] == "base_url" and value:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None, "要以 http:// 或 https:// 开头，并带主机名。"
        value = value.rstrip("/")
    if kind == "path" and value and field.get("must_exist", True) and not os.path.isfile(_resolve(value)):
        return None, f"文件不存在：{_resolve(value)}"
    return value, None


def _cross_check(name: str, merged: dict) -> dict:
    """模板和字段对照要匹配。"""
    if name == "character_image":
        import character

        return character.check_config(merged)
    if name != "comfyui":
        return {}
    _ensure_poc()
    import template_fill

    errors = {}
    for kind in _KINDS:
        workflow, fields = merged.get(f"{kind}_workflow"), merged.get(f"{kind}_fields")
        if not workflow and not fields:
            continue
        if not workflow or not fields:
            errors[f"{kind}_workflow" if not workflow else f"{kind}_fields"] = "模板和字段对照要一起填。"
            continue
        try:
            with open(_resolve(workflow), encoding="utf-8") as handle:
                graph = json.load(handle)
            with open(_resolve(fields), encoding="utf-8") as handle:
                mapping = json.load(handle)
            template_fill.validate_fields(graph, mapping)
        except (OSError, ValueError) as exc:
            errors[f"{kind}_fields"] = f"读不了：{exc}"
        except Exception as exc:  # TemplateError 等
            errors[f"{kind}_fields"] = f"和模板不匹配：{getattr(exc, 'message', None) or exc}"
    return errors


# ---- 运行时覆盖：每次 load_config 都重新读 tools.json ----

_ACTIVE: ToolConfigStore | None = None


def comfy_overlay() -> dict | None:
    store = _ACTIVE
    if store is None:
        return None
    saved = store.saved("comfyui")
    if not saved:
        return None
    overlay: dict = {}
    if saved.get("base_url"):
        overlay["comfy_url"] = saved["base_url"]
    if saved.get("auth_token"):
        overlay["auth_token"] = saved["auth_token"]
    if saved.get("output_dir"):
        overlay["output_dir"] = _resolve(saved["output_dir"])
    for kind in _KINDS:
        section = {}
        if saved.get(f"{kind}_workflow"):
            section["workflow"] = _resolve(saved[f"{kind}_workflow"])
        if saved.get(f"{kind}_fields"):
            section["fields"] = _resolve(saved[f"{kind}_fields"])
        if f"{kind}_timeout_sec" in saved:
            section["timeout_sec"] = int(saved[f"{kind}_timeout_sec"])
        if section:
            overlay[kind] = section
    defaults = {}
    for key, target in (("default_aspect_ratio", "aspect_ratio"), ("steps", "steps"), ("seed_policy", "seed_policy"), ("seed", "seed")):
        if key in saved:
            defaults[target] = saved[key]
    if defaults:
        overlay["defaults"] = defaults
    return overlay


def install(store: ToolConfigStore) -> None:
    global _ACTIVE
    _ACTIVE = store
    _ensure_poc()
    import comfy_poc

    comfy_poc.CONFIG_OVERLAY = comfy_overlay
    import comfy_client

    packs = _packs()
    from tools import sandbox

    # 12.D：tools.json 所在的数据目录（会话、句柄、审计都在这里）对第三方包的沙箱一律不可读写。
    sandbox.DATA_DIR = os.path.dirname(os.path.abspath(store.path))
    packs.CONFIG_SOURCE = lambda name: store.effective(name) if _ACTIVE is store and name in tool_declarations() else {}
    comfy_client.TOKEN_SOURCE = lambda: store.effective("comfyui").get("auth_token") if _ACTIVE is store else None


# ---- 测试连接 ----

def _get_json(url: str, token: str, timeout: float = 4.0):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def test_connection(store: ToolConfigStore, name: str) -> dict:
    if name != "comfyui":
        return {"ok": False, "code": "not_testable", "message": "这个工具没有测试连接。"}
    values = store.effective(name)
    base = str(values.get("base_url") or "").rstrip("/")
    token = str(values.get("auth_token") or "")
    try:
        try:
            _get_json(base + "/system_stats", token)
        except urllib.error.HTTPError:
            _get_json(base + "/queue", token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return {"ok": False, "code": "unreachable", "base_url": base, "message": f"连不上 ComfyUI（{base}）：{reason}"}
    try:
        info = _get_json(base + "/object_info", token, timeout=15.0)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "code": "unreachable", "base_url": base, "message": f"ComfyUI 能连上，但读不到节点列表：{exc}"}
    known = set(info) if isinstance(info, dict) else set()
    missing, checked = {}, []
    for kind in _KINDS:
        workflow = values.get(f"{kind}_workflow")
        if not workflow:
            continue
        try:
            with open(_resolve(str(workflow)), encoding="utf-8") as handle:
                graph = json.load(handle)
        except (OSError, ValueError):
            missing[kind] = ["(模板读不了)"]
            continue
        checked.append(kind)
        classes = sorted({node.get("class_type") for node in graph.values() if isinstance(node, dict) and node.get("class_type")})
        lack = [item for item in classes if item not in known]
        if lack:
            missing[kind] = lack
    if missing:
        lines = "；".join(f"{_KIND_LABEL[k]}缺 {', '.join(v)}" for k, v in missing.items())
        return {"ok": False, "code": "missing_nodes", "base_url": base, "missing": missing, "message": f"ComfyUI 能连上，但缺少节点：{lines}"}
    return {"ok": True, "code": "ok", "base_url": base, "checked": checked, "message": f"已连上 {base}，所选工作流的节点都在。"}
