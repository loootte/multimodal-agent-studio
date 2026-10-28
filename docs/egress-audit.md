# 出站审计（12.A，#57）

决策见 [ADR 0009](adr/0009-handle-scope-and-egress-audit.md)。本文件写实现：事件长什么样、存在哪、怎么看、怎么导出、怎么验。

## 一句话

所有离开本进程的请求（对话模型、第三方出图、ComfyUI 提交）都走 `services/gateway/egress.py` 的同一个钩子。
钩子只记**元数据**：目的地主机、渠道、字数字节数、是否含本地派生内容、指纹前缀、运行标识、原因码。
**不记**正文、提示词、密钥、完整 URL 路径和本地文件路径。

## 事件

类型和字段登记在 `packages/contracts/events.py`（`EGRESS_EVENTS`、`EGRESS_FIELDS`、`EGRESS_CHANNELS`、`EGRESS_SCOPES`、`EGRESS_REASONS`）。
同一次请求的几条记录共用 `egress_id`。

| 事件 | 何时 |
| --- | --- |
| `egress.planned` | 载荷已组好、检查通过、即将打开连接 |
| `egress.blocked` | 没有打开连接就拦下（网关查到本地正文、本地提示词不许发第三方、用户拒绝） |
| `egress.sent` | 对方已收下（带 `http_status`） |
| `egress.failed` | 打开了连接但失败（`http_error` / `connect_error` / `timeout` / `cancelled` / `rejected`） |

字段（只有这些，新增先在 `EGRESS_FIELDS` 登记并确认不是正文、提示词、密钥或路径）：

| 字段 | 说明 |
| --- | --- |
| `schema` | `mas.egress/1` |
| `type`、`egress_id`、`ts` | 事件类型、请求编号（`eg_` + 16 位十六进制）、秒级时间戳 |
| `channel` | `chat_completion` / `remote_image` / `comfyui_local` |
| `destination_host` | 只有主机名，去掉端口、路径、查询串和用户名密码 |
| `destination_scope` | `local`（回环）/ `lan`（私有地址）/ `third_party` |
| `modality` | 载荷里的模态（text / image …） |
| `char_count`、`byte_count` | 载荷字数（不含工具契约）、请求体字节数 |
| `message_count`、`tool_contract_bytes` | 对话消息条数、工具契约字节数（工具 schema 单独计） |
| `contains_local_derived` | 这次载荷是否由本地内容派生（例如本地提示词出图） |
| `handle_id_prefix` | 句柄前 6 个字符，只为对上号，不能解析回正文 |
| `content_fingerprint` | 请求体 sha256 的前 16 个十六进制字符 |
| `model` | 模型名（≤120 字） |
| `session_id`、`trace_id`、`run_id`、`agent_run_id` | 会话、链路、运行、子 Agent 运行 |
| `reason_code`、`http_status` | 拦截 / 失败原因码，HTTP 状态 |

原因码的白话说明在 `EGRESS_REASONS`，界面只显示白话，不回显任何正文。

## 挂钩位置

- `services/gateway/openai_chat.py`：`stream` 在开连接前 `egress.guard("chat_completion", …, local_bodies=…)`；查到本地正文就记 `blocked/local_content` 并抛 `local_content`，连接不打开。适配器带 `audits_egress = True`。
- `apps/web/remote_image.py`：第三方出图 `remote_image`。
- `poc/comfyui/comfy_client.py`：ComfyUI 提交 `comfyui_local`（目的地通常是本机，scope 照实记）。
- `apps/web/chat_run.py`：`remote_local_prompt=refuse` 拦下、用户拒绝时由宿主记 `blocked`（`local_prompt_remote_refused` / `user_declined`）；模型点名的句柄过期、越权或跨会话时也记 `blocked`（`handle_*`，12.B）；本地提示词出图时用 `egress.note(local_derived=True, handle=…)` 给下游请求打标。

运行上下文按 `trace_id` 登记（`egress.attach/detach`），会话、运行、子 Agent 运行编号从遥测的当前运行取。

## 存储

- 追加写 `<data_dir>/egress.jsonl`，一行一条。
- 同一条记录也存进该轮助手消息的 `egress` 列表（和 `events` 分开，回放时重画时间线），并以同名 SSE 事件实时推给页面。
- 写文件失败**不中断主流程**：stderr 打「警告：出站审计写入失败…」并计数，`/api/egress` 的 `write_failures` 能看到。监听者抛错同样只告警。

## 接口（只接受本机回环，其他来源 403）

- `GET /api/egress?session_id=&limit=`：`{"schema":"mas.egress.export/1","session_id","exported_at","events":[…],"summary":{…},"write_failures"}`。
- `GET /api/egress/export?session_id=`：同一载荷，作为附件 `egress-<session_id|all>.json` 下载。
- `session_id` 必须是 `s_` + 32 位十六进制，否则 400。

## 界面

- 运行过程侧栏新增「数据出站」时间线：一条请求一行（取最新状态），显示状态、渠道、目的地、范围、字节数、是否含本地派生内容、句柄前缀。
- 被拦下的行标红（`egress-blocked`），失败的行虚线框（`egress-failed`），下面一行白话原因。
- 「导出审计 JSON」按钮下载当前会话的导出。
- 确认卡（含多 Agent 冒泡上来的确认）附「数据出站：…」摘要：由宿主按工具清单的 `permissions`、当前出图配置和句柄记录生成（`by: "host"`），不取模型或工具返回的文字。
- 全部用 `textContent` 渲染，不用 `innerHTML`。

## 验证

- `python apps/web/check.py`：网关事件序列、字段白名单、无正文无密钥、写失败告警、坏监听者、HTTP 接口（403/400/导出头）、SSE 推送、确认卡摘要、本地提示词拒发 / 确认后发出、多 Agent 子运行记录、页面脚本检查。
- `python evals/run.py --only security`：`security.egress_audit_normal`（本地提示词出图：对话请求和 ComfyUI 提交都有 `sent`、ComfyUI 记录标本地派生和句柄前缀、审计文件与 API 无标记文字）和 `security.egress_audit_blocked`（网关查到本地正文：只有一条 `blocked`，供应商没收到请求）。
- `python evals/gate.py`：两条进入基线，回退即拦。

## 已知限制

- 指纹是请求体 sha256 前缀，不可逆，但对极短、可猜的提示词理论上可用字典比对确认；导出前请把它当作非公开数据。
- 只覆盖经过本进程网关和出图客户端的请求；第三方工具包若自己开连接，要到 12.C / 12.D 才统一拦截。
- 句柄作用域、过期、撤销见 [handle-scope.md](handle-scope.md)（12.B，#58）：句柄被拒时记 `egress.blocked`，原因码 `handle_not_found` / `handle_expired` / `handle_forbidden`。
