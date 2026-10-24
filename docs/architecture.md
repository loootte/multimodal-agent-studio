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

下面先写目标分层，再写每一层现在落到哪些文件。`services/tools`、`evals` 和 `ops` 还没有建。

## 一次请求

目标循环：

1. 界面提交本轮输入：文本和工件标识。文件本身已经在工件库。
2. 运行时装出本轮上下文：系统指令、近期对话、工件引用、可用工具。
3. 运行时问网关要模型能力。输入模态不被该模型接受时，直接失败，不偷偷转码或丢弃。
4. 网关把统一请求译成供应商请求，并把流式增量译回统一事件。
5. 若模型要调用工具，运行时先校验参数。写操作停下来等人确认。
6. 工具结果写回运行轨迹，再次进入循环，直到模型给出最终回答或预算用尽。
7. 界面对着同一条运行标识渲染回答和工具时间线。

文字这一刀走 `services/runtime` 的多步循环，步骤出现在聊天页右侧的运行过程窗口。明确的单次出图关键词仍走网页切片，一次出一张。已经留下的提示词要连续出图时，由第三方模型在运行时循环里多次调用 `generate_image`。`accept_local_content` 仍由网页切片接手对话框。写操作先在运行时发出 `tool.confirm`，聊天页给出确认和拒绝。确认之后循环继续。

## 现在的一次出图

1. `apps/web/static/app.js` 把用户文字 `POST` 到 `/api/sessions/{session_id}/messages`。
2. `apps/web/server.py` 交给 `apps/web/chat_run.py`。出图时，原文先由 `apps/web/store.py` 的 `put_content` 记成本会话内容。发给模型的是一句不含画面的任务。
3. `apps/web/chat_run.py` 把对话收成统一请求，交给 `services/gateway.stream`。默认适配器是 `openai_chat.py`。带着 `toolpacks/` 清单生成的工具契约去请求供应商。`image_run.image_tools()` 负责转发。载荷里一旦出现已入库的正文、文件文本或文件字节的 base64，连接不会打开。工具契约本身不参与这道检查。
4. 模型调用 `accept_local_content`，`purpose` 为 `image_prompt`，`kind` 为 `text`。`chat_run.py` 发出 `content_request`。页面上的文本框收集画面描述。草稿只出现在这条本机事件里。
5. 页面 `POST /api/runs/{run_id}/content`，正文是文字。`store.py` 的 `add_content` 用新的随机句柄留下这段文字。下一次模型请求只带句柄。
6. `chat_run.py` 用对话框里提交的文字填 `prompt`，丢掉模型自己写的画面描述。`image_run.py` 调用 `poc/comfyui/agent_tools.py` 的 `generate_image`。关键词切片一次用户消息最多一张。
7. `poc/comfyui/template_fill.py` 只改白名单字段。`poc/comfyui/comfy_client.py` 把这张图 `POST /prompt`。
8. `poc/comfyui/artifacts.py` 按当前网页会话收成 `artifact_id`。页面再按这个标识取文件。

纯文字不进入第 6 步。文件句柄也不进入第 6 步，那次运行不提交 ComfyUI。停止只在 `chat_run.py` 里置位，并由另一条连接取消已经提交的 `prompt_id`。

## 连续出图

用户说「用本地提示词连续生成5张」这类话时，不走上面的关键词切片。文字回合进入 `runtime.run`。模型列出本会话内容，选文字句柄，对每一张分别调用 `generate_image`。一次调用只出一张。工具契约没有张数字段。每张图的工具结果（句柄、工件标识、成败）写回轨迹，再问模型下一步。已经留下的提示词不再收集正文，也不把正文发给第三方。同一条助手消息上排列多张 `artifact`。写操作仍先确认。确认之后循环继续，不在第一张图后结束。张数由第三方模型理解：请求意味着多张时（不管有没有写数字，如「来一组四季风景」），模型先调用一次只读的 `plan_images`（`count` 1–20，可选 `items`），服务端把计划记在这次运行上。每张 `generate_image` 的结果带 `images_done_this_turn`、`images_total`、`images_remaining`。模型没做完计划就用文字收尾时，运行时循环的 `resume` 钩子追一句「已生成 k/N 张，请继续…」，连续两次没有新图就停；没有计划就不追。本地不再用正则数张数。关键词（`连续`、`用本地提示词` 等）只用来把请求从单次出图切片分流到运行时循环，不参与计数。时间预算只算模型思考，不算工具执行和等确认。

## 现在的一次本地内容

没有出图关键词、但用户要求把内容留在本机时，走这一条。专用工具仍在这一个网页进程里。不新建 MCP 服务器、数据库或 `services/tools`。文件字节进 `packages/artifacts`。MCP 只作为本进程里的契约投影，见 [ADR 0004](adr/0004-tool-governance.md)。

1. `app.js` 把说明文字 `POST` 到 `/api/sessions/{session_id}/messages`。要留下的正文不必写在这句里。对话附件是另一条路径：先上传到本会话，发送时只带句柄。
2. `chat_run.py` 把这句说明交给模型。模型调用 `accept_local_content`。`purpose` 只接受 `image_prompt` 或 `keep`，其他文字丢掉。`kind` 是 `text` 或 `file`，不填则是 `text`。
3. `content_request` 带到页面。`kind` 为 `text` 时是文本框，为 `file` 时是文件选择。`app.js` 不用 `innerHTML`。提交文件时不带原始文件名。
4. 页面 `POST /api/runs/{run_id}/content`。文字进 `chat_run.submit_content`，文件进 `chat_run.submit_file`。响应只有 `ok`，不回正文、字节、路径或句柄。
5. `store.py` 新建随机句柄，形如 `c_` 加十六进制，不用正文或字节的哈希。同一会话里相同正文或相同文件字节复用已有句柄。文字写在会话 JSON 的 `contents`。文件字节写在 `packages/artifacts` 的校验和库里。记录是句柄、种类、媒体类型、大小、用途和校验和。
6. 下一次模型请求只有句柄、种类和大小。模型可以调用 `list_local_content`、`retrieve_local` 或 `forget_local_content`。回到模型的结果只有句柄、种类、媒体类型、大小、用途、状态和可打开的地址。检索结果带来源标识，页面能点开。
7. `forget_content` 删掉本会话的记录和文件字节，并把已经出现在本会话消息里的正文换成一句不含正文的说明。别的会话列出、读取或删除，都像没有这枚句柄。
8. 若接着调用 `generate_image`：文字句柄按上面的出图注入。文件句柄到此结束，不调用 `image_run.generate`。
9. `openai_chat.py` 在打开连接前检查。已入库的文字、文件的文本，以及文件字节的 base64，长度不少于 8 且出现在工具契约以外，就以 `local_content` 失败。

日志行是 `content {会话} {句柄} {种类} {大小}`。不记正文、字节、路径、原始文件名和密钥。

## 各层的职责和代码

### 界面

只负责展示和收集输入。不拼供应商提示词，不决定工具能不能执行。

| 文件 | 做什么 |
| --- | --- |
| `apps/web/server.py` | 进程入口。只监听 `127.0.0.1`。静态页、身份、会话、附件、停止、SSE、本地内容提交、工具确认、按 `artifact_id` 取图片都从这里分发 |
| `apps/web/static/index.html` | 四个区域和设置对话框：会话列表、对话、输入、运行过程 |
| `apps/web/static/app.js` | 发送、附件预览、停止、重连同一条运行、渲染事件。工具时间线显示确认和拒绝。失败且可重试时出现重试。`content_request` 的 `kind` 为 `text` 时在气泡里放文本框，为 `file` 时放文件选择 |
| `apps/web/static/app.css` | 页面布局，包括附件、时间线和本地内容输入 |
| `apps/web/README.md` | 这一页怎么启动、密钥放哪、离线检查怎么跑 |

`server.py` 上的路径：

| 路径 | 文件里的去向 |
| --- | --- |
| `GET /`、`GET /static/app.js`、`GET /static/app.css` | 直接回静态文件 |
| `GET /api/identity` | `store.py` 的 `IdentityStore`。单用户开发登录 |
| `GET/PUT /api/provider` | `store.py` 的 `ProviderStore`。`input_modalities` 来自网关对该模型的能力声明。OpenAI 兼容适配器对 grok 和未知模型声明图像；`deepseek-v4-pro` 这类已知纯文本模型只声明文字 |
| `GET/POST /api/sessions`、`GET /api/sessions/{id}` | `store.py` 的 `SessionStore`。会话带 `user_id` |
| `POST /api/sessions/{id}/messages` | `chat_run.start`，响应是 SSE。附件只带句柄 |
| `POST /api/sessions/{id}/attachments`、`GET /api/sessions/{id}/attachments/{handle}` | 对话附件的上传和预览 |
| `POST /api/sessions/{id}/stop` | `chat_run.stop` |
| `POST /api/sessions/{id}/delete` | `chat_run.delete_session` → `SessionStore.delete`。删会话 JSON、会话目录、别的会话不再引用的附件和内容字节，以及 POC 工件库里这个会话的生成文件。会话还在回答时返回 409 `session_running`，不替用户停，页面提示先停下。删的是当前会话时页面新建一个空会话 |
| `GET /api/tools`、`/api/toolpacks/*` | `tool_config.listing` 和工具包检查、安装、卸载、导出，见 [tool-packs.md](tool-packs.md) |
| `POST /api/runs/{id}/content` | 文字走 `chat_run.submit_content`，文件走 `chat_run.submit_file`。响应只有 `ok`，不回正文、字节、路径或句柄 |
| `POST /api/runs/{id}/confirm` | `chat_run.submit_confirm`。写操作的确认或拒绝 |
| `GET /api/runs/{id}/events` | 接回同一次运行的 SSE |
| `GET /sessions/{session}/artifacts/{artifact}` | `image_run.open_artifact` |

### 运行时

拥有循环和停止条件。它是唯一可以调用工具的地方。模型文本里出现的「我已经执行了」不算执行。

`services/runtime` 拥有循环、预算、校验、确认和追踪。文字回合由 `chat_run.py` 交给 `runtime.run`，步骤写成 `runtime.step`，显示在聊天页右侧窗口。`accept_local_content` 仍由网页切片接手对话框。`list_local_content`、`retrieve_local`、`forget_local_content` 和 `generate_image` 在确认之后于循环内执行，结果写回轨迹再问模型下一步。

| 文件 | 做什么 |
| --- | --- |
| `services/runtime/loop.py` | 观察、决定、调用工具或回答，直到结束或碰到预算。模型文本里说已经执行，不算执行 |
| `services/runtime/budget.py` | 步数、时间和 token 预算。超过时停下并留下可读原因 |
| `services/runtime/trace.py` | 按 `run_id` 串起模型调用、工具调用、输入模态和耗时 |
| `services/runtime/tools.py` | 校验并调用 `poc/tools`。不另建 `services/tools` |
| `services/runtime/skills.py` | 扫描 `skills/*/SKILL.md`。系统指令只带技能名字和短描述。模型调用 `read_skill` 之后，这一轮才带上正文 |
| `services/runtime/agents.py` | 多智能体（ADR 0005）：`mas.agent/1` 清单校验、发现、安装导出；`delegate` 工具（层数 ≤2、禁止成环、只传句柄）；按清单裁剪工具并在运行时拒绝越权（`tool_not_allowed`）；树预算池、并行上限 2、同一后端串行；子 Agent 复用 `loop.run`，追踪写 `type: agent` 的 span（带 `parent_run_id`），遥测 `agent.run` |
| `agents/builtin/*/agent.json` | 内置 orchestrator、planner、image、reviewer |
| `poc/tools/internal/demo.py`、`toolpacks/builtin/lookup_demo/tool.json` | 只读演示 `lookup_demo` |

输入框旁勾上「多智能体」时，`chat_run.py` 把这一轮交给编排者：系统指令和工具换成编排者清单，`delegate` 起子 Agent；子 Agent 的工具调用、确认卡和 `agent` 节点事件都标出 Agent 名和 `agent_run_id`，界面右侧画成「编排者 → 子 Agent → 工具调用」的树。`accept_local_content` 只给编排者。不勾时路径不变。见 [agents.md](agents.md)。

现在一轮回答仍放在网页进程里：

| 文件 | 做什么 |
| --- | --- |
| `apps/web/chat_run.py` | 一轮回答。交给 `runtime.run`，界面只画统一运行过程窗口。系统指令附带已安装技能的名字和短摘要。把图片附件把手交给网关写进图像部件。明确的单次出图关键词仍走网页切片：一次出一张。已留下的提示词要连续出图时，交给第三方模型在运行时循环里按次调用 `generate_image`，一次一张。`accept_local_content` 仍走网页切片，先对话框里提交正文；文件不能出图。解剖说明走 `read_skill` 与 `build_skill_page`，不要走 `generate_image`。本地会话内容工具只看本会话；模型侧是会话级摘要加可引用句柄，没有正文。同一条助手消息上可有 `content_request`、`tool.confirm`、`tool_call`、`progress`、多张 `artifact` 和 `error` |
| `apps/web/skill_page.py` | 用本机 Node 跑 `skills/anatomy` 的最小例子，把页面存成这个会话的 HTML 工件。没有 Node 时这一轮以可读错误结束，不落到出图 |


`generate_video` 仍不在这一刀。issue #7 的视频队列、超时、参数白名单和参考图上传留在 `poc/comfyui`。不把第 5 或第 6 阶段标成完成。

### 模型网关

把供应商差异关在适配器里。应用代码看到的是统一的消息、模态部件、工具模式和流式事件。

| 文件 | 做什么 |
| --- | --- |
| `packages/contracts/request.py` | 统一请求和能力声明的形状。部件是文字或工件标识 |
| `services/gateway/__init__.py` | `stream` 和 `describe`。按模型标识选适配器，先核对能力再打开连接 |
| `services/gateway/registry.py` | 适配器注册表。同一条请求换模型标识就换已接入的适配器 |
| `services/gateway/openai_chat.py` | OpenAI 兼容适配器。`POST {base_url}/chat/completions`，`stream` 为 true。默认基址 `https://api.x.ai/v1`，默认模型 `grok-4.7`。文字流式，图像按工件标识写成 data URL。音频和视频返回 `modality_unsupported`，不静默丢掉。工具调用收成名字加 JSON。错误带 `retryable`。已入库文字、文件文本和文件字节的 base64，长度不少于 8 且出现在工具契约以外时，以 `local_content` 失败，且不打开连接 |
| `services/__init__.py` | 标明供应商协议不进其他服务目录 |
| `packages/contracts/events.py` | 界面、运行时和网关共用的事件名、错误码、人话和 `retryable`。网关工具事件是 `tool.requested`。`content_request` 不是结束事件 |
| `packages/contracts/mcp.py` | 进程内 MCP 的 `tools/list` 和 `tools/call` 形状。不监听端口 |
| `packages/contracts/__init__.py`、`packages/__init__.py` | 标明这是共享契约，不在每一边各写一套名字 |
| `packages/artifacts/store.py` | 按校验和保存上传文件。会话记录只引用句柄 |
| `docs/model-gateway.md` | 网关要遵守的契约。它不是运行代码 |

密钥不进适配器源码。调用方把当次密钥传进来。页面提交的密钥由 `store.py` 的 `ProviderStore` 放在不进 Git 的 `apps/web/data/provider.json`。

### 工具平面

每个工具有名字、说明、参数模式、是否写外部状态、超时。运行时按这个注册表调用，不在业务代码里散落 HTTP 调用。

这一层的目录 `services/tools` 还没建。生成工具仍在 `poc/comfyui/`，网页不另写一套 ComfyUI 客户端。

| 文件 | 做什么 |
| --- | --- |
| `poc/comfyui/agent_tools.py` | 本机工具的导入和命令入口。名单来自 `poc/tools/internal/` 的扫描。网页目前只调用 `generate_image` |
| `poc/tools/registry.py`、`poc/tools/packs.py` | 只从 `toolpacks/` 清单注册工具，见 [tool-packs.md](tool-packs.md)。安装的包重启后可用 |
| `poc/tools/internal/image.py` | 本机 `generate_image`。参数含画面描述。会改外部状态 |
| `poc/tools/internal/video.py` | 本机 `generate_video`。参数含画面描述、时长白名单和参考图。会改外部状态 |
| `poc/tools/internal/demo.py` | 本机只读演示 `lookup_demo`。按主题返回固定说明 |
| `poc/tools/mcp.py` | 两边的进程内 MCP 投影。本机 `tools/call` 会执行，外部只校验 |
| `poc/comfyui/template_fill.py` | 复制模板，只写字段对照里的白名单。模型拼出的工作流 JSON 不会被提交 |
| `poc/comfyui/comfy_poc.py` | 命令行的文生图、图生图、文生视频、图生视频。它等待下载完成 |
| `poc/comfyui/comfy_client.py` | `POST /prompt`、WebSocket `/ws`、`GET /history/{prompt_id}`、`GET /view`、取消。`submit` 拿到 `prompt_id` 就返回 |
| `poc/comfyui/styles.json` | 风格白名单，目前是 `photograph` 和 `illustration` |
| `poc/comfyui/workflows/image_api.json`、`image.fields.json` | 网页文生图用的模板和字段对照 |
| `poc/comfyui/workflows/image_i2i_api.json`、`image_i2i.fields.json` | 图生图模板。网页还没接 |
| `poc/comfyui/workflows/video_api.json`、`video.fields.json` | 文生视频模板。网页还没接 |
| `poc/comfyui/workflows/video_i2v_api.json`、`video_i2v.fields.json` | 图生视频模板。网页还没接 |
| `poc/comfyui/config.example.json` | 配置样例。本机的 `config.json` 不进 Git，也可用 `COMFY_*` 环境变量盖掉 |
| `apps/web/tool_config.py` | 工具配置（issue #28 第 1 部分）。每个工具声明配置项（字段名、类型 string/int/enum/path/secret、默认值、必填、保密、说明）。用户改过的值存到数据目录的 `tools.json`（和 `provider.json` 同目录），保密字段只回掩码提示。优先级：`COMFY_*` 环境变量 > `tools.json` > `config.json`/`config.example.json` 迁来的默认值。`comfy_poc.load_config` 每次调用 `CONFIG_OVERLAY` 钩子现读，改完下一次运行生效。接口 `GET /api/tools`、`GET/PUT /api/tools/{name}/config`、`POST /api/tools/{name}/test`（`/system_stats` 或 `/queue` 判断能否连上，`/object_info` 核对所选工作流的节点） |
| `poc/comfyui/chat.py` | 这张 POC 脚本自己的助手消息：`tool_call`、`progress`、`artifact` 或 `error`。网页的一轮回答不走这个文件 |
| `poc/comfyui/README.md` | 命令、字段对照和各条 issue 的边界 |

### 存储

两类数据：

- 会话和运行轨迹。要能按运行标识重放界面上看到的步骤。
- 工件。图片、音频、视频、文档。有类型、大小、校验和、保留期限。

| 文件 | 做什么 |
| --- | --- |
| `packages/artifacts/store.py` | 按 sha256 保存上传文件的字节。同一份文件只写一次。会话记录只留句柄和校验和 |
| `apps/web/store.py` | `IdentityStore` 是单用户开发登录。`ProviderStore` 存基址、模型标识和密钥。`SessionStore` 存会话、消息、事件、对话附件、本地内容句柄和会话记忆。会话带 `user_id`。对话附件句柄是 `a_` 加随机十六进制。本地内容句柄是 `c_` 加随机十六进制，不编码正文或字节。同一会话相同字节或相同正文复用句柄。`retrieve` 按本会话检索，结果带来源标识和可打开的地址 |
| `apps/web/data/` | 不进 Git。`identity.json` 是本机用户，`provider.json` 是供应商设置，`sessions/*.json` 是会话，`artifacts/blobs/` 是上传文件的字节。接口响应不返回内容正文、字节或路径 |
| `poc/comfyui/artifacts.py` | 按会话保存生成文件和记录。图片、视频，以及技能做成的 HTML 页面。对外只给 `artifact_id`。读文件必须会话和标识都对 |
| `poc/comfyui/artifacts/` | 不进 Git。下面按会话分 `files/` 和 `records/` |

本地文字在会话 JSON 的 `contents` 里。上传文件的字节在 `packages/artifacts`，JSON 里只留句柄、种类、媒体类型、大小、用途和校验和。生成图片在 POC 工件库里。这些都由网页进程在同一次运行里写，没有单独的数据库。删除会话时一起清掉；按校验和共用的字节只有在别的会话都不引用时才删。文件不会送进 ComfyUI。发给模型的近期对话附带会话记忆摘要和引用，不含本地正文。

### 遥测

`services/telemetry.py`。一次运行一个 trace_id（就是网页运行的 `run_id`），绑定在运行线程上，
依次记下 `web.request` → `provider`（模型、耗时、供应商返回的 `usage`，没有时按字数估算 `tokens_estimated`）
→ `tool`（名称、耗时、成败）→ `comfyui.submit` / `comfyui.run`（带 ComfyUI `prompt_id`）→ `run`（最终状态、总耗时）。
失败带 `error_code` 和归类 `failure_class`（cancelled、timeout、budget、config、auth、policy、validation、provider、backend、tool）。
只记元数据，不记提示词、回答和密钥。

写到 `<data_dir>/telemetry.jsonl`。接口：

- `GET /api/telemetry?trace_id=&limit=`：最近的记录和按种类的汇总（次数、失败率、平均/最大耗时、token）；
- `GET /api/telemetry/{trace_id}`：一次运行的整条链路。

### 检查

| 文件 | 做什么 |
| --- | --- |
| `apps/web/check.py` | 聊天页的离线检查。假供应商和假 ComfyUI 都只听本机。不访问外网，也不连接真正的 ComfyUI。覆盖运行时的只读演示循环、步数预算、写操作确认和取消，也覆盖聊天页附件、工具时间线、确认拒绝、失败重试、运行过程窗口、统一请求、出图隔离、语义唤起、上传文件、同一会话复用工件、检索来源标识和会话记忆，以及外部 MCP 只校验、不执行。等距技能只在普通回合带上名字和短描述；模型来读之后这一轮才带正文，页面按 `artifact_id` 打开，没有 Node 时不落到出图 |
| `poc/comfyui/comfy_client.py check` | 客户端自己的离线检查 |
| `poc/comfyui/comfy_poc.py check` | 模板白名单会拒绝自由 JSON |
| `poc/comfyui/agent_tools.py check` | 两个生成工具的参数校验，以及本机 MCP 投影会执行 |
| `poc/comfyui/artifacts.py check` | 工件只按会话和 `artifact_id` 读取 |
| `poc/comfyui/chat.py check` | POC 消息上的事件顺序 |

## 边界

- 模型不可用时，运行时停止并返回原因。界面不自己改用另一个模型，除非用户选了备用策略。
- 工件访问要带会话或租户范围。拿到标识不等于谁都能读。
- 日志里不记密钥，不记正文、文件字节、路径或原始文件名。记句柄、种类、大小和耗时。
- 本地内容不进供应商请求。文字在会话 JSON 里，文件字节在工件库里。网关在 `openai_chat.py` 打开连接之前再查一次载荷。
- 隔离原则见 [ADR 0003](adr/0003-local-data-isolation.md)。第三方模型只使用工具契约。本地数据由专用工具管理。外部访问没有授权就拒绝。工具规范和进程内 MCP 见 [ADR 0004](adr/0004-tool-governance.md)。

## 以后的进程

首版可以是一个应用进程加一个数据库。边界先用模块表达。到第 6 阶段再把界面和运行时拆成可以分开扩容的进程。不要在第 1 阶段就拆成一堆服务。

现在就是这一个进程：`python apps/web/server.py`。它同时提供页面、一轮回答、网关适配器和到 `poc/comfyui` 的调用。目录约定仍以 `docs/repository-layout.md` 为准。上面这些文件是提前放进去的切片。
