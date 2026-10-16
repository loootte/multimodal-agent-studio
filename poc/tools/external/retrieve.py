"""外部用途：检索本会话的附件、本地内容和生成结果。只声明查询词。"""

from __future__ import annotations

from tools.api import Tool


def tools() -> list[Tool]:
    return [Tool(
        name="retrieve_local",
        description="检索本会话的附件、本地内容和生成结果。只传入查询词。结果只有来源标识、种类、媒体类型、大小和可打开的地址。不要填写正文、字节、路径或文件名。",
        order=25,
        mutating=False,
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "检索词。匹配句柄、种类、用途、名称或媒体类型。",
                },
            },
        },
    )]
