"""示例工具包的入口：call(arguments, config) -> dict。参数已按清单校验过。"""

import random


def call(arguments: dict, config: dict) -> dict:
    sides = int(arguments.get("sides") or config.get("default_sides") or 6)
    times = int(arguments.get("times") or 1)
    rolls = [random.randint(1, sides) for _ in range(times)]
    return {"ok": True, "executed": True, "sides": sides, "rolls": rolls, "has_key": bool(config.get("api_key"))}
