"""工具包（tool pack）：每个工具一份清单 tool.json，运行时只从清单注册工具。

目录：
- toolpacks/builtin/<name>/tool.json   内置工具，随仓库发布。
- toolpacks/backends/<name>.json       共享后端（例如 ComfyUI），工具在 requires.backends 里引用。
- toolpacks/installed/<name>/          用户安装的工具包（可用 MAS_TOOLPACKS_DIR 改位置）。

清单字段见 docs/tool-packs.md。这里负责：读清单、校验、发现、安装、卸载、导出。
配置值和密钥不在包里：它们存在数据目录的 tools.json，导出时不会带走。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PACKS = os.path.join(ROOT, "toolpacks")
BUILTIN = os.path.join(PACKS, "builtin")
BACKENDS = os.path.join(PACKS, "backends")

SCHEMA = "mas.tool/1"
BACKEND_SCHEMA = "mas.backend/1"
MANIFEST = "tool.json"
GRANTS = ".granted.json"
PERMISSIONS = ("read_local_content", "network", "write_files", "send_local_to_model")
CONFIRM = ("always", "never", "by_args")
FIELD_TYPES = ("string", "int", "enum", "path", "secret")
BLOCKED_EXTERNAL = {"prompt", "text", "body", "content", "bytes", "path", "filename"}
PARAMETER_SOURCES = ("comfyui.styles",)
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_FIELDS = {"schema", "name", "version", "description", "order", "parameters", "mutating", "confirm",
           "confirm_args", "entry", "config", "requires", "permissions", "parameter_sources", "mcp"}

# 由 apps/web/tool_config.install 设置：name -> 生效配置（含密钥）。没有时用清单默认值。
CONFIG_SOURCE = None
_modules: dict = {}


class PackError(Exception):
    """稳定错误码 + 给人看的说明。"""

    def __init__(self, code: str, message: str, problems: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.problems = problems or []

    def payload(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.problems:
            out["problems"] = self.problems
        return out


def installed_dir() -> str:
    return os.environ.get("MAS_TOOLPACKS_DIR") or os.path.join(PACKS, "installed")


# ---------- 校验 ----------

def validate_manifest(data, *, builtin: bool = False) -> list[str]:
    """返回问题列表；空列表表示合格。"""
    if not isinstance(data, dict):
        return ["清单必须是 JSON 对象。"]
    problems = []
    if data.get("schema") != SCHEMA:
        problems.append(f"schema 必须是 {SCHEMA}。")
    extra = sorted(set(data) - _FIELDS)
    if extra:
        problems.append(f"不认识的字段：{', '.join(extra)}。")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        problems.append("name 必须是小写字母开头的 2-48 位 [a-z0-9_]。")
    if not isinstance(data.get("version"), str) or not _VERSION.match(data.get("version") or ""):
        problems.append("version 必须是 主.次.修 三段数字。")
    if not isinstance(data.get("description"), str) or not data.get("description", "").strip():
        problems.append("description 必填。")
    order = data.get("order", 1000)
    if not isinstance(order, int) or isinstance(order, bool):
        problems.append("order 必须是整数。")
    params = data.get("parameters")
    if params is not None:
        if not isinstance(params, dict) or params.get("type") != "object":
            problems.append("parameters 必须是 type=object 的 JSON Schema，或 null（不提供给模型）。")
        else:
            if params.get("additionalProperties") is not False:
                problems.append("parameters 必须写 additionalProperties: false。")
            props = params.get("properties") or {}
            leaked = BLOCKED_EXTERNAL.intersection(props)
            if leaked:
                problems.append(f"给模型的参数里不能有 {'、'.join(sorted(leaked))}（本地正文不经过模型）。")
            for key, source in (data.get("parameter_sources") or {}).items():
                if key not in props or source not in PARAMETER_SOURCES:
                    problems.append(f"parameter_sources.{key} 不可用。")
    if not isinstance(data.get("mutating"), bool):
        problems.append("mutating 必须是 true/false。")
    confirm = data.get("confirm")
    if confirm not in CONFIRM:
        problems.append(f"confirm 只能是 {'、'.join(CONFIRM)}。")
    if confirm == "never" and data.get("mutating") is True and not builtin:
        problems.append("改外部状态的工具不能 confirm=never。")
    if confirm == "by_args":
        args = data.get("confirm_args")
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            problems.append("confirm=by_args 时 confirm_args 必须是非空的参数名列表。")
    entry = data.get("entry")
    if not isinstance(entry, dict) or entry.get("kind") not in ("python", "host"):
        problems.append("entry.kind 只能是 python 或 host。")
    elif entry["kind"] == "host" and not builtin:
        problems.append("只有内置工具可以用 entry.kind=host。")
    elif entry["kind"] == "python":
        module = entry.get("module")
        if not isinstance(module, str) or not module.endswith(".py"):
            problems.append("entry.module 必须是 .py 文件。")
        elif not builtin and (os.path.isabs(module) or ".." in module.replace("\\", "/").split("/")):
            problems.append("entry.module 必须是包内的相对路径。")
    if entry and isinstance(entry, dict) and entry.get("kind") == "python" and params is None and not builtin:
        problems.append("第三方工具必须声明 parameters。")
    config = data.get("config", [])
    if not isinstance(config, list):
        problems.append("config 必须是列表。")
    else:
        seen = set()
        for field in config:
            problems.extend(_field_problems(field, seen))
    requires = data.get("requires", {})
    if not isinstance(requires, dict):
        problems.append("requires 必须是对象。")
    else:
        for key in ("backends", "python"):
            value = requires.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                problems.append(f"requires.{key} 必须是字符串列表。")
    perms = data.get("permissions")
    if not isinstance(perms, dict) or set(perms) - set(PERMISSIONS) or not all(isinstance(v, bool) for v in perms.values()):
        problems.append(f"permissions 只能包含布尔值的 {'、'.join(PERMISSIONS)}。")
    elif perms.get("send_local_to_model"):
        problems.append("send_local_to_model 必须是 false：本地内容不能交给第三方模型。")
    return problems


def _field_problems(field, seen: set) -> list[str]:
    if not isinstance(field, dict):
        return ["config 里每一项必须是对象。"]
    out = []
    name = field.get("name")
    if not isinstance(name, str) or not _NAME.match(name) or name in seen:
        out.append(f"config 字段名不合格或重复：{name!r}。")
    seen.add(name)
    if field.get("type") not in FIELD_TYPES:
        out.append(f"config.{name}.type 只能是 {'、'.join(FIELD_TYPES)}。")
    for key in ("default", "required", "secret", "description"):
        if key not in field:
            out.append(f"config.{name} 缺 {key}。")
    if field.get("secret") and field.get("default") not in ("", None):
        out.append(f"config.{name} 是密钥，不能在包里写默认值。")
    if field.get("type") == "enum" and not field.get("choices"):
        out.append(f"config.{name} 是 enum，要写 choices。")
    return out


def validate_backend(data) -> list[str]:
    if not isinstance(data, dict) or data.get("schema") != BACKEND_SCHEMA:
        return [f"后端声明的 schema 必须是 {BACKEND_SCHEMA}。"]
    problems = []
    if not isinstance(data.get("name"), str) or not _NAME.match(data.get("name") or ""):
        problems.append("后端 name 不合格。")
    if not isinstance(data.get("title"), str):
        problems.append("后端 title 必填。")
    return problems


# ---------- 发现 ----------

class Pack:
    def __init__(self, manifest: dict, folder: str, builtin: bool):
        self.manifest = manifest
        self.folder = folder
        self.builtin = builtin

    @property
    def name(self) -> str:
        return self.manifest["name"]

    def granted(self) -> set:
        if self.builtin:
            return {key for key, value in self.manifest["permissions"].items() if value}
        try:
            with open(os.path.join(self.folder, GRANTS), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return set()
        return set(data.get("permissions") or []) if isinstance(data, dict) else set()

    def module_path(self) -> str | None:
        entry = self.manifest["entry"]
        if entry["kind"] != "python":
            return None
        base = ROOT if self.builtin else self.folder
        return os.path.normpath(os.path.join(base, entry["module"]))


def _read(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def backends() -> dict:
    found = {}
    if not os.path.isdir(BACKENDS):
        return found
    for entry in sorted(os.listdir(BACKENDS)):
        if not entry.endswith(".json"):
            continue
        data = _read(os.path.join(BACKENDS, entry))
        problems = validate_backend(data)
        if problems:
            raise PackError("invalid_backend", f"{entry}：{'；'.join(problems)}")
        found[data["name"]] = data
    return found


def _scan(folder: str, builtin: bool) -> list[Pack]:
    packs = []
    if not os.path.isdir(folder):
        return packs
    for entry in sorted(os.listdir(folder)):
        path = os.path.join(folder, entry, MANIFEST)
        if entry.startswith((".", "_")) or not os.path.isfile(path):
            continue
        try:
            data = _read(path)
        except ValueError as exc:
            raise PackError("invalid_manifest", f"{path} 不是合法 JSON：{exc}") from exc
        problems = validate_manifest(data, builtin=builtin)
        if problems:
            raise PackError("invalid_manifest", f"{entry}：{'；'.join(problems)}", problems)
        if data["name"] != entry:
            raise PackError("invalid_manifest", f"{entry}：目录名要和 name 一致。")
        packs.append(Pack(data, os.path.join(folder, entry), builtin))
    return packs


def discover() -> list[Pack]:
    """内置 + 已安装。名字不能重复，也不能和后端重名。"""
    packs = _scan(BUILTIN, True) + _scan(installed_dir(), False)
    seen = set(backends())
    for pack in packs:
        if pack.name in seen:
            raise PackError("duplicate_tool", f"工具 {pack.name} 重名了。")
        seen.add(pack.name)
    packs.sort(key=lambda p: (p.manifest.get("order", 1000), p.name))
    return packs


def find(name: str) -> Pack | None:
    for pack in discover():
        if pack.name == name:
            return pack
    return None


# ---------- 运行时检查 ----------

def config_for(pack: Pack) -> dict:
    values = {f["name"]: f["default"] for f in pack.manifest.get("config") or []}
    if callable(CONFIG_SOURCE):
        try:
            values.update(CONFIG_SOURCE(pack.name) or {})
        except KeyError:
            pass
    return values


def missing_dependencies(pack: Pack) -> list[str]:
    requires = pack.manifest.get("requires") or {}
    known = backends()
    missing = [f"后端 {name}" for name in requires.get("backends") or [] if name not in known]
    for module in requires.get("python") or []:
        if importlib.util.find_spec(module) is None:
            missing.append(f"Python 包 {module}")
    return missing


def denied_permissions(pack: Pack) -> list[str]:
    wanted = {key for key, value in pack.manifest["permissions"].items() if value}
    return sorted(wanted - pack.granted())


def gate(pack: Pack) -> dict | None:
    """调用前的依赖和权限检查。返回 None 表示放行；否则是带稳定错误码的结果。"""
    missing = missing_dependencies(pack)
    if missing:
        return {"ok": False, "tool": pack.name, "executed": False,
                "error": {"code": "dependency_missing", "message": f"缺少依赖：{'、'.join(missing)}。", "missing": missing}}
    denied = denied_permissions(pack)
    if denied:
        return {"ok": False, "tool": pack.name, "executed": False,
                "error": {"code": "permission_denied", "message": f"没有授权：{'、'.join(denied)}。", "denied": denied}}
    return None


def needs_confirm(pack: Pack, arguments=None) -> bool:
    policy = pack.manifest["confirm"]
    if policy == "always":
        return True
    if policy == "never":
        return False
    if not isinstance(arguments, dict):
        return True
    return any(key in arguments for key in pack.manifest.get("confirm_args") or [])


def load_module(pack: Pack):
    path = pack.module_path()
    if not path or not os.path.isfile(path):
        raise PackError("dependency_missing", f"{pack.name} 的入口文件不存在。")
    key = (path, os.path.getmtime(path))
    if key in _modules:
        return _modules[key]
    if pack.builtin:
        stem = os.path.splitext(os.path.basename(path))[0]
        full_name = f"tools.internal.{stem}"
        prefix = "tools.internal"
    else:
        full_name = f"toolpacks_installed.{pack.name}"
        prefix = None
    if full_name in sys.modules and pack.builtin:
        loaded = sys.modules[full_name]
    else:
        spec = importlib.util.spec_from_file_location(full_name, path)
        if spec is None or spec.loader is None:
            raise PackError("invalid_manifest", f"无法装载 {path}。")
        loaded = importlib.util.module_from_spec(spec)
        if prefix:
            loaded.__package__ = prefix
        sys.modules[full_name] = loaded
        try:
            spec.loader.exec_module(loaded)
        except Exception:
            sys.modules.pop(full_name, None)
            raise
    _modules[key] = loaded
    return loaded


def resolve_parameters(pack: Pack) -> dict | None:
    params = pack.manifest.get("parameters")
    if params is None:
        return None
    sources = pack.manifest.get("parameter_sources") or {}
    if not sources:
        return params
    params = json.loads(json.dumps(params))
    for key, source in sources.items():
        if source == "comfyui.styles":
            from tools._support import load_styles

            styles = sorted(load_styles())
            if styles:
                params["properties"][key]["enum"] = styles
            else:
                params["properties"].pop(key, None)
    return params


# ---------- 安装 / 卸载 / 导出 ----------

def _unpack(source: str) -> tuple[str, str | None]:
    """source 是目录或 .zip。返回（包目录, 需要清理的临时目录）。"""
    if os.path.isdir(source):
        return source, None
    if os.path.isfile(source) and source.lower().endswith(".zip"):
        temp = tempfile.mkdtemp(prefix="toolpack-")
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                target = os.path.normpath(os.path.join(temp, info.filename))
                if not target.startswith(temp + os.sep) and target != temp:
                    shutil.rmtree(temp, ignore_errors=True)
                    raise PackError("invalid_package", "压缩包里有越界路径。")
            archive.extractall(temp)
        if os.path.isfile(os.path.join(temp, MANIFEST)):
            return temp, temp
        inner = [d for d in os.listdir(temp) if os.path.isfile(os.path.join(temp, d, MANIFEST))]
        if len(inner) == 1:
            return os.path.join(temp, inner[0]), temp
        shutil.rmtree(temp, ignore_errors=True)
        raise PackError("invalid_package", "压缩包里找不到 tool.json。")
    raise PackError("invalid_package", "要给一个工具包目录或 .zip 文件。")


def inspect(source: str) -> dict:
    """装之前看一眼：清单摘要、要的权限、缺的依赖、问题。"""
    folder, temp = _unpack(source)
    try:
        path = os.path.join(folder, MANIFEST)
        if not os.path.isfile(path):
            raise PackError("invalid_package", "目录里没有 tool.json。")
        try:
            data = _read(path)
        except ValueError as exc:
            raise PackError("invalid_manifest", f"tool.json 不是合法 JSON：{exc}") from exc
        problems = validate_manifest(data)
        if not problems and data["entry"]["kind"] == "python":
            module = os.path.normpath(os.path.join(folder, data["entry"]["module"]))
            if not module.startswith(os.path.abspath(folder)) or not os.path.isfile(module):
                problems.append("entry.module 指向的文件不在包里。")
        summary = {"name": data.get("name"), "version": data.get("version"), "description": data.get("description"),
                   "permissions": sorted(k for k, v in (data.get("permissions") or {}).items() if v is True),
                   "requires": data.get("requires") or {}, "problems": problems}
        if not problems:
            if find(data["name"]) is not None and not os.path.isdir(os.path.join(installed_dir(), data["name"])):
                summary["problems"] = problems = ["和内置工具重名。"]
            elif data["name"] in backends():
                summary["problems"] = problems = ["和后端重名。"]
            else:
                pack = Pack(data, folder, False)
                summary["missing"] = missing_dependencies(pack)
        return summary
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def install(source: str, accept_permissions) -> dict:
    """装到 installed_dir()。accept_permissions 必须正好列出清单要的权限。"""
    summary = inspect(source)
    if summary["problems"]:
        raise PackError("invalid_manifest", "工具包不合格：" + "；".join(summary["problems"]), summary["problems"])
    wanted = summary["permissions"]
    if not isinstance(accept_permissions, list) or sorted(set(accept_permissions)) != wanted:
        raise PackError("permission_not_accepted", f"安装前要确认权限：{'、'.join(wanted) or '无'}。")
    folder, temp = _unpack(source)
    try:
        target = os.path.join(installed_dir(), summary["name"])
        os.makedirs(installed_dir(), exist_ok=True)
        staging = target + ".installing"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(folder, staging, ignore=_export_ignore)
        with open(os.path.join(staging, GRANTS), "w", encoding="utf-8") as handle:
            json.dump({"permissions": wanted, "version": summary["version"]}, handle)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    return summary


def uninstall(name: str) -> None:
    pack = find(name)
    if pack is None:
        raise PackError("not_found", "没有这个工具。")
    if pack.builtin:
        raise PackError("builtin_tool", "内置工具不能卸载。")
    shutil.rmtree(pack.folder)


def _export_ignore(_folder, names):
    return [n for n in names if n.startswith(".") or n in ("__pycache__", "tools.json") or n.endswith(".pyc")]


def export(name: str) -> bytes:
    """打成 zip：只有清单和代码，没有授权记录、配置和密钥。"""
    pack = find(name)
    if pack is None:
        raise PackError("not_found", "没有这个工具。")
    if pack.builtin:
        raise PackError("not_exportable", "内置工具依赖本仓库代码，不导出。")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for base, dirs, files in os.walk(pack.folder):
            dirs[:] = [d for d in dirs if d not in _export_ignore(base, dirs)]
            for filename in files:
                if filename in _export_ignore(base, [filename]):
                    continue
                full = os.path.join(base, filename)
                archive.write(full, os.path.join(name, os.path.relpath(full, pack.folder)))
    return buffer.getvalue()


def summary(pack: Pack) -> dict:
    m = pack.manifest
    return {
        "name": pack.name, "version": m["version"], "description": m["description"],
        "builtin": pack.builtin, "exposed": m.get("parameters") is not None,
        "mutating": m["mutating"], "confirm": m["confirm"], "entry": m["entry"]["kind"],
        "permissions": sorted(k for k, v in m["permissions"].items() if v),
        "denied": denied_permissions(pack), "missing": missing_dependencies(pack),
        "backends": list((m.get("requires") or {}).get("backends") or []),
        "configurable": bool(m.get("config")),
    }
