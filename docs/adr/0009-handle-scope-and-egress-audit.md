# ADR 0009：句柄作用域与出站审计事件

## 状态

部分实现：出站审计事件、确认卡出站摘要和「数据出站」时间线已合并（12.A，[#57](https://github.com/loootte/multimodal-agent-studio/issues/57)），实现说明见 [egress-audit.md](../egress-audit.md)；句柄作用域（12.B，[#58](https://github.com/loootte/multimodal-agent-studio/issues/58)）已合并，说明见 [handle-scope.md](../handle-scope.md)；网关拦截硬化（12.C，[#59](https://github.com/loootte/multimodal-agent-studio/issues/59)）、执行沙箱（12.D，[#60](https://github.com/loootte/multimodal-agent-studio/issues/60)）、强制确认与数据流图（12.E，[#61](https://github.com/loootte/multimodal-agent-studio/issues/61)）已合并，说明见 [egress-policy.md](../egress-policy.md)、[tool-sandbox.md](../tool-sandbox.md)、[sensitive-confirm.md](../sensitive-confirm.md)；本地存储加密与会话隔离（12.F，[#62](https://github.com/loootte/multimodal-agent-studio/issues/62)）在 `issue-62` 分支实现，说明见 [storage-encryption.md](../storage-encryption.md)。对应路线图第 12 阶段父 issue [#56](https://github.com/loootte/multimodal-agent-studio/issues/56)；路线图入库 [#55](https://github.com/loootte/multimodal-agent-studio/issues/55)。依赖 [ADR 0003](0003-local-data-isolation.md)、[ADR 0004](0004-tool-governance.md)。首批落地见 [#57](https://github.com/loootte/multimodal-agent-studio/issues/57)（12.A）与 [#58](https://github.com/loootte/multimodal-agent-studio/issues/58)（12.B）。

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

### 12.D 实现定稿的沙箱

细节见 [tool-sandbox.md](../tool-sandbox.md)。

- 第三方包每次调用起一个子进程（`python -I -B -X utf8 poc/tools/sandbox_runner.py`），JSON 行协议；子进程装 `sys.addaudithook` 钩子强制权限：没 `network` 拦所有连接和域名解析；写只许临时目录和（授权 `write_files` 后）`<data_dir>/toolpack_data/<名字>/`；数据目录禁读禁列；起进程、ctypes 拒绝。超时 `MAS_TOOLPACK_TIMEOUT_SEC`（默认 30 秒）。
- 环境变量白名单 + 配置显式注入：子进程拿不到 API key、令牌；密钥只经 `config`。
- 出网：子进程每次连接经 IPC 问宿主。宿主裁决（无 `network` → `permission_denied`；回环 / 未指定 / 链路本地 / 解析不出 → `egress_policy_denied`；其余放行）并记 12.A 审计，新渠道 `toolpack`（策略不透明：只看权限和目的地，不看载荷），记录新增 `tool` 字段。
- 越权结果：`permission_denied` 带 `violations`（`network` / `write_files` / `read_local_content` / `process` / `native`），不写 `executed`，遥测 `security.sandbox_denied`。另有 `tool_timeout`、`tool_error`、`sandbox_error`；进程内装载第三方代码报 `sandbox_required`。
- 运行时二次核对：授权记录升级为 `mas.grant/2`（权限 + 版本 + 包内容摘要）；缺失、旧格式、内容被改都是 `permission_denied`（`grant: missing / outdated / changed`）。生效权限 = 声明 ∩ 授权。内置工具收紧为名单 `HOST_TOOLS` / `BUILTIN_PYTHON`（各自写明需要的权限，需要 ⊆ 声明 ⊆ 授权），内置模块必须在 `poc/tools/internal/`。
- 安装前权限审阅：`inspect` 返回 `review`，网页逐项勾选后才能安装。
- 例外：内置工具不进沙箱；`generate_image` / `generate_video` 访问本机 ComfyUI 是唯一允许的本机出网，仍走 12.A 审计和 12.C 策略。
- 局限：审计钩子是 Python 层尽力而为，C 扩展可绕过；操作系统级隔离（Job Object / seccomp / namespace）和第三方包的句柄 API 留作后续。

### 12.E 实现定稿的强制确认与数据流图

细节见 [sensitive-confirm.md](../sensitive-confirm.md)。

- 清单校验：`network` 或 `read_local_content` 为真的包只能 `confirm: "always"`，`never` / `by_args` 在检查、安装、发现时都报 `invalid_manifest`（`SENSITIVE_CONFIRM_PROBLEM`）。例外只给内置只读句柄工具 `list_local_content` / `retrieve_local`（`HANDLE_ONLY_READERS`）。`needs_confirm` 对敏感包兜底返回需要确认。
- 确认粒度：内置工具一轮一次不变（N 张图问一次）；敏感的第三方包每次调用都问（`confirm_every_call`，含子 Agent 冒泡）；本地提示词发第三方的同意绑定（这一轮, 句柄, 目的主机），拒绝不记住，下一轮重问。
- 确认卡固定区块 `egress.fixed`：出站主机、含本地派生内容、句柄用途三项，宿主生成、顺序固定。
- 放宽隐私的配置（目前 `generate_image.remote_local_prompt = confirm`）登记在 `GUARDED`：设置页明示「本地提示词将发往第三方出图」，接口不带 `acknowledge` 返回 409 `acknowledge_required`；每次变更记遥测 `security.policy_changed`（只有工具、字段、前后取值）。配置变更不走工具。
- 审计记录的 `tool` 字段从 `toolpack` 渠道扩到所有经工具发起的出站（`generate_image` 的 `remote_image` / `comfyui_local` 也标）。
- 会话级数据流图 `GET /api/dataflow`（只给本机，`mas.dataflow/1`）：本地内容 → 句柄（前 6 位）→ 工具 → 本机 ComfyUI / 局域网 / 第三方，边上记 sent / blocked / failed、本地派生次数和原因码；只来自审计记录和句柄公开字段，没有正文和完整句柄。
- 不做：轨迹里的隐私边类型（#44 反向用例，issue 标为可选）。

### 12.F 实现定稿的本地存储加密与会话隔离

细节、备份与恢复手册见 [storage-encryption.md](../storage-encryption.md)。

- 模式 `MAS_STORAGE_ENCRYPTION`：默认明文（兼容、且丢密钥即丢数据，要用户显式开）；`on` 加密；`memory` 仅内存会话。数据目录有 `storage.json`（`mas.storage/1`，记 `key_id`）后永远按加密打开，环境变量降级不了。
- AES-256-GCM（可选依赖 `cryptography`，缺了报 `storage_crypto_unavailable`，不回落明文）。会话密钥 = HKDF-SHA256(主密钥, `mas/session/<id>`)；会话文件 `mas.enc/1` 信封，头部作 AAD 绑会话；字节按会话存 `artifacts/sealed/<会话>/<HMAC 名>`，AAD 绑 `key_id|会话|sha256`，加密时不跨会话去重、文件名不暴露明文哈希。
- 主密钥：`MAS_MASTER_KEY` → `MAS_MASTER_KEY_FILE` → `~/.mas/master.key`（0600、不覆盖）；不许在数据目录或仓库（`storage_key_location`）；12.D 沙箱禁读密钥目录。口令备份 `mas.keybackup/1`（scrypt + AES-GCM）。导入导出只走命令行 `apps/web/storage_tool.py`，`GET /api/storage` 只读、只给本机、不带密钥和路径。
- 有明文没迁移时开 `on` 报 `storage_migration_required`；迁移先写好密文和标记再删明文。
- 网关 / 运行时不变，只见解密后的本机对象；遥测 `security.storage_key_created` / `storage_decrypt_failed` / `storage_migrated`，无密钥、正文、路径。
- 不做：生成图与 ComfyUI 输出加密、OS 钥匙串 / DPAPI、安全擦除。

## 后果

- 12.A 可先落地事件与 UI，句柄字段在 12.B 补齐；两者共享本 ADR 的事件与错误码约定。
- 网关适配器必须挂同一出口钩子，否则评测失败。
- 过严的指纹匹配可能误伤短句巧合——实现侧用「已入库指纹 + 长度阈值」，UI 只说明原因码、不回显正文。
- 12.D 的子进程沙箱见上；操作系统级隔离不在本 ADR 范围。落盘加密见 12.F：开启后丢主密钥和备份就无法恢复数据，生成图片仍是明文。
- 12.E 让 `network` / 读本地的第三方包不能再 `confirm=never`：12.E 之前装好的这类包升级后要改清单重装。

## 未决（接受前拍板）

- ~~`purpose` 枚举的最终集合与是否允许用户自定义 purpose~~：12.B 定为 `image_prompt` / `keep`，不开放自定义；消费工具走 `allowed_tools`。
- ~~默认 TTL 与「会话结束是否立即作废全部句柄」~~：12.B 定为默认 4 小时、可配置；会话结束不立即作废。
- ~~`content_fingerprint` 算法与工具 schema 字节是否单独计数~~：12.A 定为 SHA-256 前 16 个十六进制字符，工具契约单独记 `tool_contract_bytes`。
