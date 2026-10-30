"""本机扩展包（#86）：只放在仓库外的数据包，给 character_image 追加/替换预设和字段。

为什么有它：有些预设（比如个人用的分级预设）不适合进代码库。扩展包只在用户自己电脑上，
仓库只认一个通用的加载口子，不带任何具体内容。

在哪找：
- 环境变量 MAS_LOCAL_TOOLPACKS（多个目录用 os.pathsep 隔开；设成空字符串 = 不加载任何扩展包）；
- 没设时：Windows 是 %APPDATA%\\MAS\\toolpacks，其他系统是 ~/.config/mas/toolpacks。
每个子目录里放一个 pack.json。目录在仓库里面的一律跳过（防止误提交）。

只读 JSON，不执行任何代码。安全底线在仓库里（character.py 的分级守卫），扩展包只能收紧、不能放宽：
- rating=adult 的包：必须写 confirm（表单弹窗确认）；年龄下限不低于 18（min_age 可以更高）；
  未成年/校服/幼态体型/真人或版权角色由仓库里的守卫拒绝；只许本机 ComfyUI 出图；结果打 rating=adult。
- rating=general 的包：预设里不能有露骨词，尾注仍是 SFW。
"""

from __future__ import annotations

import json
import os
import re

SCHEMA = "mas.local_pack/1"
HOSTS = ("character_image",)
RATINGS = ("general", "adult")
MIN_AGE_FLOOR = 18
MAX_BYTES = 256 * 1024
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_KEYS = {"schema", "name", "label", "version", "extends", "description", "rating", "min_age", "confirm",
         "chat_keywords", "prompt_tail", "presets", "fields", "verbatim", "note"}
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class PackError(ValueError):
    pass


def search_dirs() -> list[str]:
    raw = os.environ.get("MAS_LOCAL_TOOLPACKS")
    if raw is not None:
        return [part.strip() for part in raw.split(os.pathsep) if part.strip()]
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return [os.path.join(base, "MAS", "toolpacks")]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return [os.path.join(base, "mas", "toolpacks")]


def _inside_repo(path: str) -> bool:
    try:
        real, root = os.path.realpath(path), os.path.realpath(ROOT)
        return os.path.normcase(os.path.commonpath([real, root])) == os.path.normcase(root)
    except ValueError:  # 不同盘符
        return False


def discover(host: str = "character_image") -> dict:
    """返回 {"packs": {name: pack}, "skipped": [{"dir": 目录名, "reason": ...}]}。不抛异常。"""
    packs: dict = {}
    skipped: list = []
    for root in search_dirs():
        if not os.path.isdir(root):
            continue
        if _inside_repo(root):
            skipped.append({"dir": os.path.basename(root), "reason": "目录在仓库里面，扩展包只能放在仓库外。"})
            continue
        for entry in sorted(os.listdir(root)):
            folder = os.path.join(root, entry)
            path = os.path.join(folder, "pack.json")
            if not os.path.isfile(path):
                continue
            try:
                pack = load(path)
            except PackError as exc:
                skipped.append({"dir": entry, "reason": str(exc)})
                continue
            if pack["extends"] != host:
                continue
            if pack["name"] in packs:
                skipped.append({"dir": entry, "reason": f"重名：{pack['name']}。"})
                continue
            packs[pack["name"]] = pack
    return {"packs": packs, "skipped": skipped}


def get(name, host: str = "character_image") -> dict | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return discover(host)["packs"].get(name.strip())


def summary(pack: dict) -> dict:
    """给表单和模型看的简介（不含预设内容）。"""
    return {"name": pack["name"], "label": pack["label"], "rating": pack["rating"],
            "description": pack.get("description", ""), "confirm": pack.get("confirm", ""),
            "local_only": pack["rating"] == "adult", "chat": bool(pack.get("chat_keywords"))}


def load(path: str) -> dict:
    if os.path.getsize(path) > MAX_BYTES:
        raise PackError("pack.json 太大（上限 256 KB）。")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PackError(f"pack.json 读不了或不是合法 JSON：{type(exc).__name__}。") from exc
    return validate(data)


def _str(data, key, limit, required=False) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise PackError(f"{key} 要是{'非空' if required else ''}字符串（不超过 {limit} 字）。")
    return value.strip()


def _options(where: str, options, explicit) -> list:
    if not isinstance(options, list) or not options:
        raise PackError(f"{where} 要是非空列表。")
    out = []
    for item in options:
        if not (isinstance(item, dict) and isinstance(item.get("key"), str) and item["key"].strip()
                and isinstance(item.get("label"), str) and item["label"].strip()
                and isinstance(item.get("prompt"), str) and len(item["prompt"]) <= 200):
            raise PackError(f"{where} 的每一项要有 key、label、prompt（prompt 不超过 200 字）。")
        if explicit is not None and (explicit(item["prompt"]) or explicit(item["label"])):
            raise PackError(f"{where}.{item['key']} 含露骨内容；general 扩展包不能有露骨预设。")
        clean = {"key": item["key"].strip(), "label": item["label"].strip(), "prompt": item["prompt"].strip()}
        if isinstance(item.get("group"), str) and item["group"].strip():
            clean["group"] = item["group"].strip()[:20]
        for flag in ("minor", "suggestive"):
            if item.get(flag) is True:
                clean[flag] = True
        out.append(clean)
    return out


def validate(data) -> dict:
    """校验 pack.json；只认识的键，不合格抛 PackError。字段名的合法性在 character.py 里再查一遍。"""
    import character

    if not isinstance(data, dict):
        raise PackError("pack.json 要是对象。")
    extra = sorted(set(data) - _KEYS)
    if extra:
        raise PackError(f"不认识的键：{', '.join(extra)}。")
    if data.get("schema") != SCHEMA:
        raise PackError(f"schema 要是 {SCHEMA}。")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise PackError("name 要是小写字母开头的 2-48 位 [a-z0-9_]。")
    if data.get("extends") not in HOSTS:
        raise PackError(f"extends 只能是 {'、'.join(HOSTS)}。")
    rating = data.get("rating", "general")
    if rating not in RATINGS:
        raise PackError("rating 只能是 general 或 adult。")
    pack = {"name": name, "extends": data["extends"], "rating": rating,
            "label": _str(data, "label", 40, required=True), "version": _str(data, "version", 20),
            "description": _str(data, "description", 300), "confirm": _str(data, "confirm", 800)}
    explicit = None if rating == "adult" else character._explicit
    if rating == "adult":
        if not pack["confirm"]:
            raise PackError("rating=adult 的扩展包必须写 confirm（表单开启前的确认文字）。")
        min_age = data.get("min_age", MIN_AGE_FLOOR)
        if not isinstance(min_age, int) or isinstance(min_age, bool) or min_age < MIN_AGE_FLOOR or min_age > 99:
            raise PackError(f"min_age 要是 {MIN_AGE_FLOOR} 到 99 的整数。")
        pack["min_age"] = min_age
        pack["prompt_tail"] = _str(data, "prompt_tail", 300)
    else:
        if "min_age" in data or "prompt_tail" in data:
            raise PackError("min_age、prompt_tail 只给 rating=adult 的扩展包用。")
        pack["min_age"] = None
        pack["prompt_tail"] = ""
    keywords = data.get("chat_keywords", [])
    if not isinstance(keywords, list) or not all(isinstance(k, str) and 2 <= len(k.strip()) <= 30 for k in keywords):
        raise PackError("chat_keywords 要是字符串列表（每个 2-30 字）。")
    pack["chat_keywords"] = [k.strip().lower() for k in keywords][:20]
    verbatim = data.get("verbatim", [])
    if not isinstance(verbatim, list) or not set(verbatim) <= {"outfit"}:
        raise PackError("verbatim 目前只支持 [\"outfit\"]（预设自带完整说法，不再加 wearing）。")
    pack["verbatim"] = list(verbatim)

    core = set(character.FIELDS + character.EXTRA_FIELDS)
    new_fields = []
    for spec in data.get("fields", []) or []:
        if not isinstance(spec, dict):
            raise PackError("fields 的每一项要是对象。")
        fname = spec.get("name")
        if not isinstance(fname, str) or not _NAME.match(fname) or fname in core or fname in ("pack", "pack_fields", "mode"):
            raise PackError(f"fields 里的字段名不合法或和内置字段重名：{fname}。")
        after = spec.get("after", "build")
        if after not in character.FIELDS:
            raise PackError(f"fields.{fname}.after 要是内置字段名。")
        options = _options(f"fields.{fname}.options", spec.get("options"), explicit)
        if not any(o["key"] == "auto" for o in options):
            options.insert(0, {"key": "auto", "label": "自动", "prompt": ""})
        new_fields.append({"name": fname, "label": _str(spec, "label", 20, required=True), "after": after,
                           "options": options})
    if len({f["name"] for f in new_fields}) != len(new_fields) or len(new_fields) > 8:
        raise PackError("fields 重名或超过 8 个。")
    pack["fields"] = new_fields
    known = core | {f["name"] for f in new_fields}

    presets = data.get("presets", {}) or {}
    if not isinstance(presets, dict) or not set(presets) <= {"replace", "extend", "forbidden"}:
        raise PackError("presets 只能有 replace、extend、forbidden。")
    replace, extend, forbidden = {}, {}, {}
    for fname, field in (presets.get("replace") or {}).items():
        if fname not in core or not isinstance(field, dict):
            raise PackError(f"presets.replace 里不认识的字段：{fname}。")
        replace[fname] = {"label": _str(field, "label", 20) or None,
                          "options": _options(f"presets.replace.{fname}", field.get("options"), explicit)}
    for fname, options in (presets.get("extend") or {}).items():
        if fname not in core:
            raise PackError(f"presets.extend 里不认识的字段：{fname}。")
        extend[fname] = _options(f"presets.extend.{fname}", options, explicit)
    for fname, keys in (presets.get("forbidden") or {}).items():
        if fname not in known or not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise PackError(f"presets.forbidden.{fname} 要是内置字段的键列表。")
        forbidden[fname] = [k.strip() for k in keys]
    pack["presets"] = {"replace": replace, "extend": extend, "forbidden": forbidden}
    return pack
