# 工具代码契约（表单工具，`entry.kind: "module"`，issue #86）

一个工具如果一定要用 Python 实现，代码就是**工具目录里的一个 .py 文件**，由 `tool.json` 的 `entry.module` 指明。宿主（网页服务）只按本页的函数签名调用它，不再在 `apps/web/` 里为某个工具写专门的代码。内置的 `character_image` 就是这样实现的（`toolpacks/builtin/character_image/tool.py`）。

普通的模型工具（只有一个 `call(args, config)`）仍用 `entry.kind: "python"`，见 [tool-pack-guide.md](tool-pack-guide.md)。

## 目录

```
my_tool/
  tool.json      清单（schema mas.tool/1）
  tool.py        entry.module 指向的代码；必须在这个目录里（解开符号链接以后也一样）
  presets.json   可选：代码自己读的数据
  README.md      可选
```

- `entry.module` 必须是相对路径、以 `.py` 结尾、不含 `..`；装的时候和每次发现时都用 `realpath` 核对文件确实在工具目录里，否则 `invalid_manifest`（“entry.module 指向工具目录外面。”）。
- 内置工具（`toolpacks/builtin/`）的代码是受审查的仓库代码，在宿主进程里 import（`apps/web/module_tools.load_builtin`，同样做 realpath 检查，改了文件自动重新加载）。
- 安装的工具（`toolpacks/installed/`）的代码**只在沙箱子进程里跑**（`poc/tools/sandbox.py`，见 [tool-sandbox.md](tool-sandbox.md)），宿主从不 import 它。

## 清单里多出来的字段

| 字段 | 说明 |
| --- | --- |
| `entry` | `{"kind": "module", "module": "tool.py"}` |
| `ui` | 只给 module 工具。`{"form": true, "preview": true, "title": "定制角色", "run_label": "开始出图"}`；`form=true` 才会出现在网页“定制”对话框的工具列表里 |
| `content` | 只给 module 工具。`{"rating": "general"}`（默认）或分级：`{"rating": "adult", "min_age": 18 以上整数, "label": "中文名", "confirm": "开启前弹的确认文字", "chat_keywords": ["…"]}` |

`parameters` 仍是给对话模型的 JSON Schema（字段全是字符串）；`confirm` 一般写 `never`（工具只拼文字，出图还要再经过 `generate_image`）。

## 函数

`ctx` 是纯 JSON：`{"tool": 工具名, "config": 生效配置, "rating": "general"|"adult", "min_age"?: 数字, "label"?: 文字}`。

| 函数 | 什么时候调 | 返回 |
| --- | --- | --- |
| `catalog(ctx)` | 打开表单（`GET /api/tools/<name>/catalog`） | `{"fields": [...], "extra_fields": [...], "defaults": {...}}`。每个字段 `{"name", "label", "options": [{"key", "label", "prompt", "group"?, "minor"?}], "group"?}` |
| `preview(fields, ctx)` | 表单实时预览（`POST /api/tools/<name>/preview`） | 同 `run`；没有 `preview` 时宿主改调 `run` |
| `run(args, ctx)` | 表单出图（`POST /api/sessions/<id>/tools/<name>/run`）或对话里模型调用这个工具 | `{"prompt": 英文提示词, "summary": 中文摘要, "fields": {...}, "used_defaults": [...], "rating"?}` |
| `check_config(config, ctx)` | 可选；设置页保存配置前（只调内置工具的） | `{配置字段: 问题}`，空对象表示没问题 |

- `fields` / `args` 是 `{字段名: 字符串}`：字段名要匹配 `^[a-z][a-z0-9_]{0,47}$`，值是字符串、最长 2000 字，宿主先查过再给你。
- **拒绝**时返回 `{"error": {"code": "小写下划线", "message": "给用户看的中文", "field": "字段名"}}`，不要抛异常；异常一律变成 `tool_error`“工具代码出错了。”，细节不给用户看。
- 只用标准库和工具目录里的文件；安装的工具在沙箱里，联网、写文件、读本地内容都要在 `permissions` 里声明并在安装时授权。

## 宿主守卫（工具代码改不了）

`apps/web/content_guard.py`，对**任何**声明了 `content.rating=adult` 的工具都生效，不管代码是内置的还是安装的、代码自己有没有检查：

1. 调用前（`check_inputs`）逐个字段查：未成年（中英文词表；“17岁”“16 years old”“十六岁”“age 19”、年龄栏的纯数字，低于 18 或低于 `min_age` 都算）→ `minor_unsafe`；校服 / 教室 / 学生、幼态体型 → `minor_unsafe` 并说明原因；真人、明星、熟人、版权角色（词表、《作品名》、@账号、英文人名）→ `not_original_character`“请只描述原创角色”。普通（general）工具：露骨词 → `unsafe_content`。
2. 调用后（`check_output`）再查最终提示词（同样的规则）；结果的 `rating` 一律以清单为准，工具自称 `general` 也会被改成 `adult`。普通工具拼出露骨提示词 → `unsafe_content`。
3. 表单目录（`filter_catalog`）：分级工具去掉标了 `minor` 或过不了守卫的预设；普通工具去掉露骨预设。
4. 开启：表单提交要带 `confirmed: true`（页面在选中工具时弹 `content.confirm`），否则 400 `confirm_required`；对话里只有用户**这一句**出现 `chat_keywords` 之一，模型调它才放行，否则 `confirm_required`。没写 `chat_keywords` 就只能在表单里用。
5. 只许本机出图：分级内容发第三方出图一律 400 `nsfw_backend_not_allowed`（说明可能违反服务条款），记一条 `egress.blocked`；表单把后端锁在本机 ComfyUI。
6. 结果标记：工件 `rating: "adult"`，文件名加 `adult_` 前缀，SSE `artifact` 事件带 `rating`；本地资源面板可以按分级筛选。

## 安装

“工具 → 安装工具包”填工具目录或 .zip 的路径 → 检查 → 审阅 → 逐项勾选 → 安装。装到仓库的 `toolpacks/installed/<name>/`（已在 `.gitignore` 里，装完 `git status` 不变；环境变量 `MAS_TOOLPACKS_DIR` 可以换目录，检查和评测用临时目录）。

审阅里除了权限，还有两项各要单独勾选（接口里写进 `accept_permissions`，少一项多一项都不装）：

| 项 | 什么时候出现 | 没勾的错误码 |
| --- | --- | --- |
| `code`“包含代码” | 包里有 .py 等代码（任何 python / module 工具） | `code_not_accepted` |
| `rating_adult`“成人分级” | `content.rating=adult`；审阅里写明年龄下限、只能本机出图、确认文字 | `rating_not_accepted` |

`POST /api/toolpacks/inspect` 返回 `code`、`rating`、`min_age`、`label`、`confirm`、`ui`、`target`（装到哪）和 `accept`（要勾的全部项）。授权记录 `.granted.json` 记下代码、分级和包内容摘要；装好后改了任何文件（包括把分级改掉），授权作废，工具不可用（`permission_denied`），要重新安装。

只有 `pack.json` 的旧“预设包”不再支持：检查和安装都返回 400 `preset_pack_unsupported`，请改成本页的工具包。

## 例子：最小的表单工具

`tool.json`：

```json
{
  "schema": "mas.tool/1", "name": "hello_form", "version": "0.1.0",
  "description": "示例表单工具：按选项拼一句英文提示词。",
  "parameters": {"type": "object", "additionalProperties": false,
                 "properties": {"color": {"type": "string", "maxLength": 200}}},
  "mutating": false, "confirm": "never",
  "entry": {"kind": "module", "module": "tool.py"},
  "ui": {"form": true, "preview": true, "title": "示例"},
  "content": {"rating": "general"},
  "permissions": {}
}
```

`tool.py`：

```python
COLORS = [{"key": "red", "label": "红色", "prompt": "a red umbrella"},
          {"key": "blue", "label": "蓝色", "prompt": "a blue umbrella"}]


def catalog(ctx):
    return {"fields": [{"name": "color", "label": "颜色", "options": COLORS}],
            "extra_fields": [], "defaults": {"color": "red"}}


def run(args, ctx):
    value = args.get("color") or "red"
    prompt = next((o["prompt"] for o in COLORS if value in (o["key"], o["label"])), value)
    return {"prompt": prompt + ", simple background", "summary": f"颜色：{value}",
            "fields": {}, "used_defaults": [] if args.get("color") else ["color"]}


def preview(fields, ctx):
    return run(fields, ctx)
```

## 通用接口

| 接口 | 作用 |
| --- | --- |
| `GET /api/tools` | 多了 `form_tools`：`[{name, title, builtin, form, preview, run_label, rating, version, label?, min_age?, confirm?, local_only?, unavailable?}]` |
| `GET /api/tools/<name>/catalog` | 表单目录（已过守卫）+ 上面的简介 |
| `POST /api/tools/<name>/preview` | `{fields}` → `{prompt, summary, fields, used_defaults, rating}`；拒绝时 400 + `code` |
| `POST /api/sessions/<id>/tools/<name>/run` | `{fields, count, aspect_ratio, seed?, backend, privacy, confirmed?}`，返回和发消息相同的事件流 |

旧接口 `/api/character`、`/api/character/preview`、`/api/sessions/<id>/character` 保留，等于 `character_image` 的这三个。

## 代码

- 契约调用和值检查：`apps/web/module_tools.py`
- 分级守卫：`apps/web/content_guard.py`
- 清单校验、安装确认、授权：`poc/tools/packs.py`（`entry_problem`、`has_code`、`inspect`、`install`）
- 沙箱里按函数名调用：`poc/tools/sandbox.py`、`sandbox_runner.py`（`function` = `call` / `catalog` / `preview` / `run`）
- 表单和对话：`apps/web/chat_run.py`（`start_form_tool`、`_execute_module_tool`）、`apps/web/static/character.js`
- 检查：`apps/web/check.py` 的 `_pack_character_cases`、`_tool_install_cases`；评测 `image.form_tool_*`、`image.install_*form_tool*`、`image.install_legacy_preset_pack_rejected`
