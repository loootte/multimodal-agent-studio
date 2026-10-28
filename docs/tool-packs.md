# 工具包规范

> 想知道什么样的工具能装、怎么从零写一个并安装，请看 [工具包使用和编写指南](tool-pack-guide.md)。本页是字段的简明参考。

[issue #31](https://github.com/loootte/multimodal-agent-studio/issues/31)。每个工具是一个目录，目录里一份 `tool.json` 清单。运行时只从清单注册工具，不再扫描 Python 文件。

## 目录

| 位置 | 内容 |
| --- | --- |
| `toolpacks/builtin/<名字>/tool.json` | 内置工具。随仓库走，不能卸载、不能导出 |
| `toolpacks/backends/<名字>.json` | 共享后端声明，例如 ComfyUI 地址 |
| `toolpacks/examples/` | 示例包，例如 `dice_roll` |
| `toolpacks/installed/` 或 `MAS_TOOLPACKS_DIR` | 安装的包。已忽略，不进版本库 |

## 清单字段（`schema: "mas.tool/1"`）

| 字段 | 说明 |
| --- | --- |
| `name` | 工具名，小写字母数字下划线，全局唯一 |
| `version` | `x.y.z` |
| `description`、`order` | 给模型的说明；排序 |
| `parameters` | 发给模型的 JSON Schema，`type: object`，`additionalProperties: false`。`null` 表示不提供给模型（例如 `generate_video`） |
| `mutating` | 是否改外部状态 |
| `confirm` | `always`、`never` 或 `by_args`（配 `confirm_args`，参数里出现这些键才确认） |
| `entry` | `{"kind": "host"}` 由宿主实现（仅内置）；`{"kind": "python", "module": "tool.py"}` 由包内模块的 `call(args, config)` 实现 |
| `config` | 配置字段，格式同 #28（`int`、`string`、`secret` 等） |
| `requires.backends` | 依赖的后端名 |
| `permissions` | `read_local_content`、`network`、`write_files`、`send_local_to_model`。最后一项必须为 `false` |
| `parameter_sources` | 参数枚举的来源，例如 `style ← comfyui.styles` |
| `mcp` | 保留，未实现 |

后端清单用 `schema: "mas.backend/1"`。ComfyUI 的配置写成 `"builtin:comfyui"`，沿用已有字段。

## 不变的约束

- 外部参数不含正文、字节、路径、文件名；本地内容不进模型请求。
- 一次 `generate_image` 出一张；张数由 `plan_images` 定。
- 没有 `tools.json` 时行为不变。

## 安装、导出、卸载

- 安装：工具对话框填本机目录或 zip 路径 → 先检查（`POST /api/toolpacks/inspect`，返回里多了 `review`：`permissions[{permission,label,risk,effect}]`、`not_granted`、`sandbox`、`mode: "subprocess"`）→ 页面展开「权限审阅」，每个权限逐项勾选 → `POST /api/toolpacks/install {source, accept_permissions}`。同意的权限和包内容摘要记在包目录的 `.granted.json`（`{"schema": "mas.grant/2", "permissions", "version", "digest"}`）。
- 调用前复核（12.D）：授权记录要存在、是 `mas.grant/2`、摘要和当前包内容一致（否则 `permission_denied`，`grant` 为 `missing` / `outdated` / `changed`）；生效权限 = 清单声明 ∩ 授权。内置工具另有名单 `packs.HOST_TOOLS` / `packs.BUILTIN_PYTHON`，写明每个工具实际需要的权限，运行时要求 需要 ⊆ 声明 ⊆ 授权，缺了返回 `permission_denied`（带 `denied`）。工具列表的 `pack` 里多了 `sandbox`（`subprocess` / `host`）和 `grant`。
- 强制确认（12.E）：`network` 或 `read_local_content` 为真的包必须 `confirm: "always"`，否则检查 / 安装 / 发现报 `invalid_manifest`；这类包每次调用都确认，不在一轮里复用；`review` 多 `confirm_every_call`，工具列表的 `pack` 多 `sensitive`。见 [sensitive-confirm.md](sensitive-confirm.md)。
- 导出：`GET /api/toolpacks/<名字>/export` 得到 zip。配置值和密钥留在 `tools.json`，`.granted.json` 不进包。
- 卸载：`POST /api/toolpacks/<名字>/uninstall`。内置工具拒绝。

## 错误码

`invalid_manifest`、`invalid_package`、`duplicate_tool`、`dependency_missing`、`permission_denied`、`permission_not_accepted`、`builtin_tool`、`not_exportable`、`not_found`。

沙箱调用（12.D）另有：`permission_denied`（带 `violations`，越权的类别 `network` / `write_files` / `read_local_content` / `process` / `native`）、`tool_timeout`（超过 `MAS_TOOLPACK_TIMEOUT_SEC`，默认 30 秒）、`tool_error`（入口异常、没返回对象、子进程异常退出）、`sandbox_error`（子进程没能启动）。在进程内装载第三方包代码（`packs.load_module`）一律拒绝：`sandbox_required`。

## 沙箱与本机 ComfyUI 例外

第三方工具包（`toolpacks/installed/` 下、`entry.kind: "python"`）在沙箱子进程里运行，权限按清单和授权强制执行，细节见 [tool-sandbox.md](tool-sandbox.md)。要点：

- 没有 `network`：任何连接和域名解析被拦，返回 `permission_denied`；有 `network`：只许连非本机地址，连本机（回环、未指定、链路本地、解析不出的主机）返回 `permission_denied`，审计原因 `egress_policy_denied`。每次决定都记进出站审计，渠道 `toolpack`。
- 没有 `write_files`：只能写本次调用的临时目录；有：再加 `<数据目录>/toolpack_data/<名字>/`（环境变量 `MAS_TOOLPACK_DATA`）。
- 数据目录（会话、句柄、配置、审计）不能读、不能列；本地内容只能经句柄接口，而句柄接口目前不开放给第三方包。
- 环境变量白名单，密钥和配置只经 `config` 显式注入。

**例外：内置工具不进沙箱。** `HOST_TOOLS` 里的宿主工具和 `BUILTIN_PYTHON` 里的 `generate_image` / `generate_video` / `lookup_demo` / `read_skill` 在网页服务进程里运行（模块必须在 `poc/tools/internal/` 下），受名单和授权复核约束。其中出图、出视频要访问**本机或局域网的 ComfyUI**（Windows 上通常是 `http://127.0.0.1:8188`），这是唯一被允许连本机地址的出网，按“本机白名单客户端”处理：它走 12.A 审计（渠道 `comfyui_local`）和 12.C 策略，但不受沙箱限制。第三方包想连本机 ComfyUI 会被拒，请用内置 `generate_image`。

## 未做

沙箱是 Python 审计钩子 + 子进程，不是操作系统级隔离（C 扩展可绕过；Windows Job Object / Linux seccomp、namespace 未做）；内置工具和本机 ComfyUI 不进沙箱（见上）；第三方包没有句柄接口；不做市场和签名；MCP 字段保留；宿主类工具的实现仍在 `chat_run`；安装只收本机路径，不做上传。
