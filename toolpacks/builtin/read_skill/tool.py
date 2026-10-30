"""本机读取已安装技能。只返回做法，不返回路径。"""

from __future__ import annotations

from services.tools.api import Tool
from services.tools.schema import check as check_schema

NAME = "read_skill"
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "已安装技能的名字。这一刀是 anatomy。",
        },
    },
}


def validate(arguments: dict) -> dict | None:
    return check_schema(NAME, PARAMETERS, arguments)


def call(arguments: dict) -> dict:
    problem = validate(arguments)
    if problem:
        return problem
    from services.runtime.skills import read_instructions

    return read_instructions(arguments.get("name") or "")


def tools() -> list[Tool]:
    return [Tool(
        name=NAME,
        description="读取已安装技能的做法。只返回做法，不返回本机正文、字节或路径。",
        order=40,
        mutating=False,
        parameters=PARAMETERS,
        validate=validate,
        call=call,
    )]
