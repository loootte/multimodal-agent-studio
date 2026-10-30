"""表单工具（#86）：entry.kind=module 的工具，代码是工具目录里的一个 .py（见 docs/tool-code-contract.md）。

宿主负责：
- 加载：内置工具在本进程 import（代码必须在自己目录里，解开符号链接也不能跑出去）；
  已安装的工具每次调用都在沙箱子进程里跑（services/tools/sandbox.py），先过依赖和授权（packs.gate）。
- 约定的函数：catalog(ctx) → 表单目录；preview(fields, ctx) / run(args, ctx) → {prompt, summary, ...}；
  可选 check_config(config, ctx)。拒绝时返回 {"error": {code, message, field}}。
- 守卫：调用前后都过 content_guard（rating=adult 的工具一律受宿主守卫，与工具代码无关）。
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys

import content_guard
import tool_config
from content_guard import GuardError

_LOADED: dict = {}
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
MAX_TEXT = 2000


def _packs():
    return tool_config._packs()


def get(name: str):
    """名字对应的 module 工具（Pack）；不是就返回 None。"""
    try:
        pack = _packs().find(str(name or ""))
    except Exception:
        return None
    if pack is None or pack.manifest["entry"]["kind"] != "module":
        return None
    return pack


def is_module_tool(name) -> bool:
    return isinstance(name, str) and get(name) is not None


def describe(pack) -> dict:
    info = content_guard.content(pack.manifest)
    ui = pack.manifest.get("ui") or {}
    out = {"name": pack.name, "title": str(ui.get("title") or pack.name), "builtin": pack.builtin,
           "form": bool(ui.get("form")), "preview": bool(ui.get("preview")),
           "run_label": str(ui.get("run_label") or "开始出图"), "rating": info["rating"],
           "version": pack.manifest.get("version")}
    if info["rating"] == content_guard.RATING_ADULT:
        out.update(label=info["label"], min_age=info["min_age"], confirm=str(info.get("confirm") or ""),
                   local_only=True)
    return out


def form_tools() -> list[dict]:
    """设置/表单里可选的工具：ui.form=true 的 module 工具，不可用的标出原因。"""
    out = []
    try:
        found = _packs().discover()
    except Exception:
        return out
    for pack in found:
        if pack.manifest["entry"]["kind"] != "module" or not (pack.manifest.get("ui") or {}).get("form"):
            continue
        item = describe(pack)
        problem = _packs().gate(pack)
        if problem:
            item["unavailable"] = (problem.get("error") or {}).get("message") or "不可用。"
        out.append(item)
    return out


def _inside(root: str, path: str) -> bool:
    """path 在 root 里面（都已 realpath）。Windows 上不同盘符直接算在外面。"""
    try:
        return os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:
        return False


def load_builtin(path: str, folder: str):
    """import 内置工具的代码文件。文件必须在 folder 里（realpath），改了文件会重新加载。"""
    root = os.path.realpath(folder)
    real = os.path.realpath(path)
    if not _inside(root, real) or real == root or not os.path.isfile(real):
        raise GuardError("tool_error", "工具代码不在自己的工具目录里。")
    stamp = os.stat(real).st_mtime_ns
    cached = _LOADED.get(real)
    if cached and cached[0] == stamp:
        return cached[1]
    name = "mas_tool_" + re.sub(r"\W", "_", os.path.basename(root))
    spec = importlib.util.spec_from_file_location(name, real)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    _LOADED[real] = (stamp, module)
    return module


def _ctx(pack, config: dict) -> dict:
    info = content_guard.content(pack.manifest)
    ctx = {"tool": pack.name, "config": dict(config or {}), "rating": info["rating"]}
    if info["rating"] == content_guard.RATING_ADULT:
        ctx.update(min_age=info["min_age"], label=info["label"])
    return ctx


def config(pack) -> dict:
    return _packs().config_for(pack)


def _tool_error(result) -> GuardError | None:
    error = result.get("error") if isinstance(result, dict) else None
    if not error:
        return None
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = str(error.get("code") or "tool_error")
    code = code if _CODE.match(code) else "tool_error"
    message = str(error.get("message") or "工具拒绝了这次请求。")[:500]
    field = error.get("field")
    return GuardError(code, message, field if isinstance(field, str) and _CODE.match(field) else "")


def _call(pack, function: str, payload, cfg: dict):
    problem = _packs().gate(pack)
    if problem:
        error = problem.get("error") or {}
        raise GuardError(str(error.get("code") or "permission_denied"), str(error.get("message") or "工具不可用。"))
    ctx = _ctx(pack, cfg)
    if pack.builtin:
        module = load_builtin(pack.module_path(), pack.folder)
        fn = getattr(module, function, None)
        if not callable(fn):
            if function == "check_config":
                return {}
            raise GuardError("tool_error", f"工具 {pack.name} 没有 {function}()。")
        try:
            result = fn(ctx) if function == "catalog" else fn(payload, ctx)
        except GuardError:
            raise
        except Exception as exc:
            if hasattr(exc, "public") and hasattr(exc, "code"):
                public = exc.public()
                raise GuardError(str(public.get("code") or "tool_error"), str(public.get("message") or ""),
                                 str(public.get("field") or "")) from exc
            print(f"module tool {pack.name}.{function} failed {type(exc).__name__}", file=sys.stderr)
            raise GuardError("tool_error", "工具代码出错了。") from exc
    else:
        from services.tools import sandbox

        result = sandbox.run(pack, payload if function != "catalog" else {}, ctx, pack.effective(), function=function)
        if isinstance(result, dict) and result.get("ok") is False and isinstance(result.get("error"), dict):
            raise _tool_error(result)
        if isinstance(result, dict):
            result = {k: v for k, v in result.items() if k not in ("ok", "tool")}
    if not isinstance(result, dict):
        raise GuardError("tool_error", "工具没有返回对象。")
    problem = _tool_error(result)
    if problem:
        raise problem
    return result


def catalog(pack, cfg: dict | None = None) -> dict:
    """表单目录（已过守卫过滤）+ 工具说明。"""
    cfg = config(pack) if cfg is None else cfg
    view = _call(pack, "catalog", None, cfg)
    view = content_guard.filter_catalog(pack.manifest, view)
    out = {"tool": pack.name, **describe(pack),
           "fields": view.get("fields") or [], "extra_fields": view.get("extra_fields") or [],
           "defaults": view.get("defaults") if isinstance(view.get("defaults"), dict) else {}}
    return out


def _labels(pack, cfg) -> dict:
    try:
        view = _call(pack, "catalog", None, cfg)
    except GuardError:
        return {}
    out = {}
    for group in ("fields", "extra_fields"):
        for field in view.get(group) or []:
            if isinstance(field, dict) and isinstance(field.get("name"), str):
                out[field["name"]] = str(field.get("label") or field["name"])
    return out


def _values(values) -> dict:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise GuardError("invalid_parameter", "字段要是对象。")
    out = {}
    for key, value in values.items():
        if not isinstance(key, str) or not _CODE.match(key):
            raise GuardError("invalid_field", f"不认识的字段：{str(key)[:40]}。")
        if value is None:
            continue
        if not isinstance(value, str):
            raise GuardError("invalid_field", f"{key} 要是文字。", key)
        if len(value) > MAX_TEXT:
            raise GuardError("invalid_field", f"{key} 太长了。", key)
        out[key] = value
    return out


def compose(pack, values, cfg: dict | None = None, *, phase: str = "run") -> dict:
    """preview / run：宿主先查输入，再调工具代码，再查输出。返回 {prompt, summary, fields, used_defaults, rating}。"""
    cfg = config(pack) if cfg is None else cfg
    values = _values(values)
    try:
        content_guard.check_inputs(pack.manifest, values)
    except GuardError as exc:
        labels = _labels(pack, cfg)  # 只在拒绝时才取标签，让提示更好读。
        if exc.field and labels.get(exc.field):
            content_guard.check_inputs(pack.manifest, {exc.field: values[exc.field]}, labels)
        raise
    function = "preview" if phase == "preview" else "run"
    result = _call(pack, function, values, cfg)
    if function == "preview" and "prompt" not in result:
        result = _call(pack, "run", values, cfg)
    result = content_guard.check_output(pack.manifest, result)
    used = result.get("used_defaults")
    fields = result.get("fields")
    return {"prompt": result["prompt"], "summary": str(result.get("summary") or "")[:2000],
            "fields": fields if isinstance(fields, dict) else {},
            "used_defaults": [str(x) for x in used] if isinstance(used, list) else [],
            "rating": result["rating"]}


def check_config(name: str, merged: dict) -> dict | None:
    """设置页保存前的交叉检查。不是 module 工具返回 None；只调内置工具的 check_config（已安装的不在宿主进程跑）。"""
    pack = get(name)
    if pack is None:
        return None
    if not pack.builtin:
        return {}
    try:
        result = _call(pack, "check_config", dict(merged), dict(merged))
    except GuardError as exc:
        return {exc.field or "config": exc.message}
    return {str(k): str(v) for k, v in result.items()} if isinstance(result, dict) else {}
