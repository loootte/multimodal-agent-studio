"""外部用途：文生图。只声明内容句柄和公开字段。"""

from __future__ import annotations

from tools.api import Tool


def tools() -> list[Tool]:
    from tools._support import load_styles

    styles = sorted(load_styles())
    properties = {
        "content_handle": {
            "type": "string",
            "description": "本会话本地内容的句柄。",
        },
        "aspect_ratio": {
            "type": "string",
            "enum": ["1:1", "16:9", "9:16"],
            "description": "画幅。",
        },
        "seed": {
            "type": "integer",
            "minimum": 0,
            "description": "可选。不传则由本机生成。",
        },
    }
    if styles:
        properties["style"] = {
            "type": "string",
            "enum": styles,
            "description": "可选。只能是白名单里的名字。",
        }
    return [Tool(
        name="generate_image",
        description="用本机会话中的文字句柄生成一张图片。不要填写画面描述。文件句柄不能用来出图。",
        order=40,
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["content_handle"],
            "properties": properties,
        },
    )]
