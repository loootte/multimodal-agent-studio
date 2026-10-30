# 工具包沙箱（12.D，#60）

第三方工具包的权限从“声明 + 确认”变成**运行时强制**。本文以代码为准：`services/tools/sandbox.py`（宿主侧）、`services/tools/sandbox_runner.py`（子进程入口）、`services/tools/packs.py`（名单、授权、审阅）、`services/runtime/tools.py`（`authorize`）。决定记录见 [ADR 0009](adr/0009-handle-scope-and-egress-audit.md#12d-实现定稿的沙箱)。

## 谁进沙箱

| 工具 | 运行在哪 | 约束 |
| --- | --- | --- |
| 安装的第三方包（`toolpacks/installed/*`，`entry.kind: "python"`） | 每次调用一个子进程 | 本文全部规则 |
| 内置宿主工具（`packs.HOST_TOOLS`） | 网页服务进程 | 名单 + 授权复核 |
| 内置 Python 工具（`packs.BUILTIN_PYTHON`，代码必须是自己工具目录里的 `tool.py`） | 网页服务进程 | 名单 + 授权复核；出网走 12.A 审计和 12.C 策略 |

名单写明每个内置工具真正需要的权限，例如 `retrieve_local → {read_local_content}`、`plan_images → {}`、`generate_image → {network, read_local_content, write_files}`。不在名单里的 `host` 工具或入口不是自己目录里 `tool.py` 的内置 Python 工具，清单校验直接拒绝。`packs.load_module` 对第三方包抛 `sandbox_required`，第三方代码不会被导入网页服务进程。

### 本机 ComfyUI 例外

`generate_image` / `generate_video` 要连本机（Windows 上常见 `http://127.0.0.1:8188`）或局域网的 ComfyUI。它们是名单内的宿主客户端，**不进沙箱**，出网仍记审计（渠道 `comfyui_local`）并按 12.C 策略检查。这是唯一允许连本机地址的路径；第三方包连本机地址一律拒绝，也就拿不到本机 ComfyUI 和本程序自己的 HTTP 接口。

## 调用流程

1. `services.runtime.tools.authorize(name)`（`execute_tool`、`chat_run._execute_web_tool`、`_apply_local_tool` 都先调）：
   - 授权记录 `.granted.json` 必须是 `mas.grant/2`，且 `digest` 等于当前包内容的摘要（`tool.json` 和包内其他文件的相对路径与字节，授权记录、`__pycache__` 等缓存和点文件除外）。否则 `permission_denied`，`grant` 为 `missing` / `outdated` / `changed`。
   - 内置工具：需要 ⊆ 声明 ⊆ 授权，否则 `permission_denied`，`denied` 列出缺的权限。
   - 第三方包：声明 ⊆ 授权。生效权限 = 声明 ∩ 授权。
2. 依赖检查、参数校验（不变）。
3. `sandbox.run(pack, arguments, config, granted)` 起子进程：`python -I -B -X utf8 sandbox_runner.py`，第一行 stdin 传 JSON 规格（模块、包目录、参数、配置、生效权限、可写目录、禁读目录、临时目录）。子进程装上 `sys.addaudithook` 钩子（装上后不能卸），再加载入口、调 `call`，结果按 JSON 行写回。

## 规则

### 环境变量

只继承 `SYSTEMROOT`、`WINDIR`、`LANG`、`LC_ALL`、`LC_CTYPE`、`TZ`，另设 `TMP` / `TEMP` / `TMPDIR` / `HOME` / `USERPROFILE` 指向本次临时目录、`MAS_SANDBOX=mas.sandbox/1`。用户 shell 里的 `OPENAI_API_KEY`、`GITHUB_TOKEN` 等一概拿不到。工具配置（含密钥）由宿主按 `tools.json` 和清单 `env` 字段解析后放进 `config` 显式注入。`-I` 会丢掉用户 site-packages，宿主把它作为额外路径显式传入，`requires.python` 里的包仍能导入。

### 网络

| 情况 | 结果 | 审计 |
| --- | --- | --- |
| 没授权 `network` | 任何 `connect` / `sendto` / `bind` / 域名解析都拦 | `egress.blocked`，渠道 `toolpack`，原因 `permission_denied` |
| 授权了，目的地是回环、未指定、链路本地地址，或解析不出 | 拦 | `egress.blocked`，原因 `egress_policy_denied` |
| 授权了，其他地址 | 放行 | `egress.planned` + `egress.sent`（表示连接已放行） |

子进程每次连接前经 IPC 问宿主，宿主裁决并记账；记录只有工具名（`tool` 字段）和主机名（`destination_host`），没有载荷。没授权时还把 `HTTP(S)_PROXY` 指向拒绝地址兜底。

### 文件

- 可写：本次调用的临时目录（调用结束删除）；授权 `write_files` 后再加 `<数据目录>/toolpack_data/<名字>/`，路径在 `MAS_TOOLPACK_DATA`。
- 禁读禁列：数据目录（会话 JSON、句柄、`tools.json`、审计），自己的数据目录除外。本地内容只能经句柄 API，而句柄 API 目前不给第三方包。
- 其他地方可读（Python 标准库、包目录等）。
- 路径先 `realpath` 再 `normcase` 比较，符号链接和 Windows 大小写都不能绕过。

### 进程和原生代码

`subprocess`、`os.system`、`os.exec*`、`os.fork`、`os.startfile`、`_winapi.CreateProcess`、`os.kill` 等拒绝（`process`）；`ctypes.*`、子解释器拒绝（`native`）。

### 超时和输出

`MAS_TOOLPACK_TIMEOUT_SEC`（默认 30，限在 1–300）。超时杀子进程，返回 `tool_timeout`。协议输出超过 1 MB 视为出错。包自己 `print` 的内容被挪到 stderr 并丢弃。

## 结果和错误码

- 任何越权尝试（包括包自己 `try/except` 吞掉的）都让这次调用返回 `permission_denied`，`violations` 列出类别（`network`、`write_files`、`read_local_content`、`process`、`native`），消息不带路径或正文；越权前包可能已执行一部分，所以不写 `executed`。同时记遥测 `security.sandbox_denied`（每个类别一条）。
- `tool_timeout`、`tool_error`（异常、没返回对象、子进程异常退出）、`sandbox_error`（子进程起不来）。

## 安装时的权限审阅

`POST /api/toolpacks/inspect` 返回 `review`：`permissions`（每项 `permission`、`label`、`risk`、`effect`）、`not_granted`、`sandbox`（沙箱限制几句话）、`mode: "subprocess"`。网页在工具对话框里展开「权限审阅」，每个权限勾上才能点“同意这些权限并安装”；工具详情里显示“运行方式：沙箱子进程”，授权记录失效时提示重新安装。

## 局限

- 审计钩子是 Python 层面的尽力而为：C 扩展直接发系统调用、或者已经导入的原生模块能绕过。操作系统级隔离（Windows Job Object / AppContainer，Linux seccomp / namespace）没做。只装看过代码的包。
- 内置工具和本机 ComfyUI 不进沙箱（见上）。
- 第三方包没有句柄 API，`read_local_content` 声明了也读不到本地内容。
- 每次调用起一个新进程，冷启动约几十到一百多毫秒。
