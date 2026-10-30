"""角色图片定制（#53）：十二个字段 → 英文提示词；#86 加摄像机位/光照/表情/动作场景和本机扩展包口子。

预设在 toolpacks/builtin/character_image/presets.json；工具配置（#28）里可以改默认值、用 JSON 追加/覆盖预设。
每个字段可以填预设键、中文标签或一小段自由文字；没填用默认值。reference 默认可空（无）。
这里只拼文字和做安全检查，出图仍走 generate_image。

#86：
- EXTRA_FIELDS（景别/角度/构图、光照类型/方向/氛围、表情、动作/场景）默认 auto = 不进提示词，
  所以不填时提示词和 #53 一字不差。
- 本机扩展包（local_packs.py，仓库外的 pack.json）可以替换/追加预设、加字段。仓库里不带任何扩展包内容。
  rating=adult 的扩展包一律走这里的分级守卫（_rated_guard）：年龄下限、未成年/校服/幼态体型、真人或版权角色
  都由仓库代码拒绝，扩展包只能收紧不能放宽；只许本机出图由 chat_run 按 rating 拦。
"""

from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACK = os.path.join(ROOT, "toolpacks", "builtin", "character_image")
TOOL = "character_image"
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
_AGE_NUMBER = re.compile(r"(\d{1,2})\s*(?:岁|歲|years?\s*old|yo\b|-year-old)")
_AGE_PREFIX = re.compile(r"(?:\bage[ds]?\b|年龄|年纪|年紀)\s*[:：=]?\s*(\d{1,2})(?!\d)")
_AGE_CN = re.compile(r"(?<![二三四五六七八九十百])(十[一二三四五六七八九]?|[一二两三四五六七八九])\s*岁")

# 分级守卫（#86）：rating=adult 的扩展包里，任何字段的自由文字出现就拒绝（minor_unsafe）。
# 中文按子串；英文按词边界（避免 kidney、stealth 之类误伤）。
_RATED_MINOR_CN = (
    "少女", "萝莉", "正太", "小孩", "幼女", "幼男", "幼童", "幼儿", "儿童", "孩子", "童颜", "学生", "小学", "初中", "中学", "高中",
    "未成年", "少年", "婴儿", "宝宝", "女孩", "男孩", "小女", "小男", "妹妹", "loli", "shota",
)
_RATED_MINOR_EN = re.compile(
    r"(?<![a-z0-9])(?:loli[a-z]*|lolita|shota[a-z]*|child[a-z]*|kids?|kiddo|teen[a-z]*|tween[a-z]*|underage[a-z]*|minors?|"
    r"schoolgirls?|schoolboys?|school\s*kids?|students?|pupils?|toddlers?|bab(?:y|ies)|infants?|juvenile|preteen[a-z]*|"
    r"pubescent|prepubescent|little\s+(?:girl|boy)s?|young\s+(?:girl|boy)s?|petite\s+young|jk|jc|js|nymphet)(?![a-z0-9])"
)
# 分级扩展包里不许的幼态体型描述。
_YOUTHFUL_BODY = (
    "娇小", "幼态", "幼体", "平胸幼体", "幼儿体型", "萝莉体型", "未发育", "发育中", "稚嫩", "稚气", "童体", "小只", "迷你身材",
    "petite", "childlike", "child-like", "youthful body", "underdeveloped", "flat chested child", "small childish", "baby face",
)
# 分级扩展包里不许的校服/学生装。
_SCHOOL_WORDS = (
    "校服", "学生装", "学生服", "水手服", "体操服", "运动校服", "jk制服", "书包", "红领巾",
    "school uniform", "sailor uniform", "sailor fuku", "seifuku", "school swimsuit", "sukumizu", "gym uniform",
    "school bag", "randoseru", "classroom",
)
# 分级扩展包里不许的真人/版权角色线索：命中就让用户「只描述原创角色」。
_REAL_OR_IP = (
    "明星", "演员", "歌手", "网红", "主播", "偶像", "爱豆", "名人", "真人", "本人", "照片里", "照片上", "长得像", "一模一样",
    "同事", "同学", "邻居", "前女友", "前男友", "女朋友", "男朋友", "老婆", "老公", "某某",
    "原神", "崩坏", "星穹铁道", "火影", "海贼王", "死神", "鬼灭", "进击的巨人", "新世纪福音战士", "初音", "迪士尼", "漫威",
    "宝可梦", "美少女战士", "英雄联盟", "王者荣耀", "明日方舟", "碧蓝航线", "赛马娘", "蔚蓝档案", "间谍过家家", "咒术回战",
    "哈利波特", "星球大战", "最终幻想", "塞尔达", "马里奥", "东方project", "同人", "二创", "cos", "角色扮演",
    "celebrity", "celeb", "actress", "actor", "singer", "idol", "influencer", "streamer", "youtuber", "vtuber", "real person",
    "real-life", "lookalike", "look like", "looks like", "resembling", "fan art", "fanart", "genshin", "honkai", "naruto",
    "one piece", "pokemon", "disney", "marvel", "dc comics", "evangelion", "hatsune", "miku", "sailor moon", "overwatch",
    "league of legends", "final fantasy", "zelda", "nintendo", "touhou", "blue archive", "arknights", "azur lane",
    "umamusume", "spy x family", "demon slayer", "jujutsu", "harry potter", "star wars",
)
# 规则：《作品名》、@账号、英文「名 姓」样式的人名。
_IP_RULES = (re.compile(r"《[^》]+》"), re.compile(r"@\w+"))
_PERSON_NAME = re.compile(r"(?<![A-Za-z])[A-Z][a-z]{1,15}\s+[A-Z][a-z]{1,15}(?![A-Za-z])")
_PERSON_FIELDS = ("reference", "gender", "scene", "expression")  # 扩展包新加的字段也算


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


def _rated(pack: dict | None) -> bool:
    return bool(pack) and pack.get("rating") == RATING_ADULT


def _forbidden(pack: dict | None) -> dict:
    return {name: set(keys) for name, keys in ((pack or {}).get("presets", {}).get("forbidden") or {}).items()}


def _pack_fields(pack: dict | None) -> tuple:
    return tuple(spec["name"] for spec in (pack or {}).get("fields", []))


def _copy(value):
    return json.loads(json.dumps(value))


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
        if name not in FIELDS + EXTRA_FIELDS:
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


def presets(config: dict | None = None, pack: dict | None = None) -> dict:
    """生效的预设：内置 + 配置里追加/覆盖；带扩展包时再按包替换/追加/禁用，并加上包里的新字段。"""
    fields = base_presets()
    replaced = set()
    if pack:
        for name, field in pack["presets"]["replace"].items():
            fields[name] = {"label": field.get("label") or fields[name]["label"], "options": _copy(field["options"])}
            replaced.add(name)
        for name, extra in pack["presets"]["extend"].items():
            fields[name]["options"].extend(_copy(extra))
        for spec in pack["fields"]:
            fields[spec["name"]] = {"label": spec["label"], "options": _copy(spec["options"])}
    for name, options in _overrides((config or {}).get("presets_json")).items():
        if name in replaced:
            continue  # 扩展包整组替换的字段，配置里的覆盖不再叠上去。
        current = fields[name]["options"]
        for item in options:
            clean = {"key": item["key"].strip(), "label": item["label"].strip(), "prompt": item["prompt"].strip()[:MAX_FIELD] if item["prompt"].strip() else ""}
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
    if pack:
        banned = _forbidden(pack)
        rated = _rated(pack)
        for name, field in fields.items():
            field["options"] = [item for item in field["options"]
                                if not (rated and item.get("minor")) and item["key"] not in banned.get(name, ())]
    return fields


def defaults(config: dict | None = None, pack: dict | None = None) -> dict:
    config = config or {}
    fields = presets(config, pack) if pack else base_presets()
    out = {}
    for name in FIELDS:
        value = config.get(f"default_{name}")
        out[name] = value.strip() if isinstance(value, str) and value.strip() else _first_default(fields, name)
        if pack and not _is_preset(fields, name, out[name]):
            # 配置里的默认值（如 teen / petite / 校服）在扩展包预设里没有：回到内置默认，不把它当自由文字。
            out[name] = _BUILTIN_DEFAULTS[name]
    for name in EXTRA_FIELDS + _pack_fields(pack):
        out[name] = "auto"
    return out


def _is_preset(fields: dict, name: str, value: str) -> bool:
    lowered = str(value).strip().lower()
    return any(lowered == item["key"].lower() or value == item["label"] for item in fields[name]["options"])


_BUILTIN_DEFAULTS = {
    "gender": "female",
    "age": "young_adult",
    "ethnicity": "east_asian",
    "build": "slim",
    "hairstyle": "long_straight",
    "hair_color": "black",
    "eye_color": "brown",
    "outfit": "casual",
    "pose": "standing",
    "background": "plain",
    "art_style": "anime",
    "reference": "none",
}


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


def resolve(name: str, value, fields: dict, rated: bool = False) -> dict:
    """一个字段 → {key, label, prompt, custom, minor, suggestive}。预设按键或中文标签匹配，不中就当自由文字。
    rated=True（rating=adult 的扩展包）时自由文字可以露骨（只会本机出图），但先走 _rated_guard。"""
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
    if name == "age":
        minor = any(word in lowered for word in _MINOR_WORDS)
        number = _AGE_NUMBER.search(lowered)
        if number and int(number.group(1)) < 18:
            minor = True
    elif name in ("gender",):
        minor = any(word in lowered for word in _MINOR_WORDS)
    return {"key": "", "label": text, "prompt": text, "custom": True, "minor_prompt": text, "minor": minor, "suggestive": suggestive}


def _hit(text: str, words) -> str:
    lowered = text.lower()
    return next((word for word in words if word in lowered), "")


def _under_age(text: str) -> int | None:
    """自由文字里写的最小年龄（阿拉伯数字或「十五岁」这样的中文数字）；没写返回 None。"""
    lowered = text.lower()
    ages = [int(m.group(1)) for m in _AGE_NUMBER.finditer(lowered)]
    ages += [int(m.group(1)) for m in _AGE_PREFIX.finditer(lowered)]
    for m in _AGE_CN.finditer(text):
        word = m.group(1)
        digits = "一二三四五六七八九"
        if word.startswith("十"):
            ages.append(10 + (digits.index(word[1]) + 1 if len(word) > 1 else 0))
        else:
            ages.append(2 if word == "两" else digits.index(word) + 1)
    return min(ages) if ages else None


def _rated_guard(name: str, raw: str, fields: dict, base_fields: dict, pack: dict) -> None:
    """rating=adult 扩展包的硬规则（#86）：在解析预设之前跑，命中就拒绝并说明原因。仓库代码，扩展包改不了。"""
    mode = pack["label"]
    min_age = max(int(pack.get("min_age") or 18), 18)
    label = fields[name]["label"] if name in fields else name
    text = _clean(raw)
    lowered = text.lower()
    # 1. 基础预设里被包禁用的项、标了 minor 的项，按键或标签点名拒绝。
    banned = _forbidden(pack).get(name, set())
    for item in base_fields.get(name, {}).get("options", []):
        if (item["key"] in banned or item.get("minor")) and (
                lowered == item["key"].lower() or text == item["label"] or text == item["label"].split("（")[0]):
            if item.get("minor") or name == "age":
                reason = f"{mode}不能画未成年角色（{label}：{item['label']}）。{mode}年龄从 {min_age} 岁起。"
            elif name == "outfit":
                reason = f"{mode}禁用校服类服装（{item['label']}），会让角色看起来像学生。请换一套服装。"
            elif name == "build":
                reason = f"{mode}禁用幼态体型描述（{item['label']}）。请换成成年体型。"
            else:
                reason = f"{mode}禁用“{item['label']}”这类容易显得未成年的{label}。请换一个。"
            raise CharacterError("minor_unsafe", reason, name)
    # 2. 自由文字：未成年词、低于下限的年龄、幼态体型、校服/学生。
    word = _hit(text, _RATED_MINOR_CN)
    match = _RATED_MINOR_EN.search(lowered)
    if word or match:
        hit = word or match.group(0)
        raise CharacterError("minor_unsafe", f"{mode}不能画未成年或像未成年的角色（{label}里有“{hit}”）。", name)
    age = _under_age(text)
    if age is None and name == "age" and re.fullmatch(r"\d{1,3}", text):
        age = int(text)  # 年龄栏只写了数字。
    if age is not None and age < min_age:
        reason = "未成年" if age < 18 else f"{mode}要求 {min_age} 岁以上"
        raise CharacterError("minor_unsafe", f"{mode}不能用 {age} 岁的角色（{reason}）。", name)
    word = _hit(text, _YOUTHFUL_BODY)
    if word:
        raise CharacterError("minor_unsafe", f"{mode}禁用幼态体型描述（{label}里有“{word}”）。请描述成年体型。", name)
    word = _hit(text, _SCHOOL_WORDS)
    if word:
        raise CharacterError("minor_unsafe", f"{mode}禁用校服/学生相关内容（{label}里有“{word}”）。", name)
    # 3. 真人 / 版权角色。
    word = _hit(text, _REAL_OR_IP)
    if not word:
        for rule in _IP_RULES:
            found = rule.search(text)
            if found:
                word = found.group(0)
                break
    if not word and (name in _PERSON_FIELDS or name in _pack_fields(pack)):
        found = _PERSON_NAME.search(text)
        if found:
            word = found.group(0)
    if word:
        raise CharacterError(
            "not_original_character",
            f"{mode}不能参考真人或已有版权角色（{label}里有“{word}”）。请只描述原创角色。",
            name,
        )


def pack_consent(pack: dict | None, text) -> bool:
    """对话路径：用户这一句里有没有扩展包声明的关键词。包没写关键词 = 对话里不能用（只能走表单）。"""
    if not pack or not pack.get("chat_keywords"):
        return False
    lowered = str(text or "").lower()
    return any(word in lowered for word in pack["chat_keywords"])


def looks_adult(text) -> bool:
    """通用兜底：提示词本身露骨（用来挡第三方后端、给结果打 rating）。和扩展包无关。"""
    return _explicit(str(text or ""))


def compose(values: dict | None, config: dict | None = None, pack: dict | None = None) -> dict:
    """values 只含用户/模型给的字段；没给的用默认值。返回 prompt、每个字段的结果、用了默认值的字段。

    pack 是 local_packs 校验过的扩展包（调用方先确认用户开启）；这里只管预设和安全规则。"""
    values = values if isinstance(values, dict) else {}
    rated = _rated(pack)
    extra_names = _pack_fields(pack)
    names = FIELDS + EXTRA_FIELDS + extra_names
    unknown = [key for key in values if key not in names]
    if unknown:
        raise CharacterError("invalid_field", f"不认识的字段：{'、'.join(sorted(unknown))}。")
    fields = presets(config, pack)
    base_fields = presets(config) if pack else fields
    base = defaults(config, pack)
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
        elif rated:
            _rated_guard(name, raw, fields, base_fields, pack)
        chosen[name] = resolve(name, raw, fields, rated)
    minor = chosen["age"]["minor"] or chosen["gender"]["minor"]
    if rated and minor:
        raise CharacterError("minor_unsafe", f"{pack['label']}不能画未成年角色。", "age" if chosen["age"]["minor"] else "gender")
    if minor:
        for name in ("outfit", "pose") + EXTRA_FIELDS + extra_names:
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
    #       → 动作/场景 → 摄像机位（景别/角度/构图）→ 光照（类型/方向/氛围）→ 背景 → 参考 → 安全尾注。
    # 扩展包加的字段紧跟在它声明的 after 字段后面。
    # reference 为空/none 时完全不进提示词；自定义参考包一层“灵感/原创”以免暗示抄袭。
    ref = chosen["reference"]
    if ref["prompt"].strip():
        if ref["custom"]:
            ref_part = (
                f'inspired by "{ref["prompt"]}", original character, '
                "not a copy of any copyrighted character"
            )
        else:
            ref_part = ref["prompt"]
    else:
        ref_part = ""
    outfit = chosen["outfit"]
    if pack and "outfit" in pack.get("verbatim", ()) and not outfit["custom"]:
        outfit_part = outfit["prompt"]  # 扩展包声明了 verbatim：预设自带完整说法。
    else:
        outfit_part = "wearing " + outfit["prompt"]
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
        for spec in (pack or {}).get("fields", []):
            if spec["after"] == name:
                parts.append(chosen[spec["name"]]["prompt"])
    if rated:
        tail = pack.get("prompt_tail") or RATED_TAIL.format(age=max(int(pack.get("min_age") or 18), 18))
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
        "pack": pack["name"] if pack else None,
        "rating": RATING_ADULT if rated else RATING_GENERAL,
        "summary": summary(chosen, fields, shown),
    }


def summary(chosen: dict, fields: dict, names=FIELDS) -> str:
    return "，".join(f"{fields[name]['label']}：{chosen[name]['label']}" for name in names)


def catalog(config: dict | None = None, pack: dict | None = None, packs: list | None = None) -> dict:
    """给网页表单：每个字段的预设（键、中文标签、英文片段、标记、分组 group）和生效默认值。
    fields 仍是 #53 的十二个；#86 的摄像机位/光照/表情/动作场景放在 extra_fields，扩展包的新字段 group=pack。
    packs 是本机可用的扩展包简介（只有名字/标签/分级/确认文字，没有预设内容）。"""
    fields = presets(config, pack)
    extra = EXTRA_FIELDS + _pack_fields(pack)
    return {
        "fields": [
            {"name": name, "label": fields[name]["label"], "options": fields[name]["options"]}
            for name in FIELDS
        ],
        "extra_fields": [
            {"name": name, "label": fields[name]["label"], "options": fields[name]["options"],
             "group": "camera" if name in CAMERA_FIELDS else "light" if name in LIGHT_FIELDS
             else "pack" if name in _pack_fields(pack) else "detail"}
            for name in extra
        ],
        "defaults": defaults(config, pack),
        "pack": ({"name": pack["name"], "label": pack["label"], "rating": pack["rating"]} if pack else None),
        "packs": packs or [],
    }


def check_config(merged: dict) -> dict:
    """工具配置保存前的交叉检查：预设 JSON 合法、默认值能拼出安全的提示词。"""
    try:
        compose({}, merged)
    except CharacterError as exc:
        return {exc.field and f"default_{exc.field}" or "presets_json": exc.message}
    return {}
