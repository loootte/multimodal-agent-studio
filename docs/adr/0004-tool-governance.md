# ADR 0004：工具规范

## 状态

已接受。

## 背景

本机生成工具曾写在 `poc/comfyui/agent_tools.py`。发给第三方的契约曾写在 `apps/web/image_run.py`。用途增加以后，每加一个工具都要改注册表，参数也容易被复制到另一处。

[issue #13](https://github.com/loootte/multimodal-agent-studio/issues/13) 要求工具按规范自注册，并用子目录分开内部和外部。[ADR 0003](0003-local-data-isolation.md) 已经规定第三方只能看见工具契约，而且不为此新建 MCP 服务器。本决定待在那条边界里面。

## 决定

工具都放在 `poc/tools`。注册表不写工具名单。它只扫描目录，装载符合规范的 Python 文件。

- `poc/tools/internal/` 是本机工具。`tools/call` 校验后执行。
- `poc/tools/external/` 是外部工具。网页、第三方模型和外部 MCP 只用这一边。`tools/call` 只校验参数，不执行，不读本地正文，不提交 ComfyUI。

文件名以 `_` 开头的不注册。其余每个 `.py` 提供 `tools()`，返回一个 `Tool` 列表。`Tool` 包含 `name`、`description`、`parameters`，以及可选的 `order`。`parameters` 是 JSON Schema，类型为 object，并且拒绝未声明的参数。`order` 越小越靠前，不写则排在后面。同一边的名字不能重复。

本机工具还要提供 `validate` 和 `call`。外部工具只声明参数，不能提供这两项。外部参数的属性名不能是正文、字节、路径或文件名。

新工具只要把符合规范的文件放进对应目录，重启进程后就能列出来、校验，本机工具还可以执行。不改注册表，也不改 `agent_tools.py` 里的名单。`poc/comfyui/agent_tools.py` 仍是本机命令和导入入口，名单来自扫描结果。`apps/web/image_run.image_tools()` 转发外部扫描结果。出图仍调用 `call_tool("generate_image")`，走的是本机那一份实现。网页不调用 `generate_video`。

现在已经放进去的文件：

- 本机：`internal/image.py` 的 `generate_image`，`internal/video.py` 的 `generate_video`。参数仍包含画面描述。
- 外部：`external/local_content.py` 的接受、列出和删除，`external/image.py` 的 `generate_image`。出图只收内容句柄和公开字段。

MCP 是注册表的进程内投影，不是第二个进程，也不监听端口。消息形状在 `packages/contracts/mcp.py`。`inputSchema` 就是该工具自己声明的 `parameters`。本机投影和外部投影都在 `poc/tools/mcp.py`。

不为此新建 MCP 服务器、数据库、`services/tools` 或 `packages/artifacts`。不把 `poc/comfyui` 搬进 `services/`，也不另写 ComfyUI 客户端。

## 后果

- 同一用途不在内部和外部各写一份可执行实现。外部若要碰本地数据，仍遵守 [ADR 0003](0003-local-data-isolation.md)：收句柄，回公开字段。执行留在已有的本机工具或 `chat_run.py`。
- 离线检查会往临时目录放一个符合规范的文件，确认它会被装载；也会拒绝外部参数里的正文。检查还确认外部 `tools/call` 不执行。
- 本决定不实现传输层、登录或远程授权。要让本机进程以外的客户端调用 MCP，另写架构决定，并先写明谁授权、授权看哪一笔数据。
- 网页仍不调用 `generate_video`。issue #13 和本决定都不把路线图任一阶段标成完成。
