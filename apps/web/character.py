"""#53/#86 兼容层：角色定制的代码已经搬到工具目录 toolpacks/builtin/character_image/tool.py（entry.kind=module）。

这里只把它重新导出，给旧的 import character 用；新代码请走 module_tools（宿主守卫在那里）。
"""

from __future__ import annotations

import os

import content_guard
import module_tools

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACK = os.path.join(ROOT, "toolpacks", "builtin", "character_image")
TOOL = "character_image"

_engine = module_tools.load_builtin(os.path.join(PACK, "tool.py"), PACK)

FIELDS = _engine.FIELDS
CAMERA_FIELDS = _engine.CAMERA_FIELDS
LIGHT_FIELDS = _engine.LIGHT_FIELDS
EXTRA_FIELDS = _engine.EXTRA_FIELDS
CharacterError = _engine.CharacterError
RATING_ADULT = content_guard.RATING_ADULT
RATING_GENERAL = content_guard.RATING_GENERAL
base_presets = _engine.base_presets
looks_adult = content_guard.looks_adult


def compose(values: dict | None, config: dict | None = None) -> dict:
    return _engine.compose(values, config)


def catalog(config: dict | None = None) -> dict:
    return _engine.form(config)


def check_config(merged: dict) -> dict:
    return _engine.check_config(merged, {"config": merged})
