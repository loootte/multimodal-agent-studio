"""外部只读演示。只声明主题，没有正文参数。"""

from __future__ import annotations

from tools.api import Tool


def tools() -> list[Tool]:
    return [Tool(
        name="lookup_demo",
        description="只读演示。按主题返回一条固定说明。不改任何外部状态。",
        order=50,
        mutating=False,
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["topic"],
            "properties": {
                "topic": {
                    "type": "string",
                    "minLength": 1,
                    "description": "要查询的主题。",
                },
            },
        },
    )]
