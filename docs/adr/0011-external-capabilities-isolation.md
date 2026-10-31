# ADR 0011：外部能力接入与长任务的隔离约束（第 14 阶段）

## 状态

提议（草稿）。对应路线图第 14 阶段父 issue [#73](https://github.com/loootte/multimodal-agent-studio/issues/73)，子 issue [#78](https://github.com/loootte/multimodal-agent-studio/issues/78)–[#84](https://github.com/loootte/multimodal-agent-studio/issues/84)。依赖 [ADR 0003](0003-local-data-isolation.md)、[ADR 0004](0004-tool-governance.md)、[ADR 0005](0005-multi-agent.md)、[ADR 0009](0009-handle-scope-and-egress-audit.md)。

## 背景

和 DeepSeek Harness（DSH）对比，MAS 在数据出站治理上领先，但在外部工具生态（MCP）、上下文压缩、通用规划、跨会话记忆、调度和后台任务、浏览器、评测回放上落后。这些能力都会新增数据进出的路径。本决定规定：补齐它们时哪些机制不能放松，新增的作用域和渠道怎么定义。

## 决定（提议）

### 不放松的机制

- manifest ∩ grant；社区工具与官方工具**不同权**。
- 出站一律经网关：`EGRESS_POLICIES` 字段白名单、编码变体检测、`local_fingerprint`、出站审计。
- 确认卡由宿主模板生成（`mas.confirm/1`）；工具结果、网页内容、外部 server 提供的描述全部标为不可信。
- `remote_local_prompt` 默认 `refuse`；`send_local_to_model` 硬禁止。
- 一次 `generate_image` 出一张，张数只由 `plan_images` 决定；不做「让模型写程序批量调用工具」的 Code 模式。

### 外部 MCP 是 `toolpack` 渠道

- MCP server 作为 `kind: mcp` 的工具包安装；stdio 进 12.D 沙箱，HTTP 走 `toolpack` 渠道出站。
- 远端工具默认不启用；声明 `network` 或 `read_local_content` 的只能 `confirm: always`。
- 远端工具描述或 schema 的摘要绑进授权记录，变化后要重新授权。
- 句柄只在本机宿主解析，对外部 server 是不透明字符串，不能被它消费。

> 实现说明（14.A #78，ADR 状态不变）：见 [docs/mcp-client.md](../mcp-client.md)。比本节更严的地方有四处。所有 MCP 包都 `confirm: always`。不收 `config`。HTTP 只收 https。`tools/call` 在连接前先查本地正文，查不到时也拒绝。
>
> #93 把当时待定的两处做成可配置，默认不放宽：stdio 命令白名单默认只有包内 Python；HTTP 的回环、链路本地、局域网默认都拒绝。认证头、参数黑名单和确认 / 本地正文 / 不可信结果 / 重新启用这几个开关也在同一份策略里，默认最严。见 [docs/tool-policy.md](../tool-policy.md)。本决定仍是提议。

### 句柄新增两种作用域

| purpose | 绑定 | TTL | 用途 |
| --- | --- | --- | --- |
| `memory` | 记忆条目 id | 不过期，可撤销 | content 类长期记忆；recall 时给当前会话重新签发会话句柄 |
| `scheduled` | 任务 id | `MAS_SCHEDULED_HANDLE_TTL_SEC`，默认 7 天 | 定时任务引用本地内容；任务修改或删除即失效 |

两种都不能跨会话或跨任务直接消费；失败沿用 `handle_expired` / `handle_forbidden` / `handle_not_found`。

### 记忆分级

- preference：非正文的偏好，可以进模型上下文，写入前过 `local_fingerprint`。
- content：含本地正文，只存句柄形态并加密，模型只见句柄和用户标签。
- 默认关闭；开启后每条写入要用户确认。

### 压缩与规划

- 压缩和裁剪产出的文字发出前过 `local_fingerprint`；`plan_images` 计划、未完成确认、句柄清单固定保留。
- todo 和 goal 的文字是模型可见文字，同样过指纹；todo 里的数量不算出图张数。

### 无人值守

- 定时和后台任务不降低任何确认等级：`confirm: always` 和 `acknowledge=high_risk` 永远不能预授权，遇到就挂起为「待确认」。
- 每个任务有步数、时间、token、每日次数上限。

> 实现说明（14.E #82，ADR 状态不变）：见 [docs/scheduler.md](../scheduler.md)。比本节更严的地方：到点从不直接运行，每次都进「待开始」队列，要用户点「开始本次」，超时记 `confirm_timeout`；错过不补（用户勾「补跑」才在启动后补一次）；总开关默认关。按用户决定（方案 C）加了一个窄例外：用户在面板经宿主确认卡、可以给单个出图任务开「常驻授权」，只免「内置 `generate_image` → 本机（loopback）ComfyUI、句柄就是该任务这一轮登记的那枚、不超过任务张数」的确认，绑定任务 id + `scheduled` 句柄 + ComfyUI 主机，改内容 / 过期 / 重新授权 / 删除 / 换主机即作废；模型不能设置；`acknowledge=high_risk`（远端出图）和其它 `confirm: always` 的调用仍然每次确认。

### 浏览器

- 只作为 MCP 沙箱工具包，默认关闭；禁止 `file://`、本机和局域网地址；往网页输入前过 `local_fingerprint`。

### 评测回放

- cassette 只录发往供应商的载荷，剔除鉴权头；入库前扫描密钥、本地正文指纹和绝对路径。

## 后果

- 出站渠道表、句柄 purpose 表和安全清单各增加条目；每个子阶段都带 `security.*` 用例进门禁。
- 第 9 阶段 #43 的定时入口复用 14.E 的调度器。
- 不做插件市场、Agent Teams 式自由消息协作、通用编码能力进核心。
