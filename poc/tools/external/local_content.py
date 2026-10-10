"""外部用途：接受、列出和删除本会话的本地内容。只声明参数。"""

from __future__ import annotations

from tools.api import Tool


def tools() -> list[Tool]:
    return [
        Tool(
            name="accept_local_content",
            description="向用户收集一条留在本机的内容。可以是文字或文件。不要填写正文、字节、路径或文件名。",
            order=10,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["purpose"],
                "properties": {
                    "purpose": {
                        "type": "string",
                        "enum": ["image_prompt", "keep"],
                        "description": "用途。出图用 image_prompt，留在本机用 keep。其他文字会被丢掉。",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["text", "file"],
                        "description": "text 收集文字，file 收集文件。不填则是 text。",
                    },
                },
            },
        ),
        Tool(
            name="list_local_content",
            description="列出本会话的本地内容。只有句柄、种类、媒体类型、大小和用途。",
            order=20,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
        Tool(
            name="forget_local_content",
            description="丢掉本会话的一枚内容句柄。不要填写正文。",
            order=30,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["content_handle"],
                "properties": {
                    "content_handle": {
                        "type": "string",
                        "description": "本会话本地内容的句柄。",
                    },
                },
            },
        ),
    ]
