"""工具包（tool pack）：每个工具一份清单 tool.json，运行时只从清单注册工具。

目录：
- toolpacks/builtin/<name>/tool.json   内置工具，随仓库发布。
- toolpacks/backends/<name>.json       共享后端（例如 ComfyUI），工具在 requires.backends 里引用。
- toolpacks/installed/<name>/          用户安装的工具包（.gitignore 忽略；可用 MAS_TOOLPACKS_DIR 改位置，测试用）。
  安装 = 把包目录原样复制进来（加一份授权记录 .granted.json），不改仓库里任何受版本控制的文件。

entry.kind（#86）：
- host    内置工具，宿主代码执行（名单 HOST_TOOLS）。
- python  call(arguments, config)；代码一律是工具目录里的 .py。内置的（名单 BUILTIN_PYTHON）固定为
          toolpacks/builtin/<name>/tool.py，在宿主进程里跑；第三方在沙箱子进程里跑。
- module  表单工具：代码是工具目录里的 .py（契约见 docs/tool-code-contract.md：catalog / preview / run），
          内置的在宿主进程里跑（受信任），第三方的在沙箱子进程里跑。content.rating=adult 的工具不管代码写了什么，
          宿主的 content_guard 都会在调用前后强制检查。

清单字段见 docs/tool-packs.md。这里负责：读清单、校验、发现、安装、卸载、导出。
配置值和密钥不在包里：它们存在数据目录的 tools.json，导出时不会带走。
"""

from __future__ import annotations

import hashlib
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
LEGACY_PRESET = "pack.json"  # #86 早期的预设包（只有 pack.json）：已不再支持，安装时给出说明
GRANTS = ".granted.json"
PERMISSIONS = ("read_local_content", "network", "write_files", "send_local_to_model", "ui")
# 工具自带的网页面板（ui.panel）：只能是工具目录 ui/ 里的一个 .js 和一个可选的 .css，各不超过 PANEL_MAX_BYTES。
PANEL_FILE = re.compile(r"^ui/[A-Za-z0-9][A-Za-z0-9_-]{0,47}\.(js|css)$")
PANEL_MAX_BYTES = 256 * 1024
PANEL_MOUNTS = ("dialog",)
CONFIRM = ("always", "never", "by_args")
FIELD_TYPES = ("string", "int", "enum", "path", "secret")
BLOCKED_EXTERNAL = {"prompt", "text", "body", "content", "bytes", "path", "filename"}
PARAMETER_SOURCES = ("comfyui.styles",)
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_FIELDS = {"schema", "name", "version", "description", "order", "parameters", "mutating", "confirm",
           "confirm_args", "entry", "config", "requires", "permissions", "parameter_sources", "mcp",
           "ui", "content"}
ENTRY_KINDS = ("python", "host", "module")
RATINGS = ("general", "adult")
# 「包含代码」：入口是 python/module，或包里带任何可执行的文件。安装时要单独勾选确认。
CODE_SUFFIXES = (".py", ".pyc", ".pyd", ".pyw", ".so", ".dll", ".exe", ".bat", ".cmd", ".ps1", ".sh", ".js")
ACCEPT_CODE = "code"
ACCEPT_ADULT = "rating_adult"

# 内置工具名单（12.D #60）：只有这些名字能用 entry.kind=host 或仓库里的 python 模块，在宿主进程里跑。
# 值是这个工具实际要用的权限：运行时二次核对「清单声明 ⊇ 实际需要」，清单少声明了就 permission_denied。
HOST_TOOLS = {
    "accept_local_content": frozenset({"read_local_content", "write_files"}),
    "list_local_content": frozenset({"read_local_content"}),
    "forget_local_content": frozenset({"read_local_content", "write_files"}),
    "retrieve_local": frozenset({"read_local_content"}),
    "build_skill_page": frozenset({"write_files"}),
    "plan_images": frozenset(),
    "stage_prompt": frozenset(),
    "character_image": frozenset(),
}
BUILTIN_PYTHON = {
    "generate_image": frozenset({"network", "read_local_content", "write_files"}),
    "generate_video": frozenset({"network", "read_local_content", "write_files"}),
    "lookup_demo": frozenset(),
    "read_skill": frozenset(),
}
BUILTIN_ENTRY = "tool.py"  # 内置 python 工具的入口：只能是自己工具目录里的 tool.py（#89）
# 12.E #61：只回句柄和元数据、不联网的内置读取工具。它们读本地内容但结果里没有正文，可以 confirm=never；
# 其余凡是声明了 network 或 read_local_content 的工具（内置和第三方一样）都必须 confirm=always，校验时就拒绝。
HANDLE_ONLY_READERS = frozenset({"list_local_content", "retrieve_local"})
SENSITIVE_CONFIRM_PROBLEM = "联网或读本地内容的工具必须 confirm=always（不能 never / by_args）。"
GRANT_SCHEMA = "mas.grant/2"
# 每种权限给人看的说明（安装前的权限审阅界面显示这些）。
PERMISSION_REVIEW = {
    "network": ("联网", "high", "可以连接互联网或局域网上的服务（不能连本机地址）。每次连接的主机名记进「数据出站」。"),
    "write_files": ("写文件", "medium", "可以在它自己的数据目录里写文件（data/toolpack_data/<名字>/），别处不能写。"),
    "read_local_content": ("读本地内容", "high",
                           "第三方工具包暂时拿不到本地内容：沙箱禁止它直接读会话和数据目录，句柄接口还没开放给第三方包。"),
    "send_local_to_model": ("把本地内容发给模型", "forbidden", "不允许。"),
    "ui": ("网页面板", "medium",
           "会在网页侧栏加一个入口，打开它自带的面板（ui/ 里的脚本和样式）。第三方面板只在隔离的沙箱 iframe 里运行："
           "拿不到聊天页面、Cookie、本地内容，不能联网，只能经宿主的面板接口读目录、预览提示词和发起出图；"
           "出图仍然经过宿主的内容检查和确认。"),
}
SANDBOX_FACTS = (
    "在单独的子进程里运行，超时会被停止。",
    "拿不到你电脑上的环境变量（API key、令牌）；它的配置和密钥由本程序显式传给它。",
    "不能读会话、本地内容和配置文件；本地内容只能经句柄接口。",
    "不能启动别的程序，不能加载原生库。",
    "没有联网权限时，任何联网尝试都会被拦下并记进「数据出站」。",
)

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
    if confirm in CONFIRM and confirm != "always" and _sensitive(data, builtin=builtin):
        problems.append(SENSITIVE_CONFIRM_PROBLEM)
    if confirm == "by_args":
        args = data.get("confirm_args")
        if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
            problems.append("confirm=by_args 时 confirm_args 必须是非空的参数名列表。")
    entry = data.get("entry")
    if not isinstance(entry, dict) or entry.get("kind") not in ENTRY_KINDS:
        problems.append("entry.kind 只能是 python、host 或 module。")
    elif entry["kind"] == "host" and not builtin:
        problems.append("只有内置工具可以用 entry.kind=host。")
    elif entry["kind"] == "host" and data.get("name") not in HOST_TOOLS:
        problems.append("entry.kind=host 只限内置工具名单（packs.HOST_TOOLS）。")
    elif entry["kind"] == "python" and builtin and (
            data.get("name") not in BUILTIN_PYTHON or entry.get("module") != BUILTIN_ENTRY):
        problems.append(f"内置 python 工具只限名单（packs.BUILTIN_PYTHON），入口必须是工具目录里的 {BUILTIN_ENTRY}。")
    elif entry["kind"] == "module" and builtin and data.get("name") not in HOST_TOOLS:
        problems.append("内置 module 工具只限内置工具名单（packs.HOST_TOOLS）。")
    elif entry["kind"] in ("python", "module"):
        module = entry.get("module")
        if not isinstance(module, str) or not module.endswith(".py"):
            problems.append("entry.module 必须是 .py 文件。")
        elif not _relative_inside(module):
            problems.append("entry.module 必须是工具目录里的相对路径（不能是绝对路径，不能有 ..）。")
    if entry and isinstance(entry, dict) and entry.get("kind") == "python" and params is None and not builtin:
        problems.append("第三方工具必须声明 parameters。")
    kind = entry.get("kind") if isinstance(entry, dict) else None
    problems.extend(_ui_problems(data.get("ui"), kind, data.get("permissions")))
    problems.extend(_content_problems(data.get("content"), kind))
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


def _relative_inside(module: str) -> bool:
    parts = module.replace("\\", "/").split("/")
    return bool(module) and not os.path.isabs(module) and not module.startswith(("/", "\\")) \
        and ":" not in module and ".." not in parts


def _ui_problems(ui, kind, perms=None) -> list[str]:
    """ui：网页上的通用表单（#86）和工具自带的面板（ui.panel）。只给 entry.kind=module 的工具。
    声明了 ui.panel 就必须声明 permissions.ui=true（内置也一样），反过来也是。"""
    wants_ui = isinstance(perms, dict) and perms.get("ui") is True
    if ui is None:
        return ["permissions.ui 只给声明了 ui.panel 的工具。"] if wants_ui else []
    if kind != "module":
        return ["ui 只能用在 entry.kind=module 的工具上。"]
    if not isinstance(ui, dict) or set(ui) - {"form", "preview", "title", "run_label", "panel"}:
        return ["ui 只能有 form、preview、title、run_label、panel。"]
    out = _panel_problems(ui.get("panel"))
    if "panel" in ui and not wants_ui:
        out.append("声明了 ui.panel 就要声明 permissions.ui=true（安装时要单独确认）。")
    if "panel" not in ui and wants_ui:
        out.append("permissions.ui 只给声明了 ui.panel 的工具。")
    for key in ("form", "preview"):
        if key in ui and not isinstance(ui[key], bool):
            out.append(f"ui.{key} 必须是 true/false。")
    for key in ("title", "run_label"):
        if key in ui and (not isinstance(ui[key], str) or not ui[key].strip() or len(ui[key]) > 40):
            out.append(f"ui.{key} 必须是 1-40 字的文字。")
    return out


def _panel_problems(panel) -> list[str]:
    if panel is None:
        return []
    if not isinstance(panel, dict) or set(panel) - {"script", "style", "title", "mount"} or "script" not in panel:
        return ["ui.panel 只能有 script（必填）、style、title、mount。"]
    out = []
    for key, suffix in (("script", ".js"), ("style", ".css")):
        if key in panel and (not isinstance(panel[key], str) or not PANEL_FILE.match(panel[key])
                             or not panel[key].endswith(suffix)):
            out.append(f"ui.panel.{key} 必须是 ui/ 目录里的 {suffix} 文件（形如 ui/panel{suffix}，不能有子目录和 ..）。")
    if "title" in panel and (not isinstance(panel["title"], str) or not panel["title"].strip() or len(panel["title"]) > 40):
        out.append("ui.panel.title 必须是 1-40 字的文字。")
    if panel.get("mount", "dialog") not in PANEL_MOUNTS:
        out.append(f"ui.panel.mount 只能是 {'、'.join(PANEL_MOUNTS)}。")
    return out


def panel_spec(data: dict) -> dict | None:
    ui = data.get("ui") if isinstance(data, dict) else None
    panel = ui.get("panel") if isinstance(ui, dict) else None
    return panel if isinstance(panel, dict) else None


def panel_files(folder: str, data: dict) -> dict:
    """声明的面板文件：清单里的相对路径 -> 真实路径。只有这几个文件能被网页取到（白名单）。
    解开符号链接以后必须还在工具目录的 ui/ 里，是普通文件，不超过 PANEL_MAX_BYTES；否则抛 PackError。"""
    panel = panel_spec(data)
    if not panel:
        return {}
    root = os.path.realpath(os.path.join(folder, "ui"))
    out = {}
    for key in ("script", "style"):
        rel = panel.get(key)
        if not rel:
            continue
        if not isinstance(rel, str) or not PANEL_FILE.match(rel):
            raise PackError("invalid_manifest", f"ui.panel.{key} 不合格。")
        path = os.path.realpath(os.path.join(folder, rel))
        try:
            inside = os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
        except ValueError:
            inside = False
        if not inside or path == root:
            raise PackError("invalid_manifest", f"ui.panel.{key} 指向 ui/ 目录外面。")
        if not os.path.isfile(path):
            raise PackError("invalid_manifest", f"ui.panel.{key} 指向的文件不存在：{rel}。")
        if os.path.getsize(path) > PANEL_MAX_BYTES:
            raise PackError("invalid_manifest", f"ui.panel.{key} 太大（上限 {PANEL_MAX_BYTES // 1024} KB）。")
        out[rel] = path
    return out


def panel_listing(folder: str, data: dict) -> list[dict]:
    """安装审阅里给人看的面板文件：名字、大小、sha256。"""
    items = []
    for rel, path in panel_files(folder, data).items():
        with open(path, "rb") as handle:
            raw = handle.read()
        items.append({"file": rel, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return items


def _content_problems(content, kind) -> list[str]:
    """content：工具产出的内容分级（#86）。rating=adult 只能本机出图，安装和每次使用都要明确确认。"""
    if content is None:
        return []
    if kind != "module":
        return ["content 只能用在 entry.kind=module 的工具上。"]
    if not isinstance(content, dict) or set(content) - {"rating", "min_age", "label", "confirm", "chat_keywords"}:
        return ["content 只能有 rating、min_age、label、confirm、chat_keywords。"]
    out = []
    rating = content.get("rating", "general")
    if rating not in RATINGS:
        out.append(f"content.rating 只能是 {'、'.join(RATINGS)}。")
    if "label" in content and (not isinstance(content["label"], str) or not content["label"].strip() or len(content["label"]) > 20):
        out.append("content.label 必须是 1-20 字的文字。")
    if "confirm" in content and (not isinstance(content["confirm"], str) or len(content["confirm"]) > 1000):
        out.append("content.confirm 必须是 1000 字以内的文字。")
    words = content.get("chat_keywords", [])
    if not isinstance(words, list) or len(words) > 32 or not all(isinstance(w, str) and 2 <= len(w.strip()) <= 20 for w in words):
        out.append("content.chat_keywords 必须是最多 32 个、每个 2-20 字的词。")
    if rating == "adult":
        age = content.get("min_age")
        if not isinstance(age, int) or isinstance(age, bool) or not 18 <= age <= 99:
            out.append("rating=adult 要写 min_age（18 到 99 的整数）。")
        if not str(content.get("confirm") or "").strip():
            out.append("rating=adult 要写 confirm（开启前给用户看的确认文字）。")
        if not str(content.get("label") or "").strip():
            out.append("rating=adult 要写 label（例如「成人模式」）。")
    elif "min_age" in content and (not isinstance(content["min_age"], int) or isinstance(content["min_age"], bool)
                                    or not 0 <= content["min_age"] <= 99):
        out.append("content.min_age 必须是 0 到 99 的整数。")
    return out


def entry_problem(folder: str, data: dict, builtin: bool = False) -> str | None:
    """入口代码必须是工具目录里真实存在的文件（解开符号链接以后也还在目录里）。
    内置和第三方一样按工具目录查（#89：内置 python 工具的代码也在自己的目录里）。"""
    entry = data.get("entry") or {}
    if entry.get("kind") not in ("python", "module"):
        return None
    try:
        panel_files(folder, data)
    except PackError as exc:
        return exc.message
    root = os.path.realpath(folder)
    module = os.path.realpath(os.path.join(root, entry["module"]))
    try:
        inside = os.path.normcase(os.path.commonpath([root, module])) == os.path.normcase(root)
    except ValueError:  # Windows 上不同盘符
        inside = False
    if not inside or module == root:
        return "entry.module 指向工具目录外面。"
    if not os.path.isfile(module):
        return "entry.module 指向的文件不在工具目录里。"
    return None


def has_code(folder: str, data: dict) -> bool:
    if ((data.get("entry") or {}).get("kind")) in ("python", "module"):
        return True
    for _base, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        if any(f.lower().endswith(CODE_SUFFIXES) for f in files):
            return True
    return False


def rating(data: dict) -> str:
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, dict):
        return "general"
    return content.get("rating") or "general"


def _sensitive(data, *, builtin: bool = False) -> bool:
    """12.E：会联网，或会读本地内容（读完结果回到模型就可能出站）的工具。只回句柄的内置读取工具除外。"""
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return False
    if perms.get("network") is True:
        return True
    if perms.get("read_local_content") is True:
        return not (builtin and data.get("name") in HANDLE_ONLY_READERS)
    return False


def sensitive(pack: "Pack") -> bool:
    return _sensitive(pack.manifest, builtin=pack.builtin)


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

    def declared(self) -> set:
        return {key for key, value in self.manifest["permissions"].items() if value is True}

    def grant_record(self) -> dict:
        try:
            with open(os.path.join(self.folder, GRANTS), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def grant_state(self) -> str:
        """ok / missing（没有授权记录）/ outdated（12.D 之前装的，没有指纹）/ changed（装好后包被改过）。"""
        if self.builtin:
            return "ok"
        record = self.grant_record()
        if not record:
            return "missing"
        if not record.get("digest"):
            return "outdated"
        if record.get("digest") != pack_digest(self.folder):
            return "changed"
        return "ok"

    def granted(self) -> set:
        if self.builtin:
            return self.declared()
        if self.grant_state() != "ok":
            return set()
        perms = self.grant_record().get("permissions") or []
        return {p for p in perms if p in PERMISSIONS} if isinstance(perms, list) else set()

    def effective(self) -> set:
        """生效权限 = 清单声明 ∩ 授权记录。沙箱只按这个放行。"""
        return self.declared() & self.granted()

    def module_path(self) -> str | None:
        entry = self.manifest["entry"]
        if entry["kind"] not in ("python", "module"):
            return None
        # 代码一律在工具自己的目录里（内置 python 工具也是，#89）。
        return os.path.normpath(os.path.join(self.folder, entry["module"]))

    @property
    def rating(self) -> str:
        return rating(self.manifest)

    @property
    def content(self) -> dict:
        return dict(self.manifest.get("content") or {})


def pack_digest(folder: str) -> str:
    """包内容指纹：除授权记录、缓存和点文件以外所有文件的相对路径和字节。装好后改了任何文件（包括 tool.json）都会变。"""
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in _export_ignore(base, dirs))
        for filename in sorted(files):
            if filename in _export_ignore(base, [filename]):
                continue
            full = os.path.join(base, filename)
            digest.update(os.path.relpath(full, folder).replace(os.sep, "/").encode("utf-8") + b"\0")
            with open(full, "rb") as handle:
                digest.update(hashlib.sha256(handle.read()).digest())
    return digest.hexdigest()


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
        problem = entry_problem(os.path.join(folder, entry), data, builtin)
        if problem:
            raise PackError("invalid_manifest", f"{entry}：{problem}", [problem])
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
    return sorted(pack.declared() - pack.granted())


GRANT_TEXT = {
    "missing": "没有授权记录，请重新安装并确认权限。",
    "outdated": "这个包是旧版本装的，授权没有绑定包内容，请重新安装并确认权限。",
    "changed": "装好以后包里的文件被改过，授权作废，请重新安装并确认权限。",
}


def needed_permissions(pack: Pack) -> set:
    """内置工具实际要用的权限（名单里写死）。第三方包的需要就是它声明的。"""
    if pack.builtin:
        return set(HOST_TOOLS.get(pack.name, BUILTIN_PYTHON.get(pack.name, frozenset({"__unlisted__"}))))
    return pack.declared()


def authorize(pack: Pack) -> dict | None:
    """12.D 运行时二次核对：要用的权限 ⊆ 清单声明 ⊆ 授权记录，授权绑定的包内容没变。不过关返回 permission_denied。"""
    state = pack.grant_state()
    if state != "ok":
        return {"ok": False, "tool": pack.name, "executed": False,
                "error": {"code": "permission_denied", "message": GRANT_TEXT[state], "grant": state}}
    needed = needed_permissions(pack)
    over = sorted(needed - pack.declared())
    if over:
        return {"ok": False, "tool": pack.name, "executed": False,
                "error": {"code": "permission_denied",
                          "message": f"清单没有声明 {'、'.join(over)}，不能执行。", "denied": over}}
    denied = denied_permissions(pack)
    if denied:
        return {"ok": False, "tool": pack.name, "executed": False,
                "error": {"code": "permission_denied", "message": f"没有授权：{'、'.join(denied)}。", "denied": denied}}
    return None


def review(data: dict, code: bool = False, panel: list | None = None) -> dict:
    """安装前的权限审阅：每个权限的白话说明和风险，加上沙箱会怎样限制它。
    #86：包含代码、成人分级也各是一项，要单独勾选（permission 分别是 code / rating_adult）。"""
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    wanted = sorted(k for k, v in perms.items() if v is True and k in PERMISSIONS)
    items = []
    for key in wanted:
        label, risk, effect = PERMISSION_REVIEW[key]
        if key == "ui" and panel:
            effect += " 面板文件：" + "、".join(f"{p['file']}（{p['bytes']} 字节，sha256 {p['sha256'][:12]}…）" for p in panel) + "。"
        items.append({"permission": key, "label": label, "risk": risk, "effect": effect})
    if code:
        module = ((data.get("entry") or {}).get("module")) or "（包里的 .py 文件）"
        items.append({"permission": ACCEPT_CODE, "label": "包含代码", "risk": "high",
                      "effect": f"会运行包里的 Python 代码（{module}）。第三方代码只在沙箱子进程里跑，"
                                "但它能决定这个工具产出的文字；只装你信任来源的包。"})
    if rating(data) == "adult":
        content = data.get("content") or {}
        items.append({"permission": ACCEPT_ADULT, "label": f"成人分级（{content.get('label') or 'adult'}）", "risk": "high",
                      "effect": f"这个工具会产出成人内容（{content.get('min_age')} 岁以上）。只能用本机 ComfyUI 出图，"
                                "结果标记为成人内容；每次使用还要再确认。宿主强制：不能画未成年或像未成年的角色、"
                                "校服/学生、幼态体型，不能参考真人或版权角色，不能发给第三方出图服务。"
                                + (" 作者说明：" + str(content.get("confirm")) if content.get("confirm") else "")})
    denied = [PERMISSION_REVIEW[k][0] for k in PERMISSIONS if k not in wanted and k not in ("send_local_to_model", "ui")]
    out = {"permissions": items, "not_granted": denied, "sandbox": list(SANDBOX_FACTS), "mode": "subprocess"}
    # 12.E：联网 / 读本地内容的包每次调用都弹确认，同一轮里也不复用上一次的同意。
    out["confirm_every_call"] = _sensitive(data)
    return out


def gate(pack: Pack) -> dict | None:
    """调用前的依赖和权限检查。返回 None 表示放行；否则是带稳定错误码的结果。"""
    missing = missing_dependencies(pack)
    if missing:
        return {"ok": False, "tool": pack.name, "executed": False,
                "error": {"code": "dependency_missing", "message": f"缺少依赖：{'、'.join(missing)}。", "missing": missing}}
    return authorize(pack)


def needs_confirm(pack: Pack, arguments=None) -> bool:
    # 12.E：敏感工具不论清单写了什么都要确认（校验已经拒绝了别的写法，这里是第二道）。
    if sensitive(pack):
        return True
    policy = pack.manifest["confirm"]
    if policy == "always":
        return True
    if policy == "never":
        return False
    if not isinstance(arguments, dict):
        return True
    return any(key in arguments for key in pack.manifest.get("confirm_args") or [])


def load_module(pack: Pack):
    """只装载内置工具的模块。第三方包的代码一律不进宿主进程（12.D），由 services/tools/sandbox.py 在子进程里跑。"""
    if not pack.builtin:
        raise PackError("sandbox_required", f"{pack.name} 是第三方工具包，只能在沙箱子进程里运行。")
    path = pack.module_path()
    if not path or not os.path.isfile(path):
        raise PackError("dependency_missing", f"{pack.name} 的入口文件不存在。")
    key = (path, os.path.getmtime(path))
    if key in _modules:
        return _modules[key]
    # 每个内置工具的入口都叫 tool.py，模块名按工具名区分，免得互相覆盖。
    full_name = f"_mas_builtin_tool_{pack.name}"
    if full_name in sys.modules:
        loaded = sys.modules[full_name]
    else:
        spec = importlib.util.spec_from_file_location(full_name, path)
        if spec is None or spec.loader is None:
            raise PackError("invalid_manifest", f"无法装载 {path}。")
        loaded = importlib.util.module_from_spec(spec)
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
            from services.tools._support import load_styles

            styles = sorted(load_styles())
            if styles:
                params["properties"][key]["enum"] = styles
            else:
                params["properties"].pop(key, None)
    return params


# ---------- 安装 / 卸载 / 导出 ----------

LEGACY_TEXT = "预设包（pack.json）已不再支持：请改成工具包（tool.json + 工具目录里的 .py，见 docs/tool-code-contract.md）。"


def _unpack(source: str) -> tuple[str, str | None]:
    """source 是目录或 .zip。返回（包目录, 需要清理的临时目录）。"""
    manifest = MANIFEST
    if os.path.isdir(source):
        return source, None
    if os.path.isfile(source) and source.lower().endswith(".zip"):
        temp = tempfile.mkdtemp(prefix="toolpack-")
        try:
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    target = os.path.normpath(os.path.join(temp, info.filename))
                    if not target.startswith(temp + os.sep) and target != temp:
                        raise PackError("invalid_package", "压缩包里有越界路径。")
                archive.extractall(temp)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(temp, ignore_errors=True)
            raise PackError("invalid_package", "不是合法的 .zip 文件。") from exc
        except PackError:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        if os.path.isfile(os.path.join(temp, manifest)):
            return temp, temp
        inner = [d for d in os.listdir(temp) if os.path.isfile(os.path.join(temp, d, manifest))]
        if len(inner) == 1:
            return os.path.join(temp, inner[0]), temp
        legacy = os.path.isfile(os.path.join(temp, LEGACY_PRESET)) or any(
            os.path.isfile(os.path.join(temp, d, LEGACY_PRESET)) for d in os.listdir(temp))
        shutil.rmtree(temp, ignore_errors=True)
        if legacy:
            raise PackError("preset_pack_unsupported", LEGACY_TEXT)
        raise PackError("invalid_package", f"压缩包里找不到 {manifest}。")
    raise PackError("invalid_package", "要给一个工具包目录或 .zip 文件。")


def inspect(source: str) -> dict:
    """装之前看一眼：清单摘要、要的权限、缺的依赖、问题。"""
    folder, temp = _unpack(source)
    try:
        path = os.path.join(folder, MANIFEST)
        if not os.path.isfile(path):
            if os.path.isfile(os.path.join(folder, LEGACY_PRESET)):
                raise PackError("preset_pack_unsupported", LEGACY_TEXT)
            raise PackError("invalid_package", "目录里没有 tool.json。")
        try:
            data = _read(path)
        except ValueError as exc:
            raise PackError("invalid_manifest", f"tool.json 不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise PackError("invalid_manifest", "清单必须是 JSON 对象。")
        problems = validate_manifest(data)
        panel = []
        if not problems:
            problem = entry_problem(folder, data)
            if problem:
                problems.append(problem)
            else:
                panel = panel_listing(folder, data)
        permissions = sorted(k for k, v in (data.get("permissions") or {}).items() if v is True) \
            if isinstance(data, dict) and isinstance(data.get("permissions"), dict) else []
        code = isinstance(data, dict) and has_code(folder, data)
        level = rating(data) if isinstance(data, dict) else "general"
        content = data.get("content") if isinstance(data, dict) and isinstance(data.get("content"), dict) else {}
        accept = sorted(permissions + ([ACCEPT_CODE] if code else []) + ([ACCEPT_ADULT] if level == "adult" else []))
        summary = {"name": data.get("name"), "version": data.get("version"), "description": data.get("description"),
                   "permissions": permissions, "code": code, "rating": level,
                   "min_age": content.get("min_age"), "label": content.get("label"), "confirm": content.get("confirm"),
                   "accept": accept, "ui": data.get("ui") if isinstance(data.get("ui"), dict) else {},
                   "target": os.path.join(installed_dir(), str(data.get("name") or "")),
                   "requires": data.get("requires") or {}, "problems": problems,
                   "panel": panel,
                   "review": review(data, code=code, panel=panel) if isinstance(data, dict) else None}
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
    """复制到 installed_dir()（默认仓库的 toolpacks/installed/，已被 .gitignore 忽略）。
    accept_permissions 必须正好列出 inspect 给的 accept：清单要的权限，加上 "code"（包里有代码）
    和 "rating_adult"（content.rating=adult）。少一项、多一项都不装。"""
    summary = inspect(source)
    if summary["problems"]:
        raise PackError("invalid_manifest", "工具包不合格：" + "；".join(summary["problems"]), summary["problems"])
    wanted = summary["accept"]
    given = sorted(set(accept_permissions)) if isinstance(accept_permissions, list) else None
    if given != wanted:
        given = given or []
        if summary["code"] and ACCEPT_CODE not in given:
            raise PackError("code_not_accepted", "这个工具包含代码（.py），安装前要勾选确认「包含代码」。")
        if summary["rating"] == "adult" and ACCEPT_ADULT not in given:
            raise PackError("rating_not_accepted",
                            f"这个工具声明了成人分级（rating=adult，{summary.get('min_age')} 岁以上），安装前要勾选确认。")
        perms = summary["permissions"]
        raise PackError("permission_not_accepted", f"安装前要确认权限：{'、'.join(perms) or '无'}。")
    folder, temp = _unpack(source)
    try:
        target = os.path.join(installed_dir(), summary["name"])
        os.makedirs(installed_dir(), exist_ok=True)
        staging = target + ".installing"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(folder, staging, ignore=_export_ignore)
        record = {"schema": GRANT_SCHEMA, "permissions": summary["permissions"], "version": summary["version"],
                  "code": summary["code"], "rating": summary["rating"], "digest": pack_digest(staging)}
        with open(os.path.join(staging, GRANTS), "w", encoding="utf-8") as handle:
            json.dump(record, handle)
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
        "sensitive": sensitive(pack),
        "permissions": sorted(k for k, v in m["permissions"].items() if v),
        "denied": denied_permissions(pack), "missing": missing_dependencies(pack),
        "backends": list((m.get("requires") or {}).get("backends") or []),
        "configurable": bool(m.get("config")),
        "sandbox": "host" if pack.builtin else "subprocess",
        "grant": pack.grant_state(),
        "rating": pack.rating,
        "form": bool((m.get("ui") or {}).get("form")),
        "code": m["entry"]["kind"] in ("python", "module") and not pack.builtin,
    }
