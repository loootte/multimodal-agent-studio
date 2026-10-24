"""角色图片定制（#53）：九个字段 → 英文提示词。

预设在 toolpacks/builtin/character_image/presets.json；工具配置（#28）里可以改默认值、用 JSON 追加/覆盖预设。
每个字段可以填预设键、中文标签或一小段自由文字；没填用默认值。这里只拼文字和做安全检查，出图仍走 generate_image。
"""

from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACK = os.path.join(ROOT, "toolpacks", "builtin", "character_image")
TOOL = "character_image"
FIELDS = ("gender", "age", "build", "hairstyle", "hair_color", "eye_color", "outfit", "pose", "background")
MAX_FIELD = 200
QUALITY = "character illustration, full body, highly detailed, best quality"
SAFE_TAIL = "tasteful, fully clothed, safe for work"
MINOR_TAIL = "age-appropriate, modest clothing, wholesome, non-sexualized"

# 露骨/色情：自由文字里出现就拒绝（全部小写比较）。
_EXPLICIT = (
    "nsfw", "nude", "naked", "nudity", "topless", "bottomless", "lingerie", "underwear", "bikini", "erotic",
    "sexy", "sexual", "porn", "hentai", "fetish", "seductive", "cleavage", "see-through", "lewd", "bdsm",
    "裸", "色情", "情色", "性感", "内衣", "比基尼", "走光", "露点", "诱惑", "挑逗", "暴露", "涩图", "福利图", "r18", "18禁",
)
# 自由文字里表示未成年的词。
_MINOR_WORDS = (
    "child", "kid", "teen", "teenager", "minor", "loli", "shota", "little girl", "little boy", "underage",
    "schoolgirl", "schoolboy", "toddler", "baby",
    "儿童", "小孩", "孩子", "幼女", "幼童", "未成年", "萝莉", "正太", "小学生", "初中生", "中学生", "高中生", "少年", "少女", "婴儿",
    "岁的女孩", "岁的男孩",
)
# 自由文字里的挑逗姿势/暴露服装（未成年时拒绝）。
_SUGGESTIVE_WORDS = (
    "swimsuit", "swimwear", "lying", "reclining", "bed", "short skirt", "miniskirt", "crop top", "tight",
    "泳装", "泳衣", "躺", "卧", "床", "短裙", "超短", "露脐", "紧身",
)
_AGE_NUMBER = re.compile(r"(\d{1,2})\s*(?:岁|years?\s*old|yo\b)")


class CharacterError(ValueError):
    def __init__(self, code: str, message: str, field: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def public(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.field:
            out["field"] = self.field
        return out


def base_presets() -> dict:
    with open(os.path.join(PACK, "presets.json"), encoding="utf-8") as handle:
        return json.load(handle)["fields"]


def _overrides(raw) -> dict:
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CharacterError("invalid_presets", f"presets_json 不是合法 JSON：{exc.msg}。") from exc
    if not isinstance(data, dict):
        raise CharacterError("invalid_presets", "presets_json 要是对象：{字段名: [预设, ...]}。")
    for name, options in data.items():
        if name not in FIELDS:
            raise CharacterError("invalid_presets", f"presets_json 里不认识的字段：{name}。")
        if not isinstance(options, list):
            raise CharacterError("invalid_presets", f"presets_json.{name} 要是列表。")
        for item in options:
            if not (isinstance(item, dict) and all(isinstance(item.get(k), str) and item.get(k).strip() for k in ("key", "label", "prompt"))):
                raise CharacterError("invalid_presets", f"presets_json.{name} 的每一项要有 key、label、prompt 三个非空文字。")
            if _explicit(item["prompt"]) or _explicit(item["label"]):
                raise CharacterError("unsafe_content", f"presets_json.{name}.{item['key']} 含露骨内容。")
    return data


def presets(config: dict | None = None) -> dict:
    """生效的预设：内置 + 配置里追加/覆盖。"""
    fields = base_presets()
    for name, options in _overrides((config or {}).get("presets_json")).items():
        current = fields[name]["options"]
        for item in options:
            clean = {"key": item["key"].strip(), "label": item["label"].strip(), "prompt": item["prompt"].strip()[:MAX_FIELD]}
            for flag in ("minor", "suggestive"):
                if item.get(flag) is True:
                    clean[flag] = True
            if isinstance(item.get("minor_prompt"), str) and item["minor_prompt"].strip():
                clean["minor_prompt"] = item["minor_prompt"].strip()[:MAX_FIELD]
            for index, old in enumerate(current):
                if old["key"] == clean["key"]:
                    current[index] = clean
                    break
            else:
                current.append(clean)
    return fields


def defaults(config: dict | None = None) -> dict:
    config = config or {}
    fields = base_presets()
    out = {}
    for name in FIELDS:
        value = config.get(f"default_{name}")
        out[name] = value.strip() if isinstance(value, str) and value.strip() else _first_default(fields, name)
    return out


_BUILTIN_DEFAULTS = {"gender": "female", "age": "young_adult", "build": "slim", "hairstyle": "long_straight",
                     "hair_color": "black", "eye_color": "brown", "outfit": "casual", "pose": "standing", "background": "plain"}


def _first_default(fields, name):
    return _BUILTIN_DEFAULTS[name]


def _explicit(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in _EXPLICIT)


def _clean(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:MAX_FIELD]


_STEM_TAILS = ("头发", "眼睛", "瞳孔", "色的", "发", "色", "瞳", "眼", "的")


def _stem(text: str) -> str:
    """“红发”“红色”“红色头发”都看成“红”，只用来对预设。"""
    text = text.split("（")[0].strip()
    changed = True
    while changed and len(text) > 1:
        changed = False
        for tail in _STEM_TAILS:
            if text.endswith(tail) and len(text) > len(tail):
                text = text[: -len(tail)]
                changed = True
                break
    return text


def resolve(name: str, value, fields: dict) -> dict:
    """一个字段 → {key, label, prompt, custom, minor, suggestive}。预设按键或中文标签匹配，不中就当自由文字。"""
    text = _clean(value)
    if not text:
        raise CharacterError("invalid_field", f"{fields[name]['label']}不能为空。", name)
    lowered = text.lower()
    for item in fields[name]["options"]:
        if (lowered == item["key"].lower() or text == item["label"] or text == item["label"].split("（")[0]
                or (name in ("hair_color", "eye_color") and _stem(text) == _stem(item["label"]))):
            return {"key": item["key"], "label": item["label"], "prompt": item["prompt"], "custom": False,
                    "minor_prompt": item.get("minor_prompt") or item["prompt"],
                    "minor": bool(item.get("minor")), "suggestive": bool(item.get("suggestive"))}
    if _explicit(text):
        raise CharacterError("unsafe_content", f"{fields[name]['label']}里有露骨或色情内容，不能用来出图。", name)
    minor = False
    suggestive = any(word in lowered for word in _SUGGESTIVE_WORDS)
    if name == "age":
        minor = any(word in lowered for word in _MINOR_WORDS)
        number = _AGE_NUMBER.search(lowered)
        if number and int(number.group(1)) < 18:
            minor = True
    elif name in ("gender",):
        minor = any(word in lowered for word in _MINOR_WORDS)
    return {"key": "", "label": text, "prompt": text, "custom": True, "minor_prompt": text, "minor": minor, "suggestive": suggestive}


def compose(values: dict | None, config: dict | None = None) -> dict:
    """values 只含用户/模型给的字段；没给的用默认值。返回 prompt、每个字段的结果、用了默认值的字段。"""
    values = values if isinstance(values, dict) else {}
    unknown = [key for key in values if key not in FIELDS]
    if unknown:
        raise CharacterError("invalid_field", f"不认识的字段：{'、'.join(sorted(unknown))}。")
    fields = presets(config)
    base = defaults(config)
    chosen = {}
    used_defaults = []
    for name in FIELDS:
        raw = values.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = base[name]
            used_defaults.append(name)
        elif not isinstance(raw, str):
            raise CharacterError("invalid_field", f"{fields[name]['label']}要是文字。", name)
        chosen[name] = resolve(name, raw, fields)
    minor = chosen["age"]["minor"] or chosen["gender"]["minor"]
    if minor:
        for name in ("outfit", "pose"):
            if chosen[name]["suggestive"]:
                raise CharacterError(
                    "minor_unsafe",
                    f"未成年角色不能搭配“{chosen[name]['label']}”这样的{fields[name]['label']}。请换一个{fields[name]['label']}或年龄。",
                    name,
                )
    quality = _clean((config or {}).get("quality_tags")) if (config or {}).get("quality_tags") is not None else QUALITY
    if _explicit(quality):
        raise CharacterError("unsafe_content", "quality_tags 里有露骨内容。")
    gender = chosen["gender"]["minor_prompt" if minor else "prompt"]
    parts = [quality, gender, chosen["age"]["prompt"], chosen["build"]["prompt"], chosen["hairstyle"]["prompt"], chosen["hair_color"]["prompt"],
             chosen["eye_color"]["prompt"], "wearing " + chosen["outfit"]["prompt"], chosen["pose"]["prompt"],
             chosen["background"]["prompt"], MINOR_TAIL if minor else SAFE_TAIL]
    prompt = ", ".join(part for part in parts if part)
    return {
        "prompt": prompt,
        "fields": {name: {k: chosen[name][k] for k in ("key", "label", "custom")} for name in FIELDS},
        "used_defaults": used_defaults,
        "minor": minor,
        "summary": summary(chosen, fields),
    }


def summary(chosen: dict, fields: dict) -> str:
    return "，".join(f"{fields[name]['label']}：{chosen[name]['label']}" for name in FIELDS)


def catalog(config: dict | None = None) -> dict:
    """给网页表单：每个字段的预设（键、中文标签、英文片段、标记）和生效默认值。"""
    fields = presets(config)
    return {
        "fields": [
            {"name": name, "label": fields[name]["label"], "options": fields[name]["options"]}
            for name in FIELDS
        ],
        "defaults": defaults(config),
    }


def check_config(merged: dict) -> dict:
    """工具配置保存前的交叉检查：预设 JSON 合法、默认值能拼出安全的提示词。"""
    try:
        compose({}, merged)
    except CharacterError as exc:
        return {exc.field and f"default_{exc.field}" or "presets_json": exc.message}
    return {}
