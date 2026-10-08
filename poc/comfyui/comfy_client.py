"""提交一张已经填好的模板，等待进度，下载输出。

#4 会把 WebSocket、history 和 interrupt 做成独立客户端。#2 的工具只调用 run 和 upload。
"""

from __future__ import annotations

import comfy_poc


def upload(base: str, path: str) -> str:
    return comfy_poc.upload_image(base, path)


def run(base: str, workflow: dict, fields: dict, timeout: int, output_dir: str) -> list[str]:
    return comfy_poc.execute_workflow(base, workflow, fields, timeout, output_dir)
