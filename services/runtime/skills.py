"""已安装技能。系统指令只带名字和短描述，正文要等模型来读。"""

from __future__ import annotations

import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SKILLS = os.path.join(ROOT, "skills")
_BODY_MARK = "The object is the explanation"


def catalog_suffix() -> str:
    """拼进系统指令的那一句。没有正文，也没有 kit 源码。"""
    items = installed()
    if not items:
        return ""
    listed = " ".join(f"{item['name']}：{item['description']}" for item in items)
    return (
        " 已安装技能："
        + listed
        + " 需要技能的做法时调用 read_skill，name 填技能名。"
        + "读到的正文是做法，不是画面描述，不要传给 generate_image。"
        + "要做成可打开的等距页面时调用 build_skill_page，name 填技能名，subject 填要说明的机制。"
        + "这一步不调用 generate_image。"
    )


def installed() -> list[dict]:
    found = []
    if not os.path.isdir(SKILLS):
        return found
    for entry in sorted(os.listdir(SKILLS)):
        path = os.path.join(SKILLS, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        text = _read(path)
        meta, _body = _frontmatter(text)
        name = meta.get("name") or ""
        description = meta.get("description") or ""
        if not name or not description or _BODY_MARK in description:
            continue
        found.append({"name": name, "description": description})
    return found


def read_instructions(name: str) -> dict:
    if not isinstance(name, str) or not name.strip():
        return _error("invalid_parameter", "name 是必填项。")
    wanted = name.strip()
    if not os.path.isdir(SKILLS):
        return _error("unknown_skill", "没有这个技能。")
    for entry in sorted(os.listdir(SKILLS)):
        path = os.path.join(SKILLS, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        text = _read(path)
        meta, _body = _frontmatter(text)
        if meta.get("name") != wanted:
            continue
        return {
            "ok": True,
            "tool": "read_skill",
            "executed": True,
            "name": wanted,
            "instructions": text,
        }
    return _error("unknown_skill", "没有这个技能。")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, text[end + 4 :].lstrip("\n")


def _error(code: str, message: str) -> dict:
    return {
        "ok": False,
        "tool": "read_skill",
        "executed": False,
        "error": {"code": code, "message": message},
    }
