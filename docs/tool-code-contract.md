# 工具代码契约（表单工具，`entry.kind: "module"`，issue #86）

一个工具如果一定要用 Python 实现，代码就是**工具目录里的一个 .py 文件**，由 `tool.json` 的 `entry.module` 指明。宿主（网页服务）只按本页的函数签名调用它，不再在 `apps/web/` 里为某个工具写专门的代码。内置的 `character_image` 就是这样实现的（`toolpacks/builtin/character_image/tool.py`）。

普通的模型工具（只有一个 `call(args, config)`）仍用 `entry.kind: "python"`，见 [tool-pack-guide.md](tool-pack-guide.md)。

## 目录

```
my_tool/
  tool.json      清单（schema mas.tool/1）
  tool.py        entry.module 指向的代码；必须在这个目录里（解开符号链接以后也一样）
  presets.json   可选：代码自己读的数据
  ui/            可选：工具自带的网页面板（ui.panel），只放 .js / .css
    panel.js
    panel.css
  README.md      可选
```

- `entry.module` 必须是相对路径、以 `.py` 结尾、不含 `..`；装的时候和每次发现时都用 `realpath` 核对文件确实在工具目录里，否则 `invalid_manifest`（“entry.module 指向工具目录外面。”）。
- 内置工具（`toolpacks/builtin/`）的代码是受审查的仓库代码，在宿主进程里 import（`apps/web/module_tools.load_builtin`，同样做 realpath 检查，改了文件自动重新加载）。
- 安装的工具（`toolpacks/installed/`）的代码**只在沙箱子进程里跑**（`services/tools/sandbox.py`，见 [tool-sandbox.md](tool-sandbox.md)），宿主从不 import 它。

## 清单里多出来的字段

| 字段 | 说明 |
| --- | --- |
| `entry` | `{"kind": "module", "module": "tool.py"}` |
| `ui` | 只给 module 工具。`{"form": true, "preview": true, "title": "定制角色", "run_label": "开始出图", "panel": {...}}`；`form=true` 或带 `panel` 的工具会出现在 `GET /api/tools` 的 `form_tools` 里。`panel` 见下面「网页面板」 |
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
| `ui`“网页面板” | 清单声明了 `ui.panel`（就是一项普通权限，和 `network` 等一样写在 `permissions`） | `permission_not_accepted` |
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

## 网页面板（`ui.panel`，issue #89）

工具的前端也放在工具自己的目录里。网页（`apps/web/static/`）只有一个通用的面板宿主 `panels.js`，不含任何具体工具的代码；内置的角色定制面板在 `toolpacks/builtin/character_image/ui/panel.js`、`panel.css`。

清单：

```json
"ui": {"form": true, "preview": true, "title": "定制角色", "run_label": "开始出图",
       "panel": {"script": "ui/panel.js", "style": "ui/panel.css", "title": "定制角色", "mount": "dialog"}},
"permissions": {"ui": true}
```

- `script` 必填、`style` 可选，都要匹配 `^ui/[A-Za-z0-9][A-Za-z0-9_-]{0,47}\.(js|css)$`（扩展名要对得上），解开符号链接以后必须在工具目录的 `ui/` 里，文件存在，单个不超过 256 KB；否则 `invalid_manifest`。`title` 是侧栏入口的文字，`mount` 目前只有 `dialog`。
- 声明了 `panel` 就必须声明 `permissions.ui: true`，反过来也一样。安装时审阅里有一项“网页面板”（`ui`），列出面板文件、大小和 sha256，要单独勾选，没勾 `permission_not_accepted`。授权后改了面板文件，授权作废，面板和工具一起不可用。

### 路由 `GET /api/tools/<name>/ui/<file>`

- 只给清单里声明的文件（`script`、`style`）；其他任何路径（没声明的文件、`..`、`%2e%2e`、编码的斜杠、空段、符号链接指出目录）一律 404，不读盘上别的文件。只接受 GET（其他方法 405）。响应带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。
- 内置工具：`.js` 是 `text/javascript`，`.css` 是 `text/css`，页面同源加载。
- 已安装工具：文件只按 `text/plain` 给（CSP `sandbox; default-src 'none'`），不能被当作脚本加载；面板只能通过 `GET /api/tools/<name>/ui/frame` 运行。这个文档把宿主的桥 `panel_frame.js`、面板脚本和样式内联进去，响应头 CSP：
  `sandbox allow-scripts; default-src 'none'; script-src 'sha256-…'; style-src 'sha256-…'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'`。
  只有哈希对得上的这几段能执行；不能联网（`connect-src` 落到 `none`）、不能加载别的文件、不能提交表单。内置工具没有 `frame`（404）。

### 面板接口

面板脚本只做一件事：`MASPanels.define(mount)`。宿主打开面板时调用 `mount(el, api)`，`el` 是给面板的空容器；`mount` 可以返回一个清理函数，关面板时调用。

| `api` | 作用 |
| --- | --- |
| `api.tool` | 只读的工具简介：`name`、`title`、`run_label`、`rating`、`label`、`min_age`、`local_only` |
| `api.catalog()` | → 表单目录（工具 `catalog()` 的结果，已过宿主守卫） |
| `api.preview(fields)` | → `{prompt, summary, fields, used_defaults, rating}`；拒绝时抛错，`error.code` / `error.field` |
| `api.run({fields, count, aspect_ratio, backend, privacy, seed?})` | 在当前会话里出图（`count` 1–20）；宿主自己加分级确认标记、强制只能本机、拼对话里的标签，然后关面板 |
| `api.confirm(text)` | → true / false，宿主弹的确认框 |
| `api.notify(text)` | 在对话框底部显示一行宿主提示 |
| `api.close()` | 关面板 |
| `api.handle.is(value)` | 是不是本地内容句柄 |

面板**拿不到本地内容本身**：接口里没有读资源、读文件、取会话正文的方法，最多看到句柄。面板也不应该自己 `fetch`；内置面板的检查会确认 `panel.js` 里没有 `fetch` 和 `/api/`。

分级工具（`content.rating=adult`）：打开面板前宿主先弹 `content.confirm`，用户确认后才挂载；`api.run` 的 `confirmed` 只来自这次宿主确认，面板传不进来；后端锁在本机。服务端的 `content_guard`、`confirm_required`、`nsfw_backend_not_allowed` 照旧生效，面板绕不过去。

### 为什么内置同源、安装的放沙箱 iframe

- 内置面板是受审查的仓库代码，同源加载最简单，和宿主页面共用样式。
- 安装的面板是第三方代码。同源跑就能读页面上的对话、本地资源、会话标识，调用任何接口。所以它只在 `<iframe sandbox="allow-scripts">` 里跑：不同源（opaque origin）、没有 Cookie 和存储、不能导航顶层页面、不能弹窗、CSP 禁止联网。它能做的只有经 `postMessage` 调上面这张表里的方法，宿主核对消息来自这个 iframe（`event.source`），只接受白名单里的方法，参数再校验一遍。iframe 的高度由面板报给宿主（`panel-size`）。
- 面板的文件和工具代码一样计入安装摘要；改了就要重新安装。

## 通用接口

| 接口 | 作用 |
| --- | --- |
| `GET /api/tools` | 多了 `form_tools`：`[{name, title, builtin, form, preview, run_label, rating, version, label?, min_age?, confirm?, local_only?, unavailable?, panel?}]`；`panel` 是 `{title, mount, isolated, script?, style?, frame?}`（内置给 `script`/`style`，安装的给 `frame`） |
| `GET /api/tools/<name>/ui/<file>`、`/ui/frame` | 面板文件 / 沙箱文档，见上面「网页面板」 |
| `GET /api/tools/<name>/catalog` | 表单目录（已过守卫）+ 上面的简介 |
| `POST /api/tools/<name>/preview` | `{fields}` → `{prompt, summary, fields, used_defaults, rating}`；拒绝时 400 + `code` |
| `POST /api/sessions/<id>/tools/<name>/run` | `{fields, count, aspect_ratio, seed?, backend, privacy, confirmed?}`，返回和发消息相同的事件流 |

旧接口 `/api/character`、`/api/character/preview`、`/api/sessions/<id>/character` 保留，等于 `character_image` 的这三个。

## 代码

- 契约调用和值检查：`apps/web/module_tools.py`
- 分级守卫：`apps/web/content_guard.py`
- 清单校验、安装确认、授权：`services/tools/packs.py`（`entry_problem`、`has_code`、`inspect`、`install`）
- 沙箱里按函数名调用：`services/tools/sandbox.py`、`sandbox_runner.py`（`function` = `call` / `catalog` / `preview` / `run`）
- 表单和对话：`apps/web/chat_run.py`（`start_form_tool`、`_execute_module_tool`）
- 网页面板：`services/tools/packs.py`（`panel_spec`、`panel_files`、`ui` 权限）、`apps/web/tool_panels.py`（`/api/tools/<name>/ui/` 路由、iframe 文档和 CSP）、`apps/web/static/panels.js`（通用宿主、面板接口）、`apps/web/static/panel_frame.js`（iframe 里的桥）。`apps/web/static/` 里没有任何具体工具的代码
- 检查：`apps/web/check.py` 的 `_pack_character_cases`、`_tool_install_cases`、`_tool_ui_cases`；评测 `image.form_tool_*`、`image.install_*form_tool*`、`image.install_legacy_preset_pack_rejected`、`*.tool_ui_*`
