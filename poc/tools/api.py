"""一个工具文件要交出的对象。注册表不写工具名单。"""

from __future__ import annotations


class ToolSpecError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Tool:
    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict,
        order: int = 1000,
        validate=None,
        call=None,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.order = order
        self.validate = validate
        self.call = call


def tool_error(tool: str, code: str, message: str, parameter: str | None = None, *, executed=None) -> dict:
    error = {"code": code, "message": message}
    if parameter:
        error["parameter"] = parameter
    if executed is not None:
        return {"ok": False, "tool": tool, "executed": executed, "error": error}
    return {"ok": False, "tool": tool, "error": error}
