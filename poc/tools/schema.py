"""按工具自己声明的参数模式做校验。不读取本地正文，也不执行工具。"""

from __future__ import annotations

from tools.api import tool_error


def check(name: str, parameters: dict, arguments: dict) -> dict | None:
    properties = parameters.get("properties") or {}
    if parameters.get("additionalProperties") is False:
        extra = sorted(set(arguments) - set(properties))
        if extra:
            return tool_error(name, "unexpected_parameter", f"不能传这些参数：{', '.join(extra)}。", extra[0], executed=False)
    for key in parameters.get("required") or []:
        if key not in arguments:
            return tool_error(name, "invalid_parameter", f"{key} 是必填项。", key, executed=False)
    for key, value in arguments.items():
        if key not in properties:
            continue
        problem = _property(name, key, properties[key], value)
        if problem:
            return problem
    return None


def _property(name: str, key: str, schema: dict, value) -> dict | None:
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        return tool_error(name, "invalid_parameter", f"{key} 的类型不对。", key, executed=False)
    if "enum" in schema and value not in schema["enum"]:
        return tool_error(name, "invalid_parameter", f"{key} 不在允许的值里。", key, executed=False)
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        return tool_error(name, "invalid_parameter", f"{key} 太短。", key, executed=False)
    minimum = schema.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and value < minimum:
        return tool_error(name, "invalid_parameter", f"{key} 小于允许的最小值。", key, executed=False)
    return None


def _type_ok(value, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    return True
