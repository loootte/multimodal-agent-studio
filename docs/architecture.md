# 架构

系统按层分开。上面的层只依赖下面公布的契约，不依赖某个模型供应商的 SDK。

```
Web 界面
    │  会话、附件、流式事件、工具时间线
    ▼
Agent 运行时
    │  循环、预算、工具校验、人工确认
    ▼
模型网关                         工具平面
    │  模态能力、适配器、流式         │  注册、权限、审计
    ▼                                ▼
供应商模型                       外部系统与检索
    ▲
工件库与会话存储
```

下面先写目标分层，再写每一层现在落到哪些文件。`services/runtime`、`services/tools`、`packages/artifacts`、`evals` 和 `ops` 还没有建。提前落地的文件不把路线图任一阶段标成完成。

## 一次请求

目标循环：

1. 界面提交本轮输入：文本和工件标识。文件本身已经在工件库。
2. 运行时装出本轮上下文：系统指令、近期对话、工件引用、可用工具。
3. 运行时问网关要模型能力。输入模态不被该模型接受时，直接失败，不偷偷转码或丢弃。
4. 网关把统一请求译成供应商请求，并把流式增量译回统一事件。
5. 若模型要调用工具，运行时先校验参数。写操作停下来等人确认。
6. 工具结果写回运行轨迹，再次进入循环，直到模型给出最终回答或预算用尽。
7. 界面对着同一条运行标识渲染回答和工具时间线。

预算、工具确认和多步循环还没有单独的运行时。现在接通的是一轮文字、一轮本地内容工具，或一轮文生图。这不把路线图任一阶段标成完成。

## 现在的一次出图

1. `apps/web/static/app.js` 把用户文字 `POST` 到 `/api/sessions/{session_id}/messages`。
2. `apps/web/server.py` 交给 `apps/web/chat_run.py`。出图时，原文先由 `apps/web/store.py` 的 `put_content` 记成本会话内容。发给模型的是一句不含画面的任务。
3. `services/gateway/openai_chat.py` 带着 `apps/web/image_run.py` 里的工具契约去请求供应商。载荷里一旦出现已入库的正文、文件文本或文件字节的 base64，连接不会打开。工具契约本身不参与这道检查。
4. 模型调用 `accept_local_content`，`purpose` 为 `image_prompt`，`kind` 为 `text`。`chat_run.py` 发出 `content_request`。页面上的文本框收集画面描述。草稿只出现在这条本机事件里。
5. 页面 `POST /api/runs/{run_id}/content`，正文是文字。`store.py` 的 `add_content` 用新的随机句柄留下这段文字。下一次模型请求只带句柄。
6. `chat_run.py` 用对话框里提交的文字填 `prompt`，丢掉模型自己写的画面描述。`image_run.py` 调用 `poc/comfyui/agent_tools.py` 的 `generate_image`。一条用户消息最多一次。
7. `poc/comfyui/template_fill.py` 只改白名单字段。`poc/comfyui/comfy_client.py` 把这张图 `POST /prompt`。
8. `poc/comfyui/artifacts.py` 按当前网页会话收成 `artifact_id`。页面再按这个标识取文件。

纯文字不进入第 6 步。文件句柄也不进入第 6 步，那次运行不提交 ComfyUI。停止只在 `chat_run.py` 里置位，并由另一条连接取消已经提交的 `prompt_id`。

## 现在的一次本地内容

没有出图关键词、但用户要求把内容留在本机时，走这一条。专用工具仍在这一个网页进程里。不新建 MCP 服务器、数据库、`services/tools` 或 `packages/artifacts`。

1. `app.js` 把说明文字 `POST` 到 `/api/sessions/{session_id}/messages`。要留下的正文不必写在这句里。聊天框上的附件仍然拒绝，不会跟这句一起发给模型。
2. `chat_run.py` 把这句说明交给模型。模型调用 `accept_local_content`。`purpose` 只接受 `image_prompt` 或 `keep`，其他文字丢掉。`kind` 是 `text` 或 `file`，不填则是 `text`。
3. `content_request` 带到页面。`kind` 为 `text` 时是文本框，为 `file` 时是文件选择。`app.js` 不用 `innerHTML`。提交文件时不带原始文件名。
4. 页面 `POST /api/runs/{run_id}/content`。文字进 `chat_run.submit_content`，文件进 `chat_run.submit_file`。响应只有 `ok`，不回正文、字节、路径或句柄。
5. `store.py` 新建随机句柄，形如 `c_` 加十六进制，不用正文或字节的哈希。文字写在会话 JSON 的 `contents`。文件字节写在 `apps/web/data/sessions/{session_id}/files/{handle}`。记录是句柄、种类、媒体类型、大小和用途。
6. 下一次模型请求只有句柄、种类和大小。模型可以调用 `list_local_content` 或 `forget_local_content`。回到模型的结果只有句柄、种类、媒体类型、大小、用途和状态。
7. `forget_content` 删掉本会话的记录和文件字节，并把已经出现在本会话消息里的正文换成一句不含正文的说明。别的会话列出、读取或删除，都像没有这枚句柄。
8. 若接着调用 `generate_image`：文字句柄按上面的出图注入。文件句柄到此结束，不调用 `image_run.generate`。
9. `openai_chat.py` 在打开连接前检查。已入库的文字、文件的文本，以及文件字节的 base64，长度不少于 8 且出现在工具契约以外，就以 `local_content` 失败。

日志行是 `content {会话} {句柄} {种类} {大小}`。不记正文、字节、路径、原始文件名和密钥。

## 各层的职责和代码

### 界面

只负责展示和收集输入。不拼供应商提示词，不决定工具能不能执行。

| 文件 | 做什么 |
| --- | --- |
| `apps/web/server.py` | 进程入口。只监听 `127.0.0.1`。静态页、会话、停止、SSE、本地内容提交、按 `artifact_id` 取图片都从这里分发 |
| `apps/web/static/index.html` | 三个区域和设置对话框：会话列表、对话、输入 |
| `apps/web/static/app.js` | 发送、停止、重连同一条运行、渲染事件。`content_request` 的 `kind` 为 `text` 时在气泡里放文本框，为 `file` 时放文件选择。不把原始文件名发给本机接口 |
| `apps/web/static/app.css` | 页面布局，包括本地文字框和文件选择 |
| `apps/web/README.md` | 这一页怎么启动、密钥放哪、离线检查怎么跑 |

`server.py` 上的路径：

| 路径 | 文件里的去向 |
| --- | --- |
| `GET /`、`GET /static/app.js`、`GET /static/app.css` | 直接回静态文件 |
| `GET/PUT /api/provider` | `store.py` 的 `ProviderStore` |
| `GET/POST /api/sessions`、`GET /api/sessions/{id}` | `store.py` 的 `SessionStore` |
| `POST /api/sessions/{id}/messages` | `chat_run.start`，响应是 SSE |
| `POST /api/sessions/{id}/stop` | `chat_run.stop` |
| `POST /api/runs/{id}/content` | 文字走 `chat_run.submit_content`，文件走 `chat_run.submit_file`。响应只有 `ok`，不回正文、字节、路径或句柄 |
| `GET /api/runs/{id}/events` | 接回同一次运行的 SSE |
| `GET /sessions/{session}/artifacts/{artifact}` | `image_run.open_artifact` |

### 运行时

拥有循环和停止条件。它是唯一可以调用工具的地方。模型文本里出现的「我已经执行了」不算执行。

这一层的目录 `services/runtime` 还没建。现在一轮回答放在网页进程里：

| 文件 | 做什么 |
| --- | --- |
| `apps/web/chat_run.py` | 一轮回答。文字直接流式写回。出图时先让模型调用 `accept_local_content`，等对话框提交，再注入正文并调用已有的 `generate_image`。语义唤起可以收文字或文件。`list_local_content` 和 `forget_local_content` 只操作本会话。同一条助手消息上排列 `content_request`、`tool_call`、`progress`、`artifact` 或 `error` |
| `apps/web/image_run.py` | 网页和 `poc/comfyui` 之间的唯一桥。发给模型的工具契约在这里。真正出图调用 `agent_tools.call_tool("generate_image")`。取消另开一个 ComfyUI 客户端，不关正在读模型的那条连接 |

`image_run.py` 里的契约是 `accept_local_content`、`list_local_content`、`forget_local_content` 和 `generate_image`。这些参数都没有正文、字节、路径或文件名。`generate_image` 只收 `content_handle`、画幅、可选种子和可选风格。文字句柄由 `chat_run.py` 在本机注入 `prompt`。文件句柄不调用 `generate_image` 的提交。`poc` 里的工具仍然要求 `prompt`，这个参数不转发给模型。

还没有放进运行时的部分：多步循环、预算、写操作前的人工确认、`generate_video`。issue #7 的视频队列、超时、参数白名单和参考图上传留在 `poc/comfyui`，不把路线图任一阶段标成完成。

### 模型网关

把供应商差异关在适配器里。应用代码看到的是统一的消息、模态部件、工具模式和流式事件。

| 文件 | 做什么 |
| --- | --- |
| `services/gateway/openai_chat.py` | 唯一的供应商适配器。`POST {base_url}/chat/completions`，`stream` 为 true。默认基址 `https://api.x.ai/v1`，默认模型 `grok-4.7`。把工具调用收成名字加 JSON。已入库文字、文件文本和文件字节的 base64，长度不少于 8 且出现在工具契约以外时，以 `local_content` 失败，且不打开连接 |
| `services/gateway/__init__.py` | 标明适配器只放在这个包 |
| `services/__init__.py` | 标明供应商协议不进其他服务目录 |
| `packages/contracts/events.py` | 界面、运行时和网关共用的事件名、错误码和人话。`content_request` 不是结束事件 |
| `packages/contracts/__init__.py`、`packages/__init__.py` | 标明这是共享契约，不在每一边各写一套名字 |
| `docs/model-gateway.md` | 网关要遵守的契约。它不是运行代码 |

密钥不进适配器源码。调用方把当次密钥传进来。页面提交的密钥由 `store.py` 的 `ProviderStore` 放在不进 Git 的 `apps/web/data/provider.json`。

### 工具平面

每个工具有名字、说明、参数模式、是否写外部状态、超时。运行时按这个注册表调用，不在业务代码里散落 HTTP 调用。

这一层的目录 `services/tools` 还没建。生成工具仍在 `poc/comfyui/`，网页不另写一套 ComfyUI 客户端。

| 文件 | 做什么 |
| --- | --- |
| `poc/comfyui/agent_tools.py` | `generate_image` 和 `generate_video`。校验参数、风格和画幅。模型看不到节点。网页目前只调用 `generate_image` |
| `poc/comfyui/template_fill.py` | 复制模板，只写字段对照里的白名单。模型拼出的工作流 JSON 不会被提交 |
| `poc/comfyui/comfy_poc.py` | 命令行的文生图、图生图、文生视频、图生视频。它等待下载完成 |
| `poc/comfyui/comfy_client.py` | `POST /prompt`、WebSocket `/ws`、`GET /history/{prompt_id}`、`GET /view`、取消。`submit` 拿到 `prompt_id` 就返回 |
| `poc/comfyui/styles.json` | 风格白名单，目前是 `photograph` 和 `illustration` |
| `poc/comfyui/workflows/image_api.json`、`image.fields.json` | 网页文生图用的模板和字段对照 |
| `poc/comfyui/workflows/image_i2i_api.json`、`image_i2i.fields.json` | 图生图模板。网页还没接 |
| `poc/comfyui/workflows/video_api.json`、`video.fields.json` | 文生视频模板。网页还没接 |
| `poc/comfyui/workflows/video_i2v_api.json`、`video_i2v.fields.json` | 图生视频模板。网页还没接 |
| `poc/comfyui/config.example.json` | 配置样例。本机的 `config.json` 不进 Git，也可用 `COMFY_*` 环境变量盖掉 |
| `poc/comfyui/chat.py` | 这张 POC 脚本自己的助手消息：`tool_call`、`progress`、`artifact` 或 `error`。网页的一轮回答不走这个文件 |
| `poc/comfyui/README.md` | 命令、字段对照和各条 issue 的边界 |

### 存储

两类数据：

- 会话和运行轨迹。要能按运行标识重放界面上看到的步骤。
- 工件。图片、音频、视频、文档。有类型、大小、校验和、保留期限。

工件包 `packages/artifacts` 还没建。会话在网页数据目录，生成文件在 POC 的工件库。

| 文件 | 做什么 |
| --- | --- |
| `apps/web/store.py` | `ProviderStore` 存基址、模型标识和密钥。`SessionStore` 存会话、消息、事件，以及本地内容句柄。`put_content` 按消息记一次，`add_content` 每次提交都新建句柄。`add_file` 把字节写进该会话的 `files/` 目录。`list_content` 和 `forget_content` 只看本会话。句柄是 `c_` 加随机十六进制，不编码正文或字节 |
| `apps/web/data/` | 不进 Git。`provider.json` 是供应商设置，`sessions/*.json` 是会话，`sessions/{id}/files/` 是上传文件的字节。接口响应不返回内容正文、字节、路径或原始文件名 |
| `poc/comfyui/artifacts.py` | 按会话保存生成文件和记录。对外只给 `artifact_id`。读文件必须会话和标识都对 |
| `poc/comfyui/artifacts/` | 不进 Git。下面按会话分 `files/` 和 `records/` |

本地文字在会话 JSON 的 `contents` 里。上传文件的字节在该会话目录，JSON 里只留句柄、种类、媒体类型、大小和用途。生成图片在 POC 工件库里。这些都由网页进程在同一次运行里写，没有单独的数据库。文件不会送进 ComfyUI。

### 检查

| 文件 | 做什么 |
| --- | --- |
| `apps/web/check.py` | 聊天页的离线检查。假供应商和假 ComfyUI 都只听本机。不访问外网，也不连接真正的 ComfyUI。覆盖出图隔离，也覆盖语义唤起、上传文件、跨会话、删除，以及文件句柄不出图 |
| `poc/comfyui/comfy_client.py check` | 客户端自己的离线检查 |
| `poc/comfyui/comfy_poc.py check` | 模板白名单会拒绝自由 JSON |
| `poc/comfyui/agent_tools.py check` | 两个生成工具的参数校验 |
| `poc/comfyui/artifacts.py check` | 工件只按会话和 `artifact_id` 读取 |
| `poc/comfyui/chat.py check` | POC 消息上的事件顺序 |

## 边界

- 模型不可用时，运行时停止并返回原因。界面不自己改用另一个模型，除非用户选了备用策略。
- 工件访问要带会话或租户范围。拿到标识不等于谁都能读。
- 日志里不记密钥，不记正文、文件字节、路径或原始文件名。记句柄、种类、大小和耗时。
- 本地内容不进供应商请求。文字在会话 JSON 里，文件字节在该会话目录里。网关在 `openai_chat.py` 打开连接之前再查一次载荷。
- 隔离原则见 [ADR 0003](adr/0003-local-data-isolation.md)。第三方模型只使用工具契约。本地数据由专用工具管理。外部访问没有授权就拒绝。

## 以后的进程

首版可以是一个应用进程加一个数据库。边界先用模块表达。到第 6 阶段再把界面和运行时拆成可以分开扩容的进程。不要在第 1 阶段就拆成一堆服务。

现在就是这一个进程：`python apps/web/server.py`。它同时提供页面、一轮回答、网关适配器和到 `poc/comfyui` 的调用。目录约定仍以 `docs/repository-layout.md` 为准。上面这些文件是提前放进去的切片。
