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

- 安装：工具对话框填本机目录或 zip 路径 → 先检查（`POST /api/toolpacks/inspect`）→ 页面列出权限请确认 → `POST /api/toolpacks/install {source, accept_permissions}`。同意的权限记在包目录的 `.granted.json`。
- 导出：`GET /api/toolpacks/<名字>/export` 得到 zip。配置值和密钥留在 `tools.json`，`.granted.json` 不进包。
- 卸载：`POST /api/toolpacks/<名字>/uninstall`。内置工具拒绝。

## 错误码

`invalid_manifest`、`invalid_package`、`duplicate_tool`、`dependency_missing`、`permission_denied`、`permission_not_accepted`、`builtin_tool`、`not_exportable`、`not_found`。

## 未做

没有沙箱或进程隔离，权限只声明和校验；不做市场和签名；MCP 字段保留；宿主类工具的实现仍在 `chat_run`；安装只收本机路径，不做上传。
