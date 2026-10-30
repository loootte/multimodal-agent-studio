# ADR 0004：工具规范

## 状态

已接受，注册方式已修订：

- [issue #31](https://github.com/loootte/multimodal-agent-studio/issues/31) 起，工具只从 `toolpacks/*/tool.json` 清单注册（[docs/tool-packs.md](../tool-packs.md)），不再扫描目录、不再按 Python 文件自注册。原来的外部工具目录已删除，给模型看的参数直接来自清单。
- [issue #89](https://github.com/loootte/multimodal-agent-studio/issues/89) 起，引擎从 `poc/tools/` 搬到 `services/tools/`，导入路径是 `services.tools`；四份内置 Python 工具的代码搬进各自的工具目录 `toolpacks/builtin/<名字>/tool.py`；`poc/tools/` 已删除。

下面「决定」一节写的是现在这棵树；最初按目录自注册的做法记在末尾「最初的决定（已取代）」。

## 背景

本机生成工具曾写在 `poc/comfyui/agent_tools.py`。发给第三方的契约曾写在 `apps/web/image_run.py`。用途增加以后，每加一个工具都要改注册表，参数也容易被复制到另一处。

[issue #13](https://github.com/loootte/multimodal-agent-studio/issues/13) 要求工具按规范自注册，并用子目录分开内部和外部。[ADR 0003](0003-local-data-isolation.md) 已经规定第三方只能看见工具契约，而且不为此新建 MCP 服务器。本决定待在那条边界里面。

## 决定

工具一份清单一个目录。注册表不写工具名单，也不扫描 Python 文件；它只读 `toolpacks/` 下的 `tool.json`。

```
toolpacks/
  builtin/<名字>/tool.json [+ tool.py]   内置工具，随仓库发布
  backends/<名字>.json                    共享后端（例如 ComfyUI）
  examples/<名字>/                        示例包（例如 dice_roll）
  installed/<名字>/                       用户安装的包（.gitignore；可用 MAS_TOOLPACKS_DIR 改位置）
services/tools/                           工具引擎
```

- 一个工具的代码只出现在它自己的目录里。`entry.kind=host` 的内置工具（`accept_local_content`、`list_local_content`、`forget_local_content`、`retrieve_local`、`plan_images`、`stage_prompt`、`build_skill_page`）是例外：它们依赖会话、确认和句柄，实现在 `apps/web/chat_run.py`。
- 内置 Python 工具（`generate_image`、`generate_video`、`lookup_demo`、`read_skill`，名单 `packs.BUILTIN_PYTHON`）的入口固定是自己目录里的 `tool.py`（`entry.module: "tool.py"`），在宿主进程里跑。清单校验拒绝名单外的名字和别的入口路径。表单工具 `character_image`（`entry.kind=module`）也是目录里的 `tool.py`。
- 第三方包的代码同样在包目录里，只在 12.D 子进程沙箱里跑，宿主进程不 import。
- 受众：`external`（给模型、网页和外部 MCP）的参数只来自清单的 `parameters`，属性名不能是正文、字节、路径或文件名；`tools/call` 只校验，不执行。`internal`（本机执行）的参数来自内置模块的 `tools()`，返回 `Tool`（`name`、`description`、`parameters`、`order`、`validate`、`call`）。`generate_image` 两边不同：模型给内容句柄，本机执行收画面描述，翻译留在网页宿主（`chat_run` / `image_run`）。`generate_video` 清单 `parameters=null`，不提供给模型。

引擎 `services/tools/`：`packs.py`（清单读取、校验、发现、安装、卸载、导出、授权）、`registry.py`（两种受众的注册和调用）、`schema.py`（参数校验）、`api.py`（`Tool`）、`sandbox.py` / `sandbox_runner.py`（第三方沙箱）、`mcp.py`（进程内 MCP 投影）、`_support.py`（内置出图、出视频共用的校验和监听）、`check.py`（离线检查，`python -m services.tools.check`）。`registry.load_modules` / `collect` 只留给检查做 `Tool` 规范探测，不是注册路径。

`services/runtime/tools.py` 负责授权和转发，从 `services.tools` 导入。`poc/comfyui/agent_tools.py` 仍是本机命令和导入入口，名单来自清单；网页出图仍调用 `call_tool("generate_image")`。`poc/comfyui/` 仍是本机 ComfyUI 脚本，不搬进 `services/`。

MCP 是注册表的进程内投影，不是第二个进程，也不监听端口。消息形状在 `packages/contracts/mcp.py`。`inputSchema` 就是该工具清单里的 `parameters`。本机投影和外部投影都在 `services/tools/mcp.py`。

不为此新建 MCP 服务器或数据库。`packages/artifacts` 只存文件字节。不另写 ComfyUI 客户端。

## 后果

- 同一用途不在内部和外部各写一份可执行实现。外部若要碰本地数据，仍遵守 [ADR 0003](0003-local-data-isolation.md)：收句柄，回公开字段。执行留在已有的本机工具或 `chat_run.py`。
- 离线检查（`python -m services.tools.check`、`apps/web/check.py`）确认 `Tool` 规范会拒绝外部参数里的正文、内置 Python 工具的入口只能是自己目录里的 `tool.py`、外部 `tools/call` 不执行。
- 本决定不实现传输层、登录或远程授权。要让本机进程以外的客户端调用 MCP，另写架构决定，并先写明谁授权、授权看哪一笔数据。
- 网页仍不调用 `generate_video`。issue #13 和本决定都不把路线图任一阶段标成完成。

## 最初的决定（已取代）

issue #13 时，工具都放在 `poc/tools`，注册表扫描目录、装载符合规范的 Python 文件：`internal/` 是本机工具，`external/` 是外部工具（只声明参数）。文件名以 `_` 开头的不注册，每个 `.py` 提供 `tools()`。当时也写了「不新建 `services/tools`」。issue #31 改为清单注册，issue #89 把引擎搬到 `services/tools` 并删除 `poc/tools`。
