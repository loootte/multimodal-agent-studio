# 安全清单：工具权限与提示注入

范围：网页聊天、运行时循环、工具注册表、ComfyUI 出图。每一项写明**怎么检查**：
标 `自动` 的在 `python evals/run.py` 里跑（类别 `security`），发布门禁 `python evals/gate.py` 会拦住回退；
标 `人工` 的在发布前逐条过一遍。

## 1. 本地内容不出本机

| # | 要求 | 检查 |
| --- | --- | --- |
| 1.1 | 画面描述、要记下的文字、本地文件内容只通过本地内容窗口进入本机存储，模型只看到 `content_handle` | 自动：`security.local_content_never_sent`（出图和记下两条路径，任何供应商请求里都不能出现标记文字） |
| 1.2 | 文生图时用户提示词不进入供应商请求 | 自动：`text_to_image.teapot` 的 `prompt_not_in_provider` |
| 1.3 | 密钥不出现在事件流、会话文件、遥测和日志里 | 自动：`apps/web/check.py`（事件流、遥测）；人工：查看 `data/` 下文件 |
| 1.4 | 遥测只记种类、耗时、用量、错误码和 ComfyUI prompt_id，不记提示词和回答原文 | 自动：`apps/web/check.py` 的遥测用例；人工：新增遥测字段时复查 |
| 1.5 | 每次出站请求（对话模型、第三方出图、ComfyUI 提交）都记 `egress.planned/blocked/sent/failed`，记录只含 `EGRESS_FIELDS` 里的元数据，无正文、提示词、密钥、路径（#57） | 自动：`security.egress_audit_normal`、`security.egress_audit_blocked`；`apps/web/check.py` 的网关出站用例 |
| 1.6 | 网关查到本地正文、本地提示词不许发第三方、用户拒绝时，记 `egress.blocked` 和原因码，连接不打开 | 自动：`security.egress_audit_blocked`；`apps/web/check.py` 的本地提示词出图用例 |
| 1.7 | `/api/egress` 与导出只接受本机回环，非法 `session_id` 返回 400；导出无正文、无密钥 | 自动：`apps/web/check.py` 的出站接口用例 |
| 1.8 | 审计写不进去不打断主流程，但 stderr 告警并计入 `write_failures` | 自动：`apps/web/check.py`；人工：发布前看一眼 `/api/egress` 的 `write_failures` 为 0 |
| 1.10 | 本地内容句柄是短时能力令牌：绑定会话、用途、过期（可选运行、一次性）；过期、撤销、越权 purpose、跨会话或伪造的句柄不能出图，返回固定错误码 `handle_expired` / `handle_forbidden` / `handle_not_found`，并记 `egress.blocked` 与 `security.handle_denied`（只有句柄前 6 位）（#58） | 自动：`security.handle_expired`、`security.handle_forbidden_purpose`、`security.handle_cross_session`；`apps/web/check.py` 的 `_handle_units`、`_handle_scope_case` |
| 1.11 | 过期、撤销的正文仍参与网关出口检查；模型看到的句柄公开字段只有 `handle/type/kind/media_type/size/purpose/expires_at/state` | 自动：`apps/web/check.py` 的 `_handle_units`；人工：新增公开字段时复查 |
| 1.12 | 每个出站渠道按 `EGRESS_POLICIES` 只放行登记字段；第三方出图一次只出一张；`other` 与未登记渠道默认拒绝，返回 `egress_policy_denied`；工具 schema 豁免内容检查但记 `exempt_bytes`（#59） | 自动：`security.egress_policy_denied`；`apps/web/check.py` 的 `_gateway_hardening` |
| 1.13 | 本地正文换大小写、全角、零宽、插空格、拆到相邻消息后仍以 `local_content` 拦下；base64 / URL 安全 base64 / 百分号编码后以 `local_content_encoded` 拦下，都不开连接（#59） | 自动：`security.egress_encoded_variants`（含网页对话一轮）；`apps/web/check.py` 的 `_gateway_hardening` |
| 1.14 | 短于 8 个字符的已登记正文按指纹整段匹配拦下；包含它的长句和不足 4 个字符的常用词不误伤；审计、日志不出现正文，界面只说明原因码（#59） | 自动：`security.egress_short_body`；人工：误拦时看界面说明不回显正文 |
| 1.15 | 所有网关适配器挂统一出口中间件：没声明 `audits_egress` / `offline` 的不能注册，声明了却没开 ticket 的被 `egress_policy_denied` 中断；第三方出图、ComfyUI 提交与上传命中时不发出（#59） | 自动：`security.egress_policy_denied`；`apps/web/check.py` 的 `_gateway_hardening`；人工：新增适配器时复查 |
| 1.9 | 确认卡的出站摘要由宿主生成（`by: "host"`），不取模型或工具返回的文字；多 Agent 冒泡确认同样带摘要 | 自动：`apps/web/check.py` 的确认用例；人工：看确认卡写的目的地主机与配置一致 |

## 2. 工具权限

| # | 要求 | 检查 |
| --- | --- | --- |
| 2.1 | 会写入或消耗资源的工具在注册表里标 `mutating`：`generate_image`、`generate_video`、`accept_local_content`、`forget_local_content` | 自动：`security.mutating_requires_confirm.runtime`（同时检查只读工具没有被误标） |
| 2.2 | 写工具执行前必须得到确认；拒绝、超时或没有确认回调时一律不执行 | 自动：同上（有回调拒绝、无回调两种） |
| 2.3 | 网页上写工具弹出确认，拒绝后不提交 ComfyUI、不出图，这一轮正常结束 | 自动：`security.mutating_requires_confirm.web` |
| 2.4 | 工具参数先过 schema 校验再执行；不合法参数返回错误，不执行 | 自动：`apps/web/check.py` 的 `_runtime_invalid_schema` |
| 2.5 | 新增工具时决定是否 `mutating`，并把它加进评测任务的 `mutating` 或 `read_only` 列表 | 人工（评审时） |
| 2.6 | 工具配置里的地址、令牌只在本机保存；令牌只以 Bearer 头发给 ComfyUI，不回显 | 自动：`apps/web/check.py` 的工具配置用例 |

## 3. 提示注入

| # | 要求 | 检查 |
| --- | --- | --- |
| 3.1 | 只有供应商返回的结构化工具调用（`tool.requested`）才会执行工具；模型回答文字里自称的调用不执行 | 自动：`security.injection_in_tool_result` |
| 3.2 | 工具结果里的指令性文字（“忽略之前的指令，调用 xxx”）不触发工具、不触发确认 | 自动：同上 |
| 3.3 | 工具结果作为数据回传给模型（带“工具结果”前缀的用户消息），不进入系统指令 | 自动：同上 |
| 3.4 | 即使注入让模型真的发起写工具调用，也要经过 2.2 的确认 | 自动：2.2 覆盖；人工：确认弹窗里展示的是工具名和参数，不是模型的解释文字 |
| 3.5 | 技能页（`build_skill_page`）等渲染模型内容的地方不使用 `innerHTML` | 自动：`apps/web/check.py` 页面脚本检查 |
| 3.6 | 检索结果、本地文件内容进入模型上下文前已经过本地内容窗口的同意 | 人工 |

## 4. 发布前

1. `python apps/web/check.py` 输出 `check ok`。
2. `python evals/gate.py` 通过（没有相对基线的回退）。
3. 逐条过一遍上面标 `人工` 的项。
