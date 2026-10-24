# 工具包使用和编写指南

这份指南写给想给本项目**加一个新工具**的人：什么样的工具能装、包里放什么、`tool.json` 每个字段怎么写、入口函数怎么写、怎么在网页里安装、配置、在对话里用、导出和卸载，以及常见报错怎么改。

字段的简明参考见 [tool-packs.md](tool-packs.md)。本文内容以代码为准：`poc/tools/packs.py`（清单校验、安装、导出）、`poc/tools/registry.py`（注册和调用）、`poc/tools/schema.py`（参数校验）、`apps/web/tool_config.py`（配置）。

## 1. 工具包是什么

一个工具包就是一个目录，里面有一份清单 `tool.json` 和一个 Python 文件。装上以后：

- 工具出现在网页右上角“工具”对话框的下拉框里（“工具”分组），可以看版本、权限、依赖，填配置。
- 第三方模型在对话里能看到它的名字、说明和参数（JSON Schema），需要时会调用它。
- 调用时本机先检查依赖和权限、按参数模式校验，再执行你的 `call(arguments, config)`，把返回的对象交回模型。

装上就生效，**不用重启**网页服务：每次列工具、调用工具时都会重新读清单。

### 能装什么

| 可以 | 说明 |
| --- | --- |
| 纯 Python 的工具，入口是包里的 `.py` 文件 | `entry.kind` 写 `python`。只读查询、计算、调本机或网络上的服务、写本机文件都行 |
| 只用 Python 标准库，或用已经装在运行环境里的第三方库 | 第三方库写进 `requires.python`，缺了会报 `dependency_missing`。系统**不会**替你 `pip install` |
| 需要自己的配置项和密钥 | 写在 `config` 里，用户在对话框里填，存在本机 `tools.json`，导出时不带走 |
| 依赖共享后端（目前只有 `comfyui`） | 写进 `requires.backends`。注意：只做“后端已声明”的检查，**后端的地址、令牌不会传给你的工具**（见 §4.4） |

### 不能装什么

| 不可以 | 原因 |
| --- | --- |
| `entry.kind: "host"` 的工具 | 宿主工具的实现写在网页进程 `apps/web/chat_run.py` 里（例如 `accept_local_content`、`retrieve_local`、`plan_images`、`build_skill_page`）。只有内置工具能用，第三方包会被拒：“只有内置工具可以用 entry.kind=host。” |
| 要读本会话本地内容（用户留下的正文、文件）的工具 | 没有给工具包开放读本地内容的接口。`permissions.read_local_content` 可以声明，但目前拿不到任何本地内容 |
| 要把本地内容交给模型的工具 | `send_local_to_model` 必须是 `false`，否则清单不合格 |
| 给模型的参数里带 `prompt`、`text`、`body`、`content`、`bytes`、`path`、`filename` | 这些名字被拒，本地正文不经过第三方模型 |
| MCP 服务器 | `mcp` 字段是保留字段，填了也不会启动或连接任何 MCP 服务 |
| 不给模型用、只在本机用的第三方工具（`parameters: null`） | 第三方工具必须声明 `parameters` |
| 多文件、包内相对导入的代码 | 入口文件按单个文件装载，不在 `sys.path` 上，`from . import x` 和 `import 同目录文件` 都不行。把代码写在一个文件里 |
| 和内置工具或后端同名 | 安装被拒 |

### 安全边界（请先读）

- **没有沙箱，也没有进程隔离。** 工具包的代码在网页服务进程里直接运行，和服务有同样的文件和网络权限。
- 权限只是**声明 + 安装时确认 + 调用前核对授权记录**，不会真的拦住代码联网或写文件。只装你信得过、看过代码的包。
- 工具返回的对象会原样（JSON）交给第三方模型。不要在返回值里放密钥、本机路径或隐私内容。

## 2. 目录结构

```
dice_roll/              ← 目录名必须和 name 一样
├── tool.json           ← 清单，必需
└── tool.py             ← 入口文件，路径写在 entry.module
```

- 以 `.` 开头的文件、`__pycache__`、`*.pyc`、`tools.json` 安装和导出时都会被跳过。
- 可以打成 zip 分享：zip 根目录直接是 `tool.json`，或者里面只有一个含 `tool.json` 的子目录。zip 里不能有越界路径（`../`）。
- 装好的包在 `toolpacks/installed/<name>/`（可以用环境变量 `MAS_TOOLPACKS_DIR` 换位置），里面多一个 `.granted.json` 记录你同意过的权限。
- 仓库里的位置：`toolpacks/builtin/`（内置）、`toolpacks/backends/`（后端）、`toolpacks/examples/`（示例，不会自动加载，要安装）。

## 3. `tool.json` 字段

`schema` 固定为 `mas.tool/1`。不认识的字段会被拒。

| 字段 | 必填 | 类型 | 含义 | 例子 |
| --- | --- | --- | --- | --- |
| `schema` | 是 | 字符串 | 固定 `mas.tool/1` | `"mas.tool/1"` |
| `name` | 是 | 字符串 | 工具名，小写字母开头，2–48 位 `a-z0-9_`。模型看到的就是它；目录名要一致；全局唯一 | `"dice_roll"` |
| `version` | 是 | 字符串 | `主.次.修` 三段数字 | `"0.1.0"` |
| `description` | 是 | 字符串 | 给模型和用户看的说明。写清楚什么时候该用、会做什么 | `"掷骰子……"` |
| `order` | 否 | 整数 | 列表排序，越小越靠前，默认 1000 | `900` |
| `parameters` | 是* | 对象 | 给模型的参数，JSON Schema。必须 `type: "object"` 且 `additionalProperties: false`。*内置工具可以写 `null` | 见下文 |
| `mutating` | 是 | 布尔 | 是否会改外部状态（写文件、提交任务、发请求改数据） | `false` |
| `confirm` | 是 | 字符串 | 调用前是否要用户确认：`always`、`never`、`by_args`。`mutating: true` 时不能写 `never` | `"never"` |
| `confirm_args` | `by_args` 时必填 | 字符串列表 | 参数里出现这些名字时才确认 | `["overwrite"]` |
| `entry` | 是 | 对象 | `{"kind": "python", "module": "tool.py"}`。`module` 是包内相对路径、以 `.py` 结尾。`kind: "host"` 只给内置工具 | 见上 |
| `config` | 否 | 列表 | 配置字段，见下表。默认空 | 见下文 |
| `requires` | 否 | 对象 | `{"backends": [...], "python": [...]}`，都是字符串列表 | `{"backends": ["comfyui"], "python": []}` |
| `permissions` | 是 | 对象 | 只能有这四个布尔键：`read_local_content`、`network`、`write_files`、`send_local_to_model`。不写的键视为 `false`；`send_local_to_model` 必须 `false`。可以写 `{}` | 见下文 |
| `parameter_sources` | 否 | 对象 | 参数枚举从哪里来。目前只支持 `"comfyui.styles"`（ComfyUI 风格列表） | `{"style": "comfyui.styles"}` |
| `mcp` | 否 | 任意 | 保留，不生效 | — |

### `parameters` 里能用的 JSON Schema 关键字

本机校验（`poc/tools/schema.py`）实际检查的只有：`properties`、`required`、`additionalProperties: false`、每个属性的 `type`（`string`、`integer`、`number`、`boolean`、`object`、`array`）、`enum`、`minLength`、`minimum`、`maximum`、`items.type`。其他关键字（如 `pattern`、`maxLength`）会原样给模型看，但本机不检查，入口函数里要自己再校验。每个属性都写 `description`，模型靠它理解参数。

### `config` 每一项

| 键 | 必填 | 含义 |
| --- | --- | --- |
| `name` | 是 | 字段名，规则同工具名，不能重复 |
| `type` | 是 | `string`、`int`、`enum`、`path`、`secret` |
| `default` | 是 | 默认值。密钥字段必须是 `""` 或 `null`（不能把密钥写进包） |
| `required` | 是 | 布尔，必填项 |
| `secret` | 是 | 布尔。`true` 时页面只显示掩码，导出不带走 |
| `description` | 是 | 页面上的说明 |
| `choices` | `enum` 时必填 | 可选值列表 |
| `min`、`max` | 否 | `int` 的范围，保存时检查 |
| `env` | 否 | 环境变量名。设置了这个环境变量时优先用它 |
| `must_exist` | 否 | `path` 类型时要求路径存在 |

## 4. 入口函数

### 4.1 签名

```python
def call(arguments: dict, config: dict) -> dict:
    ...
```

入口文件必须有模块级的 `call` 函数，否则列工具时报“工具包 xxx 的入口没有 call(arguments, config)。”

### 4.2 输入

- `arguments`：模型传来的参数，**已经按 `parameters` 校验过**（多余参数、必填、类型、enum、范围），是一份拷贝，可以随便改。没传的可选参数就不在字典里。
- `config`：这个工具的生效配置。优先级：`env` 指定的环境变量 > 用户在对话框里保存的值（数据目录的 `tools.json`）> 清单里的 `default`。密钥给的是原值。

调用前系统先做：依赖检查（`dependency_missing`）→ 权限授权核对（`permission_denied`）→ 参数校验（`invalid_parameter` / `unexpected_parameter`）。任何一步不过，`call` 都不会被调用。

### 4.3 返回

返回一个 JSON 可序列化的 `dict`。系统会补上 `"tool": 名字`，没写 `ok` 时补 `"ok": true`。约定：

```python
# 成功
return {"ok": True, "executed": True, "result": ...}
# 失败：给一个稳定的错误码和一句人话
return {"ok": False, "executed": False, "error": {"code": "bad_input", "message": "面数太大。"}}
```

- 返回的不是 `dict` → 系统改成 `{"ok": false, "error": {"code": "tool_error", "message": "工具没有返回对象。"}}`。
- `call` 抛异常 → 系统改成 `tool_error`，消息只带异常类名（例如“工具运行出错（RuntimeError）。”），这一轮对话不会中断。能预料的错误请自己返回 `error`，模型才能看懂。
- 整个返回对象会发给第三方模型，别放密钥和隐私。

### 4.4 声明依赖后端（例如 ComfyUI）

```json
"requires": {"backends": ["comfyui"], "python": []}
```

效果：

- 后端名必须在 `toolpacks/backends/*.json` 里声明过（目前只有 `comfyui`），否则调用返回 `dependency_missing`（“缺少依赖：后端 xxx。”）。
- 对话框里会显示“依赖后端 ComfyUI：已配好 / 未配全（缺哪些）”，旁边有“去配置 ComfyUI”按钮。
- **限制**：后端的配置（地址、令牌、工作流）**不会**传进 `config`。现在想在第三方工具里访问 ComfyUI，只能在自己的 `config` 里再声明一个地址字段。出图请继续用内置的 `generate_image`。

`requires.python` 只检查模块能否导入（`importlib.util.find_spec`），不会安装。

## 5. 权限和确认

### 权限

| 键 | 页面显示 | 现在的含义 |
| --- | --- | --- |
| `network` | 联网 | 工具会访问网络 |
| `write_files` | 写文件 | 工具会写本机文件 |
| `read_local_content` | 读本机内容 | 可以声明，但目前没有接口给工具包读本地内容 |
| `send_local_to_model` | 把本机内容交给模型 | 必须 `false`，写 `true` 清单不合格 |

安装时，页面先调用“检查”，弹出确认框：

```
安装 dice_roll 0.1.0？
示例工具包：掷骰子……
要的权限：无
缺少依赖：……（只有缺依赖时才有这一行）
```

点“确定”才会安装，同意的权限写进包目录的 `.granted.json`。每次调用都会核对：清单要的权限都在授权记录里才放行，否则返回 `permission_denied`。所以手工改了已安装包的 `tool.json`、加了新权限，要重新安装一次。授权记录不随导出走，别人装你的包时要自己再确认一次。

### 确认策略

| `confirm` | 对话里会怎样 |
| --- | --- |
| `never` | 直接执行。只能用于 `mutating: false` 的工具 |
| `always` | 每轮对话第一次调用前，页面弹出确认，用户点允许才执行；同一轮里再调用同一个工具不再问 |
| `by_args` | 只有参数里出现 `confirm_args` 中的某个名字时才确认，例如带 `overwrite` 才问 |

用户拒绝时工具不会执行，模型收到错误码 `rejected`（“这个工具需要确认后才能执行。”）。

## 6. 从零做一个工具：以 `dice_roll` 为例

仓库里的 `toolpacks/examples/dice_roll/` 就是一个完整的包。下面一步一步重做一遍。

**第 1 步：建目录。** 例如 `D:\toolpacks\dice_roll\`（目录名 = 工具名）。

**第 2 步：写 `tool.json`。**

<!-- validate:manifest -->
```json
{
  "schema": "mas.tool/1",
  "name": "dice_roll",
  "version": "0.1.0",
  "description": "示例工具包：掷骰子。只读，不碰本机内容，不联网。用来演示工具包怎么写、安装和分享。",
  "order": 900,
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "sides": {"type": "integer", "minimum": 2, "maximum": 1000, "description": "可选。骰子面数，不填用配置里的默认面数。"},
      "times": {"type": "integer", "minimum": 1, "maximum": 20, "description": "可选。掷几次，默认 1。"}
    }
  },
  "mutating": false,
  "confirm": "never",
  "entry": {"kind": "python", "module": "tool.py"},
  "config": [
    {"name": "default_sides", "type": "int", "default": 6, "required": true, "secret": false, "min": 2, "max": 1000,
     "description": "不传 sides 时用的面数。"},
    {"name": "api_key", "type": "secret", "default": "", "required": false, "secret": true,
     "description": "演示密钥字段：只存在本机 tools.json，导出工具包时不会带走。"}
  ],
  "requires": {"backends": [], "python": []},
  "permissions": {"read_local_content": false, "network": false, "write_files": false, "send_local_to_model": false}
}
```

**第 3 步：写 `tool.py`。**

```python
"""示例工具包的入口：call(arguments, config) -> dict。参数已按清单校验过。"""

import random


def call(arguments: dict, config: dict) -> dict:
    sides = int(arguments.get("sides") or config.get("default_sides") or 6)
    times = int(arguments.get("times") or 1)
    rolls = [random.randint(1, sides) for _ in range(times)]
    return {"ok": True, "executed": True, "sides": sides, "rolls": rolls, "has_key": bool(config.get("api_key"))}
```

可以先在命令行试一下函数本身：`python -c "import tool; print(tool.call({'times': 3}, {'default_sides': 6}))"`。

**第 4 步：安装。** 打开网页 → 右上角“工具” → 展开“安装工具包” → 填目录路径（如 `D:\toolpacks\dice_roll`）或 zip 路径 → “检查并安装” → 在确认框里看权限 → 确定。路径是**运行网页服务的那台机器**上的路径；不支持从浏览器上传。

**第 5 步：配置。** 下拉框“工具”分组里选 `dice_roll`，能看到“版本 0.1.0 · 已安装 · 模型可调用 · 确认：不用确认”和“权限：无”。把“默认面数”改成 20，填一个演示密钥，保存。下一次调用就生效。

**第 6 步：在对话里用。** 新建对话，说“帮我掷三次二十面骰”。模型会调用 `dice_roll`，工具时间线里能看到“调用 dice_roll”和结果。

**第 7 步：导出。** 在对话框里选中 `dice_roll` → “导出”，下载 `dice_roll.zip`。zip 里只有 `tool.json` 和 `tool.py`：你填的面数、密钥和 `.granted.json` 都不在里面。别人用第 4 步装这个 zip 即可，配置回到默认值。

**第 8 步：卸载。** 选中 → “卸载”。会删除 `toolpacks/installed/dice_roll/`；`tools.json` 里保存的配置留着，重新安装后还在。内置工具没有导出和卸载按钮。

### 进阶例子 1：联网查询（只读）

<!-- validate:manifest -->
```json
{
  "schema": "mas.tool/1",
  "name": "http_status",
  "version": "1.0.0",
  "description": "查询一个公开网址当前返回的 HTTP 状态码。只读，不下载正文。",
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "required": ["url"],
    "properties": {
      "url": {"type": "string", "minLength": 8, "description": "要检查的 http 或 https 地址。"}
    }
  },
  "mutating": false,
  "confirm": "never",
  "entry": {"kind": "python", "module": "tool.py"},
  "config": [
    {"name": "timeout_sec", "type": "int", "default": 5, "required": true, "secret": false, "min": 1, "max": 60,
     "description": "请求超时（秒）。"}
  ],
  "permissions": {"network": true}
}
```

```python
import urllib.error
import urllib.request


def call(arguments: dict, config: dict) -> dict:
    url = arguments["url"]
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "executed": False, "error": {"code": "bad_url", "message": "只支持 http 和 https。"}}
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=int(config.get("timeout_sec") or 5)) as response:
            return {"ok": True, "executed": True, "status": response.status}
    except urllib.error.HTTPError as exc:
        return {"ok": True, "executed": True, "status": exc.code}
    except (urllib.error.URLError, TimeoutError):
        return {"ok": False, "executed": True, "error": {"code": "unreachable", "message": "连不上这个地址。"}}
```

安装时确认框会写“要的权限：联网”。

### 进阶例子 2：写本机文件（要确认）

注意参数名不能叫 `text`、`content`、`path`、`filename`，所以这里用 `note` 和 `title`；文件保存的目录由用户在配置里定，不让模型指定路径。

<!-- validate:manifest -->
```json
{
  "schema": "mas.tool/1",
  "name": "save_note",
  "version": "1.0.0",
  "description": "把一段简短笔记追加到本机笔记文件。会写文件；带 overwrite 时先清空再写。",
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "required": ["title", "note"],
    "properties": {
      "title": {"type": "string", "minLength": 1, "description": "笔记标题。"},
      "note": {"type": "string", "minLength": 1, "description": "笔记内容，一两句话。"},
      "overwrite": {"type": "boolean", "description": "可选。true 时先清空笔记文件。"}
    }
  },
  "mutating": true,
  "confirm": "always",
  "entry": {"kind": "python", "module": "tool.py"},
  "config": [
    {"name": "notes_dir", "type": "path", "default": "", "required": true, "secret": false, "must_exist": true,
     "description": "笔记文件所在目录。"}
  ],
  "requires": {"backends": [], "python": []},
  "permissions": {"write_files": true}
}
```

`mutating: true` 所以不能用 `never`；如果只想在清空时才问，可以改成 `"confirm": "by_args", "confirm_args": ["overwrite"]`。

### 进阶例子 3：依赖 ComfyUI 的工具

<!-- validate:manifest -->
```json
{
  "schema": "mas.tool/1",
  "name": "comfy_queue_size",
  "version": "0.1.0",
  "description": "查看 ComfyUI 队列里还有几个任务。只读。",
  "parameters": {"type": "object", "additionalProperties": false, "properties": {}},
  "mutating": false,
  "confirm": "never",
  "entry": {"kind": "python", "module": "tool.py"},
  "config": [
    {"name": "comfy_url", "type": "string", "default": "http://127.0.0.1:8188", "required": true, "secret": false,
     "description": "ComfyUI 地址。后端配置不会传给第三方工具，这里要再填一次。"}
  ],
  "requires": {"backends": ["comfyui"], "python": []},
  "permissions": {"network": true}
}
```

对话框里会显示 ComfyUI 后端的状态和“去配置 ComfyUI”按钮；入口函数用自己的 `config["comfy_url"]` 去请求 `/queue`。

## 7. 常见安装错误

安装失败时对话框底部会显示“工具包不合格：……”，后面是下面这些原文（多条用“；”分开）。

| 提示原文 | 怎么改 |
| --- | --- |
| 要给一个工具包目录或 .zip 文件。 | 路径不存在，或者是普通文件不是 zip。检查路径是不是在运行网页服务的机器上 |
| 目录里没有 tool.json。 / 压缩包里找不到 tool.json。 | 清单要叫 `tool.json`，放在目录根部；zip 里只能有一个含 `tool.json` 的子目录 |
| 压缩包里有越界路径。 | 重新打包，不要包含 `../` |
| tool.json 不是合法 JSON：… | 修 JSON 语法（多余逗号、注释、单引号都不行） |
| schema 必须是 mas.tool/1。 | 加 `"schema": "mas.tool/1"` |
| 不认识的字段：… | 删掉表里没有的字段（例如把 `entry.type` 写在了顶层） |
| name 必须是小写字母开头的 2-48 位 [a-z0-9_]。 | 改名，例如 `DiceRoll` → `dice_roll` |
| version 必须是 主.次.修 三段数字。 | `1.0` → `1.0.0` |
| description 必填。 | 写一句说明 |
| parameters 必须是 type=object 的 JSON Schema，或 null（不提供给模型）。 | 顶层写 `"type": "object"` |
| parameters 必须写 additionalProperties: false。 | 加上 |
| 给模型的参数里不能有 …（本地正文不经过模型）。 | 参数改名，如 `text` → `note`，`path` → 放进配置 |
| parameter_sources.xxx 不可用。 | 只能对已声明的参数用 `comfyui.styles` |
| mutating 必须是 true/false。 | 写布尔值 |
| confirm 只能是 always、never、by_args。 | 改成其中之一 |
| 改外部状态的工具不能 confirm=never。 | 改 `always` 或 `by_args` |
| confirm=by_args 时 confirm_args 必须是非空的参数名列表。 | 加 `confirm_args` |
| entry.kind 只能是 python 或 host。 | 写 `{"kind": "python", "module": "tool.py"}`（是 `kind` 不是 `type`） |
| 只有内置工具可以用 entry.kind=host。 | 第三方包只能用 `python`，要把逻辑写进 `call` |
| entry.module 必须是 .py 文件。 / entry.module 必须是包内的相对路径。 / entry.module 指向的文件不在包里。 | 写包内相对路径，例如 `tool.py`，并确认文件存在 |
| 第三方工具必须声明 parameters。 | 不能写 `null`；没有参数就写 `{"type": "object", "additionalProperties": false, "properties": {}}` |
| config 字段名不合格或重复：… / config.xxx.type 只能是 … / config.xxx 缺 default（required、secret、description） | 按 §3 的 config 表补全 |
| config.xxx 是密钥，不能在包里写默认值。 | 密钥的 `default` 写 `""` |
| config.xxx 是 enum，要写 choices。 | 加 `choices` |
| requires.backends 必须是字符串列表。 | 例如 `["comfyui"]` |
| permissions 只能包含布尔值的 …。 | 只用四个键，值用 `true`/`false` |
| send_local_to_model 必须是 false：本地内容不能交给第三方模型。 | 改成 `false` |
| 和内置工具重名。 / 和后端重名。 | 换个名字（同名的已安装包会被覆盖升级，这是允许的） |
| 安装前要确认权限：… | 通过接口安装时 `accept_permissions` 要正好列出清单要的权限；在网页里点确认框的“确定”即可 |

装上以后调用时可能遇到：

| 错误码 | 原因 | 怎么改 |
| --- | --- | --- |
| `dependency_missing` | 后端没声明、Python 包没装、入口文件不见了 | 装好依赖；对话框里会列出缺什么 |
| `permission_denied` | 清单要的权限不在授权记录里（例如手改了清单） | 重新安装并确认权限 |
| `invalid_parameter` / `unexpected_parameter` | 模型传的参数不符合 `parameters` | 把 `description` 写清楚，或放宽范围 |
| `tool_error` | 入口抛了异常或没返回对象 | 看服务端日志，修代码，自己返回 `error` |

卸载时可能遇到“没有这个工具。”（`not_found`）或“内置工具不能卸载。”（`builtin_tool`）；导出内置工具会得到“内置工具依赖本仓库代码，不导出。”（`not_exportable`）。如果某个已安装包的清单坏了，列工具时会整体报错并写出是哪个包，删掉或修好 `toolpacks/installed/<name>/tool.json` 即可。

## 8. FAQ

**装完要重启吗？** 不用。下一次列工具或对话就能用。

**怎么升级？** 改 `version`，再安装一次同名的包，会整个替换旧目录，配置保留。

**配置和密钥存在哪？** 数据目录的 `tools.json`（`apps/web/data/tools.json`），不在包目录里，导出不会带走。

**我的工具能调用模型或出图吗？** 没有给工具包的接口。出图请用内置 `generate_image`。

**能读用户上传的文件或留下的正文吗？** 不能。这是 [ADR 0003](adr/0003-local-data-isolation.md) 的边界。

**能接 MCP 服务器吗？** 现在不能，`mcp` 字段只是保留。

**权限写 `network: false` 是不是就联不了网？** 不是。没有沙箱，权限只是声明和确认，靠代码审查。

**模型为什么不调用我的工具？** 多半是 `description` 没说清什么时候该用。写清楚用途和参数含义；也可以在对话里直接点名工具。

**能放多个文件吗？** 可以放（比如说明文档），但入口代码要在一个文件里，不能导入同目录的其他模块。

**工具包能在别的仓库用吗？** 能。导出的 zip 装到另一份仓库即可，配置回到默认值，权限要重新确认。
