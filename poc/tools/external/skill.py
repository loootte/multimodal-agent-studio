"""外部技能。只声明名字和机制，没有正文、字节或路径。"""

from __future__ import annotations

from tools.api import Tool


def tools() -> list[Tool]:
    return [
        Tool(
            name="read_skill",
            description="读取已安装技能的做法。只返回做法，不返回本机正文、字节或路径。",
            order=60,
            mutating=False,
            parameters={
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
            },
        ),
        Tool(
            name="build_skill_page",
            description="按已安装技能做一张可打开的等距页面。不调用 generate_image。",
            order=70,
            mutating=False,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "subject"],
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "description": "已安装技能的名字。这一刀是 anatomy。",
                    },
                    "subject": {
                        "type": "string",
                        "minLength": 1,
                        "description": "要说明的机制，短名称。不是画面描述，也不是技能正文。",
                    },
                },
            },
        ),
    ]
