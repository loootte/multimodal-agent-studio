# 角色图片定制（issue #53）

一个标准化的角色出图工具：用户决定角色的 **性别、年龄、人种、体型、发型、发色、瞳色、服装、姿势、背景、画面风格、参考已有角色** 十二项。每项都有预设可选，也可以自己写；没填的项用默认值——一个**年轻东亚女性角色**（二次元动漫风格，无参考）。

## 怎么用

**网页表单**：侧栏点「定制角色」。

- 每一项是一个下拉框（预设），选最后的「自定义…」就能自己写（最长 200 字）。「参考已有角色」默认是「无」。
- 「恢复默认」把十二项都还原成默认值；下面实时显示这次会发给出图服务的英文提示词。
- 选张数（1–20）、画幅（1:1 / 16:9 / 9:16）、出图后端（本机 ComfyUI / 第三方）、提示词去向（写进聊天记录 / 本地提示词）。
- 点「生成」：在当前对话里多出一轮，N 张就逐张调用 N 次 `generate_image`，进度、成片、停止都和普通出图一样。表单**不经过对话模型**，没配模型密钥也能用。

**对话**：直接说，例如“画一个红发女骑士角色”“画个银发蓝眼的法师人物，坐着看书”。模型调用 `character_image`，只填你说到的字段，其余用默认值，再用返回的句柄调用 `generate_image`；要几张会先 `plan_images`。

> 注意隐私闸门（ADR 0008）：只说“画一个红发女骑士”时，按默认规则这是**本地提示词**（1.3），原话不交给对话模型，直接在本机出图，不会走角色工具。说“角色”“人物立绘”或 “character” 时才把这句话交给模型，由它拆成字段。

## 默认值

| 字段 | 默认 |
| --- | --- |
| 性别 | 女性 |
| 年龄 | 青年（20 多岁） |
| 人种 | 东亚 |
| 体型 | 纤细 |
| 发型 | 黑长直 |
| 发色 | 黑色 |
| 瞳色 | 棕色 |
| 服装 | 休闲装 |
| 姿势 | 站立 |
| 背景 | 纯色背景 |
| 画面风格 | 二次元动漫 |
| 参考已有角色 | 无 |

默认提示词：

```
anime style illustration, character illustration, full body, highly detailed, best quality, woman, young adult in their early twenties, East Asian features, slim build, long straight hair, black hair, brown eyes, wearing casual outfit, sweater and jeans, standing, relaxed pose, simple plain light background, tasteful, fully clothed, safe for work
```

（`reference=none` / 空时不进入提示词。）

## 预设

中文标签后面是键（表单、对话、配置里都能用键或中文标签）。⚠未成年 = 未成年预设；⚠ = 对未成年角色不允许的服装/姿势。

| 字段 | 预设 |
| --- | --- |
| 性别（`gender`） | 女性`female`、男性`male`、中性`androgynous` |
| 年龄（`age`） | 青年（20 多岁）`young_adult`、成年（30 多岁）`adult`、中年`middle_aged`、老年`elderly`、青少年（未成年）`teen`⚠未成年、儿童（未成年）`child`⚠未成年 |
| 人种（`ethnicity`） | 东亚`east_asian`、东南亚`southeast_asian`、南亚`south_asian`、欧洲裔`european`、非洲裔`african`、中东`middle_eastern`、拉丁裔`latino`、混血`mixed` |
| 体型（`build`） | 纤细`slim`、匀称`average`、健美`athletic`、强壮`muscular`、娇小`petite`、高挑`tall`、丰满圆润`plump` |
| 发型（`hairstyle`） | 长直发`long_straight`、长卷发`long_wavy`、波波头`bob`、马尾`ponytail`、双马尾`twin_tails`、丸子头`bun`、麻花辫`braid`、短发`short`、精灵短发`pixie`、寸头`buzz` |
| 发色（`hair_color`） | 黑色`black`、棕色`brown`、金色`blonde`、红色`red`、银白`silver`、粉色`pink`、蓝色`blue`、紫色`purple`、绿色`green`、渐变色`gradient` |
| 瞳色（`eye_color`） | 棕色`brown`、黑色`black`、蓝色`blue`、绿色`green`、琥珀色`amber`、灰色`gray`、红色`red`、紫色`purple`、异色瞳`heterochromia` |
| 服装（`outfit`） | 休闲装`casual`、校服`school_uniform`、职业装`business`、连衣裙`dress`、汉服`hanfu`、和服`kimono`、骑士铠甲`knight_armor`、法师长袍`mage_robe`、科幻战斗服`sci_fi`、运动服`sportswear`、冬季大衣`winter_coat`、泳装`swimsuit`⚠ |
| 姿势（`pose`） | 站立`standing`、坐着`sitting`、行走`walking`、奔跑`running`、挥手`waving`、抱臂`arms_crossed`、战斗姿态`fighting`、看书`reading`、回眸`looking_back`、躺卧`lying`⚠ |
| 背景（`background`） | 纯色背景`plain`、影棚`studio`、城市街道`city`、夜晚都市`night_city`、森林`forest`、海边`beach`、教室`classroom`、城堡`castle`、樱花`cherry_blossom`、太空`space`、咖啡馆`cafe` |
| 画面风格（`art_style`） | 二次元动漫`anime`、写实`realistic`、半写实`semi_realistic`、水彩`watercolor`、油画`oil_painting`、水墨`ink_wash`、Q版`chibi`、像素`pixel`、漫画`comic`、3D渲染`3d_render`、素描`sketch` |
| 参考已有角色（`reference`） | 无`none`（默认可空）；也可自定义一段自由描述（按灵感生成原创角色，不复制版权角色） |

匹配规则：先按键、中文标签（或括号前的部分）找预设；发色、瞳色还认“红发”“红色头发”“蓝眼睛”这类说法。都对不上就当自定义文字原样放进提示词。`reference` 自定义会包一层 `inspired by "…", original character, not a copy of any copyrighted character`；选「无」或空则完全不进提示词。

提示词顺序：画面风格 → 画质词 → 性别 → 年龄 → 人种 → 体型 → 发型 → 发色 → 瞳色 → `wearing` 服装 → 姿势 → 背景 → 参考（可缺）→ 安全尾注。

## 改预设和默认值（工具配置，#28）

「设置 → 工具 → character_image」：

| 配置 | 含义 |
| --- | --- |
| `default_gender` … `default_background`、`default_ethnicity`、`default_art_style`、`default_reference` | 十二项的默认值，填键、中文标签或自由文字；`default_reference` 默认 `none` |
| `presets_json` | 覆盖或新增预设。格式 `{"字段": [{"key": "...", "label": "...", "prompt": "..."}]}`；同键替换、新键追加到列表末尾 |
| `quality_tags` | 提示词靠前的画质词（接在画面风格之后） |

例：

```json
{"hair_color": [{"key": "silver", "label": "银发", "prompt": "glossy platinum silver hair"},
                {"key": "mint", "label": "薄荷绿", "prompt": "mint green hair"}]}
```

保存时会校验：JSON 合法、每项有 key/label/prompt、预设里没有露骨内容、默认值能拼出安全的提示词，不合格不让保存。也可以直接改 `toolpacks/builtin/character_image/presets.json`。

## 安全规则

- **没有露骨预设。** 自定义文字或配置里的预设含露骨/色情词（如 nude、naked、nsfw、lingerie、bikini、sexy、裸、色情、性感、内衣、比基尼、r18）时拒绝出图（`unsafe_content`）。参考角色的自定义文字同样检查。
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
| `GET /api/character` | 十二个字段的预设和生效默认值 |
| `POST /api/character/preview` | `{fields}` → `{prompt, fields, used_defaults, minor, summary}`，不安全时 400 + `code` |
| `POST /api/sessions/{id}/character` | `{fields, count, aspect_ratio, seed?, backend, privacy}`，返回和发消息相同的事件流 |

宿主工具 `character_image` 的参数：十二个字段（字符串，可省略；`reference` 默认可空）+ `mode`（`model` 默认 / `chat`）。返回 `content_handle`、`summary`、`used_defaults`、`prompt`。

## 不做

不做角色一致性（LoRA、参考图）、不加负面提示词通道、不改 `generate_image` 的约定和默认隐私闸门。参考字段只接受文字灵感，不接参考图文件。

## 代码

- 工具包：`toolpacks/builtin/character_image/`（`tool.json`、`presets.json`）
- 拼提示词和安全规则：`apps/web/character.py`
- 对话工具和表单出图：`apps/web/chat_run.py`（`_execute_character`、`start_character`）
- 页面：`apps/web/static/character.js`
- 检查：`apps/web/check.py` 的 `_character_units`、`_character_cases`；评测 `image.character`

## 摄像机位、光照与本机扩展包（#86）

### 新字段（SFW 也能用）

| 字段 | 名字 | 例子 |
| --- | --- | --- |
| 景别 | `shot` | 全身 / 七分身 / 膝上 / 半身 / 胸像 / 特写 / 远景 |
| 角度 | `angle` | 平视 / 仰视 / 俯视 / 鸟瞰 / 侧面 / 背面 / 四分之三侧 / 倾斜 / 第一人称 |
| 构图 | `composition` | 居中 / 三分法 / 对称 / 对角线 / 浅景深 / 框中框 / 留白 |
| 光照类型 | `light_type` | 自然光 / 柔光 / 影棚光 / 黄金时刻 / 霓虹灯 / 烛光 / 月光 / 电影光 / 窗光 |
| 光照方向 | `light_direction` | 正面光 / 侧光 / 逆光 / 顶光 / 底光 / 伦勃朗光 / 分割光 |
| 氛围 | `light_mood` | 明亮 / 温暖 / 冷调 / 戏剧性 / 低调暗沉 / 梦幻 / 浪漫 / 神秘 |
| 表情 | `expression` | 微笑 / 大笑 / 严肃 / 平静 / 害羞 / 自信 … |
| 动作/场景 | `scene` | 自由文字，例如“在雨中撑伞” |

这些字段默认都是“自动”（不加任何词），所以默认提示词和 #53 一字不差；`FIELDS` 仍是十二个核心字段，新字段在 `EXTRA_FIELDS`。选了景别时，画质词里的 `full body` 会去掉，避免冲突。

提示词顺序：风格 → 画质 → 性别/年龄/人种/体型→ 发型发色瞳色 → 表情 → 服装 → 姿势 → 动作/场景 → 摄像机位（景别/角度/构图）→ 光照（类型/方向/氛围）→ 背景 → 参考 → 安全尾注。扩展包新加的字段紧跟在它声明的 `after` 字段后面。

表单里每个字段是“可搜索下拉 + 自定义输入”：输入能筛选预设（中文标签），也能直接写自己的描述；清空就用默认。提示词预览实时更新。

### 本机扩展包（不进代码库）

代码库里只有通用的 SFW 预设。个人用的预设（包括分级内容）做成**本机扩展包**放在仓库外，代码库只带一个加载口子和一套安全守卫，不带任何扩展包内容。

**放在哪**（`apps/web/local_packs.py`）：

- 安装目录：页面上“安装工具包”装进来的预设包放这里。Windows `%APPDATA%\MAS\toolpacks`，其他系统 `~/.config/mas/toolpacks`；环境变量 `MAS_PRESET_PACKS_DIR` 可以换（检查和评测用临时目录，不碰真实目录）。总会被加载。
- 环境变量 `MAS_LOCAL_TOOLPACKS`：另外再加一个或多个目录（Windows 用 `;` 隔开）；设成空字符串 = 只看安装目录。
- 没有缓存，每次请求都重新读：装好、卸载、改 pack.json 都不用重启。
- 每个子目录一个 `pack.json`。目录在仓库里面的一律跳过（防止误提交）；坏包跳过并在 `GET /api/character` 的 `packs_skipped` 里说明原因。

**pack.json**（`schema: mas.local_pack/1`，只读 JSON，不执行任何代码；不认识的键直接拒绝）：

| 键 | 说明 |
| --- | --- |
| `name` / `label` / `version` / `description` | 包名（`[a-z][a-z0-9_]`）、中文名、版本、说明 |
| `extends` | 目前只能是 `character_image` |
| `rating` | `general`（默认）或 `adult` |
| `confirm` | 表单选择这个包时弹的确认文字；`rating=adult` 必填 |
| `min_age` | 只给 `adult`：年龄下限，默认 18，只能更高 |
| `prompt_tail` | 只给 `adult`：提示词尾注（默认 `all characters are adults clearly over {min_age} years old, …`） |
| `chat_keywords` | 对话里用户这一句要出现其中一个词才能开；不写 = 只能在表单里用 |
| `verbatim` | `["outfit"]`：服装预设自带完整说法，不再加 `wearing` |
| `fields` | 新字段：`name`、`label`、`after`（跟在哪个核心字段后）、`options`；自动补一个“自动”选项 |
| `presets.replace` / `extend` / `forbidden` | 整组替换某字段的预设 / 追加预设 / 禁用某些内置键 |

`rating=general` 的包预设里不能有露骨词，尾注仍是 SFW。

### 在页面上安装 / 卸载预设包

“工具配置 → 安装工具包”会自动识别预设包：目录（或 .zip）里只有 `pack.json` 且 `schema` 是 `mas.local_pack/1`，就走预设包流程（`poc/tools/packs.py` 的 `source_kind` 分类，`apps/web/local_packs.py` 的 `inspect` / `install` / `uninstall` 处理）。

1. 打开“工具配置”，展开“安装工具包”，在“本机目录或 .zip 路径”里填预设包目录，例如 `C:\Users\Administrator\mas-local-toolpacks\character_image_adult`，点“检查并安装”。
2. 出现“预设包审阅”：类型（预设包，纯数据）、扩展的工具（`character_image`）、分级、年龄下限、替换/追加的预设和新字段、对话里能不能用、装到哪。权限一栏是“无”：不执行代码、不联网、不读本地内容、不写文件。
3. 勾选“我知道它只是预设数据，不要任何权限”。`rating=adult` 的包还会显示红色警告和包自带的确认文字，并多一项“我已成年，确认安装这个成人分级预设包”，也要勾上。全部勾上，“确认并安装预设包”才能点。
4. 装好后在“预设包（角色定制）”列表里能看到它：类型、分级、扩展哪个工具、来源（已安装 / 来自 MAS_LOCAL_TOOLPACKS）。不用重启，打开“角色定制”，顶部“预设来源”里就有它。
5. 卸载：在“预设包（角色定制）”列表里点它后面的“卸载”。只能卸载页面上装的；`MAS_LOCAL_TOOLPACKS` 目录里的包要自己移走。

安装时的检查（只收紧，不放宽；不合格就不装、不写任何文件）：

- `pack.json` 的全部校验（不认识的键、`rating=adult` 要 `confirm`、`min_age` ≥ 18 …）；
- 把包自带的每个预设过一遍分级守卫（`character.audit_pack`）：未成年用词、低于 `min_age` 的年龄、幼态体型、校服/学生、真人或版权角色、标了 `minor` 的预设、`age` 里的 teen/child，都不让装；
- `MAS_LOCAL_TOOLPACKS` 里已经有同名包、安装目录在仓库里面，都不让装；
- 只拷 `pack.json` 和 README，别的文件忽略（审阅里会提示）。

接口：

- `POST /api/toolpacks/inspect {source}`：预设包返回 `type: "preset"`、`permissions: []`、`permission_notes`、`rating`、`min_age`、`extends`、`confirm`、`warnings`（成人分级第一条是警告）、`problems`、`target`、`upgrade`（覆盖安装时是旧版本号）。工具包返回 `type: "tool"`，其余不变。
- `POST /api/toolpacks/install {source, accept_permissions: [], accept_rating?}`：预设包的 `accept_permissions` 必须是空列表；`rating=adult` 还要 `accept_rating: "adult"`，否则 400 `rating_not_accepted`。
- `POST /api/toolpacks/presets/<name>/uninstall`：只卸安装目录里的；`MAS_LOCAL_TOOLPACKS` 里的返回 400 `not_installed_here`。
- `GET /api/tools` 多了 `preset_packs`（`type`、`extends`、`rating`、`min_age`、`local_only`、`origin`）。
- 安装、卸载和 `/api/toolpacks/*` 的其他写操作一样，只接受本机同源请求，并记一条 `security.config_changed`。

**怎么用**：

- **表单**：本机有扩展包时，表单顶部出现“预设来源”下拉（没有就不显示）。选包会弹 `confirm` 确认；`GET /api/character?pack=名字` 换成包的预设。`rating=adult` 的包把出图后端锁在本机 ComfyUI。提交带 `pack`、`pack_confirmed: true`；没确认返回 400 `pack_confirm_required`，包不存在返回 `pack_not_found`。
- **对话**：有 `chat_keywords` 的包会在系统提示里列出名字。模型调 `character_image` 时填 `pack`（新字段放 `pack_fields`）；只有用户**这一句**说出包的关键词才放行，否则返回 `pack_confirm_required`。`pack_fields` 只能写包自己的新字段。

### 分级守卫（`rating=adult` 的包，代码库里硬拦，扩展包改不了）

| 规则 | 代码 |
| --- | --- |
| 年龄低于 18（未成年）或低于包的 `min_age`，阿拉伯数字、中文数字、`age 19`、年龄栏纯数字都算 | `minor_unsafe` |
| 内置的 teen / child 等未成年预设（按键或中文标签；包自己把它们加回来也一样拦） | `minor_unsafe` |
| 自定义文字里有 少女、萝莉、正太、小孩、幼女、儿童、学生、loli、shota、child、teen、underage、schoolgirl、petite young 等 | `minor_unsafe` |
| 校服 / 教室 / 学生类内容 | `minor_unsafe`（说明原因） |
| 幼态体型：娇小、幼态、平胸幼体等 | `minor_unsafe`（说明原因） |
| 真人、明星、熟人、版权角色（词表 + 《作品名》、@账号、英文人名规则） | `not_original_character`，“请只描述原创角色” |
| 分级内容发第三方出图 | `nsfw_backend_not_allowed`，说明可能违反服务条款；对话路径记一条 `egress.blocked` |

拒绝原因里用包的 `label`（例如“XX不能画未成年角色”）。

没有扩展包时，#53 的规则照旧：自定义文字露骨一律 `unsafe_content`，未成年角色不能配暴露服装或挑逗姿势。另外有一条和扩展包无关的通用兜底：任何提示词本身露骨（比如自己写的提示词），都算成人内容，不许发第三方、结果打 `rating: adult`。

### 结果标记

分级图（`rating=adult` 的包或通用兜底）出图后：工件记录写 `rating: "adult"`，文件改名加 `adult_` 前缀；SSE `artifact` 事件和工具结果带 `rating`。本地资源面板有“分级”筛选（`GET /api/resources?rating=adult|general`），条目带 `rating` 和 `file_name`。

### 接口变化

- `GET /api/character[?pack=名字]`：返回多了 `extra_fields`（每项带 `group`：camera / light / detail / pack）、`pack`（当前包）、`packs`（本机包简介，不含预设内容）、`packs_skipped`。
- `POST /api/character/preview`：`{fields, pack?}`。
- `POST /api/sessions/{id}/character`：多了 `pack`、`pack_confirmed`。
- `character_image` 工具（1.2.0）：多了八个字符串字段、`pack`（字符串）和 `pack_fields`（对象）。

### 检查

`apps/web/check.py` 的 `_pack_character_cases`（单元：discover / minor / school / original / default_sfw / order / camera_light；接口：form / chat）。用的是临时目录里的中性夹具包；检查开始时把 `MAS_LOCAL_TOOLPACKS` 设成空，用户自己的扩展包不会混进来。评测 `image.character_pack_*`、`image.character_camera_light_prompt`。
预设包安装：`_preset_install_cases`（install_ok / rejects_loosening / adult_requires_confirm / uninstall），评测 `image.install_preset_pack_ok`、`image.install_preset_pack_rejects_loosening`、`image.install_preset_pack_adult_requires_confirm`、`image.uninstall_preset_pack`。安装目录在 check 一开始就设成临时目录（`MAS_PRESET_PACKS_DIR`）。
