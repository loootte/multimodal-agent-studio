"""本机只读演示工具。按主题返回固定说明，不改外部状态。"""

from __future__ import annotations

from tools.api import Tool, tool_error
from tools.schema import check as check_schema

NAME = "lookup_demo"
PARAMETERS = {
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
}


def validate(arguments: dict) -> dict | None:
    return check_schema(NAME, PARAMETERS, arguments)


def call(arguments: dict) -> dict:
    problem = validate(arguments)
    if problem:
        return problem
    topic = arguments.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return tool_error(NAME, "invalid_parameter", "topic 是必填项。", "topic")
    return {
        "ok": True,
        "tool": NAME,
        "executed": True,
        "fact": "studio-demo: " + topic.strip(),
    }


def tools() -> list[Tool]:
    return [Tool(
        name=NAME,
        description="只读演示。按主题返回一条固定说明。不改任何外部状态。",
        order=30,
        mutating=False,
        parameters=PARAMETERS,
        validate=validate,
        call=call,
    )]
