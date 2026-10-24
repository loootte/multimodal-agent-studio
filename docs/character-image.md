# 角色图片定制（issue #53）

一个标准化的角色出图工具：用户决定角色的 **性别、年龄、体型、发型、发色、瞳色、服装、姿势、背景** 九项。每项都有预设可选，也可以自己写；没填的项用默认值——一个**年轻女性角色**。

## 怎么用

**网页表单**：侧栏点「定制角色」。

- 每一项是一个下拉框（预设），选最后的「自定义…」就能自己写（最长 200 字）。
- 「恢复默认」把九项都还原成默认值；下面实时显示这次会发给出图服务的英文提示词。
- 选张数（1–20）、画幅（1:1 / 16:9 / 9:16）、出图后端（本机 ComfyUI / 第三方）、提示词去向（写进聊天记录 / 本地提示词）。
- 点「生成」：在当前对话里多出一轮，N 张就逐张调用 N 次 `generate_image`，进度、成片、停止都和普通出图一样。表单**不经过对话模型**，没配模型密钥也能用。

**对话**：直接说，例如“画一个红发女骑士角色”“画个银发蓝眼的法师人物，坐着看书”。模型调用 `character_image`，只填你说到的字段，其余用默认值，再用返回的句柄调用 `generate_image`；要几张会先 `plan_images`。

> 注意隐私闸门（ADR 0008）：只说“画一个红发女骑士”时，按默认规则这是**本地提示词**（1.3），原话不交给对话模型，直接在本机出图，不会走角色工具。说“角色”“人物立绘”或 “character” 时才把这句话交给模型，由它拆成字段。

## 默认值

| 字段 | 默认 |
| --- | --- |
| 性别 | 女性 |
| 年龄 | 青年（20 多岁） |
| 体型 | 纤细 |
| 发型 | 黑长直 |
| 发色 | 黑色 |
| 瞳色 | 棕色 |
| 服装 | 休闲装 |
| 姿势 | 站立 |
| 背景 | 纯色背景 |

默认提示词：

```
character illustration, full body, highly detailed, best quality, woman, young adult in their early twenties, slim build, long straight hair, black hair, brown eyes, wearing casual outfit, sweater and jeans, standing, relaxed pose, simple plain light background, tasteful, fully clothed, safe for work
```

## 预设

中文标签后面是键（表单、对话、配置里都能用键或中文标签）。⚠未成年 = 未成年预设；⚠ = 对未成年角色不允许的服装/姿势。

| 字段 | 预设 |
| --- | --- |
| 性别（`gender`） | 女性`female`、男性`male`、中性`androgynous` |
| 年龄（`age`） | 青年（20 多岁）`young_adult`、成年（30 多岁）`adult`、中年`middle_aged`、老年`elderly`、青少年（未成年）`teen`⚠未成年、儿童（未成年）`child`⚠未成年 |
| 体型（`build`） | 纤细`slim`、匀称`average`、健美`athletic`、强壮`muscular`、娇小`petite`、高挑`tall`、丰满圆润`plump` |
| 发型（`hairstyle`） | 长直发`long_straight`、长卷发`long_wavy`、波波头`bob`、马尾`ponytail`、双马尾`twin_tails`、丸子头`bun`、麻花辫`braid`、短发`short`、精灵短发`pixie`、寸头`buzz` |
| 发色（`hair_color`） | 黑色`black`、棕色`brown`、金色`blonde`、红色`red`、银白`silver`、粉色`pink`、蓝色`blue`、紫色`purple`、绿色`green`、渐变色`gradient` |
| 瞳色（`eye_color`） | 棕色`brown`、黑色`black`、蓝色`blue`、绿色`green`、琥珀色`amber`、灰色`gray`、红色`red`、紫色`purple`、异色瞳`heterochromia` |
| 服装（`outfit`） | 休闲装`casual`、校服`school_uniform`、职业装`business`、连衣裙`dress`、汉服`hanfu`、和服`kimono`、骑士铠甲`knight_armor`、法师长袍`mage_robe`、科幻战斗服`sci_fi`、运动服`sportswear`、冬季大衣`winter_coat`、泳装`swimsuit`⚠ |
| 姿势（`pose`） | 站立`standing`、坐着`sitting`、行走`walking`、奔跑`running`、挥手`waving`、抱臂`arms_crossed`、战斗姿态`fighting`、看书`reading`、回眸`looking_back`、躺卧`lying`⚠ |
| 背景（`background`） | 纯色背景`plain`、影棚`studio`、城市街道`city`、夜晚都市`night_city`、森林`forest`、海边`beach`、教室`classroom`、城堡`castle`、樱花`cherry_blossom`、太空`space`、咖啡馆`cafe` |

匹配规则：先按键、中文标签（或括号前的部分）找预设；发色、瞳色还认“红发”“红色头发”“蓝眼睛”这类说法。都对不上就当自定义文字原样放进提示词。

提示词顺序：画质词 → 性别 → 年龄 → 体型 → 发型 → 发色 → 瞳色 → `wearing` 服装 → 姿势 → 背景 → 安全尾注。

## 改预设和默认值（工具配置，#28）

「设置 → 工具 → character_image」：

| 配置 | 含义 |
| --- | --- |
| `default_gender` … `default_background` | 九项的默认值，填键、中文标签或自由文字 |
| `presets_json` | 覆盖或新增预设。格式 `{"字段": [{"key": "...", "label": "...", "prompt": "..."}]}`；同键替换、新键追加到列表末尾 |
| `quality_tags` | 提示词开头的画质词 |

例：

```json
{"hair_color": [{"key": "silver", "label": "银发", "prompt": "glossy platinum silver hair"},
                {"key": "mint", "label": "薄荷绿", "prompt": "mint green hair"}]}
```

保存时会校验：JSON 合法、每项有 key/label/prompt、预设里没有露骨内容、默认值能拼出安全的提示词，不合格不让保存。也可以直接改 `toolpacks/builtin/character_image/presets.json`。

## 安全规则

- **没有露骨预设。** 自定义文字或配置里的预设含露骨/色情词（如 nude、naked、nsfw、lingerie、bikini、sexy、裸、色情、性感、内衣、比基尼、r18）时拒绝出图（`unsafe_content`）。
- **年龄默认是成年人**（青年，20 多岁）。所有非年龄预设都按成年人写。
- **未成年角色**（选了 青少年/儿童 预设，或自定义年龄里有“少年、小孩、萝莉、正太、child、teen…”，或写了小于 18 的“N 岁 / years old”）：
  - 和带 ⚠ 的服装（泳装）、姿势（躺卧），或自定义文字里有暗示性词（泳装、泳衣、躺、卧、床、短裙、露脐、紧身、swimsuit、lying、bed…）组合时**直接拒绝**（`minor_unsafe`），表单和预览会显示原因；
  - 其它组合会改用 girl/boy/child，并在提示词末尾加 `age-appropriate, modest clothing, wholesome, non-sexualized`。
- 成年角色的提示词末尾加 `tasteful, fully clothed, safe for work`。
- 出图工作流目前没有负面提示词通道（只能经风格白名单），所以用“拒绝 + 正向安全描述”代替负面提示词；本机模型本身的安全能力不变。

## 隐私

- 表单「写进聊天记录」（1.1）：表单内容就是你自己写在页面上的文字，会作为这一轮的用户消息保存（摘要），提示词登记成本轮句柄。发第三方时按 #49 的规则走。
- 表单「本地提示词」（1.3）：拼好的提示词存成本地内容，聊天记录里只写“定制角色出图（本地提示词，N 张）”。选第三方后端时按 `generate_image` 的 `remote_local_prompt`：默认 `refuse` 拒绝，`confirm` 先问你。
- 对话里 `character_image` 的字段来自模型（1.2）。字段里不能夹带本会话的本地正文（会被拒，`local_content`），和 `stage_prompt` 一样。
- 预览接口只在本机拼字符串，不调用模型或出图服务。

## 接口

| 接口 | 作用 |
| --- | --- |
| `GET /api/character` | 九个字段的预设和生效默认值 |
| `POST /api/character/preview` | `{fields}` → `{prompt, fields, used_defaults, minor, summary}`，不安全时 400 + `code` |
| `POST /api/sessions/{id}/character` | `{fields, count, aspect_ratio, seed?, backend, privacy}`，返回和发消息相同的事件流 |

宿主工具 `character_image` 的参数：九个字段（字符串，可省略）+ `mode`（`model` 默认 / `chat`）。返回 `content_handle`、`summary`、`used_defaults`、`prompt`。

## 不做

不做角色一致性（LoRA、参考图）、不加负面提示词通道、不改 `generate_image` 的约定和默认隐私闸门。

## 代码

- 工具包：`toolpacks/builtin/character_image/`（`tool.json`、`presets.json`）
- 拼提示词和安全规则：`apps/web/character.py`
- 对话工具和表单出图：`apps/web/chat_run.py`（`_execute_character`、`start_character`）
- 页面：`apps/web/static/character.js`
- 检查：`apps/web/check.py` 的 `_character_units`、`_character_cases`；评测 `image.character`
