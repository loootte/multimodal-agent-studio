"""外部用途：多张出图前先报计划张数。只声明参数，不碰本地正文。"""

from __future__ import annotations

from tools.api import Tool


def tools() -> list[Tool]:
    return [Tool(
        name="plan_images",
        description=(
            "用户的请求意味着要多张图片时（明说张数、一组、一套、系列、每季一张等），"
            "在第一次调用 generate_image 之前调用一次，报出你理解的总张数。只要一张时可以不调。"
            "之后仍然每张单独调用一次 generate_image。"
        ),
        order=55,
        mutating=False,
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["count"],
            "properties": {
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "这一轮要生成的总张数，1 到 20。",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选。每张的简短区别（如春、夏、秋、冬），不要写本地提示词正文。",
                },
            },
        },
    )]
