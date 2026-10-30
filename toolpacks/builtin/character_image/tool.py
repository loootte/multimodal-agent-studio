"""character_image 的工具代码（#53 / #86）：字段 → 英文提示词。按「工具代码契约」实现（docs/tool-code-contract.md）。

契约（宿主调用，ctx 是纯 JSON：tool / config / rating / min_age）：
- catalog(ctx)          → {"fields": [...], "extra_fields": [...], "defaults": {...}}  给网页表单
- preview(fields, ctx)  → {"prompt", "summary", "fields", "used_defaults", "minor"}       只拼文字
- run(args, ctx)        → 同上；宿主把 prompt 登记成本轮句柄，再走 generate_image
- check_config(config, ctx) → {配置字段: 问题}                                        保存配置前
拒绝时返回 {"error": {"code", "message", "field"}}，不抛异常。

这个文件只依赖标准库和同目录的 presets.json，所以同一份代码既能在宿主进程里跑（内置、受信任），
也能复制进第三方工具包、在沙箱子进程里跑。分级（rating=adult）的硬规则不在这里：宿主的 content_guard
对任何声明 rating=adult 的工具都会在调用前后再查一遍，工具代码改不了。

presets.json 除了 fields 还可以有（第三方工具包用）：
- "added": [{"name": 字段名, "after": 内置字段名}]  新字段，提示词里紧跟在 after 后面，表单里归 pack 组
- "verbatim": ["outfit"]                             这些字段的预设不再加 "wearing"
- "prompt_tail": "..."                               rating=adult 时的尾注（不写用默认的成年声明）
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ("gender", "age", "ethnicity", "build", "hairstyle", "hair_color", "eye_color", "outfit", "pose", "background", "art_style", "reference")
CAMERA_FIELDS = ("shot", "angle", "composition")
LIGHT_FIELDS = ("light_type", "light_direction", "light_mood")
EXTRA_FIELDS = CAMERA_FIELDS + LIGHT_FIELDS + ("expression", "scene")
MAX_FIELD = 200
QUALITY = "character illustration, full body, highly detailed, best quality"
SAFE_TAIL = "tasteful, fully clothed, safe for work"
MINOR_TAIL = "age-appropriate, modest clothing, wholesome, non-sexualized"
RATED_TAIL = "all characters are adults clearly over {age} years old, mature adult features, original character"
RATING_ADULT = "adult"
RATING_GENERAL = "general"

# 露骨/色情：普通（general）模式下自由文字里出现就拒绝（全部小写比较）。宿主的 content_guard 另有一份兜底。
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
_AGE_NUMBER = re.compile(r"(\d{1,2})\s*(?:岁|歲|years?\s*old|yo\b|-year-old)")
_BUILTIN_DEFAULTS = {
    "gender": "female", "age": "young_adult", "ethnicity": "east_asian", "build": "slim",
    "hairstyle": "long_straight", "hair_color": "black", "eye_color": "brown", "outfit": "casual",
    "pose": "standing", "background": "plain", "art_style": "anime", "reference": "none",
}
_STEM_TAILS = ("头发", "眼睛", "瞳孔", "色的", "发", "色", "瞳", "眼", "的")


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


# ---------- 预设 ----------

def _load() -> dict:
    with open(os.path.join(HERE, "presets.json"), encoding="utf-8") as handle:
        return json.load(handle)


def base_presets() -> dict:
    return _load()["fields"]


def settings() -> dict:
    data = _load()
    added = [dict(item) for item in data.get("added") or [] if isinstance(item, dict) and item.get("name") in data["fields"]]
    return {"added": added, "verbatim": list(data.get("verbatim") or []), "prompt_tail": str(data.get("prompt_tail") or "")}


def _added_names() -> tuple:
    return tuple(item["name"] for item in settings()["added"])


def _explicit(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in _EXPLICIT)


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
        if name not in FIELDS + EXTRA_FIELDS + _added_names():
            raise CharacterError("invalid_presets", f"presets_json 里不认识的字段：{name}。")
        if not isinstance(options, list):
            raise CharacterError("invalid_presets", f"presets_json.{name} 要是列表。")
        for item in options:
            if not (isinstance(item, dict) and isinstance(item.get("key"), str) and item.get("key").strip()
                    and isinstance(item.get("label"), str) and item.get("label").strip()
                    and isinstance(item.get("prompt"), str)):
                raise CharacterError("invalid_presets", f"presets_json.{name} 的每一项要有 key、label、prompt（key/label 非空；prompt 可为空白，表示跳过）。")
            if not item["prompt"].strip() and item["key"].strip().lower() not in ("none", "auto", ""):
                raise CharacterError("invalid_presets", f"presets_json.{name} 的 prompt 为空时 key 只能是 none 或 auto。")
            if _explicit(item["prompt"]) or _explicit(item["label"]):
                raise CharacterError("unsafe_content", f"presets_json.{name}.{item['key']} 含露骨内容。")
    return data


def presets(config: dict | None = None, rated: bool = False) -> dict:
    """生效的预设：presets.json + 配置里追加/覆盖。rated 时去掉标了 minor 的项。"""
    fields = base_presets()
    for name, options in _overrides((config or {}).get("presets_json")).items():
        current = fields[name]["options"]
        for item in options:
            clean = {"key": item["key"].strip(), "label": item["label"].strip(),
                     "prompt": item["prompt"].strip()[:MAX_FIELD] if item["prompt"].strip() else ""}
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
    if rated:
        for field in fields.values():
            field["options"] = [item for item in field["options"] if not item.get("minor")]
    return fields


def _is_preset(fields: dict, name: str, value: str) -> bool:
    lowered = str(value).strip().lower()
    return any(lowered == item["key"].lower() or value == item["label"] for item in fields[name]["options"])


def defaults(config: dict | None = None, rated: bool = False) -> dict:
    config = config or {}
    fields = presets(config, rated)
    out = {}
    for name in FIELDS:
        value = config.get(f"default_{name}")
        out[name] = value.strip() if isinstance(value, str) and value.strip() else _BUILTIN_DEFAULTS[name]
        if rated and not _is_preset(fields, name, out[name]):
            # 分级模式下配置里的默认值不在预设里（被去掉了）：回到内置默认，不把它当自由文字。
            out[name] = _BUILTIN_DEFAULTS[name]
    for name in EXTRA_FIELDS + _added_names():
        out[name] = "auto"
    return out


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_FIELD]


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


def resolve(name: str, value, fields: dict, rated: bool = False) -> dict:
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
    if _explicit(text) and not rated:
        raise CharacterError("unsafe_content", f"{fields[name]['label']}里有露骨或色情内容，不能用来出图。", name)
    minor = False
    suggestive = any(word in lowered for word in _SUGGESTIVE_WORDS)
    if name in ("age", "gender"):
        minor = any(word in lowered for word in _MINOR_WORDS)
        number = _AGE_NUMBER.search(lowered)
        if name == "age" and number and int(number.group(1)) < 18:
            minor = True
    return {"key": "", "label": text, "prompt": text, "custom": True, "minor_prompt": text, "minor": minor, "suggestive": suggestive}


def compose(values: dict | None, config: dict | None = None, rated: bool = False, min_age: int = 18) -> dict:
    """values 只含用户/模型给的字段；没给的用默认值。"""
    values = values if isinstance(values, dict) else {}
    extra = settings()
    added = _added_names()
    names = FIELDS + EXTRA_FIELDS + added
    unknown = [key for key in values if key not in names]
    if unknown:
        raise CharacterError("invalid_field", f"不认识的字段：{'、'.join(sorted(map(str, unknown)))}。")
    fields = presets(config, rated)
    base = defaults(config, rated)
    chosen = {}
    used_defaults = []
    for name in names:
        raw = values.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = base[name]
            if name in FIELDS:
                used_defaults.append(name)
        elif not isinstance(raw, str):
            raise CharacterError("invalid_field", f"{fields[name]['label']}要是文字。", name)
        chosen[name] = resolve(name, raw, fields, rated)
    minor = chosen["age"]["minor"] or chosen["gender"]["minor"]
    if rated and minor:
        raise CharacterError("minor_unsafe", "分级工具不能画未成年角色。", "age" if chosen["age"]["minor"] else "gender")
    if minor:
        for name in ("outfit", "pose") + EXTRA_FIELDS + added:
            if chosen[name]["suggestive"]:
                raise CharacterError(
                    "minor_unsafe",
                    f"未成年角色不能搭配“{chosen[name]['label']}”这样的{fields[name]['label']}。请换一个{fields[name]['label']}或年龄。",
                    name,
                )
    quality = _clean((config or {}).get("quality_tags")) if (config or {}).get("quality_tags") is not None else QUALITY
    if _explicit(quality):
        raise CharacterError("unsafe_content", "quality_tags 里有露骨内容。")
    if chosen["shot"]["prompt"]:
        # 选了景别就不再固定 full body，免得和「半身」「特写」打架。
        quality = ", ".join(part for part in (p.strip() for p in quality.split(",")) if part and part.lower() != "full body")
    gender = chosen["gender"]["minor_prompt" if minor else "prompt"]
    # 顺序：画面风格 → 画质 → 性别 → 年龄 → 人种 → 体型 → 发型 → 发色 → 瞳色 → 表情 → 服装 → 姿势
    #       → 动作/场景 → 摄像机位 → 光照 → 背景 → 参考 → 尾注。presets.json 的 added 字段紧跟在 after 后面。
    ref = chosen["reference"]
    if ref["prompt"].strip():
        ref_part = (f'inspired by "{ref["prompt"]}", original character, not a copy of any copyrighted character'
                    if ref["custom"] else ref["prompt"])
    else:
        ref_part = ""
    outfit = chosen["outfit"]
    outfit_part = outfit["prompt"] if ("outfit" in extra["verbatim"] and not outfit["custom"]) else "wearing " + outfit["prompt"]
    order = [
        ("art_style", chosen["art_style"]["prompt"]),
        ("", quality),
        ("gender", gender),
        ("age", chosen["age"]["prompt"]),
        ("ethnicity", chosen["ethnicity"]["prompt"]),
        ("build", chosen["build"]["prompt"]),
        ("hairstyle", chosen["hairstyle"]["prompt"]),
        ("hair_color", chosen["hair_color"]["prompt"]),
        ("eye_color", chosen["eye_color"]["prompt"]),
        ("expression", chosen["expression"]["prompt"]),
        ("outfit", outfit_part),
        ("pose", chosen["pose"]["prompt"]),
        ("scene", chosen["scene"]["prompt"]),
        *((name, chosen[name]["prompt"]) for name in CAMERA_FIELDS),
        *((name, chosen[name]["prompt"]) for name in LIGHT_FIELDS),
        ("background", chosen["background"]["prompt"]),
        ("reference", ref_part),
    ]
    parts = []
    for name, part in order:
        parts.append(part)
        for spec in extra["added"]:
            if spec.get("after") == name:
                parts.append(chosen[spec["name"]]["prompt"])
    if rated:
        tail = extra["prompt_tail"] or RATED_TAIL.format(age=max(int(min_age or 18), 18))
    else:
        tail = MINOR_TAIL if minor else SAFE_TAIL
    parts.append(tail)
    prompt = ", ".join(part for part in parts if part)
    shown = FIELDS + tuple(name for name in names[len(FIELDS):] if chosen[name]["prompt"])
    return {
        "prompt": prompt,
        "fields": {name: {k: chosen[name][k] for k in ("key", "label", "custom")} for name in names},
        "used_defaults": used_defaults,
        "minor": minor,
        "rating": RATING_ADULT if rated else RATING_GENERAL,
        "summary": "，".join(f"{fields[name]['label']}：{chosen[name]['label']}" for name in shown),
    }


def form(config: dict | None = None, rated: bool = False) -> dict:
    """给网页表单：fields 是 #53 的十二个；摄像机位/光照/表情/动作场景在 extra_fields；presets.json 的 added 归 pack 组。"""
    fields = presets(config, rated)
    added = _added_names()
    return {
        "fields": [{"name": name, "label": fields[name]["label"], "options": fields[name]["options"]} for name in FIELDS],
        "extra_fields": [
            {"name": name, "label": fields[name]["label"], "options": fields[name]["options"],
             "group": "camera" if name in CAMERA_FIELDS else "light" if name in LIGHT_FIELDS
             else "pack" if name in added else "detail"}
            for name in EXTRA_FIELDS + added
        ],
        "defaults": defaults(config, rated),
    }


# ---------- 契约入口 ----------

def _ctx(ctx) -> tuple[dict, bool, int]:
    ctx = ctx if isinstance(ctx, dict) else {}
    config = ctx.get("config") if isinstance(ctx.get("config"), dict) else {}
    rated = ctx.get("rating") == RATING_ADULT
    try:
        min_age = max(int(ctx.get("min_age") or 18), 18)
    except (TypeError, ValueError):
        min_age = 18
    return config, rated, min_age


def _guarded(fn):
    try:
        return fn()
    except CharacterError as exc:
        return {"error": exc.public()}


def catalog(ctx=None) -> dict:
    config, rated, _ = _ctx(ctx)
    return _guarded(lambda: form(config, rated))


def preview(fields, ctx=None) -> dict:
    config, rated, min_age = _ctx(ctx)
    return _guarded(lambda: compose(fields, config, rated, min_age))


def run(args, ctx=None) -> dict:
    return preview(args, ctx)


def check_config(config, ctx=None) -> dict:
    """工具配置保存前的交叉检查：预设 JSON 合法、默认值能拼出安全的提示词。"""
    _, rated, min_age = _ctx(ctx)
    try:
        compose({}, config if isinstance(config, dict) else {}, rated, min_age)
    except CharacterError as exc:
        return {exc.field and f"default_{exc.field}" or "presets_json": exc.message}
    return {}
