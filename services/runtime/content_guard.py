"""宿主的内容守卫（#86；#93 起在 services/runtime，apps/web/content_guard.py 只是兼容入口）：对声明 content.rating=adult 的工具强制执行，工具自己的代码改不了也绕不过。

#93 拆分：这里只管「审不审、审多严」（内容审核），不管内容发到哪里。
- 出图去向（成人内容只走本机 / 放行哪个第三方出图主机）是工具安全策略的路由项 adult_remote_hosts，由 chat_run 执行；
- 本地内容出不出站归本地正文那一套（send_local_to_model 锁定、remote_local_prompt 默认拒绝），也不在这里。
审核级别（工具安全策略 content_guard，默认 strict）：
- strict   全部检查（下面列的都查）。
- standard 不查版权角色 / 同人（not_original_character 只剩真人）。
- off      不做内容审核：普通工具也能出露骨内容、版权角色不查。
无论哪一级，底线（BASELINE）都生效：性化内容（分级工具，或文字本身露骨）里不能有未成年 / 像未成年的角色
（年龄、幼态体型、校服 / 学生），不能有真人（名人、熟人、真人照片、@账号、人名）。

module 工具（entry.kind=module）的每次 preview / run，宿主都会：
1. 调用前 check_inputs：逐个字段查用户/模型给的文字；
2. 调用后 check_output：再查工具拼好的最终提示词，并把分级强制设成清单里声明的 rating；
3. 目录 filter_catalog：从表单选项里去掉守卫不放行的项。

rating=adult：不能画未成年或像未成年的角色（含低于 min_age 的年龄）、校服/学生、幼态体型，不能参考真人或版权角色；
              结果标记为成人内容（发到哪里出图不归这里管，见 adult_remote_hosts，默认只走本机）。
rating=general：不能有露骨/色情内容（工具自己没拦的，这里兜底）。
"""

from __future__ import annotations

import re

RATING_ADULT = "adult"
RATING_GENERAL = "general"
LEVEL_STRICT = "strict"
LEVEL_STANDARD = "standard"
LEVEL_OFF = "off"
LEVELS = (LEVEL_STRICT, LEVEL_STANDARD, LEVEL_OFF)

# 露骨/色情（全部小写比较）。
EXPLICIT = (
    "nsfw", "nude", "naked", "nudity", "topless", "bottomless", "lingerie", "underwear", "bikini", "erotic",
    "sexy", "sexual", "porn", "hentai", "fetish", "seductive", "cleavage", "see-through", "lewd", "bdsm",
    "裸", "色情", "情色", "性感", "内衣", "比基尼", "走光", "露点", "诱惑", "挑逗", "暴露", "涩图", "福利图", "r18", "18禁",
)
_AGE_NUMBER = re.compile(r"(\d{1,2})\s*(?:岁|歲|years?\s*old|yo\b|-year-old)")
_AGE_PREFIX = re.compile(r"(?:\bage[ds]?\b|年龄|年纪|年紀)\s*[:：=]?\s*(\d{1,2})(?!\d)")
_AGE_CN = re.compile(r"(?<![二三四五六七八九十百])(十[一二三四五六七八九]?|[一二两三四五六七八九])\s*岁")
# 中文按子串；英文按词边界（避免 kidney、costume 之类误伤）。
_MINOR_CN = (
    "少女", "萝莉", "正太", "小孩", "幼女", "幼男", "幼童", "幼儿", "儿童", "孩子", "童颜", "学生", "小学", "初中", "中学", "高中",
    "未成年", "少年", "婴儿", "宝宝", "女孩", "男孩", "小女", "小男", "妹妹",
)
_MINOR_EN = re.compile(
    r"(?<![a-z0-9])(?:loli[a-z]*|lolita|shota[a-z]*|child[a-z]*|kids?|kiddo|teen[a-z]*|tween[a-z]*|underage[a-z]*|minors?|"
    r"schoolgirls?|schoolboys?|school\s*kids?|students?|pupils?|toddlers?|bab(?:y|ies)|infants?|juvenile|preteen[a-z]*|"
    r"pubescent|prepubescent|little\s+(?:girl|boy)s?|young\s+(?:girl|boy)s?|petite\s+young|jk|jc|js|nymphet)(?![a-z0-9])"
)
_YOUTHFUL = (
    "娇小", "幼态", "幼体", "平胸幼体", "幼儿体型", "萝莉体型", "未发育", "发育中", "稚嫩", "稚气", "童体", "小只", "迷你身材", "q版",
    "petite", "childlike", "child-like", "youthful body", "underdeveloped", "flat chested child", "small childish", "baby face",
    "chibi",
)
_SCHOOL = (
    "校服", "学生装", "学生服", "水手服", "体操服", "运动校服", "jk制服", "书包", "红领巾", "教室",
    "school uniform", "sailor uniform", "sailor fuku", "seifuku", "school swimsuit", "sukumizu", "gym uniform",
    "school bag", "randoseru", "classroom",
)
# 真人（底线，任何级别都查）。
_REAL = (
    "明星", "演员", "歌手", "网红", "主播", "偶像", "爱豆", "名人", "真人", "本人", "照片里", "照片上", "长得像", "一模一样",
    "同事", "同学", "邻居", "前女友", "前男友", "女朋友", "男朋友", "老婆", "老公", "某某",
    "celebrity", "celeb", "actress", "actor", "singer", "idol", "influencer", "streamer", "youtuber", "vtuber", "real person",
    "real-life", "lookalike", "look like", "looks like", "resembling",
)
# 版权角色 / 同人（只有 strict 查）。
_IP = (
    "原神", "崩坏", "星穹铁道", "火影", "海贼王", "死神", "鬼灭", "进击的巨人", "新世纪福音战士", "初音", "迪士尼", "漫威",
    "宝可梦", "美少女战士", "英雄联盟", "王者荣耀", "明日方舟", "碧蓝航线", "赛马娘", "蔚蓝档案", "间谍过家家", "咒术回战",
    "哈利波特", "星球大战", "最终幻想", "塞尔达", "马里奥", "东方project", "同人", "二创", "cos", "cosplay", "角色扮演",
    "fan art", "fanart", "genshin", "honkai", "naruto",
    "one piece", "pokemon", "disney", "marvel", "dc comics", "evangelion", "hatsune", "miku", "sailor moon", "overwatch",
    "league of legends", "final fantasy", "zelda", "nintendo", "touhou", "blue archive", "arknights", "azur lane",
    "umamusume", "spy x family", "demon slayer", "jujutsu", "harry potter", "star wars",
)
_REAL_OR_IP = _REAL + _IP
_REAL_RULES = (re.compile(r"@\w+"),)
_IP_RULES = (re.compile(r"《[^》]+》"),)
_PERSON_NAME = re.compile(r"(?<![A-Za-z])[A-Z][a-z]{1,15}\s+[A-Z][a-z]{1,15}(?![A-Za-z])")
_WORD_CACHE: dict = {}


class GuardError(ValueError):
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


def content(manifest: dict) -> dict:
    data = manifest.get("content") if isinstance(manifest, dict) else None
    data = dict(data) if isinstance(data, dict) else {}
    data["rating"] = data.get("rating") if data.get("rating") in (RATING_ADULT, RATING_GENERAL) else RATING_GENERAL
    if data["rating"] == RATING_ADULT:
        try:
            data["min_age"] = max(int(data.get("min_age") or 18), 18)
        except (TypeError, ValueError):
            data["min_age"] = 18
        data["label"] = str(data.get("label") or "成人模式")
    return data


def rated(manifest: dict) -> bool:
    return content(manifest)["rating"] == RATING_ADULT


def _normalize(text) -> str:
    """下划线、连字符当空格（school_uniform → school uniform），空白合并。"""
    return re.sub(r"\s+", " ", str(text or "").replace("_", " ")).strip()


def _word(word: str):
    pattern = _WORD_CACHE.get(word)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])")
        _WORD_CACHE[word] = pattern
    return pattern


def _hit(text: str, words) -> str:
    lowered = text.lower()
    for word in words:
        if word.isascii():
            if _word(word).search(lowered):
                return word
        elif word in lowered:
            return word
    return ""


def explicit(text) -> bool:
    lowered = _normalize(text).lower()
    return any(word in lowered for word in EXPLICIT)


looks_adult = explicit


def under_age(text: str) -> int | None:
    """文字里写的最小年龄（阿拉伯数字或「十五岁」这样的中文数字）；没写返回 None。"""
    lowered = text.lower()
    ages = [int(m.group(1)) for m in _AGE_NUMBER.finditer(lowered)]
    ages += [int(m.group(1)) for m in _AGE_PREFIX.finditer(lowered)]
    digits = "一二三四五六七八九"
    for m in _AGE_CN.finditer(text):
        word = m.group(1)
        if word.startswith("十"):
            ages.append(10 + (digits.index(word[1]) + 1 if len(word) > 1 else 0))
        else:
            ages.append(2 if word == "两" else digits.index(word) + 1)
    return min(ages) if ages else None


def level_of(value) -> str:
    return value if value in LEVELS else LEVEL_STRICT


def _rated_problem(text: str, info: dict, label: str, field: str, *, age_field: bool, people: bool,
                   level: str = LEVEL_STRICT) -> GuardError | None:
    """未成年和真人（底线）任何级别都查；版权角色只在 strict 查。"""
    mode = info["label"]
    min_age = info["min_age"]
    lowered = text.lower()
    word = _hit(text, _MINOR_CN)
    match = _MINOR_EN.search(lowered)
    if word or match:
        hit = word or match.group(0)
        return GuardError("minor_unsafe", f"{mode}不能画未成年或像未成年的角色（{label}里有“{hit}”）。{mode}年龄从 {min_age} 岁起。", field)
    age = under_age(text)
    if age is None and age_field and re.fullmatch(r"\d{1,3}", text.strip()):
        age = int(text.strip())  # 年龄栏只写了数字。
    if age is not None and age < min_age:
        reason = "未成年" if age < 18 else f"{mode}要求 {min_age} 岁以上"
        return GuardError("minor_unsafe", f"{mode}不能用 {age} 岁的角色（{reason}）。", field)
    word = _hit(text, _YOUTHFUL)
    if word:
        return GuardError("minor_unsafe", f"{mode}禁用幼态体型描述（{label}里有“{word}”）。请描述成年体型。", field)
    word = _hit(text, _SCHOOL)
    if word:
        return GuardError("minor_unsafe", f"{mode}禁用校服/学生相关内容（{label}里有“{word}”）。", field)
    word = _hit(text, _REAL)
    if not word:
        for rule in _REAL_RULES:
            found = rule.search(text)
            if found:
                word = found.group(0)
                break
    if not word and people:
        found = _PERSON_NAME.search(text)
        if found:
            word = found.group(0)
    if not word and level_of(level) == LEVEL_STRICT:
        word = _hit(text, _IP)
        if not word:
            for rule in _IP_RULES:
                found = rule.search(text)
                if found:
                    word = found.group(0)
                    break
    if word:
        return GuardError("not_original_character",
                          f"{mode}不能参考真人或已有版权角色（{label}里有“{word}”）。请只描述原创角色。", field)
    return None


def _baseline_info(info: dict) -> dict:
    """普通工具在 off 级别出了露骨内容时，按成人底线查（min_age 18）。"""
    if info["rating"] == RATING_ADULT:
        return info
    return dict(info, min_age=18, label="露骨内容")


def check_inputs(manifest: dict, values: dict, labels: dict | None = None, level: str = LEVEL_STRICT) -> None:
    """调用工具代码之前：逐个字段查。不过关抛 GuardError。level 见模块说明；底线任何级别都查。"""
    level = level_of(level)
    info = content(manifest)
    labels = labels or {}
    for name, raw in (values or {}).items():
        if not isinstance(raw, str):
            continue
        text = _normalize(raw)
        if not text:
            continue
        label = str(labels.get(name) or name)
        if info["rating"] == RATING_ADULT:
            problem = _rated_problem(text, info, label, name, age_field=name == "age", people=True, level=level)
            if problem:
                raise problem
        elif explicit(text):
            if level != LEVEL_OFF:
                raise GuardError("unsafe_content", f"{label}里有露骨或色情内容，不能用来出图。", name)
            problem = _rated_problem(text, _baseline_info(info), label, name, age_field=False, people=True, level=level)
            if problem:
                raise problem


def check_output(manifest: dict, result: dict, level: str = LEVEL_STRICT) -> dict:
    """工具代码返回以后：再查最终提示词，分级一律以清单为准（工具自称 general 也没用）。底线任何级别都查。"""
    level = level_of(level)
    if not isinstance(result, dict):
        raise GuardError("tool_error", "工具没有返回对象。")
    prompt = result.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise GuardError("tool_error", "工具没有返回提示词（prompt）。")
    if len(prompt) > 8000:
        raise GuardError("tool_error", "工具返回的提示词太长。")
    info = content(manifest)
    out = dict(result)
    if info["rating"] == RATING_ADULT:
        problem = _rated_problem(_normalize(prompt), info, "最终提示词", "", age_field=False, people=False, level=level)
        if problem:
            raise problem
        out["rating"] = RATING_ADULT
    else:
        if explicit(prompt):
            if level != LEVEL_OFF:
                raise GuardError("unsafe_content", "工具拼出的提示词里有露骨或色情内容，不能用来出图。")
            problem = _rated_problem(_normalize(prompt), _baseline_info(info), "最终提示词", "", age_field=False,
                                     people=False, level=level)
            if problem:
                raise problem
        out["rating"] = RATING_GENERAL  # 露骨与否由出图那一步再判（looks_adult），按成人内容路由
    return out


def filter_catalog(manifest: dict, view: dict, level: str = LEVEL_STRICT) -> dict:
    """表单目录：rated 工具去掉标了 minor 或守卫不放行的预设；general 工具去掉露骨预设（off 级别只去掉碰底线的露骨预设）。"""
    level = level_of(level)
    if not isinstance(view, dict):
        return {"fields": [], "extra_fields": [], "defaults": {}}
    info = content(manifest)
    adult = info["rating"] == RATING_ADULT
    out = dict(view)
    for group in ("fields", "extra_fields"):
        cleaned = []
        for field in view.get(group) or []:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            options = []
            for item in field.get("options") or []:
                if not isinstance(item, dict):
                    continue
                texts = [str(item.get(k) or "") for k in ("key", "label", "prompt")]
                if adult:
                    if item.get("minor") or any(
                            _rated_problem(_normalize(t), info, "", "", age_field=False, people=False, level=level)
                            for t in texts if t):
                        continue
                elif any(explicit(t) for t in texts[1:]):
                    if level != LEVEL_OFF or item.get("minor") or any(
                            _rated_problem(_normalize(t), _baseline_info(info), "", "", age_field=False, people=False,
                                           level=level) for t in texts if t):
                        continue
                options.append(item)
            cleaned.append(dict(field, options=options))
        out[group] = cleaned
    return out


def consent(manifest: dict, text) -> bool:
    """对话路径：rated 工具要用户这一句里有清单声明的关键词。没写关键词 = 对话里不能用（只能走表单）。"""
    words = content(manifest).get("chat_keywords") or []
    lowered = str(text or "").lower()
    return any(str(word).lower() in lowered for word in words)
