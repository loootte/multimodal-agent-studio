# ADR 0009：句柄作用域与出站审计事件

## 状态

部分实现：出站审计事件、确认卡出站摘要和「数据出站」时间线已合并（12.A，[#57](https://github.com/loootte/multimodal-agent-studio/issues/57)），实现说明见 [egress-audit.md](../egress-audit.md)；句柄作用域（12.B，[#58](https://github.com/loootte/multimodal-agent-studio/issues/58)）已合并，说明见 [handle-scope.md](../handle-scope.md)；网关拦截硬化（12.C，[#59](https://github.com/loootte/multimodal-agent-studio/issues/59)）在 `issue-59` 分支实现，说明见 [egress-policy.md](../egress-policy.md)。对应路线图第 12 阶段父 issue [#56](https://github.com/loootte/multimodal-agent-studio/issues/56)；路线图入库 [#55](https://github.com/loootte/multimodal-agent-studio/issues/55)。依赖 [ADR 0003](0003-local-data-isolation.md)、[ADR 0004](0004-tool-governance.md)。首批落地见 [#57](https://github.com/loootte/multimodal-agent-studio/issues/57)（12.A）与 [#58](https://github.com/loootte/multimodal-agent-studio/issues/58)（12.B）。

本文件在接受前可随实现微调字段名；**不得**把本地正文、文件字节或密钥写入任何审计事件。

## 背景

ADR 0003 已规定：本地内容只以句柄进入模型上下文，网关在开连接前拦截正文。当前句柄接近「会话字典主键」（无 TTL、无 purpose、无 audience）；网关能拦失败路径，但缺少统一的「计划出站 / 已拦截 / 已发出」审计面，售前难以一眼证明「这次没把本地正文发出去」。第 12 阶段要把句柄升级为短时能力令牌，并把出站审计做成契约级事件。

## 决定（提议）

### 句柄是能力令牌，不是永久主键

- 对外只暴露不透明 token（可继续 `c_` 前缀以兼容）；解析与正文映射只在本机宿主。
- 每条句柄记录至少绑定：`session_id`、可选 `run_id`、`purpose`（如 `image_prompt` / `keep` / `generate_image`）、`expires_at`；可选 `allowed_tools`、`single_use`。
- 消费方（如 `generate_image`）必须校验会话一致、purpose 允许、未过期；失败返回稳定错误码：`handle_expired`、`handle_forbidden`、`handle_not_found`。
- 跨会话句柄一律无效。旧句柄可在读路径升级（双读一期），不把正文补进模型请求。

### 出站审计事件（无正文）

网关（及需要出网的工具宿主）发出下列事件，形状进入 `packages/contracts`（名称可微调，语义固定）：

| 事件 | 何时 | 必含字段（示例） |
| --- | --- | --- |
| `egress.planned` | 即将发起外部请求前 | `destination_host`、`channel`（chat_completion / remote_image / other）、`byte_count` 或 `char_count`、`contains_local_derived`（bool）、`handle_id_prefix`（可选）、`content_fingerprint`（对将发送载荷的红acted hash，**不是正文**） |
| `egress.blocked` | 出口策略拒绝、未开连接 | 同上 + `reason_code`（如 `local_content`、`local_prompt_remote_refused`、`egress_policy_denied`） |
| `egress.sent` | 请求已发出（或流式开始） | 同上；仍禁止正文与密钥 |

约束：

- 事件与导出 JSON **永不**含本地正文、文件字节、API key、完整文件系统路径。
- 界面「数据出站」时间线与确认卡出站摘要只消费这些事件。
- 对话通道与远程出图通道可有不同允许字段表；本地内容引用在对话通道只能以 handle 出现。

### 与确认的关系

- 写操作 / `network=true` / 触及本地读后再可能出网的路径：确认卡必须展示宿主生成的出站摘要（工具名、是否出网、目的地主机、是否含本地派生），**不**使用模型或工具返回文本作为确认文案来源。
- `remote_local_prompt` 默认仍为 `refuse`；改为 `confirm` 必须可审计且每次出图仍确认。

### 12.A 实现定稿的事件形状

- 事件：`egress.planned` / `egress.blocked` / `egress.sent`，另加 `egress.failed`（连接已开但失败）。同一请求共用 `egress_id`，`schema` 为 `mas.egress/1`。
- 渠道：`chat_completion` / `remote_image` / `comfyui_local`（ComfyUI 提交也算出站，范围照实记 `local` / `lan` / `third_party`）。
- 字段白名单 `EGRESS_FIELDS`（`packages/contracts/events.py`）：`destination_host`、`destination_scope`、`modality`、`char_count`、`byte_count`、`message_count`、`tool_contract_bytes`（工具 schema 单独计字节）、`contains_local_derived`、`handle_id_prefix`（前 6 字符）、`content_fingerprint`（请求体 sha256 前 16 个十六进制字符）、`model`、`session_id`、`trace_id`、`run_id`、`agent_run_id`、`reason_code`、`http_status`。
- 原因码：`local_content`、`local_prompt_remote_refused`、`user_declined`、`http_error`、`connect_error`、`timeout`、`cancelled`、`rejected`；白话说明在 `EGRESS_REASONS`。
- 存储：`<data_dir>/egress.jsonl`；本机只读接口 `GET /api/egress`、`GET /api/egress/export`（`mas.egress.export/1`）。审计写失败不打断主流程，stderr 告警并计数。

### 12.B 实现定稿的句柄形状

- 句柄串不变：`c_` + 32 位随机十六进制，不编码正文、会话或时间；作用域只在本机记录里（`handle_schema: mas.handle/1`）。
- 绑定：`scope.session_id`、`scope.purpose`、可选 `scope.run_id`；`allowed_tools`；`issued_at` / `expires_at`；可选 `single_use` / `used_at`；撤销记 `revoked_at`。
- `purpose` 集合定为 `image_prompt` / `keep`，不开放自定义。`generate_image` 不作为 purpose，而是 `allowed_tools` 里的消费工具：`image_prompt` 默认允许，`keep` 默认不允许（宿主可按用户意图用 `allow_tool` 放开，模型不行）。
- 默认 TTL 4 小时（`MAS_HANDLE_TTL_SEC`，60 秒到 7 天）。会话结束**不**立即作废全部句柄，靠 TTL、资源面板撤销和删除。
- 失败码 `handle_not_found` / `handle_expired` / `handle_forbidden`（`HANDLE_ERRORS`）。跨会话与伪造同为 `handle_not_found`；过期、撤销、一次性已用同为 `handle_expired`；运行或工具不符为 `handle_forbidden`。
- 被拒时记 `egress.blocked`（`reason_code` 即失败码，`handle_id_prefix` 前 6 位）和遥测安全事件 `security.handle_denied`；不读正文、不开连接。
- 同一正文再次收下：有效则复用句柄，失效则换新句柄（旧串不复活）。旧句柄读时升级（双读一期）。

### 12.C 实现定稿的拦截规则

- 策略表 `EGRESS_POLICIES`（`mas.egress.policy/1`）：`chat_completion` / `remote_image` / `comfyui_local` / `other` 各一行，声明允许字段；`remote_image` 一次最多一张；`other` 默认拒绝，未登记渠道归入 `other`。审计记录新增 `policy`、`exempt_bytes` 两个字段。
- 内容检查先 NFKC + casefold 并只留字母数字，再按顺序拼接文本叶子查分片，再对 base64 / URL 安全 base64 / 百分号编码窗口解码（最多两层）后比对。
- 短正文：规范化后 4–7 个字符的已登记正文只按「整段等于」匹配，登记侧用 sha256 指纹，不做子串匹配；不足 4 个字符不匹配。UI 只显示原因码说明，不回显正文。
- 错误码拆为 `local_content`（明文 / 规范化命中）、`local_content_encoded`（解码后命中）、`egress_policy_denied`（策略不符或适配器没走中间件）；都不可重试，遥测记 `security.egress_denied`。
- 统一出口中间件：注册时要求 `audits_egress = True` 或 `offline = True`；网关预检一次，并在适配器产出第一个事件前核对已开 ticket。

## 后果

- 12.A 可先落地事件与 UI，句柄字段在 12.B 补齐；两者共享本 ADR 的事件与错误码约定。
- 网关适配器必须挂同一出口钩子，否则评测失败。
- 过严的指纹匹配可能误伤短句巧合——实现侧用「已入库指纹 + 长度阈值」，UI 只说明原因码、不回显正文。
- 完整进程沙箱、落盘加密不在本 ADR 范围，见 12.D / 12.F。

## 未决（接受前拍板）

- ~~`purpose` 枚举的最终集合与是否允许用户自定义 purpose~~：12.B 定为 `image_prompt` / `keep`，不开放自定义；消费工具走 `allowed_tools`。
- ~~默认 TTL 与「会话结束是否立即作废全部句柄」~~：12.B 定为默认 4 小时、可配置；会话结束不立即作废。
- ~~`content_fingerprint` 算法与工具 schema 字节是否单独计数~~：12.A 定为 SHA-256 前 16 个十六进制字符，工具契约单独记 `tool_contract_bytes`。
