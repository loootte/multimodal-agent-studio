# 出站策略与拦截硬化（12.C）

对应 issue [#59](https://github.com/loootte/multimodal-agent-studio/issues/59)，父 issue [#56](https://github.com/loootte/multimodal-agent-studio/issues/56)，决定见 [ADR 0009](adr/0009-handle-scope-and-egress-audit.md)。审计事件本身见 [egress-audit.md](egress-audit.md)，句柄作用域见 [handle-scope.md](handle-scope.md)。

## 一句话

每个出站渠道都有一张字段白名单；请求先过策略，再把文本叶子做规范化、分片拼接和编码解码后，与本会话的本地正文比对。命中就记 `egress.blocked` 并且不开连接。所有适配器都必须挂同一个出口中间件 `services.gateway.egress.guard`，没挂的既不能注册，也过不了网关的运行时核对。

## 策略表（`packages/contracts/events.py` 的 `EGRESS_POLICIES`，`mas.egress.policy/1`）

| 渠道 | 允许的顶层字段 | 其他约束 |
|---|---|---|
| `chat_completion` | `model` `stream` `messages` `tools` `tool_choice` `temperature` `max_tokens` `stop` | 消息只能有 `role` `content` `tool_calls` `tool_call_id` `name`；`tools` 豁免内容检查，但字节数记进 `exempt_bytes`；本地内容只能以句柄出现 |
| `remote_image` | `model` `prompt` `n` `size` `response_format` | `n` 最多 1（一次 `generate_image` 只出一张） |
| `comfyui_local` | `prompt` `client_id` | 上传原图（`raw_upload`）是唯一允许携带本地字节的出口，范围仍照实记 |
| `other` | 无 | 默认拒绝；没登记的渠道名一律归到这里 |

策略外的字段、一次多张、`other` 渠道都返回 `egress_policy_denied`。

## 内容检查（`egress.inspect`）

1. **规范化**：NFKC + casefold，只保留 Unicode 字母、数字、组合符号。大小写、全角、零宽字符、插空格、换标点都还原成同一串。
2. **分片拼接**：按顺序把所有文本叶子拼起来再查一次，挡住把正文拆到相邻几条消息或几段里。
3. **编码窗口**：找出长度 ≥ 12 的 base64（标准和 URL 安全）和百分号编码片段，解码后再规范化比对，最多解两层。`data:` URL 不解码（图片走句柄和 ComfyUI 上传）；超过 256 KB 的叶子只做明文检查。
4. **短正文**：规范化后不足 8 个字符的已登记正文，只在某个文本叶子**整段**等于它时命中，不做子串匹配，以免误伤。登记侧只存 `sha256(规范化正文)` 的前 32 位（`short_fingerprint`），审计与日志都不写正文。规范化后不足 4 个字符的不参与（如「好的」）。
5. **结构字段**：`role`、`model`、`type` 等结构键的值不参与比对。

## 错误码

| 码 | 什么时候 | 界面说明（不回显正文） |
|---|---|---|
| `local_content` | 明文或规范化后命中本地正文（含分片、工具参数） | 请求里混进了本地内容，已在本机拦下 |
| `local_content_encoded` | 解码 base64 / 百分号编码后命中 | 请求里有编码过的本地内容，已在本机拦下 |
| `egress_policy_denied` | 字段不在白名单、一次多张、未登记渠道、适配器没走出口中间件 | 这次出站不符合出站策略，没有发出 |

三者都不可重试，遥测失败分类为 `policy`，并记安全事件 `security.egress_denied`（只有渠道和原因码）。

## 统一出口中间件

- 注册：`registry.register` 只收 `audits_egress = True`（真联网，走 `egress.guard`）或 `offline = True`（不联网的测试替身）的适配器，否则抛 `ValueError`。
- 预检：`services.gateway.stream` 在交给适配器前先对规范化请求做一次 `inspect`，命中直接拦下。
- 运行时核对：非 offline 的适配器在产出第一个事件前必须已经开过 ticket，否则记 `egress_policy_denied` 并中断，输出不交出去。
- 第三方出图（`apps/web/remote_image.py`）和 ComfyUI 客户端（提交、上传）命中时同样不发出。

## 验证

- `python3 apps/web/check.py`：`_gateway_hardening`（规范化 / 编码变体、策略表、豁免字节、短正文指纹、注册拒绝、没开 ticket 的适配器、预检不开连接、审计无正文）。
- `python3 evals/run.py --only security`：`security.egress_encoded_variants`、`security.egress_short_body`、`security.egress_policy_denied`，已进 `evals/baseline.json` 门禁。

## 已知限制

- `offline` 是自我声明，靠代码评审和测试保证真不联网。
- 运行时核对在适配器开始执行后才生效；预检和 `guard` 才是开连接前的拦截。
- 不解十六进制、ROT13、自定义压缩等编码；中间插了无关消息的分片不再拼接。
- 规范化后不足 4 个字符的正文不匹配；4–7 个字符只做整段匹配。
- 第三方工具包自己开 socket 的情况要到 12.D 进程隔离才拦得住。
